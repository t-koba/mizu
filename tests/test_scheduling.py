"""Focused scheduler-policy migration slice: pure evaluator plus runtime parity."""
import dataclasses
import unittest
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.runtime import Engine, should_run
from mizu.scheduling import (
    DaemonFacts,
    DispatchFacts,
    evaluate,
    evaluate_daemon,
    evaluate_dispatch,
)


def _daemon(**overrides):
    base = {
        "armed": True, "paused": False, "draining": False,
        "goal_changed": False, "operator_woke": False, "ready_work": False, "outcome": "continue",
        "inbox_changed": False, "routed_events_due": False, "ci_due": False,
        "wait_expired": False, "progress_admits": True,
    }
    base.update(overrides)
    return DaemonFacts(**base)


def _dispatch(**overrides):
    base = {
        "event_trigger": False, "on_change": False, "code_unchanged": False,
        "workspace_write": False, "has_decision": False, "has_work_item": False,
        "has_wait": False, "has_ci": False, "goal_changed": False,
        "inbox_changed": False, "operator_woke": False, "wait_expired": False,
        "progress_self_admits": False,
    }
    base.update(overrides)
    return DispatchFacts(**base)


class DaemonEvaluatorTests(unittest.TestCase):
    def test_inputs_are_immutable(self):
        facts = _daemon()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            facts.armed = False  # type: ignore[misc]

    def test_evaluator_requests_only_next_fact(self):
        facts = DaemonFacts(armed=True, paused=False, draining=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "goal_changed"))
        facts = dataclasses.replace(facts, goal_changed=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "operator_woke"))
        facts = dataclasses.replace(facts, operator_woke=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "ready_work"))
        facts = dataclasses.replace(facts, ready_work=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "outcome"))
        facts = dataclasses.replace(facts, outcome="done")
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.reason, decision.needed),
                         (False, "done", None))
        # Non-done readiness collects each signal only after its predecessors.
        facts = dataclasses.replace(facts, outcome="wait")
        for name in ("inbox_changed", "routed_events_due", "ci_due", "wait_expired"):
            decision = evaluate_daemon(facts)
            self.assertEqual((decision.ready, decision.needed), (None, name))
            facts = dataclasses.replace(facts, **{name: False})
        self.assertEqual((evaluate_daemon(facts).ready, evaluate_daemon(facts).reason),
                         (False, "outcome"))
        facts = dataclasses.replace(facts, outcome="continue")
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "progress_admits"))

    def test_ready_work_precedes_outcomes_but_not_control_or_explicit_wakes(self):
        for outcome in ("done", "wait", "continue", "bounded"):
            facts = _daemon(ready_work=True, outcome=outcome, progress_admits=False)
            self.assertEqual(evaluate(facts).reason, "ready_work")
            self.assertTrue(evaluate(facts).ready)
            for gates in ({"armed": False}, {"paused": True}, {"draining": True}):
                self.assertFalse(evaluate(dataclasses.replace(facts, **gates)).ready)
            self.assertEqual(evaluate(dataclasses.replace(facts, goal_changed=True)).reason, "goal")
            self.assertEqual(evaluate(dataclasses.replace(facts, operator_woke=True)).reason, "operator")

    def test_table(self):
        cases = [
            # Control closes before any wake, even with every signal set.
            ("control-disarmed", _daemon(armed=False, goal_changed=True, operator_woke=True,
                                         outcome="continue", inbox_changed=True, routed_events_due=True,
                                         ci_due=True, wait_expired=True, progress_admits=True),
             False, "control"),
            ("control-paused", _daemon(paused=True, inbox_changed=True), False, "control"),
            ("control-draining", _daemon(draining=True, inbox_changed=True), False, "control"),
            # Goal and operator wakes precede the saved done outcome.
            ("goal-before-done", _daemon(goal_changed=True, outcome="done", inbox_changed=True,
                                         routed_events_due=True, ci_due=True, wait_expired=True),
             True, "goal"),
            ("operator-before-done", _daemon(operator_woke=True, outcome="done", inbox_changed=True),
             True, "operator"),
            # Done ignores inbox/events/CI/wait/progress.
            ("done-ignores-news", _daemon(outcome="done", inbox_changed=True, routed_events_due=True,
                                          ci_due=True, wait_expired=True, progress_admits=True),
             False, "done"),
            # Ordered wakes for non-done outcomes.
            ("inbox", _daemon(outcome="wait", inbox_changed=True), True, "inbox"),
            ("events", _daemon(outcome="wait", routed_events_due=True), True, "events"),
            ("ci", _daemon(outcome="wait", ci_due=True), True, "ci"),
            ("wait-expired", _daemon(outcome="wait", wait_expired=True), True, "wait"),
            # Non-continue/bounded outcomes do not reach progress.
            ("wait-not-expired", _daemon(outcome="wait", progress_admits=True), False, "outcome"),
            ("other-outcome", _daemon(outcome="failed", progress_admits=True), False, "outcome"),
            # Progress self-admission for continue/bounded.
            ("continue-admits", _daemon(outcome="continue", progress_admits=True), True, "progress"),
            ("continue-parked", _daemon(outcome="continue", progress_admits=False), False, "parked"),
            ("bounded-admits", _daemon(outcome="bounded", progress_admits=True), True, "progress"),
            ("bounded-parked", _daemon(outcome="bounded", progress_admits=False), False, "parked"),
            # Inbox precedes parked progress.
            ("inbox-before-parked", _daemon(outcome="continue", inbox_changed=True,
                                            progress_admits=False), True, "inbox"),
        ]
        for name, facts, ready, reason in cases:
            with self.subTest(name=name):
                decision = evaluate(facts)
                self.assertEqual((decision.ready, decision.reason), (ready, reason), name)
                self.assertEqual(evaluate_daemon(facts), decision)


class DispatchEvaluatorTests(unittest.TestCase):
    def test_inputs_are_immutable(self):
        facts = _dispatch()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            facts.event_trigger = True  # type: ignore[misc]

    def test_event_trigger_needs_substantive_event(self):
        # Snapshot-wide wakes alone do not admit an event trigger.
        decision = evaluate(_dispatch(event_trigger=True, workspace_write=True,
                                      goal_changed=True, inbox_changed=True,
                                      operator_woke=True, wait_expired=True,
                                      progress_self_admits=True))
        self.assertEqual((decision.admitted, decision.skipped), (False, "no-event"))
        for flag in ("has_decision", "has_work_item", "has_wait", "has_ci"):
            with self.subTest(flag=flag):
                decision = evaluate(_dispatch(event_trigger=True, **{flag: True}))
                self.assertTrue(decision.admitted)
                self.assertIsNone(decision.skipped)

    def test_label_precedence(self):
        cases = [
            ("decision-first", {"has_decision": True, "has_work_item": True, "has_wait": True, "has_ci": True}, "decision"),
            ("work-item", {"has_work_item": True, "has_wait": True, "has_ci": True}, "work_item"),
            ("wait", {"has_wait": True, "has_ci": True}, "wait"),
            ("ci", {"has_ci": True}, "ci"),
        ]
        for name, flags, label in cases:
            with self.subTest(name=name):
                # Unchanged-code dispatch and event-trigger dispatch share precedence.
                for context in ({"code_unchanged": True, "on_change": True},
                                {"event_trigger": True}):
                    decision = evaluate(_dispatch(**{**flags, **context}))
                    self.assertEqual(decision.admission, label, (name, context))

    def test_read_vs_write_unchanged(self):
        # Read-only on-change roles ignore snapshot-wide wakes and progress.
        for wake in ({"goal_changed": True}, {"operator_woke": True}, {"inbox_changed": True},
                     {"wait_expired": True}, {"progress_self_admits": True}):
            with self.subTest(wake=tuple(wake)):
                decision = evaluate(_dispatch(on_change=True, code_unchanged=True,
                                              workspace_write=False, **wake))
                self.assertEqual((decision.admitted, decision.skipped), (False, "unchanged"))
        # Write roles admit on the same wakes with a change label when eventless.
        for wake in ({"goal_changed": True}, {"operator_woke": True}, {"inbox_changed": True},
                     {"wait_expired": True}, {"progress_self_admits": True}):
            with self.subTest(wake=tuple(wake)):
                decision = evaluate(_dispatch(on_change=True, code_unchanged=True,
                                              workspace_write=True, **wake))
                self.assertEqual((decision.admitted, decision.skipped, decision.admission),
                                 (True, None, "change"))
        # Event reasons admit either workspace mode with the event label.
        for workspace_write in (False, True):
            decision = evaluate(_dispatch(on_change=True, code_unchanged=True,
                                          workspace_write=workspace_write, has_wait=True))
            self.assertEqual((decision.admitted, decision.admission), (True, "wait"))

    def test_ready_work_is_a_write_only_wake_source(self):
        for event_trigger in (False, True):
            for writable in (False, True):
                facts = _dispatch(ready_work=True, workspace_write=writable,
                                  code_unchanged=True, on_change=True,
                                  event_trigger=event_trigger)
                result = evaluate(facts)
                self.assertEqual(result.admitted, writable)
                self.assertEqual(result.admission, "ready_work" if writable else None)
                if not writable:
                    self.assertEqual(result.skipped, "no-event" if event_trigger else "unchanged")
        # Keep terminal event routing/labels independent and higher priority.
        self.assertEqual(evaluate(_dispatch(ready_work=True, workspace_write=True,
                         has_work_item=True, event_trigger=True)).admission, "work_item")

    def test_change_vs_schedule(self):
        # Changed code dispatches for either workspace mode.
        self.assertEqual(evaluate(_dispatch(
            on_change=True, code_unchanged=False)).admission, "change")
        self.assertEqual(evaluate(_dispatch(
            on_change=False, code_unchanged=False)).admission, "schedule")
        # Changed code with timer events keeps the change label; event triggers use events.
        decision = evaluate(_dispatch(on_change=True, code_unchanged=False,
                                      has_decision=True))
        self.assertEqual(decision.admission, "change")
        decision = evaluate(_dispatch(on_change=True, code_unchanged=False,
                                      event_trigger=True, has_decision=True))
        self.assertEqual(decision.admission, "decision")
        self.assertEqual(evaluate_dispatch(_dispatch()), evaluate(_dispatch()))


class SchedulingParityTests(Fixture):
    def test_done_ignores_news_but_operator_wakes(self):
        snap = {**self.project.snapshots.get(), "outcome": "done", "wake_generation": "test-wake"}
        self.project.insights.submit(source="searcher", title="News", body="interesting",
                                     base_snapshot=snap["id"])
        self.assertFalse(should_run(self.project, snap))
        self.project.set_control(wake_generation="operator-new")
        self.assertTrue(should_run(self.project, snap))

    def test_done_does_not_read_lower_priority_signals(self):
        snap = {**self.project.snapshots.get(), "outcome": "done",
                "wake_generation": "test-wake", "progress": {}}
        with patch.object(self.project.insights, "generation",
                          side_effect=AssertionError("done must not read inbox generation")), \
             patch("mizu.runtime._role_events_due",
                   side_effect=AssertionError("done must not read routed events")), \
             patch("mizu.runtime._ci_due_for",
                   side_effect=AssertionError("done must not query CI")), \
             patch("mizu.runtime.validate_progress",
                   side_effect=AssertionError("done must not inspect progress")):
            self.assertFalse(should_run(self.project, snap, role_name="worker"))
            self.project.set_control(wake_generation="operator-new")
            self.assertTrue(should_run(self.project, snap, role_name="worker"))
            changed_goal = {**snap, "goal_digest": "stale-goal"}
            self.assertTrue(should_run(self.project, changed_goal, role_name="worker"))

    def test_control_parity(self):
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="searcher", title="News", body="interesting",
                                     base_snapshot=snap["id"])
        self.assertTrue(should_run(self.project, self.project.snapshots.get()))
        for armed, paused, draining in ((False, False, False), (True, True, False), (True, False, True)):
            self.project.set_control(armed=armed, paused=paused, draining=draining)
            with self.subTest(armed=armed, paused=paused, draining=draining):
                self.assertFalse(should_run(self.project, self.project.snapshots.get()))
        self.project.set_control(armed=True, paused=False, draining=False)

    def test_event_trigger_no_event_creates_no_run(self):
        before = {path.name for path in (self.project.root / "runs").iterdir()} if (
            self.project.root / "runs").exists() else set()
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher", event_trigger=True)
        self.assertEqual(result["skipped"], "no-event")
        after = {path.name for path in (self.project.root / "runs").iterdir()} if (
            self.project.root / "runs").exists() else set()
        self.assertEqual(before, after)

    def test_read_unchanged_ignores_inbox(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")
        self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="searcher", title="News", body="interesting",
                                     base_snapshot=snap["id"])
        # Read-only on-change roles keep skipping despite the inbox wake.
        self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")

    def test_write_unchanged_uses_inbox(self):
        self.config.roles["worker"] = dataclasses.replace(
            self.config.roles["worker"], on_change=True)
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "worker")
        self.assertEqual(engine.run(self.project, "worker")["skipped"], "unchanged")
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="searcher", title="News", body="interesting",
                                     base_snapshot=snap["id"])
        result = engine.run(self.project, "worker")
        self.assertNotIn("skipped", result)

    def test_change_and_schedule_labels(self):
        from mizu.fs import read_json
        Engine(self.config, driver=ScriptDriver()).run(self.project, "reviewer")
        Engine(self.config, driver=ScriptDriver()).run(self.project, "searcher")
        admissions = {}
        for run_dir in (self.project.root / "runs").iterdir():
            started = run_dir / "started.json"
            if started.exists():
                record = read_json(started)
                admissions[record["role"]] = record["admission"]
        self.assertEqual(admissions.get("reviewer"), "change")
        self.assertEqual(admissions.get("searcher"), "schedule")

    def test_parked_progress_needs_inbox(self):
        import time
        from mizu.fs import now
        from mizu.runtime import _progress_obligations_digest
        from mizu.snapshot import PROGRESS_SIGNALS, validate_progress
        current = self.project.snapshots.get()
        progress = validate_progress({
            "schema": 1,
            "signals": {name: False for name in PROGRESS_SIGNALS},
            "selected": list(self.config.limits.progress_signals),
            "role": "worker", "has_progress": False, "no_progress_count": 1,
            "obligations_digest": _progress_obligations_digest(
                self.project.insights.pending_obligations("worker")),
            "recorded_at": now(),
        })
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state=current["state"], run=None,
            outcome="continue", summary="Parked progress parity",
            inbox_seen=self.project.insights.generation(),
            wake_generation=self.project.control().get("wake_generation", ""),
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        snap = self.project.snapshots.get()
        # Default allowance parks a no-progress continuation.
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        self.project.insights.submit(source="searcher", title="News", body="interesting",
                                     base_snapshot=snap["id"])
        self.assertTrue(should_run(self.project, snap, role_name="worker"))
