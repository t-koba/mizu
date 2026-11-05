"""Opt-in service cadence; pure rendering, not event admission or deployment."""
import dataclasses
import hashlib
import json
import plistlib
from pathlib import PurePosixPath
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from support import Fixture, default_config_tree
from mizu.config import load
from mizu.errors import ConfigError
from mizu.services import render


class EventBatchConfigTests(Fixture):
    def test_batch_bounds_and_independent_poll(self):
        self.assertIsNone(self.config.roles['reporter'].event_batch_seconds)
        original = self.file.read_text()
        for value, valid in (('60', True), ('300', True), ('2678400', True),
                             ('59', False), ('2678401', False), ('0', False),
                             ('-1', False), ('true', False), ('"300"', False), ('2.5', False)):
            with self.subTest(value=value):
                self.file.write_text(original.replace('[roles.reporter]',
                    f'[roles.reporter]\nevent_batch_seconds = {value}\nevent_poll_seconds = 120'))
                if valid:
                    role = load(self.file).roles['reporter']
                    self.assertEqual(role.event_batch_seconds, int(value))
                    self.assertEqual(role.event_poll_seconds, 120)
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)

    def test_daemon_rejects_batch(self):
        original = self.file.read_text()
        # Use the default daemon role without changing its ordinary policy.
        self.assertTrue(self.config.roles['worker'].daemon)
        self.file.write_text(original.replace('[roles.worker]',
            '[roles.worker]\nevent_batch_seconds = 300'))
        with self.assertRaisesRegex(ConfigError, 'event_batch_seconds.*daemon'):
            load(self.file)


class EventBatchRendererTests(unittest.TestCase):
    schedules = ((0, (), False), (900, (), False), (0, ('07:00',), False), (0, (), True))
    systems = ('linux', 'macos', 'windows')
    base = 'mizu-5-batch-reporter'

    def setUp(self):
        self.config = dataclasses.replace(load(default_config_tree() / 'config.toml'),
            file=PurePosixPath('/config.toml'), timezone='local')
        self.project = SimpleNamespace(name='batch', root=PurePosixPath('/project'), roles=('reporter',))

    def config_for(self, schedule):
        interval, calendar, daemon = schedule
        role = dataclasses.replace(self.config.roles['reporter'], interval_seconds=interval,
            calendar=calendar, daemon=daemon, capabilities=(), decision_events=('accept',),
            work_item_events=('done',), on_change=False, event_poll_seconds=120)
        return dataclasses.replace(self.config, roles={'reporter': role})

    def rendered(self, config, system):
        # Fixed pure inputs keep byte-level compatibility checks host-independent.
        with patch('mizu.services.sys.executable', '/python'):
            return render(config, self.project, Mock(resolve=lambda: PurePosixPath('/mizu')), system=system)

    def test_unset_preserves_legacy_bytes(self):
        # Captured before adding event batching: all files for unscheduled,
        # interval, calendar and daemon roles, including event recovery polling.
        expected = {
            'linux': 'f7942222e8169a4f65ba1bc945ba2ab2de9695026899adae80d0a6345737f400',
            'macos': '81dc4d632b4985f81c72c78e050385d6e47e79c239395e29e593146966650b63',
            'windows': 'ae431aeeed90055a0e3c1ddb2fbddcbc8cad87c3e8d3c403945ae93db4a362e7',
        }
        for system in self.systems:
            outputs = [self.rendered(self.config_for(schedule), system) for schedule in self.schedules]
            actual = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
            self.assertEqual(actual, expected[system])

    def test_batch_replaces_only_event_trigger(self):
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for system in self.systems:
            for schedule in self.schedules[:-1]:
                for seconds in (60, 300, 2678400):
                    with self.subTest(system=system, schedule=schedule, seconds=seconds):
                        config = self.config_for(schedule)
                        before = self.rendered(config, system)
                        role = dataclasses.replace(config.roles['reporter'], event_batch_seconds=seconds)
                        after = self.rendered(dataclasses.replace(config, roles={'reporter': role}), system)
                        # All ordinary schedule files are exactly unchanged.
                        self.assertEqual({k: v for k, v in before.items() if '-event.' not in k},
                                         {k: v for k, v in after.items() if '-event.' not in k})
                        event = self.base + '-event'
                        if system == 'linux':
                            self.assertNotIn(event + '.path', after)
                            self.assertEqual(after[event + '.service'], before[event + '.service'])
                            timer = after[event + '.timer']
                            self.assertIn(f'OnBootSec={seconds}s\n', timer)
                            self.assertIn(f'OnUnitInactiveSec={seconds}s\n', timer)
                            self.assertIn(f'Unit={event}.service\n', timer)
                            self.assertIn('"--event"', after[event + '.service'])
                        elif system == 'macos':
                            job = plistlib.loads(after[event + '.plist'].encode())
                            self.assertNotIn('WatchPaths', job)
                            self.assertEqual(job['StartInterval'], seconds)
                            self.assertEqual(job['ProgramArguments'][-1], '--event')
                        else:
                            job = ET.fromstring(after[event + '.xml'])
                            self.assertEqual(job.find('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval', ns).text,
                                             f'PT{seconds}S')
                            self.assertIn('--event', job.find('t:Actions/t:Exec/t:Arguments', ns).text)
                        self.assertFalse(any('WatchPaths' in v or 'PathChanged=' in v for v in after.values()))


if __name__ == '__main__':
    unittest.main()
