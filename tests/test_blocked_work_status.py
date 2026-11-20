"""Blocked-work diagnostics are bounded observations, never ledger mutations."""
import json

from support import Fixture
from mizu.errors import Denied
from mizu.fs import read_json
from support import history_write_json as write_json
from mizu.runtime import prompt_for

FUTURE = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}
PAST = {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"}


class BlockedWorkStatusTests(Fixture):
    def block(self, key, wait=FUTURE, actor="worker"):
        store = self.project.work_items
        item = store.create(idempotency_key=key, title=key, actor=actor)["record"]["work_item"]
        return store.transition(item["id"], expected_rev=1, status="blocked", actor=actor, wait=wait)


    def view(self):
        return self.project.work_items.blocked_liveness_readonly()

    def test_legacy_absence_is_actionable_but_new_writes_require_wait(self):
        store = self.project.work_items
        blocked = self.block("legacy")
        blocked["wait"] = None
        path = self.project.root / "work-items" / blocked["id"] / "2.json"
        write_json(path, blocked)  # Historical record, not a new transition.
        before = path.read_bytes()
        row = self.view()["items"][0]
        self.assertEqual((row["state"], row["reason"]), ("absent", "no_wait"))
        self.assertEqual(store.page_ready(role_name="worker")["total"], 1)
        self.assertEqual(store.page_ready(role_name="reviewer")["total"], 0)
        ready = json.loads(prompt_for(self.context()))["ready_work"]["items"][0]
        self.assertFalse(ready["wait_satisfied"])
        self.assertEqual(ready["wait_liveness"]["state"], "absent")
        with self.assertRaisesRegex(Denied, "observable wait"):
            store.transition(blocked["id"], expected_rev=2, status="blocked", actor="worker",
                             scope="Clarification", reason="Keep legacy block")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.get(blocked["id"]), blocked)
        store.transition(blocked["id"], expected_rev=2, status="open", actor="worker")
        self.assertEqual(store.get(blocked["id"])["rev"], 3)  # Chain replay survives.


    def test_incomplete_ledger_invalidates_whole_projection_without_repair(self):
        blocked = self.block("broken-chain")
        path = self.project.root / "work-items" / blocked["id"] / "2.json"
        record = read_json(path)
        record["rev"] = 4
        write_json(path, record)
        view = self.view()
        self.assertEqual(view["diagnostic"], "unreadable")
        self.assertIsNone(view["total"])
        self.assertIsNone(view["truncated"])
        self.assertFalse(view["coverage_complete"])
        self.assertEqual(read_json(path), record)
