"""The execution mechanism: bounded work, immutable inputs and explicit commit points.

There is no research/plan/implement state machine here. The role policy decides
what work is valuable. This module only validates authority and records outcomes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .delegation_jobs import DelegationJobs, _WorkspaceLease
from . import delegation_gates, delivery_settlement, editor, proposal_waits
from . import platform as _platform
from . import run_activity as _run_activity
from . import evidence_records, workspace_reads, lock_diagnostics
from .admission import admission, has_live_demand, role_exclusion
from . import check_jobs, tool_results
from . import ci_waits, dispatch_evidence, safe_point, work_item_events
from .ci_observation import (MAX_CI_CHECKS_PER_BRANCH, MAX_CI_FAILURES,
                             MAX_CI_FAILURE_BRANCHES, MAX_CI_WAKE_BRANCHES,
                             MAX_CI_WAKE_ROWS, POLL_STATE_FILE,
                             _clean_ci_failures, _clean_ci_successes, _valid_ci_run,
                             ci_failures_projection, current_ci_failures,
                             effective_ci_wake_branches,
                             wake_ci_failures)  # preserved runtime re-export seam
# CI-wake helpers keep their established runtime re-export seam.
from .ci_wake import (CI_WAKE_VERSION, DEFAULT_CI_REWAKE_SECONDS,
                      _ci_due, _ci_due_for, _ci_rewake_seconds,
                      _ci_wake_fact_digest, _ci_wake_path,
                      _normalize_ci_wake_branches, read_ci_wake, wake_facts_cover_due, write_ci_wake)
from .budget import Budget
from .config import (DAEMON_POLICIES, MAX_NO_PROGRESS_BACKOFF_SECONDS,
                     Config, Role, load,
                     role_cooldown_seconds, role_daemon_spacing_seconds,
                     role_policy_bytes, role_progress_signals, role_requests_per_run,
                     role_run_seconds, role_tools_per_run,
                     role_wrap_up_requests,
                     role_wrap_up_tools)
from .doctor import container_runtime
from .drivers import DriverCheckpoint, driver_for
from .context_driver import ContextDriverMixin
from .context_kernel_tools import ContextKernelToolsMixin
from .context_evidence import ContextEvidenceMixin
from .context_web_tools import ContextWebToolsMixin
from .context_check_tools import ContextCheckToolsMixin
from .context_read_tools import ContextReadToolsMixin
from .context_role_tools import ContextRoleToolsMixin
from .context_join_tools import ContextJoinToolsMixin
from .context_work_tools import ContextWorkToolsMixin
from .context_grant_read_tools import ContextGrantReadToolsMixin
from .context_vcs_read_tools import ContextVcsReadToolsMixin
from .context_vcs_publish_tools import ContextVcsPublishMixin
from .context_vcs_retire_tools import ContextVcsRetireMixin
from .engine_admission import EngineAdmissionMixin
from .engine_dispatch import EngineDispatchMixin
from .engine_delegation import (EngineDelegationMixin, CONSULT_FORBIDDEN,
                               DELEGATE_FORBIDDEN, check_consult_role,
                               check_delegate_role)
from .engine_collaboration import EngineCollaborationMixin
from .engine_execution import EngineExecutionMixin
from .engine_run import EngineRunMixin, _bound_started_evidence, _epoch_text
from .errors import RequestYieldExceeded, ToolYieldExceeded
from .errors import (AuthorityGrantMismatch, Busy, Cancelled, CaptureLimitExceeded,
                      ConfigError, Denied, InfraExceeded, LimitExceeded, RunDeadlineExceeded,
                      DeadlineWrapUp, DrainYieldExceeded, MissingFinish, MizuError, ModelFailure,
                      ProtocolError)
from .fs import (canonical, digest, loads_finite, lock, mkdir, now, read_json, relative_parts,
                   safe_read, safe_write, sync_dir, write_json, PREVIEW_BYTES, READ_PAGE_CHARS,
                   text_preview, ID)
from .project import Project
from .readiness_witness import settled as witness_settled, witness as state_witness
from .snapshot import (MAX_MISSING_FINISH_STREAK,
                       missing_finish_recovery_due, missing_finish_streak,
                       no_progress_recovery_due)
from .run_health import (_capture_cause, _clear_missing_finish_streak,
                         _record_health_failure, _record_missing_finish_only)
from .protocol import DEFINITIONS, operation_allowed, tool_definitions, validate
from .report import publish
from .sandbox import Sandbox, cleanup
from . import vcs as _vcs
from .ci_requests import CiRequests, request_id as ci_request_id
# source_commit keeps its established runtime re-export seam.
from .publication import Publication, source_commit
from .web import Web
from .role_state_ops import _reject_constant  # preserve the established runtime seam
# Ready-work observation keeps its established runtime import seam.
from .work_readiness import (_has_ready_work, _ready_work_changed,
                             _ready_work_observation, _writer_wait_liveness,
                             _writer_wait_sources)
from .vcs_poll_policy import (MAX_CI_BRANCHES, POLL_CALL_TIMEOUT_CAP,
                              POLL_FETCH_TIMEOUT_CAP, _vcs_poll_number,
                              capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_enabled, vcs_poll_fetch_timeout,
                              vcs_poll_interval, vcs_poll_max_branches,
                              vcs_poll_proposals_enabled, vcs_poll_status_timeout)
from .ci_polling import (_merge_ci_success_maps, _merge_ci_success_rows,
                         _prune_ci_branch_map, _live_ci_branches, poll_ci)
from .vcs_proposal_poll import _valid_proposal_cursor, poll_proposals
from .vcs_poll_state import _refresh_ci_cache, refresh_poll_state
from .vcs_upstream_poll import poll_upstream, refresh_upstream
# Keep the existing runtime imports available to callers.
from .context_lifecycle import (ContextLifecycleMixin, WRAP_UP_OPS)
from .unit_finish import (ContextFinishMixin, _READY_WORK_UNSET,
                          _progress_record, _writer_next_task)
from .unit_prompts import (MAX_READY_WORK_AGE_SECONDS,
                            _prompt_capture_limit,
                            _prompt_clock, _prompt_research_state, _prompt_run_identity,
                            _prompt_tool_surface, _ready_work_age,
                            _ready_work_projection,  # preserved unit_prompts re-export seam
                            prompt_delta_for, prompt_for, session_prompt)
# Wake evidence keeps its established runtime re-export seam.
from .wake_evidence import (OBSERVED_CURSOR_MAX_BYTES,
                            _normalized_wake_generation, _operator_woke,
                            _role_cursor, _wake_generation_digest)
# Role-admission facts keep their established runtime re-export seam.
from .role_admission import (_on_change_observed, _progress_self_admits,
                             _receipt_wait_due, _reconcile_work_item_waits,
                             _role_events_due, should_run)


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

#: Fixed memory bounds for run-local pre-send admission identities.
MAX_ENGINE_TOOL_INVOCATIONS = 4096
MAX_ENGINE_TOOL_ID = 256
MAX_READ_IDENTITIES = workspace_reads.MAX_READ_IDENTITIES
#: Bounded serialized size of the work items a CI request covers.
MAX_COVERED_BYTES = 2048

#: Re-exported for compatibility; evidence_records owns the exact bounds.
EVIDENCE_UUID = evidence_records.EVIDENCE_UUID
MAX_STARTED_EVIDENCE_BYTES = evidence_records.MAX_STARTED_EVIDENCE_BYTES


#: Default wrap-up allowance granted when a per-run spend bound is reached:
#: extra model requests and extra wrap-up tool calls the unit may still spend
#: to seal a clean `bounded` end instead of failing. Policy defaults kept here
#: for compatibility; the effective values come from `[limits] wrap_up_requests`
#: / `wrap_up_tools` with optional per-role overrides. The daily budget still
#: applies to every admitted request.
WRAP_UP_REQUESTS = 3
WRAP_UP_TOOLS = 5
#: Reads of existing state are useful observation, not harness-observable progress.
OBSERVATIONAL_OPS = frozenset({
    "files", "read", "diff", "research_read", "vcs_read",
    "command_evidence_read", "experiment_evidence_read", "work_items_read",
    "queue_read", "message_read", "message_refs",
    "result_read", "ready_work_read", "work_order_read", "report_read",
})
REPLAYABLE_READ_ARGUMENTS = {"fetch": "receipt_id", "probe": "receipt_id", "search": "result_id"}


def _is_observational_operation(operation: str, arguments: dict, result=None) -> bool:
    """Identify pure reads of existing data, which do not reset progress."""
    if operation == "check_job":
        if arguments.get("action") in ("list", "logs"):
            return True
        if arguments.get("action") == "result":
            return not (isinstance(result, dict) and result.get("status") in check_jobs.TERMINAL_WAKE_STATUSES)
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


@dataclasses.dataclass
class _SpendLedger:
    """One run's aggregate spend; Context admission holds lock through debit.

    Daily request admission runs under the same lock, before any debit, so a
    refusal leaves both ordinary and wrap-up capacity available to the caller.
    Deadlines, invocation identities and refusal state remain Context-local.
    """
    lock: object = dataclasses.field(default_factory=threading.RLock)
    request_count: int = 0
    tool_count: int = 0
    wrap_up_requests: int = 0
    wrap_up_tools: int = 0
    delegated_wrap_up_requests: int = 0
    delegated_wrap_up_tools: int = 0


def _spend_counter(name):
    # Preserve Context's counter interface for records and driver adapters.
    # Read/modify/write admission sequences additionally hold the ledger lock.
    def get(self):
        with self._spend.lock:
            return getattr(self._spend, name)

    def set(self, value):
        with self._spend.lock:
            setattr(self._spend, name, value)

    return property(get, set)


class Context(ContextFinishMixin, ContextDriverMixin, ContextLifecycleMixin,
                ContextKernelToolsMixin,
                ContextEvidenceMixin, ContextWebToolsMixin,
                ContextCheckToolsMixin, ContextReadToolsMixin,
                ContextRoleToolsMixin, ContextJoinToolsMixin,
                ContextWorkToolsMixin, ContextGrantReadToolsMixin,
                ContextVcsReadToolsMixin, ContextVcsPublishMixin,
                ContextVcsRetireMixin):
    request_count = _spend_counter("request_count")
    tool_count = _spend_counter("tool_count")
    wrap_up_requests = _spend_counter("wrap_up_requests")
    wrap_up_tools = _spend_counter("wrap_up_tools")
    delegated_wrap_up_requests = _spend_counter("delegated_wrap_up_requests")
    delegated_wrap_up_tools = _spend_counter("delegated_wrap_up_tools")

    def __init__(self, config: Config, project: Project, role: Role, run_dir: Path,
                 snapshot: dict, workspace: Path, *, stop: threading.Event | None = None,
                 consult=None, delegate=None, goal: str | None = None,
                 reservation_view=None):
        # Trusted host injection only; never populated from model arguments.
        self._lock_diagnostics = lock_diagnostics.capture()
        self._reservation_view = reservation_view
        self._workspace_lease = None
        self.config, self.project, self.role = config, project, role
        self.run_dir, self.snapshot, self.workspace = run_dir, snapshot, workspace
        self.goal = (project.goal if role.workspace == "write" else snapshot["goal"]) if goal is None else goal
        self.goal_digest = digest(self.goal.encode())
        # Normalized cause selected by dispatch; exposed to the unit prompt only.
        self.admission: str | None = None
        self.external_check_wake_facts = []
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
        self._spend = _SpendLedger()
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
        # IDs returned by _check_steer whose durable submission the adapter has
        # not confirmed yet. Replaced per notification batch, never accumulated.
        self._check_steer_pending: set[str] = set()
        # Adapter pre-send identities; never supplied by model tool arguments.
        self.engine_tool_invocations: dict[tuple[int, str], str] = {}
        # One conditional-close adapter dispatch per work unit prevents a
        # later conditional result from replacing its retained receipt.
        self.conditional_close_attempted = False
        self.progress_tool = False
        self.progress_message = False
        self.progress_input = False
        # Delegation spend sharing: the role whose per-run caps bind spend.
        # Delegation children count against the caller's unit, so theirs
        # points at the caller; everyone else spends their own role.
        self.spend_role = None
        # Per-run wrap-up: which bound was reached plus the spent allowance.
        # Set by a requests/tools refusal or the reserved wall window, so the
        # unit can seal `bounded` instead of failing.
        self.bound_hit: str | None = None
        # Delegated children use an aggregate share of these same wrap-up
        # allowances; the top-level context retains all unused capacity.
        self.delegated_child = False
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
        # Pending host tool refusal for this adapter invocation only. Successful
        # admission/work clears it; provider text never creates this evidence.
        self.tool_admission_exception: LimitExceeded | None = None
        self.read_content_ids: dict[str, str] = {}
        self.admission_wait = False
        # Selector failover identity for durable submissions. Retries and
        # recovery of this candidate keep the same ordinal; only a proven
        # terminal pi-durable unanswered settlement can advance it.
        self.attempt_ordinal = 0
        self.ci_wait_manifest = ()
        self.proposal_wait_manifest = ()
        #: Queue facts stated in this unit's opening (queue_facts.opening).
        self.queues: list = []
        self.work_item_events: list = []
        self._work_order_generation = False
        self._work_order_inputs = None
        self.wait_events: list = []
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
        self.exec_session = None
        self.sandbox = Sandbox(config, project.root, role.name, run_dir, cancel=self.tools_cancelled)
        self.web = Web(config.web, config.data / "web-cache", run_dir / "sources", role.name)

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
        with self.config.runtime_policy(), self.mutex, lock_diagnostics.bind(self._lock_diagnostics):
            delegation_gates.check_access(self._reservation_view, self.role.name,
                                          self.snapshot, operation, arguments)
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
            if operation == "_check_steer":
                # Host-owned bridge operation for the managed pi-durable lane.
                # It is not a model capability and never exposes job output:
                # only the bounded, host-written notification reaches the
                # running unit. The automatic wake stays available until the
                # adapter confirms durable submission with the separate
                # acknowledgement; unread output still needs action=result and
                # the normal successful-finish acknowledgement.
                if arguments != {}:
                    raise Denied("Unsupported check steer request")
                seen = getattr(self, 'check_steer_seen', ())
                jobs = check_jobs.steer_pending(self.project, self.role.name, seen)
                messages = [{'id': job['id'], 'message': check_jobs.steer_message(job)}
                            for job in jobs[:check_jobs.MAX_CHECK_STEER_NOTIFICATIONS]]
                ids = [message['id'] for message in messages]
                self.check_steer_seen = frozenset(set(seen) | set(ids))
                self._check_steer_pending = set(ids)
                return {'messages': messages}
            if operation == "_check_steer_delivered":
                # Host-owned second phase: consume a wake only after the
                # adapter has durably submitted the notification. Never reads
                # job output or touches result/usage retrieval.
                ids = arguments.get("ids")
                if (set(arguments) != {"ids"} or not isinstance(ids, list) or not ids
                        or len(ids) > check_jobs.MAX_CHECK_STEER_NOTIFICATIONS
                        or any(not isinstance(item, str) or item not in self._check_steer_pending
                               for item in ids)):
                    raise Denied("Invalid check steer delivery")
                check_jobs.mark_wake_delivered(self.project, self.role.name, ids, self.run_dir.name)
                self._check_steer_pending.difference_update(ids)
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
                    self.tool_admission_exception = None
                    return {"allowed": True}
                if len(self.engine_tool_invocations) >= MAX_ENGINE_TOOL_INVOCATIONS:
                    raise LimitExceeded("Engine tool invocation evidence bound exceeded")
                with self._spend.lock:
                    self.tool_count += 1
                    if self.tool_count > role_tools_per_run(self.config, self.spend_role or self.role):
                        # Native tools are never wrap-up operations: record the
                        # bound and refuse without drawing the allowance.
                        self.bound_hit = self.bound_hit or "tools"
                        self.tool_admission_exception = LimitExceeded("Per-run tool limit exceeded")
                        raise self.tool_admission_exception
                    self.tool_admission_exception = None
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
                    with self._spend.lock:
                        if sequence_key not in self.request_sequences:
                            over = self.bound_hit in ("deadline", "drain") or self.request_count >= role_requests_per_run(
                                self.config, self.spend_role or self.role)
                            if (over and self.bound_hit == "deadline"
                                    and not self.wrap_up_attempted):
                                # The reserved allowance belongs to Engine's
                                # reduced-grant follow-up, never the still-active
                                # normal invocation that crossed the cutoff.
                                raise DeadlineWrapUp(self._bound_message())
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
                                       self.config.limits.shared_daily_requests, self.config.timezone,
                                       day_file_bytes=self.config.limits.budget_day_file_bytes).take(
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
                    self.tool_admission_exception = None
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
            if not operation_allowed(operation, self.role.capabilities):
                raise Denied(f"Role has no capability: {operation}")
            if self.bound_hit is not None and operation not in WRAP_UP_OPS:
                raise Denied(self._bound_message())
            with self._spend.lock:
                self.tool_count += 1
                if self.bound_hit in ("deadline", "drain") or self.tool_count > role_tools_per_run(self.config, self.spend_role or self.role):
                    self.bound_hit = self.bound_hit or "tools"
                    tools_used, tools_limit = self._wrap_up_used_limit("tools")
                    if operation not in WRAP_UP_OPS or tools_used >= tools_limit:
                        refusal = LimitExceeded("Per-run tool limit exceeded")
                        if tools_used >= tools_limit:
                            if self.bound_hit == "drain":
                                refusal = DrainYieldExceeded("Drain wrap-up tool allowance exhausted")
                            elif self.bound_hit == "deadline":
                                refusal = RunDeadlineExceeded("Deadline wrap-up tool allowance exhausted")
                            elif self.wrap_up_attempted:
                                if self.bound_hit == "requests":
                                    refusal = RequestYieldExceeded("Request wrap-up tool allowance exhausted")
                                else:
                                    refusal = ToolYieldExceeded("Tool wrap-up allowance exhausted")
                        self.tool_admission_exception = refusal
                        raise refusal
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
                idempotent_replay = (operation == "work_items_create"
                                     and isinstance(result, dict)
                                     and result.get("created") is False)
                if (operation != "finish" and not _is_observational_operation(operation, arguments, result)
                        and not idempotent_replay):
                    self.progress_tool = True
                if operation == "queue_write":
                    self.progress_message = True
                self.tool_admission_exception = None
                return tool_results.bounded(self, operation, arguments, result)
            finally:
                # Completion of actual admitted work is not a heartbeat and
                # never resets an active request (Claude query / Codex turn).
                self.transport_idle_deadline = time.monotonic() + self.config.limits.model_request_seconds
                self._activity_change(lambda: setattr(self, "_activity_operation", None))
                self.operation_active.clear()

    def dispatch(self, op: str, args: dict) -> dict:
        # Direct host dispatch shares the handoff boundary with handle().
        with self.mutex:
            delegation_gates.check_access(self._reservation_view, self.role.name,
                                          self.snapshot, op, args)
            handler = self._DISPATCH.get(op)
            if handler is None:
                raise Denied("Unknown operation")
            return handler(self, args)

    def _observed_diff_base(self) -> str | None:
        return workspace_reads.observed_diff_base(
            self, max_bytes=OBSERVED_CURSOR_MAX_BYTES)

    def _injected_ref_paths(self) -> list[str]:
        return workspace_reads.injected_ref_paths(self)

    def _op_read(self, args: dict) -> dict:
        return workspace_reads.op_read(
            self, args, max_identities=MAX_READ_IDENTITIES)


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
        attempt = args.get("attempt", 1)
        requests = CiRequests(self.project.root, self.config.ci_requests)
        identity = ci_request_id(receipt["publication_id"], sha, workflows, attempt)
        # A request may cover work items: each covered item waits for the
        # request's result through a receipt-pinned CI wait, and its holder
        # keeps the completion decision.
        covered = self._covered_waits(args.get("covered"), receipt)

        def retain_response(marker, response):
            not_dispatched = isinstance(response, dict)
            record = {key: value for key, value in marker.items() if key != "dispatched_at"}
            record.update(runs=[] if not_dispatched else response, requested_at=now())
            if not_dispatched:
                record.update(outcome="not_dispatched", reason_code=response["reason_code"])
                if "diagnostic" in response:
                    record["diagnostic"] = response["diagnostic"]
            requests.resolve(identity, record)
            self.record_ci_request(
                identity, marker["dispatched_at"][:10], marker["workflows"],
                [entry["id"] for entry in marker.get("covered", [])],
                state="not_dispatched" if not_dispatched else "resolved",
                attempt=marker.get("attempt", 1), reason_code=record.get("reason_code"),
                possibly_duplicate_spend=marker.get("possibly_duplicate_spend", False))
            evidence_id, evidence = self._new_invocation_evidence("vcs-request")
            write_json(self.run_dir / evidence,
                       {**record, "trust": "external-untrusted", "evidence_id": evidence_id})
            return record

        recorded = requests.outcome(identity)
        marker = requests.dispatched(identity)
        if recorded is None and marker is not None:
            # Re-observe this exact attempt; this operation cannot dispatch.
            response = _vcs.reconcile_runs_via(
                self.config.vcs, marker["sha"], marker["workflows"],
                request_id=identity, attempt=marker.get("attempt", 1),
                cancel=self.tools_cancelled)
            recorded = retain_response(marker, response)
        if recorded is not None:
            if recorded.get("outcome") == "not_dispatched":
                coverage = {"registered": [], "waiting": [], "moved": [], "refused": [],
                            "not_applied": [entry["id"] for entry in covered]}
            else:
                coverage = self._coverage_state(self._recorded_coverage(recorded), identity,
                                                register=True)
                recorded_ids = {entry["id"] for entry in recorded.get("covered", [])}
                coverage["not_applied"] = [entry["id"] for entry in covered
                                           if entry["id"] not in recorded_ids]
            return {**recorded, "replayed": True, "coverage": coverage}
        if attempt > 1:
            previous_id = ci_request_id(receipt["publication_id"], sha, workflows, attempt - 1)
            previous = requests.outcome(previous_id)
            if previous is None or previous.get("outcome") != "not_dispatched":
                raise Denied("A new CI attempt requires the preceding attempt's durable "
                             "no-dispatch outcome")
        # Retry permission above is independent of spend in the rest of a
        # legacy chain. Only a fully validated no-spend history clears risk.
        possibly_duplicate_spend = requests.prior_spend_possible(
            receipt["publication_id"], sha, workflows, attempt)
        # The covered items are checked before the provider is called, so a
        # coverage that cannot take the wait never spends a run.
        self._coverage_state(covered, identity, register=False)

        dispatched_at = now()
        covered_records = [entry["record"] for entry in covered]
        covered_ids = [entry["id"] for entry in covered]

        marker = {"request_id": identity, "publication_id": receipt["publication_id"],
                  "sha": sha, "workflows": workflows, "dispatched_at": dispatched_at,
                  "attempt": attempt, "covered": covered_records}
        if possibly_duplicate_spend:
            marker["possibly_duplicate_spend"] = True

        def before_invoke():
            # The dispatch marker is the point of no return: from here the run
            # may have spent a CI run, so the run's own record names the
            # request now, and no later failure can drop it.
            requests.mark_dispatch(identity, marker)
            self.record_ci_request(identity, dispatched_at[:10], workflows,
                                   covered_ids, state="unresolved", attempt=attempt,
                                   possibly_duplicate_spend=possibly_duplicate_spend)

        response = _vcs.request_runs_via(
            self.config.vcs, sha, workflows, request_id=identity, attempt=attempt,
            cancel=self.tools_cancelled, before_invoke=before_invoke)
        not_dispatched = isinstance(response, dict)
        record = retain_response(marker, response)
        if not_dispatched:
            coverage = {"registered": [], "waiting": [], "moved": [], "refused": [],
                        "not_applied": covered_ids}
        else:
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
            self.close_exec()
            captured = self.project.snapshots.capture_files(self.workspace)
            if captured.get("skipped"):
                raise Denied("Proposal disposition requires a representable snapshot")
            code_digest = captured["code_digest"]
            approval = _vcs.require_go_approval(
                self.project, args["branch"], code_digest,
                endpoints={"target": args.get("target"), "id": args.get("id"),
                           "sha": args.get("sha"), "base": args.get("base")},
                queue=self.config.vcs.get("approval_queue"))
        parameters = {key: value for key, value in args.items() if key != "op"}
        if approval is not None:
            parameters.update(code_digest=code_digest, approval=approval["approval"])
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
            record["approval"] = approval["approval"]
        return observe(record)

    def _op_report(self, args: dict) -> dict:
        self.commentary = args
        write_json(self.run_dir / "commentary.json", args)
        return {"staged": True}

    def _writer_wait_liveness(self, *, releases=()):
        return _writer_wait_liveness(self.project, self.role, releases=releases)

    _DISPATCH = {
        "diff": ContextReadToolsMixin._op_diff, "files": ContextReadToolsMixin._op_files, "read": _op_read, "result_read": ContextReadToolsMixin._op_result_read,
        "command_evidence_read": ContextEvidenceMixin._op_command_evidence_read,
        "experiment_evidence_read": ContextEvidenceMixin._op_experiment_evidence_read,
        "exec": ContextReadToolsMixin._op_exec, "experiment": ContextReadToolsMixin._op_experiment,
        "verify": ContextCheckToolsMixin._op_verify, "check_job": ContextCheckToolsMixin._op_check_job,
        "fetch": ContextWebToolsMixin._op_fetch, "probe": ContextWebToolsMixin._op_probe, "search": ContextWebToolsMixin._op_search, "feed_updates": ContextWebToolsMixin._op_feed_updates,
        "ready_work_read": ContextGrantReadToolsMixin._op_ready_work_read,
        "work_order_read": ContextGrantReadToolsMixin._op_work_order_read,
        "report_read": ContextGrantReadToolsMixin._op_report_read,
        "work_items_read": ContextWorkToolsMixin._op_work_items_read,
        "work_items_create": ContextWorkToolsMixin._op_work_items_create,
        "work_items_transition": ContextWorkToolsMixin._op_work_items_transition,
        "work_items_reopen": ContextWorkToolsMixin._op_work_items_reopen,
        "work_order": ContextRoleToolsMixin._op_work_order,
        "queue_write": ContextKernelToolsMixin._op_queue_write,
        "queue_read": ContextKernelToolsMixin._op_queue_read,
        "queue_ack": ContextKernelToolsMixin._op_queue_ack,
        "message_read": ContextKernelToolsMixin._op_message_read,
        "message_refs": ContextKernelToolsMixin._op_message_refs,
        "sync": _op_sync, "vcs_read": ContextVcsReadToolsMixin._op_vcs_read,
        "vcs_publish": ContextVcsPublishMixin._op_vcs_publish,
        "vcs_request": _op_vcs_request,
        "vcs_retire": ContextVcsRetireMixin._op_vcs_retire,
        "vcs_dispose": _op_vcs_dispose,
        "materialize": ContextRoleToolsMixin._op_materialize,
        "consult": ContextJoinToolsMixin._op_consult, "delegate": ContextJoinToolsMixin._op_delegate, "report": _op_report,
        "research_read": ContextRoleToolsMixin._op_research_read, "research": ContextRoleToolsMixin._op_research,
        "finish": ContextFinishMixin._op_finish,
    }


#: Shared per-project poll lock name (state filename lives in ci_observation).
POLL_LOCK_NAME = "vcs-poll.lock"


def now_iso() -> str:
    """Wall-clock event stamp for CI polling."""
    return now()


class Engine(EngineAdmissionMixin, EngineDelegationMixin, EngineCollaborationMixin,
              EngineExecutionMixin, EngineDispatchMixin, EngineRunMixin):
    def __init__(self, config: Config, *, driver=None, stop: threading.Event | None = None, ephemeral: bool = False):
        self.config = config
        self.driver = driver
        self.ephemeral = ephemeral
        self.stop = stop or threading.Event()
        self._drivers: dict[str, object] = {}


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
            ci_state = refresh_poll_state(state, shared, config, refresh_ci=False)
            events = []
            fetched = poll_upstream(config, project, state, now=now, interval=interval)
            if fetched is not None:
                events.append(fetched)
            # Fetch can delete refs. Discover their lifetime before cache capacity
            # selection, but keep skipped/disabled polls free of ref reads.
            # When the CI tick is not due there is no complete inventory, so
            # defer configured capacity selection (fixed envelope still
            # applies) to avoid evicting live rows via a stale wake ref.
            ci_due = bool(config.vcs.get("command")
                          and upstream_fetch_due(ci_state.get("last_poll"), now, interval))
            live = None
            if ci_due:
                try:
                    live = _live_ci_branches(config, project)
                except (Denied, OSError):
                    pass  # Unknown inventory must not imply branch deletion.
            _refresh_ci_cache(ci_state, shared, config, live_branches=live,
                              defer_branch_cap=not ci_due)
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
                            "proposal_refs": state.get("proposal_refs", {}),
                            "proposal_complete_messages": state.get("proposal_complete_messages", {}),
                            "last_ci_wait_poll": state.get("last_ci_wait_poll"),
                            "ci_wait_cursor": state.get("ci_wait_cursor"),
                            "ci_changes_cursor": state.get("ci_changes_cursor"),
                            "ci_changes_pending": state.get("ci_changes_pending"),
                            "ci_changes_epoch": state.get("ci_changes_epoch"),
                            "failures": _clean_ci_failures(
                                ci_state.get("failures"), config,
                                defer_branch_cap=not ci_due),
                            "successes": _clean_ci_successes(
                                ci_state.get("successes"), config,
                                defer_branch_cap=not ci_due)})
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
        with contextlib.ExitStack() as lock_scope, \
                lock(config.data / "locks" / f"daemon-{name}-{role_name}.lock", blocking=False):
            lock_scope.enter_context(config.runtime_policy())
            upstream_state: dict = {"last_fetch": None}
            ordinary_after = 0.0
            project = None
            # (state witness, monotonic re-evaluation deadline) of the last
            # negative readiness result; see readiness_recheck_seconds.
            readiness_memo = None
            while not stop.is_set():
                try:
                    updated = load(config.file)
                    if updated.data != config.data:
                        raise ConfigError("Changing data_dir requires a daemon restart")
                    config = updated
                    lock_scope.close()
                    lock_scope.enter_context(config.runtime_policy())
                    from .probes import service_due, next_deadline
                    # Retain process-local history caches across unchanged reloads,
                    # but revalidate mutable project settings on every daemon poll.
                    if project is None or project.config != config:
                        project = Project(config, name)
                        readiness_memo = None
                    else:
                        project.refresh_settings()
                    role = config.roles[role_name]
                    policy = getattr(role, "daemon_policy", None)
                    if policy not in DAEMON_POLICIES:
                        raise ConfigError("Invalid daemon_policy; reload validated configuration")
                    control = project.control()
                    ready = False
                    if not control.get("armed") or control.get("paused"):
                        readiness_memo = None
                    # An unarmed or paused project has no work: probes, editor
                    # ingestion, VCS polling and readiness are skipped entirely.
                    if control.get("armed") and not control.get("paused"):
                        probe = service_due(config, name, stop)
                        if probe is not None:
                            print(json.dumps(probe, ensure_ascii=False), flush=True)
                        editor.ingest(project)
                        if vcs_poll_enabled(config):
                            interval = vcs_poll_interval(config)
                            for event in poll_project_vcs(config, project, upstream_state,
                                                          now=time.time(), interval=interval):
                                print(json.dumps(event, ensure_ascii=False), flush=True)
                        if policy == "signals" and config.limits.readiness_recheck_seconds:
                            observed = state_witness(project.root)
                            if (readiness_memo is not None and readiness_memo[0] == observed[0]
                                    and time.monotonic() < readiness_memo[1]):
                                pass
                            else:
                                # A positive result or an error is never reused.
                                readiness_memo = None
                                ready = should_run(project, project.snapshots.get(),
                                                   role_name=role_name)
                                if (not ready and witness_settled(observed[1], observed[2])
                                        and state_witness(project.root)[0] == observed[0]):
                                    readiness_memo = (observed[0], time.monotonic()
                                                      + config.limits.readiness_recheck_seconds)
                        elif policy == "signals":
                            ready = should_run(project, project.snapshots.get(), role_name=role_name)
                        else:
                            ready = not control.get("draining")
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
