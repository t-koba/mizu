"""Explicit delivery revisions and late immutable acceptance proof."""
import copy
import json
import threading
from unittest.mock import patch

from support import Fixture
from mizu import acceptance
from mizu.errors import Denied
from mizu.fs import canonical, digest, lock, write_json


class RetainedDeliveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.item = self.store.create(actor="worker", idempotency_key="delivery", title="Deliver",
                                      scope="Accepted scope")["record"]["work_item"]
        self.step("doing")
        self.serial = 0

    def step(self, status, **kwargs):
        self.item = self.store.transition(self.item["id"], expected_rev=self.item["rev"],
                                          status=status, actor="worker", **kwargs)
        return self.item

    def snapshot(self, embedded=True, publish=True):
        self.serial += 1
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run="run-1", outcome="continue",
            summary=f"Delivery {self.serial}", verification=(
                {"passed": True, "code_digest": captured["code_digest"]} if embedded else None))
        if publish:
            self.project.snapshots.publish(snapshot)
        return snapshot

    def bind(self, snapshot):
        bound = self.step("doing", snapshot_id=snapshot["id"])
        assert bound["snapshot_id"] is None
        assert bound["delivery"]["snapshot_id"] == snapshot["id"]
        return bound

    def receipt(self, snapshot):
        bound = {"commands": ["check"], "execution": "1" * 64, "environment": "2" * 64,
                 "image": "sha256:" + "3" * 64}
        value = {"schema": 1, "code_digest": snapshot["code_digest"], "scope": bound,
                 "producer": {"kind": "verify", "id": "4" * 32, "run": "4" * 32},
                 "passed": True, "commands": [{"id": "5" * 32, "exit_code": 0, "reason": "exited"}]}
        rid = acceptance._retain(self.project.root, value)
        return {"source": "retained_acceptance", "receipt_id": rid, "passed": True,
                "code_digest": snapshot["code_digest"]}, bound

    def test_done_requires_an_earlier_explicit_binding(self):
        snapshot = self.snapshot()
        with self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])
        self.bind(snapshot)
        completed = self.step("done", snapshot_id=snapshot["id"])
        self.assertEqual(completed["delivered_tree"], snapshot["code_digest"])
        self.assertEqual(completed["delivery"]["binding_rev"], 3)

    def test_binding_requires_retained_snapshot_and_same_doing_scope(self):
        unpublished = self.snapshot(publish=False)
        with self.assertRaises(Denied):
            self.bind(unpublished)
        snapshot = self.snapshot()
        for status in ("open", "blocked", "done", "dropped"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.step(status, snapshot_id=snapshot["id"])
        with self.assertRaises(Denied):
            self.step("doing", snapshot_id=snapshot["id"], scope="Different", reason="Changed")
        self.bind(snapshot)
        newer = self.snapshot()
        with self.assertRaises(Denied):
            self.step("done", snapshot_id=newer["id"])
        self.step("doing", scope="Different", reason="New accepted scope")
        with self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])
        self.bind(newer)
        self.step("done", snapshot_id=newer["id"])

    def test_bound_published_tree_closes_after_workspace_changes_and_eviction(self):
        snapshot = self.snapshot()
        self.bind(snapshot)
        (self.project.workspace / "later.txt").write_text("Later independent work")
        self.project.snapshots.history_index = 1
        self.snapshot()
        self.assertNotIn(snapshot["id"], self.project.snapshots._history_ids())
        self.step("done", snapshot_id=snapshot["id"])

    def test_late_receipt_projection_is_pinned_not_reselected_on_replay(self):
        snapshot = self.snapshot(embedded=False)
        original = canonical(self.project.snapshots.get(snapshot["id"]))
        self.bind(snapshot)
        reference, bound = self.receipt(snapshot)
        with patch("mizu.acceptance._current_scope", return_value=bound):
            completed = self.step("done", snapshot_id=snapshot["id"])
        self.assertEqual(completed["verification"], reference)
        with patch.object(self.store, "effective_verification", side_effect=AssertionError("reselection")):
            self.assertEqual(self.store.get(self.item["id"]), completed)
        self.assertEqual(canonical(self.project.snapshots.get(snapshot["id"])), original)
        path = self.project.root / "evidence/acceptance/objects" / (reference["receipt_id"] + ".json")
        value = json.loads(path.read_text())
        value["scope"]["commands"] = ["other"]
        write_json(path, value)
        with self.assertRaises(Denied):
            self.store.get(self.item["id"])

    def test_wrong_code_or_scope_projection_refuses(self):
        snapshot = self.snapshot(embedded=False)
        self.bind(snapshot)
        reference, bound = self.receipt(snapshot)
        wrong = {**bound, "commands": ["different"]}
        with patch("mizu.acceptance._current_scope", return_value=wrong), self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])
        with patch.object(self.store, "effective_verification", return_value={**reference, "code_digest": "a" * 64}):
            with self.assertRaises(Denied):
                self.step("done", snapshot_id=snapshot["id"])

    def test_dropped_correction_requires_prior_binding_and_retained_receipt(self):
        snapshot = self.snapshot()
        self.bind(snapshot)
        dropped = self.step("dropped", reason="Mistaken disposition")
        path = self.project.root / "work-items" / self.item["id"] / f'{dropped["rev"]}.json'
        original = path.read_bytes()
        for status in ("doing", "open", "blocked", "dropped"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.step(status)
        with self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])
        reference, bound = self.receipt(snapshot)
        with patch("mizu.acceptance._current_scope", return_value=bound):
            self.step("done", snapshot_id=snapshot["id"])
        self.assertEqual(self.item["verification"], reference)
        self.assertEqual(path.read_bytes(), original)
        for status in ("doing", "open", "blocked", "dropped", "done"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.step(status, snapshot_id=snapshot["id"] if status == "done" else None)

    def test_unbound_dropped_cannot_acquire_delivery_or_close(self):
        snapshot = self.snapshot()
        self.step("dropped", reason="No delivery")
        reference, bound = self.receipt(snapshot)
        with self.assertRaises(Denied):
            self.bind(snapshot)
        with patch("mizu.acceptance._current_scope", return_value=bound), self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])

    def test_marked_chain_cannot_lose_delivery_or_schema(self):
        snapshot = self.snapshot()
        binding = self.bind(snapshot)
        completed = self.step("done", snapshot_id=snapshot["id"])
        base = self.project.root / "work-items" / self.item["id"]
        for key in ("delivery", "delivery_schema"):
            changed = copy.deepcopy(completed)
            changed.pop(key)
            write_json(base / f'{completed["rev"]}.json', changed)
            with self.subTest(key=key), self.assertRaises(Denied):
                self.store.get(self.item["id"])
        write_json(base / f'{completed["rev"]}.json', completed)
        changed = copy.deepcopy(binding)
        changed.pop("delivery")
        changed.pop("delivery_schema")
        changed.pop("verification_scope")
        write_json(base / f'{binding["rev"]}.json', changed)
        with self.assertRaises(Denied):
            self.store.get(self.item["id"])

    def test_binding_holds_publication_lock_until_append_and_eviction_wins_refuses(self):
        snapshot = self.snapshot()
        self.project.snapshots.history_index = 1
        newer = self.snapshot(publish=False)
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        original = self.store._validate_transition
        errors = []
        def validate(previous, record):
            if (record.get("delivery") or {}).get("binding_rev") == record["rev"]:
                entered.set()
                if not release.wait(60):
                    raise AssertionError("test synchronization timed out")
            return original(previous, record)
        def binding():
            try:
                self.bind(snapshot)
            except Exception as exc:
                errors.append(exc)
        def publish():
            try:
                self.project.snapshots.publish(newer)
            except Exception as exc:
                errors.append(exc)
            finally:
                published.set()
        with patch.object(self.store, "_validate_transition", side_effect=validate):
            first = threading.Thread(target=binding)
            first.start()
            self.assertTrue(entered.wait(60))
            second = threading.Thread(target=publish)
            second.start()
            try:
                self.assertFalse(published.wait(.1))
            finally:
                release.set()
                first.join(60)
                second.join(60)
        self.assertFalse(errors)
        self.assertFalse(first.is_alive() or second.is_alive())
        with self.assertRaises(Denied):
            self.bind(snapshot)

    def test_binding_lock_order_and_publisher_never_takes_insights_lock(self):
        snapshot = self.snapshot()
        paths = []
        def track(path, **kwargs):
            paths.append(path.name)
            return lock(path, **kwargs)
        with patch("mizu.work_items.lock", side_effect=track), \
                patch("mizu.smoke_refs.lock", side_effect=track):
            self.bind(snapshot)
        self.assertEqual(paths[:3], ["publication.lock", "smoke-retention.lock", "insights.lock"])
        paths.clear()
        newer = self.snapshot(publish=False)
        with patch("mizu.snapshot.lock", side_effect=track):
            self.project.snapshots.publish(newer)
        self.assertEqual(paths, ["publication.lock"])

    def test_all_delivery_manifests_survive_rebinding_drop_eviction_and_reclaim(self):
        from mizu.storage import audit, reclaim
        first = self.snapshot()
        self.bind(first)
        second = self.snapshot()
        self.bind(second)
        self.step("dropped", reason="Retain earlier delivery facts")
        self.project.snapshots.history_index = 1
        self.snapshot()
        report = audit(self.project)
        self.assertEqual(report["work_item_events_skipped"], 0)
        self.assertEqual(report["references"]["work_item_snapshots"], 2)
        self.project.set_control(paused=True)
        reclaim(self.project, apply=True)
        for snapshot in (first, second):
            self.assertEqual(self.project.snapshots.get(snapshot["id"]), snapshot)
        self.assertEqual(self.store.get(self.item["id"]), self.item)

    def test_retained_reference_code_scope_and_identity_tampering_refuse(self):
        snapshot = self.snapshot(embedded=False)
        self.bind(snapshot)
        reference, bound = self.receipt(snapshot)
        with patch("mizu.acceptance._current_scope", return_value=bound):
            completed = self.step("done", snapshot_id=snapshot["id"])
        path = self.project.root / "work-items" / self.item["id"] / f'{completed["rev"]}.json'
        for field, value in (("code_digest", "a" * 64), ("receipt_id", "b" * 64),
                             ("passed", False), ("source", "other")):
            changed = copy.deepcopy(completed)
            changed["verification"][field] = value
            write_json(path, changed)
            with self.subTest(field=field), self.assertRaises((Denied, OSError)):
                self.store.get(self.item["id"])
        changed = copy.deepcopy(completed)
        changed["verification_scope"] = digest(canonical({**bound, "commands": ["other"]}))
        write_json(path, changed)
        with self.assertRaises(Denied):
            self.store.get(self.item["id"])
        write_json(path, completed)
        receipt_path = self.project.root / "evidence/acceptance/objects" / (reference["receipt_id"] + ".json")
        receipt_path.write_bytes(b"{not json")
        with self.assertRaises(Denied):
            self.store.get(self.item["id"])

    def test_unscoped_delivery_and_legacy_initial_scope_closure_remain_readable(self):
        self.step("open")  # Release the unrelated setup hold before claiming this slice.
        seed = self.store.create(actor="worker", idempotency_key="unscoped", title="No scope")["record"]["work_item"]
        self.item = seed
        self.step("doing")
        snapshot = self.snapshot()
        self.bind(snapshot)
        self.assertIsNone(self.item["delivery"]["scope_digest"])
        self.step("done", snapshot_id=snapshot["id"])
        self.assertEqual(self.store.get(seed["id"]), self.item)
        # Pre-schema closures could first state a scope at completion. Preserve
        # that exact historical interpretation, without allowing new writes.
        legacy = copy.deepcopy(self.item)
        for field in ("delivery_schema", "delivery", "verification_scope"):
            legacy.pop(field)
        legacy.update(rev=2, scope="Historical initial scope", reason="Original scope",
                      transition={"from": "open", "to": "done"})
        base = self.project.root / "work-items" / seed["id"]
        (base / "3.json").unlink()
        (base / "4.json").unlink()
        write_json(base / "2.json", legacy)
        self.assertEqual(self.store.get(seed["id"]), legacy)

    def test_binding_and_completion_revalidate_exact_manifest(self):
        snapshot = self.snapshot()
        path = self.project.snapshots.root / "snapshots" / (snapshot["id"] + ".json")
        write_json(path, {**snapshot, "summary": "Tampered"})
        with self.assertRaises(Denied):
            self.bind(snapshot)
        write_json(path, snapshot)
        self.bind(snapshot)
        write_json(path, {**snapshot, "code_digest": "a" * 64})
        with self.assertRaises(Denied):
            self.step("done", snapshot_id=snapshot["id"])
