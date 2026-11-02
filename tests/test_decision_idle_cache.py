"""Idle decision evaluation uses a complete source fingerprint; exact scans stay authoritative."""
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

    def test_shared_store_change_invalidates_the_cached_empty_page(self):
        empty = runtime._empty_decision_state(False)
        row = {"event": "e" * 32, "action": "reject"}
        non_empty = {"shared": False, "events": [row], "diagnostics": [],
                     "page": {"events": [row], "pending_count": 1,
                              "truncated": False, "count_truncated": False,
                              "byte_truncated": False}, "manifest": ()}
        with patch.object(runtime.decision_delivery, "pending",
                          side_effect=[empty, non_empty]) as pending:
            self.assertEqual(self.state()["events"], [])
            self.assertEqual(pending.call_count, 1)
            partition = self.project.insights.root / "shared-records" / "insight"
            partition.mkdir(parents=True)
            (partition / "change.json").write_text("{}")
            second = self.state()
            self.assertEqual(pending.call_count, 2)
            self.assertEqual(second["events"], [row])

    def test_dispatch_observes_a_shared_store_row_after_cache_warm(self):
        engine = runtime.Engine(self.config)
        empty = runtime._empty_decision_state(False)
        row = {"event": "f" * 32, "action": "reject"}
        non_empty = {"shared": False, "events": [row], "diagnostics": [],
                     "page": {"events": [row], "pending_count": 1,
                              "truncated": False, "count_truncated": False,
                              "byte_truncated": False}, "manifest": ()}
        with patch.object(runtime.decision_delivery, "pending",
                          side_effect=[empty, non_empty]) as pending:
            engine._dispatch_state(self.project, "worker", self.role,
                                   event_trigger=True)
            self.assertEqual(pending.call_count, 1)
            partition = self.project.insights.root / "shared-records" / "insight"
            partition.mkdir(parents=True)
            (partition / "change.json").write_text("{}")
            state = engine._dispatch_state(self.project, "worker", self.role,
                                           event_trigger=True)
            self.assertEqual(pending.call_count, 2)
            self.assertEqual(state["decision_events"], [row])

    def test_hashed_shared_receipt_store_change_invalidates_the_empty_page(self):
        from mizu.fs import digest
        empty = runtime._empty_decision_state(False)
        non_empty = {"shared": False, "events": [{"event": "a" * 32}],
                     "diagnostics": [], "page": {"events": [{"event": "a" * 32}],
                     "pending_count": 1, "truncated": False,
                     "count_truncated": False, "byte_truncated": False}, "manifest": ()}
        with patch.object(runtime.decision_delivery, "pending",
                          side_effect=[empty, non_empty]) as pending:
            self.assertEqual(self.state()["events"], [])
            receipt = (self.project.insights.root / "shared-record-receipts" /
                       digest(b"worker"))
            receipt.mkdir(parents=True)
            (receipt / "change.json").write_text("{}")
            self.assertEqual(self.state()["events"], [{"event": "a" * 32}])
            self.assertEqual(pending.call_count, 2)

    def test_windows_does_not_reuse_the_empty_page(self):
        with patch.object(runtime._platform, "IS_WINDOWS", True):
            with patch.object(runtime.decision_delivery, "pending",
                              wraps=runtime.decision_delivery.pending) as pending:
                self.state()
                self.state()
        self.assertEqual(pending.call_count, 2)

    def test_entry_metadata_change_invalidates_the_cached_empty_page(self):
        partition = self.project.insights.root / "shared-records" / "insight"
        partition.mkdir(parents=True)
        record = partition / "existing.json"
        record.write_text("{}")
        empty = runtime._empty_decision_state(False)
        row = {"event": "b" * 32, "action": "reject"}
        non_empty = {"shared": False, "events": [row], "diagnostics": [],
                     "page": {"events": [row], "pending_count": 1,
                              "truncated": False, "count_truncated": False,
                              "byte_truncated": False}, "manifest": ()}
        with patch.object(runtime.decision_delivery, "pending",
                          side_effect=[empty, non_empty]) as pending:
            self.assertEqual(self.state()["events"], [])
            record.chmod(0o600)
            self.assertEqual(self.state()["events"], [row])
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
