"""Persistent coalesced work-item cursors: bounded reads, repair and snapshots."""
import contextlib
import os
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from support import Fixture, ROOT, forget_ledger
from mizu.cli import execute, parser
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import canonical, lock
from mizu.work_item_index import ITEMS
from mizu.work_items import WorkItems


class WorkItemChangesTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.counter = 0

    def item(self, **kwargs):
        self.counter += 1
        return self.store.create(idempotency_key=f"change-{self.counter}", title="Work",
                                 actor="worker", **kwargs)["record"]["work_item"]

    def page(self, **kwargs):
        return self.store.page_changes_readonly(**kwargs)

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.project.root / "ledger.sqlite3")) as db, db:
            yield db

    def test_bootstrap_restart_poll_coalescing_and_terminal_rows(self):
        first, second = self.item(), self.item()
        page = self.page(limit=1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["items"][0]["item"], first)
        self.assertEqual(page, self.page(limit=1))  # Deterministic with no intervening write.
        restarted = WorkItems(self.project.root, self.project.snapshots, self.project.ledger)
        page = restarted.page_changes_readonly(cursor=page["next_cursor"], limit=1)
        self.assertFalse(page["has_more"])
        self.assertEqual(page["items"][0]["item"], second)
        cursor = page["next_cursor"]
        self.assertEqual(self.page(cursor=cursor)["items"], [])
        self.store.transition(first["id"], expected_rev=1, status="doing", actor="worker")
        self.store.transition(first["id"], expected_rev=2, status="dropped", actor="worker", reason="Done")
        page = self.page(cursor=cursor)
        self.assertEqual([(p["item"]["id"], p["item"]["rev"], p["item"]["status"])
                          for p in page["items"]], [(first["id"], 3, "dropped")])
        self.assertFalse(page["has_more"])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM item_changes").fetchone()[0], 1)
            plan = db.execute("EXPLAIN QUERY PLAN SELECT item FROM items WHERE change_seq>? "
                              "AND change_seq<=? ORDER BY change_seq LIMIT ?", (2, 4, 2)).fetchall()
        self.assertIn("SEARCH items USING INDEX items_changes", str(plan))

    def test_updates_to_visited_and_unvisited_rows_are_never_lost(self):
        first, second, third = self.item(), self.item(), self.item()
        page = self.page(limit=1)
        cursor = page["next_cursor"]
        for item, actor in ((first, "worker"), (second, "reviewer")):
            self.store.transition(item["id"], expected_rev=1, status="doing", actor=actor)
        new = self.item()
        page = self.page(cursor=cursor, limit=1)
        # second moved past the fixed fence; only third remains in this sequence.
        self.assertEqual([p["item"]["id"] for p in page["items"]], [third["id"]])
        self.assertFalse(page["has_more"])
        seen = {}
        while True:
            page = self.page(cursor=page["next_cursor"], limit=1)
            seen.update({p["item"]["id"]: p["item"]["rev"] for p in page["items"]})
            if not page["has_more"]:
                break
        self.assertEqual(seen, {first["id"]: 2, second["id"]: 2, new["id"]: 1})
        # Moving every remaining row out of an active sequence yields an empty
        # completed page, but its checkpoint must not skip those moved rows.
        page = self.page(limit=1)
        for item_id in (first["id"], second["id"], new["id"]):
            item = self.store.get(item_id)
            self.store.transition(item_id, expected_rev=item["rev"], status="dropped",
                                  actor="worker", reason="Finished")
        empty = self.page(cursor=page["next_cursor"])
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])
        self.assertEqual(len(self.page(cursor=empty["next_cursor"])["items"]), 3)

    def test_one_page_uses_one_snapshot_even_when_writer_commits_mid_read(self):
        item = self.item()
        original = ITEMS.change_position

        def update_after_snapshot(db, cursor):
            position = original(db, cursor)
            self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
            return position

        with patch.object(ITEMS, "change_position", side_effect=update_after_snapshot):
            page = self.page()
        self.assertEqual(page["items"][0]["item"]["rev"], 1)
        follow = self.page(cursor=page["next_cursor"])
        self.assertEqual(follow["items"][0]["item"]["rev"], 2)

    def test_invalid_incomplete_foreign_and_ahead_cursors_fail_closed(self):
        self.item()
        cursor = self.page()["next_cursor"]
        with self.db() as db:
            key, high = db.execute("SELECT token_key, seq FROM item_changes").fetchone()
        for value in ("", cursor[:-1], cursor + "x", "wc1." + "!" * 64,
                      cursor[:10] + ("A" if cursor[10] != "A" else "B") + cursor[11:],
                      ITEMS.change_cursor(os.urandom(32), 0), ITEMS.change_cursor(key, high + 1),
                      ITEMS.change_cursor(key, 0, high + 1), ITEMS.change_cursor(key, high, high)):
            with self.subTest(cursor=value), self.assertRaisesRegex(Denied, "full resync"):
                self.page(cursor=value)
        for value in (0, 101, True, "1"):
            with self.subTest(limit=value), self.assertRaises(Denied):
                self.page(limit=value)

    def test_rebuild_schema_change_missing_partial_and_noop_repair(self):
        item = self.item()
        cursor = self.page()["next_cursor"]
        with lock(self.project.root / "locks" / "insights.lock"):
            with self.store.committing([item["id"]]):
                pass  # An intent without a source change is not an update.
        self.assertEqual(self.page(cursor=cursor)["items"], [])
        for kind in ("schema", "partial", "missing", "explicit"):
            cursor = self.page()["next_cursor"]
            if kind == "missing":
                forget_ledger(self.project.root)
            elif kind in ("schema", "partial"):
                with self.db() as db:
                    db.execute("UPDATE meta SET value=0 WHERE key=?",
                               ("items.schema" if kind == "schema" else "items.complete",))
            else:
                with lock(self.project.root / "locks" / "insights.lock"):
                    self.store._ledger.rebuild("items")
            if kind != "explicit":
                with self.assertRaisesRegex(Denied, "index unavailable"):
                    self.page(cursor=cursor)
                # The recovery/bootstrap view remains available without persisting an index.
                self.assertEqual(self.store.page_open_readonly()["total"], 1)
                self.store.audit(repair=True)
            with self.assertRaisesRegex(Denied, "full resync"):
                self.page(cursor=cursor)
            self.assertEqual(self.page()["items"][0]["item"], item)

    def crash(self, item_id, phase):
        script = f'''
import os, sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'tests')!r}]
from mizu.config import load
from mizu.project import Project
from mizu import ledger
from mizu.fs import lock
from mizu.work_item_index import ITEMS
project = Project(load(Path({str(self.file)!r})), {self.project.name!r})
original_repair, original_replay = ledger.Ledger._repair, ledger.Ledger._replay
phase = {phase!r}
def repair(self, db, names, *, recover):
    pending = db.execute("SELECT 1 FROM intents WHERE domain='items'").fetchone()
    if pending and phase == 'source':
        os._exit(17)
    result = original_repair(self, db, names, recover=recover)
    if pending and phase == 'committed':
        os._exit(17)
    return result
def replay(self, db, *args, **kwargs):
    result = original_replay(self, db, *args, **kwargs)
    if phase == 'index':
        os._exit(17)
    return result
ledger.Ledger._repair, ledger.Ledger._replay = repair, replay
if phase == 'rebuild':
    original_put = ITEMS.put
    def put(db, entry):
        original_put(db, entry)
        os._exit(17)
    ITEMS.put = put
    with lock(project.root / 'locks' / 'insights.lock'):
        project.work_items._ledger.rebuild('items')
elif phase == 'intent':
    with lock(project.root / 'locks' / 'insights.lock'), project.work_items.committing([{item_id!r}]):
        os._exit(17)
else:
    project.work_items.transition({item_id!r}, expected_rev=1, status='dropped', actor='worker', reason='Finished')
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_crash_restart_at_each_commit_boundary(self):
        for phase in ("intent", "source", "index", "committed"):
            with self.subTest(phase=phase):
                item = self.item()
                cursor = self.page()["next_cursor"]
                with self.db() as db:
                    before = db.execute("SELECT seq FROM item_changes").fetchone()[0]
                self.crash(item["id"], phase)
                with self.db() as db:
                    high = db.execute("SELECT seq FROM item_changes").fetchone()[0]
                    rev = db.execute("SELECT rev FROM items WHERE id=?", (item["id"],)).fetchone()[0]
                self.assertEqual(high, before + (phase == "committed"))
                self.assertEqual(rev, 2 if phase == "committed" else 1)
                if phase != "committed":
                    # No ephemeral repair may issue a position that a later
                    # persistent replay could assign to a different update.
                    with self.assertRaisesRegex(Denied, "awaiting repair"):
                        self.page(cursor=cursor)
                recovered = self.store.get(item["id"])
                page = self.page(cursor=cursor)
                self.assertEqual(recovered["rev"], 1 if phase == "intent" else 2)
                self.assertEqual([p["item"] for p in page["items"]],
                                 [] if phase == "intent" else [recovered])
                self.assertEqual(self.page(cursor=page["next_cursor"])["items"], [])

    def test_unexpected_index_removal_invalidates_prior_cursor(self):
        item = self.item()
        cursor = self.page()["next_cursor"]
        with self.db() as db:
            ITEMS.replace(db, item["id"], [])
        with self.assertRaisesRegex(Denied, "full resync"):
            self.page(cursor=cursor)

    def test_crashed_rebuild_never_issues_temporary_cursors(self):
        first, second = self.item(), self.item()
        cursor = self.page(limit=1)["next_cursor"]
        self.crash(first["id"], "rebuild")
        with self.assertRaisesRegex(Denied, "index unavailable"):
            self.page(cursor=cursor)
        self.assertEqual(self.store.page_open_readonly()["total"], 2)
        self.store.get(first["id"])  # Resume and persist the incomplete build.
        with self.assertRaisesRegex(Denied, "full resync"):
            self.page(cursor=cursor)
        self.assertEqual({p["item"]["id"] for p in self.page()["items"]},
                         {first["id"], second["id"]})

    def test_empty_checkpoint_later_creation_and_cas_still_authoritative(self):
        # A persisted empty index can issue a zero checkpoint; a missing one cannot.
        self.store.count_open()
        empty = self.page()
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])
        item = self.item()
        page = self.page(cursor=empty["next_cursor"])
        self.assertEqual(page["items"][0]["item"], item)
        self.store.transition(item["id"], expected_rev=1, status="doing", actor="worker")
        with self.assertRaises(Denied):
            self.store.transition(item["id"], expected_rev=1, status="dropped",
                                  actor="worker", reason="Stale")
        self.assertEqual(self.page(cursor=page["next_cursor"])["items"][0]["item"]["rev"], 2)

    def test_byte_pagination_and_fail_closed_bounds(self):
        for _ in range(4):
            self.item(description="x" * 2000)
        with patch("mizu.work_items.MAX_OPEN_PAGE_BYTES", 7000):
            page = self.page(limit=100)
            self.assertTrue(page["has_more"])
            self.assertLessEqual(len(canonical(page)), 7000)
            seen = len(page["items"])
            while page["has_more"]:
                page = self.page(cursor=page["next_cursor"], limit=100)
                seen += len(page["items"])
            self.assertEqual(seen, 4)
        with patch("mizu.work_items.MAX_PROJECTION_BYTES", 1), self.assertRaises(LimitExceeded):
            self.page()

    def test_cli_readonly_contract_and_mode_exclusions(self):
        self.item()
        args = parser().parse_args(["--config", str(self.file), "work-items", self.project.name,
                                   "--changes", "--limit", "1"])
        # No inventory, history replay, locks, subscriptions or live-wait reads.
        with patch("mizu.work_item_events.progress", side_effect=AssertionError("inventory")), \
                patch.object(WorkItems, "ledger_entries", side_effect=AssertionError("history")), \
                patch("mizu.work_items.lock", side_effect=AssertionError("lock")):
            page = execute(args)
            self.assertEqual(len(page["items"]), 1)
            args = parser().parse_args(["--config", str(self.file), "work-items", self.project.name,
                                       "--change-cursor", page["next_cursor"]])
            self.assertEqual(execute(args)["items"], [])
        for other in ("--audit", "--repair", "--closed"):
            argv = ["--config", str(self.file), "work-items", self.project.name, "--changes", other]
            if other == "--closed":
                argv.append("0")
            with self.subTest(option=other), self.assertRaises(ConfigError):
                execute(parser().parse_args(argv))
