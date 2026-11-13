"""Insight and decision index: crash repair, concurrency, rebuild equality, upgrade and audit."""
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
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.insight_index import INSIGHTS


def rows(root):
    """Every derived insight row, for equality checks (the generation excluded)."""
    with contextlib.closing(sqlite3.connect(Path(root) / ledger_module.NAME)) as db, db:
        return (db.execute("SELECT * FROM insights ORDER BY id").fetchall(),
                db.execute("SELECT * FROM decisions ORDER BY insight_id").fetchall(),
                db.execute("SELECT key FROM intents WHERE domain='insights'").fetchall())


class InsightLedgerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.insights

    def submit(self, title="Idea", source="worker", **kwargs):
        return self.store.submit(source=source, title=title, body="Body " + title,
                                 base_snapshot=None, **kwargs)

    def rebuilt(self):
        expected = rows(self.project.root)
        forget_ledger(self.project.root)
        self.store.unresolved_counts([])
        return expected, rows(self.project.root)

    def test_rebuild_equals_incremental_index_on_random_history(self):
        rng = random.Random(20261113)
        ids = []
        for step in range(80):
            choice = rng.random()
            if not ids or choice < 0.3:
                ids.append(self.submit(f"Idea {step}", source=rng.choice(["worker", "reviewer"]))["id"])
                continue
            current = self.store.read(rng.choice(ids))
            action = rng.choice(["revise", "evidence", "defer", "reject", "accept", "withdraw", "gc"])
            try:
                if action == "revise":
                    self.store.revise(current["id"], source=current["source"], title=current["title"],
                                      body=current["body"] + "!", base_snapshot=None)
                elif action == "evidence":
                    self.store.attach_evidence(current["id"], source="worker", text="Seen",
                                               expected_rev=current["rev"])
                elif action == "withdraw":
                    self.store.withdraw(current["id"], source=current["source"], reason="Obsolete")
                elif action == "gc":
                    with patch("mizu.insights.time.time", return_value=4102444800):
                        self.store.gc_decided(keep_days=1)
                else:
                    self.store.decide(current["id"], action, "Because", "later" if action == "defer" else "",
                                      "run", expected_rev=current["rev"],
                                      **({"work_item_intent": "none"} if action == "accept" else {}))
            except Denied:
                pass  # Invalid random operations write nothing.
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)
        self.assertEqual(incremental[2], [])
        audit = self.project.work_items.audit()
        self.assertTrue(audit["insights"]["consistent"], audit["insights"])

    def test_generation_follows_content_not_decisions_or_noops(self):
        first = self.submit()
        generation = self.store.generation()
        self.assertEqual(self.submit()["id"], first["id"])  # Replay of open content.
        self.store.decide(first["id"], "reject", "No", "", "run", expected_rev=1)
        self.assertEqual(self.store.generation(), generation)
        other = self.submit("Other")
        self.assertNotEqual(self.store.generation(), generation)
        generation = self.store.generation()
        self.store.revise(other["id"], source="worker", title="Other", body="Body Other",
                          base_snapshot=None)  # Same content: no-op.
        self.assertEqual(self.store.generation(), generation)
        self.store.attach_evidence(other["id"], source="worker", text="Seen", expected_rev=1)
        self.assertNotEqual(self.store.generation(), generation)
        # A rebuild of identical files keeps a reader's view of change consistent.
        self.assertRegex(self.store.generation(), r"^[0-9a-f]{64}$")

    def crash_after_file(self, body):
        script = f"""
import os, sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'tests')!r}]
from mizu.config import load
from mizu.project import Project
from mizu import ledger
project = Project(load(Path({str(self.file)!r})), {self.project.name!r})
original = ledger.Ledger._repair
def crash(self, db, names, *, recover):
    if db.execute("SELECT 1 FROM intents WHERE domain='insights'").fetchone():
        os._exit(17)
    return original(self, db, names, recover=recover)
ledger.Ledger._repair = crash
{body}
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_crash_between_file_and_index_commit_is_repaired(self):
        first = self.submit()
        self.store.unresolved_counts([])
        self.crash_after_file(f"project.insights.decide({first['id']!r}, 'defer', 'Later', 'soon', 'run', expected_rev=1)")
        self.assertEqual(len(rows(self.project.root)[2]), 1)
        # A reader replays the announced key privately and already sees the decision.
        self.assertEqual(self.store.has_current_defer(), True)  # Locked use repairs it in the index.
        self.assertEqual(rows(self.project.root)[2], [])
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)
        before = self.store.generation()
        self.crash_after_file("project.insights.submit(source='worker', title='Crash', body='B', base_snapshot=None)")
        self.assertEqual(self.store.unresolved_counts(["worker"]), {"worker": 2})
        self.assertNotEqual(self.store.generation(), before)  # The replayed write advanced it exactly once.
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)

    def test_failed_write_leaves_index_equal_to_files(self):
        self.submit()
        with patch("mizu.insights.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.submit("Second")
        self.assertEqual(rows(self.project.root)[2], [])
        self.assertEqual(self.store.unresolved_counts(["worker"]), {"worker": 1})

    def test_concurrent_writers_and_readers_observe_only_committed_states(self):
        script = f"""
import sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}]
from mizu.config import load
from mizu.errors import Denied
from mizu.project import Project
project = Project(load(Path({str(self.file)!r})), {self.project.name!r})
for n in range(10):
    project.insights.submit(source=sys.argv[1], title=f"T{{n}}", body="B", base_snapshot=None)
"""
        writers = [subprocess.Popen([sys.executable, "-c", script, name]) for name in ("worker", "reviewer", "searcher")]
        seen, errors, done = [], [], threading.Event()

        def read():
            while not done.is_set():
                try:
                    counts = self.store.unresolved_counts(["worker", "reviewer", "searcher"])
                    seen.append((sum(counts.values()), len(list((self.project.root / "inbox").glob("*.json")))))
                    self.store.generation()
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
        # An index count never runs ahead of the files it was committed from.
        self.assertTrue(all(count <= files for count, files in seen))
        self.assertEqual(self.store.unresolved_counts(["worker", "reviewer", "searcher"]),
                         {"worker": 10, "reviewer": 10, "searcher": 10})
        incremental, rebuilt = self.rebuilt()
        self.assertEqual(incremental, rebuilt)

    def test_reads_never_open_inbox_or_decision_files(self):
        item = self.submit()
        self.store.decide(item["id"], "defer", "Later", "soon", "run", expected_rev=1)
        self.store.unresolved_counts([])  # The index exists and is current.
        refuse = AssertionError("current-state read touched a record file")
        readers = {
            "generation": self.store.generation, "list": self.store.list,
            "projection": self.store.projection, "scan_source": lambda: self.store.scan_source("worker"),
            "counts": lambda: self.store.unresolved_counts(["worker"]),
            "defer": self.store.has_current_defer, "page": self.store.list_page,
            "origin": lambda: self.project.work_items.origin_sources(),
        }
        with patch("mizu.insights.safe_read", side_effect=refuse), \
                patch("mizu.insights.read_json", side_effect=refuse), \
                patch("mizu.insight_index.safe_read", side_effect=refuse), \
                patch("mizu.insight_archive._connect", side_effect=refuse):
            for name, reader in readers.items():
                with self.subTest(reader=name):
                    reader()

    def test_older_schema_and_single_domain_index_are_rebuilt(self):
        item = self.submit()
        self.project.work_items.count_open()
        expected = rows(self.project.root)
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='insights.schema'", (INSIGHTS.schema - 1,))
            db.execute("DELETE FROM insights")
        self.assertEqual(self.store.scan_source("worker")[0]["id"], item["id"])  # Private build.
        self.assertEqual(self.store.unresolved_counts(["worker"]), {"worker": 1})  # Persisted by a locked use.
        self.assertEqual(rows(self.project.root), expected)
        # A stage-1 file (one domain, intents keyed by item) is replaced whole.
        (self.project.root / ledger_module.NAME).unlink()
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('schema', 1), ('complete', 1)")
            db.execute("CREATE TABLE items(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE intents(item_id TEXT PRIMARY KEY, source_event TEXT)")
        self.assertEqual(self.store.unresolved_counts(["worker"]), {"worker": 1})
        self.assertEqual(self.project.work_items.count_open(), 0)
        self.assertEqual(rows(self.project.root), expected)

    def test_audit_detects_each_corruption_class_and_divergence(self):
        item = self.submit()
        decided = self.submit("Decided")
        self.store.decide(decided["id"], "accept", "Reviewed", "", "run", expected_rev=1,
                          work_item_intent="none")
        revised = self.submit("Revised")
        self.store.revise(revised["id"], source="worker", title="Revised", body="Changed", base_snapshot=None)
        self.assertTrue(self.project.work_items.audit()["insights"]["consistent"])
        inbox = self.project.root / "inbox" / f"{item['id']}.json"
        pointer = self.project.root / "decisions" / f"{decided['id']}.json"
        prior = self.project.root / "insight-revisions" / f"{revised['id']}.r1.json"
        original = {path: path.read_bytes() for path in (inbox, pointer, prior)}
        record = json.loads(pointer.read_text())
        corruptions = {
            "malformed inbox record": (inbox, lambda: inbox.write_text("{")),
            "malformed pointer": (pointer, lambda: pointer.write_text("[]")),
            "pointer differs from its event": (pointer, lambda: write_json(
                pointer, {**record, "reason": "Forged"})),
            "decision ahead of insight": (pointer, lambda: write_json(pointer, {**record, "rev": 5})),
            "malformed prior revision": (prior, lambda: prior.write_text("{")),
        }
        for name, (path, corrupt) in corruptions.items():
            with self.subTest(corruption=name):
                corrupt()
                result = self.project.work_items.audit()["insights"]
                self.assertFalse(result["consistent"])
                self.assertGreaterEqual(result["corrupt_count"], 1, result)
                path.write_bytes(original[path])
                forget_ledger(self.project.root)
                self.assertTrue(self.project.work_items.audit(repair=True)["insights"]["consistent"] or
                                self.project.work_items.audit()["insights"]["consistent"])
        # The index alone disagreeing with valid files is divergence, repaired on request.
        self.project.work_items.audit(repair=True)
        with contextlib.closing(sqlite3.connect(self.project.root / ledger_module.NAME)) as db, db:
            db.execute("UPDATE insights SET rev=9 WHERE id=?", (item["id"],))
        result = self.project.work_items.audit()["insights"]
        self.assertEqual((result["divergent"], result["consistent"]), ([item["id"]], False))
        self.assertTrue(self.project.work_items.audit(repair=True)["insights"]["repaired"])
        self.assertTrue(self.project.work_items.audit()["insights"]["consistent"])

    def test_tampered_pointer_fails_origin_reads_only_after_rebuild(self):
        proposal = self.submit()
        accepted = self.store.decide(proposal["id"], "accept", "Reviewed", "", "run", expected_rev=1)
        item_id = accepted["work_item"]["id"]
        revised = self.store.revise(proposal["id"], source="worker", title="Idea", body="Changed",
                                    base_snapshot=None)
        self.assertEqual(self.project.work_items.projection(item_id)["current_origin_rev"], revised["rev"])
        pointer = self.project.root / "decisions" / f"{proposal['id']}.json"
        write_json(pointer, {**json.loads(pointer.read_text()), "reason": "Forged"})
        self.project.work_items.projection(item_id)  # Reads trust the index.
        forget_ledger(self.project.root)
        with self.assertRaises(Denied):
            self.project.work_items.projection(item_id)


if __name__ == "__main__":
    unittest.main()
