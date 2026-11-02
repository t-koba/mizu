"""Idle decision evaluation skips unchanged sources without a delivery cache."""
import unittest
from unittest.mock import patch

from support import Fixture
from mizu import runtime


class DecisionIdleCacheTests(Fixture):
    def setUp(self):
        super().setUp()
        runtime._DECISION_IDLE_CACHE.clear()
        self.role = self.config.roles["worker"]

    def state(self):
        return runtime._decision_delivery_state(self.project, "worker", self.role)

    def test_unchanged_idle_skips_pending_lock_and_history(self):
        with patch.object(runtime.decision_delivery, "pending",
                          wraps=runtime.decision_delivery.pending) as pending:
            first = self.state()
            self.assertEqual(pending.call_count, 1)
            with patch("mizu.insights.record_lock",
                       side_effect=AssertionError("record lock taken")), \
                    patch("mizu.insights.project_record_lock",
                          side_effect=AssertionError("project lock taken")):
                second = self.state()
            self.assertEqual(pending.call_count, 1)
        self.assertEqual(first["events"], [])
        self.assertEqual(second["events"], [])
        self.assertEqual(second["diagnostics"], [])
        self.assertEqual(second["page"], first["page"])
        self.assertEqual(second["manifest"], ())

    def test_changed_source_invalidates_the_cached_empty_page(self):
        with patch.object(runtime.decision_delivery, "pending",
                          wraps=runtime.decision_delivery.pending) as pending:
            self.state()
            self.assertEqual(pending.call_count, 1)
            self.project.insights.submit(source="worker", title="New", body="No refs",
                                         base_snapshot=None)
            self.state()
            self.assertEqual(pending.call_count, 2)

    def test_non_empty_result_is_never_reused_as_empty(self):
        non_empty = {"shared": False, "events": [{"event": "x"}], "diagnostics": [],
                     "page": {"events": [{"event": "x"}], "pending_count": 1,
                              "truncated": False, "count_truncated": False,
                              "byte_truncated": False}, "manifest": ()}
        with patch.object(runtime.decision_delivery, "pending",
                          side_effect=[non_empty, non_empty]) as pending:
            self.assertTrue(self.state()["events"])
            self.assertTrue(self.state()["events"])
            self.assertEqual(pending.call_count, 2)

    def test_empty_state_shape_matches_the_exact_scan(self):
        exact = runtime.decision_delivery.pending(self.project, self.role)
        with patch.object(runtime.decision_delivery, "authority", return_value=None):
            cached = runtime._empty_decision_state(exact["shared"])
        self.assertEqual(set(cached), set(exact))
        self.assertEqual(cached["events"], exact["events"])
        self.assertEqual(cached["diagnostics"], exact["diagnostics"])
        self.assertEqual(cached["page"], exact["page"])
        self.assertEqual(cached["manifest"], exact["manifest"])


if __name__ == "__main__":
    unittest.main()
