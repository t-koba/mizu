"""Bounded cursor paging over closed work items for readers holding only work_items_read.

Uses disposable fixtures; no deployment or live ledger is touched.
"""
from unittest.mock import patch

from support import Fixture, bind_delivery
from mizu import work_item_operations
from mizu import work_items as wi
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.work_items import (
    MAX_CLOSED_PAGE_BYTES,
    MAX_CLOSED_PAGE_ROWS,
    MAX_CLOSED_ROW_BYTES,
)


class ClosedPagingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def make_open(self, title):
        proposal = self.project.insights.submit(
            source="worker", title=title, body="Body", base_snapshot=None)
        self.project.insights.decide(proposal["id"], "accept", "reviewed", "",
                                     f"run-{title}", expected_rev=1)
        item_id = self.store.page_open()["items"][-1]["id"]
        return proposal["id"], item_id

    def drop(self, item_id, stamp):
        with patch.object(wi, "now", return_value=stamp):
            self.store.transition(item_id, expected_rev=1, status="dropped",
                                  actor="worker", reason=f"Drop {item_id}")

    def complete(self, item_id, stamp, run):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": run}
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run=run, outcome="done",
            summary="Snapshot", verification=verification)
        self.project.snapshots.publish(snapshot)
        self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
        with patch.object(wi, "now", return_value=stamp):
            self.store.transition(item_id, expected_rev=3, status="done", actor="worker",
                                  reason=f"Finish {item_id}", snapshot_id=snapshot["id"])

    def read(self, **args):
        # Minimal context: only a project is needed, no grant or mutation.
        return work_item_operations.read(type("Ctx", (), {"project": self.project})(), args)

    def test_cursor_reaches_every_closed_item_and_matches_ledger_count(self):
        for index in range(6):
            _, item_id = self.make_open(f"Item{index}")
            self.drop(item_id, f"2030-01-{index + 1:02d}T00:00:00+00:00")
        expected = {row["id"] for row in self.store.page_closed(limit=100)["items"]}
        self.assertEqual(len(expected), 6)

        seen, cursor, pages = [], None, 0
        while True:
            args = {"closed": True, "limit": 2}
            if cursor is not None:
                args["cursor"] = cursor
            page = self.read(**args)
            pages += 1
            self.assertLessEqual(len(page["items"]), 2)
            self.assertLessEqual(len(canonical(page)), MAX_CLOSED_PAGE_BYTES)
            for row in page["items"]:
                self.assertLessEqual(len(canonical(row)), MAX_CLOSED_ROW_BYTES)
                self.assertIn(row["status"], ("done", "dropped"))
            seen.extend(row["id"] for row in page["items"])
            self.assertEqual(page["total"], 6)
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10)
        self.assertEqual(len(seen), 6)
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(set(seen)), len(seen))  # no repeats

    def test_status_filter_narrows_page_without_capping_total(self):
        for index in range(2):
            _, item_id = self.make_open(f"Done{index}")
            self.complete(item_id, f"2030-02-{index + 1:02d}T00:00:00+00:00", f"done-{index}")
        for index in range(3):
            _, item_id = self.make_open(f"Drop{index}")
            self.drop(item_id, f"2030-03-{index + 1:02d}T00:00:00+00:00")
        done = self.read(closed=True, limit=100, status="done")
        self.assertEqual(done["total"], 2)
        self.assertEqual(done["total"], len(done["items"]))
        self.assertTrue(all(row["status"] == "done" for row in done["items"]))
        dropped = self.read(closed=True, limit=100, status="dropped")
        self.assertEqual(dropped["total"], 3)
        self.assertTrue(all(row["status"] == "dropped" for row in dropped["items"]))
        unfiltered = self.read(closed=True, limit=100)
        self.assertEqual(unfiltered["total"], 5)

    def test_origin_filter_narrows_page(self):
        origins = []
        for index in range(3):
            origin, item_id = self.make_open(f"Origin{index}")
            origins.append(origin)
            self.drop(item_id, f"2030-04-{index + 1:02d}T00:00:00+00:00")
        filtered = self.read(closed=True, limit=100, origin=origins[1])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(len(filtered["items"]), 1)

    def test_terminal_time_range_narrows_page(self):
        for index in range(4):
            _, item_id = self.make_open(f"Time{index}")
            self.drop(item_id, f"2030-05-{index + 1:02d}T00:00:00+00:00")
        filtered = self.read(closed=True, limit=100,
                             closed_after="2030-05-02T00:00:00+00:00",
                             closed_before="2030-05-03T00:00:00+00:00")
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(len(filtered["items"]), 2)

    def test_open_page_and_by_id_are_unchanged(self):
        _, closed_id = self.make_open("Closed")
        self.drop(closed_id, "2030-06-01T00:00:00+00:00")
        _, open_id = self.make_open("StillOpen")
        open_page = self.read(limit=100)
        self.assertEqual([row["id"] for row in open_page["items"]], [open_id])
        by_id = self.read(id=open_id, after_rev=0)
        self.assertEqual(by_id["item"]["id"], open_id)
        self.assertEqual(by_id["history_total"], 1)
        closed_page = self.read(closed=True, limit=100)
        self.assertEqual([row["id"] for row in closed_page["items"]], [closed_id])

    def test_fail_closed_on_bad_arguments_and_cursors(self):
        with self.assertRaises(Denied):
            self.read(closed=True, limit=MAX_CLOSED_PAGE_ROWS + 1)
        with self.assertRaises(Denied):
            self.read(closed=True, status="open")
        with self.assertRaises(Denied):
            self.read(closed=True, cursor="wc2|bogus")
        with self.assertRaises(Denied):
            self.read(status="done")  # filter without closed=true
        with self.assertRaises(Denied):
            self.read(closed=True, after_rev=1)
        _, item_id = self.make_open("ById")
        with self.assertRaises(Denied):
            self.read(id=item_id, closed=True)
        with self.assertRaises(Denied):
            self.read(closed=True, closed_after="2030-06-03T00:00:00+00:00",
                      closed_before="2030-06-01T00:00:00+00:00")
        with self.assertRaises(Denied):
            self.read(closed=True, closed_after="2030-06-01T00:00:00")  # naive timestamp
        with self.assertRaises(Denied):
            self.read(closed="yes")  # non-boolean selector

    def test_schema_declares_closed_mode_and_filters(self):
        from mizu.protocol import DEFINITIONS
        props = DEFINITIONS["work_items_read"][1]["properties"]
        for key in ("closed", "status", "origin", "closed_after", "closed_before"):
            self.assertIn(key, props)
