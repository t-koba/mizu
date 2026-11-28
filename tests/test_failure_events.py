"""Named, durable failure events for config-controlled reaction.

A terminal failure produces a bounded, named fact that a later slice can route
like any other event. This slice covers emission and bounded reads only: the
existing failure brake, control state and dispatch are unchanged.
"""
from support import Fixture
from mizu.errors import ProtocolError
from mizu.fs import read_json, write_json
from mizu.run_health import (
    FAILURE_EVENT_KIND, MAX_ERROR_TEXT, MAX_FAILURE_EVENT_BYTES, MAX_UNIT_CHARS,
    emit_failure_event, read_failure_event,
)
from mizu.runtime import _record_health_failure, _record_missing_finish_only


class FailureEventTests(Fixture):

    def test_emit_writes_named_bounded_fact(self):
        event = emit_failure_event(self.project, "worker", "run-1",
                                   ProtocolError("boom"))
        stored = read_json(self.project.root / "health" / "events" /
                           "worker.json")
        self.assertEqual(stored, event)
        self.assertEqual(stored["kind"], FAILURE_EVENT_KIND)
        self.assertEqual(stored["role"], "worker")
        self.assertEqual(stored["unit"], "run-1")
        self.assertEqual(stored["error_class"], "ProtocolError")
        self.assertEqual(stored["error"], "boom")
        self.assertIn("at", stored)
        self.assertEqual(stored["generation"], 0)

    def test_emission_from_failure_paths(self):
        _record_health_failure(self.project, "worker", "run-1",
                               ProtocolError("terminal"), 3)
        event = read_failure_event(self.project, "worker")
        self.assertEqual(event["kind"], FAILURE_EVENT_KIND)
        self.assertEqual(event["unit"], "run-1")
        self.assertEqual(event["error_class"], "ProtocolError")
        # Brake behaviour unchanged: below threshold, no pause.
        self.assertFalse(self.project.control()["paused"])

    def test_suppressed_missing_finish_also_emits(self):
        from mizu.errors import MissingFinish
        _record_missing_finish_only(self.project, "worker", "run-2",
                                    MissingFinish(configured=2, used=2,
                                                  counts_as_failure=False))
        event = read_failure_event(self.project, "worker")
        self.assertEqual(event["unit"], "run-2")
        self.assertEqual(event["error_class"], "MissingFinish")

    def test_generation_increments_per_role_and_is_isolated(self):
        emit_failure_event(self.project, "worker", "r1", ProtocolError("one"))
        second = emit_failure_event(self.project, "worker", "r2",
                                    ProtocolError("two"))
        self.assertEqual(second["generation"], 1)
        other = emit_failure_event(self.project, "reviewer", "r3",
                                   ProtocolError("three"))
        self.assertEqual(other["generation"], 0)
        self.assertEqual(read_failure_event(self.project, "worker")["generation"], 1)

    def test_error_text_and_unit_are_bounded(self):
        event = emit_failure_event(self.project, "worker", "u" * 500,
                                   ProtocolError("x" * 5000))
        self.assertEqual(len(event["error"]), MAX_ERROR_TEXT)
        self.assertEqual(len(event["unit"]), MAX_UNIT_CHARS)

    def test_explicit_time_is_honoured(self):
        event = emit_failure_event(self.project, "worker", "run-1",
                                   ProtocolError("boom"), at="2026-01-01T00:00:00Z")
        self.assertEqual(event["at"], "2026-01-01T00:00:00Z")

    def test_reader_fails_closed_on_unknown_kind_or_wrong_role(self):
        path = self.project.root / "health" / "events" / "worker.json"
        write_json(path, {"kind": "other", "role": "worker", "error_class": "X",
                          "error": "e", "unit": "u", "at": "t", "generation": 0})
        self.assertIsNone(read_failure_event(self.project, "worker"))

    def test_reader_fails_closed_on_unbounded_fields(self):
        path = self.project.root / "health" / "events" / "worker.json"
        write_json(path, {"kind": FAILURE_EVENT_KIND, "role": "worker",
                          "error_class": "X", "error": "e" * (MAX_ERROR_TEXT + 1),
                          "unit": "u", "at": "t", "generation": 0})
        self.assertIsNone(read_failure_event(self.project, "worker"))
        write_json(path, {"kind": FAILURE_EVENT_KIND, "role": "worker",
                          "error_class": "X", "error": "e", "unit": "u",
                          "at": "t", "generation": -1})
        self.assertIsNone(read_failure_event(self.project, "worker"))

    def test_reader_fails_closed_on_malformed_and_oversized(self):
        path = self.project.root / "health" / "events" / "worker.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        self.assertIsNone(read_failure_event(self.project, "worker"))
        path.write_bytes(b"x" * (MAX_FAILURE_EVENT_BYTES + 1))
        self.assertIsNone(read_failure_event(self.project, "worker"))

    def test_unsafe_role_name_never_writes(self):
        self.assertIsNone(emit_failure_event(self.project, "../escape", "r",
                                             ProtocolError("boom")))
        self.assertIsNone(read_failure_event(self.project, "../escape"))
        events_dir = self.project.root / "health" / "events"
        self.assertFalse(events_dir.exists() or
                         (self.project.root / "health" / "escape.json").exists())
