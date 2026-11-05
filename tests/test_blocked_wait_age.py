"""Finite blocked episodes: local age is replanning, never remote failure."""
import datetime as dt
import json
from contextlib import contextmanager
from unittest.mock import patch

from support import Fixture
from mizu.config import load, TOML_INTEGER_MAX
from mizu.errors import ConfigError, Denied
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.runtime import prompt_for, prompt_delta_for
from mizu.work_item_read_view import readonly_view
from mizu.work_items import WorkItems

START = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
CODE = {"kind": "code_change"}
FUTURE = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}


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

    def test_config_default_positive_integer_and_project_plumbing(self):
        self.assertEqual(self.config.limits.work_items_blocked_max_age_seconds, 604800)
        self.assertEqual(self.store.blocked_max_age_seconds, 604800)
        base = self.file.read_text()
        base = "".join(line for line in base.splitlines(keepends=True)
                        if not line.startswith("work_items_blocked_max_age_seconds ="))
        for value in (1, 604800, TOML_INTEGER_MAX):
            self.file.write_text(base.replace("[limits]\n", f"[limits]\nwork_items_blocked_max_age_seconds = {value}\n"))
            config = load(self.file)
            self.assertEqual(Project(config, "sample").work_items.blocked_max_age_seconds, value)
        for value in (0, -1, "true", "1.5", '\"10\"', TOML_INTEGER_MAX + 1):
            self.file.write_text(base.replace("[limits]\n", f"[limits]\nwork_items_blocked_max_age_seconds = {value}\n"))
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load(self.file)

    def test_age_survives_doing_delivery_rebinding_and_resets_only_on_open(self):
        self.store.blocked_max_age_seconds = 10
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
            self.assertEqual(self.store.page_ready()["total"], 0)
        with clock(10):
            row = self.store.page_ready(role_name="worker")["items"][0]
            live = row["wait_liveness"]
            self.assertEqual((live["state"], live["reason"]), ("expired", "blocked_max_age_reached"))
            self.assertEqual((live["blocked_age_seconds"], live["blocked_max_age_seconds"]), (10, 10))
            self.assertEqual(self.store.page_ready(role_name="reviewer")["total"], 0)
            for status, kwargs in (("blocked", {"wait": CODE, "scope": "Rebound", "reason": "Rebind"}),
                                   ("doing", {}), ("open", {})):
                with self.subTest(status=status), self.assertRaises(Denied):
                    self.change(item, status, **kwargs)
            self.assertEqual(self.store.get(item["id"]), item)
            item = self.change(item, "open", reason=live["reason"] + ": reassess missing evidence")
            self.assertIsNone(item["blocked_since"])
        with clock(12) as reopened:
            item = self.change(item, "blocked", wait=CODE)
            self.assertEqual(item["blocked_since"], reopened)
            self.assertEqual(self.store.page_ready()["total"], 0)

    def test_doing_cannot_hide_age_then_reblock_expired_episode(self):
        self.store.blocked_max_age_seconds = 10
        with clock(0):
            item = self.block()
        with clock(9):
            item = self.change(item, "doing")
        with clock(10):
            with self.assertRaisesRegex(Denied, "Reopen"):
                self.change(item, "blocked", wait=CODE)
            with self.assertRaisesRegex(Denied, "Reopen"):
                self.change(item, "blocked", wait=FUTURE)
            with self.assertRaisesRegex(Denied, "reason"):
                self.change(item, "open")
            item = self.change(item, "open", reason="blocked_max_age_reached: replan")
            self.change(item, "blocked", wait=CODE)

    def test_satisfied_wait_releases_even_after_the_age_bound(self):
        self.store.blocked_max_age_seconds = 10
        baseline = self.project.snapshots.get()["code_digest"]
        with clock(0):
            item = self.block()
        self.assertEqual(item["wait"]["digest"], baseline)
        with clock(10), patch.object(self.store.snapshots, "get", return_value={"code_digest": "f" * 64}):
            live = self.store.page_ready(role_name="worker")["items"][0]["wait_liveness"]
            self.assertEqual(live["state"], "due")
            doing = self.change(item, "doing")
            self.assertEqual(doing["status"], "doing")

    def test_due_old_wait_cannot_register_a_new_pending_wait_without_opening(self):
        self.store.blocked_max_age_seconds = 10
        with clock(0):
            item = self.block()
            baseline = item["wait"]["digest"]
            item = self.change(item, "doing")
        with clock(10), patch.object(self.store.snapshots, "get", return_value={"code_digest": baseline}):
            with self.assertRaisesRegex(Denied, "blocked_max_age_reached"):
                self.change(item, "blocked", wait=CODE)

    def test_blocked_decision_seed_ages_without_writing_a_transition(self):
        with clock(0), patch("mizu.insights.now", return_value=START.isoformat()):
            proposal = self.project.insights.submit(source="worker", title="Contingent", body="Review", base_snapshot=None)
            event = self.project.insights.decide(proposal["id"], "accept", "Deliver scope", "", "run", expected_rev=1,
                work_items=[{"title": "Contingent task", "description": "Wait for changed code", "wait": CODE}])
            seed = event["work_items"][0]
        with clock(604800):
            page = self.store.page_ready(role_name="worker")
            self.assertEqual(page["items"][0]["wait_liveness"]["state"], "expired")
            self.assertIsNone(self.store.get(seed["id"])["holder"])
            self.assertEqual(self.store.get(seed["id"]), seed)
            self.assertEqual(self.store.page_open_readonly()["items"][0]["blocked_since"], seed["created_at"])

    def test_explicit_deadline_is_independent_and_clock_rollback_clamps_age(self):
        self.store.blocked_max_age_seconds = 1
        with clock(0):
            item = self.block(wait=FUTURE)
        with clock(-1):
            row = self.store.page_open_readonly()["items"][0]
            self.assertEqual(row["blocked_age_seconds"], 0)
        with clock(604800):
            self.assertEqual(self.store.page_ready()["total"], 0)
            state = self.store.blocked_liveness_readonly()["items"][0]
            self.assertEqual((state["state"], state["blocked_max_age_seconds"]), ("pending", None))
            item = self.change(item, "doing")
            item = self.change(item, "blocked", wait=FUTURE)
        with clock(604800):
            self.block("due", {"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
            live = self.store.page_ready()["items"][0]["wait_liveness"]
            self.assertEqual((live["state"], live["reason"]), ("due", "condition_met"))
            self.assertIsNone(live["blocked_max_age_seconds"])

    def test_expired_status_full_delta_prompts_and_readonly_facade_do_not_write(self):
        with clock(0):
            item = self.block()
        self.project.status()  # Initialize unrelated check-job status lock before observing writes.
        before = {p.relative_to(self.project.root): p.read_bytes()
                  for p in self.project.root.rglob("*") if p.is_file()}
        with clock(604800), patch("mizu.work_items.lock", side_effect=AssertionError("read-only lock")):
            view = readonly_view(self.store)
            row = view.page_open_readonly()["items"][0]
            self.assertEqual(row["blocked_age_seconds"], 604800)
            diagnostic = self.project.status()["blocked_work"]
            self.assertEqual(diagnostic["counts"]["expired"], 1)
            self.assertEqual(diagnostic["items"][0]["reason"], "blocked_max_age_reached")
        after = {p.relative_to(self.project.root): p.read_bytes()
                 for p in self.project.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        with clock(604800):
            context = self.context()
            full = json.loads(prompt_for(context))
            delta = json.loads(prompt_delta_for(context, []))
            for prompt in (full, delta):
                row = prompt["ready_work"]["items"][0]
                self.assertEqual(row["wait_liveness"]["blocked_age_seconds"], 604800)
                self.assertEqual(row["wait_liveness"]["reason"], "blocked_max_age_reached")
                self.assertFalse(row["wait_satisfied"])
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

    def test_impossible_requires_reasoned_open_but_unknown_never_releases(self):
        with clock(0):
            item = self.block(wait={"kind": "insight_decided", "insight": "f" * 32})
        with clock(604800):
            state = self.store.page_ready()["items"][0]["wait_liveness"]
            self.assertEqual((state["state"], state["reason"]), ("impossible", "target_missing"))
            with self.assertRaisesRegex(Denied, "Reopen"):
                self.change(item, "blocked", wait=CODE, scope="New wait", reason="Rebind")
            with self.assertRaisesRegex(Denied, "reason"):
                self.change(item, "open")
            item = self.change(item, "open", reason=state["reason"])
            item = self.change(item, "blocked", wait=CODE)
            with patch.object(self.store.snapshots, "get", side_effect=Denied("unsafe")):
                # The pure helper cannot convert unreadable snapshot evidence into expiry.
                from mizu.work_item_waits import liveness
                self.assertEqual(liveness(self.store, item["wait"], None, item=item)["state"], "unknown")
