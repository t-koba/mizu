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
        "goal_changed": False, "operator_woke": False, "ready_work_changed": False, "ci_wait_due": False, "outcome": "continue",
        "messages_due": False, "routed_events_due": False, "ci_due": False,
        "wait_expired": False, "progress_admits": True,
    }
    base.update(overrides)
    return DaemonFacts(**base)


def _dispatch(**overrides):
    base = {
        "event_trigger": False, "on_change": False, "code_unchanged": False,
        "has_code_event": False,
        "has_message": False, "has_work_item": False,
        "has_wait": False, "has_ci": False, "explicit_wake": False,
        "operator_woke": False,
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
        self.assertEqual((decision.ready, decision.needed), (None, "ready_work_changed"))
        facts = dataclasses.replace(facts, ready_work_changed=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "ci_wait_due"))
        facts = dataclasses.replace(facts, ci_wait_due=False)
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.needed), (None, "outcome"))
        # Queue wake is already collected; it short-circuits outcome collection.
        queued = dataclasses.replace(facts, messages_due=True)
        self.assertEqual((evaluate_daemon(queued).ready,
                          evaluate_daemon(queued).reason), (True, "queue"))
        facts = dataclasses.replace(facts, messages_due=False)
        facts = dataclasses.replace(facts, outcome="done")
        decision = evaluate_daemon(facts)
        self.assertEqual((decision.ready, decision.reason, decision.needed),
                         (False, "done", None))
        # Non-done readiness collects each signal only after its predecessors.
        facts = dataclasses.replace(facts, outcome="wait")
        for name in ("routed_events_due", "ci_due", "wait_expired"):
            decision = evaluate_daemon(facts)
            self.assertEqual((decision.ready, decision.needed), (None, name))
            facts = dataclasses.replace(facts, **{name: False})
        for outcome in ("wait", "continue"):
            decision = evaluate_daemon(dataclasses.replace(facts, outcome=outcome))
            self.assertEqual((decision.ready, decision.needed), (None, "progress_admits"))

    def test_ready_work_changed_precedes_outcomes_but_not_control_or_explicit_wakes(self):
        for outcome in ("done", "wait", "continue", "bounded"):
            facts = _daemon(ready_work_changed=True, outcome=outcome, progress_admits=False)
            self.assertEqual(evaluate(facts).reason, "ready_work")
            self.assertTrue(evaluate(facts).ready)
            for gates in ({"armed": False}, {"paused": True}, {"draining": True}):
                self.assertFalse(evaluate(dataclasses.replace(facts, **gates)).ready)
            self.assertEqual(evaluate(dataclasses.replace(facts, goal_changed=True)).reason, "goal")
            self.assertEqual(evaluate(dataclasses.replace(facts, operator_woke=True)).reason, "operator")


class DispatchEvaluatorTests(unittest.TestCase):
    def test_writer_next_bypasses_unchanged_but_is_not_an_event(self):
        facts = _dispatch(on_change=True, code_unchanged=True, has_next_task=True)
        self.assertEqual(evaluate_dispatch(facts).admission, "next_task")
        event = dataclasses.replace(facts, event_trigger=True)
        self.assertEqual(evaluate_dispatch(event).skipped, "no-event")
        self.assertEqual(evaluate_dispatch(dataclasses.replace(event, has_wait=True)).admission, "wait")
        code_event = dataclasses.replace(event, code_unchanged=False, has_code_event=True)
        self.assertEqual(evaluate_dispatch(code_event).admission, "change")

    def test_inputs_are_immutable(self):
        facts = _dispatch()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            facts.event_trigger = True  # type: ignore[misc]

    def test_event_trigger_needs_substantive_event(self):
        # Snapshot-wide wakes/progress alone do not admit an event trigger.
        decision = evaluate(_dispatch(event_trigger=True, explicit_wake=True,
                                      progress_self_admits=True))
        self.assertEqual((decision.admitted, decision.skipped), (False, "no-event"))
        for flag in ("has_message", "has_work_item", "has_wait", "has_ci"):
            with self.subTest(flag=flag):
                decision = evaluate(_dispatch(event_trigger=True, **{flag: True}))
                self.assertTrue(decision.admitted)
                self.assertIsNone(decision.skipped)

    def test_label_precedence(self):
        cases = [
            ("queue-first", {"has_message": True, "has_work_item": True, "has_wait": True, "has_ci": True}, "queue"),
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

    def test_normalized_wake_and_progress_admit_unchanged(self):
        for wake in ({"explicit_wake": True}, {"progress_self_admits": True}):
            with self.subTest(wake=wake):
                decision = evaluate(_dispatch(on_change=True, code_unchanged=True, **wake))
                self.assertEqual((decision.admitted, decision.admission), (True, "change"))
        operator = evaluate(_dispatch(event_trigger=True, code_unchanged=True,
                                     operator_woke=True, explicit_wake=True))
        self.assertEqual((operator.admitted, operator.admission), (True, "operator"))
        # Generic publisher-only wakes are not event subscriptions.
        self.assertEqual(evaluate(_dispatch(event_trigger=True, explicit_wake=True)).skipped,
                         "no-event")

    def test_role_eligible_ready_work_changed_admits(self):
        for event_trigger in (False, True):
            facts = _dispatch(ready_work_changed=True, code_unchanged=True, on_change=True,
                              event_trigger=event_trigger)
            result = evaluate(facts)
            self.assertEqual((result.admitted, result.admission), (True, "ready_work"))
        self.assertEqual(evaluate(_dispatch(ready_work_changed=True,
                         has_work_item=True, event_trigger=True)).admission, "work_item")

    def test_event_trigger_treats_unobserved_code_change_as_event(self):
        # Unscheduled on_change with unobserved code admits the probe with change.
        decision = evaluate(_dispatch(event_trigger=True, on_change=True,
                                      code_unchanged=False, has_code_event=True))
        self.assertEqual((decision.admitted, decision.skipped, decision.admission),
                         (True, None, "change"))
        # A more-specific subscribed event keeps its own label.
        for flag, label in (("has_message", "queue"), ("has_work_item", "work_item"),
                            ("has_wait", "wait"), ("has_ci", "ci")):
            with self.subTest(flag=flag):
                decision = evaluate(_dispatch(event_trigger=True, on_change=True,
                                              code_unchanged=False, has_code_event=True,
                                              **{flag: True}))
                self.assertEqual((decision.admitted, decision.admission), (True, label))
        # Observed code with no subscribed input still probes cheaply.
        decision = evaluate(_dispatch(event_trigger=True, on_change=True,
                                      code_unchanged=True, has_code_event=False))
        self.assertEqual((decision.admitted, decision.skipped), (False, "no-event"))
        # Roles without on_change never treat code as an event.
        decision = evaluate(_dispatch(event_trigger=True, on_change=False,
                                      code_unchanged=False, has_code_event=False))
        self.assertEqual((decision.admitted, decision.skipped), (False, "no-event"))
        # Scheduled on_change keeps timer discovery: --event stays no-event.
        decision = evaluate(_dispatch(event_trigger=True, on_change=True,
                                      code_unchanged=False, has_code_event=False))
        self.assertEqual((decision.admitted, decision.skipped), (False, "no-event"))

    def test_change_vs_schedule(self):
        # Changed code dispatches for either workspace mode.
        self.assertEqual(evaluate(_dispatch(
            on_change=True, code_unchanged=False)).admission, "change")
        self.assertEqual(evaluate(_dispatch(
            on_change=False, code_unchanged=False)).admission, "schedule")
        # Changed code with timer events keeps the change label; event triggers use events.
        decision = evaluate(_dispatch(on_change=True, code_unchanged=False,
                                      has_message=True))
        self.assertEqual(decision.admission, "change")
        decision = evaluate(_dispatch(on_change=True, code_unchanged=False,
                                      event_trigger=True, has_message=True))
        self.assertEqual(decision.admission, "queue")
        self.assertEqual(evaluate_dispatch(_dispatch()), evaluate(_dispatch()))


class SchedulingParityTests(Fixture):
    def test_event_trigger_no_event_creates_no_run(self):
        Engine(self.config, driver=ScriptDriver()).run(self.project, "searcher")
        before = {path.name for path in (self.project.root / "runs").iterdir()} if (
            self.project.root / "runs").exists() else set()
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "searcher", event_trigger=True)
        self.assertEqual(result["skipped"], "no-event")
        after = {path.name for path in (self.project.root / "runs").iterdir()} if (
            self.project.root / "runs").exists() else set()
        self.assertEqual(before, after)

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

