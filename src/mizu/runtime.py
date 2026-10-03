"""The execution mechanism: bounded work, immutable inputs and explicit commit points.

There is no research/plan/implement state machine here. The role policy decides
what work is valuable. This module only validates authority and records outcomes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import platform as _platform
from .budget import Budget
from .config import Config, Role, load
from .doctor import container_runtime
from .drivers import driver_for
from .errors import Busy, Cancelled, ConfigError, Denied, LimitExceeded, MizuError
from .fs import canonical, digest, lock, mkdir, now, read_json, safe_read, write_json, PREVIEW_BYTES, page, text_preview, DIGEST
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


#: Capabilities that let a consultation role change shared state or execute
#: code. Consultation must stay read-only and advisory: workspace "read" plus
#: none of these. Read-only grants (files/read/diff/insights/fetch/search)
#: stay operator choice, plus the required finish.
CONSULT_FORBIDDEN = frozenset({"exec", "experiment", "verify", "decide",
                               "submit_insight", "consult", "report"})


def check_consult_role(role_name: str, role) -> None:
    """Single property-based consultation gate (runtime + smoke share this).

    Schema: role has ``workspace`` and ``capabilities``. Bounds: none beyond
    the capability sets. Trust: configuration, not model output.
    Retry/cancellation: n/a (pre-model validation). Evidence: ConfigError names
    the role. Failure: raises ConfigError before any model call or budget use.
    """
    if role.workspace != "read" or set(role.capabilities) & CONSULT_FORBIDDEN:
        raise ConfigError(
            f"Consultation role {role_name} must be read-only without "
            f"{sorted(CONSULT_FORBIDDEN)}")


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
        self.runtime_usage: dict[int, dict] = {}
        self.runtime_models: dict[int, dict] = {}
        self.tool_count = 0
        self.finished: dict | None = None
        self.admission_error: str | None = None
        self.closed = False
        self.operations_stopped = threading.Event()
        self.verification: dict | None = None
        self.commentary: dict | None = None
        self.consult = consult
        self.ephemeral = False
        self.sandbox = Sandbox(config, project.root, role.name, run_dir, cancel=self.tools_cancelled)
        self.web = Web(config.web, config.data / "web-cache", run_dir / "sources")

    def cancel_operations(self):
        self.operations_stopped.set()

    def tools_cancelled(self):
        return self.operations_stopped.is_set() or self.cancelled()

    def cancelled(self) -> bool:
        control = self.project.control()
        return (self.stop.is_set() or self.closed or not control.get("armed") or control.get("paused")
                or time.monotonic() > self.deadline)

    def handle(self, operation: str, arguments: dict) -> dict:
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise Denied("Invalid operation")
        with self.mutex:
            if self.tools_cancelled():
                raise Cancelled("Run is stopped, paused or past its deadline")
            if operation == "_hello":
                if arguments != {}:
                    raise Denied("Unsupported bridge handshake")
                self.hello.set()
                return {}
            if operation == "_model_usage":
                sequence = arguments.get("sequence")
                usage = arguments.get("usage")
                if type(sequence) is not int or sequence < 1 or not isinstance(usage, dict):
                    raise Denied("Invalid runtime usage")
                if len(canonical(arguments)) > 65536 or len(self.runtime_usage) >= 4096:
                    raise LimitExceeded("Runtime usage evidence bound exceeded")
                self.runtime_usage.setdefault(sequence, usage)
                if isinstance(arguments.get("model"), dict):
                    self.runtime_models.setdefault(sequence, arguments["model"])
                write_json(self.run_dir / "runtime-usage.json", {"observations": [
                    {"sequence": seq, "usage": value, "model": self.runtime_models.get(seq)}
                    for seq, value in self.runtime_usage.items()]})
                return {"recorded": True}
            if operation == "_engine_tool":
                if self.finished is not None:
                    raise Denied("This work unit is sealed")
                if arguments.get("name") not in self.role.engine_tools:
                    raise Denied("Engine tool has no explicit grant")
                self.tool_count += 1
                if self.tool_count > self.config.limits.tools_per_run:
                    raise LimitExceeded("Per-run tool limit exceeded")
                return {"allowed": True}
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
                        try:
                            Budget(self.config.data / "budget", self.config.limits.daily_requests,
                                   self.config.limits.retention_days).take(f"{self.run_dir.name}:{sequence}")
                        except LimitExceeded:
                            if hasattr(self, 'model_evidence'):
                                self.model_evidence['admission_status'] = 'rejected'
                            raise
                        except Exception:
                            if hasattr(self, 'model_evidence'):
                                self.model_evidence.update(requests_known=False, admission_status='unconfirmed')
                            raise
                        self.request_sequences.add(sequence)
                        self.request_count += 1
                        if hasattr(self, "model_evidence"):
                            self.model_evidence.update(requests=self.request_count, requests_known=True, admission_status="accepted")
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
        handler = self._DISPATCH.get(op)
        if handler is None:
            raise Denied("Unknown operation")
        return handler(self, args)

    def _op_diff(self, args: dict) -> dict:
        return self.project.snapshots.changes(self.snapshot["id"])

    def _op_files(self, args: dict) -> dict:
        if self.role.workspace == "none":
            names = []
        elif self.role.workspace != "write":
            names = sorted(self.snapshot["files"])
        else:
            names = []
            for directory, dirs, files in os.walk(self.workspace, followlinks=False):
                rel = Path(directory).relative_to(self.workspace)
                dirs[:] = sorted(d for d in dirs if not self.project.snapshots.excluded((rel / d).as_posix()))
                for name in sorted(files):
                    path = (rel / name).as_posix()
                    if not self.project.snapshots.excluded(path):
                        names.append(path)
                        if len(names) > self.config.limits.snapshot_files:
                            raise LimitExceeded("Workspace file count exceeded")
            names.sort()
        result = page(names, args.get("offset", 0), args.get("limit", 1000))
        result["files"] = result.pop("items")
        return result

    def _op_read(self, args: dict) -> dict:
        if self.role.workspace == "none" or self.project.snapshots.excluded(args["path"]):
            raise Denied("Path is not exposed to this role")
        data = safe_read(self.workspace, args["path"], min(self.config.limits.file_bytes, PREVIEW_BYTES))
        return {"path": args["path"], "text": data.decode("utf-8", "replace")}

    def _op_exec_experiment(self, op: str, args: dict) -> dict:
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        if op == "exec" and self.role.workspace == "write":
            self.verification = None
        result = self.sandbox.execute(self.workspace, args["script"],
                                      writable=self.role.workspace == "write", experiment=op == "experiment")
        if op == "experiment":
            science = {key: args.get(key, "") for key in ("question", "comparison", "measure")}
            write_json(self.run_dir / "experiments" / f"{result['id']}.json", {**science, "command": result})
        # The command record is complete on disk. Tool results have a fixed
        # small shape before execution; escaped output cannot invalidate success.
        response = {key:result[key] for key in ('id','kind','exit_code','reason','seconds','writable') if key in result}
        for key in ('script','stdout','stderr'):
            response[key],response[key+'_truncated'] = text_preview(result.get(key,''))
        response['evidence'] = 'commands/'+result['id']+'.json'
        return response

    def _op_verify(self, args: dict) -> dict:
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
        return {**self.verification, 'commands':[
            {**command, 'script':text_preview(command['script'],512)[0],
             'script_truncated':text_preview(command['script'],512)[1]}
            for command in self.verification['commands']], 'evidence':'verification.json'}

    def _op_fetch(self, args: dict) -> dict:
        record = bounded(self.config, self.run_dir, "fetch", args["url"], self.tools_cancelled)
        text = record.get('text','')
        offset,limit = args.get('offset',0),args.get('limit',8192)
        end = min(len(text),offset+limit)
        return {**record,'text':text[offset:end],'offset':offset,
                'next_offset':end if end<len(text) else None,'truncated':end<len(text)}

    def _op_search(self, args: dict) -> dict:
        record = bounded(self.config, self.run_dir, "search", args["query"], self.tools_cancelled)
        evidence_id=digest(canonical(record))
        write_json(self.run_dir/'sources'/('search-'+evidence_id+'.json'),record,exclusive=True)
        rows=[]
        for item in record.get('results',[]):
            row={}
            for key,bound in (('title',300),('url',4096),('summary',1500)):
                row[key],row[key+'_truncated']=text_preview(item.get(key,''),bound)
            receipt=item.get('source_receipt')
            if isinstance(receipt,str) and DIGEST.fullmatch(receipt):row['source_receipt']=receipt
            score=item.get('score')
            if type(score) in (int,float) and -2**63 <= score <= 2**63:row['score']=score
            rows.append(row)
        response=page(rows,args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
        response['results']=response.pop('items')
        response['evidence']='sources/search-'+evidence_id+'.json'
        for key in ('scope','trust','feeds_total','feeds_consulted'):
            if key in record:response[key]=record[key]
        response['source_truncated']=record.get('truncated',False)
        return response

    def _op_insights(self, args: dict) -> dict:
        if args.get("id"):
            return self.project.insights.read(args["id"])
        projection = self.project.insights.projection(limit=self.config.limits.pending_insights)
        response = page(projection['items'],args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
        response['insights'] = response.pop('items')
        response['total'] = projection['total']
        response['selection_truncated'] = projection['truncated']
        return response

    def _op_decide(self, args: dict) -> dict:
        return self.project.insights.decide(args["id"], args["action"], args["reason"],
                                            args.get("revisit", ""), self.run_dir.name)

    def _op_submit_insight(self, args: dict) -> dict:
        return self.project.insights.submit(source=self.role.name, title=args["title"], body=args["body"],
                                             base_snapshot=self.snapshot["id"], run=self.run_dir.name)

    def _op_consult(self, args: dict) -> dict:
        if self.consult is None:
            raise Denied("Nested consultation is disabled")
        return self.consult(self, args)

    def _op_report(self, args: dict) -> dict:
        self.commentary = args
        write_json(self.run_dir / "commentary.json", args)
        return {"staged": True}

    def _op_finish(self, args: dict) -> dict:
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

    def _op_exec(self, args: dict) -> dict:
        return self._op_exec_experiment("exec", args)

    def _op_experiment(self, args: dict) -> dict:
        return self._op_exec_experiment("experiment", args)

    _DISPATCH = {
        "diff": _op_diff, "files": _op_files, "read": _op_read,
        "exec": _op_exec, "experiment": _op_experiment, "verify": _op_verify,
        "fetch": _op_fetch, "search": _op_search, "insights": _op_insights,
        "decide": _op_decide, "submit_insight": _op_submit_insight,
        "consult": _op_consult, "report": _op_report, "finish": _op_finish,
    }


def prompt_for(context: Context) -> str:
    caps = set(context.role.capabilities)
    # Mechanism enforces grants, not policy choice: only capability-gated,
    # already-recorded context is offered. `pending_insights` count follows the
    # operator-selected `[limits] pending_insights` bound (newest kept);
    # `recent_snapshots` follows `[limits] prompt_snapshots`. Exact snapshot
    # references are always pinned. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    pending = context.project.insights.list(limit=context.config.limits.pending_insights) \
        if ("insights" in caps or "decide" in caps) else []
    acceptance = list(context.project.verify) if "verify" in caps else []
    return json.dumps({"goal": context.goal,
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
    def __init__(self, config: Config, *, driver=None, stop: threading.Event | None = None, ephemeral: bool = False):
        self.config = config
        self.driver = driver
        self.ephemeral = ephemeral
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
        usage = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                       self.config.limits.retention_days).usage()
        if usage["limit"] <= usage["used"]:
            raise LimitExceeded("UTC daily model-request budget unavailable")
        if shutil.disk_usage(project.root).free < self.config.limits.free_disk_mb * 1048576:
            raise LimitExceeded("Free disk space is below the configured reserve")
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(project.root / "locks" / f"run-{role_name}.lock", blocking=False))
            if role.workspace == "write":
                stack.enter_context(lock(project.root / "locks" / "workspace.lock", blocking=False))
            stack.enter_context(slot(self.config))
            project.insights.ingest_editor(keep_days=project.config.limits.retention_days)
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
            context = None
            model_result = None
            result = None
            workspace = project.workspace if role.workspace == "write" else run_dir / "input"
            try:
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
                context.ephemeral = self.ephemeral
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
                    if verification and (verification["code_digest"] != captured["code_digest"] or captured["skipped"]):
                        verification = {**verification, "passed": False, "invalidated": "code changed or final snapshot is unrepresentable"}
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
                # Publication is an observed fact, even if subsequent work failed.
                with contextlib.suppress(OSError, ValueError, TypeError):
                    publication = "unknown"
                    try:
                        pointer = read_json(project.root / "current.json", {})
                        published = role.workspace == "write" and pointer.get("snapshot") == snapshot["id"] and snapshot.get("run") == run_id
                        publication = "published" if published else "not_published"
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass
                    error = {"run": run_id, "role": role_name,
                             "status": "interrupted", "error": str(exc), "finished_at": now(),
                             "publication": publication,
                             "durability": "unconfirmed" if isinstance(exc, OSError) or publication == "unknown" or _platform.IS_WINDOWS else "confirmed"}
                    if model_result is not None:
                        error["model"] = model_result
                    elif context is not None and hasattr(context, "model_evidence"):
                        error["model"] = context.model_evidence
                    write_json(run_dir / "error.json", error)
                    if result is not None:
                        write_json(run_dir / "result.json", {**result, "status": "interrupted", **error})
                with contextlib.suppress(OSError, ValueError, TypeError):
                    health = read_json(project.root / "health" / f"{role_name}.json", {})
                    failures = health.get("consecutive_failures", 0) + 1
                    write_json(project.root / "health" / f"{role_name}.json",
                               {"consecutive_failures": failures, "last_run": run_id, "error": str(exc), "updated_at": now()})
                    if self.config.limits.max_failures > 0 and \
                            failures >= self.config.limits.max_failures and not isinstance(exc, Cancelled):
                        project.set_control(paused=True, reason="Repeated failures; inspect health and run records")
                raise
            finally:
                if context is not None:
                    context.closed = True
                with contextlib.suppress(OSError):
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
        check_consult_role(role_name, role)
        if self.config.limits.parallel_runs <= 1:
            raise Busy("Consultation needs an execution slot in addition to its parent")
        capture = parent.project.snapshots.capture_files(parent.workspace)
        snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                              run=parent.run_dir.name, outcome="wait", summary="Consultation input")

        def ask(name: str) -> dict:
            run_dir = parent.project.root / "runs" / uuid.uuid4().hex
            workspace = run_dir / "input"
            ctx = None
            try:
                with slot(self.config):
                    mkdir(run_dir)
                    write_json(run_dir / "started.json", {"run": run_dir.name,
                               "parent_run": parent.run_dir.name, "role": role.name,
                               "snapshot": snap["id"], "started_at": now()})
                    parent.project.snapshots.materialize(snap, workspace)
                    ctx = Context(self.config, parent.project, dataclasses.replace(role, profile=name),
                                  run_dir, snap, workspace, stop=parent.stop, goal=parent.goal)
                    ctx.ephemeral = True
                    ctx.deadline = min(ctx.deadline, parent.deadline)
                    model = self.resolve(name).execute(ctx, prompt_for(ctx) + "\n\nQuestion:\n" + args["question"], profile=name)
                    if ctx.cancelled():
                        raise Cancelled("Consultation cancelled before completion")
                    if ctx.finished is None:
                        raise Denied("Consultation did not finish")
                    result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                              "role": role.name, "status": "completed", "finished_at": now(),
                              "profile": name, "snapshot": snap["id"],
                              "answer": ctx.finished["summary"], "model": model}
                    write_json(run_dir / "consultation.json", result)
                    return result
            except Exception as exc:
                result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                          "role": role.name, "status": "interrupted", "finished_at": now(),
                          "profile": name, "snapshot": snap["id"], "error": str(exc)}
                if ctx is not None and hasattr(ctx, "model_evidence"):
                    result["model"] = ctx.model_evidence
                write_json(run_dir / "error.json", result)
                return result
            finally:
                if ctx is not None:
                    ctx.closed = True
                shutil.rmtree(workspace, ignore_errors=True)
        with ThreadPoolExecutor(max_workers=self.config.limits.parallel_consults) as pool:
            results = list(pool.map(ask, names))
        record = {"snapshot": snap["id"], "question": args["question"], "role": role.name, "answers": [{"run": r["run"], "profile": r["profile"], **({"error": r["error"]} if "error" in r else {})} for r in results]}
        write_json(parent.run_dir / "consultations" / f"{uuid.uuid4().hex}.json", record)
        answers=[]
        for result in results:
            answer={k:result[k] for k in ('run','profile','snapshot') if k in result}
            if 'answer' in result:
                answer['answer']=result['answer']
            if 'error' in result:
                answer['error'],answer['error_truncated']=text_preview(result['error'],2048)
            answers.append(answer)
        return {**record,'answers':answers}


def should_run(project: Project, snapshot: dict, current_time: float | None = None) -> bool:
    """Fixed single-writer publication contract (mechanism, not operator policy).

    `done` needs an explicit wake or changed goal; `continue` always runs;
    `wait`/`blocked` resume on wake, new proposals, or elapsed wait. Goal and
    wake-generation changes always resume. See docs/architecture.md.
    """
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
                    project.insights.ingest_editor(keep_days=config.limits.retention_days)
                    role = config.roles[role_name]
                    ready = should_run(project, project.snapshots.get()) if role.workspace == "write" else (
                        project.control().get("armed") and not project.control().get("paused"))
                    if ready:
                        result = Engine(config, stop=stop).run(project, role_name)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        stop.wait(config.limits.cooldown_seconds if role.workspace == "write" else config.limits.idle_seconds)
                        continue
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                stop.wait(config.limits.idle_seconds)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
