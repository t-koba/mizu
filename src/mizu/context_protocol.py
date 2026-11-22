"""Owner of the Context host-call/protocol boundary.

A driver's host-owned operations (``_request_done``, ``_hello``,
``_model_usage``, the two steer handshake operations, ``_engine_tool`` and
``_budget``) are protocol bookkeeping, not model capabilities. This module owns
their validation, ordering and state transitions. ``Context`` in ``runtime.py``
builds a narrow :class:`ProtocolInputs` from its current gate state and state
holders; it never hands its whole object to this owner. Capability validation,
tool accounting and dispatch stay in the runtime composition.

Schema: one operation string, one argument mapping and explicit state holders.
Bounds: the caller's existing run limits and evidence ceilings, never values
chosen from model input. Trust: arguments are untrusted and validated exactly
as before; host state and policy come only from the caller. Retry: a recorded
invocation identity is idempotent; refused admissions are not cached.
Evidence: retained usage, admission and engine-tool records keep their existing
paths and shapes. Failure: the same typed Mizu errors as the inline handlers.
"""
from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Callable

from . import check_jobs
from .budget import Budget
from .errors import (Busy, DeadlineWrapUp, Denied, DrainYieldExceeded,
                     InfraExceeded, LimitExceeded, MizuError, ModelFailure,
                     ProtocolError, RequestYieldExceeded, RunDeadlineExceeded)
from .fs import canonical


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


@dataclasses.dataclass(frozen=True)
class Field:
    """One narrow Context state slot, bound by the composing module.

    The read/write callables are the only reference to the state owner; this
    module never receives or retains the Context itself.
    """
    name: str
    read: Callable[[], object]
    write: Callable[[object], None]
    absent: object = None

    def get(self):
        return self.read()

    def set(self, value) -> None:
        self.write(value)


@dataclasses.dataclass(frozen=True)
class Fields:
    """The scalar Context state a host operation may read or transition."""
    request_failure_pending: Field
    check_steer_seen: Field
    check_steer_pending: Field
    bound_hit: Field
    wrap_up_attempted: Field
    tool_admission_exception: Field
    progress_tool: Field
    drain_admission_refused: Field
    request_bound_exhausted: Field
    request_admission_refused: Field
    admission_wait: Field
    admission_error: Field
    admission_exception: Field


@dataclasses.dataclass(frozen=True)
class Hooks:
    """Narrow Context transitions and host services used by protocol ops.

    ``model_evidence`` returns ``None`` when the run has no model evidence;
    ``write_json`` is the composing module's seam for retained evidence.
    """
    end_request: Callable[[int], None]
    enter_drain_wrap_up: Callable[[], None]
    drain_refusal: Callable[[str], None]
    start_request: Callable[[int], None]
    bound_message: Callable[[], str]
    model_evidence: Callable[[], dict | None]
    write_json: Callable[[Path, dict], None]
    read_request_count: Callable[[], int]
    add_tool_count: Callable[[int], int]
    add_request_count: Callable[[int], int]
    add_wrap_up_requests: Callable[[bool], None]


@dataclasses.dataclass(frozen=True)
class BudgetInputs:
    """Operator limits and identity for one daily request admission."""
    root: Path
    daily: int
    retention_days: int
    shared_daily: int
    timezone: str
    day_file_bytes: int
    project_name: str


@dataclasses.dataclass(frozen=True)
class Limits:
    """Effective per-run limits and fixed evidence-memory bounds."""
    requests: int
    tools: int
    wrap_up_requests_used: int
    wrap_up_requests_limit: int
    engine_tool_invocations: int
    engine_tool_id: int


@dataclasses.dataclass(frozen=True)
class State:
    """Shared state holders; mutated in place, never copied or owned here."""
    spend: object
    runtime_usage: dict
    runtime_models: dict
    engine_tool_invocations: dict
    request_sequences: set
    hello: threading.Event
    usage_path: Path
    admission_path: Path
    run_name: str
    project: object
    role_name: str
    engine_tools: frozenset
    delegated_child: bool
    budget: BudgetInputs
    fields: Fields
    hooks: Hooks


@dataclasses.dataclass(frozen=True)
class ProtocolInputs:
    """One host call's operation, arguments, gate-derived values and holders."""
    operation: str
    arguments: dict
    execution_attempt: int
    limits: Limits
    state: State


HOST_OPS = frozenset({
    "_request_done", "_hello", "_model_usage", "_check_steer",
    "_check_steer_delivered", "_engine_tool", "_budget",
})


def is_host_operation(operation: str) -> bool:
    return operation in HOST_OPS


def handle(inputs: ProtocolInputs) -> dict:
    """Handle one already-gated host operation, preserving protocol order."""
    operation = inputs.operation
    if operation == "_request_done":
        return _request_done(inputs)
    if operation == "_hello":
        return _hello(inputs)
    if operation == "_model_usage":
        return _model_usage(inputs)
    if operation == "_check_steer":
        return _check_steer(inputs)
    if operation == "_check_steer_delivered":
        return _check_steer_delivered(inputs)
    if operation == "_engine_tool":
        return _engine_tool(inputs)
    if operation == "_budget":
        return _budget(inputs)
    raise Denied("Unknown host operation")


def _request_done(inputs: ProtocolInputs) -> dict:
    arguments = inputs.arguments
    sequence = arguments.get("sequence")
    failed = arguments.get("failed", False)
    if type(sequence) is not int or sequence < 1 or type(failed) is not bool:
        raise Denied("Invalid completed request evidence")
    inputs.state.hooks.end_request(sequence)
    if failed:
        inputs.state.fields.request_failure_pending.set(True)
    return {"recorded": True}


def _hello(inputs: ProtocolInputs) -> dict:
    if inputs.arguments != {}:
        raise Denied("Unsupported bridge handshake")
    inputs.state.hello.set()
    return {}


def _model_usage(inputs: ProtocolInputs) -> dict:
    arguments, state = inputs.arguments, inputs.state
    sequence = arguments.get("sequence")
    usage = arguments.get("usage")
    if type(sequence) is not int or sequence < 1 or not isinstance(usage, dict):
        raise Denied("Invalid runtime usage")
    if len(canonical(arguments)) > 65536 or len(state.runtime_usage) >= 4096:
        raise LimitExceeded("Runtime usage evidence bound exceeded")
    key = (inputs.execution_attempt, sequence)
    state.runtime_usage.setdefault(key, usage)
    if isinstance(arguments.get("model"), dict):
        state.runtime_models.setdefault(key, arguments["model"])
    state.hooks.write_json(state.usage_path, {"observations": [
        {"execution_attempt": attempt, "sequence": seq, "usage": value,
         "model": state.runtime_models.get((attempt, seq))}
        for (attempt, seq), value in state.runtime_usage.items()]})
    return {"recorded": True}


def _check_steer(inputs: ProtocolInputs) -> dict:
    # Host-owned bridge operation for the managed pi-durable lane.
    # It is not a model capability and never exposes job output:
    # only the bounded, host-written notification reaches the
    # running unit. The automatic wake stays available until the
    # adapter confirms durable submission with the separate
    # acknowledgement; unread output still needs action=result and
    # the normal successful-finish acknowledgement.
    arguments, state = inputs.arguments, inputs.state
    if arguments != {}:
        raise Denied("Unsupported check steer request")
    seen = state.fields.check_steer_seen.get()
    jobs = check_jobs.steer_pending(state.project, state.role_name, seen)
    messages = [{'id': job['id'], 'message': check_jobs.steer_message(job)}
                for job in jobs[:check_jobs.MAX_CHECK_STEER_NOTIFICATIONS]]
    ids = [message['id'] for message in messages]
    state.fields.check_steer_seen.set(frozenset(set(seen) | set(ids)))
    state.fields.check_steer_pending.set(set(ids))
    return {'messages': messages}


def _check_steer_delivered(inputs: ProtocolInputs) -> dict:
    # Host-owned second phase: consume a wake only after the
    # adapter has durably submitted the notification. Never reads
    # job output or touches result/usage retrieval.
    arguments, state = inputs.arguments, inputs.state
    ids = arguments.get("ids")
    if (set(arguments) != {"ids"} or not isinstance(ids, list) or not ids
            or len(ids) > check_jobs.MAX_CHECK_STEER_NOTIFICATIONS
            or any(not isinstance(item, str) or item not in state.fields.check_steer_pending.get()
                   for item in ids)):
        raise Denied("Invalid check steer delivery")
    check_jobs.mark_wake_delivered(state.project, state.role_name, ids, state.run_name)
    state.fields.check_steer_pending.get().difference_update(ids)
    return {"recorded": True}


def _engine_tool(inputs: ProtocolInputs) -> dict:
    arguments, state, limits = inputs.arguments, inputs.state, inputs.limits
    fields = state.fields
    if fields.bound_hit.get() is not None:
        raise Denied(state.hooks.bound_message())
    name = arguments.get("name")
    invocation_id = arguments.get("invocation_id")
    if (not isinstance(invocation_id, str) or not invocation_id.strip()
            or len(invocation_id) > limits.engine_tool_id or not invocation_id.isprintable()):
        raise Denied("Invalid engine tool invocation id")
    if not isinstance(name, str) or name not in state.engine_tools:
        raise Denied("Engine tool has no explicit grant")
    key = (inputs.execution_attempt, invocation_id)
    if key in state.engine_tool_invocations:
        if state.engine_tool_invocations[key] != name:
            raise Denied("Engine tool invocation id reused for a different name")
        fields.tool_admission_exception.set(None)
        return {"allowed": True}
    if len(state.engine_tool_invocations) >= limits.engine_tool_invocations:
        raise LimitExceeded("Engine tool invocation evidence bound exceeded")
    with state.spend.lock:
        if state.hooks.add_tool_count(1) > limits.tools:
            # Native tools are never wrap-up operations: record the
            # bound and refuse without drawing the allowance.
            fields.bound_hit.set(fields.bound_hit.get() or "tools")
            refusal = LimitExceeded("Per-run tool limit exceeded")
            fields.tool_admission_exception.set(refusal)
            raise refusal
        fields.tool_admission_exception.set(None)
        state.engine_tool_invocations[key] = name
    fields.progress_tool.set(True)
    return {"allowed": True}


def _budget(inputs: ProtocolInputs) -> dict:
    arguments, state, limits = inputs.arguments, inputs.state, inputs.limits
    fields = state.fields
    state.hooks.enter_drain_wrap_up()
    if fields.bound_hit.get() == "drain" and not fields.wrap_up_attempted.get():
        # One shared handshake: the message carries the bound kind,
        # so each engine reports the kind it was given instead of
        # naming the request budget. This is not a daily-budget
        # rejection or an admitted request.
        fields.drain_admission_refused.set(True)
        state.hooks.drain_refusal("Drain bound refused the provider request")
    try:
        sequence = arguments.get("sequence")
        if type(sequence) is not int or sequence < 1:
            raise Denied("Invalid request sequence")
        sequence_key = (inputs.execution_attempt, sequence)
        with state.spend.lock:
            if sequence_key not in state.request_sequences:
                bound = fields.bound_hit.get()
                over = (bound in ("deadline", "drain")
                        or state.hooks.read_request_count() >= limits.requests)
                if over and bound == "deadline" and not fields.wrap_up_attempted.get():
                    # The reserved allowance belongs to Engine's
                    # reduced-grant follow-up, never the still-active
                    # normal invocation that crossed the cutoff.
                    raise DeadlineWrapUp(state.hooks.bound_message())
                if (over and bound in (None, "requests")
                        and not fields.wrap_up_attempted.get()):
                    fields.bound_hit.set("requests")
                    fields.request_admission_refused.set(True)
                    raise DeadlineWrapUp("Per-run provider request budget exhausted")
                if over and limits.wrap_up_requests_used >= limits.wrap_up_requests_limit:
                    if bound == "drain":
                        raise DrainYieldExceeded("Drain wrap-up request allowance exhausted")
                    if bound == "deadline":
                        raise RunDeadlineExceeded("Deadline wrap-up request allowance exhausted")
                    fields.request_bound_exhausted.set(True)
                    raise RequestYieldExceeded("Per-run provider request budget exhausted")
                try:
                    Budget(state.budget.root, state.budget.daily,
                           state.budget.retention_days, state.budget.shared_daily,
                           state.budget.timezone,
                           day_file_bytes=state.budget.day_file_bytes).take(
                               f"{state.run_name}:{inputs.execution_attempt}:{sequence}",
                               project=state.budget.project_name)
                except InfraExceeded:
                    evidence = state.hooks.model_evidence()
                    if evidence is not None:
                        evidence['admission_status'] = 'rejected'
                    fields.admission_wait.set(True)
                    raise
                except LimitExceeded:
                    evidence = state.hooks.model_evidence()
                    if evidence is not None:
                        evidence['admission_status'] = 'rejected'
                    raise
                except Exception:
                    evidence = state.hooks.model_evidence()
                    if evidence is not None:
                        evidence.update(requests_known=False, admission_status='unconfirmed')
                    raise
                if over:
                    # Wrap state records only after daily admission
                    # succeeds: an infrastructure refusal must not
                    # burn the allowance or mislabel the context.
                    fields.bound_hit.set(fields.bound_hit.get() or "requests")
                    state.hooks.add_wrap_up_requests(state.delegated_child)
                state.request_sequences.add(sequence_key)
                state.hooks.start_request(sequence)
                count = state.hooks.add_request_count(1)
                evidence = state.hooks.model_evidence()
                if evidence is not None:
                    evidence.update(requests=count, requests_known=True,
                                    admission_status="accepted")
                state.hooks.write_json(state.admission_path, {"requests": count})
            fields.admission_error.set(None)
            fields.admission_exception.set(None)
            fields.admission_wait.set(False)
            fields.tool_admission_exception.set(None)
            return {"admitted": True}
    except MizuError as exc:
        fields.admission_error.set(str(exc))
        fields.admission_exception.set(exc)
        raise
