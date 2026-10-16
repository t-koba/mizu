"""The execution mechanism: bounded work, immutable inputs and explicit commit points.

There is no research/plan/implement state machine here. The role policy decides
what work is valuable. This module only validates authority and records outcomes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import platform as _platform
from .admission import admission
from .budget import Budget
from .config import Config, Role, load, role_policy_bytes, role_requests_per_run, role_tools_per_run, role_run_seconds
from .doctor import container_runtime
from .drivers import driver_for
from .errors import Busy, Cancelled, ConfigError, Denied, InfraExceeded, LimitExceeded, RunDeadlineExceeded, DeadlineWrapUp, DrainYieldExceeded, MissingFinish, MizuError, ModelFailure, ProtocolError
from .fs import (canonical, digest, lock, mkdir, now, read_json, safe_read, write_json,
                 PREVIEW_BYTES, READ_PAGE_CHARS, page, text_preview, DIGEST)
from .project import Project
from .snapshot import PROGRESS_SIGNALS, progress_disposition, validate_progress
from .protocol import DEFINITIONS, validate
from .report import previous as _previous_report, publish
from .sandbox import Sandbox, cleanup
from . import vcs as _vcs
from . import materialize as _materialize
from .web import Web
from .web_worker import bounded


@contextlib.contextmanager
def slot(config: Config):
    """Fail-fast slot admission, respecting older pending work (also probes)."""
    with admission(config):
        yield


#: Infrastructure waits defer (Busy locks/slots, InfraExceeded daily-budget
#: and disk-reserve guards) rather than counting toward the consecutive-failure
#: brake. All other faults still count, including plain LimitExceeded bound
#: faults (logical-request deadlines, per-record transport/RPC bounds, per-run
#: tool/request bounds, file-count/snapshot bounds). A whole-unit wall or
#: deadline wrap-up ended without a seal is a bounded, non-failure result.
#: Event-stream retention
#: past the operator budget truncates retained evidence explicitly instead
#: of failing the run. The error record is still written and
#: the exception still raised, so nothing is hidden; the daemon surfaces it
#: as a run_deferred event.
INFRA_WAIT = (Busy, InfraExceeded)

#: Explicit paged insight retrieval may walk the whole pending set (tool
#: page limit, same bound as `mizu insight list`); prompt
#: selection stays bounded by `[limits] pending_insights`.
INSIGHT_RETRIEVAL_LIMIT = 1000
#: Fixed memory bounds for run-local pre-send admission identities.
MAX_ENGINE_TOOL_INVOCATIONS = 4096
MAX_ENGINE_TOOL_ID = 256

#: Exact retained-evidence locators, never prefixes or filesystem paths.
EVIDENCE_UUID = re.compile(r"[0-9a-f]{32}\Z")
MAX_STARTED_EVIDENCE_BYTES = 65536


def _reject_nonfinite_json(value: str):
    raise ValueError("Non-finite JSON value")

#: Wrap-up allowance granted when a per-run spend bound is reached: extra
#: model requests and extra wrap-up tool calls the unit may still spend to
#: seal a clean `bounded` end instead of failing. Fixed and documented; the
#: daily budget still applies to every admitted request.
WRAP_UP_REQUESTS = 3
WRAP_UP_TOOLS = 5
#: Operations admitted during wrap-up (plus bridge bookkeeping). Everything
#: else is refused once a per-run bound is reached.
WRAP_UP_OPS = frozenset({"finish", "research", "submit_insight", "attach_evidence", "decide"})
#: Reads of existing state are useful observation, not harness-observable progress.
OBSERVATIONAL_OPS = frozenset({
    "files", "read", "diff", "insights", "research_read", "vcs_read",
    "command_evidence_read", "experiment_evidence_read",
})
REPLAYABLE_READ_ARGUMENTS = {"fetch": "receipt_id", "probe": "receipt_id", "search": "result_id"}


def _is_observational_operation(operation: str, arguments: dict) -> bool:
    """Identify pure reads of existing data, which do not reset progress."""
    if operation in OBSERVATIONAL_OPS:
        return True
    replay_argument = REPLAYABLE_READ_ARGUMENTS.get(operation)
    return replay_argument is not None and replay_argument in arguments


def is_infra_wait(exc: BaseException) -> bool:
    """True when a failure is an infrastructure wait that defers, not a fault.

    Schema: exception instance. Bounds: type check only. Trust: local
    classification, never model input. Failure: never raises.
    """
    return isinstance(exc, INFRA_WAIT)


def _record_health_failure(project, role_name, run_id, exc, maximum):
    """Advance the existing role failure brake once for a terminal failure."""
    with contextlib.suppress(OSError, ValueError, TypeError):
        health = read_json(project.root / "health" / f"{role_name}.json", {})
        failures = health.get("consecutive_failures", 0) + 1
        write_json(project.root / "health" / f"{role_name}.json",
                   {"consecutive_failures": failures, "last_run": run_id,
                    "error": str(exc), "updated_at": now()})
        if maximum > 0 and failures >= maximum and not isinstance(exc, Cancelled):
            project.set_control(paused=True, reason="Repeated failures; inspect health and run records")


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
#: none of these. Read-only grants (files/read/diff/insights/fetch/probe/search)
#: stay operator choice, plus the required finish.
CONSULT_FORBIDDEN = frozenset({"exec", "experiment", "verify", "decide",
                               "submit_insight", "revise_insight", "withdraw_insight", "attach_evidence", "consult", "report", "sync",
                               "vcs_publish", "vcs_retire", "vcs_dispose",
                               "research", "materialize"})


#: Capabilities a delegation child must never hold. The child works in the
#: caller's live workspace, so decide/insight-write/vcs-mutating grants
#: and redelegation stay with the caller: verification and publication
#: never leave it. Implementation grants (files/read/exec/search/fetch and
#: the child's own profile/selector/policy) stay operator choice. finish
#: stays allowed: every configured role carries it so the child can seal,
#: and delegation ensures it (idempotent when already present).
DELEGATE_FORBIDDEN = frozenset({"decide", "submit_insight", "revise_insight",
                                "withdraw_insight", "attach_evidence", "delegate",
                                "vcs_publish", "vcs_retire", "vcs_dispose"})


def check_delegate_role(role_name: str, role) -> None:
    """Single property-based delegation gate (fail closed before any model call).

    Schema: role has ``capabilities``. Bounds: none beyond the capability
    set. Trust: configuration, not model output. Retry/cancellation: n/a
    (pre-model validation). Evidence: ConfigError names the role. Failure:
    raises ConfigError before any model call or budget use.
    """
    if set(role.capabilities) & DELEGATE_FORBIDDEN:
        raise ConfigError(
            f"Delegation role {role_name} must not hold "
            f"{sorted(DELEGATE_FORBIDDEN)}")


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
                 consult=None, delegate=None, goal: str | None = None):
        self.config, self.project, self.role = config, project, role
        self.run_dir, self.snapshot, self.workspace = run_dir, snapshot, workspace
        self.goal = (project.goal if role.workspace == "write" else snapshot["goal"]) if goal is None else goal
        self.goal_digest = digest(self.goal.encode())
        # Capture the inputs actually offered to this run, not arrivals at publication.
        self.inbox_seen = project.insights.generation()
        self.wake_generation = project.control().get("wake_generation", "")
        self.stop = stop or threading.Event()
        self.run_seconds = role_run_seconds(config, role)
        started = time.monotonic()
        self.deadline = started + self.run_seconds if self.run_seconds else None
        # Reserve a final logical-request window, capped to one of the
        # ordinary + three wrap-up windows so short runs still do useful work.
        self.wrap_up_at = (self.deadline - min(config.limits.model_request_seconds,
                                             self.run_seconds / (WRAP_UP_REQUESTS + 1))
                           if self.deadline is not None else None)
        self.request_deadlines: dict[tuple[int, int], float] = {}
        # Bounded aggregate activity evidence: these buckets are exclusive,
        # with joins over tools and tools over logical model-request spans.
        self._activity_origin = started
        self._activity_last = started
        self._activity_current = "other"
        self._activity_seconds: dict[str, float] = {}
        self._activity_counts: dict[str, int] = {}
        self._activity_operation: str | None = None
        self._activity_join: str | None = None
        self._activity_max_requests = 0
        self._activity_reaped_requests = 0
        self.request_failure_pending = False
        self.transport_idle_deadline = started + config.limits.model_request_seconds
        # Never hold mutex in transport polling: handle holds it while joining.
        self.joins_idle = threading.Event()
        self.joins_idle.set()
        self.operation_active = threading.Event()
        self.mutex = threading.RLock()
        self.hello = threading.Event()
        self.request_count = 0
        # Provider adapters restart their local sequence for each execute;
        # scope it to the reused Context's engine invocation.
        self.execution_attempt = 0
        self.request_sequences: set[tuple[int, int]] = set()
        self.runtime_usage: dict[tuple[int, int], dict] = {}
        self.runtime_models: dict[tuple[int, int], dict] = {}
        # Adapter pre-send identities; never supplied by model tool arguments.
        self.engine_tool_invocations: dict[tuple[int, str], str] = {}
        # One conditional-close adapter dispatch per work unit prevents a
        # later conditional result from replacing its retained receipt.
        self.conditional_close_attempted = False
        self.tool_count = 0
        self.progress_tool = False
        self.progress_decision = False
        self.progress_insight = False
        self.progress_input = False
        # Delegation spend sharing: the role whose per-run caps bind spend.
        # Delegation children count against the caller's unit, so theirs
        # points at the caller; everyone else spends their own role.
        self.spend_role = None
        # Per-run wrap-up: which bound was reached plus the spent allowance.
        # Set by a requests/tools refusal or the reserved wall window, so the
        # unit can seal `bounded` instead of failing.
        self.bound_hit: str | None = None
        self.wrap_up_requests = 0
        self.wrap_up_tools = 0
        # Delegated children use an aggregate share of these same fixed
        # allowances; the top-level context retains all unused capacity.
        self.delegated_child = False
        self.delegated_wrap_up_requests = 0
        self.delegated_wrap_up_tools = 0
        self.wrap_up_attempted = False
        self.finished: dict | None = None
        self.missing_finish_evidence: dict | None = None
        # Temporary follow-up grants do not fork an otherwise identical saved session.
        self.session_identity_capabilities: tuple[str, ...] | None = None
        # Host-only authority and permission for a reduced-grant continuation.
        self.turn_authority_role = None
        self.turn_continuation = False
        self.admission_error: str | None = None
        self.admission_wait = False
        # Selector failover identity for durable submissions. Retries and
        # recovery of this candidate keep the same ordinal; only a proven
        # terminal pi-durable unanswered settlement can advance it.
        self.attempt_ordinal = 0
        self.decision_events: list = []
        self.wait_events: list = []
        self.obligations: list = []
        self.closed = False
        # Host-only lease: set by Engine.run while its admission stack owns
        # an execution slot. Ephemeral children never lend that slot onward.
        self._slot_lease = False
        self.operations_stopped = threading.Event()
        self.verification: dict | None = None
        self.commentary: dict | None = None
        self.consult = consult
        self.delegate = delegate
        self.ephemeral = False
        self.sandbox = Sandbox(config, project.root, role.name, run_dir, cancel=self.tools_cancelled)
        self.web = Web(config.web, config.data / "web-cache", run_dir / "sources", role.name)

    def wait_for_joins(self):
        # Explicit stop is shared with children; still drain their cleanup
        # before releasing the parent's workspace lock.
        self.joins_idle.wait()

    def cancel_operations(self):
        # Even an exited/failed parent transport must drain admitted children
        # before releasing the writer lock or cleaning up their shared tree.
        self.wait_for_joins()
        self.operations_stopped.set()

    def tools_cancelled(self):
        return self.operations_stopped.is_set() or self.cancelled()

    def host_cancelled(self) -> bool:
        """Explicit host cancellation, distinct from the adapter run deadline."""
        control = self.project.control()
        return self.stop.is_set() or self.closed or not control.get("armed") or control.get("paused")

    def cancelled(self) -> bool:
        return self.host_cancelled() or (self.joins_idle.is_set() and self.wall_expired())

    def wall_expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def enter_deadline_wrap_up(self):
        if (not getattr(self, "recovery_probe", False) and self.finished is None
                and self.bound_hit is None and self.wrap_up_at is not None
                and time.monotonic() >= self.wrap_up_at):
            self.bound_hit = "deadline"
            return True
        return False

    def enter_drain_wrap_up(self):
        # Transport polling must never wait on a dispatch/join holding mutex.
        # Recheck under the admission mutex so a new request cannot race the
        # safe-boundary decision. Once latched, _budget refuses ordinary work.
        if not self.config.limits.drain_yield or self.ephemeral:
            return
        if not self.mutex.acquire(blocking=False):
            return
        try:
            if (self.finished is not None or self.bound_hit is not None
                    or getattr(self, "recovery_probe", False)
                    or self.request_deadlines or self.request_failure_pending or self.operation_active.is_set()
                    or not self.joins_idle.is_set()
                    or not self.project.control().get("draining")):
                return
            from .engine_config import effective
            if effective(self.config, self.role, self.role.profile).get("session") == "persistent":
                self.bound_hit = "drain"
        finally:
            self.mutex.release()

    def _activity_bucket(self) -> str:
        if self._activity_join is not None:
            return "join:" + self._activity_join
        if self._activity_operation is not None:
            kind = "local_exec" if self._activity_operation in ("exec", "experiment", "verify") else "tool"
            return kind + ":" + self._activity_operation
        if self.request_deadlines:
            return "model_request"
        return "other"

    def _activity_change(self, change, *, count: str | None = None) -> None:
        current = time.monotonic()
        self._activity_seconds[self._activity_current] = (
            self._activity_seconds.get(self._activity_current, 0.0)
            + max(0.0, current - self._activity_last))
        # Advance the accounting boundary even if a state mutation fails, then
        # derive the bucket from the state that actually remains. This prevents
        # an exception from charging the same interval again on the next read.
        self._activity_last = current
        try:
            change()
        finally:
            self._activity_current = self._activity_bucket()
        if count is not None:
            self._activity_counts[count] = self._activity_counts.get(count, 0) + 1

    def activity_summary(self) -> dict:
        """Return bounded exclusive wall-time totals for this Context only."""
        with self.mutex:
            current = time.monotonic()
            seconds = dict(self._activity_seconds)
            seconds[self._activity_current] = (
                seconds.get(self._activity_current, 0.0)
                + max(0.0, current - self._activity_last))
            return {"schema": 1, "elapsed_seconds": round(current - self._activity_origin, 6),
                    "exclusive_seconds": {key: round(value, 6)
                                          for key, value in sorted(seconds.items())},
                    "counts": dict(sorted(self._activity_counts.items())),
                    "max_concurrent_model_requests": self._activity_max_requests,
                    "open_model_requests": len(self.request_deadlines),
                    "reaped_model_requests": self._activity_reaped_requests}

    def clear_request_deadlines(self):
        with self.mutex:
            if self.request_deadlines:
                reaped = len(self.request_deadlines)
                self._activity_change(self.request_deadlines.clear)
                self._activity_reaped_requests += reaped

    def start_request(self, sequence):
        key = (self.execution_attempt, sequence)
        with self.mutex:
            if key in self.request_deadlines:
                return
            deadline = time.monotonic() + self.config.limits.model_request_seconds
            self._activity_change(lambda: self.request_deadlines.setdefault(key, deadline),
                                  count="model_request")
            self._activity_max_requests = max(self._activity_max_requests,
                                               len(self.request_deadlines))

    def end_request(self, sequence):
        key = (self.execution_attempt, sequence)
        with self.mutex:
            if key not in self.request_sequences:
                raise Denied("Unknown completed request")
            if key in self.request_deadlines:
                self._activity_change(lambda: self.request_deadlines.pop(key, None))
                self.transport_idle_deadline = time.monotonic() + self.config.limits.model_request_seconds

    def check_deadlines(self, *, wrap_up=False):
        if self.host_cancelled():
            raise Cancelled("Run stopped, paused or authority revoked")
        if not self.joins_idle.is_set():
            return  # Absolute deadlines keep ticking; never renew on join.
        if self.wall_expired():
            raise RunDeadlineExceeded("Whole-unit wall deadline exhausted")
        requests = tuple(self.request_deadlines.values())
        request_deadline = (min(requests) if requests else
                            None if self.operation_active.is_set() else self.transport_idle_deadline)
        # The reserved wall transition precedes a later request/idle timeout,
        # even if polling sees both at once. Earlier request bounds still fail;
        # neither transition nor joins renew an absolute request timer.
        if wrap_up and (request_deadline is None or
                        (self.wrap_up_at is not None and self.wrap_up_at < request_deadline)):
            self.enter_deadline_wrap_up()
            if self.finished is None and self.bound_hit == "deadline" and not self.wrap_up_attempted:
                raise DeadlineWrapUp(self._bound_message())
        if requests:
            if time.monotonic() >= min(requests):
                raise LimitExceeded("Logical model request deadline exhausted")
        elif not self.operation_active.is_set() and time.monotonic() >= self.transport_idle_deadline:
            raise LimitExceeded("Engine stalled between logical requests")

        if wrap_up:
            self.enter_drain_wrap_up()
            if self.bound_hit == "drain" and not self.wrap_up_attempted and self.finished is None:
                raise DeadlineWrapUp(self._bound_message())

    def transport_deadline(self):
        self.check_deadlines(wrap_up=True)
        if not self.joins_idle.is_set():
            return None
        requests = tuple(self.request_deadlines.values())
        if not requests and self.operation_active.is_set():
            return self.deadline  # Admitted Pi tools retain their own command/adapter bounds.
        request = min(requests) if requests else self.transport_idle_deadline
        return min(self.deadline, request) if self.deadline is not None else request

    def transport_cancelled(self):
        # send_bounded accepts a predicate; receive/process polling preserves
        # the specific timeout type via check_deadlines instead.
        if self.host_cancelled():
            return True
        if not self.joins_idle.is_set():
            return False
        requests = tuple(self.request_deadlines.values())
        if not requests and self.operation_active.is_set():
            return self.wall_expired()
        request = min(requests) if requests else self.transport_idle_deadline
        return self.wall_expired() or time.monotonic() >= request

    def _wrap_up_remaining(self) -> tuple[int, int]:
        if self.delegated_child:
            return (self.config.limits.delegate_wrap_up_requests - self.delegated_wrap_up_requests,
                    self.config.limits.delegate_wrap_up_tools - self.delegated_wrap_up_tools)
        return (WRAP_UP_REQUESTS - self.wrap_up_requests - self.delegated_wrap_up_requests,
                WRAP_UP_TOOLS - self.wrap_up_tools - self.delegated_wrap_up_tools)

    def _bound_message(self) -> str:
        requests_left, tools_left = self._wrap_up_remaining()
        how = f"{max(0, requests_left)} requests and {max(0, tools_left)} wrap-up tools"
        return (f"Per-run {self.bound_hit} bound reached; wrap-up only "
                f"(finish, research, submit_insight, attach_evidence, decide) "
                f"with {how} left")

    def handle(self, operation: str, arguments: dict) -> dict:
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise Denied("Invalid operation")
        with self.mutex:
            if self.host_cancelled() or self.operations_stopped.is_set():
                raise Cancelled("Run is stopped or paused")
            post_seal_done = self.finished is not None and operation == "_request_done"
            if self.wall_expired() and not post_seal_done:
                raise RunDeadlineExceeded("Whole-unit wall deadline exhausted")
            # A seal is irreversible. Refuse before admission-error bookkeeping,
            # counters, handshake or dispatch. Usage adds evidence; _request_done
            # only retires an already-admitted request timer.
            if self.finished is not None and operation not in ("_model_usage", "_request_done"):
                raise Denied("This work unit is sealed; no model requests or tools may run after finish")
            # A request timer is never renewed by bookkeeping or another
            # admission. Usage after a seal remains evidence-only.
            if self.finished is None:
                self.check_deadlines()
            self.enter_deadline_wrap_up()
            if operation == "_request_done":
                sequence = arguments.get("sequence")
                failed = arguments.get("failed", False)
                if type(sequence) is not int or sequence < 1 or type(failed) is not bool:
                    raise Denied("Invalid completed request evidence")
                self.end_request(sequence)
                if failed:
                    self.request_failure_pending = True
                return {"recorded": True}
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
                key = (self.execution_attempt, sequence)
                self.runtime_usage.setdefault(key, usage)
                if isinstance(arguments.get("model"), dict):
                    self.runtime_models.setdefault(key, arguments["model"])
                write_json(self.run_dir / "runtime-usage.json", {"observations": [
                    {"execution_attempt": attempt, "sequence": seq, "usage": value,
                     "model": self.runtime_models.get((attempt, seq))}
                    for (attempt, seq), value in self.runtime_usage.items()]})
                return {"recorded": True}
            if operation == "_engine_tool":
                if self.bound_hit is not None:
                    raise Denied(self._bound_message())
                name = arguments.get("name")
                invocation_id = arguments.get("invocation_id")
                if (not isinstance(invocation_id, str) or not invocation_id.strip()
                        or len(invocation_id) > MAX_ENGINE_TOOL_ID or not invocation_id.isprintable()):
                    raise Denied("Invalid engine tool invocation id")
                if not isinstance(name, str) or name not in self.role.engine_tools:
                    raise Denied("Engine tool has no explicit grant")
                key = (self.execution_attempt, invocation_id)
                if key in self.engine_tool_invocations:
                    if self.engine_tool_invocations[key] != name:
                        raise Denied("Engine tool invocation id reused for a different name")
                    return {"allowed": True}
                if len(self.engine_tool_invocations) >= MAX_ENGINE_TOOL_INVOCATIONS:
                    raise LimitExceeded("Engine tool invocation evidence bound exceeded")
                self.tool_count += 1
                if self.tool_count > role_tools_per_run(self.config, self.spend_role or self.role):
                    # Native tools are never wrap-up operations: record the
                    # bound and refuse without drawing the allowance.
                    self.bound_hit = self.bound_hit or "tools"
                    raise LimitExceeded("Per-run tool limit exceeded")
                self.engine_tool_invocations[key] = name
                self.progress_tool = True
                return {"allowed": True}
            if operation == "_budget":
                self.enter_drain_wrap_up()
                if self.bound_hit == "drain" and not self.wrap_up_attempted:
                    # Reuse the adapters' existing settled request_bound handshake;
                    # this is not a daily-budget rejection or an admitted request.
                    self.drain_admission_refused = True
                    raise DeadlineWrapUp("Per-run provider request budget exhausted")
                try:
                    sequence = arguments.get("sequence")
                    if type(sequence) is not int or sequence < 1:
                        raise Denied("Invalid request sequence")
                    sequence_key = (self.execution_attempt, sequence)
                    if sequence_key not in self.request_sequences:
                        over = self.bound_hit in ("deadline", "drain") or self.request_count >= role_requests_per_run(
                            self.config, self.spend_role or self.role)
                        requests_used = (self.delegated_wrap_up_requests if self.delegated_child else
                                         self.wrap_up_requests + self.delegated_wrap_up_requests)
                        requests_limit = (self.config.limits.delegate_wrap_up_requests if self.delegated_child
                                          else WRAP_UP_REQUESTS)
                        if over and requests_used >= requests_limit:
                            if self.bound_hit == "drain":
                                raise DrainYieldExceeded("Per-run provider request budget exhausted")
                            if self.bound_hit == "deadline":
                                raise RunDeadlineExceeded("Deadline wrap-up request allowance exhausted")
                            raise LimitExceeded("Per-run provider request budget exhausted")
                        try:
                            Budget(self.config.data / "budget", self.config.limits.daily_requests,
                                   self.config.limits.retention_days,
                                   self.config.limits.shared_daily_requests, self.config.timezone).take(
                                       f"{self.run_dir.name}:{self.execution_attempt}:{sequence}",
                                       project=self.project.name)
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
                        if over:
                            # Wrap state records only after daily admission
                            # succeeds: an infrastructure refusal must not
                            # burn the allowance or mislabel the context.
                            self.bound_hit = self.bound_hit or "requests"
                            if self.delegated_child:
                                self.delegated_wrap_up_requests += 1
                            else:
                                self.wrap_up_requests += 1
                        self.request_sequences.add(sequence_key)
                        self.start_request(sequence)
                        self.request_count += 1
                        if hasattr(self, "model_evidence"):
                            self.model_evidence.update(requests=self.request_count, requests_known=True, admission_status="accepted")
                        write_json(self.run_dir / "admission.json", {"requests": self.request_count})
                    return {"admitted": True}
                except MizuError as exc:
                    self.admission_error = str(exc)
                    raise
            if self.bound_hit == "drain" and not self.wrap_up_attempted:
                # Fence this transport after latching the boundary. Even a
                # wrap-up tool must wait for the reduced-grant invocation;
                # otherwise polling could reap it while that tool dispatches.
                raise Denied(self._bound_message())
            if operation not in self.role.capabilities:
                raise Denied(f"Role has no capability: {operation}")
            if self.bound_hit is not None and operation not in WRAP_UP_OPS:
                raise Denied(self._bound_message())
            self.tool_count += 1
            if self.bound_hit in ("deadline", "drain") or self.tool_count > role_tools_per_run(self.config, self.spend_role or self.role):
                self.bound_hit = self.bound_hit or "tools"
                tools_used = (self.delegated_wrap_up_tools if self.delegated_child else
                              self.wrap_up_tools + self.delegated_wrap_up_tools)
                tools_limit = (self.config.limits.delegate_wrap_up_tools if self.delegated_child
                               else WRAP_UP_TOOLS)
                if operation not in WRAP_UP_OPS or tools_used >= tools_limit:
                    if self.bound_hit == "drain" and tools_used >= tools_limit:
                        raise DrainYieldExceeded("Drain wrap-up tool allowance exhausted")
                    if self.bound_hit == "deadline" and tools_used >= tools_limit:
                        raise RunDeadlineExceeded("Deadline wrap-up tool allowance exhausted")
                    raise LimitExceeded("Per-run tool limit exceeded")
                if self.delegated_child:
                    self.delegated_wrap_up_tools += 1
                else:
                    self.wrap_up_tools += 1
            validate(arguments, DEFINITIONS[operation][1])
            self.operation_active.set()
            self._activity_change(lambda: setattr(self, "_activity_operation", operation),
                                  count="tool_call:" + operation)
            try:
                result = self.dispatch(operation, arguments)
                if operation != "finish" and not _is_observational_operation(operation, arguments):
                    self.progress_tool = True
                if operation == "decide":
                    self.progress_decision = True
                if operation in {"submit_insight", "revise_insight", "withdraw_insight", "attach_evidence"}:
                    self.progress_insight = True
                return result
            finally:
                # Completion of actual admitted work is not a heartbeat and
                # never resets an active request (Claude query / Codex turn).
                self.transport_idle_deadline = time.monotonic() + self.config.limits.model_request_seconds
                self._activity_change(lambda: setattr(self, "_activity_operation", None))
                self.operation_active.clear()

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
        # Injected refs live in the project harness store outside the
        # working tree, served under the virtual refs/remotes/upstream/*
        # prefix even for read roles (snapshots capture the workspace
        # only, so materialized inputs lack them by construction).
        try:
            refs = _vcs.list_refs(self.project.root)
        except OSError as exc:
            raise Denied(f"Upstream refs are unavailable: {exc}") from exc
        return [_vcs.REF_PREFIX_PATH + "/" + name for name in refs]

    def _op_read(self, args: dict) -> dict:
        path = args["path"]
        offset, limit = args.get("offset", 0), args.get("limit", READ_PAGE_CHARS)
        expected = args.get("content_id")
        if offset and expected is None:
            raise Denied("Read continuation requires content_id")
        if expected is not None and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
            raise Denied("Invalid read content_id")
        ref = _vcs.split_ref_path(path)
        if ref is not None:
            if self.role.workspace == "none":
                raise Denied("Path is not exposed to this role")
            try:
                text = _vcs.read_ref(self.project.root, ref)
            except OSError as exc:
                raise Denied(f"Upstream refs are unavailable: {exc}") from exc
            data = text.encode("utf-8")
        else:
            if self.role.workspace == "none" or self.project.snapshots.excluded(path):
                raise Denied("Path is not exposed to this role")
            # file_bytes bounds the complete file; READ_PAGE_CHARS separately
            # bounds the decoded text returned in one protocol response.
            data = safe_read(self.workspace, path, self.config.limits.file_bytes)
            text = data.decode("utf-8", "replace")
        content_id = digest(data)
        if expected is not None and expected != content_id:
            raise Denied("Read file changed since the previous page")
        end = min(len(text), offset + limit)
        return {"path": path, "text": text[offset:end], "content_id": content_id,
                "offset": offset, "next_offset": end if end < len(text) else None,
                "truncated": end < len(text)}

    def _read_retained_evidence(self, kind: str, args: dict) -> dict:
        """Read exactly two bounded files; no discovery, fallback or writes.

        started.json is capped at min(file_bytes, 64 KiB); the complete evidence
        record uses file_bytes. Identity is harness-owned run metadata plus the configured caller role;
        record contents remain untrusted. Every page revalidates both files.
        Unsafe/unavailable/corrupt records fail without a retry or history scan.
        """
        run, evidence_id = args["run"], args["evidence_id"]
        if any(not isinstance(value, str) or not EVIDENCE_UUID.fullmatch(value)
               for value in (run, evidence_id)):
            raise Denied("Evidence requires full lowercase 32-hex run and evidence UUIDs")
        offset, limit = args.get("offset", 0), args.get("limit", READ_PAGE_CHARS)
        expected = args.get("content_id")
        if offset and expected is None:
            raise Denied("Evidence continuation requires content_id")
        if expected is not None and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
            raise Denied("Invalid evidence content_id")
        prefix = f"runs/{run}/"
        evidence = f"{kind}/{evidence_id}.json"
        maximum = self.config.limits.file_bytes
        started_maximum = min(maximum, MAX_STARTED_EVIDENCE_BYTES)
        try:
            started = json.loads(safe_read(self.project.root, prefix + "started.json", started_maximum)
                                 .decode("utf-8"), parse_constant=_reject_nonfinite_json)
            if (not isinstance(started, dict) or started.get("run") != run
                    or started.get("role") != self.role.name):
                raise Denied("Evidence run identity or role mismatch")
            data = safe_read(self.project.root, prefix + evidence, maximum)
            text = data.decode("utf-8")
            record = json.loads(text, parse_constant=_reject_nonfinite_json)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise Denied("Corrupt retained evidence or run metadata") from exc
        command = record.get("command") if kind == "experiments" and isinstance(record, dict) else record
        expected_kind = "experiment" if kind == "experiments" else "command"
        if (not isinstance(command, dict) or command.get("id") != evidence_id
                or command.get("kind") != expected_kind or command.get("role") != self.role.name):
            raise Denied("Evidence record identity, kind or role mismatch")
        content_id = digest(data)
        if expected is not None and expected != content_id:
            raise Denied("Evidence changed since the previous page")
        end = min(len(text), offset + limit)
        return {"source": "retained-" + expected_kind + "-evidence", "trust": "recorded-local-untrusted",
                "run": run, "evidence_id": evidence_id, "evidence": evidence,
                "text": text[offset:end], "content_id": content_id, "offset": offset,
                "next_offset": end if end < len(text) else None, "truncated": end < len(text)}

    def _op_command_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("commands", args)

    def _op_experiment_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("experiments", args)

    def _op_exec_experiment(self, op: str, args: dict) -> dict:
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        if op == "exec" and self.role.workspace == "write":
            self.verification = None
        source = {"source_receipt": args["source_receipt"]} if "source_receipt" in args else {}
        result = self.sandbox.execute(self.workspace, args["script"],
                                      writable=self.role.workspace == "write", experiment=op == "experiment", **source)
        if op == "experiment":
            science = {key: args.get(key, "") for key in ("question", "comparison", "measure")}
            write_json(self.run_dir / "experiments" / f"{result['id']}.json", {**science, "command": result})
        # The command record is complete on disk. Tool results have a fixed
        # small shape before execution; escaped output cannot invalidate success.
        response = {key:result[key] for key in ('id','kind','exit_code','reason','seconds','writable') if key in result}
        for key in ('script','stdout','stderr'):
            response[key],response[key+'_truncated'] = text_preview(result.get(key,''))
        response['run'] = self.run_dir.name
        response['evidence'] = ('experiments/' if op == 'experiment' else 'commands/')+result['id']+'.json'
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
        if "receipt_id" in args:
            if "url" in args:
                raise Denied("Supply receipt_id or url, not both")
            record = self.web.replay(args["receipt_id"], "fetch")
        else:
            if "url" not in args:
                raise Denied("Fetch requires url or receipt_id")
            record = bounded(self.config, self.run_dir, "fetch", args["url"], self.tools_cancelled,
                             self.role.name)
        text = record.get('text','')
        offset,limit = args.get('offset',0),args.get('limit',8192)
        end = min(len(text),offset+limit)
        return {**record,'text':text[offset:end],'offset':offset,
                'next_offset':end if end<len(text) else None,'truncated':end<len(text)}

    def _op_probe(self, args: dict) -> dict:
        if "receipt_id" in args:
            if any(key in args for key in ("url", "method", "headers", "body")):
                raise Denied("Supply receipt_id or a new probe request, not both")
            record = self.web.replay(args["receipt_id"], "probe")
        else:
            if "url" not in args or "method" not in args:
                raise Denied("Probe requires url and method, or receipt_id")
            payload = {"url": args["url"], "method": args["method"],
                       "headers": args.get("headers", []), "body": args.get("body", "")}
            record = bounded(self.config, self.run_dir, "probe", payload, self.tools_cancelled,
                             self.role.name)
        text = record.get('text','')
        offset,limit = args.get('offset',0),args.get('limit',8192)
        end = min(len(text),offset+limit)
        return {**record,'text':text[offset:end],'offset':offset,
                'next_offset':end if end<len(text) else None,'truncated':end<len(text)}

    def _op_search(self, args: dict) -> dict:
        if "result_id" in args:
            if "query" in args:
                raise Denied("Supply result_id or query, not both")
            record = self.web.replay(args["result_id"], "search")
        else:
            if "query" not in args:
                raise Denied("Search requires query or result_id")
            record = bounded(self.config, self.run_dir, "search", args["query"], self.tools_cancelled,
                             self.role.name)
            record = self.web.retain("search", record)
        evidence_id = record["result_id"]
        rows=[]
        for item in record.get('results',[]):
            row={}
            for key,bound in (('title',300),('url',4096),('summary',1500),('source',4096)):
                row[key],row[key+'_truncated']=text_preview(item.get(key,''),bound)
            receipt=item.get('source_receipt')
            if isinstance(receipt,str) and DIGEST.fullmatch(receipt):row['source_receipt']=receipt
            score=item.get('score')
            if type(score) in (int,float) and -2**63 <= score <= 2**63:row['score']=score
            rows.append(row)
        response=page(rows,args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
        response['results']=response.pop('items')
        response['result_id'] = evidence_id
        response['evidence']='sources/search-'+evidence_id+'.json'
        for key in ('scope','trust','feeds_total','feeds_consulted'):
            if key in record:response[key]=record[key]
        response['source_truncated']=record.get('truncated',False)
        return response

    def _op_insights(self, args: dict) -> dict:
        if args.get("id"):
            return self.project.insights.read(args["id"])
        # Prompt selection stays at pending_insights; explicit retrieval pages
        # a stable offset-independent selection capped at INSIGHT_RETRIEVAL_LIMIT,
        # so overflow items remain discoverable and small pages enumerate each
        # item once. An explicit action instead pages decided insights (accepted
        # work stays retrievable after it leaves the pending projection).
        action = args.get("action")
        if action is not None:
            if action not in ("accept", "modify", "defer", "reject"):
                raise Denied("Insight listing needs a supported action: accept, modify, defer, or reject")
        projection = self.project.insights.projection(
            limit=INSIGHT_RETRIEVAL_LIMIT, pending=action is None, action=action)
        response = page(projection['items'],args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
        response['insights'] = response.pop('items')
        response['total'] = projection['total']
        response['selection_truncated'] = projection['truncated']
        return response

    def _op_decide(self, args: dict) -> dict:
        return self.project.insights.decide(args["id"], args["action"], args["reason"],
                                            args.get("revisit", ""), self.run_dir.name,
                                            wait=args.get("wait"),
                                            expected_rev=args["rev"])

    def _op_submit_insight(self, args: dict) -> dict:
        # Model submissions carry runtime-bound provenance only (source/run);
        # no origin field is exposed, so stored origin stays unknown and can
        # never elevate to operator authority.
        return self.project.insights.submit(source=self.role.name, title=args["title"], body=args["body"],
                                             base_snapshot=self.snapshot["id"], run=self.run_dir.name,
                                             origin=None)

    def _op_revise_insight(self, args: dict) -> dict:
        # Owner-only revision: source is fixed to the caller's role and the
        # mechanism (insights.revise) enforces the source match plus
        # compare-and-swap on the read rev. The revision is rebased onto the
        # current snapshot, like a fresh submission about the current tree.
        return self.project.insights.revise(args["id"], source=self.role.name, title=args["title"],
                                             body=args["body"], base_snapshot=self.snapshot["id"],
                                             run=self.run_dir.name, expected_rev=args["rev"])

    def _op_withdraw_insight(self, args: dict) -> dict:
        # Owner-only withdrawal: source is fixed to the caller's role and the
        # mechanism (insights.withdraw) enforces submitter-or-operator plus
        # compare-and-swap on the read rev. Operator withdrawal stays on the CLI.
        return self.project.insights.withdraw(args["id"], source=self.role.name, reason=args["reason"],
                                               expected_rev=args["rev"], run=self.run_dir.name)

    def _op_attach_evidence(self, args: dict) -> dict:
        # Attributed annotation of another insight: source is fixed to the
        # caller's role and the mechanism (insights.attach_evidence) binds to
        # the read rev without changing title, body, or rev, so ownership and
        # revise/withdraw rights stay with the submitter only.
        record = self.project.insights.attach_evidence(
            args["id"], source=self.role.name, text=args["text"],
            run=self.run_dir.name, expected_rev=args["rev"])
        write_json(self.run_dir / "evidence-attach.json",
                   {"insight": record["id"], "rev": record["rev"],
                    "author": self.role.name, "run": self.run_dir.name,
                    "evidence_count": len(record.get("evidence") or [])})
        return record

    def _op_sync(self, args: dict) -> dict:
        # Host-side refresh only: fetch upstream refs via the trusted adapter
        # and inject them read-only into the harness store (virtual
        # refs/remotes/upstream/*, never inside the working tree). Merging is
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
            receipt = _vcs.inject_refs(self.project.root, fetched["refs"])
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

    def _op_research_read(self, args: dict) -> dict:
        # Own role's current research record with its generation: the
        # read half of the replace cycle. No evidence file: read-only,
        # and the run already records the call. Other roles' records
        # are never addressable; the role name is the caller's own.
        seen = self.project.role_state.read(self.role.name)
        return {"role": self.role.name, **seen}

    def _op_research(self, args: dict) -> dict:
        # Replacement-only update of the own role's record: the state
        # arrives as a JSON string (protocol schemas cannot express a
        # free-form bounded object), parses here, then replaces under
        # the generation the caller read. Stale, invalid, or
        # unavailable states are Denied with the stored record
        # untouched; the receipt carries the new generation plus the
        # stored state as run-record audit.
        try:
            state = json.loads(args["state"], parse_constant=_reject_constant)
        except ValueError as exc:
            raise Denied("Research state must parse as JSON") from exc
        out = self.project.role_state.replace(
            self.role.name, state,
            expected_generation=args["expected_generation"],
            run=self.run_dir.name)
        record = {"role": self.role.name, "generation": out["generation"],
                  "updated_at": out["updated_at"], "state": state,
                  "evidence": "research-state.json"}
        write_json(self.run_dir / "research-state.json", record)
        return record

    def _op_vcs_read(self, args: dict) -> dict:
        # Read-only VCS view: CI status, logs, PR comments, external
        # proposals, and CI-recorded artifact files via the trusted
        # adapter. Never publishes; mutating ops are refused here even if
        # the adapter would serve them. Requires a visible workspace.
        # Branch stays required for addressed reads
        # (status/log/comments/artifacts/artifact) inside read_via;
        # proposals enumerate with an optional branch filter; acquire
        # takes a proposal id, the exact head sha, and the explicit
        # content scope (plus an optional base sha pin for full scope)
        # and needs no branch; artifact takes a branch, an artifact file
        # name, and an optional sha filter.
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        params = {}
        if "branch" in args:
            params["branch"] = args["branch"]
        if "sha" in args:
            params["sha"] = args["sha"]
        if "id" in args:
            params["id"] = args["id"]
        if "cursor" in args:
            params["cursor"] = args["cursor"]
        if "scope" in args:
            params["scope"] = args["scope"]
        if "base_sha" in args:
            params["base_sha"] = args["base_sha"]
        if "artifact" in args:
            params["artifact"] = args["artifact"]
        try:
            data = _vcs.read_via(self.config.vcs, args["op"], params)
        except OSError as exc:
            raise Denied(f"VCS read is unavailable: {exc}") from exc
        record = {"op": args["op"], "branch": args.get("branch"), "trust": "external-untrusted",
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

    def _op_vcs_retire(self, args: dict) -> dict:
        # Branch retirement: delete an owned temporary integration branch
        # at an exact expected sha behind the configured retire grant.
        # Classification is enforced inside retire_via (owned only);
        # protected, external, tracking, and other refs are preserved.
        # A failed call implies nothing about the remote ref: reconcile by
        # re-observing, never by assuming deletion. Retirement is tied to
        # terminal disposition twice: recorded observations refuse early
        # before dispatch (they may be stale), and the provider must
        # affirm atomically with the delete that no live proposal
        # references the branch, naming blockers in live_refs.
        # Integration authority is the capability plus the configured
        # grant, never source-write access: read-workspace integrators
        # retire without touching the shared workspace.
        if self.role.workspace == "none":
            raise Denied("Branch retirement requires a visible workspace")
        blocking = _vcs.live_proposal_refs(self.project, args.get("branch"))
        if blocking:
            raise Denied("Branch retirement is blocked by open proposal "
                         f"reference(s) {', '.join(blocking)}; dispose the "
                         "proposal to a terminal state and re-observe before "
                         "retiring")
        try:
            data = _vcs.retire_via(self.config.vcs, args.get("branch"),
                                   args.get("expected_sha"))
        except OSError as exc:
            raise Denied(f"Branch retirement is unavailable: {exc}") from exc
        record = {"retired": True, "branch": data["branch"], "sha": data["sha"],
                  "classification": data["classification"],
                  "live_refs": data["live_refs"],
                  "trust": "external-untrusted", "evidence": "vcs-retire.json",
                  "result": data}
        write_json(self.run_dir / "vcs-retire.json", data)
        return record

    def _op_vcs_dispose(self, args: dict) -> dict:
        # Proposal disposition: close/merge an external proposal at the
        # exact assessed head, base, and target behind the matching
        # per-action grant; close_if_adopted is a separately pinned,
        # adapter-conditional close that retains even a refusal receipt.
        # Close is routine terminal reconciliation
        # behind close_grant alone. Merge promotes the integrated tree:
        # it additionally requires a recorded human GO approval for the
        # tree, checked before the adapter spawns, so main/release
        # promotion without a matching approval is refused and approval
        # is never inferred from a role or CI. The adapter must act only
        # while the proposal is still open at all assessed endpoints and
        # echo them with the terminal state. Never automatic; a failed
        # call implies nothing about the remote proposal.
        # Integration authority is the capability plus the configured
        # grant (and approval for merges), never source-write access:
        # read-workspace integrators dispose from their materialized
        # input, whose digest binds a merge approval exactly like a
        # writable workspace capture. The approval additionally binds the
        # assessed destination and proposal endpoints, checked here before
        # the adapter spawns: a GO for another target or other proposal
        # content never authorizes this merge.
        if self.role.workspace == "none":
            raise Denied("Proposal disposition requires a visible workspace")
        op = args.get("op")
        if op == "close_if_adopted":
            expected = {"op", *_vcs.CLOSE_IF_ADOPTED_PINS}
            if set(args) != expected:
                raise Denied("Conditional close requires op and exactly the six observed pins")
            pins = {key: args[key] for key in _vcs.CLOSE_IF_ADOPTED_PINS}

            def reserve_conditional_close():
                # Called only after local grant/pin validation and immediately
                # before adapter dispatch. Failed local input can be corrected;
                # a dispatched attempt can never overwrite or retry its receipt.
                if self.conditional_close_attempted:
                    raise Denied("Conditional proposal close was already attempted in this work unit")
                self.conditional_close_attempted = True

            try:
                data = _vcs.close_if_adopted_via(
                    self.config.vcs, pins, before_invoke=reserve_conditional_close)
            except OSError as exc:
                raise Denied(f"Conditional proposal close is unavailable: {exc}") from exc
            record = {"disposed": data["closed"], "op": data["op"],
                      **pins, "predicate": data["predicate"],
                      "closed": data["closed"],
                      "trust": "external-untrusted",
                      "evidence": "vcs-dispose.json",
                      "adapter_receipt": data["adapter_receipt"]}
            # A valid refusal is durable evidence too: do not refetch,
            # retry, fall through to merge, or treat false as an exception.
            write_json(self.run_dir / "vcs-dispose.json", record)
            return record
        if op not in ("close", "merge") or set(args) != {
                "op", "id", "sha", "base", "target", "branch"}:
            raise Denied("Invalid proposal disposition arguments")
        approval = None
        code_digest = None
        if op == "merge":
            captured = self.project.snapshots.capture_files(self.workspace)
            if captured.get("skipped"):
                raise Denied("Proposal disposition requires a representable snapshot")
            code_digest = captured["code_digest"]
            approval = _vcs.require_go_approval(
                self.project, args["branch"], code_digest,
                endpoints={"target": args.get("target"), "id": args.get("id"),
                           "sha": args.get("sha"), "base": args.get("base")})
        try:
            data = _vcs.dispose_via(self.config.vcs, args["op"], args.get("id"),
                                    args.get("sha"), args.get("base"),
                                    args.get("target"))
        except OSError as exc:
            raise Denied(f"Proposal disposition is unavailable: {exc}") from exc
        record = {"disposed": True, "op": args["op"], "id": data["id"],
                  "sha": data["sha"], "base": data["base"],
                  "target": data["target"], "state": data["state"],
                  "branch": args["branch"],
                  "trust": "external-untrusted", "evidence": "vcs-dispose.json",
                  "result": data}
        if approval is not None:
            record["code_digest"] = code_digest
            record["approval"] = approval["insight"]
        write_json(self.run_dir / "vcs-dispose.json", record)
        return record

    def _op_materialize(self, args: dict) -> dict:
        # Generic offline-input materialization: trusted host adapter warms
        # pre-mounted read-only caches and/or returns digest-bound content
        # (e.g. an updated lockfile). The worker applies content as ordinary
        # workspace edits followed by verification; the mechanism never mounts
        # adapter paths and never grants network to the sandbox. Requires a
        # visible workspace; fail closed without configuration or grant.
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        try:
            data = _materialize.invoke(self.config.materialize, args["spec"])
        except OSError as exc:
            raise Denied(f"Materialization is unavailable: {exc}") from exc
        record = {"spec": args["spec"], "digest": data["digest"],
                  "cache": data["cache"], "trust": "external-untrusted",
                  "evidence": "materialize.json", "result": data}
        write_json(self.run_dir / "materialize.json", data)
        return record

    def _op_consult(self, args: dict) -> dict:
        if self.consult is None:
            raise Denied("Nested consultation is disabled")
        self.joins_idle.clear()
        self._activity_change(lambda: setattr(self, "_activity_join", "consult"), count="join:consult")
        try:
            return self.consult(self, args)
        finally:
            self._activity_change(lambda: setattr(self, "_activity_join", None))
            self.joins_idle.set()

    def _op_delegate(self, args: dict) -> dict:
        if self.delegate is None:
            raise Denied("Nested delegation is disabled")
        self.joins_idle.clear()
        self._activity_change(lambda: setattr(self, "_activity_join", "delegate"), count="join:delegate")
        try:
            return self.delegate(self, args)
        finally:
            self._activity_change(lambda: setattr(self, "_activity_join", None))
            self.joins_idle.set()

    def _op_report(self, args: dict) -> dict:
        self.commentary = args
        write_json(self.run_dir / "commentary.json", args)
        return {"staged": True}

    def _op_finish(self, args: dict) -> dict:
        if not args["summary"].strip():
            raise Denied("A nonempty work summary is required")
        from .routing import check_recommendation
        profile, reason = check_recommendation(args.get("next_profile"), args.get("next_reason"))
        args = {k: v for k, v in args.items() if k not in ("next_profile", "next_reason")}
        if profile is not None:
            args["next_profile"] = profile
        if reason is not None:
            args["next_reason"] = reason
        if self.role.workspace == "write":
            if not args.get("state", "").strip():
                raise Denied("Writable roles must provide the updated short state")
            if args["outcome"] == "done" and not (self.verification and self.verification["passed"]):
                raise Denied("Completion requires passing the configured acceptance commands")
        wait = min(args.get("wait_seconds", self.config.limits.default_wait_seconds),
                   self.config.limits.maximum_wait_seconds)
        if self.bound_hit is not None and self.bound_hit != "drain":
            # A seal after a spend bound is a bounded end, never a fresh
            # verdict: the unit saved what it could and runs again.
            args = {**args, "outcome": "bounded"}
        finished = {**args, "wait_seconds": wait, "recorded_at": now(),
                    **({"bound": self.bound_hit} if self.bound_hit is not None else {})}
        write_json(self.run_dir / "finish-intent.json", finished)
        self.finished = finished
        return {"sealed": True}

    def _op_exec(self, args: dict) -> dict:
        return self._op_exec_experiment("exec", args)

    def _op_experiment(self, args: dict) -> dict:
        return self._op_exec_experiment("experiment", args)

    _DISPATCH = {
        "diff": _op_diff, "files": _op_files, "read": _op_read,
        "command_evidence_read": _op_command_evidence_read,
        "experiment_evidence_read": _op_experiment_evidence_read,
        "exec": _op_exec, "experiment": _op_experiment, "verify": _op_verify,
        "fetch": _op_fetch, "probe": _op_probe, "search": _op_search, "insights": _op_insights,
        "decide": _op_decide, "submit_insight": _op_submit_insight,
        "revise_insight": _op_revise_insight, "withdraw_insight": _op_withdraw_insight,
        "attach_evidence": _op_attach_evidence,
        "sync": _op_sync, "vcs_read": _op_vcs_read, "vcs_publish": _op_vcs_publish,
        "vcs_retire": _op_vcs_retire, "vcs_dispose": _op_vcs_dispose,
        "materialize": _op_materialize,
        "consult": _op_consult, "delegate": _op_delegate, "report": _op_report,
        "research_read": _op_research_read, "research": _op_research,
        "finish": _op_finish,
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
    receipt = _vcs.inject_refs(project.root, fetched["refs"])
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


def vcs_poll_proposals_enabled(config) -> bool:
    """True only when global VCS polling and proposal polling are opted in."""
    try:
        return (vcs_poll_enabled(config)
                and config.vcs.get("poll_proposals_enabled") is True)
    except (AttributeError, TypeError):
        return False


#: Shared per-project poll state (persisted timestamps) and lock name.
POLL_STATE_FILE = "vcs-poll.json"
POLL_LOCK_NAME = "vcs-poll.lock"

#: Bound for the shared current-CI-failure fact (branches x checks). The
#: prompt carries at most this many flattened rows so resumed-session
#: deltas stay flat; the poll store itself is pruned to valid shapes.
MAX_CI_FAILURES = 32
#: Bound for stored per-branch check rows (keeps one tick short and the
#: shared file small).
MAX_CI_CHECKS_PER_BRANCH = 16
#: Bound for stored branches with failures (rotation prunes absent refs).
MAX_CI_FAILURE_BRANCHES = 128
#: Opaque adapter pagination tokens are persisted for resumption, but never
#: rendered into events or prompts. The VCS schema fixes this bound.
MAX_PROPOSAL_CURSOR = 256


def _valid_proposal_cursor(value) -> str | None:
    """Return a usable opaque proposal cursor, else None (never raises)."""
    try:
        if not isinstance(value, str) or not value or len(value) > MAX_PROPOSAL_CURSOR:
            return None
        _vcs._reject_terminal_controls(value, "Invalid VCS proposals cursor")
        return value
    except Denied:
        return None


def _valid_ci_run(value) -> str | None:
    """Return a valid adapter run identity or ``None`` (never raises).

    Schema: opaque string 1-128 chars without newline/NUL or terminal
    controls. Invalid reads as absent so rows without identity keep
    exactly ``{sha, check}`` and all existing behavior holds.
    """
    try:
        if not isinstance(value, str) or not value or len(value) > 128:
            return None
        if "\n" in value or "\x00" in value:
            return None
        from .vcs import _reject_terminal_controls as _reject_controls
        _reject_controls(value, "Invalid CI run identity")
        return value
    except Exception:
        return None


def _clean_ci_failures(value) -> dict:
    """Normalize a stored ``{branch: [{sha, check, run?}]}`` mapping (never raises)."""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    try:
        from .vcs import check_branch as _check_branch
        from .vcs import _SHA as _sha_re
    except Exception:
        return {}
    for branch, rows in list(value.items())[:MAX_CI_FAILURE_BRANCHES]:
        try:
            _check_branch(branch)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        kept = []
        for entry in rows[:MAX_CI_CHECKS_PER_BRANCH]:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha")
            check = entry.get("check")
            try:
                if not isinstance(sha, str) or not _sha_re.fullmatch(sha):
                    continue
                if (not isinstance(check, str) or not check
                        or len(check) > 256 or "\n" in check or "\x00" in check):
                    continue
            except Exception:
                continue
            row = {"sha": sha, "check": check}
            kept_run = _valid_ci_run(entry.get("run"))
            if kept_run is not None:
                row["run"] = kept_run
            kept.append(row)
        out[branch] = kept
    return out


def current_ci_failures(project, *, limit: int = MAX_CI_FAILURES) -> list:
    """Return cached current CI failures as ``[{branch, sha, check, run?}]`` (never raises).

    Schema: best-effort read of the shared ``vcs-poll.json`` ``failures``
    mapping written by :func:`poll_ci`; flattened branch-major, sorted,
    capped at ``limit``. Empty means no observed red (poll disabled,
    never polled, unreadable, or last ticks green). Trust: host-side
    poll cache, adapter facts stay external-untrusted; insight decisions
    are deliberately ignored so a deferred failure stays current until
    green or superseded. Failure: never raises (unreadable reads as none).
    """
    try:
        raw = read_json(project.root / POLL_STATE_FILE, {})
    except Exception:
        return []
    try:
        if not isinstance(raw, dict):
            return []
        cleaned = _clean_ci_failures(raw.get("failures"))
        flat = []
        for branch in sorted(cleaned):
            for entry in cleaned[branch]:
                row = {"branch": branch, "sha": entry["sha"], "check": entry["check"]}
                kept_run = _valid_ci_run(entry.get("run"))
                if kept_run is not None:
                    row["run"] = kept_run
                flat.append(row)
        return flat[:max(0, int(limit))]
    except Exception:
        return []


def wake_ci_failures(project, config=None, *, limit: int = MAX_CI_FAILURES) -> list:
    """Cached CI failures that may wake a parked writer (never raises).

    Schema: filters :func:`current_ci_failures` to the operator-selected
    ``[vcs] ci_branch``; an empty branch disables the wake (fail closed).
    Other branches stay visible in ``ci_failures`` prompts but never admit
    a run, so red on unrelated refs cannot spend the writer budget.
    Bounds: same cap as ``current_ci_failures``. Trust: host-side poll
    cache plus operator configuration. Failure: never raises (unknown
    configuration reads as no wake).
    """
    try:
        settings = config.vcs if config is not None else getattr(project, "config", {}).vcs if hasattr(getattr(project, "config", {}), "vcs") else {}
        # Config.vcs is a dict; project without config reads as no wake.
        try:
            branch = settings.get("ci_branch") if isinstance(settings, dict) else None
        except Exception:
            branch = None
        if not branch or not isinstance(branch, str):
            return []
        return [entry for entry in current_ci_failures(project, limit=limit)
                if entry.get("branch") == branch][:max(0, int(limit))]
    except Exception:
        return []


#: Default re-wake interval for an unchanged red (seconds). An unchanged
#: red must still re-wake eventually so it can never park a writer forever.
DEFAULT_CI_REWAKE_SECONDS = 1800
#: Version of the per-role CI wake record.
CI_WAKE_VERSION = 1


def _ci_rewake_seconds(project_config) -> float:
    """Configured ``[vcs] ci_rewake_seconds`` (never raises).

    Schema: operator config. Trust: configuration. Failure: never raises;
    missing or garbage reads as the 1800 default since tests build vcs
    dicts by hand.
    """
    try:
        settings = getattr(project_config, "vcs", None)
        if settings is None and isinstance(project_config, dict):
            settings = project_config
        raw = settings.get("ci_rewake_seconds", 1800) if isinstance(settings, dict) else 1800
    except Exception:
        return 1800.0
    try:
        if type(raw) is bool:
            return 1800.0
        value = float(raw) if isinstance(raw, (int, float)) else None
        if value is None:
            return 1800.0
        import math as _math
        if not _math.isfinite(value) or not 60 <= value <= 86400:
            return 1800.0
        return value
    except Exception:
        return 1800.0


def _ci_wake_path(project, role_name):
    """Per-role CI wake record path (None when unusable, never raises)."""
    try:
        name = str(role_name)
        if not name or "/" in name or "\\" in name or "\x00" in name or ".." in name:
            return None
        return project.root / f"vcs-ci-wake-{name}.json"
    except Exception:
        return None


def read_ci_wake(project, role_name):
    """Last CI wake record for one role (never raises).

    Schema: ``{'version': 1, 'branch': str, 'fact': [{'sha', 'check'}],
    'snapshot': str, 'at': float}``. Missing or malformed reads as None;
    wake suppression must never break dispatch.
    """
    try:
        path = _ci_wake_path(project, role_name)
        if path is None:
            return None
        raw = read_json(path, None)
        if raw is None or not isinstance(raw, dict):
            return None
        return raw
    except Exception:
        return None


def write_ci_wake(project, role_name, branch, present, snapshot_id, now=None) -> None:
    """Record that one role observed the current red (never raises).

    Schema: ``branch`` is the ``ci_branch`` string, ``present`` is the
    presence list (``[{branch, sha, check, run?}]``), ``snapshot_id`` is the
    published head observed, ``now`` is epoch seconds. Malformed inputs
    write nothing; wake suppression must never break dispatch. The fact
    carries ``run`` when the entry has a valid identity and omits it
    otherwise (prompts likewise include ``run`` only when sent).
    """
    try:
        path = _ci_wake_path(project, role_name)
        if path is None:
            return
        if not isinstance(branch, str) or not branch:
            return
        if not isinstance(snapshot_id, str) or not snapshot_id:
            return
        try:
            at = time.time() if now is None else float(now)
        except Exception:
            return
        import math as _math
        if not _math.isfinite(at):
            return
        fact = []
        try:
            for entry in present or []:
                if not isinstance(entry, dict):
                    continue
                sha = entry.get("sha")
                check = entry.get("check")
                if not isinstance(sha, str) or not isinstance(check, str):
                    continue
                row = {"sha": sha, "check": check}
                kept_run = _valid_ci_run(entry.get("run"))
                if kept_run is not None:
                    row["run"] = kept_run
                fact.append(row)
        except Exception:
            return
        # Graceful upgrade without a version bump: old records without
        # ``run`` keys mismatch new facts with ``run`` keys exactly once
        # (due -> wake -> rewrite), then stay parked until the run changes.
        fact.sort(key=lambda item: (item["sha"], item["check"], item.get("run", "")))
        write_json(path, {"version": CI_WAKE_VERSION, "branch": branch,
                          "fact": fact, "snapshot": snapshot_id, "at": at})
    except Exception:
        return


def _ci_due(present, record, snapshot_id, now, rewake_seconds) -> bool:
    """True when a present red is due to wake (fail open, never raises).

    Schema: ``present`` is the ``wake_ci_failures`` list, ``record`` is the
    last :func:`read_ci_wake` dict (or None), ``snapshot_id`` is the current
    published head, ``now`` is epoch seconds. Due on first sighting (no
    record), a changed branch/fact (new failing sha, check, or run identity,
    including a branch switch), a moved head, or an elapsed ``rewake_seconds``
    interval.
    An empty presence is never due; a malformed record/at reads as due so a
    red is never silently deferred.
    """
    try:
        if not present:
            return False
        if not isinstance(record, dict):
            return True
        try:
            current_branch = present[0].get("branch") if isinstance(present[0], dict) else None
        except Exception:
            return True
        record_branch = record.get("branch")
        if not isinstance(current_branch, str) or not isinstance(record_branch, str):
            return True
        if record_branch != current_branch:
            return True
        if record.get("version") != CI_WAKE_VERSION:
            return True
        try:
            current_rows = []
            for entry in present:
                if (not isinstance(entry, dict)
                        or not isinstance(entry.get("sha"), str)
                        or not isinstance(entry.get("check"), str)):
                    continue
                row = {"sha": entry["sha"], "check": entry["check"]}
                kept_run = _valid_ci_run(entry.get("run"))
                if kept_run is not None:
                    row["run"] = kept_run
                current_rows.append(row)
            current_fact = sorted(
                current_rows,
                key=lambda item: (item["sha"], item["check"], item.get("run", "")))
        except Exception:
            return True
        raw_fact = record.get("fact")
        if not isinstance(raw_fact, list):
            return True
        try:
            record_fact = []
            for entry in raw_fact:
                if not isinstance(entry, dict):
                    return True
                sha = entry.get("sha")
                check = entry.get("check")
                if not isinstance(sha, str) or not isinstance(check, str):
                    return True
                row = {"sha": sha, "check": check}
                if "run" in entry:
                    kept_run = _valid_ci_run(entry.get("run"))
                    if kept_run is None:
                        return True
                    row["run"] = kept_run
                record_fact.append(row)
            # Old records without ``run`` mismatch new facts with ``run``
            # exactly once (graceful upgrade, no version bump).
            record_fact.sort(key=lambda item: (item["sha"], item["check"], item.get("run", "")))
        except Exception:
            return True
        if record_fact != current_fact:
            return True
        record_snapshot = record.get("snapshot")
        if not isinstance(record_snapshot, str) or not isinstance(snapshot_id, str):
            return True
        if record_snapshot != snapshot_id:
            return True
        try:
            at = record.get("at")
            if type(at) is bool:
                return True
            at_value = float(at)
            now_value = float(now)
            rewake_value = float(rewake_seconds)
            import math as _math
            if not _math.isfinite(at_value) or not _math.isfinite(now_value):
                return True
            if not _math.isfinite(rewake_value):
                return True
        except Exception:
            return True
        return (now_value - at_value) >= rewake_value
    except Exception:
        return True


def _ci_due_for(project, project_config, role, snapshot_id=None, now=None) -> list:
    """Cached CI failures that may admit one role (never raises).

    A red ``ci_branch`` wakes only roles with ``ci_wake`` (writable roles by
    default): event-triggered or unchanged-code dispatch for any other role
    exits cheaply instead of spending a run on CI it never subscribed to.
    The red stays visible via ``current_ci_failures`` prompts, but it wakes
    only on change: a new failing sha/check, a moved published head, or an
    elapsed ``ci_rewake_seconds`` interval. Without ``snapshot_id``/``now``
    this falls back to presence (as before) so project-level checks stay
    conservative.
    """
    try:
        if isinstance(role, str):
            role_name = role
            if not _ci_wake_enabled(project_config, role):
                return []
        else:
            try:
                role_name = getattr(role, "name", None)
            except Exception:
                role_name = None
            try:
                if not bool(getattr(role, "ci_wake", False)):
                    return []
            except Exception:
                return []
        try:
            present = wake_ci_failures(project, project_config)
        except Exception:
            return []
        if not present:
            return []
        if snapshot_id is None or now is None:
            return present
        if not isinstance(role_name, str) or not role_name:
            return present
        try:
            rewake = _ci_rewake_seconds(project_config)
        except Exception:
            rewake = 1800.0
        try:
            record = read_ci_wake(project, role_name)
        except Exception:
            return present
        try:
            if _ci_due(present, record, snapshot_id, now, rewake):
                return present
            return []
        except Exception:
            return present
    except Exception:
        try:
            return wake_ci_failures(project, project_config)
        except Exception:
            return []


def _ci_wake_enabled(project_config, role_name) -> bool:
    """True when the named role opts into the CI-red wake (never raises)."""
    try:
        roles = getattr(project_config, "roles", None) or {}
        role = roles.get(role_name) if isinstance(roles, dict) else None
        return bool(getattr(role, "ci_wake", False))
    except Exception:
        return False


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
    (``vcs.list_refs``): all of them when they fit ``poll_max_branches``,
    else a rotating window tracked in ``state["ci_offset"]`` so every branch
    is covered within ceil(refs/max) ticks instead of pinning the sorted
    tail; explicit lists are validated as branch names. Returns a ``ci_poll``
    event dict or ``None`` when skipped (disabled or not due). Bounds: one
    adapter call per branch per due poll under the adapter timeout; status
    checks capped at 1024 rows each. Trust: host side only; adapter facts
    stay external-untrusted and only ``failure`` states are recorded via
    ``vcs.record_ci_result`` (stable dedup IDs, passes ignored).
    Retry/cancellation: no retry; ``last_poll`` and the rotation cursor
    advance even on failure so one bad adapter cannot busy-loop or pin the
    window. Evidence: the event names ok/recorded branches/counts/total or
    error; this helper never publishes a snapshot. Failure: ``Denied``,
    ``OSError`` and ``ValueError`` become ``ok=False`` events, never raised.
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
                names = sorted(_vcs.list_refs(project.root))
            except OSError as exc:
                raise Denied(f"CI polling is unavailable: {exc}") from exc
            branches_total = len(names)
            if len(names) <= max_branches:
                # Fall back to main so a fresh workspace still polls once configured.
                targets = names or ["main"]
            else:
                offset = state.get("ci_offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                start = offset % len(names)
                targets = [names[(start + i) % len(names)] for i in range(max_branches)]
                state["ci_offset"] = (start + max_branches) % len(names)
        else:
            targets = [_vcs.check_branch(b) for b in branches]
            branches_total = len(targets)
        recorded: list[dict] = []
        failures = 0
        tick: dict = {}
        for branch in targets:
            data = do_read(config.vcs, "status", {"branch": branch})
            checks = data.get("checks")
            if not isinstance(checks, list):
                raise Denied("VCS status must carry a checks array")
            rows = []
            for entry in checks:
                if entry.get("state") != "failure":
                    continue
                receipt = _vcs.record_ci_result(
                    project, branch=branch, sha=entry["sha"],
                    check=entry["check"], state="failure",
                    url=entry.get("url", ""), run=entry.get("run"))
                failures += 1
                tick_row = {"sha": entry["sha"], "check": entry["check"]}
                tick_run = _valid_ci_run(entry.get("run"))
                if tick_run is not None:
                    tick_row["run"] = tick_run
                rows.append(tick_row)
                if receipt.get("recorded"):
                    recorded.append({"id": receipt["id"], "branch": branch,
                                     "check": entry["check"]})
            tick[branch] = rows[:MAX_CI_CHECKS_PER_BRANCH]
        failures_map = _clean_ci_failures(state.get("failures"))
        for branch, rows in tick.items():
            failures_map[branch] = rows
        if branches is None:
            try:
                live = set(sorted(_vcs.list_refs(project.root)))
            except OSError:
                live = None
            if live is not None:
                if len(live) <= max_branches:
                    live.add("main")
                for stale in [b for b in failures_map if b not in live]:
                    failures_map.pop(stale, None)
        for empty in [b for b, rows in failures_map.items() if not rows]:
            failures_map.pop(empty, None)
        while len(failures_map) > MAX_CI_FAILURE_BRANCHES:
            failures_map.pop(sorted(failures_map)[0], None)
        state["failures"] = failures_map
        state["last_error"] = ""
        state["last_ok"] = now
        return {"event": "ci_poll", "ok": True, "branches": list(targets),
                "branches_total": branches_total,
                "failures": failures, "recorded": recorded,
                "trust": "external-untrusted", "time": now_iso()}
    except (Denied, OSError, ValueError) as exc:
        state["last_error"] = str(exc)
        return {"event": "ci_poll", "ok": False,
                "error": str(exc), "time": now_iso()}


def poll_proposals(config, project, state: dict, *, now: float,
                   interval: float, reader=None) -> dict | None:
    """Observe at most one external-proposal page on each opted-in tick.

    Schema: state stores ``last_proposal_poll`` and an opaque
    ``proposal_cursor`` across ticks. Bounds: one normalized proposal page
    (at most 256 rows) per due tick, using the existing status timeout cap.
    Trust: host-side observation only; validated rows are recorded through
    ``vcs.record_proposal_state`` and never imply state for missing rows.
    Retry/cancellation: no same-tick retries; the timestamp advances on all
    failures, while the cursor advances only after the entire page is
    recorded successfully. Evidence: one bounded ``proposal_poll`` event.
    Failure: adapter, malformed-page, or recording errors are visible as
    ``ok=False``; a failed page preserves its input cursor for retry.
    """
    if not vcs_poll_proposals_enabled(config):
        return None
    if not upstream_fetch_due(state.get("last_proposal_poll"), now, interval):
        return None
    state["last_proposal_poll"] = now
    cursor = _valid_proposal_cursor(state.get("proposal_cursor"))
    params = {"cursor": cursor} if cursor is not None else {}

    def _default_reader(settings, op, request):
        return _vcs.read_via(
            capped_vcs_settings(settings, vcs_poll_status_timeout(config)),
            op, request)

    do_read = reader if reader is not None else _default_reader
    observed = 0
    recorded = 0
    changed = 0
    try:
        data = do_read(config.vcs, "proposals", params)
        if not isinstance(data, dict):
            raise Denied("VCS proposals response must be an object")
        rows = data.get("proposals")
        complete = data.get("complete")
        next_cursor = data.get("cursor")
        if not isinstance(rows, list) or len(rows) > _vcs.MAX_PROPOSALS:
            raise Denied("VCS proposals response must carry a bounded proposals array")
        if complete is not None and not isinstance(complete, bool):
            raise Denied("Invalid VCS proposals completeness flag")
        if next_cursor is not None and _valid_proposal_cursor(next_cursor) is None:
            raise Denied("Invalid VCS proposals cursor")
        if complete is False and next_cursor is None:
            raise Denied("Incomplete VCS proposals page must carry a cursor")
        if complete is False and next_cursor == cursor:
            raise Denied("Incomplete VCS proposals cursor did not advance")

        observed = len(rows)
        for row in rows:
            receipt = _vcs.record_proposal_state(project, row, origin="daemon")
            recorded += 1
            if receipt.get("changed") is True:
                changed += 1

        # Do not move/clear the cursor until every validated row has been
        # recorded. Re-reading a partially committed page is safe because
        # record_proposal_state has stable, change-gated deduplication.
        if complete is True:
            state["proposal_cursor"] = None
        elif next_cursor is not None:
            state["proposal_cursor"] = next_cursor
        else:
            # No completeness/cursor metadata is the legacy, non-paginated
            # response shape; it represents one complete enumeration.
            state["proposal_cursor"] = None
        state["proposal_last_error"] = ""
        return {"event": "proposal_poll", "ok": True,
                "observed": observed, "recorded": recorded,
                "changed": changed,
                "complete": complete,
                "has_cursor": state.get("proposal_cursor") is not None,
                "trust": "external-untrusted", "time": now_iso()}
    except (Denied, OSError, ValueError, TypeError) as exc:
        message = str(exc) or type(exc).__name__
        message = message[:512]
        state["proposal_last_error"] = message
        return {"event": "proposal_poll", "ok": False,
                "observed": observed, "recorded": recorded,
                "error": message, "trust": "external-untrusted",
                "time": now_iso()}


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


def _reject_constant(value: str):
    """Reject non-finite JSON constants at parse time (matches ``fs``)."""
    raise ValueError(f"Non-finite JSON value: {value}")


def _prompt_research_state(context: Context):
    """Own role's current research record for prompt injection.

    Returns None without the read capability; ``absent``/``unavailable``
    statuses inject bare so absence never reads as "nothing
    researched"; current records inject whole while within
    ``role_state_prompt_bytes``, else ``truncated`` with generation
    and sizes and no partial content. Constant-size by construction,
    so resumed-session deltas may carry it without unbounded growth.
    """
    caps = set(context.role.capabilities)
    if "research_read" not in caps and "research" not in caps:
        return None
    seen = context.project.role_state.read(context.role.name)
    if seen["status"] != "current":
        return {"status": seen["status"]}
    record = seen["record"]
    body = canonical(record["state"])
    cap = context.config.limits.role_state_prompt_bytes
    if len(body) > cap:
        return {"status": "truncated", "generation": seen["generation"],
                "state_bytes": len(body), "prompt_bytes": cap,
                "updated_at": record.get("updated_at")}
    return {"status": "current", "generation": seen["generation"],
            "updated_at": record.get("updated_at"), "state": record["state"]}


def _decision_task(context: Context) -> str | None:
    """Delivery description for a run carrying delivered decision events.

    Schema: None when no decision events were delivered, else a short
    factual description naming which kinds were delivered and what counts
    as handled. Bounds: one small string. Trust: local dispatch state
    only. Failure: never raises.
    """
    events = list(getattr(context, "decision_events", []) or [])
    if not events:
        return None
    try:
        any_source = getattr(getattr(context, "role", None), "decision_sources", "own") == "any"
    except Exception:
        any_source = False
    substantive = [e for e in events if isinstance(e, dict) and e.get("action") != "defer"]
    handled = ("A deferral counts as handled only when its topic moves "
               "(meaningful revision, withdrawal, or a superseding decision); "
               "an unchanged deferral returns in a later window.")
    if any_source:
        if substantive:
            return (f"Delivered decision events from any source: {len(substantive)} substantive, "
                    f"{len(events) - len(substantive)} deferral(s). "
                    "All deliver once and are acknowledged after successful processing. " + handled +
                    " Handling is defined by the role policy.")
        return (f"Delivered deferrals from any source: {len(events)} one-shot. "
                "Each new deferral admits one focused run and is acknowledged after successful "
                "processing. " + handled +
                " Handling is defined by the role policy.")
    if substantive:
        return (f"Delivered decision events: {len(substantive)} substantive, "
                f"{len(events) - len(substantive)} deferral(s). "
                "Substantive events deliver once and are acknowledged after successful "
                "processing; deferrals ride along when this role runs for other reasons "
                "and rotate across dispatches. " + handled +
                " Handling is defined by the role policy.")
    return (f"Delivered deferrals: {len(events)} ride-along. "
            "Deferrals ride along when this role runs for other reasons "
            "and rotate across dispatches. " + handled +
            " Handling is defined by the role policy.")


def _open_backlog(context: Context, caps: set) -> dict:
    """Still-undecided backlog statement for prompts (never raises).

    Schema: ``{"ids", "total", "truncated"}`` over the same bounded pending
    set the full prompt carries as entries, so a delta that omits
    already-offered entries still states the open backlog instead of reading
    as empty. Bounds: ids follow ``[limits] pending_insights`` (newest kept)
    with explicit ``truncated``. Trust: local recorded state only.
    """
    stated: dict = {"ids": [], "total": 0, "truncated": False}
    if "insights" not in caps and "decide" not in caps:
        return stated
    try:
        backlogged = context.project.insights.projection(
            limit=context.config.limits.pending_insights)
        return {"ids": [item["id"] for item in backlogged["items"]],
                "total": backlogged["total"],
                "truncated": backlogged["truncated"]}
    except Exception:
        return stated


def prompt_for(context: Context) -> str:
    caps = set(context.role.capabilities)
    # Mechanism enforces grants, not policy choice: only capability-gated,
    # already-recorded context is offered. `pending_insights` count follows the
    # operator-selected `[limits] pending_insights` bound (newest kept), and
    # `open_insights` states the same bounded backlog with explicit total;
    # `recent_snapshots` follows `[limits] prompt_snapshots`. Exact snapshot
    # references are always pinned. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    pending = context.project.insights.list(limit=context.config.limits.pending_insights) \
        if ("insights" in caps or "decide" in caps) else []
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in caps:
        ci_branch = None
    try:
        ci_failures = current_ci_failures(context.project) if "vcs_read" in caps else None
    except Exception:
        ci_failures = None
    return json.dumps({"goal": context.goal,
                       "published_snapshot": {k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "pending_insights": pending,
                       "open_insights": _open_backlog(context, caps),
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       "acceptance_commands": acceptance,
                       "research_state": _prompt_research_state(context),
                       "ci_branch": ci_branch,
                       "ci_failures": ci_failures,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       **_prompt_clock(context)},
                      ensure_ascii=False)


def prompt_delta_for(context: Context, pending: list) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: ``goal_digest`` pin plus the pinned ``published_snapshot`` and a
    small ``snapshot_delta`` (previous/current ids), then only the pending
    proposals not yet offered, an ``open_insights`` statement of the still
    undecided backlog (ids plus total, so an already-offered insight never
    reads as done), workspace markers, capability-gated acceptance
    commands, and a capability-gated ``previous_report`` preview for roles
    holding ``report``. The full goal text and ``recent_snapshots`` history
    are never repeated: the provider session already holds them. Key order is
    fixed (pins first) so the stable prefix keeps working caches warm.
    Bounds: pending and the open backlog both follow
    ``[limits] pending_insights`` (newest kept; ``truncated`` says so
    explicitly); the delta carries no workspace file contents and no history
    array, so resumed-unit bytes stay flat as history grows apart from the
    fixed previous-report preview (at most ``report.PREVIOUS_REPORT_BYTES``).
    Trust: local recorded state only, never model input. Retry/cancellation:
    bounded local reads only (artifact pointer plus one file prefix);
    ``previous()`` never raises, so an unreadable artifact reads as no
    previous edition. Evidence: the dispatching run records
    ``prompt_mode=delta`` and ``prompt_bytes`` in ``prompt_projection.json``.
    Failure: never raises for missing state (callers fall back to the full
    prompt).
    """
    caps = set(context.role.capabilities)
    acceptance = list(context.project.verify) if "verify" in caps else []
    try:
        ci_failures = current_ci_failures(context.project) if "vcs_read" in caps else None
    except Exception:
        ci_failures = None
    open_backlog = _open_backlog(context, caps)
    return json.dumps({"goal_digest": context.goal_digest,
                       "published_snapshot": {k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "pending_insights": pending,
                       "open_insights": open_backlog,
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       "research_state": _prompt_research_state(context),
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "acceptance_commands": acceptance,
                       "ci_branch": ((context.config.vcs.get("ci_branch") or None)
                                     if "vcs_read" in caps else None),
                       "ci_failures": ci_failures,
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
    session state only. Retry/cancellation: peeking never raises (any failure
    falls back to the full prompt and empty identity), except an enforcing
    due rotation, which propagates so the unit fails before model dispatch
    instead of admitting the old conversation. Evidence: rotations write
    ``rotation.json``; dispatches record the mode in
    ``prompt_projection.json``; a failed due rotation is recorded in the run
    ``error.json`` by the Engine caller. Failure: a due rotation removes
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
        # A due rotation enforces a configured bound: its failure must fail
        # the work unit before model dispatch (Engine records error.json),
        # never substitute a full prompt and admit the old conversation.
        record = rotate_session(path, run_dir, reason)
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

    def _execute(self, context: Context, driver, prompt: str, **kwargs) -> dict:
        """Preserve a durable host seal and recover only normal unsealed returns.

        A normally settled turn without a seal gets the configured, finish-only
        follow-up allowance. Provider/protocol faults and explicit cancellation
        keep their existing paths. Deadline wrap-up remains separate and bounded.
        Final record, integrity, and publication failures still fail closed.
        """
        if context.finished is not None:
            raise Denied("No inference after the work unit is sealed")
        context.clear_request_deadlines()
        context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
        try:
            try:
                model_result = driver.execute(context, prompt, **kwargs)
                if context.finished is None:
                    context.enter_drain_wrap_up()
                    if context.bound_hit == "drain" and not context.wrap_up_attempted:
                        raise DeadlineWrapUp(context._bound_message())
            except DeadlineWrapUp:
                if context.finished is not None:
                    raise
                if context.host_cancelled():
                    raise Cancelled("Run stopped before wrap-up")
                if context.wall_expired():
                    raise RunDeadlineExceeded("Whole-unit wall deadline exhausted")
                context.wrap_up_attempted = True
                previous_authority = context.turn_authority_role
                previous_continuation = context.turn_continuation
                authority = previous_authority or context.role
                context.turn_authority_role = authority
                context.turn_continuation = True
                previous_session_capabilities = context.session_identity_capabilities
                if context.bound_hit == "drain":
                    context.session_identity_capabilities = authority.capabilities
                try:
                    context.role = dataclasses.replace(authority,
                        capabilities=tuple(c for c in authority.capabilities if c in WRAP_UP_OPS),
                        engine_tools=(), selector="")
                    context.operations_stopped.clear()
                    context.clear_request_deadlines()  # The failed transport was reaped, not renewed.
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    context.execution_attempt += 1
                    model_result = driver.execute(context, context._bound_message() + "\n\n" + prompt, **kwargs)
                finally:
                    context.turn_authority_role = previous_authority
                    context.turn_continuation = previous_continuation
                    context.session_identity_capabilities = previous_session_capabilities

            if context.finished is None:
                if context.host_cancelled():
                    raise Cancelled("Run stopped before finish")
                if context.bound_hit == "drain" and context.request_deadlines:
                    raise ProtocolError("Terminal drain wrap-up left an active model-request deadline")
                # The reserved deadline transition (or an already-used wrap-up)
                # is authoritative; a missing finish there follows its existing
                # bounded/failure behavior, not this separate policy.
                if context.wrap_up_attempted or context.bound_hit == "deadline" or context.wall_expired():
                    if context.wall_expired() or context.bound_hit == "deadline":
                        context.missing_finish_evidence = {
                            "configured_followups": context.config.limits.missing_finish_followups,
                            "used_followups": 0, "exhausted": False,
                            "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    return model_result
                if context.request_deadlines:
                    raise ProtocolError("Terminal turn left an active model-request deadline")

                configured = context.config.limits.missing_finish_followups
                used = 0
                base_role = context.role
                authority = context.turn_authority_role or base_role
                followup_prompt = (
                    "The prior turn returned normally without calling mizu_finish. "
                    "This work unit remains open. Do not repeat the work or call any other tool; "
                    "call mizu_finish now with an accurate outcome and summary. No action may follow the seal.")
                while context.finished is None and used < configured:
                    if context.host_cancelled():
                        raise Cancelled("Run stopped before missing-finish follow-up")
                    if context.wall_expired():
                        context.missing_finish_evidence = {
                            "configured_followups": configured, "used_followups": used,
                            "exhausted": False, "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                        return model_result
                    if context.request_deadlines:
                        raise ProtocolError("Terminal turn left an active model-request deadline")
                    context.check_deadlines()
                    used += 1
                    context.missing_finish_evidence = {
                        "configured_followups": configured, "used_followups": used,
                        "exhausted": False, "sealed": False,
                        "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    context.execution_attempt += 1
                    previous_authority = context.turn_authority_role
                    previous_continuation = context.turn_continuation
                    context.turn_authority_role = authority
                    context.turn_continuation = True
                    context.role = dataclasses.replace(base_role, capabilities=("finish",),
                                                       engine_tools=(), selector="")
                    previous_session_capabilities = context.session_identity_capabilities
                    context.session_identity_capabilities = base_role.capabilities
                    context.operations_stopped.clear()
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    try:
                        model_result = driver.execute(context, followup_prompt, **kwargs)
                    finally:
                        context.role = base_role
                        context.turn_authority_role = previous_authority
                        context.turn_continuation = previous_continuation
                        context.session_identity_capabilities = previous_session_capabilities
                    if context.finished is None and context.request_deadlines:
                        raise ProtocolError("Finish follow-up left an active model-request deadline")
                    if context.finished is None and (context.bound_hit == "deadline" or context.wall_expired()):
                        context.missing_finish_evidence = {
                            "configured_followups": configured, "used_followups": used,
                            "exhausted": False, "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                        return model_result

                if context.finished is None and context.host_cancelled():
                    raise Cancelled("Run stopped before missing-finish exhaustion")
                if context.finished is None:
                    context.missing_finish_evidence = {
                        "configured_followups": configured, "used_followups": used,
                        "exhausted": True,
                        "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    raise MissingFinish(configured=configured, used=used,
                                        counts_as_failure=context.config.limits.missing_finish_counts_as_failure)

                context.missing_finish_evidence = {
                    "configured_followups": configured, "used_followups": used,
                    "exhausted": False, "sealed": True,
                    "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                context.model_evidence = {**getattr(context, "model_evidence", {}),
                                          "missing_finish": context.missing_finish_evidence}
                if isinstance(model_result, dict):
                    model_result = {**model_result, "missing_finish": context.missing_finish_evidence}

            return model_result
        except Exception as exc:
            context.wait_for_joins()
            if context.finished is None:
                raise
            if context.host_cancelled():
                raise Cancelled("Run stopped after finish") from exc
            settlement = {"status": "failed", "execution_attempt": context.execution_attempt,
                          "error_type": type(exc).__name__[:128], "error": str(exc)[:4000],
                          "recorded_at": now()}
        # No synthetic session or durable-Pi result: only evidence already
        # observed by the driver, explicitly marked as not settled.
        context.model_evidence = {**getattr(context, "model_evidence", {}),
                                  "settled": False, "settlement": settlement}
        write_json(context.run_dir / "adapter-settlement.json", settlement)
        return context.model_evidence

    def preview(self, project: Project, role_name: str, attributes=None) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        if not role.selector:
            return {"profile": role.profile, "selector": None}
        from .classification import prepare
        return prepare(self, project, role, project.snapshots.get(), attributes, preview=True)

    def _check_admission(self, project: Project) -> None:
        """Fresh authority and spend guards; neither selects nor consumes a request."""
        if self.stop.is_set():
            raise Busy("Run stopped before execution admission")
        control = project.control()
        if not control.get("armed") or control.get("paused"):
            raise Denied("Project is unarmed or paused")
        if control.get("draining"):
            raise Busy("Project is draining; no new runs admitted")
        usage = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                       self.config.limits.retention_days,
                       self.config.limits.shared_daily_requests, self.config.timezone).usage(project.name)
        if usage["limit"] <= usage["used"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request budget unavailable for project '{project.name}'")
        if usage["shared_limit"] > 0 and usage["shared_used"] >= usage["shared_limit"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request shared budget unavailable")
        if shutil.disk_usage(project.root).free < self.config.limits.free_disk_mb * 1048576:
            raise InfraExceeded("Free disk space is below the configured reserve")

    def _admit(self, project: Project, role_name: str, role: Role, stack: contextlib.ExitStack,
               *, wait: bool) -> float:
        """Hold dispatch locks after FIFO admission and return its elapsed time."""
        resources = [project.root / "locks" / f"run-{role_name}.lock"]
        if role.workspace == "write":
            resources.append(project.root / "locks" / "workspace.lock")
        return stack.enter_context(admission(self.config, resources, wait=wait, stop=self.stop,
                                             check=lambda: self._check_admission(project)))

    def run(self, project: Project, role_name: str, *, attributes=None, retry_busy: bool = False,
                event_trigger: bool = False) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        with contextlib.ExitStack() as stack:
            admission_elapsed = self._admit(project, role_name, role, stack, wait=retry_busy)
            project.insights.ingest_editor(keep_days=project.config.limits.retention_days)
            snapshot = project.snapshots.get()
            cursor = read_json(project.root / "observed" / f"{role_name}.json", {})
            code_unchanged = role.on_change and _on_change_observed(cursor, snapshot)
            triggers = tuple(getattr(role, "decision_events", ()) or ())
            sources = getattr(role, "decision_sources", "own") or "own"
            decision_events = project.insights.decision_events(
                role_name, triggers, limit=project.config.limits.decision_events_limit,
                sources=sources) if triggers else []
            wait_events = project.insights.due_waits(role_name, snapshot)
            # Dependency obligations addressed to this role ride along when
            # it runs for other reasons; they never admit a run on their
            # own, so no new wake source is implied.
            obligations = project.insights.pending_obligations(role_name)
            # Event-triggered dispatch (platform file triggers) admits only on a
            # subscribed substantive decision event or a due structured wait
            # for this role; in own-source mode deferrals ride along when the role
            # runs for other reasons and never admit a run on their own. A role
            # explicitly opted into decision_sources="any" admits on any matching
            # action including deferrals (one-shot per new deferral). Otherwise
            # it exits before allocating a run directory or model use,
            # like unchanged-code admission. Periodic timers invoke without the
            # flag, so scheduled discovery still runs at its interval.
            if sources == "any":
                substantive_events = [e for e in decision_events if isinstance(e, dict)]
            else:
                substantive_events = [e for e in decision_events
                                      if isinstance(e, dict) and e.get("action") != "defer"]
            try:
                ci_present = _ci_due_for(project, self.config, role)
            except Exception:
                ci_present = []
            try:
                _ci_snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
                ci_failures = _ci_due_for(project, self.config, role, _ci_snapshot_id, time.time())
            except Exception:
                ci_failures = []
            if event_trigger and not substantive_events and not wait_events and not ci_failures:
                return {"skipped": "no-event", "snapshot": snapshot["id"],
                        "code_digest": snapshot["code_digest"]}
            # Writable roles consume these generations in their next snapshot.
            # Read-only on-change roles keep their code/event cursor semantics;
            # they cannot acknowledge snapshot-wide input by publishing.
            goal_changed = digest(project.goal.encode()) != snapshot["goal_digest"]
            inbox_changed = project.insights.generation() != snapshot.get("inbox_seen", "")
            operator_woke = project.control().get("wake_generation", "") != snapshot.get("wake_generation", "")
            prior_wait_expired = (snapshot.get("outcome") == "wait"
                                  and snapshot.get("wake_at") is not None
                                  and time.time() >= snapshot["wake_at"])
            explicit_wake = role.workspace == "write" and (
                goal_changed or operator_woke or inbox_changed or prior_wait_expired)
            progress_self_admits = False
            if (role.workspace == "write" and snapshot.get("outcome") in ("continue", "bounded")
                    and snapshot.get("progress") is not None):
                disposition = progress_disposition(snapshot["progress"], snapshot["outcome"],
                                                   self.config, role_name)
                progress_self_admits = disposition != "parked_no_progress"
            if (code_unchanged and not substantive_events and not wait_events and not ci_failures
                    and not explicit_wake and not progress_self_admits):
                return {"skipped": "unchanged", "snapshot": snapshot["id"],
                        "code_digest": snapshot["code_digest"]}
            # Pre-execution selection probe: evaluate before allocating a run
            # directory so unchanged waits coalesce onto one audit run
            # instead of one directory per idle poll. A changed wait reason
            # still audits as a new run; a dispatch clears the record.
            probed = None
            probe_error = None
            if role.selector:
                try:
                    from .classification import prepare as _prepare_selection
                    from .selection import read_wait as _read_wait
                    from .selection import wait_fingerprint as _wait_fingerprint
                    probed = _prepare_selection(self, project, role, snapshot, attributes)
                    if not probed["profile"]:
                        _fingerprint = _wait_fingerprint(probed)
                        _last = _read_wait(project, role_name)
                        if (isinstance(_last, dict) and _last.get("fingerprint") == _fingerprint
                                and isinstance(_last.get("run"), str)):
                            return {"run": _last["run"], "role": role_name, "status": "waiting",
                                    "finished_at": now(), "selection": probed,
                                    "next_evaluation_at": probed["next_evaluation_at"],
                                    "coalesced": True, "snapshot": snapshot["id"],
                                    "code_digest": snapshot["code_digest"]}
                except Exception as exc:
                    if probed is not None:
                        raise
                    probe_error = exc
            active = project.root / "active" / f"{role_name}.json"
            run_id = uuid.uuid4().hex
            run_dir = project.root / "runs" / run_id
            mkdir(run_dir)
            context = None
            decision = None
            selection_role = None
            selection_retries: list = []
            max_selection_attempts = 1
            selection_attempt = 0
            recorded_selection_failure = None
            model_result = None
            result = None
            workspace = project.workspace if role.workspace == "write" else run_dir / "input"
            try:
                write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                if code_unchanged and (substantive_events or wait_events or ci_failures):
                    if substantive_events:
                        admission = "decision"
                    elif wait_events:
                        admission = "wait"
                    else:
                        admission = "ci"
                elif event_trigger and (substantive_events or wait_events or ci_failures):
                    if substantive_events:
                        admission = "decision"
                    elif wait_events:
                        admission = "wait"
                    else:
                        admission = "ci"
                elif role.on_change:
                    admission = "change"
                else:
                    admission = "schedule"
                try:
                    _ci_branch = self.config.vcs.get("ci_branch") if isinstance(getattr(self.config, "vcs", None), dict) else None
                except Exception:
                    _ci_branch = None
                if ci_present:
                    try:
                        _ci_head = snapshot["id"] if isinstance(snapshot, dict) else None
                        write_ci_wake(project, role_name, _ci_branch, ci_present, _ci_head, time.time())
                    except Exception:
                        pass
                write_json(run_dir / "started.json", {"run": run_id, "role": role_name,
                            "snapshot": snapshot["id"], "started_at": now(),
                            "admission_elapsed_seconds": round(admission_elapsed, 6),
                            "admission": admission, "decision_events": len(decision_events),
                            "wait_events": len(wait_events),
                            "obligation_events": len(obligations),
                            "ci_failures": len(ci_present),
                            "config_sha256": digest(self.config.file.read_bytes()),
                            "policy_sha256": digest(role_policy_bytes(role))})
                if decision_events:
                    write_json(run_dir / "decision-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "events": decision_events})
                if wait_events:
                    write_json(run_dir / "wait-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "events": wait_events})
                if obligations:
                    write_json(run_dir / "obligation-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "events": obligations})
                if role.selector:
                    from .classification import prepare
                    from .selection import clear_wait as _clear_wait
                    from .selection import invalidate_wait as _invalidate_wait
                    from .selection import wait_fingerprint as _wait_fingerprint2
                    from .selection import write_wait as _write_wait
                    if probe_error is not None:
                        raise probe_error
                    decision = probed if probed is not None else prepare(self, project, role, snapshot, attributes)
                    write_json(run_dir / "selection.json", decision)
                    if not decision["profile"]:
                        result = {"run": run_id, "role": role_name, "status": "waiting",
                                  "finished_at": now(), "selection": decision,
                                  "next_evaluation_at": decision["next_evaluation_at"]}
                        write_json(run_dir / "result.json", result)
                        with contextlib.suppress(OSError, ValueError, TypeError):
                            _write_wait(project, role_name, _wait_fingerprint2(decision),
                                        run_id, decision["next_evaluation_at"])
                        return result
                    try:
                        _clear_wait(project, role_name)
                    except (OSError, ValueError, TypeError):
                        # A failed clear must not leave a stale pointer that
                        # falsely coalesces the next wait onto a pre-dispatch
                        # audit run; poison the record so it re-audits.
                        _invalidate_wait(project, role_name)
                    # Single-use routing: a dispatched unit consumes the fresh
                    # recommendation so the next unit falls back unless a newer
                    # one is recorded. Waiting selections above return early and
                    # never consume; preview never reaches this path. Sticky
                    # selectors keep the file. Best-effort, never fatal.
                    try:
                        validity = self.config.selectors.get(role.selector, {}).get(
                            'recommendation_validity', 'once')
                        if validity != 'sticky' and decision.get('recommendation', {}).get('status') == 'fresh':
                            from .routing import consume as _consume_routing
                            _consume_routing(project, role_name)
                    except Exception:
                        pass
                    selection_role = role
                    try:
                        _spec = self.config.selectors.get(role.selector, {})
                        _profiles = {c.get("profile") for _rule in _spec.get("rules", [])
                                     for c in _rule.get("candidates", [])}
                        max_selection_attempts = len(_profiles) + 1
                    except Exception:
                        max_selection_attempts = 1
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
                                  stop=self.stop, consult=self.consult, delegate=self.delegate)
                context._slot_lease = True
                context.ephemeral = self.ephemeral
                context.decision_events = decision_events
                context.wait_events = wait_events
                context.obligations = obligations
                context.progress_obligations_digest = _progress_obligations_digest(obligations)
                previous_progress = snapshot.get("progress")
                previous_obligations_digest = None
                if (isinstance(previous_progress, dict) and previous_progress.get("role") == role_name):
                    previous_obligations_digest = previous_progress.get("obligations_digest")
                new_obligation = bool(obligations) and previous_obligations_digest != context.progress_obligations_digest
                context.progress_input = (
                    context.inbox_seen != snapshot.get("inbox_seen", "")
                    or context.wake_generation != snapshot.get("wake_generation", "")
                    or bool(decision_events or wait_events or ci_failures)
                    or new_obligation or prior_wait_expired)
                prompt, prompt_mode, session_key, rotation, session_dir = session_prompt(context, role, run_dir)
                decoded = json.loads(prompt)
                write_json(run_dir / "prompt_projection.json",
                           {"run": run_id, "role": role_name, "snapshot": snapshot["id"],
                            "recent_snapshots": len(decoded.get("recent_snapshots", [])),
                            "pending_insights": len(decoded.get("pending_insights", [])),
                            "open_insights": (decoded.get("open_insights") or {}).get("total", 0),
                            "decision_events": len(decoded.get("decision_events", [])),
                            "wait_events": len(decoded.get("wait_events", [])),
                            "acceptance_commands": len(decoded.get("acceptance_commands", [])),
                            "prompt_bytes": len(prompt.encode("utf-8")), "created_at": now(),
                            "prompt_mode": prompt_mode, "session_key": session_key,
                            "rotation": (rotation or {}).get("reason", "")})
                fallback_attempt = 0
                while True:
                    context.execution_attempt += 1
                    try:
                        model_result = self._execute(context, driver, prompt)
                    except ModelFailure as exc:
                        # A matched on_error rule falls back within this
                        # dispatch: the block just recorded applies to the
                        # re-selection, so a transient provider error costs a
                        # retry, not the whole interval to the next run.
                        # Anything else (no rule match, local admission
                        # refusal, stop) fails exactly as before.
                        if (context.cancelled() or self.stop.is_set()
                                or getattr(context, "admission_error", None)
                                or getattr(context, "bound_hit", None) is not None
                                or getattr(context, "wrap_up_attempted", False)):
                            raise
                        matched = None
                        if decision and decision.get("profile") and selection_role is not None:
                            from .selection import State as _FallbackState
                            matched = _FallbackState(self.config).result(
                                decision, run=run_id, attempt=selection_attempt, error=exc)
                            recorded_selection_failure = {
                                "attempt": selection_attempt,
                                "decision": digest(canonical(decision)),
                                "error": exc,
                                "action": matched,
                            }
                        if not matched:
                            raise
                        fallback_attempt += 1
                        if fallback_attempt >= max_selection_attempts:
                            raise
                        if (self.stop.is_set() or not project.control().get("armed")
                                or project.control().get("paused")):
                            raise Cancelled("Run stopped before fallback dispatch")
                        selection_retries.append({"profile": decision["profile"],
                                                  "error": str(exc)})
                        # A reselection fault (corrupt state, invalid selector)
                        # is a local invariant failure: it propagates as itself,
                        # chained to the model failure, never relabelled as a
                        # transient provider error.
                        reselected = prepare(self, project, selection_role, snapshot, attributes)
                        if not reselected.get("profile"):
                            # Nothing eligible under the recorded blocks: fail
                            # on the last attempt as before (deferred below),
                            # keeping its decision for the failure record.
                            raise exc
                        decision = reselected
                        selection_attempt += 1
                        write_json(run_dir / "selection.json", decision)
                        role = dataclasses.replace(selection_role, profile=decision["profile"],
                                                   selector="")
                        context.role = role
                        # The failed attempt's bridge close cancelled this
                        # context's operations; the retry is a new engine
                        # connection, so reopen them. Deadline, budget, and
                        # tool counters stay accumulated on the reused context.
                        # Only unsealed failures reach fallback; the retry must
                        # finish and verify its own work.
                        # A terminal unanswered durable settlement is known
                        # not to be an in-flight recovery. Give the selected
                        # fallback its own idempotency key; every other error
                        # keeps the identity and therefore still fails closed
                        # if the candidate's grant changed.
                        if (exc.evidence.get("source") == "pi-durable"
                                and exc.evidence.get("kind") == "unanswered"):
                            context.attempt_ordinal += 1
                        context.operations_stopped.clear()
                        context.verification = None
                        context.commentary = None
                        driver = self.resolve(role.profile)
                        if getattr(driver, "requires_sandbox", True) and any(
                                c in role.capabilities for c in ("exec", "experiment", "verify")):
                            container_runtime(self.config)
                            cleanup(self.config, project.root, role_name)
                        continue
                    except LimitExceeded as exc:
                        # A per-run spend bound with no finish sealed gets one
                        # wrap-up attempt under reduced grants instead of an
                        # error: the daily budget, an unmarked bound, an
                        # already-spent allowance still fail exactly as before.
                        # Sealed driver errors were retained by _execute.
                        if (isinstance(exc, (InfraExceeded, RunDeadlineExceeded, DrainYieldExceeded))
                                or getattr(context, "bound_hit", None) is None
                                or getattr(context, "wrap_up_attempted", False)
                                or context.finished is not None):
                            raise
                        grants = [cap for cap in WRAP_UP_OPS if cap in role.capabilities]
                        if not grants:
                            raise
                        context.wrap_up_attempted = True
                        previous_authority = context.turn_authority_role
                        previous_continuation = context.turn_continuation
                        authority = previous_authority or role
                        context.turn_authority_role = authority
                        context.turn_continuation = True
                        previous_session_capabilities = context.session_identity_capabilities
                        if context.bound_hit == "drain":
                            context.session_identity_capabilities = authority.capabilities
                        try:
                            context.role = dataclasses.replace(
                                authority, capabilities=tuple(c for c in WRAP_UP_OPS if c in authority.capabilities),
                                engine_tools=(), selector="")
                            context.operations_stopped.clear()
                            driver = self.resolve(role.profile)
                            if getattr(driver, "requires_sandbox", True) and any(
                                    c in context.role.capabilities for c in ("exec", "experiment", "verify")):
                                container_runtime(self.config)
                                cleanup(self.config, project.root, role_name)
                            requests_left, tools_left = context._wrap_up_remaining()
                            notice = (f"Per-run {context.bound_hit} budget exhausted with no finish "
                                      f"sealed. Wrap-up only: seal with finish (recorded as a bounded "
                                      f"end) or use research, submit_insight, attach_evidence, decide. "
                                      f"Keep the published state unless this run changed it. "
                                      f"{max(0, requests_left)} requests and "
                                      f"{max(0, tools_left)} wrap-up tools remain.")
                            if context.bound_hit == "drain":
                                notice = context._bound_message()
                            context.execution_attempt += 1
                            model_result = self._execute(context, driver, notice + "\n\n" + prompt)
                        finally:
                            context.turn_authority_role = previous_authority
                            context.turn_continuation = previous_continuation
                            context.session_identity_capabilities = previous_session_capabilities
                    break
                if context.host_cancelled():
                    raise Cancelled("Run stopped before publication")
                if context.finished is None:
                    if context.bound_hit == "deadline" or context.wall_expired():
                        raise RunDeadlineExceeded("Deadline-bounded run ended without finish")
                    if context.bound_hit == "drain" and context.wrap_up_attempted:
                        raise DrainYieldExceeded("Drain-bounded run ended without finish")
                    raise Denied("Missing finish intent")
                finished = context.finished
                if role.workspace == "write":
                    captured = project.snapshots.capture_files(workspace)
                    verification = context.verification
                    if verification and (verification["code_digest"] != captured["code_digest"] or captured["skipped"]):
                        verification = {**verification, "passed": False, "invalidated": "code changed or final snapshot is unrepresentable"}
                    if finished["outcome"] == "done" and not (verification and verification["passed"]):
                        raise Denied("Code changed after verification; completion refused")
                    selected_progress = tuple(self.config.limits.progress_signals)
                    observed_progress = {
                        "tool": context.progress_tool,
                        "code": captured["code_digest"] != context.snapshot["code_digest"],
                        "state": finished["state"] != context.snapshot["state"],
                        "decision": context.progress_decision,
                        "insight": context.progress_insight,
                        "input": context.progress_input,
                    }
                    progress = _progress_record(context.snapshot, role_name, selected_progress,
                                               observed_progress, finished["outcome"],
                                               obligations_digest=context.progress_obligations_digest)
                    snapshot = project.snapshots.create(
                        captured, goal=context.goal, state=finished["state"], run=run_id,
                        outcome=finished["outcome"], summary=finished["summary"], verification=verification,
                        wake_at=time.time() + finished["wait_seconds"] if finished["outcome"] == "wait" else None,
                        inbox_seen=context.inbox_seen,
                        wake_generation=context.wake_generation, progress=progress,
                        parent=context.snapshot["id"])
                if decision:
                    from .selection import State
                    State(self.config).result(decision, run=run_id, attempt=selection_attempt)
                result = {"run": run_id, "role": role_name, "status": "prepared", "finished_at": now(),
                          "finish": finished, "model": model_result, "snapshot": snapshot["id"],
                          "activity": context.activity_summary(),
                          **({"selection": decision} if decision else {})}
                write_json(run_dir / "result.json", result)
                if role.workspace == "write":
                    project.snapshots.publish(snapshot)
                    # Own publication moves the head without new CI facts: refresh
                    # the wake record to the published head so this run does not
                    # re-wake itself; a new sha/check or a later head still wakes.
                    try:
                        _refresh_present = ci_present
                        _refresh_branch = _ci_branch
                    except Exception:
                        _refresh_present = []
                        _refresh_branch = None
                    if _refresh_present:
                        try:
                            write_ci_wake(project, role_name, _refresh_branch, _refresh_present,
                                          snapshot["id"] if isinstance(snapshot, dict) else None,
                                          time.time())
                        except Exception:
                            pass
                # Advisory hint: a failed routing write must not flip an
                # already-published unit to interrupted, like the other
                # best-effort post-publication records below.
                with contextlib.suppress(OSError):
                    from .routing import record as record_routing
                    record_routing(project, role_name, finished, snapshot, context.goal_digest, run_id)
                if context.commentary is not None:
                    result["artifact"] = publish(project, snapshot, context.commentary, run_id=run_id)
                # A retained host seal does not confirm session settlement.
                if session_dir is not None and model_result.get("settled") is not False:
                    with contextlib.suppress(OSError, ValueError, TypeError):
                        from .engine_config import write_prompt_state
                        write_prompt_state(session_dir, snapshot=snapshot["id"],
                                           inbox_generation=context.inbox_seen)
                result["status"] = "completed"
                if getattr(context, "wrap_up_attempted", False):
                    result["wrap_up"] = {"bound": context.bound_hit}
                write_json(run_dir / "result.json", result)
                write_json(project.root / "observed" / f"{role_name}.json",
                           {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"]})
                if context.decision_events:
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        project.insights.acknowledge_decisions(
                            role_name, context.decision_events,
                            sources=getattr(role, "decision_sources", "own") or "own")
                if context.wait_events:
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        project.insights.consume_waits(role_name, context.wait_events)
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": 0, "last_run": run_id, "updated_at": now()})
                return result
            except BaseException as exc:
                if (isinstance(exc, (RunDeadlineExceeded, DrainYieldExceeded)) and context is not None
                        and context.finished is None and not context.host_cancelled()):
                    result = {"run": run_id, "role": role_name, "status": "bounded",
                              "bound": "drain" if isinstance(exc, DrainYieldExceeded) else "deadline",
                              "finished_at": now(), "error": str(exc),
                              "publication": "not_published", "snapshot": snapshot["id"],
                              "code_digest": snapshot["code_digest"],
                              "model": getattr(context, "model_evidence", {}),
                              "activity": context.activity_summary()}
                    try:
                        write_json(run_dir / "result.json", result)
                    except Exception as record_error:
                        # A bound is non-failing only when its bounded result
                        # is durably recorded. Preserve ordinary storage-fault
                        # handling (including health accounting) if that fails.
                        exc = record_error
                    else:
                        return result
                if isinstance(exc, ModelFailure) and context is not None and context.admission_error:
                    if getattr(context, "admission_wait", False):
                        exc = InfraExceeded(f"{self.config.timezone} daily model-request budget exhausted")
                    else:
                        exc = ProtocolError("Local admission failed: " + context.admission_error)
                handled = None
                if decision and decision.get("profile") and isinstance(exc, ModelFailure) and context is not None and not context.cancelled():
                    identity = {
                        "attempt": selection_attempt,
                        "decision": digest(canonical(decision)),
                    }
                    if (isinstance(recorded_selection_failure, dict) and
                            recorded_selection_failure.get("error") is exc and
                            all(recorded_selection_failure.get(key) == value
                                for key, value in identity.items())):
                        # The fallback loop already applied this exact failure
                        # before reselection. Reuse its result rather than
                        # extending a cooldown/block with a second timestamp.
                        handled = recorded_selection_failure["action"]
                    else:
                        from .selection import State
                        handled = State(self.config).result(decision, run=run_id, attempt=selection_attempt, error=exc)
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
                             **({"activity": context.activity_summary()} if context is not None else {}),
                             **({"selection": decision} if decision else {}),
                             **({"model_failure": exc.evidence} if isinstance(exc, ModelFailure) else {}),
                             **({"selection_action": handled} if handled else {}),
                             **({"selection_retries": selection_retries} if selection_retries else {}),
                             **({"missing_finish": getattr(exc, "evidence", None)
                                  or getattr(context, "missing_finish_evidence", None)}
                                if isinstance(exc, MissingFinish)
                                or getattr(context, "missing_finish_evidence", None) else {})}
                    if model_result is not None:
                        error["model"] = model_result
                    elif context is not None and hasattr(context, "model_evidence"):
                        error["model"] = context.model_evidence
                    write_json(run_dir / "error.json", error)
                    if result is not None:
                        write_json(run_dir / "result.json", {**result, "status": "interrupted", **error})
                if handled:
                    counts_as_failure = handled.get("counts_as_failure", False)
                    if type(counts_as_failure) is not bool:
                        raise ConfigError("Invalid selector counts_as_failure policy")
                    if counts_as_failure:
                        _record_health_failure(project, role_name, run_id, exc,
                                               self.config.limits.max_failures)
                    return {"run": run_id, "role": role_name, "status": "deferred",
                            "error": str(exc), "selection": decision, "selection_action": handled,
                            **({"selection_retries": selection_retries} if selection_retries else {}),
                            "next_evaluation_at": time.time() + self.config.limits.cooldown_seconds}
                suppress_missing_finish_brake = (isinstance(exc, MissingFinish)
                                                 and not self.config.limits.missing_finish_counts_as_failure)
                if not is_deferred(exc, context) and not suppress_missing_finish_brake:
                    _record_health_failure(project, role_name, run_id, exc,
                                           self.config.limits.max_failures)
                raise exc
            finally:
                if context is not None:
                    context._slot_lease = False
                    context.closed = True
                with contextlib.suppress(OSError):
                    active.unlink(missing_ok=True)
                # Read-only inputs are reproducible from the content-addressed snapshot.
                if role.workspace != "write":
                    shutil.rmtree(workspace, ignore_errors=True)

    def consult(self, parent: Context, args: dict) -> dict:
        # The child unit takes its model from each answering role's own
        # profile or selector, exactly as a scheduled run of that role would:
        # fixed profiles run directly, selector roles resolve through a
        # bounded preview (no routing consumption, no classifier inference),
        # so retry/on_error/fallback apply with no new mechanism and the model
        # is stated once on the role instead of twice (role plus allowlist).
        if args.get("roles") is not None and args.get("role"):
            raise Denied("Consultation takes roles or role, not both")
        if args.get("roles") is not None:
            role_names = list(args.get("roles") or [])
            if (not role_names or len(set(role_names)) != len(role_names)
                    or any(not isinstance(n, str) for n in role_names)):
                raise Denied("Consultation requires unique answering roles")
        else:
            single = args.get("role") or getattr(self.config, "consult_role", "")
            if not single:
                raise Denied("Consultation role requires explicit roles or a configured consult_role")
            role_names = [single]
        for role_name in role_names:
            if role_name not in self.config.roles:
                raise ConfigError(f"Consultation role is not configured: {role_name}")
            check_consult_role(role_name, self.config.roles[role_name])
        if not parent._slot_lease or parent.closed:
            raise Busy("Consultation requires a live admitted caller execution slot")
        capture = parent.project.snapshots.capture_files(parent.workspace)
        snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                              run=parent.run_dir.name, outcome="wait", summary="Consultation input")

        def ask(role_name: str) -> dict:
            run_dir = parent.project.root / "runs" / uuid.uuid4().hex
            workspace = run_dir / "input"
            ctx = None
            role = self.config.roles[role_name]
            profile = ""
            try:
                mkdir(run_dir)
                if role.selector:
                    # Config-only gate: preview() also requires project
                    # enablement, but a consultation child is ephemeral
                    # (caller slot, no role lock, no dispatch gate), so any
                    # configured read-only role qualifies as the docs say.
                    # Resolve through the same bounded preview without the
                    # project-membership check and without consuming routing.
                    from .classification import prepare as _prepare_selection
                    decision = _prepare_selection(
                        self, parent.project, role,
                        parent.project.snapshots.get(), None, preview=True)
                    profile = decision.get("profile") or ""
                    if not profile:
                        raise Denied("Consultation selector has no selectable profile: "
                                     + str(decision.get("reason") or "waiting"))
                    role = dataclasses.replace(role, profile=profile, selector="")
                else:
                    profile = role.profile
                write_json(run_dir / "started.json", {"run": run_dir.name,
                           "parent_run": parent.run_dir.name, "role": role.name,
                           "profile": profile, "snapshot": snap["id"], "started_at": now()})
                parent.project.snapshots.materialize(snap, workspace)
                ctx = Context(self.config, parent.project, role,
                              run_dir, snap, workspace, stop=parent.stop, goal=parent.goal)
                ctx.ephemeral = True
                model = self._execute(ctx, self.resolve(profile), prompt_for(ctx) + "\n\nQuestion:\n" + args["question"], profile=profile)
                if ctx.host_cancelled() or (ctx.finished is None and ctx.cancelled()):
                    raise Cancelled("Consultation cancelled before completion")
                if ctx.finished is None:
                    raise Denied("Consultation did not finish")
                result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                          "role": role.name, "status": "completed", "finished_at": now(),
                          "profile": profile, "snapshot": snap["id"],
                          "answer": ctx.finished["summary"], "model": model,
                          "activity": ctx.activity_summary()}
                write_json(run_dir / "consultation.json", result)
                return result
            except Exception as exc:
                result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                          "role": role_name, "status": "bounded" if isinstance(exc, RunDeadlineExceeded) else "interrupted",
                          "finished_at": now(), "profile": profile, "snapshot": snap["id"], "error": str(exc)}
                if ctx is not None:
                    result["activity"] = ctx.activity_summary()
                    if hasattr(ctx, "model_evidence"):
                        result["model"] = ctx.model_evidence
                try:
                    mkdir(run_dir)
                    write_json(run_dir / "error.json", result)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                return result
            finally:
                if ctx is not None:
                    ctx.closed = True
                shutil.rmtree(workspace, ignore_errors=True)
        with ThreadPoolExecutor(max_workers=self.config.limits.parallel_consults) as pool:
            results = list(pool.map(ask, role_names))
        record = {"snapshot": snap["id"], "question": args["question"], "roles": list(role_names), "answers": [{"run": r["run"], "role": r["role"], "profile": r["profile"], **({"error": r["error"]} if "error" in r else {})} for r in results]}
        write_json(parent.run_dir / "consultations" / f"{uuid.uuid4().hex}.json", record)
        answers=[]
        for result in results:
            answer={k:result[k] for k in ('run','role','profile','snapshot') if k in result}
            if 'answer' in result:
                answer['answer']=result['answer']
            if 'error' in result:
                answer['error'],answer['error_truncated']=text_preview(result['error'],2048)
            answers.append(answer)
        return {**record,'answers':answers}


    def delegate(self, parent: Context, args: dict) -> dict:
        # One-shot implementation child in the caller's live workspace. The
        # caller waits holding its workspace lock, so exactly one writer
        # touches the tree; verification and publication stay with the
        # caller. The child takes grants, profile or selector, and policy
        # from the delegate role's own configuration, minus the forbidden
        # set, plus a mechanism-provided terminal finish seal that returns
        # to the caller. Spend counts against the caller's unit through the
        # shared counters; nested delegation and consultation stay disabled
        # so no spend escapes the unit.
        role_name = args.get("role") or getattr(self.config, "delegate_role", "")
        if not role_name:
            raise Denied("Delegation needs an explicit role or a configured delegate_role")
        if role_name == parent.role.name:
            raise Denied("A role cannot delegate to itself")
        if role_name not in self.config.roles:
            raise ConfigError(f"Delegation role is not configured: {role_name}")
        check_delegate_role(role_name, self.config.roles[role_name])
        task = args.get("task") or ""
        if not task.strip():
            raise Denied("Delegation needs a self-contained task")
        if not parent._slot_lease or parent.closed:
            raise Busy("Delegation requires a live admitted caller execution slot")
        role = self.config.roles[role_name]
        # The child works the caller's tree, so it inherits the caller's
        # workspace value: live listing and writable execution for a writer
        # caller, a consistent read view otherwise. The child unit needs a
        # terminal seal, ensured here by the mechanism (idempotent when the
        # configured role already carries finish for its own top-level runs).
        role = dataclasses.replace(role, workspace=parent.role.workspace,
                                   capabilities=tuple(dict.fromkeys([*role.capabilities, "finish"])))
        capture = parent.project.snapshots.capture_files(parent.workspace)
        snap = parent.project.snapshots.create(capture, goal=parent.goal, state=parent.snapshot["state"],
                                              run=parent.run_dir.name, outcome="wait", summary="Delegation input")
        run_dir = parent.project.root / "runs" / uuid.uuid4().hex
        ctx = None
        profile = ""
        try:
            mkdir(run_dir)
            if role.selector:
                # Config-only gate like consultation: the child is
                # ephemeral (caller slot), so any configured role
                # qualifies; resolve without consuming routing.
                from .classification import prepare as _prepare_selection
                decision = _prepare_selection(
                    self, parent.project, role,
                    parent.project.snapshots.get(), None, preview=True)
                profile = decision.get("profile") or ""
                if not profile:
                    raise Denied("Delegation selector has no selectable profile: "
                                 + str(decision.get("reason") or "waiting"))
                role = dataclasses.replace(role, profile=profile, selector="")
            else:
                profile = role.profile
            write_json(run_dir / "started.json", {"run": run_dir.name,
                       "parent_run": parent.run_dir.name, "delegation": True,
                       "role": role.name, "profile": profile, "snapshot": snap["id"],
                       "workspace": str(parent.workspace), "started_at": now()})
            driver = self.resolve(profile)
            if getattr(driver, "requires_sandbox", True) and any(
                    c in role.capabilities for c in ("exec", "experiment", "verify")):
                container_runtime(self.config)
                cleanup(self.config, parent.project.root, role_name)
            ctx = Context(self.config, parent.project, role,
                          run_dir, snap, parent.workspace, stop=parent.stop, goal=parent.goal)
            ctx.ephemeral = True
            ctx.consult = None
            ctx.spend_role = parent.role
            ctx.delegated_child = True
            ctx.request_count = parent.request_count
            ctx.tool_count = parent.tool_count
            ctx.delegated_wrap_up_requests = parent.delegated_wrap_up_requests
            ctx.delegated_wrap_up_tools = parent.delegated_wrap_up_tools
            try:
                model = self._execute(ctx, driver, prompt_for(ctx) + "\n\nTask:\n" + task
                                       + "\n\nCall finish once with a result summary (and short state) "
                                       + "when done; that seal returns to the caller, it publishes nothing.")
            finally:
                parent.request_count = ctx.request_count
                parent.tool_count = ctx.tool_count
                parent.delegated_wrap_up_requests = ctx.delegated_wrap_up_requests
                parent.delegated_wrap_up_tools = ctx.delegated_wrap_up_tools
                # Spend state the child tripped belongs to the unit:
                # a bound the delegation exceeded must coerce the
                # parent's seal too, and daily-refusal flags must
                # survive for the parent's own error handling.
                if ctx.bound_hit != "deadline":
                    parent.bound_hit = parent.bound_hit or ctx.bound_hit
                parent.admission_wait = parent.admission_wait or ctx.admission_wait
                if ctx.admission_error:
                    parent.admission_error = ctx.admission_error
            if ctx.host_cancelled() or (ctx.finished is None and ctx.cancelled()):
                raise Cancelled("Delegation cancelled before completion")
            if ctx.finished is None:
                raise Denied("Delegation did not finish")
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role.name, "status": "completed", "finished_at": now(),
                      "profile": profile, "snapshot": snap["id"],
                      "answer": ctx.finished["summary"], "model": model,
                      "activity": ctx.activity_summary()}
            write_json(run_dir / "delegation.json", result)
            record = {"task": task, "role": role.name, "run": run_dir.name,
                      "snapshot": snap["id"], "profile": profile, "answer": ctx.finished["summary"]}
            write_json(parent.run_dir / "delegations" / f"{uuid.uuid4().hex}.json", record)
            return record
        except Exception as exc:
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role_name, "status": "bounded" if isinstance(exc, RunDeadlineExceeded) else "interrupted",
                      "finished_at": now(), "profile": profile, "snapshot": snap["id"], "error": str(exc)}
            if ctx is not None:
                result["activity"] = ctx.activity_summary()
                if hasattr(ctx, "model_evidence"):
                    result["model"] = ctx.model_evidence
            try:
                mkdir(run_dir)
                write_json(run_dir / "error.json", result)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            return result
        finally:
            if ctx is not None:
                ctx.closed = True


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


def _role_events_due(project: Project, snapshot: dict, role_name=None) -> bool:
    """True when a decision event or due structured wait routes to a checked role.

    The checked set is the running role when named, else every write role in
    the project. Decision registration and wait expiry leave no inbox or
    snapshot generation footprint of their own (the operator CLI can defer
    with a wait outside any run), so the writer gate must consult the event
    and wait stores directly before suppressing dispatch. Never raises:
    unreadable stores read as no events; admission itself still fails loudly
    inside Engine.run.
    """
    try:
        names = [role_name] if role_name in project.roles else [
            name for name in project.roles
            if name in project.config.roles and project.config.roles[name].workspace == "write"]
        for name in names:
            try:
                triggers = project.config.roles[name].decision_events
                sources = getattr(project.config.roles[name], "decision_sources", "own") or "own"
                if triggers:
                    pending = project.insights.decision_events(
                        name, triggers, limit=project.config.limits.decision_events_limit,
                        sources=sources)
                    if sources == "any":
                        if any(isinstance(e, dict) for e in pending):
                            return True
                    elif any(isinstance(e, dict) and e.get("action") != "defer" for e in pending):
                        return True
            except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            try:
                if project.insights.due_waits(name, snapshot):
                    return True
            except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
    except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return False


def _progress_obligations_digest(obligations) -> str:
    """Digest only stable identity fields for the bounded pending-obligation view."""
    rows = []
    for item in obligations or ():
        if not isinstance(item, dict):
            continue
        rows.append({key: item.get(key) for key in ("insight", "rev", "registered_at", "requires")})
    rows.sort(key=lambda item: (str(item.get("registered_at") or ""),
                                str(item.get("insight") or ""),
                                int(item.get("rev") or 0)))
    return digest(canonical(rows))


def _progress_record(snapshot: dict, role_name: str, selected: tuple[str, ...],
                    signals: dict[str, bool], outcome: str, *, obligations_digest: str | None = None) -> dict:
    """Build compact unit evidence and its bounded consecutive no-progress count."""
    has_progress = any(signals[name] for name in selected)
    previous = snapshot.get("progress")
    previous_count = 0
    if isinstance(previous, dict):
        try:
            validate_progress(previous)
            if previous["role"] == role_name and set(previous["selected"]) == set(selected):
                previous_count = previous["no_progress_count"]
        except Denied:
            # Snapshot loading validates persisted records; direct callers with
            # malformed records rebase rather than trusting their counter.
            previous_count = 0
    count = previous_count
    if has_progress:
        count = 0
    elif outcome in ("continue", "bounded"):
        # 101 is one above the largest allowed continuation policy and is
        # sufficient to preserve the parked disposition without unbounded growth.
        count = min(101, previous_count + 1)
    obligations_digest = obligations_digest or _progress_obligations_digest(())
    record = {"schema": 1, "signals": {name: bool(signals.get(name, False))
                                        for name in PROGRESS_SIGNALS},
              "selected": list(selected), "role": role_name,
              "has_progress": has_progress, "no_progress_count": count,
              "obligations_digest": obligations_digest,
              "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")}
    return validate_progress(record)


def should_run(project: Project, snapshot: dict, current_time: float | None = None,
               role_name: str | None = None, config=None) -> bool:
    """Apply explicit wakes, then the configured no-progress self-admission guard.

    Legacy snapshots without progress retain their former behavior. A current
    matching record parks continue/bounded only after configured no-progress
    continuation allowance; external wakes remain authoritative.
    """
    current_time = time.time() if current_time is None else current_time
    control = project.control()
    if not control.get("armed") or control.get("paused") or control.get("draining"):
        return False
    if digest(project.goal.encode()) != snapshot["goal_digest"]:
        return True
    if control.get("wake_generation", "") != snapshot.get("wake_generation", ""):
        return True
    outcome = snapshot["outcome"]
    if outcome == "done":
        return False  # A completed objective needs an explicit wake or changed goal.
    if project.insights.generation() != snapshot.get("inbox_seen", ""):
        return True
    if _role_events_due(project, snapshot, role_name):
        return True
    try:
        effective = config if config is not None else getattr(project, "config", None)
        if role_name is not None and not _ci_wake_enabled(effective, role_name):
            pass
        elif role_name is not None:
            _snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
            if _ci_due_for(project, effective, role_name, _snapshot_id, current_time):
                return True
        elif wake_ci_failures(project, effective):
            return True
    except Exception:
        pass
    if outcome == "wait" and snapshot.get("wake_at") is not None and current_time >= snapshot["wake_at"]:
        return True
    if outcome not in ("continue", "bounded"):
        return False
    if "progress" not in snapshot or role_name is None:
        return True
    progress = snapshot["progress"]
    try:
        validate_progress(progress)
    except Denied:
        # Snapshots.get rejects malformed persisted records; fail closed for
        # direct projections rather than treating corrupt evidence as legacy.
        return False
    effective = config if config is not None else getattr(project, "config", None)
    disposition = progress_disposition(progress, outcome, effective, role_name, current_time)
    return disposition != "parked_no_progress"


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

    Schema: ``state`` is daemon in-memory poll state (fetch, CI, proposal
    timestamp/cursor); ``shared`` is the flat ``vcs-poll.json`` mapping.
    Returns the ``ci`` sub-state.
    Bounds: numeric timestamps only; anything else is ignored. Trust: local
    operator state, never model input. Failure: never raises.
    """
    ci_state = state.setdefault("ci", {})
    if not isinstance(shared, dict):
        return ci_state
    for key, target in (("last_fetch", state), ("last_poll", ci_state),
                        ("last_proposal_poll", state)):
        seen = _wall(shared.get(key))
        if seen is None:
            continue
        current = _wall(target.get(key))
        if current is None or seen > current:
            target[key] = seen
    if "proposal_cursor" in shared:
        stored_cursor = shared.get("proposal_cursor")
        if stored_cursor is None:
            state["proposal_cursor"] = None
        else:
            valid_cursor = _valid_proposal_cursor(stored_cursor)
            if valid_cursor is not None:
                state["proposal_cursor"] = valid_cursor
    if isinstance(shared.get("failures"), dict):
        merged = _clean_ci_failures(ci_state.get("failures"))
        for branch, rows in _clean_ci_failures(shared.get("failures")).items():
            merged[branch] = rows
        ci_state["failures"] = merged
    return ci_state


def poll_project_vcs(config, project, state: dict, *, now: float, interval: float) -> list:
    """Poll upstream refs and CI once for a project under the shared lock.

    Schema: ``state`` is daemon in-memory poll state; ``now`` is wall-clock
    seconds; returns the due poll events (possibly empty). Bounds: at most one
    fetch, one CI tick, and one proposal page per call under timeout caps. Trust: host
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
            proposals = poll_proposals(config, project, state, now=now,
                                       interval=interval)
            if proposals is not None:
                events.append(proposals)
            try:
                write_json(project.root / POLL_STATE_FILE,
                           {"last_fetch": state.get("last_fetch"),
                            "last_poll": ci_state.get("last_poll"),
                            "last_proposal_poll": state.get("last_proposal_poll"),
                            "proposal_cursor": state.get("proposal_cursor"),
                            "failures": _clean_ci_failures(ci_state.get("failures"))})
            except (OSError, ValueError, TypeError) as exc:
                # Do not report a proposal page as successful when its cursor
                # and tick timestamp could not be durably resumed. Other VCS
                # polling retains its existing best-effort state semantics.
                proposal_event = next((event for event in events
                                       if event.get("event") == "proposal_poll"), None)
                if proposal_event is not None and proposal_event.get("ok"):
                    message = str(exc) or type(exc).__name__
                    proposal_event.update(
                        ok=False,
                        error=f"Could not persist proposal poll state: {message}"[:512])
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
            ordinary_after = 0.0
            while not stop.is_set():
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    from .probes import service_due, next_deadline
                    probe = service_due(config, name, stop)
                    if probe is not None:
                        print(json.dumps(probe, ensure_ascii=False), flush=True)
                    project = Project(config, name)
                    project.insights.ingest_editor(keep_days=config.limits.retention_days)
                    if vcs_poll_enabled(config):
                        interval = vcs_poll_interval(config)
                        for event in poll_project_vcs(config, project, upstream_state,
                                                      now=time.time(), interval=interval):
                            print(json.dumps(event, ensure_ascii=False), flush=True)
                    role = config.roles[role_name]
                    ready = should_run(project, project.snapshots.get(), role_name=role_name) if role.workspace == "write" else (
                        project.control().get("armed") and not project.control().get("paused") and not project.control().get("draining"))
                    if ready and time.monotonic() >= ordinary_after:
                        result = Engine(config, stop=stop).run(project, role_name)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        delay = config.limits.cooldown_seconds if role.workspace == "write" else config.limits.idle_seconds
                        if result.get("status") in ("waiting", "deferred"):
                            # Poll at idle cadence as well, to observe operator fact/config updates.
                            delay = min(config.limits.idle_seconds, max(0.1, result["next_evaluation_at"] - time.time()))
                        # Probe polling must not accelerate ordinary cooldowns.
                        ordinary_after = time.monotonic() + delay
                        delay = min(delay, config.limits.idle_seconds)
                        probe_at = next_deadline(config, name)
                        if probe_at is not None:
                            delay = min(delay, max(0.1, probe_at - time.time()))
                        stop.wait(delay)
                        continue
                except (MizuError, OSError, ValueError) as exc:
                    print(json.dumps({"event": "run_deferred", "error": str(exc), "time": now()}), flush=True)
                from .probes import next_deadline
                delay = config.limits.idle_seconds
                if ordinary_after > time.monotonic():
                    delay = min(delay, ordinary_after - time.monotonic())
                try:
                    probe_at = next_deadline(config, name)
                    if probe_at is not None:
                        delay = min(delay, max(0.1, probe_at - time.time()))
                except (MizuError, OSError, ValueError):
                    pass
                stop.wait(delay)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
