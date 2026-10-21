"""Obsolete event attempts do not enter admission; locked dispatch rechecks."""
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
