"""Structured pause_cause for the automatic failure-brake pause.

The selector-controlled brake keeps its counts; this file only covers the
additive machine-readable cause stored alongside the human-readable reason.
"""
from support import Fixture
from mizu.errors import ProtocolError
from mizu.fs import read_json, write_json
from mizu.runtime import _record_health_failure
from mizu.run_health import MAX_ERROR_TEXT


class FailureBrakePauseCauseTests(Fixture):

    def test_health_fact_records_error_class_and_bounded_text(self):
        # A terminal failure is recorded as a bounded fact: class plus capped
        # text, alongside the unit and time already in the health record.
        long_text = "quota exceeded " * 200
        exc = ProtocolError(long_text)
        _record_health_failure(self.project, "worker", "run-fact", exc, 3)
        health = read_json(self.project.root / "health" / "worker.json")
        self.assertEqual(health["error_class"], "ProtocolError")
        self.assertEqual(health["error"], str(exc)[:MAX_ERROR_TEXT])
        self.assertEqual(len(health["error"]), MAX_ERROR_TEXT)
        self.assertEqual(health["last_run"], "run-fact")
        self.assertIn("updated_at", health)
        # Brake behavior is unchanged: below threshold, no pause or cause.
        self.assertFalse(self.project.control()["paused"])
        self.assertNotIn("pause_cause", self.project.control())

    def test_auto_pause_writes_bounded_cause_and_keeps_reason(self):
        exc = ProtocolError("failure detail that must stay out of control.json")
        _record_health_failure(self.project, "worker", "run-1", exc, 1)
        control = self.project.control()
        self.assertTrue(control["paused"])
        self.assertIn("Repeated failures", control["reason"])
        cause = control.get("pause_cause")
        self.assertEqual(cause, {"kind": "failure_brake", "error_class": "ProtocolError",
                                "roles": ["worker"]})
        # Bounded: no error message or unbounded payload leaks into control.
        self.assertNotIn("failure detail", repr(cause))
        self.assertEqual(set(cause), {"kind", "error_class", "roles"})

    def test_corrupt_counter_still_applies_brake(self):
        path = self.project.root / "health" / "worker.json"
        for counter in ("bad", -3):
            with self.subTest(counter=counter):
                self.project.set_control(paused=False)
                write_json(path, {"consecutive_failures": counter})
                _record_health_failure(self.project, "worker", "run-1",
                                       ProtocolError("failure"), 1)
                self.assertEqual(read_json(path)["consecutive_failures"], 1)
                control = self.project.control()
                self.assertTrue(control["paused"])
                self.assertEqual(control.get("pause_cause"),
                                 {"kind": "failure_brake", "error_class": "ProtocolError",
                                  "roles": ["worker"]})

    def test_below_threshold_writes_no_cause(self):
        _record_health_failure(self.project, "worker", "run-1",
                               ProtocolError("failure"), 3)
        self.assertFalse(self.project.control()["paused"])
        self.assertNotIn("pause_cause", self.project.control())

    def test_resume_clears_cause(self):
        _record_health_failure(self.project, "worker", "run-1",
                               ProtocolError("failure"), 1)
        self.assertIn("pause_cause", self.project.control())
        self.project.set_control(paused=False, reason="Operator resumed")
        control = self.project.control()
        self.assertFalse(control["paused"])
        self.assertNotIn("pause_cause", control)

    def test_manual_pause_clears_cause(self):
        _record_health_failure(self.project, "worker", "run-1",
                               ProtocolError("failure"), 1)
        self.assertIn("pause_cause", self.project.control())
        self.project.set_control(paused=True, reason="Operator paused")
        control = self.project.control()
        self.assertTrue(control["paused"])
        self.assertNotIn("pause_cause", control)

    def test_unrelated_update_preserves_cause(self):
        _record_health_failure(self.project, "worker", "run-1",
                               ProtocolError("failure"), 1)
        self.project.set_control(draining=True, reason="Operator requested project drain")
        control = self.project.control()
        self.assertTrue(control["paused"])
        self.assertEqual(control.get("pause_cause"),
                         {"kind": "failure_brake", "error_class": "ProtocolError",
                          "roles": ["worker"]})
