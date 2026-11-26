"""Ready work is what a role can act on now: free doing slots and live waits."""
from support import Fixture

from mizu.errors import Denied
from mizu.unit_prompts import _ready_work_projection
from mizu.work_readiness import _has_ready_work


class ReadyWorkCapacityWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.store.doing_limit = 1
        self.store.doing_limits["worker"] = 1

    def item(self, key):
        return self.store.create(idempotency_key=key, title=key,
                                 actor="worker")["record"]["work_item"]["id"]

    def change(self, item_id, status, **kwargs):
        return self.store.transition(item_id, expected_rev=self.store.get(item_id)["rev"],
                                     status=status, actor=kwargs.pop("actor", "worker"), **kwargs)

    def publish_code(self, text, run):
        (self.project.workspace / "app.py").write_bytes(text)
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal="Goal", state="State", run=run, outcome="done",
            summary="Delivery", verification={"passed": True,
                                              "code_digest": captured["code_digest"],
                                              "run": run})
        self.project.snapshots.publish(snapshot)
        return snapshot

    def test_open_work_is_suppressed_at_capacity_until_the_holder_releases(self):
        held, waiting_open = self.item("held"), self.item("waiting-open")
        self.change(held, "doing")
        page = self.store.page_ready(role_name="worker")
        self.assertEqual([row["id"] for row in page["items"]], [held])
        self.assertEqual(page["total"], 1)  # The held item, not the unclaimable open one.
        self.assertEqual((page["capacity_in_use"], page["capacity_limit"],
                          page["held_pending_waits"]), (1, 1, 0))
        self.assertEqual(page["items"][0]["status"], "doing")
        self.assertEqual(self.store.page_ready()["total"], 2)
        self.assertEqual([row["id"] for row in self.store.page_ready()["items"]],
                         [held, waiting_open])
        self.assertEqual([row["id"] for row in
                          self.store.page_ready(role_name="reviewer")["items"]],
                         [waiting_open])
        signature = page["signature"]
        self.change(held, "dropped", reason="No longer needed")
        released = self.store.page_ready(role_name="worker")
        self.assertEqual([row["id"] for row in released["items"]], [waiting_open])
        self.assertEqual(released["total"], 1)
        self.assertEqual(released["capacity_in_use"], 0)
        self.assertNotEqual(released["signature"], signature)
        self.assertTrue(_has_ready_work(self.project, self.config.roles["worker"]))

        self.store.doing_limits["worker"] = 2
        widened = self.store.page_ready(role_name="worker")
        self.assertEqual(widened["capacity_limit"], 2)
        self.assertEqual(widened["signature"], released["signature"])  # Facts are not readiness.

    def test_doing_delivery_wait_becomes_ready_only_after_a_new_snapshot(self):
        item_id = self.item("bound-waiter")
        self.change(item_id, "doing")
        baseline = self.project.snapshots.get()
        waiting = self.change(item_id, "doing", snapshot_id=baseline["id"],
                              wait={"kind": "code_change"})
        self.assertEqual(waiting["status"], "doing")
        self.assertEqual(waiting["delivery"]["snapshot_id"], baseline["id"])
        self.assertEqual(waiting["wait"]["digest"], baseline["code_digest"])
        self.assertIsNone(waiting.get("blocked_since"))

        pending = self.store.page_ready(role_name="worker")
        self.assertEqual(pending["total"], 0)
        self.assertEqual(pending["held_pending_waits"], 1)
        self.assertEqual((pending["capacity_in_use"], pending["capacity_limit"]), (1, 1))

        changed = self.publish_code(b"VALUE = 3\n", "new-code")
        self.assertNotEqual(changed["code_digest"], baseline["code_digest"])
        due = self.store.page_ready(role_name="worker")
        self.assertEqual([row["id"] for row in due["items"]], [item_id])
        self.assertEqual(due["total"], 1)
        self.assertEqual(due["held_pending_waits"], 0)
        row = due["items"][0]
        self.assertEqual(row["wait"], waiting["wait"])
        self.assertEqual(row["wait_liveness"]["state"], "due")
        self.assertNotEqual(due["signature"], pending["signature"])
        recorded = self.store.get(item_id)
        self.assertEqual(recorded["status"], "doing")
        self.assertEqual(recorded["delivery"], waiting["delivery"])

    def test_doing_waits_register_without_leaving_doing_and_blocked_still_needs_one(self):
        self.store.doing_limits["worker"] = 2
        deadline = self.item("deadline-waiter")
        self.change(deadline, "doing")
        recorded = self.change(deadline, "doing",
                               wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        self.assertEqual((recorded["status"], recorded["wait"]["kind"]), ("doing", "deadline"))
        self.assertIsNone(recorded.get("blocked_since"))

        code = self.item("code-waiter")
        self.change(code, "doing")
        recorded = self.change(code, "doing", wait={"kind": "code_change"})
        self.assertEqual((recorded["status"], recorded["wait"]["kind"]), ("doing", "code_change"))

        blocked = self.item("blocked-needs-wait")
        with self.assertRaisesRegex(Denied, "New blocked transitions require a supported observable wait"):
            self.change(blocked, "blocked")
        self.assertEqual(self.store.get(blocked)["status"], "open")

        pending = self.store.page_ready(role_name="worker")
        self.assertEqual(pending["total"], 0)
        self.assertEqual(pending["held_pending_waits"], 2)
        self.assertFalse(_has_ready_work(self.project, self.config.roles["worker"]))

    def test_ready_projection_passes_through_validated_capacity_facts(self):
        held = self.item("projection-held")
        self.change(held, "doing")
        page = self.store.page_ready(role_name="worker")
        ready = _ready_work_projection(page)
        for key in ("capacity_in_use", "capacity_limit", "held_pending_waits"):
            self.assertEqual(ready[key], page[key])
        self.assertIs(type(ready["capacity_in_use"]), int)
        self.assertIs(type(ready["capacity_limit"]), int)
        self.assertIs(type(ready["held_pending_waits"]), int)
        with self.assertRaisesRegex(Denied, "Invalid ready-work capacity_in_use"):
            _ready_work_projection({**page, "capacity_in_use": "1"})
        with self.assertRaisesRegex(Denied, "Invalid ready-work capacity_limit"):
            _ready_work_projection({**page, "capacity_limit": 0})
