"""Obsolete event attempts do not enter admission; locked dispatch rechecks."""
import dataclasses
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu.runtime import Engine


class DispatchPreflightTests(Fixture):
    def test_no_event_does_not_acquire_or_enqueue(self):
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
