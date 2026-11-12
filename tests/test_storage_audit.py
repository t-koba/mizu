"""Reference-aware storage accounting preview plus gated reclamation. Offline only."""
import io
import json
import os
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from support import Fixture, bind_delivery, forget_ledger
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest, safe_read_stream
from support import history_write_json as write_json
from mizu import fs
from mizu.snapshot_manifest import decoded_path_limit, file_member_limit, read_manifest
from mizu.storage import (_AUDIT_JSON_BYTES,
                          _session_accounting, audit, reclaim)


def _posix_path_limit(root):
    """Resolve a typical POSIX PATH_MAX under explicit semantics on every host."""
    with mock.patch('mizu.snapshot_manifest._platform.IS_WINDOWS', False), \
            mock.patch('mizu.snapshot_manifest._posix_path_max', return_value=4096):
        return decoded_path_limit(root)


class SessionEntry:
    """Minimal scandir entry; callbacks check consumption order without buffering."""
    def __init__(self, path, *, directory=False, on_stat=None):
        self.path = str(path)
        self.name = path.name
        self.directory = directory
        self.on_stat = on_stat

    def is_symlink(self):
        return False

    def is_dir(self, *, follow_symlinks=True):
        assert not follow_symlinks
        return self.directory

    def is_file(self, *, follow_symlinks=True):
        assert not follow_symlinks
        return not self.directory

    def stat(self, *, follow_symlinks=True):
        assert not follow_symlinks
        if self.on_stat:
            self.on_stat()
        return SimpleNamespace(st_size=7)


class SessionIterator:
    """Scandir-shaped lazy iterator with observable resource cleanup."""
    def __init__(self, entries):
        self.entries = iter(entries)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.entries)

    def close(self):
        self.closed = True
        self.entries.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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


    def synthetic_drop(self, serial):
        """Create a synthetic legacy decision and its valid drop sidecar."""
        event_id = f"{serial:032x}"
        event = {"id": "synthetic-insight", "action": "accept", "rev": 1,
                 "reason": "Synthetic storage-audit coverage", "revisit": "",
                 "run": "synthetic-run", "created_at": "2024-01-01T00:00:00+00:00"}
        history = self.project.root / "decision-history" / f"{event_id}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        # The reconciliation below indexes exactly this event's entry.
        fs.write_json(history, event)
        record = self.project.work_items.reconcile(
            event_id, expected_action="accept", outcome="drop",
            reason="Synthetic drop for audit coverage", actor="worker")["record"]
        return self.project.root / "work-items" / f"wi_{event_id}", record

    def unreferenced_snapshot(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        return self.project.snapshots.create(
            captured, goal=self.project.goal, state="Synthetic storage audit",
            run=None, outcome="wait", summary="Synthetic unreferenced snapshot")

    def test_synthetic_drop_sidecar_is_covered_by_audit_and_reclaim(self):
        item_dir, _record = self.synthetic_drop(1)
        snapshot = self.unreferenced_snapshot()

        report = audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertIn(snapshot["id"], report["snapshots"]["unreferenced_sample"])
        self.assertEqual(report["references"]["work_item_snapshots"], 0)

        self.project.set_control(paused=True)
        result = reclaim(self.project, apply=True)
        self.assertEqual(result["work_item_events_skipped"], 0)
        self.assertIn(f"snapshots/{snapshot['id']}.json", result["snapshots"])
        self.assertFalse((self.project.root / "snapshots" / f"{snapshot['id']}.json").exists())
        self.assertTrue((item_dir / "legacy.json").is_file())

    def test_synthetic_open_legacy_sidecar_uses_item_reconstruction(self):
        event_id = f"{5:032x}"
        event = {"id": "synthetic-insight", "action": "accept", "rev": 1,
                 "reason": "Synthetic open-item coverage", "revisit": "",
                 "run": "synthetic-run", "created_at": "2024-01-01T00:00:00+00:00"}
        history = self.project.root / "decision-history" / f"{event_id}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        write_json(history, event)
        opened = self.project.work_items.reconcile(
            event_id, expected_action="accept", outcome="open",
            reason="Synthetic open item", actor="worker",
            title="Synthetic audited item")["record"]["work_item"]

        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"],
                        "run": "synthetic-run"}
        snapshot = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Synthetic completion",
            run="synthetic-run", outcome="done", summary="Synthetic completion evidence",
            verification=verification)
        self.project.snapshots.publish(snapshot)
        bind_delivery(self.project, opened["id"], snapshot["id"])
        self.project.work_items.transition(
            opened["id"], expected_rev=3, status="done", actor="worker",
            snapshot_id=snapshot["id"])

        report = audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertGreaterEqual(report["references"]["work_item_snapshots"], 1)
        self.assertNotIn(snapshot["id"], report["snapshots"]["unreferenced_sample"])

    def test_synthetic_malformed_incomplete_and_unexpected_drop_records_fail_closed(self):
        malformed_dir, _ = self.synthetic_drop(2)
        incomplete_dir, record = self.synthetic_drop(3)
        unexpected_dir, _ = self.synthetic_drop(4)
        (malformed_dir / "legacy.json").write_bytes(b"{not-json")
        incomplete = dict(record)
        incomplete.pop("reason")
        write_json(incomplete_dir / "legacy.json", incomplete)
        temporary = unexpected_dir / ".new-abcdefgh"
        temporary.write_bytes(b"synthetic interrupted append")
        (unexpected_dir / "2.json").write_bytes(b"{}")

        snapshot = self.unreferenced_snapshot()
        self.project.set_control(paused=True)
        report = audit(self.project)
        self.assertGreaterEqual(report["work_item_events_skipped"], 3)
        with self.assertRaises(Denied):
            reclaim(self.project, apply=True)
        self.assertTrue((self.project.root / "snapshots" / f"{snapshot['id']}.json").exists())
        # Audit is read-only: unexpected history and its temporary remain.
        self.assertTrue(temporary.exists())
        self.assertTrue((unexpected_dir / "2.json").exists())

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
        bind_delivery(self.project, decision["work_item"]["id"], snapshot["id"])
        completed = self.project.work_items.transition(
            decision["work_item"]["id"], expected_rev=3, status="done", actor="worker",
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

    def test_storage_audit_flags_temp_until_locked_work_item_recovery(self):
        proposal = self.project.insights.submit(
            source="worker", title="Interrupted append", body="Recover the temporary", base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run-work-item", expected_rev=1)
        item_id = decision["work_item"]["id"]
        item_dir = self.project.root / "work-items" / item_id
        item_dir.mkdir(parents=True)
        temporary = item_dir / ".new-leftover"
        temporary.write_bytes(b"partial")
        self.assertGreater(audit(self.project)["work_item_events_skipped"], 0)
        forget_ledger(self.project.root)  # A locked rebuild or write recovers it.
        self.assertEqual(self.project.work_items.get(item_id), decision["work_item"])
        self.assertFalse(temporary.exists())
        self.assertEqual(audit(self.project)["work_item_events_skipped"], 0)

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
        from support import history_write_json as write_json
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

    def test_integrity_invalid_live_manifest_blocks_reclaim_of_referenced_objects(self):
        manifest = self.project.snapshots.get()
        live = manifest["id"]
        manifest_path = self.project.root / "snapshots" / f"{live}.json"
        original_objects = {
            entry["sha256"]: (self.project.root / "objects" / entry["sha256"]).read_bytes()
            for entry in manifest["files"].values()
        }
        self.assertTrue(original_objects)
        manifest["files"] = {}
        write_json(manifest_path, manifest)

        with self.assertRaisesRegex(Denied, "Snapshot integrity failure"):
            self.project.snapshots.get(live)
        self.project.set_control(paused=True)
        preview = reclaim(self.project)
        self.assertEqual(preview["manifest_skipped"], 1)
        self.assertEqual(audit(self.project)["snapshots"]["skipped"], 1)
        for sha in original_objects:
            self.assertIn(f"objects/{sha}", preview["objects"])
        with self.assertRaisesRegex(Denied, "snapshot manifest"):
            reclaim(self.project, apply=True)
        self.assertTrue(manifest_path.is_file())
        for sha, data in original_objects.items():
            self.assertEqual((self.project.root / "objects" / sha).read_bytes(), data)

    def test_oversized_live_manifest_blocks_reclaim_of_referenced_objects(self):
        live = self.project.snapshots.get()["id"]
        manifest_path = self.project.root / "snapshots" / f"{live}.json"
        manifest = json.loads(manifest_path.read_text())
        object_ids = [entry.get("sha256") for entry in manifest["files"].values()
                      if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)]
        self.assertTrue(object_ids)
        object_path = self.project.root / "objects" / object_ids[0]
        self.assertTrue(object_path.is_file())

        manifest["oversized_padding"] = "x" * (_AUDIT_JSON_BYTES + 128)
        write_json(manifest_path, manifest)
        self.assertGreater(manifest_path.stat().st_size, _AUDIT_JSON_BYTES)
        self.project.set_control(paused=True)

        preview = reclaim(self.project)
        self.assertEqual(preview["manifest_skipped"], 1)
        self.assertIn(f"objects/{object_ids[0]}", preview["objects"])
        with self.assertRaisesRegex(Denied, "snapshot manifest"):
            reclaim(self.project, apply=True)
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(object_path.is_file())

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
        from support import history_write_json as write_json
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


    def hidden_snapshot(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        return self.project.snapshots.create(
            captured, goal=self.project.goal, state="Hidden reference",
            run=None, outcome="wait", summary="Hidden reference snapshot")

    def test_oversized_run_result_blocks_reclaim_of_hidden_reference(self):
        snap = self.hidden_snapshot()
        run = self.project.root / "runs" / "hidden"
        run.mkdir(parents=True)
        write_json(run / "result.json", {"run": "hidden", "snapshot": snap["id"],
                                         "padding": "x" * (_AUDIT_JSON_BYTES + 128)})
        self.assertGreater((run / "result.json").stat().st_size, _AUDIT_JSON_BYTES)
        report = audit(self.project)
        self.assertGreaterEqual(report["reference_records_skipped"], 1)
        preview = reclaim(self.project)
        self.assertGreaterEqual(preview["reference_records_skipped"], 1)
        self.project.set_control(paused=True)
        with self.assertRaisesRegex(Denied, "reference records"):
            reclaim(self.project, apply=True)
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").is_file())

    def test_oversized_history_generation_blocks_reclaim_of_hidden_reference(self):
        snap = self.hidden_snapshot()
        histories = self.project.root / "histories"
        histories.mkdir(parents=True, exist_ok=True)
        hidden = histories / ("c" * 64 + ".json")
        write_json(hidden, [snap["id"], "x" * (_AUDIT_JSON_BYTES + 128)])
        self.assertGreater(hidden.stat().st_size, _AUDIT_JSON_BYTES)
        report = audit(self.project)
        self.assertGreaterEqual(report["reference_records_skipped"], 1)
        preview = reclaim(self.project)
        self.assertGreaterEqual(preview["reference_records_skipped"], 1)
        self.project.set_control(paused=True)
        with self.assertRaisesRegex(Denied, "reference records"):
            reclaim(self.project, apply=True)
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").is_file())

    def test_oversized_artifact_evidence_blocks_reclaim_of_hidden_reference(self):
        snap = self.hidden_snapshot()
        evidence_dir = self.project.root / "artifacts" / "hidden"
        evidence_dir.mkdir(parents=True)
        write_json(evidence_dir / "evidence.json", {"snapshot": snap["id"],
                                                    "padding": "x" * (_AUDIT_JSON_BYTES + 128)})
        self.assertGreater((evidence_dir / "evidence.json").stat().st_size, _AUDIT_JSON_BYTES)
        report = audit(self.project)
        self.assertGreaterEqual(report["reference_records_skipped"], 1)
        preview = reclaim(self.project)
        self.assertGreaterEqual(preview["reference_records_skipped"], 1)
        self.project.set_control(paused=True)
        with self.assertRaisesRegex(Denied, "reference records"):
            reclaim(self.project, apply=True)
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").is_file())

    def test_oversized_inbox_record_blocks_reclaim_of_hidden_reference(self):
        snap = self.hidden_snapshot()
        inbox = self.project.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        hidden = inbox / "hidden.json"
        write_json(hidden, {"base_snapshot": snap["id"],
                            "padding": "x" * (_AUDIT_JSON_BYTES + 128)})
        self.assertGreater(hidden.stat().st_size, _AUDIT_JSON_BYTES)
        report = audit(self.project)
        self.assertGreaterEqual(report["reference_records_skipped"], 1)
        preview = reclaim(self.project)
        self.assertGreaterEqual(preview["reference_records_skipped"], 1)
        self.project.set_control(paused=True)
        with self.assertRaisesRegex(Denied, "reference records"):
            reclaim(self.project, apply=True)
        self.assertTrue((self.project.root / "snapshots" / f"{snap['id']}.json").is_file())

    def assert_reference_scan_blocked(self, skipped=1):
        """Every incomplete scan must stop before even the first removal."""
        self.assertEqual(audit(self.project)["reference_records_skipped"], skipped)
        self.assertEqual(reclaim(self.project)["reference_records_skipped"], skipped)
        self.project.set_control(paused=True)
        with mock.patch.object(Path, "unlink", side_effect=AssertionError("premature removal")):
            with self.assertRaisesRegex(Denied, "reference records"):
                reclaim(self.project, apply=True)

    def test_null_reference_records_are_skipped_not_missing(self):
        current = self.project.root / "current.json"
        pointer = json.loads(current.read_text())
        history = self.project.root / "histories" / (pointer["history"] + ".json")
        run = self.project.root / "runs" / "null-record"
        run.mkdir(parents=True)
        artifacts = self.project.root / "artifacts"
        evidence = artifacts / "null-record" / "evidence.json"
        inbox = self.project.root / "inbox" / "null-record.json"
        inbox.parent.mkdir(exist_ok=True)
        for path in (current, history, run / "result.json", artifacts / "latest.json", evidence, inbox):
            with self.subTest(path=path.relative_to(self.project.root)):
                original = path.read_bytes() if path.exists() else None
                write_json(path, None)
                self.assert_reference_scan_blocked()
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
                if path == evidence:
                    path.parent.rmdir()

    def test_invalid_reference_fields_are_skipped_but_null_is_optional(self):
        root = self.project.root
        paths = ((root / "runs" / "invalid" / "result.json", "snapshot"),
                 (root / "artifacts" / "latest.json", "snapshot"),
                 (root / "artifacts" / "invalid" / "evidence.json", "snapshot"),
                 (root / "inbox" / "invalid.json", "base_snapshot"))
        for path, field in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            for value in ("bad-id", 7, [], {}):
                with self.subTest(path=path.relative_to(root), value=value):
                    write_json(path, {field: value})
                    self.assert_reference_scan_blocked()
            write_json(path, {field: None})
            if "artifacts" in path.relative_to(root).parts:
                self.assert_reference_scan_blocked()
            else:
                self.assertEqual(audit(self.project)["reference_records_skipped"], 0)
            path.unlink()
            if path.name == "evidence.json":
                path.parent.rmdir()

    def test_oversized_snapshot_metadata_stops_before_reading_full_field(self):
        path = self.project.root / "snapshots" / ("a" * 64 + ".json")
        # Do not allocate the oversized fixture in the test either.
        with path.open("wb") as stream:
            stream.write(b'{"padding":"')
            for _ in range(16):
                stream.write(b'x' * _AUDIT_JSON_BYTES)
            stream.write(b'"}')
        read_bytes = []

        class Counted:
            def __init__(self, stream):
                self.stream, self.total = stream, 0

            def read(self, size):
                data = self.stream.read(size)
                self.total += len(data)
                return data

            def tell(self):
                return self.stream.tell()

            def seek(self, offset, whence=0):
                return self.stream.seek(offset, whence)

        def tracked(root, name, maximum, consume):
            if name != "snapshots/" + path.name:
                return safe_read_stream(root, name, maximum, consume)

            def count(stream):
                wrapped = Counted(stream)
                try:
                    return consume(wrapped)
                finally:
                    read_bytes.append(wrapped.total)
            return safe_read_stream(root, name, maximum, count)

        tracemalloc.start()
        try:
            with mock.patch("mizu.snapshot.safe_read_stream", side_effect=tracked):
                self.assertEqual(audit(self.project)["snapshots"]["skipped"], 1)
                from mizu.storage import _automatic_snapshot_gc
                result = _automatic_snapshot_gc(self.project, 2_000_000_000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * _AUDIT_JSON_BYTES)
        self.assertEqual(result["status"], "refused", result)
        self.assertTrue(path.exists())
        self.assertTrue(read_bytes)
        self.assertLessEqual(max(read_bytes), _AUDIT_JSON_BYTES + 8192)

    def test_streamed_manifest_content_and_textual_contract(self):
        record = self.project.snapshots.get()
        # Exercise UTF-8, escapes and chunk boundaries; metadata order/spacing
        # need not be canonical, but the large goal token must be.
        goal = ('é😀\\\"' + ''.join(map(chr, range(32)))) * 300
        record['goal'], record['goal_digest'] = goal, digest(goal.encode())
        record['id'] = digest(canonical({k: v for k, v in record.items() if k != 'id'}))
        raw = json.dumps(record, ensure_ascii=False, indent=2).encode()
        parsed, goal_digest = read_manifest(io.BytesIO(raw), _AUDIT_JSON_BYTES, record['id'],
                                            self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        self.assertNotIn('goal', parsed)
        self.assertEqual(goal_digest, record['goal_digest'])
        for bad in (raw.replace(b'"goal":', b'"goal":"duplicate", "goal":', 1),
                    raw.replace('é'.encode(), b'\\u00e9', 1),
                    raw.replace(b'"goal": "', b'"goal": "changed', 1),
                    raw + b'trailing'):
            with self.subTest(prefix=bad[:40]), self.assertRaises((Denied, ValueError)):
                read_manifest(io.BytesIO(bad), _AUDIT_JSON_BYTES, record['id'],
                              self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        # Invalid continuation bytes must not become an ever-growing tail.
        with self.assertRaises((Denied, ValueError)):
            read_manifest(io.BytesIO(b'{"goal":"' + b'\x80' * 32768),
                          _AUDIT_JSON_BYTES, record['id'], self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))

    def test_file_map_whitespace_run_is_bounded(self):
        record = self.project.snapshots.get()
        raw = canonical(record)
        marker = b'"files":{'
        self.assertIn(marker, raw)
        for count in (1024, 1025):
            changed = raw.replace(marker, marker + b' ' * count, 1)
            with self.subTest(count=count):
                if count == 1024:
                    parsed, _ = read_manifest(
                        io.BytesIO(changed), _AUDIT_JSON_BYTES, record['id'],
                        self.project.snapshots.max_files, self.project.snapshots.max_file,
                        decoded_path_limit(self.project.root))
                    self.assertEqual(len(parsed['file_shas']), len(record['files']))
                else:
                    with self.assertRaisesRegex(LimitExceeded, 'file member exceeds'):
                        read_manifest(
                            io.BytesIO(changed), _AUDIT_JSON_BYTES, record['id'],
                            self.project.snapshots.max_files, self.project.snapshots.max_file,
                            decoded_path_limit(self.project.root))

    def test_streamed_manifest_accepts_whitespace_across_reader_chunks(self):
        record = self.project.snapshots.get()
        raw = canonical(record)
        raw = raw[:1] + b' ' * (io.DEFAULT_BUFFER_SIZE + 3) + raw[1:]
        parsed, goal_digest = read_manifest(io.BytesIO(raw), _AUDIT_JSON_BYTES,
                                        record['id'], self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        self.assertNotIn('goal', parsed)
        self.assertEqual(goal_digest, record['goal_digest'])

    def test_streamed_file_map_bounds_and_duplicate_keys(self):
        record = self.project.snapshots.get()
        raw = canonical(record)
        name, entry = next(iter(record['files'].items()))
        member = canonical(name)[:-1] + b':' + canonical(entry)[:-1]
        # Duplicate decoded names (including alternate spelling) must not be
        # silently collapsed into a content-address-valid map.
        duplicate = raw.replace(member, member + b',' + member, 1)
        escaped = member.replace(name[0].encode(),
                                 ('\\u%04x' % ord(name[0])).encode(), 1)
        for bad in (duplicate, raw.replace(member, escaped + b',' + member, 1)):
            with self.assertRaisesRegex(Denied, 'Duplicate snapshot file key'):
                read_manifest(io.BytesIO(bad), _AUDIT_JSON_BYTES, record['id'],
                              self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        changed = canonical(dict(entry, executable=not entry['executable']))[:-1]
        with self.assertRaisesRegex(Denied, 'code digest mismatch'):
            read_manifest(io.BytesIO(raw.replace(canonical(entry)[:-1], changed, 1)),
                          _AUDIT_JSON_BYTES, record['id'], self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        # A content-address-valid manifest still requires the files field;
        # an empty-map code digest must not make a missing field look valid.
        missing_files = dict(record)
        missing_files.pop('files')
        missing_files['code_digest'] = digest(canonical({}))
        missing_files['id'] = digest(canonical({k: v for k, v in missing_files.items()
                                                if k != 'id'}))
        with self.assertRaisesRegex(Denied, 'Invalid snapshot manifest'):
            read_manifest(io.BytesIO(canonical(missing_files)), _AUDIT_JSON_BYTES,
                          missing_files['id'], self.project.snapshots.max_files,
                          self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))

        # Small fields on both sides of files still share one metadata budget.
        padding = b'x' * (_AUDIT_JSON_BYTES // 2)
        with self.assertRaisesRegex(LimitExceeded, 'metadata exceeds'):
            read_manifest(io.BytesIO(b'{"before":"' + padding + b'","files":{' +
                                     member + b'},"after":"' + padding + b'"}'),
                          _AUDIT_JSON_BYTES, record['id'], self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        with self.assertRaisesRegex(LimitExceeded, 'file count'):
            read_manifest(io.BytesIO(raw), _AUDIT_JSON_BYTES, record['id'], 0,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        # Oversized file members are rejected within the platform-derived
        # per-member bound, not the generic 1 MiB cap, with bounded reads.
        with self.assertRaisesRegex(LimitExceeded, 'file member exceeds'):
            read_manifest(io.BytesIO(b'{"files":{"oversized":{"padding":"' +
                                     b'x' * (_AUDIT_JSON_BYTES + 1)),
                          _AUDIT_JSON_BYTES, record['id'], self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))

    def test_streamed_file_member_oversized_is_bounded(self):
        from mizu.snapshot_manifest import decoded_path_limit as _decoded_limit, file_member_limit as _member_limit
        record = self.project.snapshots.get()
        # Corrupt member with huge extra field (100 KiB) stays below the generic
        # 1 MiB cap, so only the platform-derived per-member bound rejects it.
        huge = (b'{"files":{"oversized":{"sha256":"' + b'a' * 64 +
                b'","bytes":1,"executable":false,"padding":"' + b'x' * 100000 + b'"}}}')
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(LimitExceeded, 'file member exceeds'):
                read_manifest(io.BytesIO(huge), _AUDIT_JSON_BYTES, record['id'],
                              self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * _AUDIT_JSON_BYTES)
        # Simulate POSIX PATH_MAX explicitly so the rejection is deterministic
        # on Windows hosts as well as POSIX hosts.
        long_name = 'a' * 5000
        long_member = (canonical(long_name)[:-1] + b':{"sha256":"' + b'a' * 64 +
                       b'","bytes":1,"executable":false}')
        with self.assertRaisesRegex(LimitExceeded, 'file member exceeds'):
            read_manifest(io.BytesIO(b'{"files":{' + long_member + b'}}'),
                          _AUDIT_JSON_BYTES, record['id'],
                          self.project.snapshots.max_files,
                          self.project.snapshots.max_file,
                              _posix_path_limit(self.project.root))
        # Disk reads for a huge corrupt member stay bounded to the per-member
        # bound plus I/O buffering, not the whole multi-hundred-KiB file.
        from mizu.storage import _SNAPSHOT_AUDIT_BYTES
        raw = canonical(record)
        name, entry = next(iter(record['files'].items()))
        member = canonical(name)[:-1] + b':' + canonical(entry)[:-1]
        corrupt = raw.replace(member, b'"oversized":{"sha256":"' + b'a' * 64 +
                              b'","bytes":1,"executable":false,"padding":"' +
                              b'x' * 200000 + b'"}', 1)
        # Build a syntactically valid carrier with the huge member so the
        # reader must stream past it to reject; keep other fields small.
        path = self.project.root / 'snapshots' / (record['id'] + '.json')
        # Use a fresh ID for the corrupt carrier to avoid clobbering live state;
        # the audit must still reject it as oversized before any identity check.
        original = path.read_bytes()
        try:
            path.write_bytes(corrupt)
            read_bytes = []
            orig_stream = __import__('mizu.snapshot', fromlist=['safe_read_stream']).safe_read_stream
            def tracked(root, name_, maximum, consume):
                from mizu.fs import safe_read_stream as real
                class Counted:
                    def __init__(self, stream):
                        self.stream = stream
                        self.total = 0
                    def read(self, size=-1):
                        chunk = self.stream.read(size)
                        self.total += len(chunk)
                        return chunk
                    def seek(self, *args, **kwargs):
                        return self.stream.seek(*args, **kwargs)
                    def tell(self):
                        return self.stream.tell()
                def count(stream):
                    wrapped = Counted(stream)
                    try:
                        return consume(wrapped)
                    finally:
                        read_bytes.append(wrapped.total)
                return real(root, name_, maximum, count)
            with mock.patch('mizu.snapshot.safe_read_stream', side_effect=tracked):
                with self.assertRaises(LimitExceeded):
                    self.project.snapshots.audit_manifest(
                        record['id'], metadata_bytes=_AUDIT_JSON_BYTES,
                        maximum=_SNAPSHOT_AUDIT_BYTES)
            self.assertTrue(read_bytes)
            self.assertLessEqual(max(read_bytes),
                                 _AUDIT_JSON_BYTES + _member_limit(_decoded_limit(self.project.root)) + 8192)
        finally:
            path.write_bytes(original)

    def test_long_windows_path_accepted_by_audit_under_windows_semantics(self):
        from mizu import platform as _platform
        from mizu.storage import _SNAPSHOT_AUDIT_BYTES
        base = self.project.snapshots.get()
        entry = next(iter(base['files'].values()))
        long_name = 'a' * 5000
        files = {long_name: dict(entry)}
        record = dict(base, files=files, code_digest=digest(canonical(files)))
        record['id'] = digest(canonical({k: v for k, v in record.items()
                                         if k != 'id'}))
        # The normal reader accepts the 5000-character path under the
        # existing Windows collision/path policy (no Mizu length cap).
        with mock.patch.object(_platform, 'IS_WINDOWS', True):
            self.project.snapshots._path_validation_cache.clear()
            try:
                self.assertEqual(
                    self.project.snapshots.validate_manifest(
                        record['id'], record)['code_digest'],
                    record['code_digest'])
            finally:
                self.project.snapshots._path_validation_cache.clear()
        # Explicit POSIX PATH_MAX semantics reject the same path on every host.
        with self.assertRaisesRegex(LimitExceeded, 'file member exceeds'):
            read_manifest(io.BytesIO(canonical(record)), _AUDIT_JSON_BYTES,
                          record['id'], self.project.snapshots.max_files,
                          self.project.snapshots.max_file,
                          _posix_path_limit(self.project.root))
        path = self.project.root / 'snapshots' / (record['id'] + '.json')
        write_json(path, record)
        try:
            with mock.patch.object(_platform, 'IS_WINDOWS', True):
                self.project.snapshots._path_validation_cache.clear()
                try:
                    tracemalloc.start()
                    try:
                        parsed, goal_digest = read_manifest(
                            io.BytesIO(canonical(record)), _AUDIT_JSON_BYTES,
                            record['id'], self.project.snapshots.max_files,
                            self.project.snapshots.max_file,
                            decoded_path_limit(self.project.root))
                        self.assertNotIn('goal', parsed)
                        self.assertNotIn('files', parsed)
                        self.assertIn(long_name, parsed['file_names'])
                        self.assertEqual(goal_digest, record['goal_digest'])
                        self.assertEqual(audit(self.project)["snapshots"]["skipped"], 0)
                        audited = self.project.snapshots.audit_manifest(
                            record['id'], metadata_bytes=_AUDIT_JSON_BYTES,
                            maximum=_SNAPSHOT_AUDIT_BYTES)
                        self.assertNotIn('file_names', audited)
                        self.assertNotIn('files', audited)
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    self.assertLess(peak, 4 * _AUDIT_JSON_BYTES)
                finally:
                    self.project.snapshots._path_validation_cache.clear()
        finally:
            if path.exists():
                path.unlink()

    def test_streamed_file_map_code_and_content_identity(self):
        record = self.project.snapshots.get()
        raw = canonical(record)
        name, entry = next(iter(record['files'].items()))
        # Tampered code_digest field alone (files unchanged) is a code mismatch.
        bad_code = raw.replace(record['code_digest'].encode(), b'b' * 64, 1)
        with self.assertRaisesRegex(Denied, 'code digest mismatch'):
            read_manifest(io.BytesIO(bad_code), _AUDIT_JSON_BYTES, record['id'],
                          self.project.snapshots.max_files,
                          self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))
        # Tampered files with a matching tampered code_digest still fail the
        # content-addressed ID, which binds the exact sorted file map.
        tampered_entry = dict(entry, executable=not entry['executable'])
        tampered_files = dict(record['files'])
        tampered_files[name] = tampered_entry
        tampered_code = digest(canonical(tampered_files))
        tampered_raw = raw.replace(canonical(entry)[:-1],
                                   canonical(tampered_entry)[:-1], 1)
        tampered_raw = tampered_raw.replace(record['code_digest'].encode(),
                                            tampered_code.encode(), 1)
        with self.assertRaisesRegex(Denied, 'integrity failure'):
            read_manifest(io.BytesIO(tampered_raw), _AUDIT_JSON_BYTES, record['id'],
                          self.project.snapshots.max_files,
                          self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))

    def test_streamed_file_map_path_collision_and_windows_casefold(self):
        from mizu import platform as _platform
        base = self.project.snapshots.get()
        entry = next(iter(base['files'].values()))
        # Directory/file prefix collision fails closed on every host.
        files = {'collision': entry, 'collision/child': entry}
        record = dict(base, files=files, code_digest=digest(canonical(files)))
        record['id'] = digest(canonical({k: v for k, v in record.items() if k != 'id'}))
        path = self.project.root / 'snapshots' / (record['id'] + '.json')
        write_json(path, record)
        try:
            self.assertEqual(audit(self.project)["snapshots"]["skipped"], 1)
        finally:
            path.unlink()
        # Case-only collision passes on POSIX but fails when Windows casefold applies.
        files = {'A': entry, 'a': entry}
        record = dict(base, files=files, code_digest=digest(canonical(files)))
        record['id'] = digest(canonical({k: v for k, v in record.items() if k != 'id'}))
        path = self.project.root / 'snapshots' / (record['id'] + '.json')
        write_json(path, record)
        try:
            # Exercise POSIX acceptance explicitly on every CI host; Windows
            # rejects the same case-only names after its casefold policy applies.
            for is_windows, skipped in ((False, 0), (True, 1)):
                with mock.patch.object(_platform, 'IS_WINDOWS', is_windows):
                    self.project.snapshots._path_validation_cache.clear()
                    try:
                        self.assertEqual(audit(self.project)["snapshots"]["skipped"], skipped)
                    finally:
                        self.project.snapshots._path_validation_cache.clear()
        finally:
            if path.exists():
                path.unlink()

    def test_streamed_manifest_preserves_field_validation(self):
        base = self.project.snapshots.get()
        bad_files = (dict(base['files'], **{'../outside': next(iter(base['files'].values()))}),
                     {name: dict(entry, bytes=-1) for name, entry in base['files'].items()},
                     {'collision': next(iter(base['files'].values())),
                      'collision/child': next(iter(base['files'].values()))})
        changes = ({'goal_digest': 'a' * 64}, {'code_digest': 'b' * 64},
                   {'parent': 'invalid'}, {'progress': {}},
                   *({'files': files, 'code_digest': digest(canonical(files))}
                     for files in bad_files))
        for change in changes:
            with self.subTest(fields=list(change)):
                record = dict(base, **change)
                record['id'] = digest(canonical({k: v for k, v in record.items() if k != 'id'}))
                path = self.project.root / 'snapshots' / (record['id'] + '.json')
                write_json(path, record)
                try:
                    self.assertEqual(audit(self.project)["snapshots"]["skipped"], 1)
                finally:
                    path.unlink()

    def test_streamed_manifest_does_not_reuse_stat_only_proof(self):
        record = self.project.snapshots.get()
        path = self.project.root / "snapshots" / (record['id'] + '.json')
        self.assertEqual(audit(self.project)["snapshots"]["skipped"], 0)
        info = path.stat()
        raw = path.read_bytes()
        path.write_bytes(raw.replace(canonical(record['goal'])[:-1],
                                     canonical('x' + record['goal'][1:])[:-1], 1))
        self.assertEqual(path.stat().st_size, info.st_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(audit(self.project)["snapshots"]["skipped"], 1)

    def test_streamed_manifest_portable_and_opened_file_checks(self):
        from mizu import fs
        # Cover the callback branch of the portable reader on every host too.
        for portable in (False, True):
            with self.subTest(portable=portable), mock.patch.object(
                    fs._platform, 'HAS_OPENAT', fs._platform.HAS_OPENAT and not portable):
                self.assertEqual(audit(self.project)["snapshots"]["skipped"], 0)
                path = self.project.root / 'stream-check'
                path.write_bytes(b'abc')

                def unstable(stream):
                    data = stream.read(3)
                    path.write_bytes(b'changed')
                    return data

                with self.assertRaises(Denied):
                    safe_read_stream(self.project.root, path.name, 100, unstable)
                alias = self.project.root / 'stream-alias'
                os.link(path, alias)
                try:
                    with self.assertRaises(Denied):
                        safe_read_stream(self.project.root, path.name, 100,
                                         lambda stream: self.fail('hardlink consumed'))
                finally:
                    alias.unlink()
                    path.unlink()

    def test_goal_reread_requires_content_match(self):
        record = self.project.snapshots.get()
        raw = canonical(record)

        class Changing(io.BytesIO):
            scanned = False

            def read(self, size):
                chunk = super().read(size)
                self.scanned |= not chunk
                return chunk

            def seek(self, offset, whence=0):
                # The identity pass seeks backwards after EOF; change the
                # goal without changing length or any simulated stat fields.
                if self.scanned:
                    self.scanned = False
                    replacement = raw.replace(canonical(record['goal'])[:-1],
                                              canonical('x' + record['goal'][1:])[:-1], 1)
                    super().seek(0)
                    self.write(replacement)
                return super().seek(offset, whence)

        with self.assertRaisesRegex(Denied, 'changed while reading'):
            read_manifest(Changing(raw), _AUDIT_JSON_BYTES, record['id'],
                          self.project.snapshots.max_files,
                              self.project.snapshots.max_file,
                              decoded_path_limit(self.project.root))

    def test_active_history_reads_are_bounded_and_reused(self):
        from mizu.fs import safe_read
        snap = self.hidden_snapshot()
        live = self.project.snapshots.get()["id"]
        history = [snap["id"], live]
        generation = digest(canonical(history))
        history_path = self.project.root / "histories" / (generation + ".json")
        write_json(history_path, history)
        write_json(self.project.root / "current.json", {"snapshot": live, "history": generation})
        with mock.patch.object(self.project.snapshots, "_history_ids", side_effect=AssertionError("unbounded history")):
            with mock.patch("mizu.storage.safe_read", wraps=safe_read) as reader:
                report = audit(self.project)
        self.assertEqual(report["reference_records_skipped"], 0)
        self.assertEqual(report["snapshots"]["live"], 2)
        active_reads = [call for call in reader.call_args_list
                        if call.args[1] == f"histories/{generation}.json"]
        self.assertEqual(len(active_reads), 1)
        for call in reader.call_args_list:
            self.assertFalse(call.args[1].startswith("snapshots/"))
            self.assertEqual(call.args[2], _AUDIT_JSON_BYTES)

    def test_oversized_current_pointer_is_bounded_and_blocks_apply(self):
        current = self.project.root / "current.json"
        pointer = json.loads(current.read_text())
        pointer["padding"] = "x" * (_AUDIT_JSON_BYTES + 128)
        write_json(current, pointer)
        with mock.patch.object(self.project.snapshots, "_history_ids", side_effect=AssertionError("unbounded history")):
            self.assert_reference_scan_blocked()
        # Its valid history references remain visible despite the bad pointer.
        self.assertEqual(audit(self.project)["snapshots"]["referenced"], 1)

    def test_current_pointer_shape_and_fields_fail_closed(self):
        current = self.project.root / "current.json"
        pointer = json.loads(current.read_text())
        bad_pointers = ([], {}, {"snapshot": pointer["snapshot"]},
                        {**pointer, "snapshot": "bad"}, {**pointer, "history": "bad"},
                        {**pointer, "history": "d" * 64})
        for value in bad_pointers:
            with self.subTest(pointer=value):
                write_json(current, value)
                self.assert_reference_scan_blocked()
        current.unlink()
        self.assertEqual(audit(self.project)["reference_records_skipped"], 0)

    def test_active_history_shape_digest_and_end_are_checked_once(self):
        current = self.project.root / "current.json"
        pointer = json.loads(current.read_text())
        path = self.project.root / "histories" / (pointer["history"] + ".json")
        original = path.read_bytes()
        snap = self.hidden_snapshot()
        for value in ({}, [], [pointer["snapshot"], "bad"], [snap["id"]],
                      [snap["id"], pointer["snapshot"]]):
            with self.subTest(history=value):
                write_json(path, value)
                self.assert_reference_scan_blocked()
                if isinstance(value, list) and snap["id"] in value:
                    self.assertNotIn(f"snapshots/{snap['id']}.json", reclaim(self.project)["snapshots"])
        path.write_bytes(original)
        # A correctly hashed generation must still end at current.snapshot.
        history = [snap["id"]]
        generation = digest(canonical(history))
        write_json(path.parent / (generation + ".json"), history)
        write_json(current, {**pointer, "history": generation})
        self.assert_reference_scan_blocked()
        (path.parent / (generation + ".json")).unlink()
        write_json(current, pointer)
        path.unlink()  # An absent required generation is not an optional file.
        self.assert_reference_scan_blocked()

    def test_malformed_nonactive_history_preserves_usable_references(self):
        snap = self.hidden_snapshot()
        histories = self.project.root / "histories"
        for name, value in (("invalid-name", [snap["id"]]),
                            (digest(canonical([snap["id"], None])), [snap["id"], None])):
            with self.subTest(name=name):
                path = histories / (name + ".json")
                write_json(path, value)
                self.assert_reference_scan_blocked()
                self.assertNotIn(f"snapshots/{snap['id']}.json", reclaim(self.project)["snapshots"])
                path.unlink()

    def test_artifacts_nondirectory_root_is_skipped_without_pointer_read(self):
        artifacts = self.project.root / "artifacts"
        artifacts.write_text("not a directory")
        from mizu.fs import safe_read
        with mock.patch("mizu.storage.safe_read", wraps=safe_read) as reader:
            self.assert_reference_scan_blocked()
        self.assertFalse(any(call.args[1].startswith("artifacts/") for call in reader.call_args_list))

    def test_symlinked_artifacts_root_is_never_read(self):
        artifacts = self.project.root / "artifacts"
        outside = self.root / "outside-artifacts"
        outside.mkdir()
        write_json(outside / "latest.json", {"snapshot": "a" * 64})
        try:
            artifacts.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        from mizu.fs import safe_read
        with mock.patch("mizu.storage.safe_read", wraps=safe_read) as reader:
            self.assert_reference_scan_blocked()
        self.assertFalse(any(call.args[1].startswith("artifacts/") for call in reader.call_args_list))

    def test_artifact_directory_without_evidence_is_incomplete(self):
        artifact = self.project.root / "artifacts" / "missing-evidence"
        artifact.mkdir(parents=True)
        self.assert_reference_scan_blocked()

    def test_missing_snapshot_root_is_incomplete(self):
        snapshots = self.project.root / "snapshots"
        outside = self.root / "moved-snapshots"
        snapshots.rename(outside)
        try:
            self.assertGreaterEqual(audit(self.project)["snapshots"]["skipped"], 1)
            original_manifests = list(outside.glob("*.json"))
            self.assertTrue(original_manifests)
            self.project.set_control(paused=True)
            with mock.patch.object(Path, "unlink", side_effect=AssertionError("premature removal")):
                with self.assertRaises(Denied):
                    reclaim(self.project, apply=True)
            self.assertTrue(all(path.exists() for path in original_manifests))
        finally:
            if outside.exists() and not snapshots.exists():
                outside.rename(snapshots)

    def test_symlinked_snapshot_root_is_not_scanned_or_reclaimed(self):
        snapshots = self.project.root / "snapshots"
        outside = self.root / "outside-snapshots"
        snapshots.rename(outside)
        try:
            snapshots.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            outside.rename(snapshots)
            self.skipTest("symlinks unavailable")
        try:
            self.assertGreaterEqual(audit(self.project)["snapshots"]["skipped"], 1)
            original_manifests = list(outside.glob("*.json"))
            self.assertTrue(original_manifests)
            self.project.set_control(paused=True)
            with mock.patch.object(Path, "unlink", side_effect=AssertionError("premature removal")):
                with self.assertRaises(Denied):
                    reclaim(self.project, apply=True)
            self.assertTrue(all(path.exists() for path in original_manifests))
        finally:
            if snapshots.is_symlink():
                snapshots.unlink()
            if outside.exists():
                outside.rename(snapshots)

    def test_symlinked_object_root_is_never_unlinked(self):
        objects = self.project.root / "objects"
        outside = self.root / "outside-objects"
        objects.rename(outside)
        try:
            objects.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            outside.rename(objects)
            self.skipTest("symlinks unavailable")
        try:
            self.assertGreaterEqual(audit(self.project)["objects"]["skipped"], 1)
            original_objects = list(outside.iterdir())
            self.assertTrue(original_objects)
            self.project.set_control(paused=True)
            with mock.patch.object(Path, "unlink", side_effect=AssertionError("premature removal")):
                with self.assertRaises(Denied):
                    reclaim(self.project, apply=True)
            self.assertTrue(all(path.exists() for path in original_objects))
        finally:
            if objects.is_symlink():
                objects.unlink()
            if outside.exists():
                outside.rename(objects)

    def test_failed_reference_directory_listing_is_incomplete(self):
        artifacts = self.project.root / "artifacts"
        artifacts.mkdir()
        scandir = os.scandir

        def fail_listing(path):
            if Path(path) == artifacts:
                raise PermissionError("synthetic listing failure")
            return scandir(path)

        with mock.patch("mizu.storage.os.scandir", side_effect=fail_listing):
            self.assert_reference_scan_blocked()

    def test_symlinked_reference_record_is_skipped(self):
        outside = self.root / "outside-reference.json"
        write_json(outside, {"snapshot": "a" * 64})
        run = self.project.root / "runs" / "unsafe"
        run.mkdir(parents=True)
        try:
            (run / "result.json").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assert_reference_scan_blocked()
        self.assertEqual(audit(self.project)["references"]["run_snapshots"], 0)

    def test_invalid_archived_reference_marks_archive_incomplete(self):
        live = self.project.snapshots.get()["id"]
        with mock.patch("mizu.storage.insight_archive.snapshot_references",
                        return_value=[None, "bad", {}, live]):
            self.assert_reference_scan_blocked()
            self.assertEqual(audit(self.project)["references"]["insight_snapshots"], 1)

    def test_clean_tree_reports_zero_reference_skips_and_reclaims(self):
        report = audit(self.project)
        self.assertEqual(report["reference_records_skipped"], 0)
        self.assertEqual(report["work_item_events_skipped"], 0)
        preview = reclaim(self.project)
        self.assertEqual(preview["reference_records_skipped"], 0)


class SessionAccountingTests(Fixture):
    def test_missing_sessions_root_is_all_zero(self):
        from pathlib import Path as _Path
        import shutil
        sessions = self.project.root / "sessions"
        if sessions.is_symlink() or sessions.exists():
            shutil.rmtree(sessions, ignore_errors=True)
            try:
                sessions.unlink(missing_ok=True)
            except OSError:
                pass
        self.assertFalse(_Path(sessions).exists())
        report = audit(self.project)
        self.assertEqual(report["sessions"], {"records": 0, "files": 0, "bytes": 0, "skipped": 0})

    def test_lazy_wide_directory_counts_exact_without_materialization(self):
        wide = self.project.root / "sessions" / "worker" / "wide"
        wide.mkdir(parents=True)
        fake_count = 100001  # Cross the former cutoff without creating real files.
        stat_count = 0

        def counted():
            nonlocal stat_count
            stat_count += 1

        def entries():
            for i in range(fake_count):
                self.assertEqual(stat_count, i, "entries buffered before stat")
                name = "session.json" if i % 20000 == 0 else f"synthetic-{i}.bin"
                yield SessionEntry(wide / name, on_stat=counted)
            self.assertEqual(stat_count, fake_count)

        iterator = SessionIterator(entries())
        real_scandir = os.scandir

        def scandir(path):
            return iterator if Path(path) == wide else real_scandir(path)

        with mock.patch("mizu.storage.os.scandir", side_effect=scandir):
            with mock.patch.object(Path, "rglob", side_effect=AssertionError("eager rglob")):
                report = audit(self.project)["sessions"]
        self.assertEqual(report, {"records": 6, "files": fake_count,
                                  "bytes": fake_count * 7, "skipped": 0})
        self.assertTrue(iterator.closed)

    def test_wide_sibling_directories_are_descended_before_parent_advances(self):
        role = self.project.root / "sessions" / "worker"
        role.mkdir(parents=True)
        sibling_count = 128
        stat_count = 0
        last_child = None

        def counted():
            nonlocal stat_count
            stat_count += 1

        def siblings():
            for i in range(sibling_count):
                self.assertEqual(stat_count, i, "sibling directories buffered before descent")
                if last_child is not None:
                    self.assertTrue(last_child.closed)
                yield SessionEntry(role / f"key-{i}", directory=True)
            self.assertEqual(stat_count, sibling_count)
            self.assertTrue(last_child.closed)

        def child_entries(path):
            yield SessionEntry(path / "session.json", on_stat=counted)

        parent = SessionIterator(siblings())
        real_scandir = os.scandir

        def scandir(path):
            nonlocal last_child
            path = Path(path)
            if path == role:
                return parent
            if path.parent == role:
                last_child = SessionIterator(child_entries(path))
                return last_child
            return real_scandir(path)

        with mock.patch("mizu.storage.os.scandir", side_effect=scandir):
            report = audit(self.project)["sessions"]
        self.assertEqual(report, {"records": sibling_count, "files": sibling_count,
                                  "bytes": sibling_count * 7, "skipped": 0})
        self.assertTrue(parent.closed)
        self.assertTrue(last_child.closed)

    def test_iterators_close_on_faults_and_ancestors_resume(self):
        root = self.project.root / "sessions"
        root.mkdir(exist_ok=True)
        for fault in ("open", "iterate", "stat", "unexpected"):
            with self.subTest(fault=fault):
                def fail():
                    if fault == "unexpected":
                        raise RuntimeError("synthetic unexpected failure")
                    raise OSError("synthetic entry failure")

                def root_entries():
                    yield SessionEntry(root / "key", directory=True)
                    yield SessionEntry(root / "session.json")

                def child_entries():
                    if fault in ("iterate", "unexpected"):
                        fail()
                    yield SessionEntry(root / "key" / "bad.bin", on_stat=fail)
                    yield SessionEntry(root / "key" / "session.json")

                parent = SessionIterator(root_entries())
                child = SessionIterator(child_entries())

                def scandir(path):
                    if Path(path) == root:
                        return parent
                    if fault == "open":
                        fail()
                    return child

                with mock.patch("mizu.storage.os.scandir", side_effect=scandir):
                    if fault == "unexpected":
                        with self.assertRaisesRegex(RuntimeError, "synthetic unexpected"):
                            _session_accounting(self.project)
                    else:
                        count = 2 if fault == "stat" else 1
                        self.assertEqual(_session_accounting(self.project),
                                         {"records": count, "files": count,
                                          "bytes": count * 7, "skipped": 1})
                self.assertTrue(parent.closed)
                if fault != "open":
                    self.assertTrue(child.closed)

    def test_symlink_entries_are_skipped_not_followed(self):
        import os as _os
        outside = self.root / "outside.bin"
        outside.write_bytes(b"outside-bytes")
        sessions = self.project.root / "sessions"
        key = sessions / "worker" / "k1"
        key.mkdir(parents=True)
        payload = b'{"v": 2}'
        (key / "session.json").write_bytes(payload)
        baseline = audit(self.project)["sessions"]
        try:
            _os.symlink(str(outside), str(key / "link-file.bin"))
            _os.symlink(str(key), str(sessions / "link-dir"))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unsupported: {exc}")
        report = audit(self.project)["sessions"]
        self.assertEqual(report["files"], baseline["files"])
        self.assertEqual(report["records"], baseline["records"])
        self.assertEqual(report["bytes"], baseline["bytes"])
        self.assertGreaterEqual(report["skipped"], baseline["skipped"] + 2)
