"""Reference-aware storage accounting preview plus gated reclamation. Offline only."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from support import Fixture
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.storage import _AUDIT_JSON_BYTES, _session_accounting, audit, reclaim


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
        write_json(history, event)
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
        self.project.work_items.transition(
            opened["id"], expected_rev=1, status="done", actor="worker",
            snapshot_id=snapshot["id"])

        report = audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertGreaterEqual(report["references"]["work_item_snapshots"], 1)
        self.assertNotIn(snapshot["id"], report["snapshots"]["unreferenced_sample"])

    def test_synthetic_malformed_incomplete_and_unexpected_drop_records_fail_closed(self):
        malformed_dir, _ = self.synthetic_drop(2)
        (malformed_dir / "legacy.json").write_bytes(b"{not-json")

        incomplete_dir, record = self.synthetic_drop(3)
        incomplete = dict(record)
        incomplete.pop("reason")
        write_json(incomplete_dir / "legacy.json", incomplete)

        unexpected_dir, _ = self.synthetic_drop(4)
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
