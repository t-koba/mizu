"""Read-view reuse must preserve validation, output and request isolation."""
import dataclasses
from unittest.mock import patch

from support import Fixture, ScriptDriver, bind_delivery
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.runtime import Engine
from mizu.snapshot import Snapshots
from mizu.work_item_events import closed_page, progress
from mizu.work_item_read_view import _Records, readonly_view


class ReadViewTests(Fixture):
    def prepare(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run="run-1", outcome="done",
            summary="Verified", verification={
                "passed": True, "code_digest": captured["code_digest"], "run": "run-1"})
        self.project.snapshots.publish(snapshot)
        for n in range(3):
            created = self.project.work_items.create(
                actor="worker", title=f"Item {n}", idempotency_key=f"item-{n}")
            seed = created["record"]["work_item"]
            bind_delivery(self.project, seed["id"], snapshot["id"])
            self.project.work_items.transition(
                seed["id"], expected_rev=3, status="done", actor="worker",
                reason="Verified", snapshot_id=snapshot["id"])
        return snapshot

    def render(self, store):
        roles = {name: dataclasses.replace(role, work_item_events=("done",),
                                           work_item_sources="any")
                 for name, role in self.config.roles.items()}
        return {"open": store.page_open_readonly(),
                "closed": closed_page(store, roles),
                "delivery": progress(store, roles)}

    def test_one_manifest_validation_and_exact_result(self):
        self.prepare()
        expected = self.render(self.project.work_items)
        calls = []
        original = Snapshots.validate_manifest

        def validate(store, snapshot_id, record):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record)

        with patch.object(Snapshots, "validate_manifest", validate):
            self.assertEqual(self.render(readonly_view(self.project.work_items)), expected)
        self.assertEqual(len(calls), 1)

    def test_cli_uses_one_validation_for_all_sections(self):
        from types import SimpleNamespace
        from mizu.cli import _cmd_work_items
        self.prepare()
        calls = []
        original = Snapshots.validate_manifest

        def validate(store, snapshot_id, record):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record)

        with patch.object(Snapshots, "validate_manifest", validate):
            result = _cmd_work_items(self.config, self.project,
                                     SimpleNamespace(cursor=None, limit=100, closed=100))
        self.assertEqual(len(result["closed"]["items"]), 3)
        self.assertEqual(len(calls), 1)
        self.assertIs(type(self.project.snapshots), Snapshots)

    def test_next_request_sees_corruption(self):
        snapshot = self.prepare()
        self.render(readonly_view(self.project.work_items))
        snapshot["summary"] = "tampered"
        write_json(self.project.root / "snapshots" / f"{snapshot['id']}.json", snapshot)
        with self.assertRaises(Denied):
            self.render(readonly_view(self.project.work_items))

    def test_next_request_sees_transition(self):
        self.prepare()
        self.render(readonly_view(self.project.work_items))
        created = self.project.work_items.create(
            actor="worker", title="New", idempotency_key="new")
        item_id = created["record"]["work_item"]["id"]
        current = readonly_view(self.project.work_items).page_open_readonly()
        self.assertEqual([row["id"] for row in current["items"]], [item_id])

    def test_changed_history_is_not_hidden_by_cached_projection(self):
        created = self.project.work_items.create(
            actor="worker", title="New", idempotency_key="new")
        item_id = created["record"]["work_item"]["id"]
        view = readonly_view(self.project.work_items)
        self.assertEqual(view.page_open_readonly()["items"][0]["status"], "open")
        self.project.work_items.transition(
            item_id, expected_rev=1, status="doing", actor="worker")
        self.assertEqual(view.page_open_readonly()["items"][0]["status"], "doing")

    def dispatch(self, *, event_trigger=True, continuation=False):
        role = dataclasses.replace(self.config.roles["worker"], on_change=False,
                                   work_item_events=("done", "dropped"), work_item_sources="any")
        return Engine(self.config, driver=ScriptDriver())._dispatch_state(
            self.project, "worker", role, event_trigger=event_trigger, continuation=continuation)

    def archive_delivery_snapshot(self):
        snapshot = self.prepare()
        # Corruption must be detected through the item chains, not merely by
        # the dispatch's independent read of the current snapshot pointer.
        captured = self.project.snapshots.capture_files(self.project.workspace)
        current = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Current", run=None,
            outcome="wait", summary="Current dispatch input")
        self.project.snapshots.publish(current)
        return snapshot

    def test_dispatch_reuses_validation_without_changing_results(self):
        self.archive_delivery_snapshot()
        original = Snapshots.validate_manifest
        calls = []

        def validate(store, snapshot_id, record):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record)

        store = self.project.work_items
        snapshots = store.snapshots
        for event_trigger, continuation in ((True, False), (False, False), (True, True)):
            with self.subTest(event_trigger=event_trigger, continuation=continuation):
                with patch.object(Snapshots, "validate_manifest", validate):
                    with patch("mizu.runtime.readonly_view", side_effect=lambda value: value):
                        expected = self.dispatch(event_trigger=event_trigger, continuation=continuation)
                    uncached = len(calls)
                    calls.clear()
                    actual = self.dispatch(event_trigger=event_trigger, continuation=continuation)
                    self.assertEqual(actual, expected)
                    self.assertEqual(len(actual["terminal_events"]), 3)
                    self.assertEqual(len(calls), 2)  # current pointer + shared delivery snapshot
                    self.assertLess(len(calls), uncached)
                    calls.clear()
        self.assertIs(self.project.work_items, store)
        self.assertIs(store.snapshots, snapshots)
        self.assertNotIn("_read", vars(store))
        self.assertNotIn("get", vars(snapshots))

    def test_next_dispatch_sees_history_and_ready_work_changes(self):
        self.assertEqual(self.dispatch()["skip"]["skipped"], "no-event")
        created = self.project.work_items.create(
            actor="worker", title="New", idempotency_key="new")
        item_id = created["record"]["work_item"]["id"]
        self.assertEqual(self.dispatch()["_dispatch"].admission, "ready_work")
        self.project.work_items.transition(
            item_id, expected_rev=1, status="dropped", actor="worker", reason="No longer needed")
        current = self.dispatch()
        self.assertEqual(current["_dispatch"].admission, "work_item")
        self.assertEqual([row["id"] for row in current["terminal_events"]], [item_id])
        with patch("mizu.runtime.readonly_view", side_effect=lambda value: value):
            self.assertEqual(self.dispatch(), current)

    def test_next_dispatch_revalidates_historical_manifest(self):
        snapshot = self.archive_delivery_snapshot()
        self.assertEqual(len(self.dispatch()["terminal_events"]), 3)
        snapshot["summary"] = "tampered"
        write_json(self.project.root / "snapshots" / f"{snapshot['id']}.json", snapshot)
        with self.assertRaises(Denied):
            self.dispatch()
        with patch("mizu.runtime.readonly_view", side_effect=lambda value: value):
            with self.assertRaises(Denied):
                self.dispatch()

    def test_cached_paths_still_check_collisions_and_exclusions(self):
        view = readonly_view(self.project.work_items)
        view.snapshots.validate_paths({"allowed": {}})
        with self.assertRaises(Denied):
            view.snapshots.validate_paths({"allowed": {}, "allowed/child": {}})
        with self.assertRaises(Denied):
            view.snapshots.validate_paths({"../escape": {}})
        view.snapshots.excludes = ("private",)
        with self.assertRaises(Denied):
            view.snapshots.validate_paths({"private/file": {}})

    def test_record_budget_eviction_and_copy_isolation(self):
        calls = []

        def read(key):
            calls.append(key)
            return {"key": key}

        cache = _Records(read, 15)
        cache("a")["key"] = "changed"
        self.assertEqual(cache("a"), {"key": "a"})
        cache("b")
        cache("a")
        self.assertEqual(calls, ["a", "b", "a"])
        self.assertLessEqual(cache.size, cache.budget)
