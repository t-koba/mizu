"""Work readiness owns bounded observation; runtime keeps its re-export seam."""
import unittest
from unittest.mock import patch

from mizu import runtime, work_readiness
from mizu.snapshot import PROGRESS_SIGNALS


class FakeRole:
    def __init__(self, name='worker', capabilities=('work_items_transition',)):
        self.name = name
        self.capabilities = frozenset(capabilities)


class FakeStore:
    def __init__(self, page):
        self.page = page
        self.calls = []

    def page_ready(self, **kwargs):
        self.calls.append(kwargs)
        return self.page


class FakeProject:
    def __init__(self, page):
        self.work_items = FakeStore(page)


def _progress(role, **extra):
    record = {
        'schema': 1, 'signals': {name: False for name in PROGRESS_SIGNALS},
        'selected': [], 'role': role, 'has_progress': False,
        'no_progress_count': 1, 'obligations_digest': '0' * 64,
        'recorded_at': '2026-01-01T00:00:00+00:00',
    }
    record.update(extra)
    return record


class WorkReadinessOwnerTests(unittest.TestCase):
    def test_runtime_reexports_the_owner_functions(self):
        for name in ('_has_ready_work', '_ready_work_observation', '_ready_work_changed',
                     '_writer_wait_liveness', '_writer_wait_sources'):
            with self.subTest(name=name):
                owner = getattr(work_readiness, name)
                self.assertEqual(owner.__module__, 'mizu.work_readiness')
                self.assertIs(getattr(runtime, name), owner)

    def test_has_ready_work_needs_the_capability_and_reads_one_row(self):
        project = FakeProject({'items': [{'id': 'wi_1'}]})
        reader = FakeRole(capabilities=())
        self.assertFalse(work_readiness._has_ready_work(project, reader))
        self.assertEqual(project.work_items.calls, [])
        self.assertTrue(work_readiness._has_ready_work(project, FakeRole()))
        self.assertEqual(project.work_items.calls,
                         [{'limit': 1, 'role_name': 'worker'}])
        project.work_items.page = {'items': []}
        self.assertFalse(work_readiness._has_ready_work(project, FakeRole()))
        self.assertEqual(project.work_items.calls[-1],
                         {'limit': 1, 'role_name': 'worker'})

    def test_observation_fails_closed_on_malformed_progress(self):
        project = FakeProject({'signature': 'a' * 64, 'total': 1,
                               '_wait_resolution_pending': False})
        self.assertEqual(work_readiness._ready_work_observation(
            project, {'progress': {'schema': 3}}, FakeRole()), (False, False, False))
        self.assertEqual(project.work_items.calls, [])

    def test_observation_fails_closed_on_a_mismatched_progress_role(self):
        progress = _progress(
            'reporter', schema=4, ready_work=False, ready_work_signature='1' * 64,
            wait_resolution_tokens=['wi_' + 'a' * 32 + ':1'])
        project = FakeProject({'signature': 'b' * 64, 'total': 1,
                               '_wait_resolution_pending': False})
        self.assertEqual(work_readiness._ready_work_observation(
            project, {'progress': progress}, FakeRole()), (False, False, False))
        self.assertEqual(project.work_items.calls,
                         [{'limit': 1, 'role_name': 'worker',
                           '_delivered_wait_resolution_tokens': []}])

    def test_writer_wait_liveness_only_delegates(self):
        project, role, releases = object(), FakeRole(), ('wi_1',)
        for live in (True, False, None):
            with self.subTest(live=live):
                with patch('mizu.work_readiness._writer_wait_sources',
                           return_value=live) as sources:
                    result = work_readiness._writer_wait_liveness(
                        project, role, releases=releases)
                self.assertIs(result, live)
                sources.assert_called_once_with(project, role, releases=releases)


if __name__ == '__main__':
    raise SystemExit(unittest.main())
