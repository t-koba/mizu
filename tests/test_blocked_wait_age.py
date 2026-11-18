"""Blocked age is diagnostic, never replanning or remote failure evidence."""
import datetime as dt
import json
from contextlib import contextmanager
from unittest.mock import patch

from support import Fixture, stored_question_insight
from mizu.errors import Denied
from mizu.fs import read_json
from support import history_write_json as write_json
from mizu.runtime import prompt_for, prompt_delta_for
from mizu.work_items import WorkItems

START = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
CODE = {"kind": "code_change"}
FUTURE = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}


def _external_wait(project):
    question = stored_question_insight(project)
    return {"kind": "insight_decided", "insight": question["id"]}


@contextmanager
def clock(seconds):
    stamp = (START + dt.timedelta(seconds=seconds)).isoformat()
    with patch("mizu.work_items.now", return_value=stamp), patch("mizu.work_item_waits.now", return_value=stamp):
        yield stamp


class BlockedWaitAgeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def block(self, key="item", wait=CODE):
        seed = self.store.create(actor="worker", idempotency_key=key, title=key)["record"]["work_item"]
        return self.change(seed, "blocked", wait=wait)

    def change(self, item, status, **kwargs):
        return self.store.transition(item["id"], expected_rev=item["rev"], actor="worker", status=status, **kwargs)

    def test_age_survives_doing_delivery_rebinding_and_resets_only_on_open(self):
        with clock(0) as start:
            item = self.block()
        with clock(3):
            item = self.change(item, "doing")
            for _ in range(2):
                # Rebinding a retained publication must not buy a new wait episode.
                item = self.change(item, "doing", snapshot_id=self.project.snapshots.get()["id"])
        with clock(7):
            item = self.change(item, "blocked", wait=CODE)
            self.assertEqual(item["blocked_since"], start)
            # Holder-only work reads as ordinary held-open work, including
            # in the unscoped ready read.
            self.assertEqual(self.store.page_ready()["total"], 1)
        with clock(10**9):
            live = self.store.blocked_liveness_readonly()["items"][0]
            self.assertEqual((live["state"], live["reason"]), ("pending", "condition_pending"))
            self.assertEqual(live["blocked_age_seconds"], 10**9)
            # An ancient wait stays pending, but a holder-only code_change
            # wait has no external resolver, so the item reads as ordinary
            # held-open ready work for its holder.
            ready = self.store.page_ready(role_name="worker")
            self.assertEqual(ready["total"], 1)
            self.assertEqual(ready["items"][0]["status"], "open")
            for absent in ("wait", "wait_liveness", "blocked_since", "blocked_age_seconds"):
                self.assertNotIn(absent, ready["items"][0])
            self.assertEqual(self.store.page_ready(role_name="reviewer")["total"], 0)
            self.assertEqual(self.store.get(item["id"]), item)
            item = self.change(item, "doing")
            item = self.change(item, "blocked", wait=CODE)
            self.assertEqual(item["blocked_since"], start)
            item = self.change(item, "open")  # Explicit holder action, not age authority.
            self.assertIsNone(item["blocked_since"])
        with clock(10**9 + 1) as reopened:
            item = self.change(item, "blocked", wait=CODE)
            self.assertEqual(item["blocked_since"], reopened)
            self.assertEqual(self.store.page_ready()["total"], 1)

    def test_exact_code_change_releases_even_an_ancient_wait(self):
        baseline = self.project.snapshots.get()["code_digest"]
        with clock(0):
            item = self.block()
        self.assertEqual(item["wait"]["digest"], baseline)
        with clock(10**9), patch.object(self.store.snapshots, "get", return_value={"code_digest": "f" * 64}):
            live = self.store.page_ready(role_name="worker")["items"][0]["wait_liveness"]
            self.assertEqual(live["state"], "due")
            doing = self.change(item, "doing")
            self.assertEqual(doing["status"], "doing")

    def test_old_doing_episode_can_register_pending_wait_without_forced_open(self):
        with clock(0) as start:
            item = self.block()
            baseline = item["wait"]["digest"]
            item = self.change(item, "doing")
        with clock(10**9), patch.object(self.store.snapshots, "get", return_value={"code_digest": baseline}):
            item = self.change(item, "blocked", wait=_external_wait(self.project))
            self.assertEqual(item["blocked_since"], start)
            self.assertEqual(self.store.page_ready()["total"], 0)

    def test_blocked_decision_seed_ages_without_writing_a_transition(self):
        with clock(0), patch("mizu.insights.now", return_value=START.isoformat()):
            proposal = self.project.insights.submit(source="worker", title="Contingent", body="Review", base_snapshot=None)
            event = self.project.insights.decide(proposal["id"], "accept", "Deliver scope", "", "run", expected_rev=1,
                work_items=[{"title": "Contingent task", "description": "Wait for changed code", "wait": CODE}])
            seed = event["work_items"][0]
        with clock(10**9):
            page = self.store.page_ready(role_name="worker")
            self.assertEqual(page["total"], 0)
            self.assertEqual(self.store.blocked_liveness_readonly()["items"][0]["state"], "pending")
            self.assertIsNone(self.store.get(seed["id"])["holder"])
            self.assertEqual(self.store.get(seed["id"]), seed)
            self.assertEqual(self.store.page_open_readonly()["items"][0]["blocked_since"], seed["created_at"])

    def test_explicit_deadline_is_independent_and_clock_rollback_clamps_age(self):
        with clock(0):
            item = self.block(wait=FUTURE)
        with clock(-1):
            row = self.store.page_open_readonly()["items"][0]
            self.assertEqual(row["blocked_age_seconds"], 0)
        with clock(10**9):
            # The holder's own future deadline is deferred work, so it reads as
            # ready here rather than as blocked backpressure.
            self.assertEqual(self.store.page_ready()["total"], 1)
            state = self.store.blocked_liveness_readonly()["items"][0]
            self.assertEqual((state["state"], state["blocked_age_seconds"]), ("pending", 10**9))
            item = self.change(item, "doing")
            item = self.change(item, "blocked", wait=FUTURE)
        with clock(10**9):
            self.block("due", {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
            live = self.store.page_ready()["items"][0]["wait_liveness"]
            self.assertEqual((live["state"], live["reason"]), ("due", "condition_met"))

    def test_ancient_pending_status_full_delta_prompts_and_readonly_pages_do_not_write(self):
        with clock(0):
            item = self.block()
        self.project.status()  # Initialize unrelated check-job status lock before observing writes.
        before = {p.relative_to(self.project.root): p.read_bytes()
                  for p in self.project.root.rglob("*") if p.is_file()}
        with clock(10**9), patch("mizu.work_items.lock", side_effect=AssertionError("read-only lock")):
            row = self.store.page_open_readonly()["items"][0]
            self.assertEqual(row["blocked_age_seconds"], 10**9)
            diagnostic = self.project.status()["blocked_work"]
            self.assertEqual(diagnostic["counts"]["pending"], 0)
            self.assertEqual(diagnostic["items"], [])
        after = {p.relative_to(self.project.root): p.read_bytes()
                 for p in self.project.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        with clock(10**9):
            context = self.context()
            full = json.loads(prompt_for(context))
            delta = json.loads(prompt_delta_for(context, []))
            for prompt in (full, delta):
                self.assertEqual(prompt["ready_work"]["total"], 1)
                self.assertEqual(prompt["ready_work"]["items"][0]["status"], "open")
                self.assertNotIn("wait_liveness", prompt["ready_work"]["items"][0])
                self.assertNotIn("wait_satisfied", prompt["ready_work"]["items"][0])
            self.assertEqual(self.store.get(item["id"]), item)

    def test_legacy_history_derives_first_block_and_rejects_reset_claim(self):
        with clock(0) as start:
            item = self.block()
        with clock(1):
            item = self.change(item, "doing")
        # Emulate an entirely pre-field history, leaving the old sidecar stale.
        directory = self.project.root / "work-items" / item["id"]
        for rev in (2, 3):
            path = directory / f"{rev}.json"
            record = read_json(path)
            record.pop("blocked_since")
            write_json(path, record)
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.insights)
        self.store = fresh
        self.assertNotIn("blocked_since", fresh.get(item["id"]))
        self.assertEqual(fresh.projection(item["id"])["blocked_since"], start)
        with clock(2):
            item = self.change(item, "blocked", wait=CODE)
            self.assertEqual(item["blocked_since"], start)
        path = directory / f"{item['rev']}.json"
        write_json(path, {**item, "blocked_since": item["updated_at"]})
        with self.assertRaises(Denied):
            fresh.get(item["id"])
        self.assertFalse(fresh.blocked_liveness_readonly()["coverage_complete"])

    def test_impossible_requires_reasoned_open_regardless_of_age(self):
        with clock(0):
            item = self.block(wait={"kind": "insight_decided", "insight": "f" * 32})
        with clock(10**9):
            state = self.store.page_ready()["items"][0]["wait_liveness"]
            self.assertEqual((state["state"], state["reason"]), ("impossible", "target_missing"))
            with self.assertRaisesRegex(Denied, "Reopen"):
                self.change(item, "blocked", wait=CODE, scope="New wait", reason="Rebind")
            with self.assertRaisesRegex(Denied, "reason"):
                self.change(item, "open")
            item = self.change(item, "open", reason=state["reason"])
            item = self.change(item, "blocked", wait=CODE)

    def test_ancient_unknown_wait_stays_blocked_without_forcing_reopen(self):
        with clock(0) as start:
            item = self.block()
        with clock(10**9), patch.object(self.store.snapshots, "get", return_value={}):
            live = self.store.blocked_liveness_readonly()["items"][0]
            self.assertEqual(live["state"], "unknown")
            self.assertEqual(live["blocked_age_seconds"], 10**9)
            page = self.store.page_ready(role_name="worker")
            self.assertEqual((page["total"], page["unknown_blocked"]), (0, 1))
            self.assertEqual(self.store.get(item["id"]), item)
            item = self.change(item, "doing")
            item = self.change(item, "blocked", wait=FUTURE)
            self.assertEqual(item["blocked_since"], start)
