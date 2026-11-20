"""Own bounded role-admission facts and daemon readiness: progress self-admission,
completed-change observation, receipt waits, routed events, due queue messages,
work-item wait reconciliation and the lazy ``should_run`` evaluator.

Runtime re-exports them for their established seam.
"""
from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from . import check_jobs, ci_waits, queue_facts, reviews, work_item_events
from . import scheduling as _scheduling
from .ci_observation import wake_ci_failures
from .ci_wake import _ci_due_for, _ci_wake_enabled
from .errors import Denied, LimitExceeded, MizuError
from .fs import digest, lock
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
    """Whether a CI wait has a deliverable terminal event for the role.

    Holds ``records.lock`` once and feeds every blocked item from the
    current-state index to the route-owned read-only accumulator; CI evidence
    and time-dependent readiness are reevaluated each poll. Index failures
    abort. No recovery, writes, syncs or delivery/ack observation.
    """
    ci_state = ci_waits.readiness_init(store, role, limit=limit)
    with lock(store.root / "locks" / "records.lock"):
        for item in store.current_items(statuses=("blocked",)):
            ci_waits.readiness_feed(ci_state, store, item)
        return bool(ci_waits.readiness_events(ci_state))

def _role_events_due(project: Project, snapshot: dict, role_name=None, *,
                     completed_wait_probes=None) -> bool:
    """Check-job results, terminal work-item events or CI waits due to a role.

    Inspect the named role, else project write roles. ``completed_wait_probes``
    names roles whose check-job/CI probes completed empty earlier in this same
    ``should_run`` evaluation. An omitted role (or ``None``) is probed here;
    errors never populate the set.
    """
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
            if (not wait_probes_completed
                    and _receipt_wait_due(project.work_items, name, limit=1)):
                return True
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
    return False


def should_run(project: Project, snapshot: dict, current_time: float | None = None,
               role_name: str | None = None, config=None) -> bool:
    """Collect daemon signals lazily, following the pure due evaluator's order.

    The evaluator names one next fact at a time. This preserves the
    short-circuit contract: absent ready work or receipt-pinned CI wait evidence,
    done does not read events/general CI/progress. Due messages in a queue
    that wakes the role are checked before done. A higher-priority wake does
    not probe lower-priority stores. Named roles only see role-eligible ready work;
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
        messages_due=None,
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
        elif needed == "messages_due":
            # Without a named role, the project's write roles stand in, as for
            # the other routed facts. A placeholder role name that is not
            # configured for the project has no queues.
            names = ([role_name] if role_name in project.roles else
                     [] if role_name is not None else
                     [name for name in project.roles
                      if name in project.config.roles
                      and project.config.roles[name].workspace == "write"])
            value = any(queue_facts.due(project, name) for name in names)
        elif needed == "outcome":
            value = role_record.get("outcome", "wait")
            if observer and value not in ("continue", "bounded", "done", "wait", "blocked"):
                value = "wait"
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
    return bool(decision.ready)

