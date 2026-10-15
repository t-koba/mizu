"""Reference-aware storage accounting preview plus gated reclamation. Offline only."""
import json
import unittest
from support import Fixture
from mizu.errors import Denied
from mizu.storage import audit, reclaim


class StorageAuditTests(Fixture):
    def test_live_snapshot_is_referenced_and_preview_removes_nothing(self):
        live = self.project.snapshots.get()["id"]
        report = audit(self.project)
        self.assertTrue(report["preview_only"])
        self.assertEqual(report["snapshots"]["manifests"], 1)
        self.assertEqual(report["snapshots"]["live"], 1)
        self.assertEqual(report["snapshots"]["referenced"], 1)
        self.assertEqual(report["snapshots"]["unreferenced"], 0)
        self.assertEqual(report["objects"]["orphan_count"], 0)
        self.assertGreater(report["objects"]["count"], 0)
        self.assertGreater(report["snapshots"]["bytes"], 0)
        # Preview changes nothing.
        self.assertEqual(self.project.snapshots.get()["id"], live)
        self.assertEqual(len(list((self.project.root / "snapshots").glob("*.json"))), 1)

    def test_orphan_object_and_unpublished_snapshot_are_reported(self):
        orphan = "f" * 64
        (self.project.root / "objects" / orphan).write_bytes(b"orphan-bytes")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snap = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Preview only",
            run=None, outcome="wait", summary="Unpublished preview")
        report = audit(self.project)
        self.assertEqual(report["snapshots"]["manifests"], 2)
        self.assertEqual(report["snapshots"]["live"], 1)
        self.assertEqual(report["snapshots"]["unreferenced"], 1)
        self.assertIn(snap["id"], report["snapshots"]["unreferenced_sample"])
        self.assertFalse(report["snapshots"]["unreferenced_truncated"])
        self.assertGreater(report["snapshots"]["unreferenced_bytes"], 0)
        self.assertEqual(report["objects"]["orphan_count"], 1)
        self.assertIn(orphan, report["objects"]["orphan_sample"])
        self.assertFalse(report["objects"]["orphan_truncated"])
        self.assertEqual(report["objects"]["orphan_bytes"], len(b"orphan-bytes"))
        # Nothing was removed.
        self.assertTrue((self.project.root / "objects" / orphan).exists())
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").exists())


    def completed_work_item(self):
        proposal = self.project.insights.submit(
            source="worker", title="Retain evidence", body="Verify the snapshot reference", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run-work-item", expected_rev=1)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": "run-work-item"}
        snapshot = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Verified work item", run="run-work-item",
            outcome="done", summary="Completion evidence", verification=verification)
        self.project.snapshots.publish(snapshot)
        completed = self.project.work_items.transition(
            decision["work_item"]["id"], expected_rev=1, status="done", actor="worker",
            snapshot_id=snapshot["id"])
        return decision["work_item"]["id"], snapshot, completed

    def test_completed_work_item_reference_survives_history_pruning_and_reclaim(self):
        item_id, completed_snapshot, _completed = self.completed_work_item()
        self.project.snapshots.history_index = 1
        captured = self.project.snapshots.capture_files(self.project.workspace)
        newer = self.project.snapshots.create(
            captured, goal=self.project.goal, state="New head", run=None,
            outcome="wait", summary="Retire old history")
        self.project.snapshots.publish(newer)
        report = audit(self.project)
        self.assertGreaterEqual(report["references"]["work_item_snapshots"], 1)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertNotIn(completed_snapshot["id"], report["snapshots"]["unreferenced_sample"])
        self.project.set_control(paused=True)
        result = reclaim(self.project, apply=True)
        self.assertNotIn(f"snapshots/{completed_snapshot['id']}.json", result["snapshots"])
        self.assertTrue((self.project.root / "snapshots" / f"{completed_snapshot['id']}.json").is_file())
        self.assertEqual(self.project.work_items.get(item_id)["status"], "done")

    def test_unreadable_work_item_history_blocks_snapshot_reclaim(self):
        proposal = self.project.insights.submit(
            source="worker", title="Malformed history", body="Do not discard evidence", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run-work-item", expected_rev=1)
        item_path = self.project.root / "work-items" / decision["work_item"]["id"] / "2.json"
        item_path.parent.mkdir(parents=True)
        item_path.write_bytes(b"{not-json")
        self.project.set_control(paused=True)
        report = audit(self.project)
        self.assertGreater(report["work_item_events_skipped"], 0)
        with self.assertRaises(Denied):
            reclaim(self.project, apply=True)

    def test_run_reference_protects_snapshot(self):
        from mizu.fs import write_json
        run = self.project.root / "runs" / "probe"
        run.mkdir(parents=True)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snap = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Referenced by a run",
            run="probe", outcome="wait", summary="Run reference")
        write_json(run / "started.json", {"run": "probe", "snapshot": snap["id"]})
        report = audit(self.project)
        self.assertEqual(report["snapshots"]["unreferenced"], 0)
        self.assertGreaterEqual(report["references"]["run_snapshots"], 1)


if __name__ == "__main__":
    unittest.main()


class StorageReclaimTests(Fixture):
    def test_dry_run_lists_candidates_and_removes_nothing(self):
        orphan = "e" * 64
        (self.project.root / "objects" / orphan).write_bytes(b"orphan-bytes")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snap = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Preview only",
            run=None, outcome="wait", summary="Unpublished preview")
        report = reclaim(self.project)
        self.assertFalse(report["applied"])
        self.assertIn(f"snapshots/{snap['id']}.json", report["snapshots"])
        self.assertIn(f"objects/{orphan}", report["objects"])
        self.assertTrue((self.project.root / "objects" / orphan).exists())
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").exists())

    def test_apply_requires_quiescence(self):
        with self.assertRaises(Denied):
            reclaim(self.project, apply=True)

    def test_apply_removes_only_unreferenced_and_writes_audit(self):
        orphan = "e" * 64
        (self.project.root / "objects" / orphan).write_bytes(b"orphan-bytes")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snap = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Unreferenced",
            run=None, outcome="wait", summary="Unpublished preview")
        live = self.project.snapshots.get()["id"]
        self.project.set_control(paused=True)
        result = reclaim(self.project, apply=True)
        self.assertTrue(result["applied"])
        self.assertIn(f"snapshots/{snap['id']}.json", result["snapshots"])
        self.assertIn(f"objects/{orphan}", result["objects"])
        self.assertFalse((self.project.root / "snapshots" / f"{snap['id']}.json").exists())
        self.assertFalse((self.project.root / "objects" / orphan).exists())
        # Live history and its objects survive.
        self.assertTrue((self.project.root / "snapshots" / f"{live}.json").exists())
        audit_path = self.project.root / result["audit"]
        self.assertTrue(audit_path.is_file())
        record = json.loads(audit_path.read_text())
        self.assertIn("applied_at", record)
        # Second reclaim converges to empty.
        again = reclaim(self.project, apply=True)
        self.assertTrue(again["applied"])
        self.assertEqual(again["snapshots"], [])
        self.assertEqual(again["objects"], [])

    def test_apply_protects_run_referenced_snapshot(self):
        from mizu.fs import write_json
        run = self.project.root / "runs" / "probe"
        run.mkdir(parents=True)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snap = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Referenced by a run",
            run="probe", outcome="wait", summary="Run reference")
        write_json(run / "started.json", {"run": "probe", "snapshot": snap["id"]})
        self.project.set_control(paused=True)
        result = reclaim(self.project, apply=True)
        self.assertNotIn(f"snapshots/{snap['id']}.json", result["snapshots"])
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").exists())
