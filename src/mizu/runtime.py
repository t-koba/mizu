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
from .admission import admission, has_live_demand, role_exclusion
from . import check_jobs
from . import ci_waits, dispatch_evidence, safe_point, work_item_events
from .ci_observation import (MAX_CI_CHECKS_PER_BRANCH, MAX_CI_FAILURES,
                             MAX_CI_FAILURE_BRANCHES, MAX_CI_WAKE_BRANCHES,
                             MAX_CI_WAKE_ROWS, POLL_STATE_FILE,
                             _clean_ci_failures, _clean_ci_successes, _valid_ci_run,
                             ci_failures_projection, current_ci_failures,
                             effective_ci_wake_branches, wake_ci_failures)
from .ci_wake import (CI_WAKE_VERSION, DEFAULT_CI_REWAKE_SECONDS,
                      _ci_due, _ci_due_for, _ci_rewake_seconds,
                      _ci_wake_enabled, _ci_wake_fact_digest, _ci_wake_path,
                      _normalize_ci_wake_branches, read_ci_wake, write_ci_wake)
from .budget import Budget
from .config import (DAEMON_POLICIES, Config, Role, load,
                     role_cooldown_seconds, role_daemon_spacing_seconds, role_decision_events_limit,
                     role_policy_bytes, role_progress_signals, role_requests_per_run,
                     role_run_seconds, role_tools_per_run,
                     role_work_item_events_limit, role_wrap_up_requests,
                     role_wrap_up_tools)
from .doctor import container_runtime
from .drivers import DriverCheckpoint, driver_for
from .errors import RequestYieldExceeded
from .errors import (AuthorityGrantMismatch, Busy, Cancelled, CaptureLimitExceeded,
                      ConfigError, Denied, InfraExceeded, LimitExceeded, RunDeadlineExceeded,
                      DeadlineWrapUp, DrainYieldExceeded, MissingFinish, MizuError, ModelFailure,
                      ProtocolError)
from .fs import (canonical, digest, lock, mkdir, now, read_json, relative_parts,
                   safe_read, safe_write, sync_dir, write_json, PREVIEW_BYTES, READ_PAGE_CHARS, page,
                   text_preview, DIGEST, ID)
from .project import Project
from .snapshot import (MAX_MISSING_FINISH_STREAK, PROGRESS_SIGNALS, missing_finish_recovery_due,
                       missing_finish_streak, no_progress_recovery_due,
                       progress_disposition, validate_progress)
from .protocol import DEFINITIONS, tool_definitions, validate
from .report import previous as _previous_report, publish
from .sandbox import Sandbox, cleanup
from . import vcs as _vcs
from . import scheduling as _scheduling
from . import materialize as _materialize
from .ci_requests import CiRequests, request_id as ci_request_id
from .publication import Publication, source_commit
from .web import Web
from .web_worker import bounded
from .vcs_poll_policy import (MAX_CI_BRANCHES, POLL_CALL_TIMEOUT_CAP,
                              POLL_FETCH_TIMEOUT_CAP, _vcs_poll_number,
                              capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_enabled, vcs_poll_fetch_timeout,
                              vcs_poll_interval, vcs_poll_max_branches,
                              vcs_poll_proposals_enabled, vcs_poll_status_timeout)
from .ci_polling import (_merge_ci_success_maps, _merge_ci_success_rows,
                         _prune_ci_branch_map, poll_ci)
from .vcs_proposal_poll import (MAX_PROPOSAL_CURSOR, _valid_proposal_cursor,
                                poll_proposals)
from .vcs_upstream_poll import poll_upstream, refresh_upstream


def _normalized_wake_generation(value) -> str:
    """Fail closed on malformed local control generations without crashing a run."""
    return value if isinstance(value, str) else ""


def _wake_generation_digest(value) -> str:
    """Bound observer cursor state to a valid generation or the empty sentinel."""
    return digest(_normalized_wake_generation(value).encode())


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
#: A durable authority change mid-run is likewise a bounded, non-failure
#: result (bound "authority"): the next turn's resume refusal ends the unit
#: without further turns, publication, or health accounting.
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
#: Run-local shadow queue evidence retains the first few contended admissions.
MAX_QUEUE_OBSERVATIONS = 8
#: Fixed memory bounds for run-local pre-send admission identities.
MAX_ENGINE_TOOL_INVOCATIONS = 4096
MAX_ENGINE_TOOL_ID = 256
MAX_READ_IDENTITIES = 1024
#: Bound for role cursors: snapshot/code, wake generation, and (for read-only
#: completion) state digest, outcome and validated compact progress evidence.
OBSERVED_CURSOR_MAX_BYTES = 4096
#: Bounded serialized size of the work items a CI request covers.
MAX_COVERED_BYTES = 2048

#: Exact retained-evidence locators, never prefixes or filesystem paths.
EVIDENCE_UUID = re.compile(r"[0-9a-f]{32}\Z")
MAX_STARTED_EVIDENCE_BYTES = 65536


def _reject_nonfinite_json(value: str):
    raise ValueError("Non-finite JSON value")

#: Default wrap-up allowance granted when a per-run spend bound is reached:
#: extra model requests and extra wrap-up tool calls the unit may still spend
#: to seal a clean `bounded` end instead of failing. Policy defaults kept here
#: for compatibility; the effective values come from `[limits] wrap_up_requests`
#: / `wrap_up_tools` with optional per-role overrides. The daily budget still
#: applies to every admitted request.
WRAP_UP_REQUESTS = 3
WRAP_UP_TOOLS = 5
#: Operations admitted during wrap-up (plus bridge bookkeeping). Everything
#: else is refused once a per-run bound is reached.
WRAP_UP_OPS = frozenset({"finish", "research", "submit_insight", "attach_evidence", "decide"})
#: Reads of existing state are useful observation, not harness-observable progress.
OBSERVATIONAL_OPS = frozenset({
    "files", "read", "diff", "insights", "research_read", "vcs_read",
    "command_evidence_read", "experiment_evidence_read", "work_items_read", "work_items_legacy_read",
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


def _capture_cause(exc, cause=None) -> str | None:
    """The capture-limit cause this unit observed, if any.

    A capture bound names its cause in the error itself, so the authoritative
    publication capture reaches health without a separate hand-off. A cause
    recorded by an earlier observation of this unit wins.
    """
    if isinstance(cause, str) and cause:
        return cause
    if isinstance(exc, CaptureLimitExceeded):
        return str(exc)[:512]
    return None


def _record_health_failure(project, role_name, run_id, exc, maximum, cause=None,
                           resolved=False):
    """Advance the existing role failure brake once for a terminal failure.

    The consecutive-failure brake semantics are unchanged. A terminal
    missing-finish increments the dedicated consecutive-missing-finish
    streak; any other counted failure resets it to zero. The streak lives
    in the same health record (``consecutive_missing_finish``) and is
    bounded by ``MAX_MISSING_FINISH_STREAK``.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        health = read_json(project.root / "health" / f"{role_name}.json", {})
        failures = health.get("consecutive_failures", 0) + 1
        if isinstance(exc, MissingFinish):
            streak = missing_finish_streak(health) + 1
            streak = min(streak, MAX_MISSING_FINISH_STREAK)
        else:
            streak = 0
        observed = _capture_cause(exc, cause)
        previous = health.get("capture_limit") if isinstance(health, dict) else None
        # The cause that still applies: this unit's, or the recorded one while
        # nothing proved the workspace representable again.
        if observed:
            recorded = observed
        elif resolved:
            recorded = None
        else:
            recorded = previous if isinstance(previous, str) and previous else None
        write_json(project.root / "health" / f"{role_name}.json",
                   {"consecutive_failures": failures, "consecutive_missing_finish": streak,
                    "last_run": run_id, "error": str(exc), "updated_at": now(),
                    **({"capture_limit": recorded} if recorded else {})})
        if maximum > 0 and failures >= maximum and not isinstance(exc, Cancelled):
            reason = "Repeated failures; inspect health and run records"
            if recorded:
                reason = f"{reason}: {recorded}"
            project.set_control(paused=True, reason=reason)


def _record_missing_finish_only(project, role_name, run_id, exc, cause=None,
                                resolved=False):
    """Count a suppressed missing-finish toward the rotation streak only.

    A missing-finish with ``missing_finish_counts_as_failure`` disabled never
    touches the consecutive-failure brake or the pause control; it only
    advances the dedicated consecutive-missing-finish streak so a stuck
    session still rotates. Failure: never raises.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        health = read_json(project.root / "health" / f"{role_name}.json", {})
        if not isinstance(health, dict):
            health = {}
        streak = min(missing_finish_streak(health) + 1, MAX_MISSING_FINISH_STREAK)
        failures = health.get("consecutive_failures", 0)
        if type(failures) is not int or failures < 0:
            failures = 0
        observed = _capture_cause(exc, cause)
        previous = health.get("capture_limit") if isinstance(health, dict) else None
        if observed:
            recorded = observed
        elif resolved:
            recorded = None
        else:
            recorded = previous if isinstance(previous, str) and previous else None
        write_json(project.root / "health" / f"{role_name}.json",
                   {"consecutive_failures": failures, "consecutive_missing_finish": streak,
                    "last_run": run_id, "error": str(exc), "updated_at": now(),
                    **({"capture_limit": recorded} if recorded else {})})


def _clear_missing_finish_streak(project, role_name):
    """Reset the missing-finish streak after a non-missing-finish unit.

    Best-effort and byte-preserving when there is nothing to reset: a
    health record without the streak field (or already zero) is left
    untouched so bounded/deferred paths stay exactly as before.
    Failure: never raises.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        path = project.root / "health" / f"{role_name}.json"
        health = read_json(path, {})
        if not isinstance(health, dict):
            return
        if health.get("consecutive_missing_finish", 0) == 0 and "consecutive_missing_finish" not in health:
            return
        if missing_finish_streak(health) == 0 and health.get("consecutive_missing_finish") == 0:
            return
        health = {**health, "consecutive_missing_finish": 0, "updated_at": now()}
        write_json(path, health)


def _has_ready_work(project, role=None) -> bool:
    """Ready-work existence from a filtered bounded ledger scan; errors close."""
    if role is not None and "work_items_transition" not in role.capabilities:
        return False
    try:
        return bool(project.work_items.page_ready(
            limit=1, **({"role_name": role.name} if role is not None else {}))["items"])
    except Exception:
        return False


def _ready_work_changed(project, record, role=None) -> bool:
    """Compare full role-eligible membership to the input actually delivered.

    Current store/read or malformed progress errors close. Valid legacy records
    (or an undelivered input) admit nonempty sets once; empty legacy sets do not
    manufacture an event. The progress role and fingerprint scope prevent an
    observer from consuming another role's ready-work signal.
    """
    if role is not None and "work_items_transition" not in role.capabilities:
        return False
    try:
        previous = record.get("progress")
        if previous is not None:
            validate_progress(previous)
        page = project.work_items.page_ready(
            limit=1, **({"role_name": role.name} if role is not None else {}))
        signature = page["signature"]
        if not isinstance(signature, str) or not DIGEST.fullmatch(signature):
            return False
        if (previous is None or previous.get("ready_work_signature") is None
                or role is not None and previous["role"] != role.name):
            return page["total"] > 0
        return signature != previous["ready_work_signature"]
    except Exception:
        return False


# Saturating display bound, not an admission/aging policy.
MAX_READY_WORK_AGE_SECONDS = 2 ** 31 - 1


def _ready_work_age(updated_at, current_time):
    """Idle seconds since the latest ledger transition; unknown stays unknown."""
    try:
        timestamp = dt.datetime.fromisoformat(updated_at)
        if timestamp.tzinfo is None:
            return None
        age = current_time - timestamp.timestamp()
        if age != age or age in (float("inf"), float("-inf")):
            return None
        return int(min(MAX_READY_WORK_AGE_SECONDS, max(0, age)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _ready_work_projection(context):
    """Independent bounded ready page, never inferred from the general page."""
    page = context.project.work_items.page_ready(
        limit=context.config.limits.work_items_prompt, role_name=context.role.name)
    # Capture the same locked scan that supplies the full/delta input, never
    # the finish-time inventory: concurrent new work must remain unconsumed.
    context.ready_work_signature = page["signature"]
    current_time = time.time()
    return {"items": [{**{key: row[key] for key in
                        ("id", "title", "status", "stale", "redecision", "updated_at")},
                       "age_seconds": _ready_work_age(row["updated_at"], current_time),
                       **({"wait": row["wait"], "wait_satisfied": True}
                          if row["status"] == "blocked" else {})}
                      for row in page["items"]],
            **{key: page[key] for key in ("total", "overflow_count", "truncated",
                                         "unseeded_decisions", "coverage_complete")}}


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
                               "submit_insight", "revise_insight", "withdraw_insight", "withdraw_decided", "attach_evidence", "consult", "report", "sync",
                               "vcs_publish", "vcs_retire", "vcs_dispose", "vcs_request",
                               "research", "materialize", "work_items_create", "work_items_transition", "work_items_reconcile"})


#: Capabilities a delegation child must never hold. The child works in the
#: caller's live workspace, so decide/insight-write/vcs-mutating grants
#: and redelegation stay with the caller: verification and publication
#: never leave it. Implementation grants (files/read/exec/search/fetch and
#: the child's own profile/selector/policy) stay operator choice. finish
#: stays allowed: every configured role carries it so the child can seal,
#: and delegation ensures it (idempotent when already present).
DELEGATE_FORBIDDEN = frozenset({"decide", "submit_insight", "revise_insight",
                                "withdraw_insight", "withdraw_decided", "attach_evidence", "delegate",
                                "vcs_publish", "vcs_retire", "vcs_dispose", "vcs_request",
                                "work_items_create", "work_items_transition", "work_items_reconcile"})


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
        self.wake_generation = _normalized_wake_generation(
            project.control().get("wake_generation", ""))
        self.stop = stop or threading.Event()
        self.run_seconds = role_run_seconds(config, role)
        started = time.monotonic()
        self.deadline = started + self.run_seconds if self.run_seconds else None
        # Reserve a final logical-request window, capped to one of the
        # ordinary + wrap-up windows so short runs still do useful work.
        self.wrap_up_at = (self.deadline - min(config.limits.model_request_seconds,
                                             self.run_seconds / (role_wrap_up_requests(config, role) + 1))
                           if self.deadline is not None else None)
        self.request_deadlines: dict[tuple[int, int], float] = {}
        # Bounded aggregate activity evidence: these buckets are exclusive,
        # with joins over tools and tools over logical model-request spans.
        self._activity_origin = started
        self._activity_last = started
        self._activity_current = "other"
        self._activity_seconds: dict[str, float] = {}
        self._activity_counts: dict[str, int] = {}
        self._activity_published: str | None = None
        self._activity_operation: str | None = None
        self._activity_join: str | None = None
        self._activity_max_requests = 0
        self._activity_reaped_requests = 0
        self.request_failure_pending = False
        # The CI requests this run made, for the run's own record.
        self.ci_requests: list = []
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
        self.driver_checkpoint: DriverCheckpoint | None = None
        self.completed_driver_units = 0
        self.safe_point_yield_requested = False
        self.checkpoint_seal_only = False
        self._checkpoint_launch_pending = False
        self._checkpoint_driver_started = False
        self._checkpoint_invocation_attempted = False
        self._checkpoint_finish_attempted = False
        self._checkpoint_seal_error = None
        self._checkpoint_settled = False
        self._checkpoint_unit = None
        self._checkpoint_engine = None
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
        # Delegated children use an aggregate share of these same wrap-up
        # allowances; the top-level context retains all unused capacity.
        self.delegated_child = False
        self.delegated_wrap_up_requests = 0
        self.delegated_wrap_up_tools = 0
        self.wrap_up_attempted = False
        self.finished: dict | None = None
        self.missing_finish_evidence: dict | None = None
        # The capture-limit cause of this unit, when the workspace could not
        # be represented, and whether a capture proved it representable
        # again. The cause reaches the writer, the run record and health; it
        # stays while it applies and is cleared when a capture succeeds.
        self.capture_limit: str | None = None
        self.capture_resolved: bool = False
        # Temporary follow-up grants do not fork an otherwise identical saved session.
        self.session_identity_capabilities: tuple[str, ...] | None = None
        # Host-only authority and permission for a reduced-grant continuation.
        self.turn_authority_role = None
        self.turn_continuation = False
        self.admission_error: str | None = None
        self.admission_exception: MizuError | None = None
        self.read_content_ids: dict[str, str] = {}
        self.admission_wait = False
        # Selector failover identity for durable submissions. Retries and
        # recovery of this candidate keep the same ordinal; only a proven
        # terminal pi-durable unanswered settlement can advance it.
        self.attempt_ordinal = 0
        self.decision_events: list = []
        self.decision_receipt_diagnostics: list = []
        self.work_item_events: list = []
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

    @property
    def driver_capabilities(self):
        """Expose no ordinary operation in a claimed checkpoint seal phase."""
        return tuple(c for c in self.role.capabilities if not self.checkpoint_seal_only or c == "finish")

    def begin_driver(self, engine: str, unit: str):
        """Invalidate a prior boundary before launch; a claimed one is final."""
        with self.mutex:
            if self.checkpoint_seal_only:
                self.check_deadlines(wrap_up=True)
                authority = self.turn_authority_role or self.role
                if (not self._checkpoint_launch_pending or self.role.engine_tools or authority.engine_tools
                        or (engine, unit) != (self._checkpoint_engine, self._checkpoint_unit)):
                    raise Denied("No inference in the checkpoint seal phase")
                self._checkpoint_launch_pending = False
                self._checkpoint_driver_started = True
            self.driver_checkpoint = None

    def complete_driver_checkpoint(self, engine: str, unit: str):
        """Called only after successful terminal completion AND bridge teardown.

        Query/turn completion is not a Pi model_request boundary. Engine-tool
        admission has no completion acknowledgment, so those paths fail closed.
        This records a candidate; only the configured host scheduler may consume it.
        """
        with self.mutex:
            self.driver_checkpoint = None
            authority = self.turn_authority_role or self.role
            if self.checkpoint_seal_only:
                self._checkpoint_settled = (
                    self._checkpoint_driver_started and (engine, unit) == (self._checkpoint_engine, self._checkpoint_unit)
                    and (engine, unit) in (("claude", "query"), ("codex", "turn"))
                    and self.role.workspace == "write" and authority.workspace == "write"
                    and not self.delegated_child and not self.ephemeral
                    and not self.role.engine_tools and not authority.engine_tools and self.operations_stopped.is_set()
                    and not self.request_deadlines and not self.request_failure_pending
                    and self.admission_error is None and self.admission_exception is None
                    and not self.operation_active.is_set() and self.joins_idle.is_set())
                return
            if ((engine, unit) not in (("claude", "query"), ("codex", "turn"))
                    or self.role.workspace != "write" or authority.workspace != "write"
                    or self.delegated_child or self.ephemeral
                    or self.role.engine_tools or authority.engine_tools or self.finished is not None
                    or self.checkpoint_seal_only or not self.operations_stopped.is_set()
                    or self.request_deadlines or self.request_failure_pending
                    or self.admission_error is not None or self.admission_exception is not None
                    or self.operation_active.is_set() or not self.joins_idle.is_set() or not self.request_count):
                return
            self.driver_checkpoint = DriverCheckpoint(engine, unit, self.execution_attempt, self.request_count)
            if self.config.limits.safe_point_yield_quantum:
                self.completed_driver_units += 1

    def claim_driver_checkpoint(self, checkpoint: DriverCheckpoint):
        """Host-only, one-shot transition to finish-only, not a finish or yield.

        The caller must already own any scheduling decision. No ordinary tools
        or new inference may start after this latch, except the one host-armed
        finish-only driver unit with normal grant, budget and seal checks.
        Tokens cannot survive another dispatch, operation, attempt or restart.
        """
        with self.mutex:
            self.check_deadlines(wrap_up=True)  # stop/authority and bounds win
            if self.bound_hit is not None:
                raise Denied(self._bound_message())
            authority = self.turn_authority_role or self.role
            if (checkpoint is None or checkpoint is not self.driver_checkpoint
                    or (checkpoint.engine, checkpoint.unit) not in (("claude", "query"), ("codex", "turn"))
                    or checkpoint.execution_attempt != self.execution_attempt
                    or checkpoint.requests != self.request_count
                    or self.role.workspace != "write" or authority.workspace != "write"
                    or self.delegated_child or self.ephemeral
                    or self.role.engine_tools or authority.engine_tools or self.finished is not None
                    or self.checkpoint_seal_only or not self.operations_stopped.is_set()
                    or self.request_deadlines or self.request_failure_pending
                    or self.admission_error is not None or self.admission_exception is not None
                    or self.operation_active.is_set() or not self.joins_idle.is_set()):
                raise Denied("No quiescent driver checkpoint")
            self.checkpoint_seal_only = True
            self._checkpoint_engine = checkpoint.engine
            self._checkpoint_unit = checkpoint.unit
            self.driver_checkpoint = None

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
        if not self.config.limits.drain_yield:
            return
        if self.ephemeral and not getattr(self, "delegated_child", False):
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
            if getattr(self, "delegated_child", False):
                # A delegated child has no session to preserve. It stops at
                # this safe boundary and tells its caller, so a draining
                # parent is not held by a child that cannot finish.
                self.bound_hit = "drain"
                return
            from .engine_config import effective
            if effective(self.config, self.role, self.role.profile).get("session") == "persistent":
                self.bound_hit = "drain"
        finally:
            self.mutex.release()

    #: The refusal the shared handshake carries when a drain bound refuses a
    #: provider request. The durable launcher classifies this exact text, so a
    #: delegated child's stop is the drain bound on every engine.
    DRAIN_REQUEST_REFUSED = "Drain bound refused the provider request"

    def drain_refusal(self, message: str):
        """Raise the refusal the drain bound requires at this boundary.

        A delegated child has no session to wrap up: it stops at the safe
        boundary and reports the interruption to its caller, so a draining
        parent is not held by a child that cannot finish. Every engine's
        admission path uses this, so one drain rule holds for all of them.

        Bounds: one message. Trust: the project's own drain state. Failure:
        raises ``Cancelled`` for a delegated child and ``DeadlineWrapUp``
        otherwise.
        """
        if getattr(self, "delegated_child", False):
            self.drain_admission_refused = True
            # The same text the adapters classify: a refusal the launcher
            # does not recognise is a provider failure, not the drain bound.
            raise Cancelled(self.DRAIN_REQUEST_REFUSED)
        raise DeadlineWrapUp(message)

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
        if self._activity_current != self._activity_published:
            self._publish_activity()
        if count is not None:
            self._activity_counts[count] = self._activity_counts.get(count, 0) + 1

    def _publish_activity(self) -> None:
        """Publish the same bounded live gauge for top-level and child runs.

        runs/<id>/activity.json is local observational evidence (schema 1),
        not authority. Updates atomically replace one record on bucket changes;
        no prompt, command or model content is included. Failure is best-effort,
        never blocks cancellation or changes admission. Terminal run evidence
        determines whether this retained gauge is still live.
        """
        try:
            from .fs import read_json as _read, write_json as _write
            path = self.project.root / "active" / f"{self.role.name}.json"
            record = _read(path, {})
            owns_active = isinstance(record, dict) and record.get("run") == self.run_dir.name
            if not owns_active and not getattr(self, "parent_run", None):
                return
            seconds = dict(self._activity_seconds)
            total = sum(seconds.values())
            seconds[self._activity_current] = seconds.get(self._activity_current, 0.0)
            gauge = {"activity": self._activity_current,
                     "activity_since": now(),
                     "activity_seconds": {key: round(value, 3) for key, value in sorted(seconds.items())},
                     "activity_total_seconds": round(total, 3)}
            _write(self.run_dir / "activity.json", {
                "schema": 1, "run": self.run_dir.name, "role": self.role.name,
                "parent_run": getattr(self, "parent_run", None), **gauge})
            self._activity_published = self._activity_current
            if owns_active:
                _write(path, {**record, **gauge})
        except Exception:
            return

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

    def record_ci_request(self, request_id, date, workflows, covered, *, state="unresolved"):
        """Record one CI request this run made, for the run's own record.

        Schema: one entry per request, ``{"request_id", "date", "workflows",
        "covered", "state"}``; the date is the UTC day the request was made
        and the state is ``unresolved`` while only the dispatch is known and
        ``resolved`` once the outcome is durable. A request is recorded when
        it is dispatched, because from that moment the run may have spent a
        CI run; the same request is updated, not repeated, when its outcome
        arrives. The day a request was made is fixed at the dispatch and
        never moves: a later answer does not move the request to the day it
        was answered, so an update keeps the day that was recorded first.
        The list is bounded: at most 16 entries are kept and the count of all
        requests is reported beside them, so a truncated list is visible.
        """
        with self.mutex:
            entry = {"request_id": request_id, "date": date,
                     "workflows": list(workflows), "covered": list(covered),
                     "state": state}
            for existing in self.ci_requests:
                if existing["request_id"] == request_id:
                    # The day is the day the request was made, and it was
                    # fixed when the dispatch was recorded. Keeping it here
                    # makes it impossible for a later update to move it.
                    existing.update({**entry, "date": existing["date"]})
                    return
            self.ci_requests.append(entry)

    def ci_request_summary(self) -> dict:
        """The CI requests this run made, bounded for the run record."""
        with self.mutex:
            entries = list(self.ci_requests)
        return {"requests": entries[-16:], "total": len(entries)}

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
                # The transport poll is a drain crossing like any other: the
                # same refusal, so a delegated child stops here instead of
                # taking a wrap-up turn it cannot finish.
                self.drain_refusal(self._bound_message())

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

    def _delegated_wrap_up_limits(self) -> tuple[int, int]:
        """Effective wrap-up allowance for a delegated child (clamped shares).

        The child share never exceeds the spend role's effective total minus
        one, so a small configured total cannot starve the caller's own seal:
        the caller always retains at least one wrap-up request and tool.
        """
        role = self.spend_role or self.role
        total_requests = role_wrap_up_requests(self.config, role)
        total_tools = role_wrap_up_tools(self.config, role)
        return (max(0, min(self.config.limits.delegate_wrap_up_requests, total_requests - 1)),
                max(0, min(self.config.limits.delegate_wrap_up_tools, total_tools - 1)))

    def _wrap_up_request_used_limit(self) -> tuple[int, int]:
        """Effective (used, limit) for wrap-up model requests on this context."""
        if self.delegated_child:
            return (self.delegated_wrap_up_requests, self._delegated_wrap_up_limits()[0])
        role = self.spend_role or self.role
        return (self.wrap_up_requests + self.delegated_wrap_up_requests,
                role_wrap_up_requests(self.config, role))

    def _wrap_up_remaining(self) -> tuple[int, int]:
        if self.delegated_child:
            child_requests, child_tools = self._delegated_wrap_up_limits()
            return (child_requests - self.delegated_wrap_up_requests,
                    child_tools - self.delegated_wrap_up_tools)
        role = self.spend_role or self.role
        return (role_wrap_up_requests(self.config, role) - self.wrap_up_requests - self.delegated_wrap_up_requests,
                role_wrap_up_tools(self.config, role) - self.wrap_up_tools - self.delegated_wrap_up_tools)

    def _bound_message(self) -> str:
        requests_left, tools_left = self._wrap_up_remaining()
        how = f"{max(0, requests_left)} requests and {max(0, tools_left)} wrap-up tools"
        tools = ", ".join(tool["name"] for tool in tool_definitions(
            tuple(c for c in self.role.capabilities if c in WRAP_UP_OPS))) or "none"
        return (f"For the rest of this work unit only: per-run {self.bound_hit} "
                f"bound reached; wrap-up only ({tools}) with {how} left")

    def handle(self, operation: str, arguments: dict) -> dict:
        if not self.checkpoint_seal_only or operation != "finish":
            return self._handle(operation, arguments)
        with self.mutex:
            try:
                return self._handle(operation, arguments)
            except Exception as exc:
                # Preserve the first failed seal even if the adapter masks the
                # tool error. Neither another finish nor a normal return repairs it.
                if self._checkpoint_finish_attempted and self.finished is None and self._checkpoint_seal_error is None:
                    self._checkpoint_seal_error = exc
                raise

    def _handle(self, operation: str, arguments: dict) -> dict:
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise Denied("Invalid operation")
        with self.mutex:
            checkpoint_protocol = self.checkpoint_seal_only and operation in ("_model_usage", "_request_done")
            if self.host_cancelled() or (self.operations_stopped.is_set() and not checkpoint_protocol):
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
            if self.checkpoint_seal_only:
                handshake = (operation == "_hello" and self._checkpoint_driver_started
                             and not self._checkpoint_invocation_attempted and not self.hello.is_set())
                if operation != "finish" and not checkpoint_protocol and not handshake:
                    raise Denied("Only finish is allowed in the checkpoint seal phase")
                if operation == "finish":
                    if (not self._checkpoint_driver_started
                            or self.invocation_admitted_attempt != self.execution_attempt
                            or self._checkpoint_finish_attempted):
                        raise Denied("Checkpoint finish requires its one admitted model unit")
                    self._checkpoint_finish_attempted = True
            self.driver_checkpoint = None
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
                    # One shared handshake: the message carries the bound kind,
                    # so each engine reports the kind it was given instead of
                    # naming the request budget. This is not a daily-budget
                    # rejection or an admitted request.
                    self.drain_admission_refused = True
                    self.drain_refusal("Drain bound refused the provider request")
                try:
                    sequence = arguments.get("sequence")
                    if type(sequence) is not int or sequence < 1:
                        raise Denied("Invalid request sequence")
                    sequence_key = (self.execution_attempt, sequence)
                    if sequence_key not in self.request_sequences:
                        over = self.bound_hit in ("deadline", "drain") or self.request_count >= role_requests_per_run(
                            self.config, self.spend_role or self.role)
                        if (over and self.bound_hit in (None, "requests")
                                and not self.wrap_up_attempted):
                            self.bound_hit = "requests"
                            self.request_admission_refused = True
                            raise DeadlineWrapUp("Per-run provider request budget exhausted")
                        requests_used, requests_limit = self._wrap_up_request_used_limit()
                        if over and requests_used >= requests_limit:
                            if self.bound_hit == "drain":
                                raise DrainYieldExceeded("Drain wrap-up request allowance exhausted")
                            if self.bound_hit == "deadline":
                                raise RunDeadlineExceeded("Deadline wrap-up request allowance exhausted")
                            self.request_bound_exhausted = True
                            raise RequestYieldExceeded("Per-run provider request budget exhausted")
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
                    self.admission_error = None
                    self.admission_exception = None
                    self.admission_wait = False
                    return {"admitted": True}
                except MizuError as exc:
                    self.admission_error = str(exc)
                    self.admission_exception = exc
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
                if self.delegated_child:
                    tools_used = self.delegated_wrap_up_tools
                    tools_limit = self._delegated_wrap_up_limits()[1]
                else:
                    tools_used = self.wrap_up_tools + self.delegated_wrap_up_tools
                    tools_limit = role_wrap_up_tools(self.config, self.spend_role or self.role)
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
                idempotent_replay = (operation in ("work_items_create", "work_items_reconcile")
                                     and isinstance(result, dict)
                                     and result.get("created") is False)
                if (operation != "finish" and not _is_observational_operation(operation, arguments)
                        and not idempotent_replay):
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
        base = self._observed_diff_base()
        if base is not None:
            try:
                return self.project.snapshots.changes(self.snapshot["id"], base=base)
            except Denied:
                pass
        return self.project.snapshots.changes(self.snapshot["id"])

    def _observed_diff_base(self) -> str | None:
        """Return this role's observed snapshot ID when it is usable as a diff base.

        Schema: the ``observed/<role>.json`` cursor the engine writes, holding
        ``snapshot`` and ``code_digest`` (and read-only scheduling evidence). Bounds: the cursor is read under
        ``OBSERVED_CURSOR_MAX_BYTES``. Trust: local recorded state, never a
        claim about the tree, so an absent, malformed, digest-mismatched or
        no-longer-retained cursor is unusable and yields None; ancestry is
        rechecked by the snapshot store at diff time and falls back there.
        Failure: never raises for cursor problems; a valid cursor naming the
        current snapshot is usable and renders an empty diff.
        """
        try:
            cursor = read_json(
                self.project.root / "observed" / f"{self.role.name}.json",
                {}, max_bytes=OBSERVED_CURSOR_MAX_BYTES)
        except (OSError, ValueError, Busy, Denied, LimitExceeded):
            return None
        if not isinstance(cursor, dict):
            return None
        snapshot_id = cursor.get("snapshot")
        code_digest = cursor.get("code_digest")
        if (not isinstance(snapshot_id, str) or not DIGEST.fullmatch(snapshot_id)
                or not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest)):
            return None
        try:
            record = self.project.snapshots.get(snapshot_id)
        except (OSError, ValueError, Busy, Denied, LimitExceeded):
            return None
        if record.get("code_digest") != code_digest:
            return None
        return snapshot_id

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
            expected = self.read_content_ids.get(path)
        if expected is not None and (not isinstance(expected, str) or not DIGEST.fullmatch(expected)):
            raise Denied("Invalid read content_id")
        ref = _vcs.split_ref_path(path)
        if ref is not None:
            if self.role.workspace == "none":
                raise Denied("Path is not exposed to this role")
            # Existence probe, not a read: any workspace entry at the
            # virtual path collides, even a symlink, directory, special
            # file or hardlink that safe_read would refuse. Only a
            # missing path falls through to the virtual ref. Unreadable
            # states fail closed toward refusal.
            try:
                target = self.workspace.joinpath(*relative_parts(path))
            except (ValueError, Denied, OSError):
                collision = False
            else:
                try:
                    os.lstat(target)
                except FileNotFoundError:
                    collision = False
                except NotADirectoryError:
                    collision = False
                except OSError:
                    collision = True
                else:
                    collision = True
            if collision:
                raise Denied("Path collides with the reserved virtual upstream-ref prefix; "
                             "relocate genuine files out of refs/remotes/upstream/")
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
        if path not in self.read_content_ids and len(self.read_content_ids) >= MAX_READ_IDENTITIES:
            raise LimitExceeded("Read identity retention bound exceeded")
        self.read_content_ids[path] = content_id
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

    def _new_invocation_evidence(self, kind: str) -> tuple[str, str]:
        """Return a fresh per-invocation adapter evidence identity.

        Schema: ``kind`` names the evidence directory (``vcs-read``,
        ``vcs-publish``, ``vcs-retire``, ``vcs-dispose``, ``materialize``);
        the path is ``<kind>/<uuid>.json``. Returns ``(evidence_id,
        relative_path)``. The caller builds the full bound record (with
        ``evidence_id`` and ``evidence``). Mutation callers persist an intent
        before dispatch and update that same path with the observation; other
        callers write once. Every invocation gets a fresh id that no later
        call can overwrite. Write errors propagate.
        """
        evidence_id = uuid.uuid4().hex
        return evidence_id, f"{kind}/{evidence_id}.json"

    def _mutation_evidence(self, kind: str, op: str, parameters: dict):
        """Build dispatch/observation callbacks for one VCS mutation.

        Only bounded, validated non-secret pins/approval identities belong in
        ``parameters``, never adapter argv, credentials or approval bodies.
        The dispatch guard allocates and durably writes an outcome-unknown
        intent; failure prevents dispatch. A validated observation retains the
        intent and identity at the same path. An interrupted/failed adapter call
        leaves the intent alone; nothing here retries or reconciles it.
        """
        pending = None

        def before_invoke():
            nonlocal pending
            evidence_id, evidence = self._new_invocation_evidence(kind)
            pending = {"evidence_id": evidence_id, "evidence": evidence,
                       "outcome": "unknown",
                       "intent": {"op": op, "parameters": parameters}}
            write_json(self.run_dir / evidence, pending, exclusive=True)
            # Persist the evidence-directory entry as well as the file itself.
            sync_dir(self.run_dir)

        def observe(record):
            record = {**record, **pending, "outcome": "observed"}
            path = self.run_dir / pending["evidence"]
            try:
                write_json(path, record)
            except BaseException:
                # Atomic writes normally leave the intent untouched. Restore it
                # also if a late failure (e.g. directory fsync) followed rename.
                # This is evidence repair only, never another adapter attempt.
                write_json(path, pending)
                raise
            return record

        return before_invoke, observe

    def _op_command_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("commands", args)

    def _op_experiment_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("experiments", args)

    def _op_exec_experiment(self, op: str, args: dict) -> dict:
        if "check_job" in self.role.capabilities and "source_receipt" not in args and (op == "experiment" or self.role.workspace == "read"):
            return check_jobs.submit(self, args["script"], kind=op)
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
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

    def _op_check_job(self, args: dict) -> dict:
        action = args['action']
        if action == 'start':
            return check_jobs.submit(self, args.get('script'), fresh=args.get('fresh', False),
                                     purpose=args.get('purpose'))
        if action == 'list':
            from .acceptance import discover, producer_diagnostics
            discovery = discover(self.project)
            return {'jobs': check_jobs.list_jobs(self.project, self.role.name, discovery=discovery),
                    'available': check_jobs.available(self, discovery=discovery),
                    **producer_diagnostics(discovery[1])}
        if not args.get('id'):
            raise Denied('Check result requires id')
        if action == 'join':
            return check_jobs.join(self, args['id'])
        if action == 'logs':
            return check_jobs.logs(self, args['id'], args.get('evidence_id'), offset=args.get('offset', 0), limit=args.get('limit', 8192), content_id=args.get('content_id'))
        if action == 'cancel':
            return check_jobs.cancel(self, args['id'])
        return check_jobs.result(self, args['id'])

    def _op_verify(self, args: dict) -> dict:
        if "check_job" in self.role.capabilities:
            return check_jobs.submit(self)
        if not self.project.verify:
            raise Denied("No operator acceptance commands are configured")
        from . import acceptance
        bound = acceptance.scope(self.project)
        if bound is not None and check_jobs.execution_key(self.config) != bound['execution']:
            bound = None  # A context cannot attest another execution config.
        before = self.project.snapshots.capture_files(self.workspace)
        results = [self.sandbox.execute(self.workspace, command, writable=True)
                   for command in self.project.verify]
        after = self.project.snapshots.capture_files(self.workspace)
        passed = (all(r["exit_code"] == 0 and r["reason"] == "exited" for r in results)
                  and [r["script"] for r in results] == list(self.project.verify))
        representable = not before["skipped"] and not after["skipped"]
        unchanged = before["code_digest"] == after["code_digest"]
        dependencies_unchanged = bound is None or acceptance.scope(self.project) == bound
        self.verification = {"passed": passed and unchanged and representable and dependencies_unchanged,
                             "dependencies_unchanged": dependencies_unchanged,
                             "snapshot_representable": representable,
                             "code_digest": after["code_digest"], "image": self.config.sandbox.image,
                             "unchanged_during_verification": unchanged,
                             "commands": [{"id": r["id"], "exit_code": r["exit_code"], "reason": r["reason"],
                                           "script": r["script"]} for r in results],
                             "run": self.run_dir.name, "created_at": now()}
        producer_record = dict(self.verification)
        if self.verification['passed'] and bound is not None and not self.tools_cancelled():
            producer_record['acceptance_scope'] = bound
        write_json(self.run_dir / "verification.json", producer_record)
        if producer_record.get('acceptance_scope') is not None:
            acceptance.recover_run(self.project, self.run_dir.name)
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

    def _op_work_items_read(self, args: dict) -> dict:
        if "id" in args:
            if "cursor" in args:
                raise Denied("Work-item by-ID history cannot be combined with open-page cursor")
            return self.project.work_items.explain(
                args["id"], after_rev=args.get("after_rev", 0), limit=args.get("limit", 25))
        if "after_rev" in args:
            raise Denied("Work-item after_rev requires id")
        return self.project.work_items.page_open(cursor=args.get("cursor"),
                                                 limit=args.get("limit", 25))

    def _op_work_items_legacy_read(self, args: dict) -> dict:
        return self.project.work_items.page_legacy(cursor=args.get("cursor"),
                                                   limit=args.get("limit", 25))

    def _op_work_items_reconcile(self, args: dict) -> dict:
        return self.project.work_items.reconcile(
            args["event_id"], expected_action=args["expected_action"],
            expected_rev=args.get("expected_rev"), outcome=args["outcome"],
            reason=args["reason"], title=args.get("title"), actor=self.role.name)

    def _op_work_items_create(self, args: dict) -> dict:
        return self.project.work_items.create(
            idempotency_key=args["idempotency_key"], title=args["title"],
            origin=args.get("origin"), actor=self.role.name, smoke_refs=args.get("smoke_refs"),
            **({"description": args["description"]} if "description" in args else {}),
            **({"scope": args["scope"]} if "scope" in args else {}))

    def _op_work_items_transition(self, args: dict) -> dict:
        return self.project.work_items.transition(
            args["id"], expected_rev=args["expected_rev"], status=args["status"],
            actor=self.role.name, reason=args.get("reason"), wait=args.get("wait"),
            snapshot_id=args.get("snapshot_id"), scope=args.get("scope"), smoke_refs=args.get("smoke_refs"))

    def _op_insights(self, args: dict) -> dict:
        if args.get("id"):
            record = self.project.insights.read(args["id"])
            # The original submitter may follow an accepted proposal into its
            # current item without receiving a separate work-item capability.
            # This is a read-only projection; all item validation remains in
            # WorkItems, and list paths/non-owners keep their existing shape.
            if record.get("source") == self.role.name:
                status = self.project.work_items.submitter_status(record["id"])
                if status is not None:
                    record["work_item_status"] = status
            return record
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
                                            expected_rev=args["rev"], work_item_intent=args.get("work_item_intent"),
                                            scope=args.get("scope"), work_items=args.get("work_items"))

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
        # mechanism enforces source match plus compare-and-swap on the read rev.
        # Hiding a current accept/modify additionally needs the supplemental
        # withdraw_decided grant; the operator CLI passes no such permission.
        capabilities = set(self.role.capabilities)
        allow_decided = ("withdraw_insight" in capabilities
                         and "withdraw_decided" in capabilities)
        return self.project.insights.withdraw(args["id"], source=self.role.name, reason=args["reason"],
                                               expected_rev=args["rev"], run=self.run_dir.name,
                                               allow_decided=allow_decided)

    def _op_attach_evidence(self, args: dict) -> dict:
        # Attributed annotation of another insight: source is fixed to the
        # caller's role and the mechanism (insights.attach_evidence) binds to
        # the read rev without changing title, body, or rev, so ownership and
        # revise/withdraw rights stay with the submitter only.
        record = self.project.insights.attach_evidence(
            args["id"], source=self.role.name, text=args["text"],
            run=self.run_dir.name, expected_rev=args["rev"], smoke_refs=args.get("smoke_refs"))
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
            fetched = _vcs.fetch_refs(self.config.vcs, cancel=self.tools_cancelled)
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

    def _op_vcs_read_publications(self, args: dict) -> dict:
        # Local listing of the project's recorded publication receipts.
        # No provider is contacted: this reads the project's own durable
        # evidence, so no adapter call is made. Only the op and the exact
        # code_digest/branch filters are accepted.
        if set(args) - {"op", "code_digest", "branch", "published_sha"}:
            raise Denied("publication listing takes only code_digest, branch and published_sha filters")
        # The page reports the match count, whether the page dropped matches
        # and whether a scan bound stopped the search, so an absent receipt is
        # never confused with an omitted one.
        listing = Publication.list_receipts(
            self.project.root, code_digest=args.get("code_digest"),
            branch=args.get("branch"), published_sha=args.get("published_sha"), limit=100)
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": "publications", "branch": args.get("branch"),
                  "trust": "recorded-local-untrusted", "evidence_id": evidence_id,
                  "evidence": evidence, "result": listing}
        write_json(self.run_dir / evidence, record)
        # The listing is also the top-level result, like the other read ops.
        return {**record, **listing}

    def _op_vcs_read_ci_requests(self, args: dict) -> dict:
        # Local listing of the project's recorded CI requests, grouped by day.
        # No provider is contacted: this reads the project's own durable
        # evidence, so no adapter call is made. Only the op is accepted.
        if set(args) - {"op"}:
            raise Denied("CI request listing takes no filters")
        listing = CiRequests(self.project.root).daily(limit=100)
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": "ci_requests", "trust": "recorded-local-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence, "result": listing}
        write_json(self.run_dir / evidence, record)
        # The listing is also the top-level result, like the other read ops.
        return {**record, **listing}

    def _op_vcs_read(self, args: dict) -> dict:
        # Read-only VCS view: CI status, logs, PR comments, external
        # proposals, and CI-recorded artifact files via the trusted
        # adapter. Never publishes; mutating ops are refused here even if
        # the adapter would serve them. The adapter-backed reads require a
        # visible workspace. A
        # publication_id is a status-only locator resolved through the
        # validated durable receipt; callers cannot override its branch/SHA.
        # Branch stays required for addressed reads
        # (status/log/comments/artifacts/artifact) inside read_via;
        # proposals enumerate with an optional branch filter; acquire
        # takes a proposal id, the exact head sha, and the explicit
        # content scope (plus an optional base sha pin for full scope)
        # and needs no branch; artifact takes a branch, an artifact file
        # name, and an optional sha filter. The publications op lists the
        # project's recorded receipts from local durable evidence and
        # contacts no provider.
        if args["op"] == "publications":
            # Local evidence listing: no adapter, no network, no workspace.
            return self._op_vcs_read_publications(args)
        if args["op"] == "ci_requests":
            # Local evidence listing: no adapter, no network, no workspace.
            return self._op_vcs_read_ci_requests(args)
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        if "code_digest" in args:
            # The digest filter belongs to the local publications listing
            # only; an addressed read never accepts it.
            raise Denied("code_digest is only valid for the publications listing")
        params = {}
        publication = None
        if "publication_id" in args:
            if args["op"] != "status" or set(args) != {"op", "publication_id"}:
                raise Denied("publication_id status reads take no branch, sha, or other filters")
            publication = Publication.read_receipt(self.project.root, args["publication_id"])
            params["branch"] = publication["branch"]
            params["sha"] = publication["published_sha"]
        else:
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
            data = _vcs.read_via(self.config.vcs, args["op"], params,
                             cancel=self.tools_cancelled)
        except OSError as exc:
            raise Denied(f"VCS read is unavailable: {exc}") from exc
        if publication is not None:
            observed = {
                "publication_id": publication["publication_id"],
                "source_commit": publication["source_commit"],
                "code_digest": publication["code_digest"],
                "branch": publication["branch"],
                "published_sha": publication["published_sha"],
                "approval": publication["approval"],
                "run": publication["run"],
            }
            if any(key in publication for key in ("projection_schema", "projection_algorithm",
                                                  "projection_digest")):
                observed = {**observed,
                            "projection_schema": publication["projection_schema"],
                            "projection_algorithm": publication["projection_algorithm"],
                            "projection_digest": publication["projection_digest"]}
            data = {**data, "publication": observed}
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": args["op"], "branch": params.get("branch"), "trust": "external-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence, "result": data}
        write_json(self.run_dir / evidence, record)
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
        from .distribution import public_source_identity
        try:
            projection = public_source_identity(captured.get("files", {}))
        except ValueError as exc:
            raise Denied(f"Publication requires a projectable snapshot: {exc}") from exc
        approval = _vcs.require_go_approval(self.project, args["branch"], code_digest)
        try:
            source = source_commit(self.workspace, cancel=self.tools_cancelled)
            publication = Publication(self.project.root, source, code_digest,
                                      args["branch"], args["op"], projection=projection)
            with lock(publication.lock):
                intent, receipt = publication.prepare(approval["insight"], self.run_dir.name)
                before_local, observe = self._mutation_evidence(
                    "vcs-publish", args["op"],
                    {"branch": args["branch"], "code_digest": code_digest,
                     "projection_schema": projection["schema"],
                     "projection_algorithm": projection["algorithm"],
                     "projection_digest": projection["digest"],
                     "approval": approval["insight"], "source_commit": source,
                     "publication_id": publication.id})

                def before_invoke():
                    before_local()
                    publication.mark_dispatch()

                if receipt is None:
                    try:
                        data = _vcs.publish_via(
                            self.config.vcs, args["op"],
                            {**publication.identity, "publication_id": publication.id},
                            cancel=self.tools_cancelled, before_invoke=before_invoke)
                    except (Denied, OSError) as exc:
                        raise Denied(f"VCS publication {publication.id}: {exc}") from exc
                    receipt = publication.complete(intent, data["published_sha"])
                else:
                    # Retain a run-local observation even on a durable replay.
                    before_local()
                    data = {"digest": code_digest, "publication_id": publication.id,
                            "projection_schema": projection["schema"],
                            "projection_algorithm": projection["algorithm"],
                            "projection_digest": projection["digest"],
                            "published_sha": receipt["published_sha"],
                            "trust": "external-untrusted"}
        except OSError as exc:
            raise Denied(f"VCS publication is unavailable: {exc}") from exc
        record = {**receipt, "published": True,
                  "receipt": publication.path("receipt").relative_to(self.project.root).as_posix(),
                  "trust": "external-untrusted", "result": data}
        return observe(record)

    def _op_vcs_request(self, args: dict) -> dict:
        # Ask the provider for one CI run per workflow at an exact published
        # commit. The target is a completed publication receipt, never a
        # branch head: the product verifies every returned run's head sha
        # against that commit and fails closed on any other binding. A
        # recorded request replays, and an unresolved dispatch is never
        # retried blindly, because a second request would spend a second run
        # for the same commit.
        receipt = Publication.read_receipt(self.project.root, args["publication_id"])
        sha = receipt["published_sha"]
        # One request per set of workflows: a reordered retry is the same
        # request and replays its recorded outcome.
        workflows = sorted(str(name) for name in args["workflows"])
        requests = CiRequests(self.project.root)
        identity = ci_request_id(receipt["publication_id"], sha, workflows)
        # A request may cover work items: each covered item waits for the
        # request's result through a receipt-pinned CI wait, and its holder
        # keeps the completion decision.
        covered = self._covered_waits(args.get("covered"), receipt)
        recorded = requests.outcome(identity)
        if recorded is not None:
            # A recorded request replays: the durable outcome is returned
            # whatever became of the covered items, because the run is spent.
            # The recorded coverage governs; a different set is reported.
            coverage = self._coverage_state(self._recorded_coverage(recorded), identity,
                                            register=True)
            # The recorded coverage governs, so only the requested items it
            # does not name are reported as not applied. The comparison is by
            # item identity: the same items in another order are the same
            # coverage, and an item the recorded request covers is never
            # reported as uncovered.
            recorded_ids = {entry["id"] for entry in recorded.get("covered", [])}
            coverage["not_applied"] = [entry["id"] for entry in covered
                                       if entry["id"] not in recorded_ids]
            return {**recorded, "replayed": True, "coverage": coverage}
        if requests.dispatched(identity) is not None:
            raise Denied("An earlier CI request for this publication is unresolved; "
                         "re-observe the CI status before requesting again")
        # The covered items are checked before the provider is called, so a
        # coverage that cannot take the wait never spends a run.
        self._coverage_state(covered, identity, register=False)

        dispatched_at = now()
        covered_records = [entry["record"] for entry in covered]
        covered_ids = [entry["id"] for entry in covered]

        def before_invoke():
            # The dispatch marker is the point of no return: from here the run
            # may have spent a CI run, so the run's own record names the
            # request now, and no later failure can drop it.
            requests.mark_dispatch(identity, {
                "request_id": identity, "publication_id": receipt["publication_id"],
                "sha": sha, "workflows": workflows, "dispatched_at": dispatched_at,
                "covered": covered_records})
            self.record_ci_request(identity, dispatched_at[:10], workflows,
                                   covered_ids, state="unresolved")

        runs = _vcs.request_runs_via(self.config.vcs, sha, workflows,
                                     cancel=self.tools_cancelled, before_invoke=before_invoke)
        record = {"request_id": identity, "publication_id": receipt["publication_id"],
                  "sha": sha, "workflows": workflows, "runs": runs, "requested_at": now(),
                  "covered": covered_records}
        requests.record_outcome(identity, record)
        # The outcome is durable, so the request is resolved. The run's own
        # record names the requests it made, so the CI a unit spent is visible
        # per unit and not only in the project-level listing. This happens
        # before the waits are registered: the run was spent, so the evidence
        # of the request must survive a failure there. The day is the day the
        # request was made, not the day the answer arrived: the provider was
        # asked at the dispatch, and that is the moment a CI run may have
        # been spent.
        self.record_ci_request(identity, dispatched_at[:10], workflows,
                               covered_ids, state="resolved")
        evidence_id, evidence = self._new_invocation_evidence("vcs-request")
        write_json(self.run_dir / evidence,
                   {**record, "trust": "external-untrusted", "evidence_id": evidence_id})
        coverage = self._coverage_state(covered, identity, register=True)
        return {**record, "replayed": False, "coverage": coverage}

    def _covered_waits(self, raw, receipt):
        """Validate the covered work items and the CI waits they carry.

        Each covered item names the revision it is covered at and the checks
        whose result settles it. The wait is pinned to the published receipt,
        so it observes exactly that commit, and the holder of the item keeps
        the completion decision. Bounds: at most 16 items, at most 64 checks
        each, and at most 2048 UTF-8 bytes of covered records.
        """
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 16:
            raise Denied("A CI request covers at most 16 work items")
        if raw and "work_items_transition" not in set(self.role.capabilities):
            raise Denied("Covering work items requires the work-item capability")
        if raw and not (self.config.vcs.get("poll_enabled") and self.config.vcs.get("command")):
            raise Denied("Covering work items requires automatic VCS polling and an adapter")
        from .ci_waits import normalize as normalize_ci_wait
        waits = []
        seen = set()
        for entry in raw:
            item_id = entry["id"]
            if item_id in seen:
                raise Denied("Duplicate covered work item")
            seen.add(item_id)
            request_wait = {"kind": "ci", "publication_id": receipt["publication_id"],
                            "checks": entry["checks"]}
            wait = normalize_ci_wait(self.project.root, request_wait)
            waits.append({"id": item_id, "rev": entry["rev"], "wait": wait,
                          "input": request_wait,
                          "record": {"id": item_id, "rev": entry["rev"], **wait}})
        if len(canonical([entry["record"] for entry in waits])) > MAX_COVERED_BYTES:
            raise Denied("The covered work-item set exceeds 2048 UTF-8 bytes")
        return waits

    def _coverage_state(self, waits, identity, *, register):
        """Report the covered items' state and register the pending waits.

        A covered item that cannot take the CI wait is refused before the
        provider is called, so a bad coverage never spends a run. Once the
        run is spent the state is only reported: a holder that resumed an
        item keeps that decision, and the recorded outcome is still returned.
        """
        registered, waiting, moved, refused = [], [], [], []
        for entry in waits:
            item = self.project.work_items.get(entry["id"])
            if item["status"] == "blocked" and item.get("wait") == entry["wait"]:
                # Already registered: the item's revision moved with the wait.
                waiting.append(entry["id"])
                continue
            if item["status"] in ("done", "dropped") or item["status"] == "blocked":
                # A finished item takes no wait, and a blocked item carries
                # another wait that only its holder may replace.
                refused.append(entry["id"])
                if not register:
                    raise Denied(f"Covered work item {entry['id']} cannot take the CI wait")
                continue
            if item["rev"] != entry["rev"]:
                moved.append(entry["id"])
                if not register:
                    raise Denied(f"Covered work item {entry['id']} changed; "
                                 "re-read it before registering the wait")
                continue
            if register:
                self.project.work_items.transition(
                    entry["id"], expected_rev=entry["rev"], status="blocked",
                    actor=self.role.name, wait=entry["input"],
                    reason=f"Waits for the CI request {identity}")
                registered.append(entry["id"])
        return {"registered": registered, "waiting": waiting,
                "moved": moved, "refused": refused, "not_applied": []}

    def _recorded_coverage(self, recorded):
        """The waits a recorded request covered, rebuilt from its own record."""
        waits = []
        for entry in recorded.get("covered", []):
            wait = {key: entry[key]
                    for key in ("kind", "publication_id", "branch", "sha", "checks")}
            waits.append({"id": entry["id"], "rev": entry["rev"], "wait": wait,
                          "input": {"kind": "ci", "publication_id": wait["publication_id"],
                                    "checks": wait["checks"]}})
        return waits

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
        before_invoke, observe = self._mutation_evidence(
            "vcs-retire", "retire",
            {"branch": args.get("branch"), "expected_sha": args.get("expected_sha")})
        try:
            data = _vcs.retire_via(self.config.vcs, args.get("branch"),
                                   args.get("expected_sha"), cancel=self.tools_cancelled,
                                   before_invoke=before_invoke)
        except OSError as exc:
            raise Denied(f"Branch retirement is unavailable: {exc}") from exc
        record = {"retired": True, "branch": data["branch"], "sha": data["sha"],
                  "classification": data["classification"],
                  "live_refs": data["live_refs"],
                  "trust": "external-untrusted",
                  "result": data}
        return observe(record)

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

            before_invoke, observe = self._mutation_evidence("vcs-dispose", op, pins)

            def reserve_conditional_close():
                # Called only after local grant/pin validation and immediately
                # before adapter dispatch. Failed local input can be corrected;
                # a dispatched attempt can never overwrite or retry its receipt.
                if self.conditional_close_attempted:
                    raise Denied("Conditional proposal close was already attempted in this work unit")
                self.conditional_close_attempted = True
                before_invoke()

            try:
                data = _vcs.close_if_adopted_via(
                    self.config.vcs, pins, cancel=self.tools_cancelled,
                    before_invoke=reserve_conditional_close)
            except OSError as exc:
                raise Denied(f"Conditional proposal close is unavailable: {exc}") from exc
            record = {"disposed": data["closed"], "op": data["op"],
                      **pins, "predicate": data["predicate"],
                      "closed": data["closed"],
                      "trust": "external-untrusted",
                      "adapter_receipt": data["adapter_receipt"]}
            # A valid refusal is durable evidence too: do not refetch,
            # retry, fall through to merge, or treat false as an exception.
            return observe(record)
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
        parameters = {key: value for key, value in args.items() if key != "op"}
        if approval is not None:
            parameters.update(code_digest=code_digest, approval=approval["insight"])
        before_invoke, observe = self._mutation_evidence("vcs-dispose", op, parameters)
        try:
            data = _vcs.dispose_via(self.config.vcs, args["op"], args.get("id"),
                                    args.get("sha"), args.get("base"),
                                    args.get("target"), cancel=self.tools_cancelled,
                                    before_invoke=before_invoke)
        except OSError as exc:
            raise Denied(f"Proposal disposition is unavailable: {exc}") from exc
        record = {"disposed": True, "op": args["op"], "id": data["id"],
                  "sha": data["sha"], "base": data["base"],
                  "target": data["target"], "state": data["state"],
                  "branch": args["branch"],
                  "trust": "external-untrusted",
                  "result": data}
        if approval is not None:
            record["code_digest"] = code_digest
            record["approval"] = approval["insight"]
        return observe(record)

    def _op_materialize(self, args: dict) -> dict:
        # Generic offline-input materialization, one of two exclusive forms
        # sharing the single `materialize` grant (no new capability):
        # `{spec}` invokes the trusted host adapter (warms pre-mounted
        # read-only caches and/or returns digest-bound content plus the
        # policy-checked output), while `{apply: {run, evidence_id}}` writes
        # one retained receipt byte-exactly only when the retained adapter
        # `result.output` equals the retained JSON spec `output` exactly.
        # Invoke needs a visible workspace; apply needs a writable one.
        # The mechanism never mounts adapter paths and never grants network
        # to the sandbox. Fail closed without configuration or grant.
        has_spec = "spec" in args
        has_apply = "apply" in args
        if has_spec == has_apply:
            raise Denied("Supply spec or apply, not both or neither")
        if has_spec:
            if self.role.workspace == "none":
                raise Denied("This role has no workspace")
            spec = args["spec"]
            if not isinstance(spec, str):
                raise Denied("Materialize spec must be a string")
            try:
                data = _materialize.invoke(self.config.materialize, spec,
                                           cancel=self.tools_cancelled)
            except OSError as exc:
                raise Denied(f"Materialization is unavailable: {exc}") from exc
            evidence_id, evidence = self._new_invocation_evidence("materialize")
            record = {"run": self.run_dir.name, "role": self.role.name,
                      "kind": "materialize", "evidence_id": evidence_id,
                      "evidence": evidence, "spec": spec, "digest": data["digest"],
                      "cache": data["cache"], "trust": "external-untrusted",
                      "result": data}
            write_json(self.run_dir / evidence, record)
            return record
        # Apply mode: caller supplies only retained locators; the path and
        # bytes come solely from the retained receipt. Failed validation
        # never touches the destination; success stages an audit receipt.
        if self.role.workspace != "write":
            raise Denied("Materialize apply requires a writable workspace")
        apply = args["apply"]
        if (not isinstance(apply, dict) or set(apply) != {"run", "evidence_id"}
                or not isinstance(apply["run"], str)
                or not isinstance(apply["evidence_id"], str)):
            raise Denied("Materialize apply requires run and evidence_id")
        try:
            source = _materialize.load_source(
                self.config.materialize, self.project.root,
                apply["run"], apply["evidence_id"], self.role.name,
                self.config.limits.file_bytes)
        except OSError as exc:
            raise Denied(f"Materialize apply is unavailable: {exc}") from exc
        path = source["output"]
        if self.project.snapshots.excluded(path):
            raise Denied("Materialize output path is not allowed")
        data = source["content"].encode("utf-8")
        if len(data) > self.config.limits.file_bytes:
            raise Denied("Materialize evidence content exceeds the byte limit")
        safe_write(self.workspace, path, data)
        evidence_id, evidence = self._new_invocation_evidence("materialize")
        record = {"kind": "materialize-apply",
                  "run": self.run_dir.name, "role": self.role.name,
                  "source_run": source["source_run"],
                  "source_evidence": source["source_evidence"],
                  "path": path, "digest": source["digest"],
                  "bytes": len(data), "cache": source["cache"],
                  "trust": "external-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence}
        write_json(self.run_dir / evidence, record)
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
        tokens = args.get("ack_events", [])
        delivered = {event["ack_token"]: event for event in self.decision_events}
        if (not isinstance(tokens, list) or len(tokens) > 512
                or any(not isinstance(token, str) or len(token) > 64 for token in tokens)
                or len(set(tokens)) != len(tokens)
                or any(token not in delivered for token in tokens)):
            raise Denied("ack_events must select unique decision tokens delivered to this role in this unit")
        # Recheck the immutable decision binding, not mutable topic state: the
        # role may have revised/withdrawn a topic as part of handling it.
        for token in tokens:
            if self.project.insights._decision_token(self.role.name, delivered[token]) != token:
                raise Denied("Decision acknowledgement binding changed")
        if args.get('wait_for_check'):
            if 'check_job' not in self.role.capabilities or args['outcome'] != 'wait':
                raise Denied('Check wait requires check_job and outcome=wait')
            job = check_jobs.read(self.project, args['wait_for_check'])
            if self.role.name != job['role']:
                raise Denied('Check wait belongs to another role')
        releases = args.get('release_checks', [])
        if releases:
            if 'check_job' not in self.role.capabilities:
                raise Denied('Releasing checks requires check_job')
            if args.get('wait_for_check') in releases:
                raise Denied('Cannot wait for a released check')
            for job_id in releases:
                if check_jobs.read(self.project, job_id)['role'] != self.role.name:
                    raise Denied('Check release belongs to another role')
        from .routing import check_recommendation
        profile, reason = check_recommendation(args.get("next_profile"), args.get("next_reason"))
        if profile is not None and profile not in self.config.profiles:
            raise Denied("next_profile names no configured profile")
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
            # The final snapshot must be representable. Check it here, while
            # the writer can still remove or ignore the offending file: a
            # capture limit that only surfaced at publication ended the unit
            # without naming the cause.
            try:
                self.project.snapshots.check_bounds(self.workspace)
            except LimitExceeded as exc:
                self.capture_limit = str(exc)[:512]
                raise Denied(self.capture_limit) from exc
            # The workspace is representable again: a recorded cause no longer
            # applies to this role.
            self.capture_limit = None
            self.capture_resolved = True
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
        "exec": _op_exec, "experiment": _op_experiment, "verify": _op_verify, "check_job": _op_check_job,
        "fetch": _op_fetch, "probe": _op_probe, "search": _op_search, "insights": _op_insights,
        "work_items_read": _op_work_items_read,
        "work_items_legacy_read": _op_work_items_legacy_read,
        "work_items_reconcile": _op_work_items_reconcile,
        "work_items_create": _op_work_items_create,
        "work_items_transition": _op_work_items_transition,
        "decide": _op_decide, "submit_insight": _op_submit_insight,
        "revise_insight": _op_revise_insight, "withdraw_insight": _op_withdraw_insight,
        "attach_evidence": _op_attach_evidence,
        "sync": _op_sync, "vcs_read": _op_vcs_read, "vcs_publish": _op_vcs_publish,
        "vcs_request": _op_vcs_request,
        "vcs_retire": _op_vcs_retire, "vcs_dispose": _op_vcs_dispose,
        "materialize": _op_materialize,
        "consult": _op_consult, "delegate": _op_delegate, "report": _op_report,
        "research_read": _op_research_read, "research": _op_research,
        "finish": _op_finish,
    }


def _epoch_text(epoch) -> str:
    """Name an epoch second in UTC, so a bound reads as a time."""
    try:
        return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return str(epoch)



#: Shared per-project poll lock name (state filename lives in ci_observation).
POLL_LOCK_NAME = "vcs-poll.lock"


def now_iso() -> str:
    """Wall-clock event stamp for CI polling."""
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
    diagnostics = list(getattr(context, "decision_receipt_diagnostics", []) or [])
    if not events and not diagnostics:
        return None
    if events and diagnostics:
        return (f"Delivered decision events: {len(events)}. "
                f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "Select each handled row's ack_token in finish.ack_events; omitted rows "
                "remain pending oldest-first, including deferrals. Finishing alone "
                "does not acknowledge decisions. Handling is defined by role policy.")
    if diagnostics:
        return (f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "No deliverable decision events; handling is defined by role policy.")
    return (f"Delivered decision events: {len(events)}. "
            "Select each handled row's ack_token in finish.ack_events; omitted rows "
            "remain pending oldest-first, including deferrals. Finishing alone "
            "does not acknowledge decisions. Handling is defined by role policy.")


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


#: Fixed statement opening every dispatch prompt, full and delta alike.
#: A resumed session's delta is otherwise bare data; without this a model
#: can read it as an acknowledgement of the previous seal ("sealed
#: successfully") instead of a new unit that must do work and finish.
#: Constant text in a fixed first position, so prompt caches stay warm.
UNIT_BEGINS_STATEMENT = (
    "A new work unit begins. This is not an acknowledgement of a previous "
    "seal: do useful work within your role and capabilities, then finish "
    "with mizu_finish and a short state.")

def _prompt_tool_surface(context: Context) -> dict:
    """Names-only broker surface, bounded by this dispatch role's configured grants.

    This is an opening snapshot, not a live grant: narrowed re-dispatches
    prepend their notice to the original prompt later in the same unit.
    """
    return {
        "scope": (
            "UNIT-OPENING host broker tool surface for this work unit. This "
            "supersedes restrictions from earlier work units, not later "
            "restrictions within this same unit. Host tools and enforcement "
            "remain authoritative; this text grants nothing and does not "
            "advertise native engine tools."),
        "tools": [tool["name"] for tool in tool_definitions(context.role.capabilities)],
    }


def _prompt_run_identity(context: Context) -> dict:
    """Bounded run identity for the opening prompt (never fails).

    The run directory name is the run id. ``started_at`` is read back from
    the run's own ``started.json`` when present; prompts built before it is
    written (or with an unreadable record) carry the id alone.
    """
    identity = {"id": context.run_dir.name}
    try:
        started = read_json(context.run_dir / "started.json", {})
    except Exception:
        started = {}
    if isinstance(started, dict) and isinstance(started.get("started_at"), str):
        identity["started_at"] = started["started_at"]
    return identity


def _prompt_capture_limit(context: Context) -> str | None:
    """The capture-limit cause that still applies to this role, if any.

    A workspace file that cannot be captured ends the unit that produced it.
    The cause is set when a capture fails, kept while it applies, and
    cleared when a capture succeeds, so the next unit reads a live cause
    instead of repeating the same capture failure. Bounded to the stored
    health record value.
    """
    try:
        health = read_json(context.project.root / "health" / f"{context.role.name}.json", {})
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(health, dict):
        return None
    cause = health.get("capture_limit")
    return cause if isinstance(cause, str) and cause else None


def prompt_for(context: Context) -> str:
    # The charter body belongs to stable engine system context on every
    # dispatch (including resume); full and delta unit prompts carry its pin.
    caps = set(context.role.capabilities)
    # Mechanism enforces grants, not policy choice: capability-gated context
    # is offered where appropriate, alongside the always-present, bounded
    # project-wide work-item page. `pending_insights` count follows the
    # operator-selected `[limits] pending_insights` bound (newest kept), and
    # `open_insights` states the same bounded backlog with explicit total;
    # `recent_snapshots` follows `[limits] prompt_snapshots`. Exact snapshot
    # references are always pinned. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    pending = context.project.insights.list(limit=context.config.limits.pending_insights) \
        if ("insights" in caps or "decide" in caps) else []
    open_work_items = context.project.work_items.page_open(
        limit=context.config.limits.work_items_prompt)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in caps:
        ci_branch = None
    ci_failures = ci_failures_may_be_truncated = None
    if "vcs_read" in caps:
        ci_failures, ci_failures_may_be_truncated = ci_failures_projection(context.project)
    return json.dumps({"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       "published_snapshot": {**{k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                           "effective_verification": context.project.effective_verification(context.snapshot)},
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "pending_insights": pending,
                       "open_insights": _open_backlog(context, caps),
                       "open_work_items": open_work_items,
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_receipt_diagnostics": list(
                           getattr(context, "decision_receipt_diagnostics", []) or []),
                       **({"work_item_events": list(context.work_item_events)}
                          if getattr(context, "work_item_events", None) else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       **check_jobs.prompt_projection(context),
                       "acceptance_commands": acceptance,
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "ci_branch": ci_branch,
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": _ready_work_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)},
                      ensure_ascii=False)


def prompt_delta_for(context: Context, pending: list) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: the constant ``unit`` opening, then the names-only
    ``unit_tool_surface``, the ``goal_digest`` pin, the
    pinned ``published_snapshot`` and a small ``snapshot_delta``
    (previous/current ids), then only the pending
    proposals not yet offered, an ``open_insights`` statement of the still
    undecided backlog (ids plus total, so an already-offered insight never
    reads as done), the always-present bounded ``open_work_items`` page,
    workspace markers, capability-gated acceptance commands, and a
    capability-gated ``previous_report`` preview for roles holding
    ``report``. The independent bounded ``ready_work`` projection follows
    this stable context in the volatile suffix, before run identity and clock.
    The charter body is supplied separately in stable system instructions;
    ``recent_snapshots`` history is not repeated in deltas. Key order is fixed (stable context
    first, volatile ready-work age, run identity and clock last) so
    repeated prompts preserve as much identical prefix as possible.
    Bounds: pending and the open insight backlog both follow
    ``[limits] pending_insights`` (newest kept; ``truncated`` says so
    explicitly); the work-item page follows ``[limits] work_items_prompt`` and
    includes its overflow count, cursor, truncation, and legacy coverage status;
    the delta carries no workspace file contents and no history
    array, so resumed-unit bytes stay flat as history grows apart from the
    fixed previous-report preview (at most ``report.PREVIOUS_REPORT_BYTES``).
    Trust: local recorded state, which may be untrusted model input.
    Retry/cancellation: local bounded reads; the work-item page scan preserves
    the store's fail-closed errors. ``previous()`` never raises, so an
    unreadable artifact reads as no
    previous edition. Evidence: the dispatching run records
    ``prompt_mode=delta`` and ``prompt_bytes`` in ``prompt_projection.json``.
    Failure: missing non-work-item state follows its existing fallback; the
    fail-closed work-item page reader propagates store errors rather than
    synthesizing an empty inventory.
    """
    caps = set(context.role.capabilities)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_failures = ci_failures_may_be_truncated = None
    if "vcs_read" in caps:
        ci_failures, ci_failures_may_be_truncated = ci_failures_projection(context.project)
    open_backlog = _open_backlog(context, caps)
    open_work_items = context.project.work_items.page_open(
        limit=context.config.limits.work_items_prompt)
    return json.dumps({"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       "published_snapshot": {**{k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                           "effective_verification": context.project.effective_verification(context.snapshot)},
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "pending_insights": pending,
                       "open_insights": open_backlog,
                       "open_work_items": open_work_items,
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_receipt_diagnostics": list(
                           getattr(context, "decision_receipt_diagnostics", []) or []),
                       **({"work_item_events": list(context.work_item_events)}
                          if getattr(context, "work_item_events", None) else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       **check_jobs.prompt_projection(context),
                       "acceptance_commands": acceptance,
                       "ci_branch": ((context.config.vcs.get("ci_branch") or None)
                                     if "vcs_read" in caps else None),
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": _ready_work_projection(context),
                       "run": _prompt_run_identity(context),
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
    instead of admitting the old conversation. No-progress recovery also
    rotates a due persistent session before dispatch, even if its session
    record is missing. Missing-finish recovery likewise rotates a persistent
    session whose consecutive-missing-finish streak reached
    ``[limits] missing_finish_recovery_after`` when the filtered ready-work
    scan still shows actionable (open/doing) work without structured waits;
    a queue with no ready work never rotates. Evidence: rotations write
    ``rotation.json``; dispatches record the mode in ``prompt_projection.json``;
    failed rotations are stored
    in the run ``error.json`` by the Engine caller. Failure: a due rotation removes
    ``session.json`` so the next dispatch mints a fresh provider session
    while the published snapshot and composed policy reload unchanged; a
    missing record otherwise takes the full prompt.
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
    progress = context.snapshot.get("progress")
    recovery_due = no_progress_recovery_due(
        progress, context.snapshot.get("outcome", ""), context.config, context.role.name)
    recovery_reason = (
        f"no-progress units {progress['no_progress_count']} "
        f">={context.config.limits.no_progress_recovery_after}") if recovery_due else None
    missing_recovery_due = False
    missing_reason = None
    try:
        health_record = read_json(context.project.root / "health" / f"{context.role.name}.json", {})
        if isinstance(health_record, dict) and missing_finish_recovery_due(health_record, context.config):
            streak = missing_finish_streak(health_record)
            if _has_ready_work(context.project):
                missing_recovery_due = True
                missing_reason = (
                    f"missing-finish units {streak} "
                    f">={context.config.limits.missing_finish_recovery_after}")
    except Exception:
        missing_recovery_due = False
        missing_reason = None
    if saved is None:
        if recovery_due:
            record = rotate_session(path, run_dir, recovery_reason)
            return prompt_for(context), "full", key, record, session_dir
        if missing_recovery_due:
            record = rotate_session(path, run_dir, missing_reason)
            return prompt_for(context), "full", key, record, session_dir
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
    if recovery_due:
        record = rotate_session(path, run_dir, recovery_reason)
        return prompt_for(context), "full", key, record, session_dir
    if missing_recovery_due:
        record = rotate_session(path, run_dir, missing_reason)
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

    def _dispatch(self, context: Context, driver, prompt: str, **kwargs) -> dict:
        # Retain host exception identity across an adapter's error event. Model
        # text can neither fabricate a local bound nor change its accounting.
        context.admission_error = None
        context.admission_exception = None
        context.admission_wait = False
        try:
            return dispatch_evidence.execute(context, driver, prompt, **kwargs)
        except ModelFailure as exc:
            if isinstance(context.admission_exception, MizuError):
                raise context.admission_exception from exc
            raise

    def _seal_driver_checkpoint(self, context: Context, checkpoint: DriverCheckpoint, *, profile=None,
                                safe_point_yield=False) -> dict:
        """Consume a host-verified boundary for exactly one finish-only query/turn.

        The caller must own the scheduling decision. Never use _execute here:
        its missing-finish and deadline follow-ups would authorize more than one
        model unit.
        The caller retains its workspace/slot ownership and handles publication.
        A seal's outcome is untouched; any failure propagates without retry.
        """
        with context.mutex:
            context.check_deadlines(wrap_up=True)
            profile = profile or context.role.profile
            authority = context.turn_authority_role or context.role
            if (checkpoint is None or context.config.engine(profile) != checkpoint.engine
                    or context.role.workspace != "write" or authority.workspace != "write"
                    or context.delegated_child or context.ephemeral
                    or context.role.engine_tools or authority.engine_tools
                    or "finish" not in context.role.capabilities):
                raise Denied("Checkpoint seal requires a supported driver and finish grant")
            if safe_point_yield:
                safe_point.begin(context, profile)
                # The durable fence precedes the one-shot checkpoint claim. If
                # stop, authority or a deadline wins that race, retire only
                # this exact not-yet-sealed marker and preserve the old path.
                context.safe_point_yield_requested = True
            try:
                context.claim_driver_checkpoint(checkpoint)
            except BaseException:
                if safe_point_yield:
                    safe_point.abandon(context)
                    context.safe_point_yield_requested = False
                raise
            previous_continuation = context.turn_continuation
            context.turn_continuation = True
            context.execution_attempt += 1
            context.hello.clear()
            context.operations_stopped.clear()
            context._checkpoint_launch_pending = True
        try:
            driver = driver_for(context.config, profile, self._drivers)
            result = self._dispatch(context, driver,
                "This is a finish-only seal phase for the completed work unit. "
                "Call mizu_finish exactly once with an accurate outcome, summary and retained state. "
                "Do no further work; no operation may follow the seal.", profile=profile)
            context.check_deadlines(wrap_up=True)
            if context._checkpoint_seal_error is not None:
                raise context._checkpoint_seal_error
            if context.finished is None:
                raise ProtocolError("Checkpoint seal unit ended without finish")
            if not context._checkpoint_settled:
                raise ProtocolError("Checkpoint seal unit did not prove quiescent teardown")
            return result
        except Exception:
            context.check_deadlines(wrap_up=True)
            if context._checkpoint_seal_error is not None:
                raise context._checkpoint_seal_error
            raise
        finally:
            with context.mutex:
                context._checkpoint_launch_pending = False
                context._checkpoint_driver_started = False
                context.operations_stopped.set()
                context.turn_continuation = previous_continuation

    def _maybe_safe_point_yield(self, context: Context, *, profile=None) -> dict | None:
        """Seal once only at a configured unit quantum with live FIFO demand.

        The observation grants no admission or priority: the next invocation
        still uses ordinary FIFO admission. Invalid/contended queue evidence is
        no demand. Drain, deadline, stop and authority paths keep their existing
        transitions and never enter this optional path.
        """
        quantum = context.config.limits.safe_point_yield_quantum
        if (quantum == 0 or context.finished is not None or context.bound_hit is not None
                or context.wrap_up_attempted or context.safe_point_yield_requested
                or context.driver_checkpoint is None
                or context.completed_driver_units % quantum != 0
                or context.host_cancelled() or context.wall_expired()):
            return None
        # Drain remains a separate opt-in transition even when drain_yield is
        # disabled for this role/session. A safe-point yield must not mask it.
        if context.project.control().get("draining"):
            return None
        profile = profile or context.role.profile
        checkpoint = context.driver_checkpoint
        try:
            if context.config.engine(profile) != checkpoint.engine:
                return None
        except (ConfigError, KeyError, TypeError, ValueError):
            return None
        if "finish" not in context.role.capabilities or not has_live_demand(context.config):
            return None
        return self._seal_driver_checkpoint(context, checkpoint, profile=profile,
                                            safe_point_yield=True)

    def _execute(self, context: Context, driver, prompt: str, **kwargs) -> dict:
        """Preserve a durable host seal and recover only normal unsealed returns.

        A normally settled turn without a seal gets the configured continuation
        allowance. With no prior tool calls it retains the work grant; after
        tool activity, follow-ups are finish-only. Provider/protocol faults and
        explicit cancellation keep their existing paths. Deadline wrap-up remains
        separate and bounded.
        Final record, integrity, and publication failures still fail closed.
        A request attempted after the seal short-circuits without calling
        the driver and settles with the sealed outcome (failed settlement
        evidence; the run still publishes the seal).
        """
        try:
            if context.finished is not None:
                raise Denied("No inference after the work unit is sealed")
            context.clear_request_deadlines()
            context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
            try:
                model_result = self._dispatch(context, driver, prompt, **kwargs)
                if context.finished is None:
                    context.enter_drain_wrap_up()
                    if context.bound_hit == "drain" and not context.wrap_up_attempted:
                        context.drain_refusal(context._bound_message())
                yielded = self._maybe_safe_point_yield(context, profile=kwargs.get("profile"))
                if yielded is not None:
                    model_result = yielded
            except DeadlineWrapUp as exc:
                if context.finished is not None:
                    raise
                if context.safe_point_yield_requested:
                    # The one permitted finish-only seal already consumed the
                    # checkpoint. Do not turn its deadline failure into a
                    # second provider call; preserve the workspace fence and
                    # record the ordinary bounded deadline result.
                    raise RunDeadlineExceeded("Safe-point seal reached the deadline without finish") from exc
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
                if context.bound_hit in ("drain", "requests"):
                    context.session_identity_capabilities = authority.capabilities
                try:
                    context.role = dataclasses.replace(authority,
                        capabilities=tuple(c for c in authority.capabilities if c in WRAP_UP_OPS),
                        engine_tools=(), selector="")
                    context.operations_stopped.clear()
                    context.admission_error = None
                    context.admission_exception = None
                    context.clear_request_deadlines()  # The failed transport was reaped, not renewed.
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    context.execution_attempt += 1
                    model_result = self._dispatch(context, driver, context._bound_message() + "\n\n" + prompt, **kwargs)
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
                if context.wrap_up_attempted and context.bound_hit == "requests":
                    raise RequestYieldExceeded("Request wrap-up ended without a seal")
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
                    did_tool_work = bool(context.tool_count or context.engine_tool_invocations)
                    if did_tool_work:
                        followup_prompt = (
                            "The prior turn returned normally without calling mizu_finish. "
                            "This work unit remains open. For the rest of this work unit only: "
                            "do not repeat the work or call any other tool; "
                            "call mizu_finish now with an accurate outcome and summary. No action may follow the seal.")
                        followup_role = dataclasses.replace(base_role, capabilities=("finish",),
                                                            engine_tools=(), selector="")
                    else:
                        followup_prompt = (
                            "The prior turn returned normally without calling mizu_finish or any tool. "
                            "This work unit has not started. Begin the requested work using the original "
                            "goal and state in this session, use your granted tools as needed, and then "
                            "call mizu_finish with an accurate outcome and summary. No action may follow the seal.")
                        followup_role = base_role
                    context.missing_finish_evidence = {
                        "configured_followups": configured, "used_followups": used,
                        "exhausted": False, "sealed": False,
                        "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    context.execution_attempt += 1
                    previous_authority = context.turn_authority_role
                    previous_continuation = context.turn_continuation
                    context.turn_authority_role = authority
                    context.turn_continuation = True
                    context.role = followup_role
                    previous_session_capabilities = context.session_identity_capabilities
                    context.session_identity_capabilities = base_role.capabilities
                    context.operations_stopped.clear()
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    try:
                        model_result = self._dispatch(context, driver, followup_prompt, **kwargs)
                        yielded = self._maybe_safe_point_yield(context, profile=kwargs.get("profile"))
                        if yielded is not None:
                            model_result = yielded
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
               *, wait: bool, enqueue: bool = False, observation_sink=None) -> float:
        """Hold dispatch locks after FIFO admission and return its elapsed time."""
        resources = [role_exclusion(project.root, role_name)]
        if role.workspace == "write":
            resources.append(project.root / "locks" / "workspace.lock")
        # Only fixed configuration is known before admission. A selector's
        # eventual dispatch profile is not a hint about this queue transaction.
        hints = {"profile": None, "engine": None,
                 "profile_engine_reason": "selector_not_resolved_at_admission" if role.selector
                                          else "configured_hint_unavailable"}
        if observation_sink is not None and not role.selector:
            with contextlib.suppress(Exception):
                if isinstance(role.profile, str) and ID.fullmatch(role.profile):
                    hints = {"profile": role.profile, "engine": self.config.engine(role.profile),
                             "profile_engine_reason": None}
        def observe(row):
            observation_sink({**row, **hints})
        admission_kwargs = {
            "stop": self.stop,
            "check": lambda: self._check_admission(project),
        }
        if observation_sink is not None:
            admission_kwargs["observation_sink"] = observe
        elapsed = stack.enter_context(admission(self.config, resources, wait=wait, enqueue=enqueue,
                                                **admission_kwargs))
        if role.workspace == "write":
            safe_point.guard(project.snapshots, role_name)
        return elapsed

    def _selection_retry_wait(self, context: Context, project: Project, stack: contextlib.ExitStack,
                              selection_role: Role, role_name: str, wait_seconds: float) -> None:
        """Bounded retry wait holding no execution slot and spending nothing.

        Mechanism: releases dispatch admission (slot plus role/workspace
        locks), polls at idle_seconds with stop wake, revalidates
        authority/budget without consuming anything, then fail-fast
        re-admits. Waiting never renews deadlines, consumes budget or
        requests, or holds a slot. Budget, stop, wall/deadline or
        re-admission failure abandons the retry; the caller then drops the
        unit through the existing path for the raised error (original
        failure deferred, or the infra/authority/bound error itself).
        """
        stack.close()
        context._slot_lease = False
        deadline = time.monotonic() + max(0.0, wait_seconds)
        idle = self.config.limits.idle_seconds
        while True:
            if self.stop.is_set() or context.host_cancelled():
                raise Cancelled("Run stopped before selection retry")
            if context.wall_expired():
                raise RunDeadlineExceeded("Whole-unit wall deadline exhausted before selection retry")
            self._check_admission(project)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.stop.wait(min(idle, remaining))
        self._admit(project, role_name, selection_role, stack, wait=False,
                    observation_sink=getattr(context, "_admission_observation_sink", None))
        context._slot_lease = True

    def _dispatch_state(self, project, role_name, role, *, event_trigger, continuation=False):
        """Recompute scheduling inputs; a skipped attempt never claims execution.

        Used before queueing and again under dispatch locks. No events or
        observed cursors are acknowledged here, so a concurrent consumer can
        make a queued attempt obsolete without losing pending work.
        """
        snapshot = project.snapshots.get()
        cursor = _role_cursor(project, role_name)
        code_unchanged = role.on_change and _on_change_observed(cursor, snapshot)
        triggers = tuple(getattr(role, "decision_events", ()) or ())
        sources = getattr(role, "decision_sources", "own") or "own"
        if triggers:
            decision_events, receipt_diagnostics = project.insights.decision_events_with_diagnostics(
                role_name, triggers, limit=role_decision_events_limit(project.config, role),
                sources=sources)
        else:
            decision_events, receipt_diagnostics = [], []
        terminal_page = work_item_events.pending(
            project.work_items, role_name, role.work_item_events, sources=role.work_item_sources,
            limit=role_work_item_events_limit(project.config, role))
        terminal_events = terminal_page["events"]
        wait_events = (project.insights.due_waits(role_name, snapshot)
                       + ci_waits.pending(project.work_items, role_name))
        # Dependency obligations addressed to this role ride along when
        # it runs for other reasons; they never admit a run on their
        # own, so no new wake source is implied.
        obligations = project.insights.pending_obligations(role_name)
        # Event-triggered dispatch (platform file triggers) admits only on a
        # subscribed substantive decision/terminal event or a due structured wait
        # for this role. Decision routing uses decision_sources: own-source
        # deferrals ride along but do not admit; decision_sources="any" admits
        # on matching actions including deferrals. Terminal routing is already
        # filtered independently by work_item_sources above. Otherwise
        # it exits before allocating a run directory or model use,
        # like unchanged-code admission. Periodic timers invoke without the
        # flag, so scheduled discovery still runs at its interval.
        if sources == "any":
            substantive_events = [e for e in decision_events if isinstance(e, dict)]
            substantive_diagnostics = [d for d in receipt_diagnostics if isinstance(d, dict)]
        else:
            substantive_events = [e for e in decision_events if e.get("action") != "defer"]
            substantive_diagnostics = [d for d in receipt_diagnostics
                                        if isinstance(d, dict) and d.get("action") != "defer"]
            if not substantive_events and not substantive_diagnostics and "defer" in triggers:
                # A prompt-size window of old deferrals must not suppress a
                # pending substantive trigger outside that window.
                substantive_events, substantive_diagnostics = (
                    project.insights.decision_events_with_diagnostics(
                        role_name, tuple(a for a in triggers if a != "defer"), limit=1, sources=sources))
                substantive_diagnostics = [d for d in substantive_diagnostics
                                            if isinstance(d, dict) and d.get("action") != "defer"]
        try:
            ci_present = _ci_due_for(project, self.config, role)
        except Exception:
            ci_present = []
        try:
            _ci_snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
            ci_failures = _ci_due_for(project, self.config, role, _ci_snapshot_id, time.time())
        except Exception:
            ci_failures = []
        # A check-result subscription is not a request to start unrelated
        # project work. Explicit-only roles may resume their owned checks,
        # but generic ledger readiness belongs to configured autonomous runs.
        autonomous = bool(role.daemon or role.interval_seconds or role.calendar
                          or role.decision_events or role.work_item_events)
        unscheduled = not bool(role.daemon or role.interval_seconds or role.calendar)
        has_code_event = bool(role.on_change and unscheduled and not code_unchanged)
        ready_work_changed = ((not event_trigger or autonomous) and _ready_work_changed(
            project, snapshot if role.workspace == "write" else cursor, role))
        # Only publishers consume snapshot-wide generations. Observers consume
        # operator wakes independently, even when no new code is published.
        wake_base = snapshot if role.workspace == "write" else cursor
        operator_woke = _operator_woke(project.control(), wake_base, observer=role.workspace != "write")
        if event_trigger:
            event_gate = _scheduling.evaluate(_scheduling.DispatchFacts(
                event_trigger=True,
                continuation=continuation,
                on_change=bool(role.on_change),
                code_unchanged=bool(code_unchanged),
                has_code_event=bool(has_code_event),
                operator_woke=operator_woke and role.workspace != "write",
                explicit_wake=operator_woke and role.workspace != "write",
                ready_work_changed=ready_work_changed,
                has_decision=bool(substantive_events or substantive_diagnostics),
                has_work_item=bool(terminal_events),
                has_wait=bool(wait_events or check_jobs.pending(project, role_name)),
                has_ci=bool(ci_failures),
            ))
            if event_gate.skipped == "no-event":
                return {"skip": {"skipped": "no-event", "snapshot": snapshot["id"],
                        "code_digest": snapshot["code_digest"]}}
        # Only publishers consume snapshot-wide goal/inbox/run-wait input.
        goal_changed = role.workspace == "write" and digest(project.goal.encode()) != snapshot["goal_digest"]
        inbox_changed = role.workspace == "write" and project.insights.generation() != snapshot.get("inbox_seen", "")
        _dispatch_now = time.time()
        prior_wait_expired = (snapshot.get("outcome") == "wait"
                              and snapshot.get("wake_at") is not None
                              and _dispatch_now >= snapshot["wake_at"])
        progress_base = snapshot if role.workspace == "write" else cursor
        if role.workspace == "write":
            progress_self_admits = (snapshot.get("outcome") in ("continue", "bounded")
                and snapshot.get("progress") is not None
                and progress_disposition(snapshot["progress"], snapshot["outcome"],
                                         self.config, role_name) != "parked_no_progress")
        else:
            progress_self_admits = _progress_self_admits(progress_base, self.config, role_name)
        explicit_wake = operator_woke or (role.workspace == "write" and (
            goal_changed or inbox_changed or prior_wait_expired))
        _dispatch = _scheduling.evaluate(_scheduling.DispatchFacts(
            event_trigger=bool(event_trigger),
            continuation=continuation,
            on_change=bool(role.on_change),
            code_unchanged=bool(code_unchanged),
            has_code_event=bool(has_code_event),
            explicit_wake=bool(explicit_wake),
            ready_work_changed=ready_work_changed,
            has_decision=bool(substantive_events or substantive_diagnostics),
            has_work_item=bool(terminal_events),
            has_wait=bool(wait_events or check_jobs.pending(project, role_name)),
            has_ci=bool(ci_failures),
            operator_woke=bool(operator_woke and role.workspace != "write"),
            progress_self_admits=bool(progress_self_admits),
        ))
        if _dispatch.skipped == "unchanged":
            return {"skip": {"skipped": "unchanged", "snapshot": snapshot["id"],
                    "code_digest": snapshot["code_digest"]}}
        return {'snapshot': snapshot, 'sources': sources, 'terminal_events': terminal_events, 'terminal_page': terminal_page, 'wait_events': wait_events, 'obligations': obligations, 'ci_present': ci_present, 'ci_failures': ci_failures, 'prior_wait_expired': prior_wait_expired, 'decision_events': decision_events, 'decision_receipt_diagnostics': receipt_diagnostics, '_dispatch': _dispatch}

    def run(self, project: Project, role_name: str, *, attributes=None, retry_busy: bool = False,
                enqueue_busy: bool = False, event_trigger: bool = False, probes_serviced: bool = False) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        from .workspace_cache import maintain
        maintain(project)  # Before probes/admission take any execution locks.
        from .storage import maintain_automatic_prune
        maintain_automatic_prune(project)  # Due run retention; best-effort, never fails the run.
        from .smoke_retention import maintain_automatic_smoke_retention
        maintain_automatic_smoke_retention(project)  # Due shared-smoke sweep; best-effort, never fails the run.
        # Any top-level run services due recovery probes first — daemon,
        # one-shot, or smoke — so block recovery never depends on a resident
        # daemon for the sponsoring role or that role's execution mode. The
        # daemon loop already attempts the same service before its readiness
        # gate and passes probes_serviced=True, so a ready daemon iteration
        # services at most one event per top-level run. Standalone and
        # one-shot runs keep the default attempt. Best-effort: probe failures
        # never fail the run's own work, exactly like a skipped daemon iteration.
        if not probes_serviced:
            try:
                from .probes import service_due as _service_due_probes
                _service_due_probes(self.config, project.name, self.stop)
            except (MizuError, OSError, ValueError):
                pass
        with contextlib.ExitStack() as continuation_owner, contextlib.ExitStack() as stack:
            if "check_job" in role.capabilities:
                hold = check_jobs.deferral(project, role_name)
                if hold is not None:
                    raise Busy("Waiting for sandbox check " + hold["id"]
                               + " until " + _epoch_text(hold["wait_until"])
                               + "; no model slot acquired")
            self._check_admission(project)
            from . import continuations
            intent = (continuations.ready(project.snapshots, role_name)
                      if role.workspace == "write" else None)
            project.insights.ingest_editor(keep_days=project.config.limits.retention_days)
            if (event_trigger or role.on_change) and not (
                    role.workspace == "write" and safe_point.pending_hint(project.snapshots)):
                dispatch_state = self._dispatch_state(project, role_name, role, event_trigger=event_trigger,
                                                      continuation=intent is not None)
                if dispatch_state.get("skip"):
                    return dispatch_state["skip"]
            # Producer admission: hold only configured producer roles before
            # any model or run-slot use. A held attempt claims no run
            # directory, slot, or lock and acknowledges no event or cursor;
            # pending input stays durable until an existing schedule or
            # subscription re-admits the producer. A running unit may finish.
            held = producer_hold(project, role_name, role)
            if held is not None:
                try:
                    current_snapshot = project.snapshots.get()
                    snapshot_id = current_snapshot.get("id")
                    code_digest = current_snapshot.get("code_digest")
                except Exception:
                    snapshot_id, code_digest = None, None
                return {"skip": {"skipped": PRODUCER_HELD_SKIPPED, "snapshot": snapshot_id,
                                 "code_digest": code_digest, "reason": held.get("reason"),
                                 "reasons": list(held.get("reasons", ())),
                                 "source_pending": held.get("source_pending"),
                                 "open_count": held.get("open_count"),
                                 "limits": dict(held.get("limits", {}))}}
            # Bounded run-local evidence, not a scheduling input. Keep the
            # first eight contended admissions, count any further omissions.
            queue_observations = {"version": 1, "observations": [], "omitted": 0}
            started = None
            def record_admission(row):
                if len(queue_observations["observations"]) < MAX_QUEUE_OBSERVATIONS:
                    queue_observations["observations"].append(row)
                else:
                    queue_observations["omitted"] += 1
                if started is not None:
                    started["queue_observations"] = queue_observations
                    write_json(run_dir / "started.json", started)
            continuation = None
            if intent is not None:
                continuation = stack.enter_context(continuations.reservation(
                    self, project, role_name, wait=retry_busy, enqueue=enqueue_busy,
                    observation_sink=record_admission, expected=intent, owner_stack=continuation_owner))
                if continuation is None:
                    return {"skipped": "stale-continuation"}
                admission_elapsed = continuation["admission_elapsed_seconds"]
                recovered = continuations.recover(project.snapshots, continuation)
                if recovered is not None:
                    return recovered
            else:
                admission_elapsed = self._admit(project, role_name, role, stack, wait=retry_busy,
                                               enqueue=enqueue_busy, observation_sink=record_admission)
                # A yield may have committed while this ordinary wake queued.
                # Do not allocate a different identity for the same demand.
                if role.workspace == "write" and continuations.ready(project.snapshots, role_name):
                    return {"skipped": "continuation-pending"}
                if role.workspace == "write" and safe_point.read(project.snapshots) is None:
                    # Admission already owns role/workspace exclusions. Cleanup
                    # takes publication last; ambiguous evidence cannot hold up
                    # this ordinary writer or discard its unsealed workspace.
                    with contextlib.suppress(MizuError, OSError, ValueError):
                        continuations.reconcile_prepared(project.snapshots, role_name)
            recovery = (safe_point.guard(project.snapshots, role_name)
                        if role.workspace == "write" else None)
            recovery_authority = role
            if recovery is not None:
                recovery_profiles = {role.profile}
                if role.selector:
                    recovery_profiles.update(c["profile"]
                        for rule in self.config.selectors[role.selector].get("rules", [])
                        for c in rule.get("candidates", []))
                if (self.ephemeral or "finish" not in role.capabilities or role.engine_tools
                        or recovery["profile"] not in self.config.profiles
                        or recovery["profile"] not in recovery_profiles
                        or self.config.engine(recovery["profile"]) not in ("claude", "codex")):
                    raise Denied("Safe-point recovery requires original writer finish authority")
                role = dataclasses.replace(role, profile=recovery["profile"], selector="", engine_tools=(),
                    capabilities=tuple(c for c in role.capabilities if c in ("files", "read", "diff", "finish")))
            dispatch_state = self._dispatch_state(project, role_name, role, event_trigger=event_trigger,
                                                  continuation=continuation is not None or recovery is not None)
            if dispatch_state.get("skip"):
                return dispatch_state["skip"]
            snapshot = dispatch_state['snapshot']
            sources = dispatch_state['sources']
            terminal_events = dispatch_state['terminal_events']
            terminal_page = dispatch_state['terminal_page']
            wait_events = dispatch_state['wait_events']
            obligations = dispatch_state['obligations']
            ci_present = dispatch_state['ci_present']
            ci_failures = dispatch_state['ci_failures']
            prior_wait_expired = dispatch_state['prior_wait_expired']
            decision_events = dispatch_state['decision_events']
            receipt_diagnostics = dispatch_state.get('decision_receipt_diagnostics', [])
            _dispatch = dispatch_state['_dispatch']
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
                        if (continuation is None and isinstance(_last, dict) and _last.get("fingerprint") == _fingerprint
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
            if continuation is not None:
                captured = project.snapshots.capture_files(project.workspace)
                if captured["skipped"] or captured["code_digest"] != snapshot["code_digest"]:
                    raise Denied("Continuation requires the current published workspace tree")
            active = project.root / "active" / f"{role_name}.json"
            run_id = continuation["successor"] if continuation is not None else uuid.uuid4().hex
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
                if continuation is None:
                    write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                admission = _dispatch.admission
                _ci_wake_branches = effective_ci_wake_branches(self.config)
                if ci_present:
                    try:
                        _ci_head = snapshot["id"] if isinstance(snapshot, dict) else None
                        write_ci_wake(project, role_name, _ci_wake_branches, ci_present, _ci_head, time.time())
                    except Exception:
                        pass
                started = {"run": run_id, "role": role_name,
                            "snapshot": snapshot["id"], "started_at": now(),
                            "admission_elapsed_seconds": round(admission_elapsed, 6),
                            "admission": admission, "decision_events": len(decision_events),
                            "decision_receipt_diagnostics": len(receipt_diagnostics),
                            "work_item_events": len(terminal_events),
                            "wait_events": len(wait_events),
                            "obligation_events": len(obligations),
                            "ci_failures": len(ci_present),
                            "config_sha256": digest(self.config.file.read_bytes()),
                            "policy_sha256": digest(role_policy_bytes(role))}
                if queue_observations["observations"]:
                    started["queue_observations"] = queue_observations
                if continuation is not None:
                    continuations.start(project.snapshots, continuation, started)
                    started["continuation"] = continuations.identity(continuation)
                else:
                    write_json(run_dir / "started.json", started)
                if continuation is not None:
                    write_json(active, {"run": run_id, "role": role_name, "started_at": now()})
                if decision_events:
                    write_json(run_dir / "decision-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "events": decision_events})
                if receipt_diagnostics:
                    write_json(run_dir / "decision-receipt-diagnostics.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                "diagnostics": receipt_diagnostics})
                if terminal_events:
                    write_json(run_dir / "work-item-events.json",
                               {"run": run_id, "role": role_name, "admission": admission,
                                **terminal_page})
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
                                  "next_evaluation_at": decision["next_evaluation_at"],
                                  **({"snapshot": snapshot["id"],
                                      "continuation": continuations.identity(continuation)}
                                     if continuation is not None else {})}
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
                    cleanup(self.config, project.root, role_name, owner=True)
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
                context._admission_observation_sink = record_admission
                context.ephemeral = self.ephemeral
                if recovery is not None:
                    safe_point.recover(project.snapshots, recovery, run_id)
                    context.safe_point_yield_requested = True
                    context.turn_authority_role = recovery_authority
                    context.spend_role = recovery_authority
                context.decision_events = decision_events
                context.decision_receipt_diagnostics = list(receipt_diagnostics or [])
                context.work_item_events = terminal_events
                context.wait_events = wait_events
                context.obligations = obligations
                context.progress_obligations_digest = _progress_obligations_digest(obligations)
                role_cursor = _role_cursor(project, role_name) if role.workspace != "write" else snapshot
                previous_progress = role_cursor.get("progress")
                previous_obligations_digest = None
                if (isinstance(previous_progress, dict) and previous_progress.get("role") == role_name):
                    previous_obligations_digest = previous_progress.get("obligations_digest")
                new_obligation = bool(obligations) and previous_obligations_digest != context.progress_obligations_digest
                context.progress_input = (
                    (role.workspace == "write" and context.inbox_seen != snapshot.get("inbox_seen", ""))
                    or _operator_woke({"wake_generation": context.wake_generation}, role_cursor,
                                      observer=role.workspace != "write")
                    or bool(decision_events or receipt_diagnostics or terminal_events or wait_events or ci_failures)
                    or new_obligation or (role.workspace == "write" and prior_wait_expired))
                prompt, prompt_mode, session_key, rotation, session_dir = session_prompt(context, role, run_dir)
                dispatch_evidence.capture_projection(context, role.profile, prompt_mode,
                                                     session_key, rotation, session_dir)
                decoded = json.loads(prompt)
                work_items_page = decoded["open_work_items"]
                write_json(run_dir / "prompt_projection.json",
                           {"run": run_id, "role": role_name, "profile": role.profile, "snapshot": snapshot["id"],
                            "recent_snapshots": len(decoded.get("recent_snapshots", [])),
                            "pending_insights": len(decoded.get("pending_insights", [])),
                            "open_insights": (decoded.get("open_insights") or {}).get("total", 0),
                            "open_work_items": {
                                "shown": len(work_items_page["items"]),
                                "total": work_items_page["total"],
                                "overflow_count": work_items_page["overflow_count"],
                                "unseeded_decisions": work_items_page["unseeded_decisions"],
                                "coverage_complete": work_items_page["coverage_complete"]},
                            "decision_events": len(decoded.get("decision_events", [])),
                            "decision_receipt_diagnostics": len(decoded.get("decision_receipt_diagnostics", [])),
                            "work_item_events": len(decoded.get("work_item_events", [])),
                            "wait_events": len(decoded.get("wait_events", [])),
                            "acceptance_commands": len(decoded.get("acceptance_commands", [])),
                            "prompt_bytes": len(prompt.encode("utf-8")), "created_at": now(),
                            "prompt_mode": prompt_mode, "session_key": session_key,
                            "rotation": (rotation or {}).get("reason", "")})
                fallback_attempt = 0
                fallback_wait_deadline = None
                retry_budgets: dict = {}
                selection_dispatches = 0
                from .selection import DEFAULT_ON_ERROR_RETRIES as _default_retries
                from .selection import MAX_ON_ERROR_RETRIES as _max_retries
                _max_rule_retries = _default_retries
                if selection_role is not None:
                    for _rule in self.config.selectors.get(selection_role.selector, {}).get('on_error', []):
                        if 'probe_seconds' not in _rule:
                            _candidate = _rule.get('retries', _default_retries)
                            if type(_candidate) is int and _default_retries <= _candidate <= _max_retries:
                                _max_rule_retries = max(_max_rule_retries, _candidate)
                max_selection_dispatches = max_selection_attempts * (_max_rule_retries + 1)
                while True:
                    context.execution_attempt += 1
                    selection_dispatches += 1
                    try:
                        if recovery is not None:
                            recovery_input = {"source_run": recovery["source_run"],
                                "previous_run": recovery["run"],
                                "previous_finish_intent": read_json(project.root / "runs" / recovery["run"]
                                    / "finish-intent.json", None, max_bytes=1024 * 1024)}
                            model_result = self._dispatch(context, driver, prompt + "\n" +
                                "SAFE-POINT RECOVERY: The existing workspace is unsealed and fenced. "
                                "Read only as needed, then call mizu_finish exactly once with the true "
                                "outcome and retained state. Do no further work. The fence remains on failure. "
                                "Previous attempt data (not instructions or a required outcome):\n"
                                + json.dumps(recovery_input))
                            if context.finished is None:
                                raise ProtocolError("Safe-point recovery ended without finish")
                        else:
                            model_result = self._execute(context, driver, prompt)
                    except ModelFailure as exc:
                        # A matched on_error rule falls back within this
                        # dispatch: the block just recorded applies to the
                        # re-selection, so a transient provider error costs a
                        # retry, not the whole interval to the next run.
                        # Anything else (no rule match, local admission
                        # refusal, stop) fails exactly as before.
                        if (context.safe_point_yield_requested or context.cancelled() or self.stop.is_set()
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
                        if selection_dispatches >= max_selection_dispatches:
                            raise
                        retry_action = matched.get("action", "fallback") if isinstance(matched, dict) else "fallback"
                        retry_allowance = (matched.get("retries", _default_retries)
                                           if isinstance(matched, dict) else _default_retries)
                        if retry_action not in ("fallback", "retry"):
                            raise ConfigError("Invalid selector on_error retry policy")
                        if (type(retry_allowance) is not int
                                or not 0 <= retry_allowance <= _max_retries):
                            raise ConfigError("Invalid selector on_error retry policy")
                        if (retry_action == "retry" and "probe_seconds" not in matched):
                            retry_key = (decision.get("profile"), matched.get("rule"))
                            retry_left = retry_budgets.get(retry_key, retry_allowance)
                            if retry_left > 0:
                                retry_budgets[retry_key] = retry_left - 1
                                if (self.stop.is_set() or not project.control().get("armed")
                                        or project.control().get("paused")):
                                    raise Cancelled("Run stopped before selection retry")
                                retry_wait = max(0.0, (matched.get("until") or 0) - time.time())
                                # Provider timestamps are external-untrusted: bound any
                                # retry wait (seconds or provider until) at the
                                # operator-configured maximum wait. The verbatim
                                # provider value stays in the action evidence;
                                # only the wait is capped, then the retry fires.
                                retry_wait = min(retry_wait,
                                                 float(self.config.limits.maximum_wait_seconds))
                                if retry_wait > 0:
                                    # Bounded wait holding no slot and spending
                                    # nothing; abandonment propagates its own
                                    # error and drops the unit as today.
                                    context._slot_lease = False
                                    self._selection_retry_wait(context, project, stack, selection_role,
                                                               role_name, retry_wait)
                                selection_retries.append({"profile": decision["profile"],
                                                          "error": str(exc)})
                                context.operations_stopped.clear()
                                context.verification = None
                                context.commentary = None
                                driver = self.resolve(role.profile)
                                if getattr(driver, "requires_sandbox", True) and any(
                                        c in role.capabilities for c in ("exec", "experiment", "verify")):
                                    container_runtime(self.config)
                                    cleanup(self.config, project.root, role_name, owner=True)
                                continue
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
                        while not reselected.get("profile"):
                            # Only one-shot calls may wait for timed recovery.
                            # Keep one monotonic window across early wakes and
                            # fallback attempts; polling never renews it.
                            unblock_at = reselected.get("next_unblock_at")
                            if (not retry_busy or unblock_at is None
                                    or reselected.get("reason") == "classification_wait"):
                                raise exc
                            if fallback_wait_deadline is None:
                                fallback_wait_deadline = (time.monotonic()
                                                          + self.config.limits.busy_retry_seconds)
                            remaining = fallback_wait_deadline - time.monotonic()
                            wall_now = time.time()
                            if remaining <= 0 or unblock_at - wall_now > remaining:
                                # Preserve the failed decision and original
                                # provider error, with no extra model attempt.
                                raise exc
                            wake_at = min(unblock_at, reselected["next_evaluation_at"])
                            wait_seconds = min(self.config.limits.idle_seconds, remaining,
                                               max(0.0, wake_at - wall_now))
                            context._slot_lease = False
                            self._selection_retry_wait(context, project, stack, selection_role,
                                                       role_name, wait_seconds)
                            if time.monotonic() > fallback_wait_deadline:
                                raise exc
                            # Read-only preparation: a wake must not spend on
                            # classification or launch a recovery probe. Other
                            # executors may have supplied recovery meanwhile.
                            reselected = prepare(self, project, selection_role, snapshot,
                                                 attributes, preview=True)
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
                            cleanup(self.config, project.root, role_name, owner=True)
                        continue
                    except LimitExceeded as exc:
                        # A per-run spend bound with no finish sealed gets one
                        # wrap-up attempt under reduced grants instead of an
                        # error: the daily budget, an unmarked bound, an
                        # already-spent allowance still fail exactly as before.
                        # Sealed driver errors were retained by _execute.
                        if (context.safe_point_yield_requested
                                or isinstance(exc, (InfraExceeded, RunDeadlineExceeded, DrainYieldExceeded))
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
                                cleanup(self.config, project.root, role_name, owner=True)
                            notice = context._bound_message()
                            if context.bound_hit != "drain":
                                notice += (". Seal with finish (recorded as a bounded end). "
                                           "Keep the published state unless this run changed it.")
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
                check_wait = None
                if finished.get("wait_for_check"):
                    # The unit's own wait bound ends the deferral: a check
                    # wait holds the role for as long as the wait it sealed
                    # with, never longer, so it cannot outlive the bound the
                    # operator configured for that wait.
                    check_wait = {"id": finished["wait_for_check"],
                                  "wait_until": time.time() + float(
                                      finished.get("wait_seconds")
                                      or self.config.limits.default_wait_seconds)}
                if role.workspace == "write":
                    captured = project.snapshots.capture_files(workspace)
                    verification = context.verification
                    if verification and (verification["code_digest"] != captured["code_digest"] or captured["skipped"]):
                        verification = {**verification, "passed": False, "invalidated": "code changed or final snapshot is unrepresentable"}
                    if finished["outcome"] == "done" and not (verification and verification["passed"]):
                        raise Denied("Code changed after verification; completion refused")
                    selected_progress = tuple(role_progress_signals(self.config, role))
                    observed_progress = {
                        "tool": context.progress_tool,
                        "code": captured["code_digest"] != context.snapshot["code_digest"],
                        "state": finished["state"] != context.snapshot["state"],
                        "decision": context.progress_decision,
                        "insight": context.progress_insight,
                        "input": context.progress_input,
                    }
                    # Recorded-at-finish ready-work observation: one bounded
                    # single-row existence probe, never a live join in status.
                    # Unreadable ledgers record explicit unknown (None) so the
                    # diagnostic fails closed without blocking publication.
                    try:
                        ready_work_at_finish = bool(
                            project.work_items.page_ready(limit=1, role_name=role_name)["items"])
                    except (Denied, OSError):
                        ready_work_at_finish = None
                    progress = _progress_record(context.snapshot, role_name, selected_progress,
                                               observed_progress, finished["outcome"],
                                               obligations_digest=context.progress_obligations_digest,
                                               ready_work=ready_work_at_finish,
                                               ready_work_signature=getattr(context, "ready_work_signature", None))
                    snapshot = project.snapshots.create(
                        captured, goal=context.goal, state=finished["state"], run=run_id,
                        outcome=finished["outcome"], summary=finished["summary"], verification=verification,
                        wake_at=time.time() + finished["wait_seconds"] if finished["outcome"] == "wait" else None,
                        inbox_seen=context.inbox_seen,
                        wake_generation=context.wake_generation, progress=progress,
                        parent=context.snapshot["id"])
                    if context.safe_point_yield_requested:
                        safe_point.target(project.snapshots, run_id, snapshot["id"])
                    if (context.safe_point_yield_requested and finished["outcome"] == "continue"):
                        continuations.prepare(project.snapshots, role=role_name, source_run=run_id,
                                              snapshot=snapshot["id"])
                if decision:
                    from .selection import State
                    State(self.config).result(decision, run=run_id, attempt=selection_attempt)
                result = {"run": run_id, "role": role_name, "status": "prepared", "finished_at": now(),
                          "finish": finished, "model": model_result, "snapshot": snapshot["id"],
                          "activity": context.activity_summary(),
                          "ci_requests": context.ci_request_summary(),
                          **({"continuation": continuations.identity(continuation)}
                             if continuation is not None else {}),
                          **({"selection": decision} if decision else {})}
                write_json(run_dir / "result.json", result)
                if role.workspace == "write":
                    project.snapshots.publish(snapshot)
                    if context.safe_point_yield_requested:
                        safe_point.guard(project.snapshots, role_name)
                    # Own publication moves the head without new CI facts: refresh
                    # the wake record to the published head so this run does not
                    # re-wake itself; a new sha/check or a later head still wakes.
                    try:
                        _refresh_present = ci_present
                        _refresh_branch = _ci_wake_branches
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
                result["status"] = "waiting" if finished.get("wait_for_check") else "completed"
                if check_wait is not None:
                    result["next_evaluation_at"] = time.time() + self.config.limits.idle_seconds
                    result["waiting_for_check"] = check_wait
                if getattr(context, "wrap_up_attempted", False):
                    result["wrap_up"] = {"bound": context.bound_hit}
                write_json(run_dir / "result.json", result)
                if finished.get("outcome") != "bounded" and not context.bound_hit:
                    released = set(finished.get("release_checks", []))
                    for job_id in released:
                        check_jobs.cancel(context, job_id)
                    with contextlib.suppress(OSError, ValueError, Denied):
                        check_jobs.acknowledge(project, role_name,
                                               set(getattr(context, "check_results_read", set())) | released)
                if not finished.get("wait_for_check"):
                    observed = {"snapshot": snapshot["id"], "code_digest": snapshot["code_digest"]}
                    if role.workspace != "write":
                        state_digest = digest(finished.get("state", "").encode())
                        signals = {
                            "tool": context.progress_tool, "code": False,
                            "state": state_digest != role_cursor.get(
                                "state_digest", digest(context.snapshot["state"].encode())),
                            "decision": context.progress_decision,
                            "insight": context.progress_insight, "input": context.progress_input,
                        }
                        observed.update(
                            wake_generation_digest=_wake_generation_digest(context.wake_generation),
                            state_digest=state_digest, outcome=finished["outcome"],
                            progress=_progress_record(
                                role_cursor, role_name, tuple(role_progress_signals(self.config, role)),
                                signals, finished["outcome"],
                                obligations_digest=context.progress_obligations_digest,
                                ready_work=None,
                                ready_work_signature=getattr(context, "ready_work_signature", None)))
                    write_json(project.root / "observed" / f"{role_name}.json", observed)
                if finished.get("ack_events"):
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        project.insights.acknowledge_decisions(
                            role_name, [e for e in context.decision_events
                                        if e["ack_token"] in finished["ack_events"]],
                            sources=getattr(role, "decision_sources", "own") or "own")
                if context.work_item_events and not finished.get("wait_for_check") and finished.get("outcome") != "bounded" and not context.bound_hit:
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        work_item_events.acknowledge(project.work_items, role_name, context.work_item_events)
                if context.wait_events and not finished.get("wait_for_check"):
                    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                        project.insights.consume_waits(role_name, context.wait_events)
                    if finished.get("outcome") != "bounded" and not context.bound_hit:
                        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, Denied):
                            ci_waits.acknowledge(project.work_items, role_name, context.wait_events)
                if check_wait is not None:
                    check_jobs.set_wait(project, role_name, check_wait["id"], check_wait["wait_until"], continuation={
                        "run": run_id, "snapshot": snapshot["id"], "code_digest": snapshot["code_digest"],
                        "summary": finished["summary"], "state": finished.get("state", "")})
                else:
                    with contextlib.suppress(OSError):
                        check_jobs.clear_wait(project, role_name)
                write_json(project.root / "health" / f"{role_name}.json",
                           {"consecutive_failures": 0, "consecutive_missing_finish": 0,
                            "last_run": run_id, "updated_at": now()})
                return result
            except BaseException as exc:
                if continuation is not None and context is None:
                    # Before model setup, leave only the create-only start
                    # (if any). A failed start must remain retryable.
                    raise
                if (isinstance(exc, (RunDeadlineExceeded, DrainYieldExceeded, RequestYieldExceeded, AuthorityGrantMismatch))
                        and context is not None
                        and context.finished is None and not context.host_cancelled()):
                    result = {"run": run_id, "role": role_name, "status": "bounded",
                              **({"continuation": continuations.identity(continuation)}
                                 if continuation is not None else {}),
                              "bound": ("drain" if isinstance(exc, DrainYieldExceeded)
                                        else ("authority" if isinstance(exc, AuthorityGrantMismatch)
                                              else ("requests" if isinstance(exc, RequestYieldExceeded) else "deadline"))),
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
                        _clear_missing_finish_streak(project, role_name)
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
                    cause = _capture_cause(
                        exc, getattr(context, "capture_limit", None) if context is not None else None)
                    resolved = bool(getattr(context, "capture_resolved", False)) and not cause
                    error = {"run": run_id, "role": role_name,
                             "status": "interrupted", "error": str(exc), "finished_at": now(),
                             **({"capture_limit": cause} if cause else {}),
                             "publication": publication,
                             "durability": "unconfirmed" if isinstance(exc, OSError) or publication == "unknown" or _platform.IS_WINDOWS else "confirmed",
                             **({"activity": context.activity_summary()} if context is not None else {}),
                             **({"ci_requests": context.ci_request_summary()}
                                if context is not None else {}),
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
                                               self.config.limits.max_failures, cause=cause,
                                               resolved=resolved)
                    else:
                        _clear_missing_finish_streak(project, role_name)
                    deferred_cooldown = (role_cooldown_seconds(self.config, role)
                                             if role.workspace == "write"
                                             else self.config.limits.cooldown_seconds)
                    return {"run": run_id, "role": role_name, "status": "deferred",
                            "error": str(exc), "selection": decision, "selection_action": handled,
                            **({"selection_retries": selection_retries} if selection_retries else {}),
                            "next_evaluation_at": time.time() + deferred_cooldown}
                suppress_missing_finish_brake = (isinstance(exc, MissingFinish)
                                                 and not self.config.limits.missing_finish_counts_as_failure)
                if is_deferred(exc, context):
                    _clear_missing_finish_streak(project, role_name)
                elif suppress_missing_finish_brake:
                    _record_missing_finish_only(project, role_name, run_id, exc, cause=cause,
                                                resolved=resolved)
                else:
                    _record_health_failure(project, role_name, run_id, exc,
                                           self.config.limits.max_failures, cause=cause,
                                           resolved=resolved)
                raise exc
            finally:
                if continuation is not None:
                    # Failure leaves a pinned row for recovery. Never turn a
                    # completed execution into an error over pointer cleanup.
                    with contextlib.suppress(MizuError, OSError, ValueError):
                        continuations.settle(project.snapshots, continuation)
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
                ctx.parent_run = parent.run_dir.name
                ctx.dispatch_class = "consult"
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
                          "role": role_name, "status": "bounded" if isinstance(exc, (RunDeadlineExceeded, AuthorityGrantMismatch)) else "interrupted",
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
        # The child is an invocation of its own role, so it takes that role's
        # exclusion for its whole life: a sibling cleanup cannot remove the
        # child's containers while it runs, and a role runs once at a time in
        # a project, delegated children included.
        guard = contextlib.ExitStack()
        try:
            guard.enter_context(lock(role_exclusion(parent.project.root, role_name),
                                     blocking=False))
        except Busy as exc:
            guard.close()
            raise Denied("The delegate role is active; delegation needs the role free") from exc
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
                cleanup(self.config, parent.project.root, role_name, owner=True)
            ctx = Context(self.config, parent.project, role,
                          run_dir, snap, parent.workspace, stop=parent.stop, goal=parent.goal)
            ctx.ephemeral = True
            ctx.parent_run = parent.run_dir.name
            ctx.dispatch_class = "delegate"
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
                    if ctx.bound_hit != "deadline":
                        parent.admission_exception = ctx.admission_exception
            if ctx.host_cancelled() or (ctx.finished is None and ctx.cancelled()):
                raise Cancelled("Delegation cancelled before completion")
            if ctx.finished is None:
                raise Denied("Delegation did not finish")
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role.name, "status": "completed", "finished_at": now(),
                      "profile": profile, "snapshot": snap["id"],
                      "answer": ctx.finished["summary"], "model": model,
                      "activity": ctx.activity_summary(),
                      "ci_requests": ctx.ci_request_summary()}
            result["check_jobs"] = [j for j in check_jobs.list_jobs(parent.project, parent.role.name) if j["run"] == run_dir.name]
            write_json(run_dir / "delegation.json", result)
            record = {"task": task, "role": role.name, "run": run_dir.name,
                      "snapshot": snap["id"], "profile": profile, "answer": ctx.finished["summary"],
                      "check_jobs": result["check_jobs"]}
            write_json(parent.run_dir / "delegations" / f"{uuid.uuid4().hex}.json", record)
            return record
        except Exception as exc:
            result = {"run": run_dir.name, "parent_run": parent.run_dir.name,
                      "role": role_name, "status": "bounded" if isinstance(exc, (RunDeadlineExceeded, AuthorityGrantMismatch)) else "interrupted",
                      "finished_at": now(), "profile": profile, "snapshot": snap["id"], "error": str(exc)}
            if ctx is not None:
                result["activity"] = ctx.activity_summary()
                result["ci_requests"] = ctx.ci_request_summary()
                if hasattr(ctx, "model_evidence"):
                    result["model"] = ctx.model_evidence
            try:
                mkdir(run_dir)
                write_json(run_dir / "error.json", result)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            return result
        finally:
            guard.close()
            if ctx is not None:
                ctx.closed = True


#: Skip label for a held producer admission. The attempt claims no run
#: directory, model slot, or execution lock, and acknowledges no event or
#: cursor; pending input stays durable until an existing schedule or
#: subscription re-admits the producer.
PRODUCER_HELD_SKIPPED = "producer-held"


def _producer_limit(project, name: str) -> int:
    """Read one validated producer/open-work limit, fail-closed.

    Limits are exact integers 0-1073741824 (0 disables); booleans and any
    other type, range breach, or unreadable value raises so the caller
    holds instead of silently disabling the bound.
    """
    value = getattr(project.insights, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Denied(f"Invalid {name}")
    if not 0 <= value <= 1073741824:
        raise Denied(f"Invalid {name}")
    return value


def _producer_raw_limit(project, name: str):
    """Best-effort raw limit for hold diagnostics; None when unreadable."""
    try:
        return getattr(project.insights, name)
    except Exception:
        return None


def producer_hold(project, role_name: str, role=None) -> dict | None:
    """Hold one producer role at a configured congestion threshold, if held.

    Only configured non-writer project roles holding ``submit_insight`` are
    gated; the writable role remains free to resolve, merge, defer, or reject
    input at capacity. Operator/vcs/editor input channels are never role names
    (config refuses them), so they are never held here. Reads only the metrics required by
    enabled limits: the live unresolved input count for this role-name
    source (undecided plus deferred; resolved and archive-only history
    excluded) when the per-source bound is enabled, and the authoritative
    WorkItems open/doing/blocked count when the open-work bound is enabled.
    Disabling a bound never holds for its metric; when both bounds are 0
    the producer is admitted without reading either store. Fail closed when
    a required limit or load cannot be read. A running unit is never
    cancelled; this only holds the next admission. Returns ``None`` when
    admitted, else a bounded hold record with ``reasons``,
    ``source_pending``, ``open_count`` and ``limits``.
    """
    try:
        current = role if role is not None else project.config.roles.get(role_name)
    except Exception:
        current = None
    if current is None:
        return None
    try:
        if role_name not in project.roles:
            return None
        if (getattr(current, "workspace", None) == "write"
                or "submit_insight" not in set(getattr(current, "capabilities", ()) or ())):
            return None
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": None, "open_count": None,
                "limits": {"producer_inputs_per_source": _producer_raw_limit(project, "producer_inputs_per_source"),
                           "open_work_items": _producer_raw_limit(project, "open_work_items")}}
    try:
        per_source_limit = _producer_limit(project, "producer_inputs_per_source")
        open_limit = _producer_limit(project, "open_work_items")
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": None, "open_count": None,
                "limits": {"producer_inputs_per_source": _producer_raw_limit(project, "producer_inputs_per_source"),
                           "open_work_items": _producer_raw_limit(project, "open_work_items")}}
    if not per_source_limit and not open_limit:
        return None
    source_pending = None
    open_count = None
    try:
        if per_source_limit:
            source_pending = project.insights.unresolved_counts([role_name])[role_name]
        if open_limit:
            open_count = project.work_items.count_open_readonly()
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": source_pending, "open_count": open_count,
                "limits": {"producer_inputs_per_source": per_source_limit,
                           "open_work_items": open_limit}}
    decision = _scheduling.evaluate(_scheduling.ProducerFacts(
        is_producer=True, per_source_limit=per_source_limit,
        open_work_limit=open_limit, source_pending=source_pending or 0,
        open_count=open_count or 0, load_ok=True))
    if not decision.held:
        return None
    reasons = tuple(decision.reasons)
    return {"reasons": reasons, "reason": reasons[0] if reasons else "load_unavailable",
            "source_pending": source_pending, "open_count": open_count,
            "limits": {"producer_inputs_per_source": per_source_limit,
                       "open_work_items": open_limit}}


def _role_cursor(project, role_name) -> dict:
    """Read bounded local observation evidence; unsafe/malformed cursors close."""
    try:
        cursor = read_json(project.root / "observed" / f"{role_name}.json", {},
                           max_bytes=OBSERVED_CURSOR_MAX_BYTES)
        return cursor if isinstance(cursor, dict) else {}
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return {}


def _operator_woke(control, cursor, *, observer=False) -> bool:
    raw_generation = control.get("wake_generation", "")
    if not isinstance(raw_generation, str):
        return False
    generation = raw_generation
    if observer:
        # A digest keeps the observer cursor bounded without narrowing the
        # publisher's existing control-generation contract. Malformed local
        # control state is the empty sentinel, never a wake or a crash.
        return (_wake_generation_digest(generation) !=
                cursor.get("wake_generation_digest", _wake_generation_digest("")))
    seen = _normalized_wake_generation(cursor.get("wake_generation", ""))
    return generation != seen


def _progress_self_admits(record, config, role_name, current_time=None) -> bool:
    """Only validated, role-specific continuation evidence can self-admit."""
    if record.get("outcome") not in ("continue", "bounded"):
        return False
    progress = record.get("progress")
    try:
        validate_progress(progress)
    except Denied:
        return False
    if progress["role"] != role_name:
        return False
    return progress_disposition(progress, record["outcome"], config, role_name,
                                current_time) != "parked_no_progress"


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
    """True when a routed decision/terminal event or due wait reaches a role.

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
                if "check_job" in project.config.roles[name].capabilities and check_jobs.pending(project, name):
                    return True
                terminal_triggers = project.config.roles[name].work_item_events
                if terminal_triggers and work_item_events.pending(
                        project.work_items, name, terminal_triggers,
                        sources=project.config.roles[name].work_item_sources, limit=1)["events"]:
                    return True
                triggers = project.config.roles[name].decision_events
                sources = getattr(project.config.roles[name], "decision_sources", "own") or "own"
                if triggers:
                    pending, troubled = project.insights.decision_events_with_diagnostics(
                        name, triggers if sources == "any" else tuple(a for a in triggers if a != "defer"),
                        limit=1, sources=sources)
                    if sources == "any":
                        if any(isinstance(e, dict) for e in pending) or any(
                                isinstance(d, dict) for d in troubled):
                            return True
                    elif (any(isinstance(e, dict) and e.get("action") != "defer" for e in pending)
                          or any(isinstance(d, dict) and d.get("action") != "defer" for d in troubled)):
                        return True
            except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            try:
                if (project.insights.due_waits(name, snapshot)
                        or ci_waits.pending(project.work_items, name, limit=1)):
                    return True
            except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
    except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
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


#: Sentinel for _progress_record callers that carry no ready-work observation.
#: Omission keeps legacy shapes for direct callers; run completion supplies
#: the prompt-bound signature explicitly and produces schema 3.
_READY_WORK_UNSET: object = object()


def _progress_record(snapshot: dict, role_name: str, selected: tuple[str, ...],
                    signals: dict[str, bool], outcome: str, *, obligations_digest: str | None = None,
                    ready_work: bool | None | object = _READY_WORK_UNSET,
                    ready_work_signature: str | None | object = _READY_WORK_UNSET) -> dict:
    """Build compact unit evidence and its bounded consecutive no-progress count.

    ``ready_work`` is the bounded ``page_ready(limit=1)`` existence probe
    taken at writer run completion (True/False) or None when that probe was
    unreadable. An explicit value produces a schema-2 record; omission keeps
    the legacy schema-1 shape for direct callers without an observation.
    An explicit ready_work_signature produces schema 3, binding admission to
    the ready set shown in the unit input rather than the finish-time probe.
    """
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
    if ready_work_signature is not _READY_WORK_UNSET and ready_work is _READY_WORK_UNSET:
        ready_work = None
    if ready_work is _READY_WORK_UNSET:
        record = {"schema": 1, "signals": {name: bool(signals.get(name, False))
                                            for name in PROGRESS_SIGNALS},
                  "selected": list(selected), "role": role_name,
                  "has_progress": has_progress, "no_progress_count": count,
                  "obligations_digest": obligations_digest,
                  "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")}
        return validate_progress(record)
    if ready_work is not None and type(ready_work) is not bool:
        raise Denied("Invalid ready-work observation")
    record = {"schema": 2, "signals": {name: bool(signals.get(name, False))
                                        for name in PROGRESS_SIGNALS},
              "selected": list(selected), "role": role_name,
              "has_progress": has_progress, "no_progress_count": count,
              "obligations_digest": obligations_digest,
              "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
              "ready_work": ready_work}
    if ready_work_signature is not _READY_WORK_UNSET:
        record.update(schema=3, ready_work_signature=ready_work_signature)
    return validate_progress(record)


def should_run(project: Project, snapshot: dict, current_time: float | None = None,
               role_name: str | None = None, config=None) -> bool:
    """Collect daemon signals lazily, following the pure due evaluator's order.

    The evaluator names one next fact at a time. This preserves the old
    short-circuit contract: absent ready work or receipt-pinned CI wait evidence,
    done does not read inbox/events/general CI/insight waits/progress. A higher-priority wake does not probe
    lower-priority stores. Named roles only see role-eligible ready work;
    observers use their own completion cursor, never the writer's progress.
    """
    current_time = time.time() if current_time is None else current_time
    control = project.control()
    effective = config if config is not None else getattr(project, "config", None)
    role = effective.roles.get(role_name) if effective is not None and role_name is not None else None
    observer = role is not None and role.workspace != "write"
    role_record = _role_cursor(project, role_name) if observer else snapshot
    if role_name is not None and check_jobs.waiting(project, role_name):
        return False
    facts = _scheduling.DaemonFacts(
        armed=bool(control.get("armed")),
        paused=bool(control.get("paused")),
        draining=bool(control.get("draining")),
    )
    decision = _scheduling.evaluate(facts)
    if (decision.ready is None and role_name in project.roles and role is not None
            and role.workspace == "write"):
        from .continuations import ready as continuation_ready
        if continuation_ready(project.snapshots, role_name) is not None:
            facts = dataclasses.replace(facts, continuation_ready=True)
            decision = _scheduling.evaluate(facts)
    while decision.ready is None:
        needed = decision.needed
        if needed == "goal_changed":
            value = not observer and digest(project.goal.encode()) != snapshot["goal_digest"]
        elif needed == "operator_woke":
            value = _operator_woke(control, role_record, observer=observer)
        elif needed == "ready_work_changed":
            value = ((role_name is None or role is not None)
                     and _ready_work_changed(project, role_record, role))
        elif needed == "ci_wait_due":
            names = [role_name] if role_name in project.roles else [
                name for name in project.roles
                if project.config.roles[name].workspace == "write"]
            try:
                value = any(check_jobs.pending(project, name) or ci_waits.pending(project.work_items, name, limit=1) for name in names)
            except (Denied, LimitExceeded, OSError, ValueError):
                value = False
        elif needed == "outcome":
            value = role_record.get("outcome", "wait")
            if observer and value not in ("continue", "bounded", "done", "wait", "blocked"):
                value = "wait"
        elif needed == "inbox_changed":
            value = not observer and project.insights.generation() != snapshot.get("inbox_seen", "")
        elif needed == "routed_events_due":
            value = _role_events_due(project, snapshot, role_name)
        elif needed == "ci_due":
            try:
                effective = config if config is not None else getattr(project, "config", None)
                if role_name is not None and not _ci_wake_enabled(effective, role_name):
                    value = False
                elif role_name is not None:
                    _snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
                    value = bool(_ci_due_for(project, effective, role_name, _snapshot_id, current_time))
                else:
                    value = bool(wake_ci_failures(project, effective))
            except Exception:
                value = False
        elif needed == "wait_expired":
            value = (not observer and snapshot.get("outcome") == "wait"
                     and snapshot.get("wake_at") is not None
                     and current_time >= snapshot["wake_at"])
        elif needed == "progress_admits":
            if observer:
                value = _progress_self_admits(role_record, effective, role_name, current_time)
            elif "progress" not in snapshot or role_name is None:
                value = True
            else:
                progress = snapshot["progress"]
                try:
                    validate_progress(progress)
                except Denied:
                    # Snapshots.get rejects malformed persisted records; fail
                    # closed for direct projections rather than treating corrupt
                    # evidence as a legacy snapshot.
                    value = False
                else:
                    effective = config if config is not None else getattr(project, "config", None)
                    disposition = progress_disposition(progress, snapshot["outcome"],
                                                       effective, role_name, current_time)
                    value = disposition != "parked_no_progress"
        else:
            raise RuntimeError(f"scheduler evaluator requested unknown fact: {needed!r}")
        facts = dataclasses.replace(facts, **{needed: value})
        decision = _scheduling.evaluate(facts)
    if not decision.ready:
        return False
    # Producer admission vetoes an otherwise-ready daemon iteration before
    # any model or run-slot use. The check runs only here so an unready
    # daemon never pays for load reads; a held producer simply stays
    # unready until an existing schedule or subscription re-evaluates it.
    # No event or cursor is acknowledged by this veto.
    if role_name is not None and role is not None:
        try:
            if producer_hold(project, role_name, role) is not None:
                return False
        except Exception:
            return False
    return True


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
                        ("last_proposal_poll", state), ("last_ci_wait_poll", state)):
        seen = _wall(shared.get(key))
        if seen is None:
            continue
        current = _wall(target.get(key))
        if current is None or seen > current:
            target[key] = seen
    if isinstance(shared.get("ci_wait_cursor"), str):
        state["ci_wait_cursor"] = shared["ci_wait_cursor"]
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
    ci_state["successes"] = _merge_ci_success_maps(
        ci_state.get("successes"), shared.get("successes"))
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
            waited = ci_waits.poll(config, project, state, now=now, interval=interval)
            if waited is not None:
                events.append(waited)
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
                            "last_ci_wait_poll": state.get("last_ci_wait_poll"),
                            "ci_wait_cursor": state.get("ci_wait_cursor"),
                            "failures": _clean_ci_failures(ci_state.get("failures")),
                            "successes": _clean_ci_successes(ci_state.get("successes"))})
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
                    policy = getattr(role, "daemon_policy", None)
                    if policy not in DAEMON_POLICIES:
                        raise ConfigError("Invalid daemon_policy; reload validated configuration")
                    if policy == "signals":
                        ready = should_run(project, project.snapshots.get(), role_name=role_name)
                    else:
                        ready = (project.control().get("armed") and not project.control().get("paused")
                                 and not project.control().get("draining"))
                    if ready and time.monotonic() >= ordinary_after:
                        result = Engine(config, stop=stop).run(project, role_name, probes_serviced=True,
                                        enqueue_busy=True)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        delay = role_daemon_spacing_seconds(config, role)
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
