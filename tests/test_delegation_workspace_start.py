"""Reservation/admission serialization, not async runner or quiescence proof."""
import contextlib
from unittest.mock import patch

from support import Fixture
from mizu import delegation_jobs as jobs
from mizu.errors import Busy, Denied
from mizu.fs import held, lock
from mizu.runtime import Engine


class WorkspaceStartTests(Fixture):
    def setUp(self):
        super().setUp()
        self.engine = Engine(self.config)
        self.workspace_lock = self.project.root / 'locks' / 'workspace.lock'
        self.state = self.project.root / 'delegation-jobs' / 'state.json'
        self.request = {'role': 'helper', 'task': 'Implement a focused change',
                        'profile': None,
                        'source_snapshot': self.project.snapshots.get()['id']}

    def admit(self, stack):
        return self.engine._admit(self.project, 'worker', self.config.roles['worker'],
                                  stack, wait=False)

    def assert_admitted(self):
        with contextlib.ExitStack() as stack:
            self.admit(stack)

    def test_existing_writer_excludes_start_before_initialization_or_preparation(self):
        with contextlib.ExitStack() as writer:
            self.admit(writer)
            with self.assertRaises(Busy):
                with jobs.DelegationJobs.workspace_start(self.project.root):
                    self.fail('Preparation must not run behind an admitted writer')
            self.assertFalse(self.state.parent.exists())
        self.assert_admitted()

    def test_preparation_and_commit_hold_the_admission_lock(self):
        with jobs.DelegationJobs.workspace_start(self.project.root) as store:
            self.assertTrue(held(self.workspace_lock))
            # Before there is a durable reservation, the process lock fences
            # both a second coordinator and real writer admission.
            self.assertIsNone(store.reservation())
            with contextlib.ExitStack() as contender, self.assertRaises(Busy):
                self.admit(contender)
            with self.assertRaises(Busy):
                with jobs.DelegationJobs.workspace_start(self.project.root):
                    self.fail('Second preparation ran')
            # Real source preparation happens inside the same critical section.
            snap = self.project.snapshots.create(
                self.project.snapshots.capture_files(self.project.workspace),
                goal='test', state='', run='fixture', outcome='wait', summary='input')
            request = {**self.request, 'source_snapshot': snap['id']}
            job = store.start('worker', 'first', request)
        self.assertFalse(held(self.workspace_lock))
        self.assertEqual(store.reservation()['request'], request)
        with contextlib.ExitStack() as contender, self.assertRaisesRegex(Denied, 'reservation'):
            self.admit(contender)
        # Exact retry is inspection, not permission to repeat preparation.
        with lock(self.workspace_lock, blocking=False):
            self.assertEqual(store.start('worker', 'first', request), job)
        with self.assertRaises(Denied):
            with jobs.DelegationJobs.workspace_start(self.project.root):
                self.fail('Preparation ran against a committed reservation')

    def test_preparation_failure_releases_lock_without_inventing_a_reservation(self):
        with self.assertRaisesRegex(RuntimeError, 'capture failed'):
            with jobs.DelegationJobs.workspace_start(self.project.root):
                raise RuntimeError('capture failed')
        self.assertFalse(held(self.workspace_lock))
        self.assertIsNone(jobs.DelegationJobs.existing_reservation(self.project.root))
        self.assert_admitted()

    def test_commit_failure_preserves_old_or_new_state_and_never_unreserves(self):
        real_write = jobs.safe_write
        for committed in (False, True):
            with self.subTest(committed=committed):
                def fail_write(*args, **kwargs):
                    self.assertTrue(held(self.workspace_lock))
                    if committed:
                        real_write(*args, **kwargs)
                    raise OSError('lost commit acknowledgement')

                with self.assertRaises(OSError):
                    with jobs.DelegationJobs.workspace_start(self.project.root) as store:
                        with patch.object(jobs, 'safe_write', side_effect=fail_write):
                            store.start('worker', 'uncertain', self.request)
                self.assertFalse(held(self.workspace_lock))
                reopened = jobs.DelegationJobs(self.project.root)
                if committed:
                    self.assertEqual(reopened.reservation()['request'], self.request)
                    with contextlib.ExitStack() as contender, self.assertRaises(Denied):
                        self.admit(contender)
                    with self.assertRaises(Denied):
                        with jobs.DelegationJobs.workspace_start(self.project.root):
                            self.fail('Uncertain commit repeated preparation')
                else:
                    self.assertIsNone(reopened.reservation())
                    self.assert_admitted()

    def test_interrupted_initialization_or_corrupt_ledger_denies_before_yield(self):
        self.state.parent.mkdir()
        for raw in (None, b'not-json'):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.state.write_bytes(raw)
                    (self.state.parent / 'store.lock').touch()
                with self.assertRaises(Denied):
                    with jobs.DelegationJobs.workspace_start(self.project.root):
                        self.fail('Unknown state permitted preparation')
                self.assertFalse(held(self.workspace_lock))
                with contextlib.ExitStack() as contender, self.assertRaises(Denied):
                    self.admit(contender)
                if raw is None:
                    self.assertFalse(self.state.exists())
                else:
                    self.assertEqual(self.state.read_bytes(), raw)
