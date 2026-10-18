"""Local SQLite legacy recovery: transactions, unsafe files and bounded pages."""
import contextlib
import os
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from support import Fixture, ROOT
from mizu import insight_archive
from mizu.errors import Denied
from mizu.fs import canonical, read_json, write_json
from mizu.project import Project
from mizu.storage import audit, backup, restore


class InsightTombstoneTests(Fixture):
    def lost(self, key="a" * 32, rev=None):
        item = self.project.insights.submit(source="searcher", title="Lost title", body="Lost body",
                                            base_snapshot=self.project.snapshots.get()["id"], insight_id=key)
        (self.project.root / "inbox" / f"{key}.json").unlink()
        path = self.project.root / "insight-ids" / f"{key}.json"
        value = read_json(path)
        if rev is not None:
            value["rev"] = rev
            write_json(path, value)
        return item, path, value

    def page(self, **kwargs):
        return self.project.insights.list_page(scope="archive", **kwargs)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db:
            return db.execute("SELECT seq, id, rev, record, decision, identity FROM insights ORDER BY seq").fetchall()

    def test_summary_known_metadata_no_fabricated_content_and_default_reads(self):
        item, path, identity = self.lost(rev=4)
        # Neither a stray decision nor references can recreate a lost body.
        write_json(self.project.root / "decisions" / path.name,
                   {"id": item["id"], "rev": 4, "action": "accept", "work_item": {"id": "not-real"}})
        identity.update(title="Do not copy", base_snapshot=item["base_snapshot"], work_item="not-real")
        write_json(path, identity)
        expected = {"id": item["id"], "rev": 4, "created_at": identity["created_at"], "body_unavailable": True}
        self.assertEqual(self.project.insights.list(pending=False), [])
        with self.assertRaisesRegex(Denied, "body unavailable"):
            self.project.insights.read(item["id"])
        self.assertEqual(self.page()["items"], [expected])
        self.assertEqual(self.project.insights.list_page()["items"], [])
        self.assertEqual(audit(self.project)["references"]["insight_snapshots"], 0)
        self.assertEqual(list(insight_archive.snapshot_references(self.project.root)), [])
        self.assertEqual(list((self.project.root / "work-items").glob("*.json")), [])
        # The permanent index itself retains the distinction, even without the legacy file.
        path.unlink()
        for key in (item["id"], "aaaa"):
            with self.assertRaisesRegex(Denied, "body unavailable"):
                self.project.insights.read(key)
        with self.assertRaisesRegex(Denied, "not found"):
            self.project.insights.read("unknown")

    def test_existing_work_item_projection_handles_unavailable_origin(self):
        insight = self.project.insights.submit(source="searcher", title="Origin", body="Body",
                                               base_snapshot=None)
        decision = self.project.insights.decide(insight["id"], "accept", "Reviewed", "", "run",
                                                expected_rev=1)
        (self.project.root / "inbox" / f"{insight['id']}.json").unlink()
        self.page()
        status = self.project.work_items.submitter_status(insight["id"])
        self.assertIsNotNone(status)
        self.assertTrue(status["stale"])
        self.assertIsNone(status["current_origin_rev"])
        self.assertEqual(status["id"], decision["work_item"]["id"])

    def test_tombstones_do_not_authorize_mutation(self):
        item, _, _ = self.lost()
        self.page()
        key = item["id"]
        before = self.rows()
        calls = [
            lambda: self.project.insights.submit(source="searcher", title="Lost title", body="Lost body",
                                                 base_snapshot=item["base_snapshot"], insight_id=key),
            lambda: self.project.insights.revise(key, source="searcher", title="New", body="New",
                                                 base_snapshot=None, expected_rev=1),
            lambda: self.project.insights.attach_evidence(key, source="searcher", text="new", expected_rev=1),
            lambda: self.project.insights.decide(key, "accept", "reason", "", "run", expected_rev=1),
            lambda: self.project.insights.withdraw(key, source="searcher", reason="reason", expected_rev=1),
            lambda: insight_archive.store(self.project.root, item, {}, {}),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(Denied):
                call()
        self.assertEqual(self.rows(), before)
        self.assertFalse((self.project.root / "inbox" / f"{key}.json").exists())
        self.assertFalse((self.project.root / "decisions" / f"{key}.json").exists())

    def test_preserves_live_and_archive_rows_and_never_infers_revision(self):
        lost, _, identity = self.lost()
        live = self.project.insights.submit(source="searcher", title="Live", body="Live",
                                            base_snapshot=None, insight_id="b" * 32)
        archived = self.project.insights.submit(source="searcher", title="Archive", body="Archive",
                                                base_snapshot=None, insight_id="c" * 32)
        decision = {"action": "reject", "rev": 1}
        insight_archive.store(self.project.root, archived, decision, {"original": "binding"})
        before = self.rows()
        (self.project.root / "inbox" / f'{archived["id"]}.json').unlink()
        page = self.page()["items"]
        self.assertEqual(self.rows()[0], before[0])
        self.assertEqual(len(page), 2)
        self.assertEqual(page[0]["decision"], decision)
        self.assertEqual(page[1], {"id": lost["id"], "created_at": identity["created_at"], "body_unavailable": True})
        self.assertEqual(self.project.insights.read(live["id"]), live)
        self.assertEqual(self.project.insights.read(archived["id"]), archived)
        self.assertEqual(len(self.project.insights.list(pending=False)), 1)
        self.project.insights.revise(live["id"], source="searcher", title="Live", body="Changed",
                                     base_snapshot=None, expected_rev=1)
        # Live history does not fabricate a tombstone in the permanent archive.
        self.assertEqual(len(self.page()["items"]), 2)

    def test_atomic_retry_on_interrupt_and_one_time_index_after_success(self):
        for n in range(3):
            self.lost(f"{n:032x}")
        real_identity = insight_archive.identity
        count = 0

        def interrupt(root, key):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt("injected cancellation after one insert")
            return real_identity(root, key)

        with patch("mizu.insight_archive.identity", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.page()
        self.assertEqual(self.rows(), [])
        first = self.page(limit=1)
        rows = self.rows()
        # An established traversal must use SQLite, not scan/read identity files again.
        with patch("mizu.insight_archive.identity", side_effect=AssertionError("rescan")), \
             patch("mizu.insight_archive._directory", side_effect=AssertionError("rescan")):
            cursor = first["next_cursor"]
            items = first["items"][:]
            while cursor:
                page = self.page(limit=1, cursor=cursor)
                self.assertEqual(page, self.page(limit=1, cursor=cursor))
                self.assertEqual(page["generation"], first["generation"])
                items.extend(page["items"])
                cursor = page["next_cursor"]
            self.assertEqual(len(items), 3)
            self.assertEqual(len(self.page()["items"]), 3)
        self.assertEqual(self.rows(), rows)
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db:
            plan = str(db.execute("EXPLAIN QUERY PLAN SELECT seq, record, decision FROM insights "
                                  "WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?", (0, 3, 2)).fetchall())
            self.assertIn("INTEGER PRIMARY KEY", plan)
            self.assertNotIn("SCAN insights", plan)

    def test_killed_migration_transaction_recovers_and_retries(self):
        for n in range(20):
            self.lost(f"{n:032x}")
        # A real terminated writer, before its completion marker/commit.
        subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mizu import insight_archive
from mizu.fs import lock
class Kill(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.startswith('INSERT INTO insight_migrations'):
            os._exit(0)
        return super().execute(sql, *args)
root = Path(sys.argv[2])
with lock(root / 'locks' / 'insights.lock'):
    with insight_archive._connect(root, write=True):
        pass
    db = sqlite3.connect(root / insight_archive.NAME, factory=Kill)
    db.execute('PRAGMA cache_size=1')
    insight_archive._migrate_identities(root, db)
raise AssertionError('writer did not terminate')
""", str(ROOT / "src"), str(self.project.root)], check=True)
        insight_archive.recover(self.project.root)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.page()["items"]), 20)
        self.assertEqual(len(self.rows()), 20)

    def test_corrupt_identity_fails_closed_and_repair_retries(self):
        _, first_path, _ = self.lost("a" * 32)
        _, bad_path, value = self.lost("b" * 32)
        malformed = [b"{", b"[]", canonical({**value, "id": "other"}),
                     canonical({**value, "sha256": "invalid"}), canonical({**value, "created_at": "2026-01-01"}),
                     canonical({**value, "rev": True}), canonical({**value, "rev": 0}),
                     canonical({**value, "rev": 2 ** 63}),
                     canonical(value).rstrip()[:-1] + b', "id": "other"}',
                     canonical({**value, "extra": "x" * 4096}),
                     canonical(value).rstrip()[:-1] + b', "extra": NaN}']
        for raw in malformed:
            with self.subTest(raw=raw[:100]):
                bad_path.write_bytes(raw)
                with self.assertRaises(Denied):
                    self.page()
                self.assertEqual(self.rows(), [])
        write_json(bad_path, value)
        self.assertEqual(len(self.page()["items"]), 2)
        self.assertTrue(first_path.exists())

    def test_unsafe_identity_entries_and_directories_fail_closed(self):
        _, path, value = self.lost()
        path.unlink()
        target = self.root / "identity.json"
        write_json(target, value)
        cases = ("symlink", "hardlink", "directory", "bad-name", "extra")
        for kind in cases:
            with self.subTest(kind=kind):
                candidate = path
                if kind == "symlink":
                    candidate.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, candidate)
                elif kind == "directory":
                    candidate.mkdir()
                else:
                    candidate = path.parent / ("INVALID.json" if kind == "bad-name" else "unexpected.txt")
                    candidate.write_bytes(canonical(value))
                with self.assertRaises(Denied):
                    self.page()
                self.assertEqual(self.rows(), [])
                candidate.rmdir() if candidate.is_dir() else candidate.unlink()
        directory = path.parent
        directory.rmdir()
        directory.symlink_to(target.parent, target_is_directory=True)
        with self.assertRaises(Denied):
            self.page()
        directory.unlink()
        write_json(path, value)
        self.assertEqual(len(self.page()["items"]), 1)

    def test_unsafe_or_malformed_live_candidate_is_not_silently_skipped(self):
        item, _, _ = self.lost()
        live = self.project.root / "inbox" / f'{item["id"]}.json'
        for raw in (b"{", canonical({**item, "id": "other"}), canonical({**item, "body": None})):
            live.write_bytes(raw)
            with self.assertRaises(Denied):
                self.page()
            self.assertEqual(self.rows(), [])
        live.unlink()
        target = self.root / "live.json"
        write_json(target, item)
        live.symlink_to(target)
        with self.assertRaises(Denied):
            self.page()
        live.unlink()
        write_json(live, item)
        self.assertEqual(self.page()["items"], [])

    def test_backup_restore_before_and_after_migration(self):
        item, _, _ = self.lost()
        self.project.set_control(paused=True)
        for stage in ("before", "after"):
            if stage == "after":
                self.page()
            destination = self.root / f"{stage}.tar.gz"
            backup(self.project, destination)
            restore(self.config, stage, destination)
            restored = Project(self.config, stage)
            items = restored.insights.list_page(scope="archive")["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], item["id"])
            self.assertTrue(items[0]["body_unavailable"])
            with self.assertRaisesRegex(Denied, "body unavailable"):
                restored.insights.read(item["id"])
            self.assertEqual(restored.insights.list(pending=False), [])
            with patch("mizu.insight_archive.identity", side_effect=AssertionError("rescan")):
                self.assertEqual(restored.insights.list_page(scope="archive")["items"], items)

    def test_over_1000_tombstones_prefixes_and_high_water_append_isolation(self):
        directory = self.project.root / "insight-ids"
        for n in range(1007):
            key = f"{n:032x}"
            write_json(directory / f"{key}.json", {"id": key, "sha256": "f" * 64,
                                                 "created_at": "2026-01-01T00:00:00+00:00"})
        first = self.page(limit=1000)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["items"]), 1000)
        appended = self.project.insights.submit(source="searcher", title="New", body="New", base_snapshot=None)
        insight_archive.store(self.project.root, appended, {"action": "reject"}, {})
        second = self.page(limit=1000, cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 7)
        self.assertFalse(second["has_more"])
        self.assertEqual(first["generation"], second["generation"])
        items = first["items"] + second["items"]
        self.assertEqual({i["id"] for i in items}, {f"{n:032x}" for n in range(1007)})
        self.assertTrue(all(i["body_unavailable"] and "rev" not in i for i in items))
        self.assertNotEqual(self.page()["generation"], first["generation"])
        with self.assertRaisesRegex(Denied, "Ambiguous"):
            self.project.insights.read("0000")
        with self.assertRaisesRegex(Denied, "body unavailable"):
            self.project.insights.read(f"{0:032x}")

    def test_corrupt_candidate_with_archived_body_fails_without_overwrite(self):
        item, path, value = self.lost()
        insight_archive.store(self.project.root, item, {"action": "reject"}, value)
        before = self.rows()
        write_json(path, {**value, "sha256": "bad"})
        with self.assertRaises(Denied):
            self.page()
        self.assertEqual(self.rows(), before)
        write_json(path, value)
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db, db:
            db.execute("UPDATE insights SET record='{}'")
        with self.assertRaises(Denied):
            self.page()
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db, db:
            db.execute("UPDATE insights SET record=?", (canonical(item).decode(),))
        self.assertEqual(self.page()["items"][0]["id"], item["id"])
        self.assertEqual(self.rows(), before)

    def test_missing_candidate_and_enumeration_errors_roll_back(self):
        self.lost()
        with patch("mizu.insight_archive.identity", return_value=None):
            with self.assertRaises(Denied):
                self.page()
        self.assertEqual(self.rows(), [])
        with patch("mizu.insight_archive.os.scandir", side_effect=PermissionError("injected unreadable identities")):
            with self.assertRaises(Denied):
                self.page()
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.page()["items"]), 1)

    def test_live_evidence_larger_than_preview_is_not_a_missing_body(self):
        item = self.project.insights.submit(source="searcher", title="Large", body="b" * 120000,
                                            base_snapshot=None)
        for n in range(3):
            self.project.insights.attach_evidence(item["id"], source="searcher", text="e" * 8192, expected_rev=1)
        self.assertEqual(self.page()["items"], [])
        self.assertEqual(self.project.insights.read(item["id"])["body"], item["body"])

    def test_pre_migration_cursor_keeps_high_water_and_fresh_page_includes_import(self):
        item, _, _ = self.lost()
        for key in ("b" * 32, "c" * 32):
            record = {**item, "id": key}
            insight_archive.store(self.project.root, record, {"action": "reject"}, {})
        # Simulate a cursor issued by the pre-tombstone implementation.
        with patch("mizu.insight_archive._migrate_identities"):
            first = self.page(limit=1)
        second = self.page(limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(second["items"][0]["id"], "c" * 32)
        self.assertFalse(second["has_more"])
        fresh = self.page()
        self.assertEqual(len(fresh["items"]), 3)
        self.assertEqual(fresh["items"][-1]["id"], item["id"])
        self.assertTrue(fresh["items"][-1]["body_unavailable"])
