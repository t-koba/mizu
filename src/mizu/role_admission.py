"""Own bounded role-admission facts: progress self-admission, completed-change
observation, receipt waits, routed events and work-item wait reconciliation.

Runtime re-exports them for their established seam.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import (check_jobs, ci_waits, decision_delivery, proposal_waits, reviews,
               work_item_events)
from .decision_idle import _decision_delivery_state
from .errors import Denied, LimitExceeded, MizuError
from .fs import lock
from .snapshot import progress_disposition, validate_progress

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
