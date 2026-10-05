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
from .config import Config, Role, load, role_policy_bytes
from .doctor import container_runtime
from .drivers import driver_for
from .errors import Busy, Cancelled, ConfigError, Denied, InfraExceeded, LimitExceeded, MizuError, ModelFailure, ProtocolError
from .fs import canonical, digest, lock, mkdir, now, read_json, safe_read, write_json, PREVIEW_BYTES, page, text_preview, DIGEST
from .project import Project
from .protocol import DEFINITIONS, validate
from .report import previous as _previous_report, publish
from .sandbox import Sandbox, cleanup
from . import vcs as _vcs
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


#: Infrastructure waits defer (Busy locks/slots, InfraExceeded daily-budget
#: and disk-reserve guards) rather than counting toward the consecutive-failure
#: brake. All other faults still count, including plain LimitExceeded bound
#: faults (engine deadlines, event-stream/RPC bounds, per-run tool/request
#: bounds, file-count/snapshot bounds). The error record is still written and
#: the exception still raised, so nothing is hidden; the daemon surfaces it
#: as a run_deferred event.
INFRA_WAIT = (Busy, InfraExceeded)


def is_infra_wait(exc: BaseException) -> bool:
    """True when a failure is an infrastructure wait that defers, not a fault.

    Schema: exception instance. Bounds: type check only. Trust: local
    classification, never model input. Failure: never raises.
    """
    return isinstance(exc, INFRA_WAIT)


def is_deferred(exc: BaseException, context=None) -> bool:
    """True when a failure defers rather than counting toward the brake.

    Schema: exception plus the run Context (or None). Bounds: type check plus
    one host-side flag. Trust: local classification, never model input.
    Failure: never raises.

    The Pi transport converts an in-run host-side daily-budget refusal
    (InfraExceeded from ``_budget``) into ``{"ok": false}`` on the bridge,
    so the adapter collapses and the host driver surfaces ``ProtocolError``
    or ``ModelFailure``. ``Context.admission_wait`` records that refusal by
    type (set only for InfraExceeded, never by message text), so a
    ProtocolError/ModelFailure arriving after such a refusal is the same
    infrastructure wait, not a new model fault.
    """
    if isinstance(exc, INFRA_WAIT):
        return True
    try:
        if context is not None and getattr(context, "admission_wait", False):
            return isinstance(exc, (ProtocolError, ModelFailure))
    except Exception:
        return False
    return False


#: Capabilities that let a consultation role change shared state or execute
#: code. Consultation must stay read-only and advisory: workspace "read" plus
#: none of these. Read-only grants (files/read/diff/insights/fetch/search)
#: stay operator choice, plus the required finish.
CONSULT_FORBIDDEN = frozenset({"exec", "experiment", "verify", "decide",
                               "submit_insight", "consult", "report", "sync",
                               "vcs_publish"})


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
        self.admission_wait = False
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
                                   self.config.limits.retention_days,
                                   self.config.limits.shared_daily_requests, self.config.timezone).take(
                                       f"{self.run_dir.name}:{sequence}", project=self.project.name)
                        except InfraExceeded:
                            if hasattr(self, 'model_evidence'):
                                self.model_evidence['admission_status'] = 'rejected'
                            self.admission_wait = True
                            raise
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
            names = sorted(names + self._injected_ref_paths())
            result = page(names, args.get("offset", 0), args.get("limit", 1000))
            result["files"] = result.pop("items")
            return result
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
            names = names + self._injected_ref_paths()
            names.sort()
        result = page(names, args.get("offset", 0), args.get("limit", 1000))
        result["files"] = result.pop("items")
        return result

    def _injected_ref_paths(self) -> list[str]:
        # Injected refs live in the project workspace even for read roles
        # (snapshots exclude them, so materialized inputs lack them).
        try:
            refs = _vcs.list_refs(self.project.workspace)
        except OSError as exc:
            raise Denied(f"Upstream refs are unavailable: {exc}") from exc
        return [_vcs.REF_PREFIX_PATH + "/" + name for name in refs]

    def _op_read(self, args: dict) -> dict:
        ref = _vcs.split_ref_path(args["path"])
        if ref is not None:
            if self.role.workspace == "none":
                raise Denied("Path is not exposed to this role")
            try:
                text = _vcs.read_ref(self.project.workspace, ref)
            except OSError as exc:
                raise Denied(f"Upstream ref is unavailable: {exc}") from exc
            return {"path": args["path"], "text": text}
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

    def _op_sync(self, args: dict) -> dict:
        # Host-side refresh only: fetch upstream refs via the trusted adapter
        # and inject them read-only under refs/remotes/upstream/*. Merging is
        # worker policy (workspace edits + verify + publish); the mechanism
        # only records the receipt. Requires a writable workspace so read
        # roles cannot mutate the shared ref view.
        if self.role.workspace != "write":
            raise Denied("Sync requires a writable workspace")
        try:
            fetched = _vcs.fetch_refs(self.config.vcs)
        except OSError as exc:
            raise Denied(f"Upstream sync is unavailable: {exc}") from exc
        try:
            receipt = _vcs.inject_refs(self.project.workspace, fetched["refs"])
        except OSError as exc:
            raise Denied(f"Upstream refs are unavailable: {exc}") from exc
        names = sorted(fetched["refs"])
        record = {"synced": True, "injected": receipt["injected"],
                  "prefix": receipt["prefix"], "trust": "external-untrusted",
                  "upstream": fetched["refs"].get("main"),
                  "evidence": "sync.json", "refs": names}
        write_json(self.run_dir / "sync.json",
                   {"injected": receipt["injected"], "prefix": receipt["prefix"],
                    "trust": "external-untrusted", "refs": fetched["refs"]})
        return record

    def _op_vcs_read(self, args: dict) -> dict:
        # Read-only VCS view: CI status, logs, PR comments via the trusted
        # adapter. Never publishes; mutating ops are refused here even if
        # the adapter would serve them. Requires a visible workspace.
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        try:
            data = _vcs.read_via(self.config.vcs, args["op"],
                                 {"branch": args["branch"], **({"sha": args["sha"]} if "sha" in args else {})})
        except OSError as exc:
            raise Denied(f"VCS read is unavailable: {exc}") from exc
        record = {"op": args["op"], "branch": args["branch"], "trust": "external-untrusted",
                  "evidence": "vcs-read.json", "result": data}
        write_json(self.run_dir / "vcs-read.json", data)
        return record

    def _op_vcs_publish(self, args: dict) -> dict:
        # External publication: push/PR via the trusted adapter behind a
        # recorded human GO approval bound to branch and code digest.
        # Fail-closed without approval; stale digests refused. Merging stays
        # workspace edits + verify; this only ships the approved tree.
        if self.role.workspace != "write":
            raise Denied("Publication requires a writable workspace")
        captured = self.project.snapshots.capture_files(self.workspace)
        if captured.get("skipped"):
            raise Denied("Publication requires a representable snapshot")
        code_digest = captured["code_digest"]
        approval = _vcs.require_go_approval(self.project, args["branch"], code_digest)
        try:
            data = _vcs.publish_via(self.config.vcs, args["op"], {"branch": args["branch"], "code_digest": code_digest})
        except OSError as exc:
            raise Denied(f"VCS publication is unavailable: {exc}") from exc
        record = {"published": True, "op": args["op"], "branch": args["branch"],
                  "code_digest": code_digest, "approval": approval["insight"],
                  "trust": "external-untrusted", "evidence": "vcs-publish.json",
                  "result": data}
        write_json(self.run_dir / "vcs-publish.json",
                   {"op": args["op"], "branch": args["branch"],
                    "code_digest": code_digest, "approval": approval["insight"],
                    "trust": "external-untrusted", "result": data})
        return record

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
        "sync": _op_sync, "vcs_read": _op_vcs_read, "vcs_publish": _op_vcs_publish,
        "consult": _op_consult, "report": _op_report, "finish": _op_finish,
    }


def refresh_upstream(config, project) -> dict:
    """Host-side upstream refresh: fetch refs and inject them read-only.

    Schema: returns ``{"injected": n, "prefix": ..., "trust": ...}``.
    Bounds: adapter bounds from ``config.vcs``; at most 4096 refs.
    Trust: operator-owned adapter; refs stay external-untrusted.
    Failure: ``Denied`` on adapter/shape errors; ``OSError`` on host I/O.
    No capability check here; the ``sync`` tool adds the grant gate.
    """
    fetched = _vcs.fetch_refs(capped_vcs_settings(config.vcs, vcs_poll_fetch_timeout(config)))
    receipt = _vcs.inject_refs(project.workspace, fetched["refs"])
    return {"injected": receipt["injected"], "prefix": receipt["prefix"],
            "trust": "external-untrusted", "refs": dict(fetched["refs"])}


def upstream_fetch_due(last_fetch: float | None, now: float, interval: float) -> bool:
    """Return True when a periodic upstream fetch is due (fake-clock testable).

    Schema: wall-clock seconds; ``last_fetch=None`` means never fetched.
    Bounds: ``interval <= 0`` always fetches (config floors keep it positive).
    Trust: pure time arithmetic, no I/O. Failure: never raises.
    """
    if last_fetch is None:
        return True
    try:
        last = float(last_fetch)
        current = float(now)
    except (TypeError, ValueError):
        return True
    if last > current:
        return True
    try:
        return (current - last) >= float(interval)
    except (TypeError, ValueError):
        return True


def poll_upstream(config, project, state: dict, *, now: float, interval: float,
                  refresh=None) -> dict | None:
    """Best-effort periodic upstream refresh for the daemon loop.

    Schema: ``state`` holds ``last_fetch`` (wall-clock) across calls; returns
    an ``upstream_fetch`` event dict or ``None`` when skipped (disabled or not
    due). Bounds: one ``refresh`` invocation per due poll under the adapter
    timeout. Trust: host side only via ``refresh_upstream``; refs stay
    external-untrusted. Retry/cancellation: no retry; ``last_fetch`` advances
    on failure too so one bad adapter cannot busy-loop. Evidence: the
    returned event names ok/injected or error; this helper never publishes a
    snapshot (injected refs are digest-excluded). Failure: ``Denied``,
    ``OSError`` and ``ValueError`` become ``ok=False`` events, never raised.
    """
    if not config.vcs.get("command"):
        return None
    if not upstream_fetch_due(state.get("last_fetch"), now, interval):
        return None
    state["last_fetch"] = now
    do_refresh = refresh if refresh is not None else refresh_upstream
    try:
        receipt = do_refresh(config, project)
    except (Denied, OSError, ValueError) as exc:
        state["last_error"] = str(exc)
        return {"event": "upstream_fetch", "ok": False,
                "error": str(exc), "time": now_iso()}
    state["last_error"] = ""
    state["last_ok"] = now
    return {"event": "upstream_fetch", "ok": True,
            "injected": receipt["injected"], "prefix": receipt["prefix"],
            "trust": "external-untrusted", "time": now_iso()}


#: Fail-closed defaults for daemon polling bounds (operator policy in
#: `[vcs]` carries the same values; see ``vcs_poll_*`` below). The branch
#: cap keeps one tick short so a poll cannot block the loop for minutes
#: even when every adapter call uses its full per-call timeout. The
#: per-call caps bound daemon ticks via `min(timeout_seconds, cap)` so a
#: large on-demand timeout cannot stall the loop.
MAX_CI_BRANCHES = 4
POLL_CALL_TIMEOUT_CAP = 15
POLL_FETCH_TIMEOUT_CAP = 30


def _vcs_poll_number(settings: dict, key: str, default: int) -> int:
    """Configured `[vcs]` poll bound or its fail-closed default."""
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def vcs_poll_max_branches(config) -> int:
    """Daemon CI branches per tick from `vcs.poll_max_branches` (default 4)."""
    try:
        settings = config.vcs
    except AttributeError:
        return MAX_CI_BRANCHES
    return _vcs_poll_number(settings, "poll_max_branches", MAX_CI_BRANCHES)


def vcs_poll_fetch_timeout(config) -> int:
    """Daemon fetch cap from `vcs.poll_fetch_timeout_seconds` (default 30)."""
    try:
        settings = config.vcs
    except AttributeError:
        return POLL_FETCH_TIMEOUT_CAP
    return _vcs_poll_number(settings, "poll_fetch_timeout_seconds",
                            POLL_FETCH_TIMEOUT_CAP)


def vcs_poll_status_timeout(config) -> int:
    """Daemon per-branch status cap from `vcs.poll_status_timeout_seconds` (default 15)."""
    try:
        settings = config.vcs
    except AttributeError:
        return POLL_CALL_TIMEOUT_CAP
    return _vcs_poll_number(settings, "poll_status_timeout_seconds",
                            POLL_CALL_TIMEOUT_CAP)
#: Shared per-project poll state (persisted timestamps) and lock name.
POLL_STATE_FILE = "vcs-poll.json"
POLL_LOCK_NAME = "vcs-poll.lock"


def vcs_poll_enabled(config) -> bool:
    """True only when daemon VCS polling is explicitly opted in.

    Schema: operator config. Trust: config, never model input.
    Failure: never raises; unconfigured/disabled reads as False.
    """
    try:
        return bool(config.vcs.get("poll_enabled")) and bool(config.vcs.get("command"))
    except (AttributeError, TypeError):
        return False


def vcs_poll_interval(config) -> float:
    """Daemon VCS poll cadence (seconds) from `vcs.poll_interval_seconds`."""
    try:
        value = float(config.vcs.get("poll_interval_seconds", 300))
    except (TypeError, ValueError):
        return 300.0
    return value if value > 0 else 300.0


def capped_vcs_settings(settings: dict, cap: int) -> dict:
    """Copy adapter settings with the timeout capped for daemon polling."""
    try:
        timeout = int(settings.get("timeout_seconds", 20))
    except (TypeError, ValueError):
        timeout = 20
    return {**settings, "timeout_seconds": min(timeout, cap)}


def poll_ci(config, project, state: dict, *, now: float, interval: float,
            branches=None, reader=None) -> dict | None:
    """Best-effort periodic CI polling: status per branch, failures to insights.

    Schema: ``branches=None`` derives from injected upstream refs
    (``vcs.list_refs``, first ``poll_max_branches`` sorted); explicit lists
    are validated as branch names. Returns a ``ci_poll`` event dict or ``None`` when skipped
    (disabled or not due). Bounds: one adapter call per branch per due poll
    under the adapter timeout; status checks capped at 1024 rows each.
    Trust: host side only; adapter facts stay external-untrusted and only
    ``failure`` states are recorded via ``vcs.record_ci_result`` (stable
    dedup IDs, passes ignored). Retry/cancellation: no retry; ``last_poll``
    advances even on failure so one bad adapter cannot busy-loop. Evidence:
    the event names ok/recorded branches/counts or error; this helper never
    publishes a snapshot. Failure: ``Denied``, ``OSError`` and ``ValueError``
    become ``ok=False`` events, never raised.
    """
    if not config.vcs.get("command"):
        return None
    if not upstream_fetch_due(state.get("last_poll"), now, interval):
        return None
    state["last_poll"] = now
    max_branches = vcs_poll_max_branches(config)
    def _default_reader(settings, op, params):
        return _vcs.read_via(capped_vcs_settings(settings, vcs_poll_status_timeout(config)),
                             op, params)
    do_read = reader if reader is not None else _default_reader
    try:
        if branches is not None and len(list(branches)) > max_branches:
            raise Denied("Too many CI branches per poll")
        if branches is None:
            try:
                names = sorted(_vcs.list_refs(project.workspace))[:max_branches]
            except OSError as exc:
                raise Denied(f"CI polling is unavailable: {exc}") from exc
            # Fall back to main so a fresh workspace still polls once configured.
            targets = names or ["main"]
        else:
            targets = [_vcs.check_branch(b) for b in branches]
        recorded: list[dict] = []
        failures = 0
        for branch in targets:
            data = do_read(config.vcs, "status", {"branch": branch})
            checks = data.get("checks")
            if not isinstance(checks, list):
                raise Denied("VCS status must carry a checks array")
            for entry in checks:
                if entry.get("state") != "failure":
                    continue
                receipt = _vcs.record_ci_result(
                    project, branch=branch, sha=entry["sha"],
                    check=entry["check"], state="failure",
                    url=entry.get("url", ""))
                failures += 1
                if receipt.get("recorded"):
                    recorded.append({"id": receipt["id"], "branch": branch,
                                     "check": entry["check"]})
        state["last_error"] = ""
        state["last_ok"] = now
        return {"event": "ci_poll", "ok": True, "branches": list(targets),
                "failures": failures, "recorded": recorded,
                "trust": "external-untrusted", "time": now_iso()}
    except (Denied, OSError, ValueError) as exc:
        state["last_error"] = str(exc)
        return {"event": "ci_poll", "ok": False,
                "error": str(exc), "time": now_iso()}


def now_iso() -> str:
    """Wall-clock event stamp for ``poll_upstream``."""
    return now()


def _prompt_clock(context: Context) -> dict:
    """Current wall-clock time plus the configured zone for agent reasoning.

    Schema: ``{"now": <ISO-8601 with offset>, "timezone": <name>}`` appended
    last so the stable prefix keeps working caches warm. Bounds: two small
    string fields. Trust: local clock plus operator ``timezone`` (default
    UTC). Retry/cancellation: n/a (pure projection). Evidence: recorded in
    ``prompt_projection.json`` bytes only. Failure: raises on broken
    configuration (fail closed, never a silent UTC day).
    """
    import datetime as _dt
    tz = context.config.tzinfo
    name = context.config.timezone
    return {"now": _dt.datetime.now(tz).isoformat(timespec="seconds"), "timezone": str(name)}


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
                       "acceptance_commands": acceptance,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       **_prompt_clock(context)},
                      ensure_ascii=False)


def prompt_delta_for(context: Context, pending: list) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: ``goal_digest`` pin plus the pinned ``published_snapshot`` and a
    small ``snapshot_delta`` (previous/current ids), then only the pending
    proposals not yet offered, workspace markers, capability-gated acceptance
    commands, and a capability-gated ``previous_report`` preview for roles
    holding ``report``. The full goal text and ``recent_snapshots`` history
    are never repeated: the provider session already holds them. Key order is
    fixed (pins first) so the stable prefix keeps working caches warm.
    Bounds: pending follows ``[limits] pending_insights``; the delta carries
    no workspace file contents and no history array, so resumed-unit bytes
    stay flat as history grows apart from the fixed previous-report preview
    (at most ``report.PREVIOUS_REPORT_BYTES``). Trust: local recorded state
    only, never model input. Retry/cancellation: bounded local reads only
    (artifact pointer plus one file prefix); ``previous()`` never raises,
    so an unreadable artifact reads as no previous edition. Evidence: the
    dispatching run records ``prompt_mode=delta`` and ``prompt_bytes`` in
    ``prompt_projection.json``. Failure: never raises for missing state
    (callers fall back to the full prompt).
    """
    caps = set(context.role.capabilities)
    acceptance = list(context.project.verify) if "verify" in caps else []
    return json.dumps({"goal_digest": context.goal_digest,
                       "published_snapshot": {k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "pending_insights": pending,
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "acceptance_commands": acceptance,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       **_prompt_clock(context)},
                      ensure_ascii=False)


def session_prompt(context: Context, role, run_dir: Path) -> tuple:
    """Choose the full or delta prompt for this dispatch.

    Schema: peeks at the content-bound session record for the role profile;
    returns ``(prompt, mode, session_key, rotation, session_dir)`` where mode
    is ``"full"`` or ``"delta"`` and rotation is the ``rotation.json`` record
    (or None). Bounds: prompt-state and rotation evidence are small JSON
    files; pending lists follow ``[limits] pending_insights``. Trust: local
    session state only. Retry/cancellation: never raises for session I/O;
    any failure falls back to the full prompt and empty identity. Evidence:
    rotations write ``rotation.json``; dispatches record the mode in
    ``prompt_projection.json``. Failure: a due rotation removes
    ``session.json`` so the next dispatch mints a fresh provider session
    while the published snapshot and composed policy reload unchanged; a
    missing or invalid record (new, compacted-away or rotated session) takes
    the full prompt.
    """
    from .engine_config import (effective, read_prompt_state, rotate_session,
                                rotation_due, session_record)
    if getattr(context, "ephemeral", False):
        return prompt_for(context), "full", "", None, None
    try:
        settings = effective(context.config, role, role.profile)
    except Exception:
        return prompt_for(context), "full", "", None, None
    if settings.get("session") != "persistent":
        return prompt_for(context), "full", "", None, None
    try:
        path, saved = session_record(context, role.profile, settings)
    except Exception:
        return prompt_for(context), "full", "", None, None
    session_dir = path.parent
    key = session_dir.name
    if saved is None:
        return prompt_for(context), "full", key, None, session_dir
    try:
        due, reason = rotation_due(context.config.limits, path, saved,
                                   settings.get("engine", "unknown"))
    except Exception:
        due, reason = False, ""
    if due:
        try:
            record = rotate_session(path, run_dir, reason)
        except Exception:
            return prompt_for(context), "full", key, None, session_dir
        return prompt_for(context), "full", key, record, session_dir
    caps = set(context.role.capabilities)
    try:
        current_generation = context.project.insights.generation()
        last = read_prompt_state(session_dir)
        if (last is not None
                and last.get("inbox_generation") == current_generation
                and last.get("snapshot") == context.snapshot["id"]):
            pending: list = []
        else:
            pending = context.project.insights.list(limit=context.config.limits.pending_insights) \
                if ("insights" in caps or "decide" in caps) else []
    except Exception:
        pending = []
    try:
        return prompt_delta_for(context, pending), "delta", key, None, session_dir
    except Exception:
        return prompt_for(context), "full", key, None, session_dir


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

    def preview(self, project: Project, role_name: str, attributes=None) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        if not role.selector:
            return {"profile": role.profile, "selector": None}
        from .classification import prepare
        return prepare(self, project, role, project.snapshots.get(), attributes, preview=True)

    def run(self, project: Project, role_name: str, *, attributes=None) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        control = project.control()
        if not control.get("armed") or control.get("paused"):
            raise Denied("Project is unarmed or paused")
        usage = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                       self.config.limits.retention_days,
                       self.config.limits.shared_daily_requests, self.config.timezone).usage(project.name)
        if usage["limit"] <= usage["used"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request budget unavailable for project '{project.name}'")
        if usage["shared_limit"] > 0 and usage["shared_used"] >= usage["shared_limit"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request shared budget unavailable")
        if shutil.disk_usage(project.root).free < self.config.limits.free_disk_mb * 1048576:
            raise InfraExceeded("Free disk space is below the configured reserve")
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(project.root / "locks" / f"run-{role_name}.lock", blocking=False))
            if role.workspace == "write":
                stack.enter_context(lock(project.root / "locks" / "workspace.lock", blocking=False))
            stack.enter_context(slot(self.config))
            project.insights.ingest_editor(keep_days=project.config.limits.retention_days)
            snapshot = project.snapshots.get()
            cursor = read_json(project.root / "observed" / f"{role_name}.json", {})
            if role.on_change and _on_change_observed(cursor, snapshot):
                return {"skipped": "unchanged", "snapshot": snapshot["id"],
                        "code_digest": snapshot["code_digest"]}
            active = project.root / "active" / f"{role_name}.json"
            run_id = uuid.uuid4().hex
            run_dir = project.root / "runs" / run_id
            mkdir(run_dir)
            context = None
            decision = None
            model_result = None
            result = None
            workspace = project.workspace if role.workspace == "write" else run_dir / "input"
            try:
                write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                write_json(run_dir / "started.json", {"run": run_id, "role": role_name,
                            "snapshot": snapshot["id"], "started_at": now(),
                            "config_sha256": digest(self.config.file.read_bytes()),
                            "policy_sha256": digest(role_policy_bytes(role))})
                if role.selector:
                    from .classification import prepare
                    decision = prepare(self, project, role, snapshot, attributes)
                    write_json(run_dir / "selection.json", decision)
                    if not decision["profile"]:
                        result = {"run": run_id, "role": role_name, "status": "waiting",
                                  "finished_at": now(), "selection": decision,
                                  "next_evaluation_at": decision["next_evaluation_at"]}
                        write_json(run_dir / "result.json", result)
                        return result
                    role = dataclasses.replace(role, profile=decision["profile"], selector="")
                if self.stop.is_set() or not project.control().get("armed") or project.control().get("paused"):
                    raise Cancelled("Run stopped before model dispatch")
                driver = self.resolve(role.profile)
                if getattr(driver, "requires_sandbox", True) and any(c in role.capabilities for c in ("exec", "experiment", "verify")):
                    container_runtime(self.config)
                    cleanup(self.config, project.root, role_name)
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
                prompt, prompt_mode, session_key, rotation, session_dir = session_prompt(context, role, run_dir)
                decoded = json.loads(prompt)
                write_json(run_dir / "prompt_projection.json",
                           {"run": run_id, "role": role_name, "snapshot": snapshot["id"],
                            "recent_snapshots": len(decoded.get("recent_snapshots", [])),
                            "pending_insights": len(decoded.get("pending_insights", [])),
                            "acceptance_commands": len(decoded.get("acceptance_commands", [])),
                            "prompt_bytes": len(prompt.encode("utf-8")), "created_at": now(),
                            "prompt_mode": prompt_mode, "session_key": session_key,
                            "rotation": (rotation or {}).get("reason", "")})
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
                if decision:
                    from .selection import State
                    State(self.config).result(decision, run=run_id)
                result = {"run": run_id, "role": role_name, "status": "prepared", "finished_at": now(),
                          "finish": finished, "model": model_result, "snapshot": snapshot["id"],
                          **({"selection": decision} if decision else {})}
                write_json(run_dir / "result.json", result)
                if role.workspace == "write":
                    project.snapshots.publish(snapshot)
                if context.commentary is not None:
                    result["artifact"] = publish(project, snapshot, context.commentary, run_id=run_id)
                if session_dir is not None:
                    with contextlib.suppress(OSError, ValueError, TypeError):
                        from .engine_config import write_prompt_state
                        write_prompt_state(session_dir, snapshot=snapshot["id"],
                                           inbox_generation=context.inbox_seen)
                result["status"] = "completed"
                write_json(run_dir / "result.json", result)
                write_json(project.root / "observed" / f"{role_name}.json",
                           {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"]})
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": 0, "last_run": run_id, "updated_at": now()})
                return result
            except BaseException as exc:
                if isinstance(exc, ModelFailure) and context is not None and context.admission_error:
                    if getattr(context, "admission_wait", False):
                        exc = InfraExceeded(f"{self.config.timezone} daily model-request budget exhausted")
                    else:
                        exc = ProtocolError("Local admission failed: " + context.admission_error)
                handled = None
                if decision and decision.get("profile") and isinstance(exc, ModelFailure) and context is not None and not context.cancelled():
                    from .selection import State
                    handled = State(self.config).result(decision, run=run_id, error=exc)
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
                             "durability": "unconfirmed" if isinstance(exc, OSError) or publication == "unknown" or _platform.IS_WINDOWS else "confirmed",
                             **({"selection": decision} if decision else {}),
                             **({"model_failure": exc.evidence} if isinstance(exc, ModelFailure) else {}),
                             **({"selection_action": handled} if handled else {})}
                    if model_result is not None:
                        error["model"] = model_result
                    elif context is not None and hasattr(context, "model_evidence"):
                        error["model"] = context.model_evidence
                    write_json(run_dir / "error.json", error)
                    if result is not None:
                        write_json(run_dir / "result.json", {**result, "status": "interrupted", **error})
                if handled:
                    return {"run": run_id, "role": role_name, "status": "deferred",
                            "error": str(exc), "selection": decision, "selection_action": handled,
                            "next_evaluation_at": time.time() + self.config.limits.cooldown_seconds}
                if not is_deferred(exc, context):
                    with contextlib.suppress(OSError, ValueError, TypeError):
                        health = read_json(project.root / "health" / f"{role_name}.json", {})
                        failures = health.get("consecutive_failures", 0) + 1
                        write_json(project.root / "health" / f"{role_name}.json",
                                   {"consecutive_failures": failures, "last_run": run_id, "error": str(exc), "updated_at": now()})
                        if self.config.limits.max_failures > 0 and \
                                failures >= self.config.limits.max_failures and not isinstance(exc, Cancelled):
                            project.set_control(paused=True, reason="Repeated failures; inspect health and run records")
                raise exc
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


def _on_change_observed(cursor: dict, snapshot: dict) -> bool:
    """True when an ``on_change`` role already observed this code.

    Schema: observed cursor plus the published snapshot. Bounds: string
    comparison only. Trust: local operator state plus content digests, never
    model input. Failure: never raises; unreadable cursors read as unseen.

    State-only republications keep the same ``code_digest`` under a new
    snapshot id, so they do not schedule ``on_change`` roles. A wake caused
    by an insight that needs no action still publishes (writer judgment),
    but that publication carries the same digest and observers keep
    skipping, so one no-op wake cannot retrigger the loop. Legacy cursors
    without ``code_digest`` fall back to snapshot-id equality once, then
    upgrade on the next completed run.
    """
    try:
        current = snapshot.get("code_digest")
        if not isinstance(cursor, dict):
            return False
        if not isinstance(current, str) or not current:
            return cursor.get("snapshot") == snapshot.get("id")
        seen = cursor.get("code_digest")
        if isinstance(seen, str) and seen:
            return seen == current
        return cursor.get("snapshot") == snapshot.get("id")
    except Exception:
        return False


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


def _wall(value):
    """Wall-clock timestamp or None for poll state merging."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def refresh_poll_state(state: dict, shared: dict) -> dict:
    """Refresh in-memory poll state from shared persisted state.

    Schema: ``state`` is the daemon in-memory ``{"last_fetch", "ci": {"last_poll"}}``;
    ``shared`` is the flat ``vcs-poll.json`` mapping. Returns the ``ci`` sub-state.
    Bounds: numeric timestamps only; anything else is ignored. Trust: local
    operator state, never model input. Failure: never raises.
    """
    ci_state = state.setdefault("ci", {})
    if not isinstance(shared, dict):
        return ci_state
    for key, target in (("last_fetch", state), ("last_poll", ci_state)):
        seen = _wall(shared.get(key))
        if seen is None:
            continue
        current = _wall(target.get(key))
        if current is None or seen > current:
            target[key] = seen
    return ci_state


def poll_project_vcs(config, project, state: dict, *, now: float, interval: float) -> list:
    """Poll upstream refs and CI once for a project under the shared lock.

    Schema: ``state`` is daemon in-memory poll state; ``now`` is wall-clock
    seconds; returns the due poll events (possibly empty). Bounds: at most one
    fetch plus one CI tick per call under adapter timeout caps. Trust: host
    side only; refs and CI facts stay external-untrusted. Retry/cancellation:
    no retry; timestamps advance even on failure. Evidence: returned events and
    the shared ``vcs-poll.json`` timestamps. Failure: never raises; a missed
    lock or bad state yields no events.
    """
    try:
        with lock(project.root / "locks" / POLL_LOCK_NAME, blocking=False):
            try:
                shared = read_json(project.root / POLL_STATE_FILE, {})
            except (OSError, ValueError, TypeError):
                shared = {}
            ci_state = refresh_poll_state(state, shared)
            events = []
            fetched = poll_upstream(config, project, state, now=now, interval=interval)
            if fetched is not None:
                events.append(fetched)
            polled = poll_ci(config, project, ci_state, now=now, interval=interval)
            if polled is not None:
                events.append(polled)
            with contextlib.suppress(OSError, ValueError, TypeError):
                write_json(project.root / POLL_STATE_FILE,
                           {"last_fetch": state.get("last_fetch"),
                            "last_poll": ci_state.get("last_poll")})
            return events
    except Busy:
        return []
    except (OSError, ValueError, TypeError):
        return []


def daemon(config: Config, name: str, role_name: str) -> None:
    stop = threading.Event()
    def stopped(signum, frame):
        stop.set()
    watched = [sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)) if sig is not None]
    old = {s: signal.signal(s, stopped) for s in watched}
    try:
        with lock(config.data / "locks" / f"daemon-{name}-{role_name}.lock", blocking=False):
            upstream_state: dict = {"last_fetch": None}
            while not stop.is_set():
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    project = Project(config, name)
                    project.insights.ingest_editor(keep_days=config.limits.retention_days)
                    if vcs_poll_enabled(config):
                        interval = vcs_poll_interval(config)
                        for event in poll_project_vcs(config, project, upstream_state,
                                                      now=time.time(), interval=interval):
                            print(json.dumps(event, ensure_ascii=False), flush=True)
                    role = config.roles[role_name]
                    ready = should_run(project, project.snapshots.get()) if role.workspace == "write" else (
                        project.control().get("armed") and not project.control().get("paused"))
                    if ready:
                        result = Engine(config, stop=stop).run(project, role_name)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        delay = config.limits.cooldown_seconds if role.workspace == "write" else config.limits.idle_seconds
                        if result.get("status") in ("waiting", "deferred"):
                            # Poll at idle cadence as well, to observe operator fact/config updates.
                            delay = min(config.limits.idle_seconds, max(0.1, result["next_evaluation_at"] - time.time()))
                        stop.wait(delay)
                        continue
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                stop.wait(config.limits.idle_seconds)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
