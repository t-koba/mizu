"""Atomic per-role doing slots and history-derived, visibility-only hold age."""
import json
import multiprocessing
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, read_json, text_preview, write_json
from mizu.insights import Insights
from mizu.project import Project
from mizu.runtime import prompt_delta_for, prompt_for
from mizu.work_items import MAX_OPEN_ROW_BYTES, WorkItems


# Host synchronization bounds, not product deadlines; no sustained load.
CHILD_WAIT_SECONDS = 60
CHILD_RELEASE_SECONDS = 600


def claim(root, item_id, actor, ready, release, results):
    root = Path(root)
    store = WorkItems(root, None, Insights(root))
    ready.put(actor)
    if not release.wait(CHILD_RELEASE_SECONDS):
        results.put((actor, "timeout"))
        return
    try:
        store.transition(item_id, expected_rev=1, status="doing", actor=actor)
        results.put((actor, "claimed"))
    except Denied as exc:
        results.put((actor, str(exc)))


class WorkItemHoldTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def item(self, key):
        return self.store.create(idempotency_key=key, title=key, actor="worker")["record"]["work_item"]["id"]

    def change(self, item_id, status="doing", **kwargs):
        return self.store.transition(item_id, expected_rev=self.store.get(item_id)["rev"],
                                     status=status, actor=kwargs.pop("actor", "worker"), **kwargs)

    def snapshot(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run="hold-test", outcome="done", summary="Delivery",
            verification={"passed": True, "code_digest": captured["code_digest"], "run": "hold-test"})
        self.project.snapshots.publish(snapshot)
        return snapshot

    def race(self, actors):
        context = multiprocessing.get_context("spawn")
        ready, results = context.Queue(), context.Queue()
        release = context.Event()
        items = [self.item(f"race-{index}") for index in range(len(actors))]
        children = [context.Process(target=claim, args=(str(self.project.root), item_id, actor,
                                                       ready, release, results))
                    for item_id, actor in zip(items, actors)]
        try:
            for child in children:
                child.start()
            for _ in children:
                ready.get(timeout=CHILD_WAIT_SECONDS)
            release.set()
            observed = [results.get(timeout=CHILD_WAIT_SECONDS) for _ in children]
            for child in children:
                child.join(CHILD_WAIT_SECONDS)
                self.assertEqual(child.exitcode, 0)
            return observed, items
        finally:
            release.set()
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(CHILD_WAIT_SECONDS)
            ready.close()
            results.close()

    def test_concurrent_same_role_claims_share_one_slot(self):
        observed, items = self.race(["worker", "worker"])
        self.assertEqual(sum(result == "claimed" for _, result in observed), 1)
        self.assertEqual(sum("doing slots full" in result for _, result in observed), 1)
        states = [self.store.get(item_id) for item_id in items]
        self.assertEqual(sorted(item["rev"] for item in states), [1, 2])
        self.assertEqual(sum(item["status"] == "doing" for item in states), 1)

    def test_concurrent_distinct_roles_have_independent_slots(self):
        observed, items = self.race(["worker", "reviewer"])
        self.assertEqual(sorted(observed), [("reviewer", "claimed"), ("worker", "claimed")])
        self.assertEqual({self.store.get(item_id)["holder"] for item_id in items}, {"worker", "reviewer"})

    def test_blocked_items_do_not_count_and_denied_claim_is_not_appended(self):
        first, second = self.item("first"), self.item("second")
        self.change(first)
        self.change(first, "blocked", wait={"kind": "deadline", "at": "2030-01-01T00:00:00+00:00"})
        self.change(second)
        with self.assertRaisesRegex(Denied, "doing slots full"):
            self.change(first)
        self.assertEqual(self.store.get(first)["rev"], 3)
        self.change(second, "open")
        self.change(first)
        self.assertEqual(self.store.get(first)["holder"], "worker")

    def test_legacy_over_limit_recovery_and_same_doing_revisions(self):
        self.store.doing_limit = 4
        items = [self.item(f"legacy-{i}") for i in range(4)]
        for item_id in items:
            self.change(item_id)
        self.store = Project(self.config, self.project.name).work_items  # Default one, existing four.
        self.assertEqual(self.store.page_open()["total"], 4)
        extra = self.item("extra")
        with self.assertRaisesRegex(Denied, "doing slots full"):
            self.change(extra)
        self.change(items[0], scope="Revised scope", reason="Clarify scope")
        self.change(items[0], "open")
        self.change(items[1], "blocked")
        self.change(items[2], "dropped", reason="No longer needed")
        snapshot = self.snapshot()
        self.change(items[3], snapshot_id=snapshot["id"])
        self.change(items[3], "done", snapshot_id=snapshot["id"])
        self.change(extra)
        self.assertEqual(self.store.get(extra)["status"], "doing")

    def test_scope_binding_and_rollback_do_not_reset_uninterrupted_age(self):
        item_id = self.item("age")
        start = "2030-01-01T00:00:00+00:00"
        with patch("mizu.work_items.now", return_value=start):
            self.change(item_id)
        snapshot = self.snapshot()
        # Wall-clock rollback affects updated_at, not the hold's start.
        with patch("mizu.work_items.now", return_value="2029-12-31T23:00:00+00:00"):
            self.change(item_id, scope="Clarified", reason="Clarify scope")
            self.change(item_id, snapshot_id=snapshot["id"])
        with patch("mizu.work_items.now", return_value="2030-01-02T00:00:00+00:00"):
            view = self.store.explain(item_id)
            self.assertEqual(view["doing_since"], start)
            self.assertEqual(view["doing_age_seconds"], 86400)
            self.assertTrue(view["doing_stale"])
        self.change(item_id, "blocked")
        with patch("mizu.work_items.now", return_value="2030-01-03T00:00:00+00:00"):
            self.change(item_id)
            view = self.store.projection(item_id)
            self.assertEqual(view["doing_since"], "2030-01-03T00:00:00+00:00")
            self.assertEqual(view["doing_age_seconds"], 0)
            self.assertFalse(view["doing_stale"])

    def test_exact_boundary_future_start_and_timezone_offsets(self):
        item_id = self.item("boundary")
        start = "2030-01-01T09:00:00+09:00"
        with patch("mizu.work_items.now", return_value=start):
            self.change(item_id)
        for timestamp, age, stale in (
                ("2029-12-31T23:59:59+00:00", 0, False),
                ("2030-01-01T00:00:00+00:00", 0, False),
                ("2030-01-01T23:59:59.999999+00:00", 86399, False),
                ("2030-01-02T00:00:00+00:00", 86400, True),
                ("2030-01-02T00:00:00.000001+00:00", 86400, True)):
            with self.subTest(timestamp=timestamp), patch("mizu.work_items.now", return_value=timestamp):
                view = self.store.projection(item_id)
                self.assertEqual(view["doing_age_seconds"], age)
                self.assertIs(view["doing_stale"], stale)

    def test_age_in_open_readonly_explain_submitter_and_full_delta_prompts(self):
        proposal = self.project.insights.submit(source="worker", title="Age", body="Review", base_snapshot=None)
        item_id = self.project.insights.decide(proposal["id"], "accept", "Take", "", "run",
                                                expected_rev=1)["work_item"]["id"]
        start = "2030-01-01T00:00:00+00:00"
        with patch("mizu.work_items.now", return_value=start):
            self.change(item_id)
        context = self.context()
        signature = self.store.page_ready(role_name="worker")["signature"]
        with patch("mizu.work_items.now", return_value="2030-01-02T00:00:00+00:00"):
            self.assertEqual(self.store.page_ready(role_name="worker")["signature"], signature)
            views = [self.store.page_open()["items"][0], self.store.page_open_readonly()["items"][0],
                     self.store.explain(item_id), self.store.submitter_status(proposal["id"])]
            for prompt in (prompt_for, lambda context: prompt_delta_for(context, [])):
                opening = json.loads(prompt(context))
                ready = opening["ready_work"]["items"][0]
                self.assertIn("age_seconds", ready)
                views.extend([opening["open_work_items"]["items"][0], ready])
            for view in views:
                self.assertEqual(view["doing_since"], start)
                self.assertEqual(view["doing_age_seconds"], 86400)
                self.assertTrue(view["doing_stale"])
                self.assertFalse(view["stale"])  # Origin staleness is independent.
        self.change(item_id, "open")
        view = self.store.explain(item_id)
        self.assertIsNone(view["doing_since"])
        self.assertIsNone(view["doing_age_seconds"])
        self.assertFalse(view["doing_stale"])

    def test_hold_fields_preserve_the_fixed_compact_row_cap(self):
        item = {"id": "wi_" + "1" * 32, "title": "t", "origin": None,
                "status": "doing", "rev": 2, "holder": "worker",
                "created_at": "2030-01-01T00:00:00+00:00",
                "updated_at": "2030-01-01T00:00:00+00:00",
                "description": "\x01" * 560, "scope": "\x01" * 560, "reason": "r" * 1024}
        view = {"current_origin_rev": None, "stale": False, "redecision": None,
                "doing_since": item["updated_at"], "doing_age_seconds": 0, "doing_stale": False}
        _, row = self.store._open_row(item, view)
        self.assertLessEqual(len(canonical(row)), MAX_OPEN_ROW_BYTES)
        self.assertTrue(row["description_truncated"] or row["scope_truncated"]
                        or row["reason_truncated"])

        # Before hold-age fields, the complete 1024-byte previews fit under
        # the same existing 8192-byte cap; keep that row readable by trimming
        # previews only when the new metadata actually needs the bytes.
        old_row = dict(row)
        for source, preview_key, truncated_key in (
                ("description", "description_preview", "description_truncated"),
                ("scope", "scope_preview", "scope_truncated"),
                ("reason", "reason_preview", "reason_truncated")):
            old_row[preview_key], old_row[truncated_key] = text_preview(item[source], 1024)
        for key in ("doing_since", "doing_age_seconds", "doing_stale"):
            old_row.pop(key)
        old_bytes = len(canonical(old_row))
        self.assertLessEqual(old_bytes, MAX_OPEN_ROW_BYTES)
        self.assertLess(MAX_OPEN_ROW_BYTES - old_bytes, 100)

    def test_malformed_earlier_timestamp_fails_closed_in_reads_and_claims(self):
        first, second = self.item("bad-history"), self.item("new-claim")
        self.change(first)
        self.change(first, scope="Revised", reason="Clarify")
        path = self.project.root / "work-items" / first / "2.json"
        original = read_json(path)
        for timestamp in ("bad", "2030-01-01T00:00:00", None, 5):
            write_json(path, {**original, "updated_at": timestamp})
            for read in (lambda: self.store.explain(first), self.store.page_open,
                         self.store.page_open_readonly, lambda: self.change(second)):
                with self.subTest(timestamp=timestamp), self.assertRaises(Denied):
                    read()
            self.assertEqual(self.store.get(second)["rev"], 1)
        write_json(path, original)

    def test_config_defaults_bounds_overrides_and_threshold(self):
        self.assertEqual(self.config.limits.work_items_doing, 1)
        self.assertEqual(self.config.limits.work_items_stale_seconds, 86400)
        self.assertIsNone(self.config.roles["worker"].work_items_doing)
        base = "\n".join(line for line in self.file.read_text().splitlines()
                         if not line.startswith(("work_items_doing =", "work_items_stale_seconds =")))

        def configured(project_limit, role_limit, threshold):
            self.file.write_text(base.replace("[limits]\n", f"[limits]\nwork_items_doing = {project_limit}\n"
                                             f"work_items_stale_seconds = {threshold}\n")
                                 .replace("[roles.worker]", f"[roles.worker]\nwork_items_doing = {role_limit}"))
            return load(self.file)

        for limit in (1, 100):
            config = configured(limit, limit, 31536000)
            self.assertEqual(config.limits.work_items_doing, limit)
            self.assertEqual(config.roles["worker"].work_items_doing, limit)
        for bad in (0, -1, 101, "true", "1.5", '"2"'):
            for project_limit, role_limit in ((bad, 1), (1, bad)):
                with self.subTest(project=project_limit, role=role_limit), self.assertRaises(ConfigError):
                    configured(project_limit, role_limit, 86400)
        for bad in (0, -1, 31536001, "true", "1.5", '"24"'):
            with self.subTest(threshold=bad), self.assertRaises(ConfigError):
                configured(1, 1, bad)
        config = configured(2, 1, 1)
        store = Project(config, self.project.name).work_items
        self.assertEqual(store.doing_limit, 2)
        self.assertEqual(store.doing_limits["worker"], 1)
        config = configured(1, 2, 1)
        store = Project(config, self.project.name).work_items
        first, second, third = [self.item(f"override-{i}") for i in range(3)]
        for item_id in (first, second):
            store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        with self.assertRaisesRegex(Denied, "doing slots full"):
            store.transition(third, expected_rev=1, status="doing", actor="worker")
        store.transition(third, expected_rev=1, status="doing", actor="reviewer")
        with patch("mizu.work_items.now", return_value="2030-01-01T00:00:00+00:00"):
            store.transition(third, expected_rev=2, status="open", actor="reviewer")
            store.transition(third, expected_rev=3, status="doing", actor="reviewer")
        with patch("mizu.work_items.now", return_value="2030-01-01T00:00:01+00:00"):
            self.assertTrue(store.projection(third)["doing_stale"])
