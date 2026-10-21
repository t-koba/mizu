"""Ready ledger work is a bounded wake/input source, not a terminal event."""
import dataclasses
import datetime as dt
import json
from threading import Event
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.errors import Denied
from mizu.fs import now, read_json, write_json
from mizu.runtime import (Engine, MAX_READY_WORK_AGE_SECONDS, _ready_work_age,
                          daemon, prompt_delta_for, prompt_for, should_run)
from mizu.snapshot import PROGRESS_SIGNALS


class ReadyWorkTests(Fixture):
    def item(self, key, status="open", wait=None):
        item = self.project.work_items.create(
            idempotency_key=key, title=f"Ready candidate {key}", actor="worker")["record"]["work_item"]
        if status != "open":
            self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                               status=status, wait=wait)
        return item

    def parked(self, outcome="continue"):
        current = self.project.snapshots.get()
        progress = {"schema": 1, "signals": {name: False for name in PROGRESS_SIGNALS},
                    "selected": list(self.config.limits.progress_signals), "role": "worker",
                    "has_progress": False, "no_progress_count": 1,
                    "obligations_digest": "0" * 64, "recorded_at": now()}
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Parked", run=None, outcome=outcome,
            summary="Parked test", wake_at=4102444800 if outcome == "wait" else None,
            inbox_seen=self.project.insights.generation(), wake_generation="test-wake",
            progress=progress, parent=current["id"])
        self.project.snapshots.publish(snapshot)
        return snapshot

    def test_open_and_doing_wake_done_wait_and_no_progress(self):
        for outcome in ("done", "wait", "continue", "bounded"):
            snapshot = self.parked(outcome)
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
            item = self.item(outcome)
            for status, rev in (("open", 1), ("doing", 2)):
                if status == "doing":
                    self.project.work_items.transition(item["id"], expected_rev=1,
                                                       status=status, actor="worker")
                self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
                for control in ({"armed": False}, {"paused": True}, {"draining": True}):
                    self.project.set_control(**control)
                    self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
                    self.project.set_control(armed=True, paused=False, draining=False)
            self.project.work_items.transition(item["id"], expected_rev=rev,
                                               status="dropped", actor="worker", reason="Test candidate settled")

    def test_blocked_rows_cannot_hide_ready_work_or_wake_on_their_own(self):
        for key in range(3):
            # A blocked row without a wait also remains non-ready.
            self.item(str(key), "blocked")
        snapshot = self.parked("done")
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(self.project.work_items.page_ready(limit=1)["total"], 0)
        ready = self.item("last")
        general = self.project.work_items.page_open(limit=2)
        self.assertTrue(general["truncated"])
        self.assertNotIn(ready["id"], [row["id"] for row in general["items"]])
        page = self.project.work_items.page_ready(limit=1)
        self.assertEqual([row["id"] for row in page["items"]], [ready["id"]])
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        self.project.work_items.transition(ready["id"], expected_rev=1, actor="worker",
            status="blocked", wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))

    def test_store_ready_page_bounds_and_unseeded_coverage(self):
        for index in range(3):
            self.item(str(index))
        write_json(self.project.root / "decision-history" / ("b" * 32 + ".json"),
                   {"action": "accept"}, exclusive=True)
        page = self.project.work_items.page_ready(limit=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual((page["total"], page["overflow_count"], page["truncated"]), (3, 1, True))
        self.assertEqual(page["unseeded_decisions"], 1)
        self.assertFalse(page["coverage_complete"])
        for limit in (0, 101, True):
            with self.assertRaises(Denied):
                self.project.work_items.page_ready(limit=limit)

    def test_ready_read_fails_closed_and_explicit_wakes_keep_short_circuit(self):
        snapshot = self.parked("done")
        with patch.object(self.project.work_items, "page_ready", side_effect=Denied("unsafe")):
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
            with self.assertRaises(Denied):
                prompt_for(self.context())
            with self.assertRaises(Denied):
                prompt_delta_for(self.context(), [])
        with patch.object(self.project.work_items, "page_ready", side_effect=AssertionError("not needed")):
            self.assertTrue(should_run(self.project, {**snapshot, "goal_digest": "changed"}, role_name="worker"))
            self.project.set_control(wake_generation="explicit")
            self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
            self.project.set_control(armed=False)
            self.assertFalse(should_run(self.project, snapshot, role_name="worker"))

    def test_dispatch_unchanged_and_event_gates_do_not_suppress_ready_work(self):
        self.config.roles["worker"] = dataclasses.replace(self.config.roles["worker"], on_change=True)
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, "worker")  # Mark code observed, then keep it unchanged.
        self.item("dispatch")
        for outcome in ("done", "wait", "continue"):
            for event_trigger in (False, True):
                self.parked(outcome)
                result = engine.run(self.project, "worker", event_trigger=event_trigger)
                self.assertEqual(result["status"], "completed")
                context = driver.calls[-1][0]
                self.assertEqual(read_json(context.run_dir / "started.json")["admission"], "ready_work")
                self.assertEqual(json.loads(driver.calls[-1][1])["ready_work"]["total"], 1)

    def test_read_only_admission_is_unchanged(self):
        self.item("writer-work")
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")
        snapshot = self.parked("done")
        with patch.object(self.project.work_items, "page_ready", side_effect=AssertionError("read role")):
            self.assertFalse(should_run(self.project, snapshot, role_name="reviewer"))

    def test_prompt_ready_page_is_independent_bounded_and_shared(self):
        for index in range(3):
            self.item(f"blocked-{index}", "blocked")
        with patch("mizu.work_items.now", return_value="2026-01-01T01:00:00+01:00"):
            ready = [self.item(f"ready-{index}") for index in range(3)]
        ctx = self.context()
        ctx.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=2))
        stamp = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        with patch("mizu.runtime.time.time", return_value=stamp + 42.9):
            full = json.loads(prompt_for(ctx))
            delta = json.loads(prompt_delta_for(ctx, []))
        self.assertTrue(full["open_work_items"]["truncated"])
        self.assertTrue(all(row["status"] == "blocked" for row in full["open_work_items"]["items"]))
        page = full["ready_work"]
        self.assertEqual(page, delta["ready_work"])
        self.assertEqual((page["total"], page["overflow_count"], page["truncated"]), (3, 1, True))
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue({row["id"] for row in page["items"]} <= {row["id"] for row in ready})
        for row in page["items"]:
            self.assertEqual(row["status"], "open")
            self.assertFalse(row["stale"])
            self.assertFalse(row["redecision"])
            self.assertIn("Ready candidate", row["title"])
            self.assertEqual(row["age_seconds"], 42)

    def test_empty_ready_projection_is_present_without_capabilities(self):
        ctx = self.context("consult")
        ctx.role = dataclasses.replace(ctx.role, capabilities=())
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertEqual(json.loads(prompt)["ready_work"], {
                "items": [], "total": 0, "overflow_count": 0, "truncated": False,
                "unseeded_decisions": 0, "coverage_complete": True})

    def test_prompt_age_is_safe_for_future_missing_invalid_and_extreme_times(self):
        cases = [("2026-01-01T00:00:00+00:00", 0, 0),
                 (None, 0, None), ("bad", 0, None), ("2026-01-01T00:00:00", 0, None),
                 ("0001-01-01T00:00:00+00:00", 0, MAX_READY_WORK_AGE_SECONDS),
                 ("1970-01-01T00:00:00+00:00", float("nan"), None),
                 ("1970-01-01T00:00:00+00:00", float("inf"), None)]
        for updated_at, clock, expected in cases:
            with self.subTest(updated_at=updated_at, clock=clock):
                self.assertEqual(_ready_work_age(updated_at, clock), expected)

    def test_ready_work_preserves_configured_daemon_cooldown(self):
        self.item("always-ready")
        self.parked("wait")
        event = Event()
        ticks = [1000.0]
        starts = []
        waits = []
        def wait(seconds):
            waits.append(seconds)
            ticks[0] += seconds
            if len(waits) >= 12:
                event.set()
        event.wait = wait
        def run(*args, **kwargs):
            starts.append(ticks[0])
            return {"status": "completed"}
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, idle_seconds=10, cooldown_seconds=100))
        with patch("mizu.runtime.threading.Event", return_value=event), \
             patch("mizu.runtime.load", return_value=config), \
             patch("mizu.runtime.vcs_poll_enabled", return_value=False), \
             patch("mizu.probes.service_due", return_value=None), \
             patch("mizu.runtime.time.monotonic", side_effect=lambda: ticks[0]), \
             patch("mizu.probes.next_deadline", return_value=None), patch("builtins.print"), \
             patch.object(Engine, "run", side_effect=run):
            daemon(config, "sample", "worker")
        self.assertEqual(starts, [1000, 1100])
        self.assertEqual(waits, [10] * 12)

class SatisfiedWorkWaitTests(Fixture):
    item = ReadyWorkTests.item
    parked = ReadyWorkTests.parked

    def test_expired_deadline_wakes_and_preserves_blocked_revision(self):
        self.parked('done')
        original = self.item('expired', 'blocked', {'kind': 'deadline', 'at': '2000-01-01T00:00:00+00:00'})
        before = self.project.work_items.get(original['id'])
        page = self.project.work_items.page_ready(limit=1)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['status'], 'blocked')
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(self.project.work_items.get(original['id']), before)
        ready = json.loads(prompt_for(self.context()))['ready_work']['items'][0]
        self.assertTrue(ready['wait_satisfied'])
        self.assertEqual(ready['wait']['kind'], 'deadline')

    def test_unexpired_and_unstructured_blocks_stay_out_of_ready_page(self):
        self.item('future', 'blocked', {'kind': 'deadline', 'at': '2099-01-01T00:00:00+00:00'})
        self.item('manual', 'blocked')
        self.assertEqual(self.project.work_items.page_ready()['total'], 0)

    def test_code_change_wait_wakes_only_on_published_code_change(self):
        self.parked('done')
        self.item('code', 'blocked', {'kind': 'code_change'})
        self.assertEqual(self.project.work_items.page_ready()['total'], 0)
        (self.project.workspace / 'changed.txt').write_text('changed\n')
        self.parked('done')
        self.assertEqual(self.project.work_items.page_ready()['total'], 1)
