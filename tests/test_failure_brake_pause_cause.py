"""Structured pause_cause for the automatic failure-brake pause.

The selector-controlled brake keeps its counts; this file only covers the
additive machine-readable cause stored alongside the human-readable reason.
"""
from support import Fixture
from mizu.errors import ProtocolError
from mizu.runtime import _record_health_failure


class FailureBrakePauseCauseTests(Fixture):
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
