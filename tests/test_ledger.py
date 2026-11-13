"""Current-state index: crash repair, concurrency, rebuild equality and audit."""
import contextlib
import json
import random
import sqlite3
import subprocess
import sys
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
        unseeded = db.execute("SELECT event_id FROM unseeded ORDER BY event_id").fetchall()
        intents = db.execute("SELECT key FROM intents").fetchall()
        insights = db.execute("SELECT * FROM insights ORDER BY id").fetchall() if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='insights'").fetchone() else []
        decisions = db.execute("SELECT * FROM decisions ORDER BY insight_id").fetchall() if insights else []
    return items, unseeded, intents, insights, decisions


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
        proposal = self.project.insights.submit(source="worker", title=title, body="Body",
                                                base_snapshot=None)
        return self.project.insights.decide(proposal["id"], "accept", "reviewed", "", "run-1",
                                            expected_rev=1)["work_item"]

    def rebuilt(self):
        """Rows of a fresh index built from the files, in a separate store."""
        expected = index_rows(self.project.root)
        forget_ledger(self.project.root)
        WorkItems(self.project.root, self.project.snapshots, self.project.insights).count_open()
        self.project.insights.unresolved_counts([])
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
        self.assertEqual(incremental[2], [])
        self.assertTrue(self.store.audit()["consistent"])

    def test_retirement_and_legacy_rows_match_rebuild(self):
        item = self.item()
        template = self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        directory = self.project.root / "work-items" / item["id"]
        previous = template
        for rev in range(3, MAX_REVISIONS + 1):
            status = "open" if previous["status"] == "doing" else "doing"
            previous = {**template, "rev": rev, "status": status,
                        "holder": "worker" if status == "doing" else None,
                        "transition": {"from": previous["status"], "to": status}}
            write_json(directory / f"{rev}.json", previous)
        forget_ledger(self.project.root)
        self.store.transition(item["id"], expected_rev=MAX_REVISIONS, status="dropped",
                              actor="worker", reason="Exhausted")
        self.assertEqual(self.store.get(item["id"])["status"], "dropped")
        legacy = {"id": "a" * 32, "action": "accept", "reason": "old", "revisit": "",
                  "run": "run-1", "created_at": "2026-01-01T00:00:00+00:00", "rev": 1}
        self.project.insights.submit(source="worker", title="Old", body="Body", base_snapshot=None)
        write_json(self.project.root / "decision-history" / f"{'b' * 32}.json", legacy)
        forget_ledger(self.project.root)
        self.assertEqual(self.store.page_open()["unseeded_decisions"], 1)
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)

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
        self.assertEqual(len(index_rows(self.project.root)[2]), 1)
        # A reader replays the announced item privately and sees the commit.
        page = self.store.page_open_readonly()
        self.assertNotEqual(page, before)
        self.assertEqual([row["status"] for row in page["items"]], ["doing"])
        self.assertEqual(len(index_rows(self.project.root)[2]), 1)
        # The next locked use repairs exactly that item in the index itself.
        self.assertEqual(self.store.page_open()["items"], page["items"])
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental[2], [])
        self.assertEqual(incremental, rebuilt)

    def test_failed_write_leaves_index_equal_to_files(self):
        item = self.item()
        with patch("mizu.work_items.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        self.assertEqual(index_rows(self.project.root)[2], [])
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
            store = WorkItems(self.project.root, self.project.snapshots, self.project.insights)
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
            "unseeded_decisions": self.store.unseeded_decisions,
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

    def test_audit_detects_each_corruption_class_and_divergence(self):
        item = self.item()
        doing = self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        decided = self.decided()
        self.assertEqual(self.store.audit(), {
            "items": 2, "unseeded_decisions": 0, "index": "complete", "consistent": True,
            "repaired": False, "corrupt": [], "corrupt_count": 0, "divergent": [],
            "divergent_count": 0, "divergent_unseeded": [], "divergent_unseeded_count": 0,
            "insights": {"insights": 1, "decisions": 1, "index": "complete", "consistent": True,
                         "repaired": False, "corrupt": [], "corrupt_count": 0, "divergent": [],
                         "divergent_count": 0}})
        revision = self.project.root / "work-items" / item["id"] / "2.json"
        create = self.project.root / "work-items" / item["id"] / "create.json"
        seed = self.project.root / "decision-history" / f"{decided['id'][3:]}.json"
        original = {path: path.read_bytes() for path in (revision, create, seed)}
        corruptions = {
            "revision fields": (revision, lambda: write_json(revision, {**doing, "holder": "other"})),
            "malformed revision": (revision, lambda: revision.write_text("{")),
            "gap": (revision, lambda: revision.rename(revision.with_name("3.json"))),
            "seed binding": (create, lambda: create.write_text(create.read_text().replace(
                '"Work"', '"Forged"'))),
        }
        for name, (path, corrupt) in corruptions.items():
            with self.subTest(corruption=name):
                corrupt()
                result = self.store.audit(repair=True)
                self.assertFalse(result["consistent"])
                self.assertFalse(result["repaired"])
                self.assertEqual(result["corrupt_count"], 1)
                # A rebuild of the index fails closed on the same history.
                forget_ledger(self.project.root)
                with self.assertRaises(Denied):
                    self.store.page_open_readonly()
                for leftover in path.parent.glob("3.json"):
                    leftover.unlink()
                path.write_bytes(original[path])
                self.store.page_open()
                self.assertTrue(self.store.audit()["consistent"])
        # A self-consistent rewrite of a seed passes validation but not the comparison.
        seed.write_text(seed.read_text().replace('"Decided"', '"Forged"', 1))
        result = self.store.audit()
        self.assertEqual((result["divergent"], result["corrupt_count"]), ([decided["id"]], 0))
        seed.write_bytes(original[seed])
        # The index alone disagreeing with valid history is divergence, repaired on request.
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("UPDATE items SET status='done' WHERE id=?", (item["id"],))
        result = self.store.audit()
        self.assertEqual((result["divergent"], result["consistent"]), ([item["id"]], False))
        self.assertTrue(self.store.audit(repair=True)["repaired"])
        self.assertTrue(self.store.audit()["consistent"])

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
