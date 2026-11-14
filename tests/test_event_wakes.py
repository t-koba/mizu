"""roles.<name>.event_wakes gates which wake kinds admit a standalone --event dispatch."""
import contextlib
import dataclasses
from pathlib import Path
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.config import EVENT_WAKE_KINDS, load
from mizu.errors import ConfigError
from mizu.runtime import Engine
from mizu.services import event_watches, render


class EventWakesConfigTests(Fixture):
    def test_default_is_every_kind(self):
        self.assertEqual(self.config.roles["worker"].event_wakes, EVENT_WAKE_KINDS)

    def test_validation(self):
        original = self.file.read_text()
        for value, ok in (('[]', True), ('["wait", "ci"]', True), ('["bogus"]', False),
                          ('["wait", "wait"]', False), ('"wait"', False), ('[1]', False)):
            with self.subTest(value=value):
                self.file.write_text(original.replace("[roles.worker]",
                                                      f"[roles.worker]\nevent_wakes = {value}"))
                if ok:
                    expected = tuple(eval(value))
                    self.assertEqual(load(self.file).roles["worker"].event_wakes, expected)
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)

    def test_empty_list_omits_event_units_only_for_that_role(self):
        exe = Path("/usr/bin/mizu")
        before = render(self.config, self.project, exe, system="linux")
        self.assertTrue(any(k.endswith("-event.service") for k in before))
        names = [n for n in self.project.roles if n in event_watches(self.config, self.project)]
        roles = {**self.config.roles}
        target = names[0]
        roles[target] = dataclasses.replace(roles[target], event_wakes=())
        config = dataclasses.replace(self.config, roles=roles)
        after = render(config, self.project, exe, system="linux")
        self.assertNotIn(target, event_watches(config, self.project))
        gone = set(before) - set(after)
        self.assertTrue(gone)
        self.assertTrue(all("-event." in unit for unit in gone))
        self.assertEqual(set(after) - set(before), set())
        self.assertEqual(len(event_watches(config, self.project)), len(names) - 1)


class EventWakesGateTests(Fixture):
    def _role(self, name, **changes):
        self.config.roles[name] = dataclasses.replace(self.config.roles[name], **changes)

    def _kinds(self):
        """kind -> (role, context patches that make that wake pending)."""
        def p(target, value):
            return patch(target, **({"return_value": value} if not callable(value) else {"side_effect": value}))
        return {
            "wait": ("worker", [p("mizu.runtime.check_jobs.wake_ready", True)]),
            "ci": ("worker", [p("mizu.runtime._ci_due_for", [{"x": 1}])]),
            "operator": ("reviewer", [p("mizu.runtime._operator_woke", True)]),
            "decision": ("worker", [p("mizu.runtime._decision_delivery_state",
                                      {"events": [{"action": "approve"}], "diagnostics": []})]),
            "work_item": ("worker", [patch("mizu.runtime.work_item_events.delivery",
                                           return_value={"events": [{"x": 1}]})]),
            "operator_record": ("worker", [patch(
                "mizu.runtime.operator_records.pending",
                return_value=({"wake_pending": True, "events": []},))]),
            "ready_work": ("worker", [patch("mizu.runtime._ready_work_observation",
                                            return_value=(True, True, False))]),
        }

    def _state(self, role_name, **kw):
        engine = Engine(self.config, driver=ScriptDriver())
        return engine._dispatch_state(self.project, role_name, self.config.roles[role_name],
                                      event_trigger=True, **kw)

    def _worker_event_only(self):
        self._role("worker", daemon=False, interval_seconds=0, calendar="", on_change=False,
                   decision_events=("approve",), work_item_events=())

    def test_each_kind_is_gated_individually(self):
        self._worker_event_only()
        self.project.set_control(wake_generation="")
        for kind, (role, patches) in self._kinds().items():
            for allowed, expect_skip in (((), True), ((kind,), False),
                                         (tuple(k for k in EVENT_WAKE_KINDS if k != kind), True),
                                         (EVENT_WAKE_KINDS, False)):
                with self.subTest(kind=kind, allowed=allowed):
                    self._role(role, event_wakes=allowed)
                    with contextlib.ExitStack() as stack:
                        for item in patches:
                            stack.enter_context(item)
                        state = self._state(role)
                    self.assertEqual("skip" in state, expect_skip)
                    if expect_skip:
                        self.assertEqual(state["skip"]["skipped"], "no-event")
                    else:
                        self.assertEqual(state["_dispatch"].admission,
                                         {"operator_record": "operator_record"}.get(kind, kind))

    def test_listed_kind_labels_even_when_other_wakes_pending(self):
        self._worker_event_only()
        self._role("worker", event_wakes=("wait",))
        with patch("mizu.runtime.check_jobs.wake_ready", return_value=True), \
             patch("mizu.runtime.operator_records.pending",
                   return_value=({"wake_pending": True, "events": []},)):
            state = self._state("worker")
        self.assertEqual(state["_dispatch"].admission, "wait")

    def test_continuation_is_not_a_standalone_wake(self):
        self._worker_event_only()
        self._role("worker", event_wakes=())
        self.assertNotIn("skip", self._state("worker", continuation=True))

    def test_code_change_is_gated(self):
        self._role("consult", on_change=True, event_wakes=())
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, "consult")
        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        changed = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Changed.", run=None, outcome="wait", summary="c")
        self.project.snapshots.publish(changed)
        with patch.object(engine, "_admit", side_effect=AssertionError("must not queue")):
            self.assertEqual(engine.run(self.project, "consult", event_trigger=True)["skipped"],
                             "no-event")
        self._role("consult", event_wakes=("code",))
        result = engine.run(self.project, "consult", event_trigger=True)
        self.assertNotIn("skipped", result)
        self.assertEqual(len(driver.calls), 2)

    def test_default_role_still_admits_on_every_kind(self):
        self._worker_event_only()
        self.project.set_control(wake_generation="")
        with patch("mizu.runtime.check_jobs.wake_ready", return_value=True):
            self.assertNotIn("skip", self._state("worker"))
