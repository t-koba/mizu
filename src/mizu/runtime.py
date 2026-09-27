"""The execution mechanism: bounded work, immutable inputs and explicit commit points.

There is no research/plan/implement state machine here. The role policy decides
what work is valuable. This module only validates authority and records outcomes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import difflib
import json
import os
import shutil
import signal
import tempfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .budget import Budget
from .config import Config, Role, load
from .doctor import container_runtime
from .drivers import driver_for
from .errors import Busy, Cancelled, ConfigError, Denied, LimitExceeded, MizuError
from .fs import atomic_write, canonical, digest, lock, mkdir, now, read_json, safe_read, write_json, PREVIEW_BYTES
from .project import Project
from .protocol import DEFINITIONS, validate
from .report import publish
from .sandbox import Sandbox, cleanup
from .web import Web
from .web_worker import bounded


@contextlib.contextmanager
def slot(config: Config):
    for index in range(config.limits.parallel_runs):
        manager = lock(config.data / "slots" / f"{index}.lock", blocking=False)
        try:
            manager.__enter__()
        except Busy:
            continue
        try:
            yield
        finally:
            manager.__exit__(None, None, None)
        return
    raise Busy("All configured execution slots are in use")


class Context:
    def __init__(self, config: Config, project: Project, role: Role, run_dir: Path,
                 snapshot: dict, workspace: Path, *, stop: threading.Event | None = None,
                 consult=None, goal: str | None = None):
        self.config, self.project, self.role = config, project, role
        self.run_dir, self.snapshot, self.workspace = run_dir, snapshot, workspace
        self.goal = (project.goal if role.workspace == "write" else snapshot["goal"]) if goal is None else goal
        self.goal_digest = digest(self.goal.encode())
        # Capture the inputs actually offered to this run, not arrivals at publication.
        self.inbox_seen = project.insights.generation()
        self.wake_generation = project.control().get("wake_generation", "")
        self.stop = stop or threading.Event()
        self.deadline = time.monotonic() + config.limits.run_seconds
        self.mutex = threading.RLock()
        self.hello = threading.Event()
        self.request_count = 0
        self.request_sequences: set[int] = set()
        self.tool_count = 0
        self.finished: dict | None = None
        self.admission_error: str | None = None
        self.closed = False
        self.verification: dict | None = None
        self.commentary: dict | None = None
        self.consult = consult
        self.sandbox = Sandbox(config, project.root, role.name, run_dir, cancel=self.cancelled)
        self.web = Web(config.web, config.data / "web-cache", run_dir / "sources")

    def cancelled(self) -> bool:
        control = self.project.control()
        return (self.stop.is_set() or self.closed or not control.get("armed") or control.get("paused")
                or time.monotonic() > self.deadline)

    def handle(self, operation: str, arguments: dict) -> dict:
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise Denied("Invalid operation")
        with self.mutex:
            if self.cancelled():
                raise Cancelled("Run is stopped, paused or past its deadline")
            if operation == "_hello":
                if arguments != {"protocol": 1}:
                    raise Denied("Unsupported bridge handshake")
                self.hello.set()
                return {"protocol": 1}
            if operation == "_budget":
                try:
                    sequence = arguments.get("sequence")
                    if type(sequence) is not int or sequence < 1:
                        raise Denied("Invalid request sequence")
                    if self.finished is not None:
                        raise Denied("No model requests are permitted after finish")
                    if sequence not in self.request_sequences:
                        if self.request_count >= self.config.limits.requests_per_run:
                            raise LimitExceeded("Per-run provider request budget exhausted")
                        Budget(self.config.data / "budget", self.config.limits.daily_requests).take(
                            f"{self.run_dir.name}:{sequence}")
                        self.request_sequences.add(sequence)
                        self.request_count += 1
                        write_json(self.run_dir / "admission.json", {"requests": self.request_count})
                    return {"admitted": True}
                except MizuError as exc:
                    self.admission_error = str(exc)
                    raise
            if self.finished is not None:
                raise Denied("This work unit is sealed; no tools may run after finish")
            if operation not in self.role.capabilities:
                raise Denied(f"Role has no capability: {operation}")
            self.tool_count += 1
            if self.tool_count > self.config.limits.tools_per_run:
                raise LimitExceeded("Per-run tool limit exceeded")
            validate(arguments, DEFINITIONS[operation][1])
            result = self.dispatch(operation, arguments)
            return result

    def dispatch(self, op: str, args: dict) -> dict:
        if op == "diff":
            return self.project.snapshots.changes(self.snapshot["id"])
        if op == "files":
            if self.role.workspace == "none":
                return {"files": []}
            if self.role.workspace != "write":
                return {"files": sorted(self.snapshot["files"])}
            names = []
            for directory, dirs, files in os.walk(self.workspace, followlinks=False):
                rel = Path(directory).relative_to(self.workspace)
                dirs[:] = sorted(d for d in dirs if not self.project.snapshots.excluded((rel / d).as_posix()))
                for name in sorted(files):
                    path = (rel / name).as_posix()
                    if not self.project.snapshots.excluded(path):
                        names.append(path)
                    if len(names) >= self.config.limits.snapshot_files:
                        return {"files": names, "truncated": True}
            return {"files": names}
        if op == "read":
            if self.role.workspace == "none" or self.project.snapshots.excluded(args["path"]):
                raise Denied("Path is not exposed to this role")
            data = safe_read(self.workspace, args["path"], min(self.config.limits.file_bytes, PREVIEW_BYTES))
            return {"path": args["path"], "text": data.decode("utf-8", "replace")}
        if op in ("exec", "experiment"):
            if self.role.workspace == "none":
                raise Denied("This role has no workspace")
            if op == "exec" and self.role.workspace == "write":
                self.verification = None
            result = self.sandbox.execute(self.workspace, args["script"],
                                          writable=self.role.workspace == "write", experiment=op == "experiment")
            if op == "experiment":
                science = {key: args[key] for key in ("question", "comparison", "measure")}
                write_json(self.run_dir / "experiments" / f"{result['id']}.json", {**science, "command": result})
            return result
        if op == "verify":
            if not self.project.verify:
                raise Denied("No operator acceptance commands are configured")
            before = self.project.snapshots.capture_files(self.workspace)
            results = [self.sandbox.execute(self.workspace, command, writable=True)
                       for command in self.project.verify]
            after = self.project.snapshots.capture_files(self.workspace)
            passed = all(r["exit_code"] == 0 and r["reason"] == "exited" for r in results)
            representable = not before["skipped"] and not after["skipped"]
            unchanged = before["code_digest"] == after["code_digest"]
            self.verification = {"passed": passed and unchanged and representable,
                                 "snapshot_representable": representable,
                                 "code_digest": after["code_digest"], "image": self.config.sandbox.image,
                                 "unchanged_during_verification": unchanged,
                                 "commands": [{"id": r["id"], "exit_code": r["exit_code"], "reason": r["reason"],
                                               "script": r["script"]} for r in results],
                                 "run": self.run_dir.name, "created_at": now()}
            write_json(self.run_dir / "verification.json", self.verification)
            return self.verification
        if op == "fetch":
            return bounded(self.config, self.run_dir, "fetch", args["url"], self.cancelled)
        if op == "search":
            return bounded(self.config, self.run_dir, "search", args["query"], self.cancelled)
        if op == "insights":
            return self.project.insights.read(args["id"]) if args.get("id") else {"insights": self.project.insights.list()}
        if op == "decide":
            return self.project.insights.decide(args["id"], args["action"], args["reason"],
                                                args.get("revisit", ""), self.run_dir.name)
        if op == "submit_insight":
            return self.project.insights.submit(source=self.role.name, title=args["title"], body=args["body"],
                                                 base_snapshot=self.snapshot["id"], run=self.run_dir.name)
        if op == "consult":
            if self.consult is None:
                raise Denied("Nested consultation is disabled")
            return self.consult(self, args)
        if op == "report":
            self.commentary = args
            write_json(self.run_dir / "commentary.json", args)
            return {"staged": True}
        if op == "finish":
            if not args["summary"].strip():
                raise Denied("A nonempty work summary is required")
            if self.role.workspace == "write":
                if not args.get("state", "").strip():
                    raise Denied("Writable roles must provide the updated short state")
                if args["outcome"] == "done" and not (self.verification and self.verification["passed"]):
                    raise Denied("Completion requires passing the configured acceptance commands")
            wait = min(args.get("wait_seconds", self.config.limits.default_wait_seconds),
                       self.config.limits.maximum_wait_seconds)
            self.finished = {**args, "wait_seconds": wait, "recorded_at": now()}
            write_json(self.run_dir / "finish-intent.json", self.finished)
            return {"sealed": True}
        raise Denied("Unknown operation")


def prompt_for(context: Context) -> str:
    caps = set(context.role.capabilities)
    # Mechanism interprets grants: only offer actionable context. Roles without
    # insights/decide cannot fetch proposal bodies via tools, so listing pending
    # titles would be un-actionable tokens. Roles without verify cannot run
    # acceptance commands. Exact snapshot references are always pinned.
    pending = context.project.insights.list() if ("insights" in caps or "decide" in caps) else []
    acceptance = list(context.project.verify) if "verify" in caps else []
    return json.dumps({"protocol": 1, "goal": context.goal,
                       "published_snapshot": {k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "pending_insights": pending,
                       "acceptance_commands": acceptance},
                      ensure_ascii=False)


class Engine:
    def __init__(self, config: Config, *, driver=None, stop: threading.Event | None = None):
        self.config = config
        self.driver = driver
        self.stop = stop or threading.Event()
        self._drivers: dict[str, object] = {}

    def resolve(self, profile: str):
        """Return the driver for a profile's engine (registry) or the injected test driver."""
        if self.driver is not None:
            return self.driver
        return driver_for(self.config, profile, self._drivers)

    def run(self, project: Project, role_name: str) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        control = project.control()
        if not control.get("armed") or control.get("paused"):
            raise Denied("Project is unarmed or paused")
        usage = Budget(self.config.data / "budget", self.config.limits.daily_requests).usage()
        if usage["limit"] <= usage["used"]:
            raise LimitExceeded("UTC daily model-request budget unavailable")
        if shutil.disk_usage(project.root).free < self.config.limits.free_disk_mb * 1048576:
            raise LimitExceeded("Free disk space is below the configured reserve")
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(project.root / "locks" / f"run-{role_name}.lock", blocking=False))
            if role.workspace == "write":
                stack.enter_context(lock(project.root / "locks" / "workspace.lock", blocking=False))
            stack.enter_context(slot(self.config))
            project.insights.ingest_editor()
            snapshot = project.snapshots.get()
            cursor = read_json(project.root / "observed" / f"{role_name}.json", {})
            if role.on_change and cursor.get("snapshot") == snapshot["id"]:
                return {"skipped": "unchanged", "snapshot": snapshot["id"]}
            active = project.root / "active" / f"{role_name}.json"
            driver = self.resolve(role.profile)
            if getattr(driver, "requires_sandbox", True) and \
                    any(c in role.capabilities for c in ("exec", "experiment", "verify")):
                container_runtime(self.config)
                cleanup(self.config, project.root, role_name)
            run_id = uuid.uuid4().hex
            run_dir = project.root / "runs" / run_id
            mkdir(run_dir)
            write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
            write_json(run_dir / "started.json", {"run": run_id, "role": role_name,
                        "snapshot": snapshot["id"], "started_at": now(),
                        "config_sha256": digest(self.config.file.read_bytes()),
                        "policy_sha256": digest(role.policy.read_bytes())})
            workspace = project.workspace
            if role.workspace != "write":
                workspace = run_dir / "input"
                if role.workspace == "none":
                    mkdir(workspace)
                else:
                    project.snapshots.materialize(snapshot, workspace)
            context = Context(self.config, project, role, run_dir, snapshot, workspace,
                              stop=self.stop, consult=self.consult)
            try:
                prompt = prompt_for(context)
                decoded = json.loads(prompt)
                write_json(run_dir / "prompt_projection.json",
                           {"run": run_id, "role": role_name, "snapshot": snapshot["id"],
                            "recent_snapshots": len(decoded.get("recent_snapshots", [])),
                            "pending_insights": len(decoded.get("pending_insights", [])),
                            "acceptance_commands": len(decoded.get("acceptance_commands", [])),
                            "prompt_bytes": len(prompt.encode("utf-8")), "created_at": now()})
                model_result = driver.execute(context, prompt)
                if context.cancelled():
                    raise Cancelled("Run stopped before publication")
                if context.finished is None:
                    raise Denied("Missing finish intent")
                finished = context.finished
                if role.workspace == "write":
                    captured = project.snapshots.capture_files(workspace)
                    verification = context.verification
                    if verification and verification["code_digest"] != captured["code_digest"]:
                        verification = {**verification, "passed": False, "invalidated": "code changed after verification"}
                    if finished["outcome"] == "done" and not (verification and verification["passed"]):
                        raise Denied("Code changed after verification; completion refused")
                    snapshot = project.snapshots.create(
                        captured, goal=context.goal, state=finished["state"], run=run_id,
                        outcome=finished["outcome"], summary=finished["summary"], verification=verification,
                        wake_at=time.time() + finished["wait_seconds"] if finished["outcome"] == "wait" else None,
                        inbox_seen=context.inbox_seen,
                        wake_generation=context.wake_generation)
                result = {"run": run_id, "role": role_name, "status": "prepared", "finished_at": now(),
                          "finish": finished, "model": model_result, "snapshot": snapshot["id"]}
                write_json(run_dir / "result.json", result)
                if role.workspace == "write":
                    project.snapshots.publish(snapshot)
                if context.commentary is not None:
                    result["artifact"] = publish(project, snapshot, context.commentary, run_id=run_id)
                result["status"] = "completed"
                write_json(run_dir / "result.json", result)
                write_json(project.root / "observed" / f"{role_name}.json", {"snapshot": snapshot["id"]})
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": 0, "last_run": run_id, "updated_at": now()})
                return result
            except BaseException as exc:
                # No reset --hard, no inferred success from an unfinished tool call.
                write_json(run_dir / "error.json", {"run": run_id, "role": role_name,
                           "status": "interrupted", "error": str(exc), "finished_at": now()})
                health = read_json(project.root / "health" / f"{role_name}.json", {})
                failures = health.get("consecutive_failures", 0) + 1
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": failures, "last_run": run_id, "error": str(exc), "updated_at": now()})
                if failures >= self.config.limits.max_failures and not isinstance(exc, Cancelled):
                    project.set_control(paused=True, reason="Repeated failures; inspect health and run records")
                raise
            finally:
                context.closed = True
                active.unlink(missing_ok=True)
                # Read-only inputs are reproducible from the content-addressed snapshot.
                if role.workspace != "write":
                    shutil.rmtree(workspace, ignore_errors=True)

    def consult(self, parent: Context, args: dict) -> dict:
        names = args.get("profiles", list(self.config.consult_profiles))
        if not names or any(n not in self.config.consult_profiles for n in names) or len(set(names)) != len(names):
            raise Denied("Consultation requires unique, operator-allowlisted model profiles")
        role_name = args.get("role") or "consult"
        if role_name not in self.config.roles:
            raise ConfigError(f"Consultation role is not configured: {role_name}")
        role = self.config.roles[role_name]
        if role.workspace != "read" or set(role.capabilities) - {"files", "read", "finish"}:
            raise ConfigError(f"Consultation role {role_name} must be read-only with only files/read/finish")
        capture = parent.project.snapshots.capture_files(parent.workspace)
        snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                              run=parent.run_dir.name, outcome="wait", summary="Consultation input")

        def ask(name: str) -> dict:
            try:
                with slot(self.config):
                    run_dir = parent.project.root / "runs" / uuid.uuid4().hex
                    mkdir(run_dir)
                    workspace = run_dir / "input"
                    parent.project.snapshots.materialize(snap, workspace)
                    ctx = Context(self.config, parent.project, dataclasses.replace(role, profile=name, persistent=False),
                                  run_dir, snap, workspace, stop=parent.stop, goal=parent.goal)
                    ctx.deadline = min(ctx.deadline, parent.deadline)
                    try:
                        model = self.resolve(name).execute(ctx, prompt_for(ctx) + "\n\nQuestion:\n" + args["question"], profile=name)
                        result = {"profile": name, "snapshot": snap["id"], "answer": ctx.finished["summary"], "model": model}
                        write_json(run_dir / "consultation.json", result)
                        return result
                    finally:
                        ctx.closed = True
                        shutil.rmtree(workspace, ignore_errors=True)
            except Exception as exc:
                return {"profile": name, "snapshot": snap["id"], "error": str(exc)}
        with ThreadPoolExecutor(max_workers=self.config.limits.parallel_consults) as pool:
            results = list(pool.map(ask, names))
        record = {"snapshot": snap["id"], "question": args["question"], "role": role.name, "answers": results}
        write_json(parent.run_dir / "consultations" / f"{uuid.uuid4().hex}.json", record)
        return record


def should_run(project: Project, snapshot: dict, current_time: float | None = None) -> bool:
    current_time = time.time() if current_time is None else current_time
    control = project.control()
    if not control.get("armed") or control.get("paused"):
        return False
    if digest(project.goal.encode()) != snapshot["goal_digest"]:
        return True
    if control.get("wake_generation", "") != snapshot.get("wake_generation", ""):
        return True
    outcome = snapshot["outcome"]
    if outcome == "done":
        return False  # A completed objective needs an explicit wake or changed goal.
    if outcome == "continue":
        return True
    if project.insights.generation() != snapshot.get("inbox_seen", ""):
        return True
    return outcome == "wait" and snapshot.get("wake_at") is not None and current_time >= snapshot["wake_at"]


def daemon(config: Config, name: str, role_name: str) -> None:
    stop = threading.Event()
    def stopped(signum, frame):
        stop.set()
    watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if sig is not None]
    old = {s: signal.signal(s, stopped) for s in watched}
    try:
        with lock(config.data / "locks" / f"daemon-{name}-{role_name}.lock", blocking=False):
            while not stop.is_set():
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    project = Project(config, name)
                    project.insights.ingest_editor()
                    if should_run(project, project.snapshots.get()):
                        result = Engine(config, stop=stop).run(project, role_name)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        stop.wait(config.limits.cooldown_seconds)
                        continue
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                stop.wait(config.limits.idle_seconds)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
