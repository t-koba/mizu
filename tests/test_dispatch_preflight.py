"""Obsolete event attempts do not enter admission; locked dispatch rechecks."""
import dataclasses
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu.fs import read_json
from mizu.runtime import Engine


class DispatchPreflightTests(Fixture):
    def test_no_event_does_not_acquire_or_enqueue(self):
        self.project.set_control(wake_generation="")  # no pending observer wake
        engine = Engine(self.config, driver=ScriptDriver())
        with patch.object(engine, '_admit', side_effect=AssertionError('must not queue')):
            result = engine.run(self.project, 'reviewer', event_trigger=True, retry_busy=True)
        self.assertEqual(result['skipped'], 'no-event')
        self.assertFalse((self.project.root / 'runs').exists())

    def test_observed_code_skips_before_admission(self):
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, 'reviewer')
        with patch.object(engine, '_admit', side_effect=AssertionError('must not queue')):
            result = engine.run(self.project, 'reviewer', retry_busy=True)
        self.assertEqual(result['skipped'], 'unchanged')
        self.assertEqual(len(driver.calls), 1)

    def test_peer_consumes_work_during_wait_then_locked_check_skips(self):
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        peer = Engine(self.config, driver=driver)
        original = engine._admit
        def admit(*args, **kwargs):
            peer.run(self.project, 'reviewer')
            return original(*args, **kwargs)
        with patch.object(engine, '_admit', side_effect=admit):
            result = engine.run(self.project, 'reviewer', retry_busy=True)
        self.assertEqual(result['skipped'], 'unchanged')
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(len(list((self.project.root / 'runs').iterdir())), 1)

    def test_check_grant_alone_does_not_start_unrequested_project_work(self):
        # A role with only a result subscription remains explicit-run only.
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], daemon=False, interval_seconds=0,
            calendar='', decision_events=(), work_item_events=(),
            capabilities=('check_job', 'files', 'read', 'finish'))
        self.project.work_items.create(idempotency_key='unrelated',
            title='Unrelated project work', actor='worker')
        engine = Engine(self.config, driver=ScriptDriver())
        with patch.object(engine, '_admit', side_effect=AssertionError('unsolicited work')):
            result = engine.run(self.project, 'worker', event_trigger=True, retry_busy=True)
        self.assertEqual(result['skipped'], 'no-event')
        self.assertFalse((self.project.root / 'runs').exists())

    def test_explicit_run_and_owned_check_result_keep_their_authority(self):
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], daemon=False, interval_seconds=0,
            calendar='', decision_events=(), work_item_events=())
        self.project.work_items.create(idempotency_key='explicit',
            title='Work exists', actor='worker')
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        # The result itself is still owned and delivered by the check store;
        # its presence, not an unrelated project task, admits the callback.
        with patch('mizu.runtime.check_jobs.pending', return_value=[{'id': 'owned-result'}]):
            state = engine._dispatch_state(self.project, 'worker',
                self.config.roles['worker'], event_trigger=True)
        self.assertNotIn('skip', state)
        self.assertEqual(state['_dispatch'].admission, 'wait')


class CodeChangeEventTests(Fixture):
    def _unscheduled_on_change(self):
        self.config.roles["consult"] = dataclasses.replace(
            self.config.roles["consult"], on_change=True)
        return self.config.roles["consult"]

    def test_published_code_change_admits_event_with_change_label(self):
        self._unscheduled_on_change()
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, "consult")
        before_runs = {path.name for path in (self.project.root / "runs").iterdir()}

        (self.project.workspace / "app.py").write_bytes(b"VALUE = 3\n")
        captured = self.project.snapshots.capture_files(self.project.workspace)
        changed = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Changed code.", run=None,
            outcome="wait", summary="Code-change wake test.")
        self.project.snapshots.publish(changed)

        result = engine.run(self.project, "consult", event_trigger=True)
        self.assertNotIn("skipped", result)
        self.assertEqual(len(driver.calls), 2)
        new_run = next(iter({path.name for path in (self.project.root / "runs").iterdir()}
                            - before_runs))
        self.assertEqual(read_json(self.project.root / "runs" / new_run / "started.json")["admission"],
                         "change")

    def test_observed_digest_is_a_cheap_no_event_after_state_republication(self):
        self._unscheduled_on_change()
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        engine.run(self.project, "consult", event_trigger=True)
        before_digest = self.project.snapshots.get()["code_digest"]
        captured = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(captured["code_digest"], before_digest)
        republished = self.project.snapshots.create(
            captured, goal=self.project.goal, state="New state, same code.", run=None,
            outcome="wait", summary="State-only publication.")
        self.project.snapshots.publish(republished)
        before_runs = {path.name for path in (self.project.root / "runs").iterdir()}
        with patch.object(engine, "_admit", side_effect=AssertionError("must not queue")):
            result = engine.run(self.project, "consult", event_trigger=True)
        self.assertEqual(result["skipped"], "no-event")
        self.assertEqual({path.name for path in (self.project.root / "runs").iterdir()}, before_runs)
        self.assertEqual(len(driver.calls), 1)
