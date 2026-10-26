"""Private delegation-state tests; no runtime, process or supervisor smoke claims.

Covers the durable stable child run identity: generation, retry stability,
reconstruction, owner-view hiding, tombstone retention and fail-closed
rejection of malformed/duplicate/version-1 state."""
import concurrent.futures
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mizu import delegation_jobs as jobs
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, loads_finite


class DelegationJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = jobs.DelegationJobs(self.root)
        self.path = self.root / 'delegation-jobs' / 'state.json'
        self.request = {'role': 'helper', 'task': 'Implement the focused slice',
                        'profile': None, 'source_snapshot': 'a' * 64}

    def start(self, key='first', owner='worker'):
        return self.store.start(owner, key, self.request)['id']

    def runner(self):
        reservation = self.store.reservation()
        return reservation['id'], reservation['token']

    def complete(self, text='Retained child evidence'):
        job_id, token = self.runner()
        self.store.runner_started(job_id, token)
        self.store.runner_finished(job_id, token, 'succeeded')
        self.store.runner_quiescent(job_id, token, text)

    def test_exact_and_conflicting_replay_across_all_transitions(self):
        job_id = self.start()
        original = self.store.reservation()
        for transition in ('queued', 'running', 'cancel-requested', 'finished', 'terminal'):
            with self.subTest(transition=transition):
                if transition == 'running':
                    self.store.runner_started(*self.runner())
                elif transition == 'cancel-requested':
                    self.store.cancel('worker', job_id)
                elif transition == 'finished':
                    self.store.runner_finished(*self.runner(), 'cancelled')
                elif transition == 'terminal':
                    self.store.runner_quiescent(*self.runner(), 'Stopped and cleaned')
                self.assertEqual(self.start(), job_id)
                for field, value in [('role', 'other'), ('task', 'Different task'),
                                     ('profile', 'fast'), ('source_snapshot', 'b' * 64)]:
                    with self.subTest(field=field), self.assertRaises(Denied):
                        self.store.start('worker', 'first', {**self.request, field: value})
        self.assertEqual(self.store.result('worker', job_id)['result'], 'Stopped and cleaned')
        self.assertIsNone(self.store.reservation())
        self.assertNotIn('token', self.store.status('worker', job_id))
        self.assertNotIn('request', self.store.start('worker', 'first', self.request))
        self.assertEqual(original['request'], self.request)

    def test_one_active_job_for_entire_project_and_independent_owner_keys(self):
        first = self.start()
        for owner, key in [('worker', 'second'), ('editor', 'first')]:
            with self.assertRaises(Denied):
                self.start(key, owner)
        self.complete()
        second = self.start(owner='editor')
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.reservation()['owner'], 'editor')

    def test_owner_checks_for_status_result_and_cancellation(self):
        job_id = self.start()
        for completed in (False, True):
            if completed:
                self.complete()
            for method in (self.store.status, self.store.result, self.store.cancel):
                with self.subTest(method=method.__name__, completed=completed):
                    before = self.path.read_bytes()
                    with self.assertRaises(Denied):
                        method('editor', job_id)
                    with self.assertRaises(Denied):
                        method('worker', 'b' * 64)
                    self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.cancel('worker', job_id)['status'], 'terminal')

    def test_reservation_survives_queued_cancel_failure_and_recovery(self):
        job_id = self.start()
        binding = self.runner()
        self.assertEqual(self.store.cancel('worker', job_id)['status'], 'cancel-requested')
        self.assertEqual(self.store.cancel('worker', job_id)['status'], 'cancel-requested')
        with self.assertRaises(Denied):
            self.store.runner_started(*binding)
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*binding, 'Not terminal yet')
        self.store.runner_finished(*binding, 'cancelled')
        self.assertTrue(self.store.status('worker', job_id)['reserved'])
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.runner(), binding)
        with self.assertRaises(Denied):
            self.start('second')
        self.store.runner_quiescent(*binding, 'No child launched; teardown verified')
        self.assertFalse(self.store.status('worker', job_id)['reserved'])
        self.start('second')

    def test_runner_failure_requires_explicit_quiescence_and_cannot_replay(self):
        job_id = self.start()
        binding = self.runner()
        self.store.runner_started(*binding)
        for _ in range(2):
            self.store = jobs.DelegationJobs(self.root)
            self.assertEqual(self.store.reservation()['status'], 'running')
            self.assertEqual(self.start(), job_id)
            with self.assertRaises(Denied):
                self.store.runner_started(*binding)
            with self.assertRaises(Denied):
                self.start('second')
        self.store.runner_finished(*binding, 'failed')
        self.store.runner_finished(*binding, 'failed')  # report replay is harmless
        with self.assertRaises(Denied):
            self.store.runner_finished(*binding, 'succeeded')
        self.store.cancel('worker', job_id)
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.store.reservation()['execution'], 'failed')
        with self.assertRaises(Denied):
            self.store.result('worker', job_id)
        self.assertTrue(self.store.status('worker', job_id)['reserved'])
        self.store.runner_quiescent(*binding, 'Dead runner reconciled; no live descendants')
        self.assertIsNone(self.store.reservation())
        # A lost completion acknowledgement is resolved by inspection, not replay.
        self.assertEqual(self.store.result('worker', job_id)['execution'], 'failed')
        with self.assertRaises(Denied):
            self.store.runner_started(*binding)

    def test_runner_binding_checks_and_queued_failure(self):
        self.start()
        job_id, token = self.runner()
        before = self.path.read_bytes()
        for binding in [(job_id, '0' * 64), ('b' * 64, token), (job_id, '../unsafe')]:
            for method, args in [(self.store.runner_started, ()),
                                 (self.store.runner_finished, ('failed',)),
                                 (self.store.runner_quiescent, ('evidence',))]:
                with self.subTest(method=method.__name__, binding=binding), self.assertRaises(Denied):
                    method(*binding, *args)
        with self.assertRaises(Denied):
            self.store.runner_finished(job_id, token, 'succeeded')
        self.assertEqual(before, self.path.read_bytes())
        self.store.runner_finished(job_id, token, 'failed')
        self.assertIsNotNone(self.store.reservation())
        self.store.runner_quiescent(job_id, token, 'Launch failed; teardown verified')

    def test_result_bound_and_eviction_leave_idempotency_tombstones(self):
        first = self.start()
        binding = self.runner()
        self.store.runner_started(*binding)
        self.store.runner_finished(*binding, 'succeeded')
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*binding, 'é' * (jobs.MAX_RESULT_BYTES // 2 + 1))
        self.assertIsNotNone(self.store.reservation())
        result = 'é' * (jobs.MAX_RESULT_BYTES // 2)
        self.store.runner_quiescent(*binding, result)
        self.assertEqual(self.store.result('worker', first)['result'], result)
        second = self.start('second')
        self.complete('Second result')
        self.store = jobs.DelegationJobs(self.root)
        self.assertFalse(self.store.status('worker', first)['result_available'])
        with self.assertRaises(Denied):
            self.store.result('worker', first)
        self.assertEqual(self.start(), first)
        with self.assertRaises(Denied):
            self.store.start('worker', 'first', {**self.request, 'task': 'Conflicting reuse'})
        self.assertEqual(self.store.result('worker', second)['result'], 'Second result')
        state = loads_finite(self.path.read_bytes())
        tombstone = state['jobs'][first]
        self.assertIsNone(tombstone['request'])
        self.assertIsNone(tombstone['token'])
        self.assertIsNone(tombstone['result'])
        self.assertLess(len(canonical(tombstone)), 600)

    def test_history_capacity_refuses_new_starts_but_keeps_exact_replay(self):
        with patch.object(jobs, 'MAX_JOBS', 2):
            first = self.start()
            self.complete()
            self.start('second')
            self.complete()
            self.assertEqual(self.start(), first)
            with self.assertRaises(Denied):
                self.start('third')
            self.assertIsNone(self.store.reservation())

    def test_start_atomicity_before_and_after_uncertain_commit(self):
        real_write = jobs.safe_write
        for committed in (False, True):
            with self.subTest(committed=committed):
                key = 'after' if committed else 'before'
                def interrupted(*args):
                    if committed:
                        real_write(*args)
                    raise OSError('Injected lost write acknowledgement')
                with patch.object(jobs, 'safe_write', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.start(key)
                self.store = jobs.DelegationJobs(self.root)
                reservation = self.store.reservation()
                if committed:
                    self.assertIsNotNone(reservation)
                    token = reservation['token']
                    job_id = self.start(key)
                    self.assertEqual(job_id, reservation['id'])
                    self.assertEqual(self.runner()[1], token)
                else:
                    self.assertIsNone(reservation)
                    self.start(key)
                self.complete()

    def test_uncertain_runner_claim_never_allows_second_claim(self):
        job_id = self.start()
        binding = self.runner()
        real_write = jobs.safe_write
        def interrupted(*args):
            real_write(*args)
            raise OSError('Injected lost claim acknowledgement')
        with patch.object(jobs, 'safe_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.runner_started(*binding)
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.store.status('worker', job_id)['status'], 'running')
        with self.assertRaises(Denied):
            self.store.runner_started(*binding)
        self.assertIsNotNone(self.store.reservation())

    def test_interrupted_initialization_and_missing_state_fail_closed(self):
        self.path.unlink()
        for action in (lambda: jobs.DelegationJobs(self.root), lambda: self.start(),
                       self.store.reservation):
            with self.assertRaises(Denied):
                action()
        self.assertFalse(self.path.exists())
        other = self.root / 'other-project'
        other.mkdir()
        with patch.object(jobs, 'write_json', side_effect=OSError('Interrupted initialization')):
            with self.assertRaises(OSError):
                jobs.DelegationJobs(other)
        with self.assertRaises(Denied):
            jobs.DelegationJobs(other)

    def test_concurrent_exact_starts_converge_and_different_keys_serialize(self):
        # Separate store objects, using the same local+kernel file lock.
        other = jobs.DelegationJobs(self.root)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.start, 'worker', 'same', self.request)
                       for store in (self.store, other)]
            ids = [future.result()['id'] for future in futures]
        self.assertEqual(ids[0], ids[1])
        self.complete()
        def attempt(store, key):
            try:
                return store.start('worker', key, self.request)['id']
            except Denied:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, store, key)
                       for store, key in [(self.store, 'one'), (other, 'two')]]
            winners = [f.result() for f in futures if f.result() is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.reservation()['id'], winners[0])

    def test_invalid_requests_do_not_reserve_and_input_is_detached(self):
        invalid = [None, {}, {**self.request, 'extra': True},
                   {**self.request, 'role': '../escape'},
                   {**self.request, 'source_snapshot': 'live'},
                   {**self.request, 'profile': []}, {**self.request, 'task': '  '},
                   {**self.request, 'task': '\ud800'},
                   {**self.request, 'task': 'é' * 32769},
                   {**self.request, 'task': '\x00' * 65536}]
        for request in invalid:
            with self.subTest(request_type=type(request)), self.assertRaises(Denied):
                self.store.start('worker', 'first', request)
            self.assertIsNone(self.store.reservation())
        job_id = self.start()
        self.request['task'] = 'Changed after start'
        self.assertEqual(self.store.reservation()['request']['task'], 'Implement the focused slice')
        view = self.store.reservation()
        view['request']['task'] = 'Changed returned copy'
        self.assertEqual(self.store.reservation()['id'], job_id)
        self.assertEqual(self.store.reservation()['request']['task'], 'Implement the focused slice')

    def test_corrupt_and_oversized_state_is_never_reset(self):
        self.start()
        valid = self.path.read_bytes()
        state = loads_finite(valid)
        job_id = state['active']
        variants = [b'{', b'null', b'{"version":NaN}', b'[' * 65 + b']' * 65,
                    b' ' * (jobs.MAX_STATE_BYTES + 1),
                    valid[:-2], valid + b' ', valid.replace(b'"version":2', b'"version":2,"version":2')]
        for field, value in [('status', 'lost'), ('request_digest', '0' * 64),
                             ('token', None), ('run_id', None), ('execution', []),
                             ('request', None), ('result', 'premature'), ('owner', 'editor')]:
            bad = copy.deepcopy(state)
            bad['jobs'][job_id][field] = value
            variants.append(canonical(bad))
        for field, value in [('version', True), ('active', None), ('active', {}),
                             ('jobs', []), ('extra', True)]:
            bad = copy.deepcopy(state)
            bad[field] = value
            variants.append(canonical(bad))
        for raw in variants:
            with self.subTest(raw_prefix=raw[:60]):
                self.path.write_bytes(raw)
                for action in (lambda: jobs.DelegationJobs(self.root), lambda: self.start(),
                               self.store.reservation):
                    with self.assertRaises((Denied, LimitExceeded)):
                        action()
                self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_bytes(valid)
        self.assertEqual(self.store.reservation()['id'], job_id)

    def test_corrupt_terminal_result_and_duplicate_active_reservations(self):
        first = self.start()
        active = loads_finite(self.path.read_bytes())
        self.complete()
        terminal = loads_finite(self.path.read_bytes())
        bad = copy.deepcopy(terminal)
        bad['jobs'][first]['result'] = 'Replaced result'
        self.path.write_bytes(canonical(bad))
        with self.assertRaises(Denied):
            self.store.result('worker', first)
        self.path.write_bytes(canonical(terminal))
        second = self.start('second')
        bad = loads_finite(self.path.read_bytes())
        bad['jobs'][first] = active['jobs'][first]
        self.path.write_bytes(canonical(bad))
        with self.assertRaises(Denied):
            self.store.status('worker', second)

    def test_run_id_generated_once_and_stable_across_retry_and_reconstruction(self):
        job_id = self.start()
        first = self.store.reservation()
        run_id = first['run_id']
        self.assertIsInstance(run_id, str)
        self.assertRegex(run_id, r'\A[0-9a-f]{32}\Z')
        # Exact retry at every transition returns the same binding.
        self.store.runner_started(*self.runner())
        self.store.runner_finished(*self.runner(), 'failed')
        for _ in range(2):
            self.assertEqual(self.start(), job_id)
            self.assertEqual(self.store.reservation()['run_id'], run_id)
            self.store = jobs.DelegationJobs(self.root)
            self.assertEqual(self.store.reservation()['run_id'], run_id)
            self.assertEqual(self.store.reservation()['id'], job_id)
        self.store.runner_quiescent(*self.runner(), 'evidence')
        # Tombstone retains the identity for audit after reconstruction.
        self.store = jobs.DelegationJobs(self.root)
        state = loads_finite(self.path.read_bytes())
        self.assertEqual(state['jobs'][job_id]['run_id'], run_id)
        self.assertEqual(self.start(), job_id)
        self.assertIsNone(self.store.reservation())
        self.assertEqual(loads_finite(self.path.read_bytes())['jobs'][job_id]['run_id'], run_id)

    def test_run_id_hidden_from_owner_views_and_distinct_per_job(self):
        first = self.start()
        first_run = self.store.reservation()['run_id']
        for view in (self.store.status('worker', first),
                     self.store.start('worker', 'first', self.request)):
            self.assertNotIn('run_id', view)
            self.assertNotIn('token', view)
            self.assertNotIn('request', view)
        self.complete('First evidence')
        result_view = self.store.result('worker', first)
        self.assertNotIn('run_id', result_view)
        second = self.start('second')
        second_run = self.store.reservation()['run_id']
        self.assertRegex(second_run, r'\A[0-9a-f]{32}\Z')
        self.assertNotEqual(first_run, second_run)
        self.assertNotIn('run_id', self.store.status('worker', second))
        self.complete('Second evidence')
        self.assertNotIn('run_id', self.store.result('worker', second))
        self.assertNotIn('run_id', self.store.cancel('worker', second))
        state = loads_finite(self.path.read_bytes())
        self.assertEqual(state['jobs'][first]['run_id'], first_run)
        self.assertEqual(state['jobs'][second]['run_id'], second_run)
        self.assertLess(len(canonical(state['jobs'][first])), 600)
        self.assertLess(len(canonical(state['jobs'][second])), 600)

    def test_malformed_duplicate_and_version1_run_state_rejected_without_reset(self):
        job_id = self.start()
        valid = self.path.read_bytes()
        state = loads_finite(valid)
        malformed = [None, 0, '', '0' * 31, '0' * 33, '0' * 64,
                     'G' * 32, 'A' * 32, '0' * 32 + ' ', ' 0' * 16]
        for bad_run in malformed:
            with self.subTest(bad_run=bad_run):
                bad = copy.deepcopy(state)
                bad['jobs'][job_id]['run_id'] = bad_run
                raw = canonical(bad)
                self.path.write_bytes(raw)
                for action in (lambda: jobs.DelegationJobs(self.root), lambda: self.start(),
                               self.store.reservation):
                    with self.assertRaises((Denied, LimitExceeded)):
                        action()
                self.assertEqual(self.path.read_bytes(), raw)
        # Missing run identity is refused, never treated as empty.
        bad = copy.deepcopy(state)
        del bad['jobs'][job_id]['run_id']
        raw = canonical(bad)
        self.path.write_bytes(raw)
        with self.assertRaises(Denied):
            self.store.reservation()
        self.assertEqual(self.path.read_bytes(), raw)
        # Version 1 ledger without the run identity is refused, never migrated.
        old = copy.deepcopy(state)
        old['version'] = 1
        for job in old['jobs'].values():
            del job['run_id']
        raw = canonical(old)
        self.path.write_bytes(raw)
        for action in (lambda: jobs.DelegationJobs(self.root), lambda: self.start(),
                       self.store.reservation):
            with self.assertRaises((Denied, LimitExceeded)):
                action()
        self.assertEqual(self.path.read_bytes(), raw)
        # Duplicate run identities among retained jobs are refused.
        self.path.write_bytes(valid)
        self.complete('First done')
        second = self.start('second')
        dup = loads_finite(self.path.read_bytes())
        dup['jobs'][second]['run_id'] = dup['jobs'][job_id]['run_id']
        raw = canonical(dup)
        self.path.write_bytes(raw)
        with self.assertRaises(Denied):
            self.store.status('worker', second)
        self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_bytes(valid)
        self.assertEqual(self.store.reservation()['run_id'],
                         loads_finite(valid)['jobs'][job_id]['run_id'])

    def test_unsafe_state_paths_are_refused(self):
        original = self.path.read_bytes()
        target = self.root / 'target'
        target.write_bytes(original)
        for name in ('state.json', 'store.lock'):
            path = self.path.parent / name
            saved = path.read_bytes()
            path.unlink()
            try:
                os.link(target, path)
            except OSError:
                path.write_bytes(saved)
                continue
            with self.subTest(kind='hardlink', name=name), self.assertRaises(Denied):
                self.store.reservation()
            path.unlink()
            path.write_bytes(saved)
            path.unlink()
            try:
                path.symlink_to(target)
            except OSError:
                path.write_bytes(saved)
                continue
            with self.subTest(kind='symlink', name=name), self.assertRaises(Denied):
                self.store.reservation()
            path.unlink()
            path.write_bytes(saved)
        if hasattr(os, 'mkfifo'):
            self.path.unlink()
            os.mkfifo(self.path)
            with self.assertRaises(Denied):
                self.store.reservation()
            self.path.unlink()
            self.path.write_bytes(original)
        # Reject the dedicated directory itself if redirected to another store.
        alternate = self.root / 'alternate'
        self.path.parent.rename(alternate)
        try:
            self.path.parent.symlink_to(alternate, target_is_directory=True)
        except OSError:
            alternate.rename(self.path.parent)
        else:
            with self.assertRaises(Denied):
                jobs.DelegationJobs(self.root)
            self.path.parent.unlink()
            alternate.rename(self.path.parent)


if __name__ == '__main__':
    unittest.main()
