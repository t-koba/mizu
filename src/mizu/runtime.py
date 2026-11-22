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
from . import context_protocol
from .context_host_tools import ContextHostToolsMixin
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
from .context_vcs_dispose_tools import ContextVcsDisposeMixin
from .context_vcs_retire_tools import ContextVcsRetireMixin
from .context_vcs_request_tools import ContextVcsRequestToolsMixin
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
# source_commit keeps its established runtime re-export seam.
from .publication import source_commit
from .web import Web
# Deferred-fault classification belongs to the protocol owner; runtime keeps
# the established re-export seam for existing callers and tests.
from .context_protocol import INFRA_WAIT, is_deferred, is_infra_wait
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


#: Fixed memory bounds for run-local pre-send admission identities.
MAX_ENGINE_TOOL_INVOCATIONS = 4096
MAX_ENGINE_TOOL_ID = 256
MAX_READ_IDENTITIES = workspace_reads.MAX_READ_IDENTITIES

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


def _bump_counter(context, name: str, delta: int = 1) -> int:
    """Advance one Context counter through its property seam; return the value."""
    value = getattr(context, name) + delta
    setattr(context, name, value)
    return value


def _bump_wrap_up_requests(context, delegated: bool) -> None:
    name = "delegated_wrap_up_requests" if delegated else "wrap_up_requests"
    setattr(context, name, getattr(context, name) + 1)


def _state_field(context, name: str, absent=None) -> context_protocol.Field:
    """Bind one named Context state slot for the protocol owner."""
    return context_protocol.Field(
        name, lambda: getattr(context, name, absent),
        lambda value, name=name: setattr(context, name, value), absent)


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
                ContextVcsRetireMixin, ContextVcsDisposeMixin,
                ContextVcsRequestToolsMixin, ContextHostToolsMixin):
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
        # Mizu-owned driver channels created for this context, retained only
        # for bounded teardown proof of an in-process delegation child.
        self._driver_channels: list = []
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

    def _protocol_inputs(self, operation: str, arguments: dict) -> context_protocol.ProtocolInputs:
        """Compose one host call's inputs from live gate and state holders."""
        config, spend_role = self.config, self.spend_role or self.role
        used, limit = self._wrap_up_request_used_limit()
        fields = context_protocol.Fields(
            request_failure_pending=_state_field(self, "request_failure_pending"),
            check_steer_seen=_state_field(self, "check_steer_seen", ()),
            check_steer_pending=_state_field(self, "_check_steer_pending"),
            bound_hit=_state_field(self, "bound_hit"),
            wrap_up_attempted=_state_field(self, "wrap_up_attempted"),
            tool_admission_exception=_state_field(self, "tool_admission_exception"),
            progress_tool=_state_field(self, "progress_tool"),
            drain_admission_refused=_state_field(self, "drain_admission_refused"),
            request_bound_exhausted=_state_field(self, "request_bound_exhausted"),
            request_admission_refused=_state_field(self, "request_admission_refused"),
            admission_wait=_state_field(self, "admission_wait"),
            admission_error=_state_field(self, "admission_error"),
            admission_exception=_state_field(self, "admission_exception"),
        )
        hooks = context_protocol.Hooks(
            end_request=self.end_request,
            enter_drain_wrap_up=self.enter_drain_wrap_up,
            drain_refusal=self.drain_refusal,
            start_request=self.start_request,
            bound_message=self._bound_message,
            model_evidence=lambda: getattr(self, "model_evidence", None),
            write_json=write_json,
            read_request_count=lambda: self.request_count,
            add_tool_count=lambda delta: _bump_counter(self, "tool_count", delta),
            add_request_count=lambda delta: _bump_counter(self, "request_count", delta),
            add_wrap_up_requests=lambda delegated: _bump_wrap_up_requests(self, delegated),
        )
        budget = context_protocol.BudgetInputs(
            root=config.data / "budget",
            daily=config.limits.daily_requests,
            retention_days=config.limits.retention_days,
            shared_daily=config.limits.shared_daily_requests,
            timezone=config.timezone,
            day_file_bytes=config.limits.budget_day_file_bytes,
            project_name=self.project.name,
        )
        limits = context_protocol.Limits(
            requests=role_requests_per_run(config, spend_role),
            tools=role_tools_per_run(config, spend_role),
            wrap_up_requests_used=used,
            wrap_up_requests_limit=limit,
            engine_tool_invocations=MAX_ENGINE_TOOL_INVOCATIONS,
            engine_tool_id=MAX_ENGINE_TOOL_ID,
        )
        state = context_protocol.State(
            spend=self._spend,
            runtime_usage=self.runtime_usage,
            runtime_models=self.runtime_models,
            engine_tool_invocations=self.engine_tool_invocations,
            request_sequences=self.request_sequences,
            hello=self.hello,
            usage_path=self.run_dir / "runtime-usage.json",
            admission_path=self.run_dir / "admission.json",
            run_name=self.run_dir.name,
            project=self.project,
            role_name=self.role.name,
            engine_tools=self.role.engine_tools,
            delegated_child=bool(getattr(self, "delegated_child", False)),
            budget=budget,
            fields=fields,
            hooks=hooks,
        )
        return context_protocol.ProtocolInputs(
            operation=operation, arguments=arguments,
            execution_attempt=self.execution_attempt, limits=limits, state=state)

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
            if context_protocol.is_host_operation(operation):
                return context_protocol.handle(self._protocol_inputs(operation, arguments))
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
        "sync": ContextHostToolsMixin._op_sync, "vcs_read": ContextVcsReadToolsMixin._op_vcs_read,
        "vcs_publish": ContextVcsPublishMixin._op_vcs_publish,
        "vcs_request": ContextVcsRequestToolsMixin._op_vcs_request,
        "vcs_retire": ContextVcsRetireMixin._op_vcs_retire,
        "vcs_dispose": ContextVcsDisposeMixin._op_vcs_dispose,
        "materialize": ContextRoleToolsMixin._op_materialize,
        "consult": ContextJoinToolsMixin._op_consult, "delegate": ContextJoinToolsMixin._op_delegate,
        "delegate_join": ContextJoinToolsMixin._op_delegate_join, "report": ContextHostToolsMixin._op_report,
        "research_read": ContextRoleToolsMixin._op_research_read, "research": ContextRoleToolsMixin._op_research,
        "finish": ContextFinishMixin._op_finish,
    }


#: Poll coordination and daemon orchestration live in daemon_runtime;
#: re-exported here to preserve the established runtime seams.
from .daemon_runtime import POLL_LOCK_NAME, daemon, now_iso, poll_project_vcs


class Engine(EngineAdmissionMixin, EngineDelegationMixin, EngineCollaborationMixin,
              EngineExecutionMixin, EngineDispatchMixin, EngineRunMixin):
    def __init__(self, config: Config, *, driver=None, stop: threading.Event | None = None, ephemeral: bool = False):
        self.config = config
        self.driver = driver
        self.ephemeral = ephemeral
        self.stop = stop or threading.Event()
        self._drivers: dict[str, object] = {}
