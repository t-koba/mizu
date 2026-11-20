"""Own bounded role-admission facts and daemon readiness: progress self-admission,
completed-change observation, receipt waits, routed events, work-item wait
reconciliation and the lazy ``should_run`` evaluator.

Runtime re-exports them for their established seam.
"""
from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from . import (check_jobs, ci_waits, decision_delivery, proposal_waits, reviews,
               work_item_events)
from . import scheduling as _scheduling
from .ci_observation import wake_ci_failures
from .ci_wake import _ci_due_for, _ci_wake_enabled
from .decision_idle import _decision_delivery_state
from .errors import Denied, LimitExceeded, MizuError
from .fs import digest, lock
from .producer_admission import producer_hold
from .record_authority import _record_delivery
from .snapshot import progress_disposition, validate_progress
from .unit_finish import _writer_next_task
from .wake_evidence import _operator_woke, _role_cursor
from .work_readiness import _ready_work_observation

if TYPE_CHECKING:
    from .project import Project


def _reconcile_work_item_waits(project, role, config):
    """Return whether reconciliation ran and its holder-owned proof observation."""
    if (role is None or role.name not in project.roles or role.workspace != "write"
            or not {"work_order", "work_items_transition"} <= set(role.capabilities)):
        return False, False
    from .work_item_resolution import reconcile
    try:
        page, _ = reconcile(project, role.name, limit=config.limits.work_items_prompt)
        pending = page.get("_wait_resolution_pending")
        return (True, pending) if type(pending) is bool else (False, None)
    except (MizuError, OSError, ValueError, TypeError, KeyError, AttributeError,
            OverflowError, RecursionError):
        # A failed proof/read cannot reopen a waiter or suppress other wakes.
        # Any successful append before the failure remains durably discoverable.
        return False, None

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

def _on_change_observed(cursor: dict, snapshot: dict, *, exact_snapshot=False,
                        project=None, config=None, role_name="") -> bool:
    """True when an ``on_change`` role completed its relevant observation.

    A review_verdict capability holder is bound to the observed code digest:
    its observation is complete only when the cursor resolves to a valid typed
    review snapshot and create-only run receipt for that digest. The reviewed
    snapshot stays recorded as evidence, so a same-code successor is unchanged
    while a new digest re-arms review. A verdict-less attempt is not review
    completion; subsequent scheduled/event dispatches may retry at their
    existing cadence, with no internal retry loop. Other roles retain the same
    code-digest deduplication. Failure: never raises; invalid evidence is
    treated as unseen.

    State-only republications do not schedule ordinary ``on_change`` roles.
    Review cursors name the reviewed source, not the separate receipt snapshot.
    Legacy typed cursors remain verifiable through the shared review validator.
    """
    try:
        if not isinstance(cursor, dict):
            return False
        if exact_snapshot:
            if project is None or config is None or not role_name:
                return False
            reviews.cursor_review(project, config, role_name, cursor, snapshot)
            return True
        current = snapshot.get("code_digest")
        if not isinstance(current, str) or not current:
            return cursor.get("snapshot") == snapshot.get("id")
        seen = cursor.get("code_digest")
        if isinstance(seen, str) and seen:
            return seen == current
        return cursor.get("snapshot") == snapshot.get("id")
    except Exception:
        return False

def _receipt_wait_due(store, role, *, limit=1) -> bool:
    """Share one authoritative work-item traversal for CI/proposal readiness.

    Holds ``insights.lock`` once and feeds every blocked item from the
    current-state index to both accumulators; CI/proposal evidence and
    time-dependent readiness are reevaluated by the route-owned read-only
    accumulators each poll. Retains only O(limit) candidates plus the first
    deferred proposal failure; index failures abort even after an
    earlier candidate. Finalizes CI first: a CI event returns True and
    discards the deferred proposal failure, otherwise the deferred failure
    propagates or the proposal accumulator finalizes. No recovery, writes,
    syncs or delivery/ack observation; ready-work validation is untouched.
    """
    ci_state = ci_waits.readiness_init(store, role, limit=limit)
    proposal_state = proposal_waits.readiness_init(store, role, limit=limit)
    with lock(store.root / "locks" / "insights.lock"):
        for item in store.current_items(statuses=("blocked",)):
            ci_waits.readiness_feed(ci_state, store, item)
            proposal_waits.readiness_feed(proposal_state, store, item)
        if ci_waits.readiness_events(ci_state):
            return True
        return bool(proposal_waits.readiness_events(proposal_state))

def _role_events_due(project: Project, snapshot: dict, role_name=None, *,
                     completed_wait_probes=None) -> bool:
    """Routed decisions/insight attention, terminal events or waits due to a role.

    Inspect the named role, else project write roles. Exact decision/attention
    authority, route, budget and store failures propagate: never suppress a wake
    by treating invalid delivery as an empty queue. Legacy decision-history
    isolation remains in its source scanner; terminal/wait probing is unchanged.
    ``completed_wait_probes`` names roles whose check-job/CI/proposal probes
    completed empty earlier in this same ``should_run`` evaluation. An omitted
    role (or ``None``) is probed here; errors never populate the set.
    """
    decision_delivery.authority(project.insights.root)
    names = [role_name] if role_name in project.roles else [
        name for name in project.roles
        if name in project.config.roles and project.config.roles[name].workspace == "write"]
    for name in names:
        wait_probes_completed = (completed_wait_probes is not None
                                 and name in completed_wait_probes)
        try:
            if (not wait_probes_completed
                    and "check_job" in project.config.roles[name].capabilities
                    and check_jobs.wake_ready(project, name)):
                return True
            terminal_triggers = project.config.roles[name].work_item_events
            if terminal_triggers and work_item_events.pending(
                    project.work_items, name, terminal_triggers,
                    sources=project.config.roles[name].work_item_sources, limit=1)["events"]:
                return True
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        triggers = project.config.roles[name].decision_events
        sources = getattr(project.config.roles[name], "decision_sources", "own") or "own"
        if sources == "any" or "defer" not in triggers:
            state = _decision_delivery_state(project, name, project.config.roles[name])
        else:
            # Deferrals must not hide a substantive trigger outside a bounded
            # full page; preserve the filtered exact scan for this edge.
            state = decision_delivery.pending(
                project, project.config.roles[name],
                actions=tuple(a for a in triggers if a != "defer"), limit=1)
        pending, troubled = state["events"], state["diagnostics"]
        if sources == "any":
            if pending or troubled:
                return True
        elif (any(e.get("action") != "defer" for e in pending)
              or any(d.get("action") != "defer" for d in troubled)):
            return True
        try:
            if (project.insights.due_waits(name, snapshot, limit=1)
                    or (not wait_probes_completed
                        and _receipt_wait_due(project.work_items, name, limit=1))):
                return True
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    return False


def should_run(project: Project, snapshot: dict, current_time: float | None = None,
               role_name: str | None = None, config=None) -> bool:
    """Collect daemon signals lazily, following the pure due evaluator's order.

    The evaluator names one next fact at a time. This preserves the old
    short-circuit contract: absent ready work or receipt-pinned CI wait evidence,
    done does not read inbox/decision events/general CI/insight waits/progress.
    Addressed operator records are checked before done, directly from SharedRecords. A higher-priority wake does not probe
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
        operator_records_due=None,
    )
    decision = _scheduling.evaluate(facts)
    if (decision.ready is None and role_name in project.roles and role is not None
            and role.workspace == "write"):
        from .continuations import ready as continuation_ready
        if continuation_ready(project.snapshots, role_name) is not None:
            facts = dataclasses.replace(facts, continuation_ready=True)
            decision = _scheduling.evaluate(facts)
    completed_wait_probes = None
    ready_work_observed = False
    ready_work_present = False
    while decision.ready is None:
        needed = decision.needed
        if needed == "goal_changed":
            value = not observer and digest(project.goal.encode()) != snapshot["goal_digest"]
        elif needed == "operator_woke":
            value = _operator_woke(control, role_record, observer=observer)
        elif needed == "ready_work_changed":
            if role_name is None or role is not None:
                _reconcile_work_item_waits(project, role, effective)
                value, ready_work_present, _ = _ready_work_observation(project, role_record, role)
                ready_work_observed = True
            else:
                value = False
        elif needed == "ci_wait_due":
            names = [role_name] if role_name in project.roles else [
                name for name in project.roles
                if project.config.roles[name].workspace == "write"]
            try:
                value = any(check_jobs.wake_ready(project, name)
                            or _receipt_wait_due(project.work_items, name, limit=1)
                            for name in names)
                if not value:
                    # The identical role set is examined again later by
                    # _role_events_due. Reuse only a fully completed negative
                    # scan; an error leaves it unset so that route retains its
                    # independent retry behavior.
                    completed_wait_probes = frozenset(names)
            except (Denied, LimitExceeded, OSError, ValueError):
                completed_wait_probes = None
                value = False
        elif needed == "operator_records_due":
            # A code-change/wait evaluation may name a placeholder role (for
            # example before admission resolves configuration). It cannot have
            # addressed operator records; don't let that suppress other causes.
            value = (_record_delivery(project).due(project, role_name)
                     if role_name is None or (role_name in project.roles
                                              and role_name in project.config.roles)
                     else False)
        elif needed == "outcome":
            value = role_record.get("outcome", "wait")
            if observer and value not in ("continue", "bounded", "done", "wait", "blocked"):
                value = "wait"
        elif needed == "inbox_changed":
            value = not observer and project.insights.generation() != snapshot.get("inbox_seen", "")
        elif needed == "routed_events_due":
            value = _role_events_due(project, snapshot, role_name,
                                     completed_wait_probes=completed_wait_probes)
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
            elif role_name is None:
                value = role_record.get("outcome") in ("continue", "bounded")
            else:
                value = bool(_writer_next_task(project, snapshot, role_name))
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

