"""Storage-only archive regressions; local SQLite and injected I/O failures."""
import contextlib
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import insight_archive
from mizu.errors import Denied
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.storage import audit, backup, reclaim, restore


class InsightArchiveTests(Fixture):
    epoch = 2000000000

    def proposal(self, insight_id="a" * 32, action="reject", age=32, **kwargs):
        item = self.project.insights.submit(source="searcher", title="Title", body="Body",
                                            base_snapshot=None, insight_id=insight_id, **kwargs)
        self.project.insights.attach_evidence(insight_id, source="searcher", text="receipt", expected_rev=1)
        if action == "withdraw":
            decision = self.project.insights.withdraw(insight_id, source="searcher", reason="moot", expected_rev=1)
        else:
            decision = self.project.insights.decide(insight_id, action, "reason", "later" if action == "defer" else "",
                                                    "run", expected_rev=1)
        decision["created_at"] = dt.datetime.fromtimestamp(self.epoch - age * 86400, dt.timezone.utc).isoformat()
        write_json(self.project.root / "decisions" / f"{insight_id}.json", decision)
        return self.project.insights.read(item["id"])

    def gc(self, **kwargs):
        with patch("mizu.insights.time.time", return_value=self.epoch):
            return self.project.insights.gc_decided(**kwargs)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db:
            return db.execute("SELECT id, rev, record, decision, identity FROM insights ORDER BY id, rev").fetchall()

    def test_boundary_disable_and_terminal_actions(self):
        for n, action in enumerate(("accept", "modify", "reject", "withdraw")):
            self.proposal(str(n) * 32, action, age=31)
        self.proposal("f" * 32, age=31 - 1 / 86400)
        self.assertEqual(self.gc(keep_days=0), 0)
        self.assertFalse((self.project.root / insight_archive.NAME).exists())
        self.project.insights.retention_days = 60
        self.assertEqual(self.gc(), 0)
        self.project.insights.retention_days = 31
        self.assertEqual(self.gc(), 4)
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue((self.project.root / "inbox" / ("f" * 32 + ".json")).exists())

    def test_pending_deferred_stale_and_malformed_stay_live(self):
        self.project.insights.submit(source="searcher", title="Pending", body="b", base_snapshot=None, insight_id="pending")
        self.proposal("deferred", "defer")
        self.proposal("stale")
        path = self.project.root / "inbox/stale.json"
        record = read_json(path)
        record["rev"] = 2
        write_json(path, record)
        for n, (key, value) in enumerate((("rev", "bad"), ("evidence", {}), ("body", None), ("source", None))):
            insight_id = f"bad{n}"
            self.proposal(insight_id)
            path = self.project.root / "inbox" / f"{insight_id}.json"
            record = read_json(path)
            record[key] = value
            write_json(path, record)
        for n, (key, value) in enumerate((("action", "unknown"), ("rev", "bad"),
                                         ("id", "mismatch"), ("created_at", "bad"))):
            insight_id = f"decision{n}"
            self.proposal(insight_id)
            path = self.project.root / "decisions" / f"{insight_id}.json"
            decision = read_json(path)
            decision[key] = value
            write_json(path, decision)
        self.assertEqual(self.gc(), 0)
        self.assertEqual(len(list((self.project.root / "inbox").glob("*.json"))), 11)

    def test_archive_preserves_complete_body_bindings_and_is_idempotent(self):
        item = self.proposal(origin="trusted-caller", run="source-run")
        self.assertEqual(self.gc(), 1)
        self.assertEqual(self.gc(), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][2]), item)
        self.assertEqual(json.loads(rows[0][3])["rev"], item["rev"])
        self.assertEqual(json.loads(rows[0][4])["id"], item["id"])
        self.assertEqual(self.project.insights.read(item["id"]), item)
        self.assertEqual(self.project.insights.history(item["id"]), [])
        self.assertEqual(self.project.insights.list(pending=False), [])
        self.assertEqual(self.project.insights.scan_source("searcher"), [])

    def test_unlink_failure_keeps_both_and_retry_converges(self):
        item = self.proposal()
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path.parent.name == "inbox":
                self.assertEqual(json.loads(self.rows()[0][2]), item)
                raise OSError("injected unlink")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            self.assertEqual(self.gc(), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.project.insights.read("aaaa"), item)  # duplicate ID is not ambiguous
        self.assertTrue((self.project.root / "inbox" / f"{item['id']}.json").exists())
        self.assertEqual(self.gc(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.project.insights.read(item["id"]), item)

    def test_commit_failure_retains_inbox_then_retries(self):
        item = self.proposal()
        connect = sqlite3.connect
        class FailingCommit(sqlite3.Connection):
            def __exit__(self, *args):
                self.rollback()
                raise sqlite3.OperationalError("injected commit")
        with patch("mizu.insight_archive.sqlite3.connect", side_effect=lambda *a, **k: connect(*a, **k, factory=FailingCommit)):
            self.assertEqual(self.gc(), 0)
        self.assertEqual(self.project.insights.read(item["id"]), item)
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(self.gc(), 1)

    def test_directory_sync_failure_keeps_both(self):
        item = self.proposal()
        with patch("mizu.insight_archive.sync_dir", side_effect=OSError("injected fsync")):
            self.assertEqual(self.gc(), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue((self.project.root / "inbox" / f"{item['id']}.json").exists())
        self.assertEqual(self.gc(), 1)

    def test_read_prefix_ambiguity_spans_live_and_archive(self):
        item = self.proposal()
        self.gc()
        self.assertEqual(self.project.insights.read("aaaa"), item)
        self.project.insights.submit(source="searcher", title="Other", body="b", base_snapshot=None,
                                     insight_id="a" * 31 + "b")
        with self.assertRaisesRegex(Denied, "Ambiguous"):
            self.project.insights.read("aaaa")
        self.proposal("a" * 31 + "c")
        self.gc()
        with self.assertRaisesRegex(Denied, "Ambiguous"):
            self.project.insights.read("aaaa")
        self.assertEqual(self.project.insights.read(item["id"]), item)
        self.project.insights.submit(source="searcher", title="Exact", body="b", base_snapshot=None, insight_id="aaaa")
        self.assertEqual(self.project.insights.read("aaaa")["title"], "Exact")
        with self.assertRaises(Denied):
            self.project.insights.read("ffff")

    def test_archived_submit_retry_and_revise_owner_cas_reopen(self):
        item = self.proposal(origin="caller", run="original")
        self.gc()
        args = dict(source="searcher", title="Title", body="Body", base_snapshot=None, insight_id=item["id"])
        generation = self.project.insights.generation()
        self.assertEqual(self.project.insights.submit(**args, run="retry", origin="other"), item)
        with self.assertRaises(Denied):
            self.project.insights.submit(**{**args, "body": "other"})
        revise = {k: v for k, v in args.items() if k != "insight_id"}
        self.assertEqual(self.project.insights.revise(item["id"], **revise, expected_rev=1), item)
        self.assertEqual(self.project.insights.generation(), generation)
        with self.assertRaises(Denied):
            self.project.insights.revise(item["id"], **{**revise, "source": "operator", "body": "new"}, expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.revise(item["id"], **{**revise, "body": "new"}, expected_rev=2)
        revised = self.project.insights.revise(item["id"], **{**revise, "body": "new"}, expected_rev=1)
        self.assertEqual(revised["rev"], 2)
        self.assertEqual(revised["origin"], "caller")
        self.assertEqual(revised["evidence"], [])
        self.assertNotEqual(self.project.insights.generation(), generation)
        self.assertEqual(self.project.insights.list()[0]["rev"], 2)
        self.assertEqual(self.project.insights.history(item["id"])[0]["evidence"], item["evidence"])
        self.assertEqual(json.loads(self.rows()[0][2]), item)
        self.assertEqual(self.project.insights.submit(**{**args, "body": "new"}), revised)
        with self.assertRaises(Denied):
            self.project.insights.revise(item["id"], **{**revise, "body": "new"}, expected_rev=1)

    def test_archived_mutations_do_not_bypass_terminal_checks(self):
        item = self.proposal()
        self.gc()
        with self.assertRaisesRegex(Denied, "decided"):
            self.project.insights.attach_evidence(item["id"], source="searcher", text="extra", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.withdraw(item["id"], source="other", reason="not owner", expected_rev=1)
        with self.assertRaises(Denied):
            self.project.insights.withdraw(item["id"], source="searcher", reason="old", expected_rev=2)
        withdrawn = self.project.insights.withdraw(item["id"], source="searcher", reason="moot", expected_rev=1)
        self.assertEqual(withdrawn["action"], "withdraw")
        self.assertEqual(json.loads(self.rows()[0][2]), item)
        self.assertFalse((self.project.root / "inbox" / f"{item['id']}.json").exists())

    def test_backup_restore_preserves_archive_and_work_item_origin(self):
        item = self.proposal(action="accept")
        decision = read_json(self.project.root / "decisions" / f"{item['id']}.json")
        self.gc()
        self.assertFalse(self.project.work_items.projection(decision["work_item"]["id"])["stale"])
        self.project.set_control(paused=True)
        destination = self.root / "archive.tar.gz"
        backup(self.project, destination)
        restore(self.config, "restored", destination)
        restored = Project(self.config, "restored")
        self.assertEqual(restored.insights.read(item["id"]), item)
        self.assertEqual(restored.insights.list(pending=False), [])
        self.assertFalse(restored.work_items.projection(decision["work_item"]["id"])["stale"])

    def test_archived_snapshot_remains_a_maintenance_reference(self):
        item = self.proposal()
        path = self.project.root / "inbox" / f"{item['id']}.json"
        item["base_snapshot"] = self.project.snapshots.get()["id"]
        write_json(path, item)
        self.gc()
        self.assertEqual(audit(self.project)["references"]["insight_snapshots"], 1)

    def test_revise_failure_from_archive_retries_without_losing_either_history(self):
        item = self.proposal()
        self.gc()
        write = write_json
        def fail(path, *args, **kwargs):
            if path.parent.name == "inbox":
                raise OSError("injected reopen write")
            return write(path, *args, **kwargs)
        revise = dict(source="searcher", title="Title", body="Improved", base_snapshot=None, expected_rev=1)
        with patch("mizu.insights.write_json", side_effect=fail):
            with self.assertRaises(OSError):
                self.project.insights.revise(item["id"], **revise)
        self.assertEqual(self.project.insights.read(item["id"]), item)
        self.assertEqual(self.project.insights.revise(item["id"], **revise)["rev"], 2)
        self.assertEqual(json.loads(self.rows()[0][2]), item)
        self.assertEqual(len(self.project.insights.history(item["id"])), 1)
        self.project.insights.decide(item["id"], "reject", "reviewed", "", "run", expected_rev=2)
        decision_path = self.project.root / "decisions" / f"{item['id']}.json"
        decision = read_json(decision_path)
        decision["created_at"] = dt.datetime.fromtimestamp(self.epoch - 32 * 86400, dt.timezone.utc).isoformat()
        write_json(decision_path, decision)
        self.assertEqual(self.gc(), 1)
        self.assertEqual([row[1] for row in self.rows()], [1, 2])
        self.assertEqual(self.project.insights.read(item["id"])["body"], "Improved")
        self.assertEqual(len(self.project.insights.history(item["id"])), 1)

    def test_conflicting_crash_leftover_keeps_live_copy(self):
        item = self.proposal()
        self.gc()
        # Simulate a conflicting local inbox record; never replace archive audit.
        write_json(self.project.root / "inbox" / f"{item['id']}.json", {**item, "body": "conflict"})
        self.assertEqual(self.gc(), 0)
        self.assertEqual(json.loads(self.rows()[0][2]), item)
        self.assertEqual(self.project.insights.read(item["id"])["body"], "conflict")

    def test_backup_recovers_hot_journal_before_copy(self):
        item = self.proposal()
        self.gc()
        path = self.project.root / insight_archive.NAME
        # Real killed SQLite transaction, not a mocked commit. Force cache spill
        # so a plain database copy without its hot journal would be unsafe.
        subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE insights SET record=?', ('x' * 200000,))
os._exit(0)
""", str(path)], check=True)
        self.assertTrue(Path(str(path) + "-journal").is_file())
        self.project.set_control(paused=True)
        destination = self.root / "hot-journal.tar.gz"
        backup(self.project, destination)
        restore(self.config, "recovered", destination)
        restored = Project(self.config, "recovered")
        self.assertEqual(restored.insights.read(item["id"]), item)
        self.assertEqual(self.project.insights.read(item["id"]), item)

    def test_unreadable_archive_refuses_backup_and_snapshot_reclamation(self):
        self.proposal()
        self.gc()
        (self.project.root / insight_archive.NAME).write_bytes(b"not a SQLite database")
        self.project.set_control(paused=True)
        with self.assertRaises(Denied):
            backup(self.project, self.root / "bad.tar.gz")
        with self.assertRaises(Denied):
            reclaim(self.project, apply=True)

    def test_identity_repair_from_archived_record_does_not_reopen(self):
        item = self.proposal(origin="caller")
        self.gc()
        (self.project.root / "insight-ids" / f"{item['id']}.json").unlink()
        args = dict(source="searcher", title="Title", body="Body", base_snapshot=None)
        with self.assertRaises(Denied):
            self.project.insights.submit(insight_id=item["id"], **{**args, "source": "other"})
        self.assertEqual(self.project.insights.submit(insight_id=item["id"], **args), item)
        self.assertEqual(self.project.insights.revise(item["id"], **args, expected_rev=1), item)
        self.assertFalse((self.project.root / "inbox" / f"{item['id']}.json").exists())

    def test_archived_decision_retry_preserves_original_event(self):
        # Do not fake-age this record: keyed retry validates the exact original
        # immutable event and must repair/return it just as for a live insight.
        item = self.project.insights.submit(source="searcher", title="Title", body="Body", base_snapshot=None)
        decision = self.project.insights.decide(item["id"], "reject", "reason", "", "first", expected_rev=1)
        decided = dt.datetime.fromisoformat(decision["created_at"]).timestamp()
        with patch("mizu.insights.time.time", return_value=decided + 32 * 86400):
            self.assertEqual(self.project.insights.gc_decided(), 1)
        self.assertEqual(self.project.insights.decide(item["id"], "reject", "reason", "", "retry", expected_rev=1), decision)
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "accept", "reason", "", "retry", expected_rev=1)
