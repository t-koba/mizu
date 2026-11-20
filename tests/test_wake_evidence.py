"""Wake evidence owns bounded observer cursors and generation comparison."""
import unittest
from pathlib import Path
from unittest.mock import patch

from mizu import runtime, wake_evidence
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import digest


class FakeProject:
    def __init__(self):
        self.root = Path('/project')


class WakeEvidenceOwnerTests(unittest.TestCase):
    def test_runtime_reexports_the_owner_objects(self):
        for name in ('_normalized_wake_generation', '_wake_generation_digest',
                     '_role_cursor', '_operator_woke'):
            with self.subTest(name=name):
                owner = getattr(wake_evidence, name)
                self.assertEqual(owner.__module__, 'mizu.wake_evidence')
                self.assertIs(getattr(runtime, name), owner)
        self.assertEqual(runtime.OBSERVED_CURSOR_MAX_BYTES, 4096)
        self.assertEqual(runtime.OBSERVED_CURSOR_MAX_BYTES,
                         wake_evidence.OBSERVED_CURSOR_MAX_BYTES)

    def test_non_string_control_generations_never_wake(self):
        for control in ({'wake_generation': None}, {'wake_generation': 17},
                        {'wake_generation': []}, {'wake_generation': b'gen'},
                        {'wake_generation': {}}):
            for observer in (False, True):
                with self.subTest(control=control, observer=observer):
                    self.assertFalse(wake_evidence._operator_woke(
                        control, {'wake_generation': 'gen',
                                  'wake_generation_digest': digest(b'other')},
                        observer=observer))

    def test_publisher_comparison_normalizes_the_cursor_generation(self):
        control = {'wake_generation': 'gen'}
        self.assertFalse(wake_evidence._operator_woke(
            control, {'wake_generation': 'gen'}))
        self.assertTrue(wake_evidence._operator_woke(control, {}))
        self.assertTrue(wake_evidence._operator_woke(
            control, {'wake_generation': None}))
        with patch('mizu.wake_evidence._normalized_wake_generation',
                   wraps=wake_evidence._normalized_wake_generation) as normalized:
            self.assertTrue(wake_evidence._operator_woke(
                control, {'wake_generation': 17}))
            normalized.assert_called_once_with(17)

    def test_observer_comparison_uses_a_bounded_digest_fallback(self):
        control = {'wake_generation': 'gen'}
        self.assertFalse(wake_evidence._operator_woke(
            control, {'wake_generation_digest': digest(b'gen')}, observer=True))
        self.assertTrue(wake_evidence._operator_woke(control, {}, observer=True))
        self.assertEqual(wake_evidence._normalized_wake_generation(17), '')
        self.assertEqual(wake_evidence._wake_generation_digest(17), digest(b''))
        self.assertFalse(wake_evidence._operator_woke(
            {'wake_generation': ''},
            {'wake_generation_digest': digest(b'')}, observer=True))
        # A malformed cursor digest is bounded evidence, never an exception.
        self.assertTrue(wake_evidence._operator_woke(
            control, {'wake_generation_digest': None}, observer=True))

    def test_role_cursor_reads_bounded_file_and_closes_on_bad_reads(self):
        project = FakeProject()
        expected = project.root / 'observed' / 'worker.json'
        with patch('mizu.wake_evidence.read_json', return_value={'a': 1}) as reader:
            self.assertEqual(wake_evidence._role_cursor(project, 'worker'), {'a': 1})
            reader.assert_called_once_with(expected, {}, max_bytes=4096)
        for value in ([], 'malformed', None, 17):
            with self.subTest(value=value), \
                    patch('mizu.wake_evidence.read_json', return_value=value):
                self.assertEqual(wake_evidence._role_cursor(project, 'worker'), {})
        for error in (OSError('missing'), ValueError('bad json'), Busy('busy'),
                      Denied('denied'), LimitExceeded('too big')):
            with self.subTest(error=error), \
                    patch('mizu.wake_evidence.read_json', side_effect=error):
                self.assertEqual(wake_evidence._role_cursor(project, 'worker'), {})


if __name__ == '__main__':
    raise SystemExit(unittest.main())
