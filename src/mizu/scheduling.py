"""Central admission policy for daemon readiness and run dispatch.

Mechanism/policy split: event readers, wait stores, CI caches, progress
records and configuration remain the producers of facts. This module only
orders normalized facts into a readiness/dispatch decision. It
performs no filesystem or project reads, so it stays pure and table-testable.

Two call contexts share event vocabulary but not semantics, so they use
distinct input types evaluated by one :func:`evaluate` entry point:

- :class:`DaemonFacts` -- writer-daemon ``should_run`` readiness.
- :class:`DispatchFacts` -- :meth:`Engine.run` dispatch/on-change gates.

Ordering mirrors ``src/mizu/runtime.py`` exactly; see
``docs/scheduling.md`` for the behavior map.
"""
from __future__ import annotations

import dataclasses


#: Outcomes whose daemon may self-admit on progress evidence.
_PROGRESS_OUTCOMES = ("continue", "bounded")


@dataclasses.dataclass(frozen=True)
class DaemonFacts:
    """Normalized facts for the writer-daemon readiness gate."""

    armed: bool = False
    paused: bool = False
    draining: bool = False
    # ``None`` means the runtime has not collected this next fact yet. The
    # evaluator returns which fact to collect, preserving short-circuit I/O.
    goal_changed: bool | None = None
    operator_woke: bool | None = None
    # Full role-eligible ID set differs from the last delivered unit input.
    ready_work_changed: bool | None = None
    ci_wait_due: bool | None = None
    outcome: str | None = None
    inbox_changed: bool | None = None
    routed_events_due: bool | None = None
    ci_due: bool | None = None
    wait_expired: bool | None = None
    # Caller-resolved progress self-admission: legacy missing record or
    # unscoped calls resolve True (existing permissive behavior), malformed
    # persisted records resolve False (fail closed), otherwise the
    # ``progress_disposition(...) != "parked_no_progress"`` verdict.
    progress_admits: bool | None = None


@dataclasses.dataclass(frozen=True)
class DaemonDecision:
    # ``ready=None`` requests the next fact named by ``needed``; otherwise
    # this is a terminal policy decision.
    ready: bool | None
    reason: str
    needed: str | None = None


@dataclasses.dataclass(frozen=True)
class DispatchFacts:
    """Normalized facts for event-trigger and on-change dispatch gates."""

    event_trigger: bool = False
    on_change: bool = False
    code_unchanged: bool = False
    # Subscribed code-change event for --event probes: true only for
    # unscheduled on_change roles with an unobserved code digest. Scheduled
    # on_change roles keep timer-based discovery; their --event probes still
    # exit with no-event when no subscribed decision/work/wait/CI is due.
    has_code_event: bool = False
    has_decision: bool = False
    has_work_item: bool = False
    ready_work_changed: bool = False
    has_wait: bool = False
    has_ci: bool = False
    # Runtime normalizes role-consumable wakes. A role-cursored operator
    # wake may also admit an event probe; snapshot-wide wakes do not.
    explicit_wake: bool = False
    operator_woke: bool = False
    # Runtime resolves the role-specific persisted progress policy.
    progress_self_admits: bool = False


@dataclasses.dataclass(frozen=True)
class DispatchDecision:
    admitted: bool
    skipped: str | None = None
    admission: str | None = None


def _has_event(facts: DispatchFacts) -> bool:
    return bool(facts.has_decision or facts.has_work_item or facts.has_wait or facts.has_ci
                or facts.ready_work_changed or (facts.event_trigger and facts.operator_woke))


def _event_label(facts: DispatchFacts) -> str:
    if facts.has_decision:
        return "decision"
    if facts.has_work_item:
        return "work_item"
    if facts.has_wait:
        return "wait"
    if facts.has_ci:
        return "ci"
    if facts.event_trigger and facts.operator_woke:
        return "operator"
    return "ready_work"


def evaluate_daemon(facts: DaemonFacts) -> DaemonDecision:
    """Order daemon facts and request only the next signal needed."""
    if not facts.armed or facts.paused or facts.draining:
        return DaemonDecision(ready=False, reason="control")
    if facts.goal_changed is None:
        return DaemonDecision(ready=None, reason="collecting", needed="goal_changed")
    if facts.goal_changed:
        return DaemonDecision(ready=True, reason="goal")
    if facts.operator_woke is None:
        return DaemonDecision(ready=None, reason="collecting", needed="operator_woke")
    if facts.operator_woke:
        return DaemonDecision(ready=True, reason="operator")
    if facts.ready_work_changed is None:
        return DaemonDecision(ready=None, reason="collecting", needed="ready_work_changed")
    if facts.ready_work_changed:
        return DaemonDecision(ready=True, reason="ready_work")
    if facts.ci_wait_due is None:
        return DaemonDecision(ready=None, reason="collecting", needed="ci_wait_due")
    if facts.ci_wait_due:
        return DaemonDecision(ready=True, reason="ci_wait")
    if facts.outcome is None:
        return DaemonDecision(ready=None, reason="collecting", needed="outcome")
    if facts.outcome == "done":
        # Without changed ready work, a completed objective ignores inbox/events/CI/wait/progress.
        return DaemonDecision(ready=False, reason="done")
    if facts.inbox_changed is None:
        return DaemonDecision(ready=None, reason="collecting", needed="inbox_changed")
    if facts.inbox_changed:
        return DaemonDecision(ready=True, reason="inbox")
    if facts.routed_events_due is None:
        return DaemonDecision(ready=None, reason="collecting", needed="routed_events_due")
    if facts.routed_events_due:
        return DaemonDecision(ready=True, reason="events")
    if facts.ci_due is None:
        return DaemonDecision(ready=None, reason="collecting", needed="ci_due")
    if facts.ci_due:
        return DaemonDecision(ready=True, reason="ci")
    if facts.wait_expired is None:
        return DaemonDecision(ready=None, reason="collecting", needed="wait_expired")
    if facts.wait_expired:
        return DaemonDecision(ready=True, reason="wait")
    if facts.outcome not in _PROGRESS_OUTCOMES:
        return DaemonDecision(ready=False, reason="outcome")
    if facts.progress_admits is None:
        return DaemonDecision(ready=None, reason="collecting", needed="progress_admits")
    if facts.progress_admits:
        return DaemonDecision(ready=True, reason="progress")
    return DaemonDecision(ready=False, reason="parked")


def evaluate_dispatch(facts: DispatchFacts) -> DispatchDecision:
    """Order dispatch facts exactly as ``Engine.run`` does."""
    if facts.event_trigger and not _has_event(facts) and not facts.has_code_event:
        return DispatchDecision(admitted=False, skipped="no-event", admission=None)
    if (
        facts.code_unchanged
        and not _has_event(facts)
        and not facts.explicit_wake
        and not facts.progress_self_admits
    ):
        return DispatchDecision(admitted=False, skipped="unchanged", admission=None)
    if facts.code_unchanged and _has_event(facts):
        return DispatchDecision(admitted=True, skipped=None, admission=_event_label(facts))
    if facts.event_trigger and _has_event(facts):
        return DispatchDecision(admitted=True, skipped=None, admission=_event_label(facts))
    if facts.on_change:
        return DispatchDecision(admitted=True, skipped=None, admission="change")
    return DispatchDecision(admitted=True, skipped=None, admission="schedule")


#: Hold reasons for producer admission. ``load_unavailable`` fails closed
#: when the required load cannot be read; the producer thresholds never
#: cancel a running unit, they only hold the next admission.
PRODUCER_HOLD_REASONS = ("per_source_full", "open_work_full", "load_unavailable")


@dataclasses.dataclass(frozen=True)
class ProducerFacts:
    """Normalized facts for per-source producer admission."""

    is_producer: bool = False
    per_source_limit: int = 0
    open_work_limit: int = 0
    source_pending: int = 0
    open_count: int = 0
    load_ok: bool = True


@dataclasses.dataclass(frozen=True)
class ProducerDecision:
    held: bool
    reasons: tuple = ()


def evaluate_producer(facts: ProducerFacts) -> ProducerDecision:
    """Hold only configured producers at a configured congestion threshold."""
    if not facts.is_producer:
        return ProducerDecision(held=False, reasons=())
    if not facts.load_ok:
        return ProducerDecision(held=True, reasons=("load_unavailable",))
    reasons = []
    if facts.per_source_limit and facts.source_pending >= facts.per_source_limit:
        reasons.append("per_source_full")
    if facts.open_work_limit and facts.open_count >= facts.open_work_limit:
        reasons.append("open_work_full")
    return ProducerDecision(held=bool(reasons), reasons=tuple(reasons))


def evaluate(facts: DaemonFacts | DispatchFacts | ProducerFacts) -> DaemonDecision | DispatchDecision | ProducerDecision:
    """Evaluate one normalized fact set for its own call context."""
    if isinstance(facts, DaemonFacts):
        return evaluate_daemon(facts)
    if isinstance(facts, DispatchFacts):
        return evaluate_dispatch(facts)
    if isinstance(facts, ProducerFacts):
        return evaluate_producer(facts)
    raise TypeError("evaluate requires DaemonFacts or DispatchFacts")
