"""Promotion-time conversion of retained insight identities into archive rows."""
import contextlib
import sqlite3

from pathlib import Path

from support import Fixture, forget_ledger
from mizu import insight_archive, insight_identity_migration, install_migration, record_migration
from mizu.errors import Denied
from mizu.fs import canonical, read_json, write_json


class InsightIdentityMigrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project.set_control(paused=True)

    def lost(self, key="a" * 32, rev=None):
        item = self.project.insights.submit(source="searcher", title="Lost title", body="Lost body",
                                            base_snapshot=None, insight_id=key)
        (self.project.root / "inbox" / f"{key}.json").unlink()
        forget_ledger(self.project.root)
        path = self.project.root / "insight-ids" / f"{key}.json"
        value = read_json(path)
        if rev is not None:
            value["rev"] = rev
            write_json(path, value)
        return item, path, value

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db:
            return db.execute("SELECT seq, id, rev, record, decision, identity FROM insights ORDER BY seq").fetchall()

    def test_preflight_finds_the_exact_family_without_writing(self):
        item, path, value = self.lost(rev=4)
        self.assertTrue(insight_identity_migration.present(self.project))
        report = insight_identity_migration.plan(self.project)
        self.assertTrue(report["ok"])
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["entries_examined"], 1)
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["records"], [dict(id=item["id"])])
        self.assertFalse(record_migration.flat_records_present(self.project))
        self.assertFalse((self.project.root / "insight-archive.sqlite3").exists())
        self.assertEqual(read_json(path), value)

    def test_corrupt_identity_is_refused_without_a_partial_conversion(self):
        _, path, _ = self.lost("a" * 32)
        path.write_bytes(b"{not json")
        report = insight_identity_migration.plan(self.project)
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["path"], "insight-ids")
        with self.assertRaisesRegex(Denied, "Corrupt retained insight history"):
            insight_identity_migration.apply(self.project)
        self.assertFalse((self.project.root / "insight-archive.sqlite3").exists())

    def test_conflicting_archive_row_is_refused_without_silent_fallback(self):
        item, _, _ = self.lost()
        with contextlib.closing(sqlite3.connect(self.project.root / insight_archive.NAME)) as db, db:
            db.execute("CREATE TABLE insights (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "id TEXT NOT NULL, rev INTEGER NOT NULL, record TEXT NOT NULL, "
                       "decision TEXT NOT NULL, identity TEXT NOT NULL, UNIQUE (id, rev))")
            db.execute("INSERT INTO insights (id, rev, record, decision, identity) "
                       "VALUES (?, 0, '{\"other\": true}', 'null', '{}')", (item["id"],))
        before = self.rows()
        with self.assertRaisesRegex(Denied, "read-back verification"):
            insight_identity_migration.apply(self.project)
        self.assertEqual(self.rows(), before)

    def test_conversion_is_verified_and_preserves_existing_reads(self):
        item, path, value = self.lost(rev=4)
        raw = path.read_bytes()
        report = insight_identity_migration.apply(self.project)
        self.assertEqual((report["entries_examined"], report["candidates"], report["converted"]),
                         (1, 1, 1))
        self.assertTrue(report["ok"])
        self.assertFalse(insight_identity_migration.present(self.project))
        self.assertEqual(path.read_bytes(), raw)
        row, = self.rows()
        expected = canonical({"id": item["id"], "body_unavailable": True,
                              "created_at": value["created_at"], "rev": 4}).decode()
        self.assertEqual(row[1:], (item["id"], 0, expected, "null", canonical(value).decode()))
        # The legacy reader still reports the same explicit refusal, and the
        # converted row now carries the same tombstone in archive pages.
        with self.assertRaisesRegex(Denied, "body unavailable"):
            self.project.insights.read(item["id"])
        page = self.project.insights.list_page(scope="archive")
        self.assertEqual(page["items"], [{"id": item["id"], "body_unavailable": True,
                                          "rev": 4, "created_at": value["created_at"]}])
        self.assertEqual(self.project.insights.list_page()["items"], [])
        self.assertEqual(record_migration.flat_records_present(self.project), False)

    def test_retry_after_conversion_is_a_no_op(self):
        _, _, _ = self.lost("a" * 32)
        first = insight_identity_migration.apply(self.project)
        rows = self.rows()
        second = insight_identity_migration.apply(self.project)
        self.assertEqual((second["candidates"], second["converted"]), (0, 0))
        self.assertEqual(self.rows(), rows)
        self.assertEqual(first["converted"], 1)

    def test_install_registers_the_family_and_keeps_its_backup_receipt(self):
        item, path, _ = self.lost()
        original = path.read_bytes()
        rows = install_migration.convert(self.config, [self.project.root], self.root / "release")
        row, = rows
        self.assertEqual(row["status"], "converted")
        conversion = row["conversions"]["migrate-insight-identities"]
        self.assertEqual(conversion["status"], "converted")
        self.assertEqual((conversion["candidates"], conversion["converted"]), (1, 1))
        self.assertEqual(read_json(Path(row["receipt"]))["conversions"]["migrate-insight-identities"],
                         conversion)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.rows()[0][1], item["id"])
        self.assertFalse(install_migration.convert(self.config, [self.project.root],
                                                   self.root / "release"))
