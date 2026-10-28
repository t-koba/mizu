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

    def test_role_filter_precedes_ready_page_selection_and_counting(self):
        # An earlier other-holder row must not hide this role's later work.
        other = self.item("other", "doing")
        own = self.item("own")
        self.project.work_items.transition(own["id"], expected_rev=1,
                                           status="doing", actor="reviewer")
        page = self.project.work_items.page_ready(limit=1, role_name="reviewer")
        self.assertEqual([row["id"] for row in page["items"]], [own["id"]])
        self.assertEqual((page["total"], page["overflow_count"]), (1, 0))
        self.project.work_items.transition(own["id"], expected_rev=2, status="blocked",
            actor="reviewer", wait={"kind": "deadline", "at": "2000-01-01T00:00:00+00:00"})
        opened = self.item("unheld")
        page = self.project.work_items.page_ready(limit=1, role_name="reviewer")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["overflow_count"], 1)
        self.assertNotEqual(page["items"][0]["id"], other["id"])
        self.assertEqual(self.project.work_items.page_ready()["total"], 3)
        self.assertEqual(self.project.work_items.get(own["id"])["rev"], 3)
        with self.assertRaises(Denied):
            self.project.work_items.page_ready(role_name="../invalid")

    def test_transition_capable_read_role_admits_without_source_authority(self):
        role = self.config.roles["reviewer"]
        self.config.roles["reviewer"] = dataclasses.replace(role, capabilities=(
            *role.capabilities, "work_items_transition"))
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, "reviewer")  # consume initial wake/code
        other = self.item("other-role", "doing")
        self.assertEqual(engine.run(self.project, "reviewer")["skipped"], "unchanged")
        opened = self.item("open-to-claim")
        before = self.project.snapshots.get()["id"]
        self.assertEqual(engine.run(self.project, "reviewer")["status"], "completed")
        self.assertEqual(self.project.snapshots.get()["id"], before)
        self.project.work_items.transition(opened["id"], expected_rev=1,
                                           status="doing", actor="reviewer")
        # Claiming the same ID changes no membership and creates no ready event.
        self.assertEqual(engine.run(self.project, "reviewer", event_trigger=True)["skipped"], "no-event")
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name="reviewer"))
        ctx = self.context("reviewer")
        self.assertEqual(ctx.role.workspace, "read")
        self.assertNotEqual(ctx.workspace, self.project.workspace)
        self.assertEqual(ctx.role.capabilities, (*role.capabilities, "work_items_transition"))
        self.assertEqual(self.project.work_items.get(other["id"])["holder"], "worker")

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


class ReadySetChangeTests(Fixture):
    item = ReadyWorkTests.item
    parked = ReadyWorkTests.parked

    def engine(self, callback=None):
        self.config.roles['worker'] = dataclasses.replace(self.config.roles['worker'], on_change=True)
        return Engine(self.config, driver=ScriptDriver(callback))

    def signature(self, role='worker', limit=1):
        return self.project.work_items.page_ready(limit=limit, role_name=role)['signature']

    def test_wait_with_unchanged_ready_work_parks_both_dispatch_paths(self):
        self.item('persistent')
        engine = self.engine()
        self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        snapshot = self.project.snapshots.get()
        self.assertEqual(snapshot['progress']['schema'], 3)
        self.assertEqual(snapshot['progress']['ready_work_signature'], self.signature())
        self.assertTrue(self.project.status()['idle_with_open_work']['ready_work'])
        for _ in range(3):
            self.assertFalse(should_run(self.project, snapshot, role_name='worker'))
            self.assertEqual(engine.run(self.project, 'worker')['skipped'], 'unchanged')
            self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
        # Independent run wait expiry is not suppressed by stable ready work.
        expired = {**snapshot, 'wake_at': 1}
        self.assertTrue(should_run(self.project, expired, current_time=2, role_name='worker'))
        self.project.set_control(wake_generation='new-operator-input')
        self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))

    def test_add_remove_and_equal_total_replacement_beyond_prompt_limit(self):
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=1))
        self.project.config = self.config
        items = [self.item(f'offscreen-{i}') for i in range(3)]
        engine = self.engine()
        engine.run(self.project, 'worker')
        original = json.loads(engine.driver.calls[-1][1])['ready_work']
        old_signature = self.signature()
        self.project.work_items.transition(items[-1]['id'], expected_rev=1, actor='worker',
                                           status='dropped', reason='Replaced candidate')
        self.item('replacement')
        page = self.project.work_items.page_ready(limit=1, role_name='worker')
        self.assertEqual(page['total'], original['total'])
        self.assertEqual(page['items'][0]['id'], original['items'][0]['id'])
        self.assertNotEqual(self.signature(), old_signature)
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        extra = self.item('addition')
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        self.project.work_items.transition(extra['id'], expected_rev=1, actor='worker',
                                           status='dropped', reason='Removed candidate')
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))

    def test_removal_to_empty_wakes_once(self):
        item = self.item('last')
        engine = self.engine()
        engine.run(self.project, 'worker')
        self.project.work_items.transition(item['id'], expected_rev=1, actor='worker',
                                           status='blocked', reason='Needs reassessment')
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        engine.run(self.project, 'worker', event_trigger=True)
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')

    def test_new_ready_item_during_unit_is_not_consumed_at_finish(self):
        self.item('offered')
        before = self.signature()
        engine = self.engine(lambda *_: self.item('arrived-during-run'))
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.assertEqual(snapshot['progress']['ready_work_signature'], before)
        self.assertNotEqual(before, self.signature())
        self.assertTrue(snapshot['progress']['ready_work'])
        self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
        engine.driver = ScriptDriver()
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))

    def test_blocked_wait_satisfaction_changes_membership_without_transition(self):
        blocked = self.item('waiting', 'blocked',
                            {'kind': 'deadline', 'at': '2099-01-01T00:00:00+00:00'})
        engine = self.engine()
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, snapshot, role_name='worker'))
        # Controlled wait fact, no clock sleeps or ledger transition.
        with patch.object(self.project.insights, '_wait_due', return_value=True):
            self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
            self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
            self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(self.project.work_items.get(blocked['id'])['rev'], 2)

    def test_full_and_delta_inputs_capture_same_role_scoped_signature(self):
        self.item('other-held', 'doing')
        own = self.item('claimable')
        ctx = self.context('reviewer')
        for build in (lambda: prompt_for(ctx), lambda: prompt_delta_for(ctx, [])):
            page = json.loads(build())['ready_work']
            self.assertEqual([r['id'] for r in page['items']], [own['id']])
            self.assertEqual(ctx.ready_work_signature, self.signature('reviewer'))
            self.assertNotEqual(ctx.ready_work_signature, self.signature('worker'))

    def test_unnamed_diagnostic_does_not_invent_a_change_for_empty_or_equal_sets(self):
        engine = self.engine()
        engine.run(self.project, 'worker')
        self.assertFalse(should_run(self.project, self.project.snapshots.get()))
        self.item('same-aggregate-and-worker-set')
        engine.run(self.project, 'worker')
        self.assertFalse(should_run(self.project, self.project.snapshots.get()))

    def test_legacy_migration_empty_and_nonempty(self):
        from mizu.runtime import _ready_work_changed
        role = self.config.roles['worker']
        base = self.parked('wait')
        v1 = base['progress']
        v2 = {**v1, 'schema': 2, 'ready_work': False}
        for progress in (None, v1, v2):
            record = {**base, 'progress': progress}
            self.assertFalse(_ready_work_changed(self.project, record, role))
        self.item('new-to-legacy')
        for progress in (None, v1, v2):
            self.assertTrue(_ready_work_changed(self.project, {**base, 'progress': progress}, role))
        engine = self.engine()
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.assertEqual(snapshot['progress']['schema'], 3)
        self.assertFalse(_ready_work_changed(self.project, snapshot, role))
        # Invalid evidence closes, never reinterpreted as permissive legacy.
        for bad in ({}, {**snapshot['progress'], 'ready_work_signature': 'bad'}):
            self.assertFalse(_ready_work_changed(self.project, {'progress': bad}, role))

    def test_signature_schema_validation_and_projection(self):
        from mizu.snapshot import progress_projection, validate_progress
        self.engine().run(self.project, 'worker')
        progress = self.project.snapshots.get()['progress']
        self.assertEqual(progress_projection(progress)['ready_work_signature'], self.signature())
        for bad in ('', 'a' * 65, 'A' * 64, 1, [], {}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                validate_progress({**progress, 'ready_work_signature': bad})
        validate_progress({**progress, 'ready_work_signature': None})
        with self.assertRaises(Denied):
            validate_progress({k: v for k, v in progress.items() if k != 'ready_work_signature'})

    def test_unchanged_ready_work_does_not_self_admit_writer(self):
        self.item('persistent-ready-work')
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, no_progress_backoff_seconds=60))
        self.project.config = self.config
        def finish(ctx, *_):
            ctx.handle('finish', {'outcome': 'continue', 'summary': 'Observed only',
                                  'state': 'An ordinary free-text rewrite'})
        engine = self.engine(finish)
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.assertTrue(snapshot['progress']['has_progress'])
        self.assertEqual(snapshot['progress']['no_progress_count'], 0)
        stamp = dt.datetime.fromisoformat(snapshot['progress']['recorded_at']).timestamp()
        self.assertFalse(should_run(self.project, snapshot, current_time=stamp + 61,
                                    role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker')['skipped'], 'unchanged')
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')

    def test_observer_records_offered_signature_not_work_arriving_during_run(self):
        role = self.config.roles['reviewer']
        self.config.roles['reviewer'] = dataclasses.replace(
            role, capabilities=(*role.capabilities, 'work_items_transition'))
        before = self.signature('reviewer')
        engine = Engine(self.config, driver=ScriptDriver(lambda *_: self.item('observer-new')))
        engine.run(self.project, 'reviewer')
        cursor = read_json(self.project.root / 'observed' / 'reviewer.json')
        self.assertEqual(cursor['progress']['ready_work_signature'], before)
        snapshot = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, snapshot, role_name='reviewer'))
        engine.driver = ScriptDriver()
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True)['status'], 'completed')
        self.assertFalse(should_run(self.project, snapshot, role_name='reviewer'))

    def test_fingerprint_scan_memory_output_and_order_are_bounded(self):
        import tracemalloc
        store = self.project.work_items
        seed = self.item('template')
        view = {'item': seed, 'current_origin_rev': None, 'stale': False, 'redecision': None,
                'doing_since': None, 'doing_age_seconds': None, 'doing_stale': False}

        def scan(count, reverse=False, limit=1):
            indices = range(count - 1, -1, -1) if reverse else range(count)
            events = ((f'{i:032x}', {'id': f'wi_{i:032x}'}) for i in indices)
            with patch.object(store, '_iter_item_events', return_value=events), \
                 patch.object(store, '_seed_for_event', new=lambda _, event, **kw: ('seed', event)), \
                 patch.object(store, '_build_projection', new=lambda item_id, **kw: {
                     **view, 'item': {**seed, 'id': item_id}}):
                tracemalloc.start()
                try:
                    page = store._page_open_impl(cursor=None, limit=limit, readonly=True,
                                                 ready_only=True, role_name='worker')
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            return page, peak

        small, small_peak = scan(10)
        large, large_peak = scan(5000)
        reordered, _ = scan(5000, reverse=True, limit=2)
        self.assertEqual(large['signature'], reordered['signature'])
        self.assertNotEqual(small['signature'], large['signature'])
        self.assertEqual(len(large['signature']), 64)
        self.assertEqual(len(large['items']), 1)
        self.assertLess(len(json.dumps(large)), 8192)
        # One heap row plus fixed-size accumulator, not retained IDs/hashes.
        self.assertLess(large_peak - small_peak, 128 * 1024)
