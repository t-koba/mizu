"""Engine dispatch preparation: selection preview, bounded state projection and
slot-gated run entry.

This mixin is composed into ``Engine`` by the runtime, which keeps no duplicate
implementation of these methods.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import (check_jobs, ci_waits, decision_delivery, lock_diagnostics,
               proposal_waits, work_item_events)
from . import scheduling as _scheduling
from .ci_observation import selected_ci_failures_present
from .ci_wake import _ci_due_for, _ci_due_for_with_facts
from .config import role_work_item_events_limit
from .decision_idle import _decision_delivery_state
from .errors import Denied
from .fs import digest
from .record_authority import _record_delivery
from .record_migration import flat_records_present
from .role_admission import (_on_change_observed, _progress_self_admits,
                             _reconcile_work_item_waits)
from .unit_finish import _writer_next_task
from .wake_evidence import _operator_woke, _role_cursor
from .work_readiness import _ready_work_observation

if TYPE_CHECKING:
    from .runtime import Project


class EngineDispatchMixin:
    def preview(self, project: Project, role_name: str, attributes=None) -> dict:
        if role_name not in project.roles:
            raise Denied("Role is not enabled for this project")
        role = self.config.roles[role_name]
        if not role.selector:
            return {"profile": role.profile, "selector": None}
        from .classification import prepare
        return prepare(self, project, role, project.snapshots.get(), attributes, preview=True)

    def _dispatch_state(self, project, role_name, role, *, event_trigger, continuation=False,
                        prior_wait_resolution_pending=False, reconcile_waits=True):
        """Recompute scheduling inputs; a skipped attempt never claims execution.

        Used before queueing and again under dispatch locks. No events or
        observed cursors are acknowledged here, so a concurrent consumer can
        make a queued attempt obsolete without losing pending work.
        """
        snapshot = project.snapshots.get()
        cursor = _role_cursor(project, role_name)
        review_holder = "review_verdict" in getattr(role, "capabilities", ())
        code_unchanged = role.on_change and _on_change_observed(
            cursor, snapshot, exact_snapshot=review_holder,
            project=project if review_holder else None,
            config=self.config if review_holder else None, role_name=role_name)
        triggers = tuple(getattr(role, "decision_events", ()) or ())
        sources = getattr(role, "decision_sources", "own") or "own"
        record_state = _record_delivery(project).pending(project, role)
        decision_state = _decision_delivery_state(project, role_name, role)
        decision_events = decision_state["events"]
        receipt_diagnostics = decision_state["diagnostics"]
        # Like daemon readiness, authoritative dispatch prepares waiters before
        # projections. Public reads never resolve; each append rechecks proof.
        waits_reconciled, pending_resolution = (
            _reconcile_work_item_waits(project, role, self.config) if reconcile_waits
            else (False, prior_wait_resolution_pending))
        work_items = project.work_items
        terminal_page = work_item_events.pending(
            work_items, role_name, role.work_item_events, sources=role.work_item_sources,
            limit=role_work_item_events_limit(project.config, role))
        terminal_page = work_item_events.delivery(role_name, terminal_page)
        terminal_events = terminal_page["events"]
        wait_limit = project.config.limits.wait_events_limit
        ci_events, ci_manifest = ci_waits.delivery(work_items, role_name, limit=wait_limit)
        proposal_events, proposal_manifest = proposal_waits.delivery(
            work_items, role_name, limit=wait_limit)
        wait_events = (project.insights.due_waits(role_name, snapshot, limit=wait_limit)
                       + ci_events + proposal_events)
        has_wait = bool(wait_events or check_jobs.wake_ready(project, role_name))
        # Dependency obligations addressed to this role ride along when
        # it runs for other reasons; they never admit a run on their
        # own, so no new wake source is implied.
        obligations = project.insights.pending_obligations(role_name, limit=wait_limit)
        # Event-triggered dispatch (platform file triggers) admits only on a
        # addressed operator record, subscribed decision/terminal event, due wait,
        # or an undelivered, role-owned exact wait-resolution proof.
        # Decision routing uses decision_sources: own-source
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
                substantive = decision_delivery.pending(
                    project, role, actions=tuple(a for a in triggers if a != "defer"), limit=1)
                substantive_events, substantive_diagnostics = substantive["events"], substantive["diagnostics"]
                substantive_diagnostics = [d for d in substantive_diagnostics
                                            if isinstance(d, dict) and d.get("action") != "defer"]
        ci_unavailable = False
        try:
            ci_present = _ci_due_for(project, self.config, role)
        except Exception:
            ci_present = []
            ci_unavailable = True
        ci_current_present = bool(ci_present)
        if role.scheduled_inputs and role.ci_wake:
            try:
                ci_current_present = (ci_current_present or
                                      selected_ci_failures_present(project, self.config))
            except Exception:
                ci_unavailable = True
        try:
            _ci_snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else None
            ci_failures, external_check_wake_facts = _ci_due_for_with_facts(
                project, self.config, role, _ci_snapshot_id, time.time())
        except Exception:
            ci_failures, external_check_wake_facts = [], []
            ci_unavailable = True
        # A check-result subscription is not a request to start unrelated
        # project work. Explicit-only roles may resume their owned checks,
        # but generic ledger readiness belongs to configured autonomous runs.
        autonomous = bool(role.daemon or role.interval_seconds or role.calendar
                          or role.decision_events or role.work_item_events)
        unscheduled = not bool(role.daemon or role.interval_seconds or role.calendar)
        has_code_event = bool(role.on_change and unscheduled and not code_unchanged)
        ready_work_changed, ready_work_present, resolved_wait = (False, False, False)
        if not event_trigger or autonomous:
            ready_work_changed, ready_work_present, resolved_wait = _ready_work_observation(
                project, snapshot if role.workspace == "write" else cursor, role,
                work_items=work_items)
        elif (pending_resolution is not False and role.workspace == "write"
              and {"work_order", "work_items_transition"} <= set(role.capabilities)):
            # Explicit-only event roles do not self-admit generic ready work.
            # Reconciliation scans the durable ledger on every fresh attempt;
            # only a pending proof justifies a ready-set comparison here.
            _, _, resolved_wait = _ready_work_observation(
                project, snapshot, role, work_items=work_items)
        resolved_wait = bool(resolved_wait and role.workspace == "write"
                             and {"work_order", "work_items_transition"} <= set(role.capabilities))
        has_wait = has_wait or resolved_wait
        # Only publishers consume snapshot-wide generations. Observers consume
        # operator wakes independently, even when no new code is published.
        wake_base = snapshot if role.workspace == "write" else cursor
        operator_woke = _operator_woke(project.control(), wake_base, observer=role.workspace != "write")
        # ``roles.<name>.event_wakes``: a standalone event dispatch sees only the
        # listed wake kinds. Masking the facts here leaves nothing consumed:
        # a skipped attempt acknowledges no event, record or cursor.
        def wakes(kind):
            return not event_trigger or kind in role.event_wakes
        has_code_event = has_code_event and wakes("code")
        ready_work_changed = ready_work_changed and wakes("ready_work")
        operator_woke = operator_woke and wakes("operator")
        has_wait = has_wait and wakes("wait")
        ci_failures_wake = bool(ci_failures) and wakes("ci")
        record_wake = bool(record_state[0]["wake_pending"]) and wakes("operator_record")
        decision_wake = bool(substantive_events or substantive_diagnostics) and wakes("decision")
        work_item_wake = bool(terminal_events) and wakes("work_item")
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
                has_record=record_wake,
                has_decision=decision_wake,
                has_work_item=work_item_wake,
                has_wait=has_wait,
                has_ci=ci_failures_wake,
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
        next_task = _writer_next_task(project, snapshot, role_name) if role.workspace == "write" else None
        if role.workspace == "write":
            # Writers never self-admit on bare progress here: a consumed
            # continue/progress record must not re-dispatch without a new
            # handoff. The durable handoff arrives via ``has_next_task`` and
            # holder-only readiness via ``ready_work_changed``; the removed
            # self-wait backoff timer has no successor.
            progress_self_admits = False
        else:
            progress_self_admits = _progress_self_admits(cursor, self.config, role_name)
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
            has_record=record_wake,
            has_decision=decision_wake,
            has_work_item=work_item_wake,
            has_wait=has_wait,
            has_ci=ci_failures_wake,
            operator_woke=bool(operator_woke and role.workspace != "write"),
            progress_self_admits=bool(progress_self_admits),
            has_next_task=bool(next_task),
        ))
        if _dispatch.skipped == "unchanged":
            return {"skip": {"skipped": "unchanged", "snapshot": snapshot["id"],
                    "code_digest": snapshot["code_digest"]}}
        other_scheduled_input = bool(
            ci_unavailable or not isinstance(record_state, (list, tuple)) or not record_state
            or not isinstance(record_state[0], dict)
            or record_state[0].get("wake_pending") or record_state[0].get("events")
            or decision_events or receipt_diagnostics or terminal_events or wait_events
            or ci_current_present or ci_failures or has_wait or ready_work_changed
            or ready_work_present or explicit_wake
            or progress_self_admits or next_task or (role.on_change and not code_unchanged))
        return {'next_task': next_task, 'proposal_manifest': proposal_manifest, 'ci_manifest': ci_manifest, 'record_state': record_state, 'decision_state': decision_state, 'snapshot': snapshot, 'sources': sources, 'terminal_events': terminal_events, 'terminal_page': terminal_page, 'wait_events': wait_events, 'obligations': obligations, 'ci_present': ci_present, 'ci_failures': ci_failures, 'prior_wait_expired': prior_wait_expired, 'decision_events': decision_events, 'decision_receipt_diagnostics': receipt_diagnostics, '_resolved_wait': resolved_wait,
                'external_check_wake_facts': external_check_wake_facts,
                '_pending_wait_resolution': pending_resolution,
                '_waits_reconciled': waits_reconciled, '_dispatch': _dispatch,
                '_has_other_scheduled_input': other_scheduled_input}

    def run(self, project: Project, role_name: str, *, attributes=None, retry_busy: bool = False,
                enqueue_busy: bool = False, event_trigger: bool = False, probes_serviced: bool = False,
                scheduled_invocation: bool = False) -> dict:
        if flat_records_present(project):
            raise Denied(f"Flat shared records remain; pause the project and run "
                         f"mizu migrate-records {project.name} --apply before resuming")
        with self.config.runtime_policy(), lock_diagnostics.collect(self.config.limits.lock_diagnostics):
            return self._run(project, role_name, attributes=attributes, retry_busy=retry_busy,
                             enqueue_busy=enqueue_busy, event_trigger=event_trigger,
                             probes_serviced=probes_serviced, scheduled_invocation=scheduled_invocation)
