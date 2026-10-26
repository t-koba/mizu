"""Host-only source projection is not a delivery or receipt authority."""
import contextlib
import copy
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest, read_json, write_json
from mizu.insights import Insights, INSIGHT_DECISION_POLICY
from mizu.shared_records import SharedRecords
from mizu.smoke_refs import record_lock


class DecisionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.insights = Insights(self.root)
        self.store = SharedRecords(self.root, {"insight.decision": INSIGHT_DECISION_POLICY})

    def decide(self, index=0, *, action="reject", source="reviewer", evidence=False):
        item = self.insights.submit(source=source, title=f"Finding {index}",
                                    body=f"Exact body {index}", base_snapshot=None,
                                    origin="opaque origin, not writer authority")
        if evidence:
            self.insights.attach_evidence(item["id"], source=source, text="Exact evidence",
                                          run="test", expected_rev=1)
        self.insights.decide(item["id"], action, "Exact reason", "Exact revisit", "test",
                             expected_rev=1, **({"work_item_intent": "none"}
                                               if action in ("accept", "modify") else {}))
        return item

    def rows(self):
        return self.insights.decision_events("reviewer", ("accept", "modify", "reject", "defer"),
                                             sources="any", limit=100)

    def record_path(self, record_id):
        return self.root / "shared-records" / f"{digest(record_id.encode())}.json"

    def test_exact_payload_deterministic_digest_and_replay_after_restart(self):
        self.decide(evidence=True)
        row = self.rows()[0]
        payload = {key: value for key, value in row.items() if key != "ack_token"}
        record_id = f"insight.decision/{row['event']}"
        expected = {"schema": 1, "id": record_id, "kind": "insight.decision",
                    "created_at": row["decided_at"], "writer": "host.insights",
                    "subject": {key: row[key] for key in ("event", "insight", "rev")},
                    "payload": payload}
        projected = self.insights.project_decision_records()
        self.assertEqual(projected, {record_id: digest(canonical(expected))})
        self.assertEqual(self.store.get(record_id), (expected, projected[record_id]))
        path = self.record_path(record_id)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(Insights(self.root).project_decision_records(), projected)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.rows(), [row])
        self.assertFalse((self.root / "shared-record-receipts").exists())
        self.assertFalse((self.root / "decision-receipts").exists())

    def test_all_current_actions_sources_and_205_rows_ignore_legacy_receipts(self):
        with patch("mizu.insights.now", return_value="2026-01-01T01:00:00+01:00"):
            for i in range(205):
                self.decide(i, action=("accept", "modify", "reject", "defer")[i % 4],
                            source="reviewer" if i % 2 else "searcher")
        expected_ids = {f"insight.decision/{path.stem}" for path in
                        (self.root / "decision-history").glob("*.json")}
        rows = self.rows()
        self.insights.acknowledge_decisions("reviewer", rows[:10], sources="any")
        # Even a corrupt exact legacy receipt must not affect source projection.
        receipt = self.root / "decision-receipts/reviewer" / f"{rows[0]['ack_token']}.json"
        receipt.write_bytes(b"not json")
        before = self.insights.decision_events_with_diagnostics(
            "reviewer", ("accept", "modify", "reject", "defer"), sources="any", limit=100)
        write_json(self.root / "decision-cursors/reviewer.json", {"acknowledged_at": "2099-01-01"})
        projected = self.insights.project_decision_records()
        self.assertEqual(set(projected), expected_ids)
        self.assertEqual(len(projected), 205)
        self.assertEqual(self.insights.decision_events_with_diagnostics(
            "reviewer", ("accept", "modify", "reject", "defer"), sources="any", limit=100), before)
        self.assertEqual(receipt.read_bytes(), b"not json")
        self.assertFalse((self.root / "shared-record-receipts").exists())

    def test_stale_withdrawn_and_superseded_omitted_history_retained(self):
        stale = self.decide(0)
        withdrawn = self.decide(1)
        deferred = self.decide(2, action="defer")
        initial = self.insights.project_decision_records()
        retained = {key: self.record_path(key).read_bytes() for key in initial}
        self.insights.revise(stale["id"], source="reviewer", title="Revised",
                             body="New evidence", base_snapshot=None, expected_rev=1)
        self.insights.withdraw(withdrawn["id"], source="reviewer", reason="Withdrawn", expected_rev=1)
        self.insights.decide(deferred["id"], "reject", "Final reason", "", "test", expected_rev=1)
        current = self.insights.project_decision_records()
        self.assertEqual(len(current), 1)
        self.assertTrue(set(current).isdisjoint(initial))
        self.assertEqual({key: self.record_path(key).read_bytes() for key in initial}, retained)
        self.assertEqual(len(list((self.root / "shared-records").glob("*.json"))), 4)
        self.assertEqual(set(current), {f"insight.decision/{self.rows()[0]['event']}"})
        # A fresh projection must omit the same stale/withdrawn history too.
        with tempfile.TemporaryDirectory() as other:
            for name in ("inbox", "decisions", "decision-history"):
                shutil.copytree(self.root / name, Path(other) / name)
            self.assertEqual(Insights(Path(other)).project_decision_records(), current)

    def test_malformed_and_conflicting_source_fails_closed(self):
        item = self.decide()
        row = self.rows()[0]
        history = self.root / "decision-history" / f"{row['event']}.json"
        pointer = self.root / "decisions" / f"{item['id']}.json"
        inbox = self.root / "inbox" / f"{item['id']}.json"
        originals = {path: path.read_bytes() for path in (history, pointer, inbox)}
        cases = [(history, b"not json"), (history, b"[]"),
                 (history, {**read_json(history), "decision_key": "0" * 32}),
                 (pointer, {**read_json(pointer), "reason": "Conflicting reason"}),
                 (pointer, {**read_json(pointer), "rev": "1"}),
                 (inbox, {**read_json(inbox), "rev": True}),
                 (inbox, {**read_json(inbox), "evidence": "not evidence"}),
                 (inbox, {**read_json(inbox), "title": []})]
        for path, bad in cases:
            with self.subTest(path=path.name, bad=bad):
                path.write_bytes(bad if isinstance(bad, bytes) else canonical(bad))
                try:
                    with self.assertRaises(Denied):
                        self.insights.project_decision_records()
                    self.assertFalse((self.root / "shared-records").exists())
                finally:
                    path.write_bytes(originals[path])
        # A keyed event cannot be copied to another UUID and projected twice.
        copied = history.with_name("0" * 32 + ".json")
        copied.write_bytes(originals[history])
        with self.assertRaises(Denied):
            self.insights.project_decision_records()
        copied.unlink()

        # Deferred events have random history filenames; duplicate the exact
        # current record under another filename to ensure the source binding
        # cannot create a second shared identity.
        deferred = self.decide(1, action="defer")
        event = next(row["event"] for row in self.rows()
                     if row["insight"] == deferred["id"])
        original_path = self.root / "decision-history" / f"{event}.json"
        duplicate = original_path.with_name("e" * 32 + ".json")
        duplicate.write_bytes(original_path.read_bytes())
        with self.assertRaises(Denied):
            self.insights.project_decision_records()

    def test_closed_policy_and_authenticated_writer(self):
        self.decide()
        projected = self.insights.project_decision_records()
        envelope, _ = self.store.get(next(iter(projected)))
        for field, value in (("extra", "not allowed"), ("rev", True),
                             ("action", "withdraw"), ("action", []), ("ack_token", "forged")):
            invalid = copy.deepcopy(envelope)
            invalid["payload"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(Denied):
                self.store.put(invalid, writer="host.insights")
        invalid = copy.deepcopy(envelope)
        invalid["subject"]["event"] = "0" * 32
        with self.assertRaises(Denied):
            self.store.put(invalid, writer="host.insights")
        with self.assertRaises(Denied):
            self.store.put(envelope, writer="model.reviewer")
        with self.assertRaises(Denied):
            SharedRecords(self.root).get(envelope["id"])

    def test_corrupt_and_conflicting_store_not_repaired(self):
        self.decide()
        projected = self.insights.project_decision_records()
        key = next(iter(projected))
        path = self.record_path(key)
        original = path.read_bytes()
        changed = json.loads(original)
        changed["payload"]["title"] = "Conflicting immutable content"
        for bad in (b"not json", canonical(changed)):
            path.write_bytes(bad)
            with self.assertRaises(Denied):
                self.insights.project_decision_records()
            self.assertEqual(path.read_bytes(), bad)
        path.write_bytes(original)

    def test_unsafe_source_and_store_paths_fail_closed(self):
        item = self.decide()
        projected = self.insights.project_decision_records()
        path = self.record_path(next(iter(projected)))
        original = path.read_bytes()
        inbox = self.root / "inbox" / f"{item['id']}.json"
        moved_inbox = inbox.with_suffix(".retained")
        inbox.rename(moved_inbox)
        try:
            inbox.symlink_to(moved_inbox)
        except (OSError, NotImplementedError):
            moved_inbox.rename(inbox)
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(Denied):
            self.insights.project_decision_records()
        inbox.unlink()
        moved_inbox.rename(inbox)
        directory = path.parent
        moved = directory.with_name("retained")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(Denied):
            self.insights.project_decision_records()
        self.assertEqual((moved / path.name).read_bytes(), original)

    def test_current_payload_drift_cannot_rewrite_projected_event(self):
        item = self.decide(action="defer")
        projected = self.insights.project_decision_records()
        path = self.record_path(next(iter(projected)))
        original = path.read_bytes()
        self.insights.attach_evidence(item["id"], source="reviewer", text="Later evidence",
                                      run="test", expected_rev=1)
        with self.assertRaises(Denied):
            self.insights.project_decision_records()
        self.assertEqual(path.read_bytes(), original)

    def test_bound_before_writes_and_retry_after_partial_store_failure(self):
        self.decide(0)
        self.decide(1)
        for limit in (0, True, 513):
            with self.assertRaises(Denied):
                self.insights.project_decision_records(limit=limit)
        with self.assertRaises(LimitExceeded):
            self.insights.project_decision_records(limit=1)
        self.assertFalse((self.root / "shared-records").exists())
        put = SharedRecords.put
        count = 0

        def interrupted(store, envelope, *, writer):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Interrupted projection")
            return put(store, envelope, writer=writer)

        with patch.object(SharedRecords, "put", interrupted), self.assertRaises(OSError):
            self.insights.project_decision_records()
        retained = {p: p.read_bytes() for p in (self.root / "shared-records").glob("*.json")}
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(self.insights.project_decision_records()), 2)
        self.assertEqual({p: p.read_bytes() for p in retained}, retained)

    def test_selection_and_writes_share_source_record_lock(self):
        self.decide()
        held = False
        put = SharedRecords.put
        select = Insights._current_decision_event

        @contextlib.contextmanager
        def observed_lock(root):
            nonlocal held
            with record_lock(root):
                held = True
                try:
                    yield
                finally:
                    held = False

        def observed_select(insights, *args, **kwargs):
            self.assertTrue(held)
            return select(insights, *args, **kwargs)

        def observed_put(store, *args, **kwargs):
            self.assertTrue(held)
            return put(store, *args, **kwargs)

        with patch("mizu.insights.record_lock", observed_lock), \
                patch.object(Insights, "_current_decision_event", observed_select), \
                patch.object(SharedRecords, "put", observed_put):
            self.assertEqual(len(self.insights.project_decision_records()), 1)
        self.assertFalse(held)


if __name__ == "__main__":
    unittest.main()
