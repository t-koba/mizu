"""Focused owner tests for bounded role-admission facts and their runtime seam."""
import contextlib
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mizu import role_admission, runtime
from mizu.fs import digest
from mizu.snapshot import PROGRESS_SIGNALS, validate_progress

OWNED = ('_reconcile_work_item_waits', '_progress_self_admits', '_on_change_observed',
         '_receipt_wait_due', '_role_events_due')


def _progress(role):
    return validate_progress({
        'schema': 1,
        'signals': {name: False for name in PROGRESS_SIGNALS},
        'selected': [],
        'role': role,
        'has_progress': False,
        'no_progress_count': 0,
        'obligations_digest': '0' * 64,
        'recorded_at': '2026-01-01T00:00:00+00:00',
    })


class RoleAdmissionOwnerTests(unittest.TestCase):
    def test_runtime_reexports_owner_functions_by_identity(self):
        for name in OWNED:
            with self.subTest(name=name):
                self.assertIs(getattr(runtime, name), getattr(role_admission, name))

    def test_progress_self_admits_refuses_foreign_and_non_continue(self):
        foreign = {'outcome': 'continue', 'progress': _progress('other')}
        with patch.object(role_admission, 'progress_disposition',
                          side_effect=AssertionError('no disposition read')):
            self.assertFalse(role_admission._progress_self_admits(foreign, None, 'writer'))
        done = {'outcome': 'done', 'progress': _progress('writer')}
        with patch.object(role_admission, 'validate_progress',
                          side_effect=AssertionError('no progress read')):
            self.assertFalse(role_admission._progress_self_admits(done, None, 'writer'))

    def test_progress_self_admits_only_on_explicit_self_admit_disposition(self):
        record = {'outcome': 'continue', 'progress': _progress('writer')}
        # Any evidence-gap or non-self-admitting disposition must fail closed:
        # a reevaluate is an evidence gap, never proof of progress.
        for disposition in ('reevaluate', 'not_applicable', 'writer_requires_work_signal',
                            'progress_requires_next', 'parked_no_progress'):
            with self.subTest(disposition=disposition):
                with patch.object(role_admission, 'progress_disposition',
                                  return_value=disposition):
                    self.assertFalse(
                        role_admission._progress_self_admits(record, None, 'writer'))
        # Only an explicit self_admit_* disposition may admit the run.
        for disposition in ('self_admit_progress', 'self_admit_allowance',
                            'self_admit_backoff'):
            with self.subTest(disposition=disposition):
                with patch.object(role_admission, 'progress_disposition',
                                  return_value=disposition):
                    self.assertTrue(
                        role_admission._progress_self_admits(record, None, 'writer'))

    def test_reconcile_skips_read_only_roles(self):
        observer = types.SimpleNamespace(name='searcher', workspace='read', capabilities=())
        project = types.SimpleNamespace(roles={'searcher': observer})
        with patch('mizu.work_item_resolution.reconcile',
                   side_effect=AssertionError('reconcile must not run')):
            self.assertEqual(role_admission._reconcile_work_item_waits(project, observer, None),
                             (False, False))
        # A writability-qualified holder with no bool proof reports no observation.
        writer = types.SimpleNamespace(
            name='writer', workspace='write',
            capabilities=('work_order', 'work_items_transition'))
        project.roles['writer'] = writer
        config = types.SimpleNamespace(limits=types.SimpleNamespace(work_items_prompt=1))
        with patch('mizu.work_item_resolution.reconcile', return_value=({}, None)):
            self.assertEqual(role_admission._reconcile_work_item_waits(project, writer, config),
                             (False, None))


if __name__ == '__main__':
    unittest.main()
