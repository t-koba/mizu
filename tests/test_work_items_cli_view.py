"""Read-only work-items CLI view: open plus newest-closed, no locks or recovery."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from support import Fixture, bind_delivery, forget_ledger
from mizu.cli import parser, execute
from mizu import work_items as work_items_module
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import canonical
from mizu.work_items import (
    MAX_CLOSED_PAGE_BYTES,
    MAX_CLOSED_ROW_BYTES,
    MAX_OPEN_PAGE_BYTES,
    MAX_OPEN_ROW_BYTES,
)


class WorkItemsCliViewTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def make_open(self, title, body="Body text"):
        item = self.store.create(idempotency_key=title, title=title, actor="worker",
                                 description=body)["record"]["work_item"]
        return {"id": item["id"]}, {"work_item": item}, item["id"]

    def make_snapshot(self, run="run-1"):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        verification = {"passed": True, "code_digest": captured["code_digest"], "run": run}
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run=run, outcome="done",
            summary="Snapshot", verification=verification)
        self.project.snapshots.publish(snapshot)
        return snapshot

    def test_parser_defaults(self):
        args = parser().parse_args(["work-items", "sample"])
        self.assertEqual(args.project, "sample")
        self.assertIsNone(args.limit)  # The store owns the default page size.
        self.assertIsNone(args.cursor)
        self.assertIsNone(args.closed)
        self.assertFalse(args.audit)
        self.assertFalse(args.repair)

    def test_parser_custom_values(self):
        args = parser().parse_args(
            ["work-items", "sample", "--limit", "2", "--cursor", "tok", "--closed", "3"])
        self.assertEqual((args.limit, args.cursor, args.closed), (2, "tok", 3))
        audit = parser().parse_args(["work-items", "sample", "--audit", "--repair"])
        self.assertEqual((audit.audit, audit.repair), (True, True))

    def test_execute_uses_read_only_project(self):
        self.make_open("First item")
        seen = {}

        import mizu.project as proj

        real_project = proj.Project

        def spy(config, name, *, read_only=False):
            seen["read_only"] = read_only
            seen["name"] = name
            return real_project(config, name, read_only=read_only)

        with patch("mizu.project.Project", side_effect=spy):
            args = parser().parse_args(["--config", str(self.file), "work-items", "sample"])
            result = execute(args)
        self.assertTrue(seen.get("read_only") is True)
        self.assertEqual(seen.get("name"), "sample")
        self.assertIn("open", result)
        self.assertIn("closed", result)

    def test_execute_audit_is_an_explicit_writable_maintenance_variant(self):
        _, _, item_id = self.make_open("Audited item")
        seen = {}
        import mizu.project as proj
        real_project = proj.Project

        def spy(config, name, *, read_only=False):
            seen["read_only"] = read_only
            return real_project(config, name, read_only=read_only)

        with patch("mizu.project.Project", side_effect=spy):
            result = execute(parser().parse_args(
                ["--config", str(self.file), "work-items", "sample", "--audit"]))
        self.assertTrue(seen.get("read_only") is False)
        self.assertEqual((result["items"], result["consistent"], result["repaired"]), (1, True, False))
        with self.assertRaises(ConfigError):
            execute(parser().parse_args(
                ["--config", str(self.file), "work-items", "sample", "--repair"]))

    def test_execute_schema_open_matches_page_open(self):
        _, _, item_id = self.make_open("Open item")
        args = parser().parse_args(["--config", str(self.file), "work-items", "sample"])
        result = execute(args)
        expected_open = self.store.page_open()
        self.assertEqual(result["open"], expected_open)
        self.assertEqual(result["open"]["total"], 1)
        for key in ("items", "total", "cursor", "next_cursor",
                    "overflow_count", "truncated"):
            self.assertIn(key, result["open"])
        row = result["open"]["items"][0]
        for key in ("id", "title", "origin", "status", "rev", "holder",
                    "created_at", "updated_at"):
            self.assertIn(key, row)
        self.assertLessEqual(len(canonical(row)), MAX_OPEN_ROW_BYTES)
        self.assertLessEqual(len(canonical(result["open"])), MAX_OPEN_PAGE_BYTES)
        self.assertEqual(row["id"], item_id)

    def test_closed_fields_and_newest_first(self):
        import mizu.work_items as wi
        stamps = ["2030-01-01T00:00:00+00:00", "2030-02-01T00:00:00+00:00",
                  "2030-03-01T00:00:00+00:00"]
        ids = []
        for index, title in enumerate(["Alpha", "Beta", "Gamma"]):
            _, _, item_id = self.make_open(title)
            ids.append(item_id)
            with patch.object(wi, "now", return_value=stamps[index]):
                if index < 2:
                    self.store.transition(item_id, expected_rev=1, status="dropped",
                                          actor="worker", reason=f"Drop {title}")
                else:
                    snapshot = self.make_snapshot(run=f"run-{index}")
                    self.store.transition(item_id, expected_rev=1, status="doing",
                                          actor="worker")
                    bind_delivery(self.project, item_id, snapshot["id"], expected_rev=2)
                    with patch.object(wi, "now", return_value=stamps[index]):
                        self.store.transition(item_id, expected_rev=3, status="done",
                                              actor="worker", reason=f"Finish {title}",
                                              snapshot_id=snapshot["id"])
        args = parser().parse_args(
            ["--config", str(self.file), "work-items", "sample", "--closed", "10"])
        result = execute(args)
        closed = result["closed"]
        self.assertEqual(closed["total"], 3)
        self.assertEqual([row["id"] for row in closed["items"]], list(reversed(ids)))
        self.assertFalse(closed["truncated"])
        self.assertEqual(closed["overflow_count"], 0)
        for row in closed["items"]:
            for key in ("id", "title", "status", "origin", "rev",
                        "updated_at", "actor", "snapshot_id"):
                self.assertIn(key, row)
            self.assertIn(row["status"], ("done", "dropped"))
            if row["status"] == "done":
                self.assertIsInstance(row["snapshot_id"], str)
                self.assertIn("reason_preview", row)
            else:
                self.assertIsNone(row["snapshot_id"])
            if "reason_preview" in row:
                self.assertIn("reason_truncated", row)
            self.assertLessEqual(len(canonical(row)), MAX_CLOSED_ROW_BYTES)
        self.assertLessEqual(len(canonical(closed)), MAX_CLOSED_PAGE_BYTES)
        # done row carries the verified snapshot id
        done_rows = [row for row in closed["items"] if row["status"] == "done"]
        self.assertEqual(len(done_rows), 1)

    def test_closed_limit_zero_reports_total_without_rows(self):
        self.make_open("Solo")
        self.store.transition(self.store.page_open()["items"][0]["id"],
                              expected_rev=1, status="dropped",
                              actor="worker", reason="No longer needed")
        args = parser().parse_args(
            ["--config", str(self.file), "work-items", "sample", "--closed", "0"])
        result = execute(args)
        self.assertEqual(result["closed"]["items"], [])
        self.assertEqual(result["closed"]["total"], 1)
        self.assertEqual(result["closed"]["overflow_count"], 1)
        self.assertTrue(result["closed"]["truncated"])

    def test_closed_limit_two_returns_newest_two_with_overflow(self):
        import mizu.work_items as wi
        stamps = ["2030-01-01T00:00:00+00:00", "2030-02-01T00:00:00+00:00",
                  "2030-03-01T00:00:00+00:00"]
        ids = []
        for index, title in enumerate(["Alpha", "Beta", "Gamma"]):
            _, _, item_id = self.make_open(title)
            ids.append(item_id)
            with patch.object(wi, "now", return_value=stamps[index]):
                self.store.transition(item_id, expected_rev=1, status="dropped",
                                      actor="worker", reason=f"Drop {title}")
        # Read-only page with limit 2 over 3 closed items: newest two only.
        page = self.store.page_closed_readonly(limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual([row["id"] for row in page["items"]], list(reversed(ids))[0:2])
        self.assertEqual(page["overflow_count"], 1)
        self.assertTrue(page["truncated"])
        # Same metadata through the CLI view.
        args = parser().parse_args(
            ["--config", str(self.file), "work-items", "sample", "--closed", "2"])
        result = execute(args)
        closed = result["closed"]
        self.assertEqual(closed["total"], 3)
        self.assertEqual([row["id"] for row in closed["items"]], list(reversed(ids))[0:2])
        self.assertEqual(closed["overflow_count"], 1)
        self.assertTrue(closed["truncated"])

    def test_open_limit_cursor_overflow(self):
        ids = [self.make_open(f"Item {i}")[2] for i in range(4)]
        args = parser().parse_args(
            ["--config", str(self.file), "work-items", "sample",
             "--limit", "2", "--closed", "0"])
        first = execute(args)
        self.assertEqual(len(first["open"]["items"]), 2)
        self.assertEqual(first["open"]["total"], 4)
        self.assertEqual(first["open"]["overflow_count"], 2)
        self.assertTrue(first["open"]["truncated"])
        self.assertIsNotNone(first["open"]["next_cursor"])
        cursor = first["open"]["next_cursor"]
        args2 = parser().parse_args(
            ["--config", str(self.file), "work-items", "sample",
             "--limit", "2", "--cursor", cursor, "--closed", "0"])
        second = execute(args2)
        self.assertEqual(len(second["open"]["items"]), 2)
        self.assertEqual(second["open"]["overflow_count"], 0)
        self.assertFalse(second["open"]["truncated"])
        self.assertIsNone(second["open"]["next_cursor"])
        seen = [row["id"] for row in first["open"]["items"]]
        seen += [row["id"] for row in second["open"]["items"]]
        self.assertEqual(len(set(seen)), 4)
        # Paging past the final row yields no rows but keeps the total.
        from mizu.work_items import _open_cursor_token
        cursor_walk, seen_walk, page = None, [], None
        while True:
            page = self.store.page_open_readonly(cursor=cursor_walk, limit=2)
            seen_walk.extend(row["id"] for row in page["items"])
            if not page["truncated"]:
                break
            cursor_walk = page["next_cursor"]
        self.assertEqual(sorted(seen_walk), sorted(ids))
        past = self.store.page_open_readonly(
            cursor=_open_cursor_token(page["items"][-1]), limit=2)
        self.assertEqual(past["items"], [])
        self.assertEqual(past["total"], 4)

    def test_invalid_limits_fail_closed(self):
        for limit in (0, 101, True, "2"):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.store.page_open_readonly(limit=limit)
        for limit in (-1, 101, True, "1"):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.store.page_closed_readonly(limit=limit)
        self.assertEqual(self.store.page_closed_readonly(limit=0)["items"], [])
        with self.assertRaises(Denied):
            self.store.page_open_readonly(cursor="bad-cursor")

    def test_readonly_does_not_acquire_lock(self):
        self.make_open("Locked check")
        import mizu.work_items as wi
        with patch.object(wi, "lock", side_effect=AssertionError("lock must not be acquired")):
            opened = self.store.page_open_readonly()
            closed = self.store.page_closed_readonly(limit=10)
        self.assertEqual(opened["total"], 1)
        self.assertEqual(closed["total"], 0)

    def test_readonly_does_not_create_lock_file(self):
        self.make_open("No lock file")
        lock_path = self.project.root / "locks" / "insights.lock"
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        self.assertFalse(lock_path.exists())
        self.store.page_open_readonly()
        self.store.page_closed_readonly(limit=5)
        self.assertFalse(lock_path.exists())

    def _archive_origin(self, insight_id):
        import datetime as dt
        from mizu.fs import read_json
        from support import history_write_json as write_json
        dec_path = self.project.root / "decisions" / f"{insight_id}.json"
        decision = read_json(dec_path)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
        decision["created_at"] = old
        write_json(dec_path, decision)
        removed = self.project.insights.gc_decided()
        self.assertEqual(removed, 1)
        self.assertFalse((self.project.root / "inbox" / f"{insight_id}.json").exists())

