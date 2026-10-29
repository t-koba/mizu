"""Pre-integration admission/maintenance gates, using real local file locks.

No child runner, native-tool fencing, or descendant quiescence is exercised.
"""
import contextlib
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from support import Fixture
from mizu import runtime, storage, workspace_cache
from mizu.config import WorkspaceCache
from mizu.delegation_jobs import DelegationJobs
from mizu.errors import Busy, Denied
from mizu.fs import lock
from mizu.runtime import Engine


class ReservationGateTests(Fixture):
    def start(self):
        store = DelegationJobs(self.project.root)
        with lock(self.project.root / 'locks/workspace.lock'):
            store.start('worker', 'first', {
                'role': 'helper', 'task': 'Focused test', 'profile': None,
                'source_snapshot': 'a' * 64})
        return store

    def terminal(self, store):
        reservation = store.reservation()
        binding = reservation['id'], reservation['token']
        store.runner_finished(*binding, 'cancelled')
        store.runner_quiescent(*binding, 'Test state only; no child launched')

    def admit(self):
        with contextlib.ExitStack() as stack:
            Engine(self.config)._admit(self.project, 'worker', self.config.roles['worker'],
                                       stack, wait=False)

    def test_absent_read_never_initializes_and_terminal_allows_admission(self):
        root = self.project.root / 'delegation-jobs'
        self.assertIsNone(DelegationJobs.existing_reservation(self.project.root))
        self.admit()
        with storage.quiescent(self.project, require_pause=False):
            pass
        self.assertFalse(root.exists())
        store = self.start()
        self.terminal(store)
        before = (root / 'state.json').read_bytes()
        self.assertIsNone(DelegationJobs.existing_reservation(self.project.root))
        self.admit()
        with storage.quiescent(self.project, require_pause=False):
            pass
        self.assertEqual((root / 'state.json').read_bytes(), before)
        object.__setattr__(self.config, 'workspace_cache', WorkspaceCache(('__pycache__',), 1, 1))
        with patch('mizu.workspace_cache._preflight', return_value=([], 0)) as preflight:
            self.assertEqual(workspace_cache.maintain(self.project)['status'], 'below-threshold')
            preflight.assert_called_once()

    def test_existing_incomplete_or_corrupt_store_is_denied_without_repair(self):
        root = self.project.root / 'delegation-jobs'
        root.mkdir()
        with self.assertRaises(Denied):
            DelegationJobs.existing_reservation(self.project.root)
        self.assertEqual(list(root.iterdir()), [])
        (root / 'store.lock').touch()
        for content in (None, b'{', b'{}', b'{"version":1,"active":null,"jobs":{}}'):
            with self.subTest(content=content):
                if content is not None:
                    (root / 'state.json').write_bytes(content)
                with self.assertRaises(Denied):
                    DelegationJobs.existing_reservation(self.project.root)
                self.assertEqual((root / 'state.json').read_bytes() if content else None, content)
        with patch('mizu.delegation_jobs.MAX_STATE_BYTES', 1):
            with self.assertRaises(Denied):
                DelegationJobs.existing_reservation(self.project.root)
        with patch('mizu.delegation_jobs.safe_read', side_effect=PermissionError):
            with self.assertRaises(Denied):
                DelegationJobs.existing_reservation(self.project.root)

    def test_run_denies_before_maintenance_and_probes(self):
        store = self.start()
        state = store.root / 'state.json'
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    state.write_bytes(b'{')
                with patch('mizu.workspace_cache.maintain') as cache, \
                     patch('mizu.storage.maintain_automatic_prune') as prune, \
                     patch('mizu.smoke_retention.maintain_automatic_smoke_retention') as smoke, \
                     patch('mizu.probes.service_due') as probes:
                    with self.assertRaises(Denied):
                        Engine(self.config).run(self.project, 'worker')
                    for operation in (cache, prune, smoke, probes):
                        operation.assert_not_called()

    def test_stale_reservation_fences_admission_storage_and_cache(self):
        store = self.start()  # Start's workspace lock is already released.
        object.__setattr__(self.config, 'workspace_cache', WorkspaceCache(('__pycache__',), 1, 1))
        reservation = store.reservation()
        binding = reservation['id'], reservation['token']
        for phase in ('queued', 'running', 'cancel-requested', 'finished', 'corrupt'):
            with self.subTest(phase=phase):
                if phase == 'running':
                    store.runner_started(*binding)
                elif phase == 'cancel-requested':
                    store.cancel('worker', binding[0])
                elif phase == 'finished':
                    store.runner_finished(*binding, 'cancelled')
                elif phase == 'corrupt':
                    (store.root / 'state.json').write_bytes(b'{}')
                with patch('mizu.runtime.safe_point.guard') as safe_point:
                    with self.assertRaises(Denied):
                        self.admit()
                    safe_point.assert_not_called()
                with self.assertRaises(Denied):
                    with storage.quiescent(self.project, require_pause=False):
                        self.fail('Entered storage maintenance')
                with patch('mizu.workspace_cache._preflight') as preflight:
                    self.assertEqual(workspace_cache.maintain(self.project), {'status': 'unavailable'})
                    preflight.assert_not_called()
                self.assertFalse((self.project.root / 'maintenance/workspace-cache.json').exists())
                # A denied attempt must not leak the workspace lock.
                with lock(self.project.root / 'locks/workspace.lock', blocking=False):
                    pass

    def test_start_between_early_read_and_admission_is_seen_under_workspace_lock(self):
        config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, idle_seconds=0.01, busy_retry_seconds=5,
            admission_wait_seconds=5))
        store = DelegationJobs(self.project.root)
        DelegationJobs.guard(self.project.root)  # Engine.run's early read is clear.
        entered = threading.Event()
        real_admission = runtime.admission

        @contextlib.contextmanager
        def observed_admission(*args, **kwargs):
            entered.set()
            with real_admission(*args, **kwargs) as elapsed:
                # Admission must own workspace.lock before the gate reads state.
                with self.assertRaises(Busy):
                    with lock(self.project.root / 'locks/workspace.lock', blocking=False):
                        pass
                yield elapsed

        def attempt():
            with contextlib.ExitStack() as stack:
                Engine(config)._admit(self.project, 'worker', config.roles['worker'], stack, wait=True)

        with patch('mizu.runtime.admission', observed_admission), \
             patch('mizu.runtime.safe_point.guard') as safe_point, \
             ThreadPoolExecutor(max_workers=1) as pool:
            with lock(self.project.root / 'locks/workspace.lock'):
                future = pool.submit(attempt)
                self.assertTrue(entered.wait(5), 'Admission did not begin')
                store.start('worker', 'race', {
                    'role': 'helper', 'task': 'Lock-order test', 'profile': None,
                    'source_snapshot': 'a' * 64})
                self.assertFalse(future.done())
            with self.assertRaises(Denied):
                future.result(timeout=10)
            safe_point.assert_not_called()
        with lock(self.project.root / 'locks/workspace.lock', blocking=False):
            pass
