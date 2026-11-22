"""Current-state index: crash repair, concurrency, rebuild equality and audit."""
import contextlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT, Fixture, forget_ledger
from mizu import ledger as ledger_module
from mizu.work_item_index import COLUMNS, ITEMS
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.work_items import MAX_REVISIONS, WorkItems


def index_rows(root):
    """The complete derived contents of an index file, for equality checks."""
    with contextlib.closing(sqlite3.connect(Path(root) / ledger_module.NAME)) as db, db:
        items = db.execute(f"SELECT {', '.join(COLUMNS)} FROM items ORDER BY id").fetchall()
        intents = db.execute("SELECT key FROM intents").fetchall()
    return items, intents


class LedgerPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.ledger = ledger_module.Ledger(self.root)
        self.outside = self.root.parent / "outside.sqlite3"
        with contextlib.closing(sqlite3.connect(self.outside)) as db, db:
            db.execute("CREATE TABLE original(value)")

    def assert_link_refused(self):
        before = self.outside.read_bytes()
        for create in (False, True):
            with self.subTest(create=create):
                with self.assertRaises(Denied), self.ledger._connect(create=create) as db:
                    if db is not None:
                        db.execute("CREATE TABLE probe(value)")
                self.assertEqual(self.outside.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.outside.parent.iterdir()),
                         ["outside.sqlite3", "project"])

    def test_symlink_refused_without_changing_target(self):
        try:
            self.ledger.path.symlink_to(self.outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        self.assert_link_refused()

    def test_hardlink_refused_without_changing_target(self):
        try:
            os.link(self.outside, self.ledger.path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Hardlinks unavailable: {exc}")
        self.assert_link_refused()

    def test_dangling_symlink_refused(self):
        self.outside.unlink()
        try:
            self.ledger.path.symlink_to(self.outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        for create in (False, True):
            with self.subTest(create=create), self.assertRaises(Denied):
                with self.ledger._connect(create=create):
                    pass
        self.assertFalse(self.outside.exists())

    def test_nonregular_path_refused_before_sqlite(self):
        self.ledger.path.mkdir()
        for create in (False, True):
            with self.subTest(create=create), patch.object(ledger_module.sqlite3, "connect") as connect:
                with self.assertRaises(Denied), self.ledger._connect(create=create):
                    pass
                connect.assert_not_called()

    def test_unavailable_metadata_is_not_a_missing_index(self):
        for create in (False, True):
            with self.subTest(create=create), patch.object(Path, "lstat", side_effect=PermissionError):
                with self.assertRaises(Denied), self.ledger._connect(create=create):
                    pass


class LedgerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.counter = 0

    def item(self, title="Work"):
        self.counter += 1
        return self.store.create(idempotency_key=f"key-{self.counter}", title=title,
                                 actor="worker")["record"]["work_item"]

    def decided(self, title="Decided"):
        self.counter += 1
        return self.store.create(idempotency_key=f"decided-{self.counter}", title=title,
                                 actor="reviewer")["record"]["work_item"]

    def rebuilt(self):
        """Rows of a fresh index built from the files, in a separate store."""
        expected = index_rows(self.project.root)
        forget_ledger(self.project.root)
        WorkItems(self.project.root, self.project.snapshots, self.project.ledger).count_open()
        return expected, index_rows(self.project.root)

    def test_rebuild_equals_incremental_index_on_random_history(self):
        rng = random.Random(20261112)
        ids = []
        for step in range(60):
            choice = rng.random()
            if not ids or choice < 0.25:
                ids.append((self.item() if rng.random() < 0.5 else self.decided())["id"])
                continue
            item = self.store.get(rng.choice(ids))
            if item["status"] in ("done", "dropped"):
                continue
            status = rng.choice(["doing", "open", "blocked", "dropped"])
            kwargs = {"reason": "Because"} if status in ("open", "dropped") else {}
            if status == "blocked":
                kwargs["wait"] = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}
            try:
                self.store.transition(item["id"], expected_rev=item["rev"], status=status,
                                      actor=rng.choice(["worker", "reviewer"]), **kwargs)
            except Denied:
                pass  # Invalid random transitions write nothing.
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)
        self.assertEqual(incremental[1], [])
        self.assertTrue(self.store.audit()["consistent"])

    def test_missing_outdated_and_partial_index_rebuild(self):
        item = self.item()
        expected = self.store.page_open()
        forget_ledger(self.project.root)
        # A reader never writes the index: it answers from a private build.
        self.assertEqual(self.store.page_open_readonly(), expected)
        self.assertFalse((self.project.root / ledger_module.NAME).exists())
        self.assertEqual(self.store.page_open(), expected)
        self.assertTrue((self.project.root / ledger_module.NAME).exists())
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='items.schema'", (ITEMS.schema - 1,))
            db.execute("DELETE FROM items")
        self.assertEqual(self.store.page_open_readonly(), expected)
        self.assertEqual(self.store.page_open(), expected)
        # An interrupted build resumes from the rows it already committed.
        other = self.item("Other")
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("UPDATE meta SET value=0 WHERE key='items.complete'")
            db.execute("DELETE FROM items WHERE id=?", (other["id"],))
        replayed = []
        original = self.store._ledger_row

        def counted(item_id, *args, **kwargs):
            replayed.append(item_id)
            return original(item_id, *args, **kwargs)

        with patch.object(self.store, "_ledger_row", side_effect=counted):
            self.assertEqual(self.store.count_open(), 2)
        self.assertEqual(replayed, [other["id"]])
        self.assertEqual(self.store.get(item["id"])["id"], item["id"])

    def test_rebuild_changes_generation_but_not_the_snapshot(self):
        self.item("First")
        expected = self.project.ledger.snapshot("items")
        with self.project.ledger.read("items", locked=False) as db:
            first = ledger_module.generation(db, "items")
        self.assertEqual(len(first), 32)
        self.project.ledger.rebuild("items")
        with self.project.ledger.read("items", locked=False) as db:
            second = ledger_module.generation(db, "items")
        self.assertNotEqual(first, second)
        self.assertEqual(self.project.ledger.snapshot("items"), expected)

    def test_complete_index_without_generation_gets_one_for_readers(self):
        self.item("First")
        path = self.project.root / ledger_module.NAME
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM meta WHERE key='items.generation'")
        expected = self.project.ledger.snapshot("items")
        with self.project.ledger.read("items", locked=False) as db:
            private = ledger_module.generation(db, "items")
        self.assertEqual(len(private), 32)
        # The locked recovery persists a generation into the durable index.
        with self.project.ledger.read("items", locked=True) as db:
            durable = ledger_module.generation(db, "items")
        self.assertEqual(len(durable), 32)
        self.assertEqual(self.project.ledger.snapshot("items"), expected)

    def test_read_recomputes_usability_from_backed_up_generation(self):
        item = self.item()
        self.item("Other")
        path = self.project.root / ledger_module.NAME
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            db.execute("INSERT INTO intents(domain, key) VALUES ('items', ?)", (item["id"],))

        connect = sqlite3.connect
        backups = []

        class RacingConnection(sqlite3.Connection):
            def backup(self, target, *args, **kwargs):
                # Commit a writer's incomplete generation after the reader's
                # usability transaction, immediately before copying it.
                if not backups:
                    with contextlib.closing(connect(path)) as writer, writer:
                        writer.execute("UPDATE meta SET value=0 WHERE key='items.complete'")
                        writer.execute("DELETE FROM items")
                backups.append(True)
                return super().backup(target, *args, **kwargs)

        with patch.object(ledger_module.sqlite3, "connect",
                          side_effect=lambda *a, **kw: connect(*a, factory=RacingConnection, **kw)):
            with self.store._ledger.read("items", locked=False) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM items WHERE status IN ('open', 'doing', 'blocked')"
                ).fetchone()[0]
                copied_rows = db.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM items ORDER BY id").fetchall()
        self.assertEqual(len(backups), 1)
        self.assertEqual(count, 2)
        # Private recovery must not complete the on-disk generation.
        with contextlib.closing(connect(path)) as db:
            self.assertEqual(db.execute(
                "SELECT value FROM meta WHERE key='items.complete'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        forget_ledger(self.project.root)
        self.assertEqual(self.store.count_open(), 2)
        self.assertEqual(copied_rows, index_rows(self.project.root)[0])

    def crash_after_commit(self, item_id, rev):
        """Kill a real process after its revision file is durable, before the index commit."""
        script = f"""
import os, sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'tests')!r}]
from mizu.config import load
from mizu.project import Project
from mizu import ledger
config = load(Path({str(self.file)!r}))
project = Project(config, {self.project.name!r})
original = ledger.Ledger._repair
def crash(self, db, names, *, recover):
    if db.execute("SELECT 1 FROM intents").fetchone() and os.path.exists(
            {str(self.project.root / 'work-items' / item_id / f'{rev}.json')!r}):
        os._exit(17)
    return original(self, db, names, recover=recover)
ledger.Ledger._repair = crash
project.work_items.transition({item_id!r}, expected_rev={rev - 1}, status="doing", actor="worker")
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_crash_between_file_and_index_commit_is_repaired(self):
        item = self.item()
        before = self.store.page_open_readonly()
        self.crash_after_commit(item["id"], 2)
        self.assertEqual(len(index_rows(self.project.root)[1]), 1)
        # A reader replays the announced item privately and sees the commit.
        page = self.store.page_open_readonly()
        self.assertNotEqual(page, before)
        self.assertEqual([row["status"] for row in page["items"]], ["doing"])
        self.assertEqual(len(index_rows(self.project.root)[1]), 1)
        # The next locked use repairs exactly that item in the index itself.
        # Elapsed-age fields are recomputed at read time and can tick between pages.
        dynamic_fields = {"doing_age_seconds", "doing_stale", "blocked_age_seconds"}

        def stable_rows(rows):
            return [{key: value for key, value in row.items() if key not in dynamic_fields}
                    for row in rows]

        repaired = self.store.page_open()["items"]
        self.assertEqual(stable_rows(repaired), stable_rows(page["items"]))
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental[1], [])
        self.assertEqual(incremental, rebuilt)

    def test_failed_write_leaves_index_equal_to_files(self):
        item = self.item()
        with patch("mizu.work_items.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(index_rows(self.project.root)[1], [])
        self.assertEqual(self.store.get(item["id"])["rev"], 1)

    def test_concurrent_writers_and_readers_observe_only_committed_states(self):
        items = [self.item(f"Item {n}") for n in range(3)]
        script = f"""
import sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}]
from mizu.config import load
from mizu.errors import Denied
from mizu.project import Project
project = Project(load(Path({str(self.file)!r})), {self.project.name!r})
item_id, actor = sys.argv[1], sys.argv[2]
for _ in range(12):
    current = project.work_items.get(item_id)
    status = "open" if current["status"] == "doing" else "doing"
    try:
        project.work_items.transition(item_id, expected_rev=current["rev"], status=status,
                                      actor=actor, reason="Cycle" if status == "open" else None)
    except Denied:
        pass
"""
        writers = [subprocess.Popen([sys.executable, "-c", script, items[n % 3]["id"], actor])
                   for n, actor in enumerate(("worker", "reviewer", "worker", "searcher"))]
        observed, errors, done = [], [], threading.Event()

        def read():
            store = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
            while not done.is_set():
                try:
                    for row in store.current_items():
                        observed.append(row)
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(exc)

        readers = [threading.Thread(target=read) for _ in range(2)]
        for reader in readers:
            reader.start()
        for writer in writers:
            self.assertEqual(writer.wait(timeout=120), 0)
        done.set()
        for reader in readers:
            reader.join()
        self.assertEqual(errors, [])
        self.assertTrue(observed)
        for row in observed:
            path = self.project.root / "work-items" / row["id"] / f"{row['rev']}.json"
            committed = json.loads(path.read_text()) if row["rev"] > 1 else self.store.history(row["id"]).__next__()
            self.assertEqual(row, committed)
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)

    def test_reads_never_open_history_or_manifests(self):
        item = self.item()
        self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        self.store.page_open()  # The index exists and is current.
        readers = {
            "page_open": self.store.page_open, "page_open_readonly": self.store.page_open_readonly,
            "page_ready": self.store.page_ready, "page_open_for_order": self.store.page_open_for_order,
            "page_closed_readonly": self.store.page_closed_readonly,
            "open_state_readonly": self.store.open_state_readonly,
            "count_open": self.store.count_open, "count_open_readonly": self.store.count_open_readonly,
            "get": lambda: self.store.get(item["id"]), "current": lambda: self.store.current(item["id"]),
            "current_items": self.store.current_items, "projection": lambda: self.store.projection(item["id"]),
            "creators": self.store.creators,
            "closed_items": lambda: self.store.closed_items(limit=10),
        }
        refuse = AssertionError("current-state read touched history")
        with patch.object(self.store, "_read", side_effect=refuse), \
                patch.object(self.store, "_history_revisions", side_effect=refuse), \
                patch.object(self.project.snapshots, "binding", side_effect=refuse), \
                patch("mizu.work_items.os.scandir", side_effect=refuse):
            for name, reader in readers.items():
                with self.subTest(reader=name):
                    reader()

    def test_audit_validates_delivery_manifests_strictly(self):
        item = self.item()
        self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        store = self.project.snapshots
        snapshot = store.create(store.capture_files(self.project.workspace), goal="Goal",
                                state="State", run="run-1", outcome="done", summary="Bound")
        store.publish(snapshot)
        self.store.transition(item["id"], expected_rev=2, status="doing", actor="worker",
                              snapshot_id=snapshot["id"])
        self.assertTrue(self.store.audit()["consistent"])
        manifest = store.root / "snapshots" / f"{snapshot['id']}.json"
        value = json.loads(manifest.read_text())
        value["summary"] = "Forged"
        manifest.write_text(json.dumps(value))
        result = self.store.audit()
        self.assertEqual([entry["id"] for entry in result["corrupt"]], [item["id"]])
        # Reads keep answering from the index; the audit is where tamper shows.
        self.assertEqual(self.store.page_open_readonly()["total"], 1)


if __name__ == "__main__":
    unittest.main()
