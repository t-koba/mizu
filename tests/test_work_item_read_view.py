"""Read-view reuse must preserve validation, output and request isolation."""
import dataclasses
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver, bind_delivery
from mizu import acceptance, work_items as work_items_module
from mizu.cli import _cmd_work_items
from mizu.config import Limits
from mizu.distribution import public_source_identity
from mizu.publication import Publication
from mizu.work_items import WorkItems
from mizu.errors import Denied
from mizu.fs import canonical, read_json, write_json
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

        def validate(store, snapshot_id, record, **kwargs):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record, **kwargs)

        # Binding checks are memoized per process; start from a cold memo.
        self.project.snapshots._binding_cache.clear()
        with patch.object(Snapshots, "validate_manifest", validate):
            self.assertEqual(self.render(readonly_view(self.project.work_items)), expected)
        self.assertEqual(len(calls), 1)

    def test_cli_uses_one_validation_for_all_sections(self):
        self.prepare()
        calls = []
        original = Snapshots.validate_manifest

        def validate(store, snapshot_id, record, **kwargs):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record, **kwargs)

        self.project.snapshots._binding_cache.clear()
        with patch.object(Snapshots, "validate_manifest", validate):
            result = _cmd_work_items(self.config, self.project,
                                     SimpleNamespace(cursor=None, limit=100, closed=100))
        self.assertEqual(len(result["closed"]["items"]), 3)
        self.assertEqual(len(calls), 1)
        self.assertIs(type(self.project.snapshots), Snapshots)

    def test_cli_subscription_full_proof_supplies_status_pages(self):
        snapshots, store = self.project.snapshots, self.project.work_items
        store.vcs = {**self.config.vcs, "poll_enabled": True, "command": ["configured-adapter"]}
        historical = snapshots.create(
            snapshots.capture_files(self.project.workspace), goal="Goal", state="Historical",
            run="run-1", outcome="wait", summary="Historical delivery")
        snapshots.publish(historical)
        publication = Publication(self.project.root, "a" * 40, historical["code_digest"],
                                  "published", "push", projection=public_source_identity({}))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        publication.complete(intent, "b" * 40)
        item = store.create(actor="worker", title="Two deliveries and pinned wait",
                            idempotency_key="two-deliveries")["record"]["work_item"]
        bind_delivery(self.project, item["id"], historical["id"])
        store.transition(item["id"], expected_rev=3, status="blocked", actor="worker",
                         wait={"kind": "ci", "publication_id": publication.id, "checks": ["core"]})
        (self.project.workspace / "delivery.txt").write_text("Changed delivery tree")
        current = snapshots.create(
            snapshots.capture_files(self.project.workspace), goal="Goal", state="Current",
            run="run-1", outcome="wait", summary="Current delivery")
        snapshots.publish(current)
        self.assertNotEqual(historical["code_digest"], current["code_digest"])
        bound = {"commands": ["true"], "execution": "a" * 64, "environment": "b" * 64,
                 "image": "sha256:" + "c" * 64}
        receipt_id = acceptance.retain(self.project, {
            "passed": True, "snapshot_representable": True, "unchanged_during_verification": True,
            "dependencies_unchanged": True, "code_digest": current["code_digest"],
            "image": bound["image"], "run": "d" * 32,
            "commands": [{"id": "e" * 32, "exit_code": 0, "reason": "exited"}]},
            bound, producer={"kind": "verify", "id": "d" * 32, "run": "d" * 32})
        store.effective_verification = lambda _: {
            "source": "retained_acceptance", "receipt_id": receipt_id,
            "passed": True, "code_digest": current["code_digest"]}
        store.transition(item["id"], expected_rev=4, status="doing", actor="worker")
        bind_delivery(self.project, item["id"], current["id"], expected_rev=5)
        store.transition(item["id"], expected_rev=6, status="done", actor="worker",
                         reason="Verified", snapshot_id=current["id"])
        args = SimpleNamespace(cursor=None, limit=100, closed=100)
        worker = self.config.roles["worker"]
        self.config.roles["worker"] = dataclasses.replace(
            worker, work_item_events=("done",), work_item_sources="any")

        def uncached(value, **kwargs):
            view = readonly_view(value, **kwargs)
            view._peek_full_item = lambda *a, **kw: None
            return view

        def render(reuse):
            store._projection_cache.clear()
            with (patch("mizu.work_item_read_view.readonly_view",
                        readonly_view if reuse else uncached),
                  patch.object(WorkItems, "_iter_chain", autospec=True,
                               side_effect=WorkItems._iter_chain) as chains,
                  patch.object(WorkItems, "_sidecar_load", autospec=True,
                               side_effect=WorkItems._sidecar_load) as sidecars,
                  patch.object(WorkItems, "_validate_transition", autospec=True,
                               side_effect=WorkItems._validate_transition) as transitions):
                result = _cmd_work_items(self.config, self.project, args)
            return canonical(result), (chains.call_count, sidecars.call_count, transitions.call_count)

        for subscribed in (True, False):
            with self.subTest(subscribed=subscribed):
                self.config.roles["worker"] = dataclasses.replace(
                    self.config.roles["worker"], work_item_events=("done",) if subscribed else ())
                expected, before = render(False)
                actual, after = render(True)
                self.assertEqual(actual, expected)
                if subscribed:
                    self.assertEqual(after, (1, 0, 6))
                    self.assertGreater(before[2], after[2])
                else:
                    self.assertEqual(after, before)
                    if not work_items_module.IS_WINDOWS:
                        self.assertEqual(after[1], 2)

        self.config.roles["worker"] = dataclasses.replace(
            worker, work_item_events=("done",), work_item_sources="any")
        # A fresh response cannot reuse the prior response's full proof.
        paths = [self.project.root / "snapshots" / f"{historical['id']}.json",
                 self.project.root / "evidence/acceptance/objects" / f"{receipt_id}.json"]
        for path in paths:
            original = read_json(path)
            with self.subTest(corruption=path.parent.name):
                write_json(path, {**original, "code_digest": "0" * 64})
                with self.assertRaises(Denied):
                    _cmd_work_items(self.config, self.project, args)
                write_json(path, original)

    def test_full_item_peek_is_exact_and_preserves_active_ages(self):
        store = self.project.work_items
        for status in ("open", "doing", "blocked"):
            seed = store.create(actor="worker", title=status,
                                idempotency_key=status)["record"]["work_item"]
            if status != "open":
                store.transition(seed["id"], expected_rev=1, status=status, actor="worker",
                                 wait={"kind": "deadline", "at": "2099-02-01T00:00:00+00:00"}
                                 if status == "blocked" else None)
            if status == "blocked":
                # Legacy blocked rows require history-derived blocked_since.
                path = store.root / "work-items" / seed["id"] / "2.json"
                record = read_json(path)
                record.pop("blocked_since")
                write_json(path, record)
        roles = {"worker": dataclasses.replace(self.config.roles["worker"],
                                               work_item_events=("done",))}
        with (patch("mizu.work_items.now", return_value="2099-01-01T00:00:00+00:00"),
              patch("mizu.work_item_waits.now", return_value="2099-01-01T00:00:00+00:00")):
            expected = store.page_open_readonly()
            store._projection_cache.clear()
            view = readonly_view(store)
            progress(view, roles)
            self.assertEqual(canonical(view.page_open_readonly()), canonical(expected))
        event_id, event = next(store._iter_item_events(recover=False))
        _, seed = store._seed_for_event(event_id, event, readonly=True)
        revisions = store._history_revisions(seed["id"], recover=False)
        def peek(view, observed=revisions, source=event):
            return view._peek_full_item(seed["id"], seed_event=source, history_revisions=observed)

        view = readonly_view(store)
        self.assertIsNone(peek(view))
        for snapshots, delivery in ((False, False), (False, True), (True, True)):
            view._get(seed["id"], seed_event=event, history_revisions=revisions,
                      verify_snapshots=snapshots, verify_current_delivery=delivery)
            self.assertIsNone(peek(view))
        view._get(seed["id"], seed_event=event, history_revisions=revisions)
        self.assertIsNotNone(peek(view))
        self.assertIsNone(peek(view, observed=(revisions or set()) | {255}))
        self.assertIsNone(peek(view, source={**event, "changed": True}))
        copy = peek(view)
        copy["title"] = "Not authoritative"
        self.assertNotEqual(peek(view)["title"], copy["title"])

    def test_fast_closed_item_cache_does_not_satisfy_strict_history_read(self):
        proposal = self.project.insights.submit(
            source="reviewer", title="Two deliveries", body="Check proof modes", base_snapshot=None)
        seed = self.project.insights.decide(
            proposal["id"], "accept", "reviewed", "", "run", expected_rev=1)["work_item"]

        def snapshot(label):
            captured = self.project.snapshots.capture_files(self.project.workspace)
            verification = {"passed": True, "code_digest": captured["code_digest"], "run": label}
            value = self.project.snapshots.create(
                captured, goal="Goal", state=label, run=label, outcome="done",
                summary=label, verification=verification)
            self.project.snapshots.publish(value)
            return value

        historical = snapshot("historical")
        bind_delivery(self.project, seed["id"], historical["id"])
        current = snapshot("current")
        self.project.work_items.transition(seed["id"], expected_rev=3, status="doing", actor="worker",
                                           snapshot_id=current["id"])
        self.project.work_items.transition(seed["id"], expected_rev=4, status="done", actor="worker",
                                           reason="Verified", snapshot_id=current["id"])

        store = self.project.work_items
        seed_event = None
        for event_id, event in store._iter_item_events(recover=False):
            kind, candidate = store._seed_for_event(event_id, event, readonly=True)
            if kind == "seed" and candidate["id"] == seed["id"]:
                seed_event = event
                break
        self.assertIsNotNone(seed_event)
        revisions = store._history_revisions(seed["id"], recover=False)
        view = readonly_view(store)
        closed = closed_page(view, {}, limit=10)
        self.assertEqual(closed["items"][0]["id"], seed["id"])

        path = self.project.root / "snapshots" / f"{historical['id']}.json"
        old = read_json(path)
        old["summary"] = "tampered after the fast closure projection"
        write_json(path, old)
        with self.assertRaises(Denied):
            view._get(seed["id"], seed_event=seed_event,
                      history_revisions=revisions, verify_snapshots=True)

    def test_cached_fast_projection_does_not_satisfy_current_binding_check(self):
        snapshot = self.prepare()
        created = self.project.work_items.create(
            actor="worker", title="Active", idempotency_key="active")
        seed = created["record"]["work_item"]
        bind_delivery(self.project, seed["id"], snapshot["id"])

        store = self.project.work_items
        seed_event = None
        for event_id, event in store._iter_item_events(recover=False):
            kind, candidate = store._seed_for_event(event_id, event, readonly=True)
            if kind == "seed" and candidate["id"] == seed["id"]:
                seed_event = event
                break
        self.assertIsNotNone(seed_event)
        revisions = store._history_revisions(seed["id"], recover=False)
        path = self.project.root / "work-items" / seed["id"] / "3.json"
        record = read_json(path)
        record["delivery"]["code_digest"] = "0" * 64
        write_json(path, record)

        view = readonly_view(store)
        fast = view._get(seed["id"], seed_event=seed_event,
                         history_revisions=revisions, verify_snapshots=False)
        self.assertEqual(fast["delivery"]["code_digest"], "0" * 64)
        with self.assertRaisesRegex(Denied, "Delivery does not match its immutable snapshot"):
            view._get(seed["id"], seed_event=seed_event,
                      history_revisions=revisions, verify_snapshots=False,
                      verify_current_delivery=True)

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

        def validate(store, snapshot_id, record, **kwargs):
            calls.append(snapshot_id)
            return original(store, snapshot_id, record, **kwargs)

        store = self.project.work_items
        snapshots = store.snapshots
        for event_trigger, continuation in ((True, False), (False, False), (True, True)):
            with self.subTest(event_trigger=event_trigger, continuation=continuation):
                self.project.snapshots._binding_cache.clear()
                with patch.object(Snapshots, "validate_manifest", validate):
                    with patch("mizu.runtime.readonly_view", side_effect=lambda value, **kwargs: value):
                        expected = self.dispatch(event_trigger=event_trigger, continuation=continuation)
                    uncached = len(calls)
                    calls.clear()
                    actual = self.dispatch(event_trigger=event_trigger, continuation=continuation)
                    self.assertEqual(actual, expected)
                    self.assertEqual(len(actual["terminal_events"]), 3)
                    expected_validations = 5 if work_items_module.IS_WINDOWS else 1
                    self.assertEqual(len(calls), expected_validations)
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
        with patch("mizu.runtime.readonly_view", side_effect=lambda value, **kwargs: value):
            self.assertEqual(self.dispatch(), current)

    def test_next_dispatch_revalidates_historical_manifest(self):
        snapshot = self.archive_delivery_snapshot()
        self.assertEqual(len(self.dispatch()["terminal_events"]), 3)
        snapshot["summary"] = "tampered"
        write_json(self.project.root / "snapshots" / f"{snapshot['id']}.json", snapshot)
        with self.assertRaises(Denied):
            self.dispatch()
        with patch("mizu.runtime.readonly_view", side_effect=lambda value, **kwargs: value):
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

    def test_cli_and_dispatch_forward_configured_limits(self):
        self.prepare()
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_item_read_view_snapshot_cache_bytes=137,
            work_item_read_view_file_cache_bytes=139,
            work_item_read_view_item_cache_bytes=149,
            work_item_read_view_item_cache_entries=2))
        with patch("mizu.work_item_read_view.readonly_view", wraps=readonly_view) as view:
            _cmd_work_items(self.config, self.project,
                            SimpleNamespace(cursor=None, limit=100, closed=100))
        view.assert_called_once_with(self.project.work_items, limits=self.config.limits)
        with patch("mizu.runtime.readonly_view", wraps=readonly_view) as view:
            self.dispatch()
        view.assert_called_once_with(self.project.work_items, limits=self.config.limits)

    def test_small_budgets_repeat_validation_without_changing_projection(self):
        self.prepare()
        expected = self.render(readonly_view(self.project.work_items))
        limits = dataclasses.replace(
            Limits(), work_item_read_view_snapshot_cache_bytes=1,
            work_item_read_view_file_cache_bytes=1,
            work_item_read_view_item_cache_bytes=1,
            work_item_read_view_item_cache_entries=1)
        # The per-process binding memo would hide the repeats; disable it.
        self.project.snapshots._binding_cache.clear()
        with patch("mizu.snapshot._PATH_VALIDATION_CACHE_LIMIT", 0), \
                patch.object(Snapshots, "validate_manifest", autospec=True,
                          side_effect=Snapshots.validate_manifest) as validate:
            self.assertEqual(self.render(readonly_view(self.project.work_items, limits=limits)),
                             expected)
        self.assertGreater(validate.call_count, 1)

    def test_each_byte_budget_is_used_exactly_and_misses_are_not_cached(self):
        value = {"key": "a"}
        for kind in ("snapshot", "file", "item"):
            size = len(canonical(value)) + (len(canonical({})) if kind == "item" else 0)
            for budget, expected_reads in ((size - 1, 4), (size, 3), (2 * size, 2)):
                with self.subTest(kind=kind, budget=budget):
                    reader = Mock(return_value=value)
                    store = SimpleNamespace(
                        snapshots=SimpleNamespace(get=reader, _path_key=lambda key: key),
                        _read=reader, _get=reader)
                    limits = dataclasses.replace(
                        Limits(), **{f"work_item_read_view_{kind}_cache_bytes": budget})
                    view = readonly_view(store, limits=limits)
                    read = {"snapshot": view.snapshots.get, "file": view._read,
                            "item": lambda key: view._get(
                                key, seed_event={}, history_revisions=None)}[kind]
                    for key in ("a", "a", "b", "a"):
                        self.assertEqual(read(key), value)
                    self.assertEqual(reader.call_count, expected_reads)
                    # Exceptions on a cache miss must be retried, not retained.
                    reader.side_effect = Denied("invalid record")
                    for _ in range(2):
                        with self.assertRaisesRegex(Denied, "invalid record"):
                            read("bad")
                    self.assertEqual(reader.call_count, expected_reads + 2)

    def test_parsed_item_entry_budget_and_default_fallback(self):
        for entries, expected_reads in ((None, 2), (1, 3), (2, 2)):
            with self.subTest(entries=entries):
                reader = Mock(return_value={"key": "a"})
                store = SimpleNamespace(
                    snapshots=SimpleNamespace(get=reader, _path_key=lambda key: key),
                    _read=reader, _get=reader)
                kwargs = {} if entries is None else {"limits": dataclasses.replace(
                    Limits(), work_item_read_view_item_cache_entries=entries)}
                view = readonly_view(store, **kwargs)
                self.assertEqual(view._read.budget, Limits().work_item_read_view_file_cache_bytes)
                for key in ("a", "a", "b", "a"):
                    item = view._get(key, seed_event={}, history_revisions=None)
                    self.assertEqual(item, {"key": "a"})
                    item["key"] = "changed"
                self.assertEqual(reader.call_count, expected_reads)
                fresh = readonly_view(store, **kwargs)
                self.assertEqual(fresh._get("a", seed_event={}, history_revisions=None),
                                 {"key": "a"})
                self.assertEqual(reader.call_count, expected_reads + 1)

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
