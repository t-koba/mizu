"""Engine dispatch preparation: selection preview, bounded state projection and
slot-gated run entry.

This mixin is composed into ``Engine`` by the runtime, which keeps no duplicate
implementation of these methods.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import (check_jobs, ci_waits, lock_diagnostics, proposal_waits,
               queue_facts, work_item_events)
from . import scheduling as _scheduling
from .ci_wake import _ci_due_for, _ci_due_for_with_facts
from .config import role_work_item_events_limit
from .errors import Denied
from .fs import digest
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
        queues = queue_facts.opening(project, role_name)
        message_due = queue_facts.due(project, role_name)
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
        wait_events = [*ci_events, *proposal_events]
        has_wait = bool(wait_events or check_jobs.wake_ready(project, role_name))
        # Event-triggered dispatch (platform file triggers) admits only on a
        # due message in a queue that wakes the role, a terminal work-item
        # event, a due wait, CI, or an undelivered role-owned exact
        # wait-resolution proof. Otherwise it exits before allocating a run
        # directory or model use, like unchanged-code admission. Periodic
        # timers invoke without the flag, so scheduled discovery still runs
        # at its interval.
        ci_unavailable = False
        try:
            ci_present = _ci_due_for(project, self.config, role)
        except Exception:
            ci_present = []
            ci_unavailable = True
        ci_current_present = bool(ci_present)
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
                          or role.work_item_events)
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
        message_wake = message_due and wakes("queue")
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
                has_message=message_wake,
                has_work_item=work_item_wake,
                has_wait=has_wait,
                has_ci=ci_failures_wake,
            ))
            if event_gate.skipped == "no-event":
                return {"skip": {"skipped": "no-event", "snapshot": snapshot["id"],
                        "code_digest": snapshot["code_digest"]}}
        # Only publishers consume snapshot-wide goal/run-wait input.
        goal_changed = role.workspace == "write" and digest(project.goal.encode()) != snapshot["goal_digest"]
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
            goal_changed or prior_wait_expired))
        _dispatch = _scheduling.evaluate(_scheduling.DispatchFacts(
            event_trigger=bool(event_trigger),
            continuation=continuation,
            on_change=bool(role.on_change),
            code_unchanged=bool(code_unchanged),
            has_code_event=bool(has_code_event),
            explicit_wake=bool(explicit_wake),
            ready_work_changed=ready_work_changed,
            has_message=message_wake,
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
        return {'next_task': next_task, 'ci_manifest': ci_manifest,
                'proposal_manifest': proposal_manifest, 'queues': queues, 'snapshot': snapshot, 'terminal_events': terminal_events, 'terminal_page': terminal_page, 'wait_events': wait_events, 'ci_present': ci_present, 'ci_failures': ci_failures, 'prior_wait_expired': prior_wait_expired, '_resolved_wait': resolved_wait,
                'external_check_wake_facts': external_check_wake_facts,
                '_pending_wait_resolution': pending_resolution,
                '_waits_reconciled': waits_reconciled, '_dispatch': _dispatch}

    def run(self, project: Project, role_name: str, *, attributes=None, retry_busy: bool = False,
                enqueue_busy: bool = False, event_trigger: bool = False, probes_serviced: bool = False) -> dict:
        with self.config.runtime_policy(), lock_diagnostics.collect(self.config.limits.lock_diagnostics):
            return self._run(project, role_name, attributes=attributes, retry_busy=retry_busy,
                             enqueue_busy=enqueue_busy, event_trigger=event_trigger,
                             probes_serviced=probes_serviced)
