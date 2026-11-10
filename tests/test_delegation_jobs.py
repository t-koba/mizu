"""Private delegation-state tests; no runtime, process or supervisor smoke claims.

Covers stable run identities and supervisor-only execution bindings: durable
retry/reconstruction, owner-view hiding, tombstone retention, exact directory
identity, refusal of caller cleanup assertions, and fail-closed invalid/older state."""
import concurrent.futures
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import seed_terminal_delegation_fixture
from mizu import delegation_jobs as jobs
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest, loads_finite


class DelegationJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = jobs.DelegationJobs(self.root)
        self.path = self.root / 'delegation-jobs' / 'state.json'
        self.request = {'role': 'helper', 'task': 'Implement the focused slice',
                        'profile': 'fixture', 'source_snapshot': 'a' * 64, 'authority_digest': 'b' * 64}

    def start(self, key='first', owner='worker'):
        return self.store.start(owner, key, self.request)['id']

    def scope(self):
        if not hasattr(os, 'getuid'):
            self.skipTest('User-manager binding fixture requires a POSIX uid')
        unit = 'mizu-delegation-' + self.store.reservation()['run_id'] + '.service'
        return {'unit': unit, 'cgroup': jobs.user_cgroup(unit)}

    def runner(self):
        reservation = self.store.reservation()
        if reservation['binding'] is None:
            directory = self.root / reservation['run_id']
            directory.mkdir(exist_ok=True)
            self.store.runner_bind(reservation['id'], reservation['token'], str(directory),
                                   self.scope(), [], authority_digest='b' * 64)
        return reservation['id'], reservation['token']

    def cleanup(self):
        binding = self.store.reservation()['binding']
        return {'binding': binding, 'scope_clean': True,
                'containers_absent': [c['id'] for c in binding['containers']]}

    def assert_tombstone(self, tombstone):
        # Seeded terminal state no longer references the binding; retain only the
        # bounded idempotency and execution-outcome tombstone.
        self.assertIsNone(tombstone['binding'])
        self.assertIsNone(tombstone['intent'])
        self.assertIsNone(tombstone['request'])
        self.assertLess(len(canonical(tombstone)), 600)
        self.assertEqual(jobs._INTENT_NULL_OVERHEAD, 14)
        self.assertEqual(jobs.MAX_STATE_BYTES,
                         1024 * 1024 + jobs.MAX_BINDING_BYTES + 14 * jobs.MAX_JOBS + 86)

    def complete(self, text='Retained child evidence'):
        job_id, token = self.runner()
        self.store.runner_started(job_id, token, authority_digest='b' * 64)
        self.store.runner_finished(job_id, token, 'succeeded')
        seed_terminal_delegation_fixture(self.store, job_id, token, text)

    def test_exact_and_conflicting_replay_across_all_transitions(self):
        job_id = self.start()
        original = self.store.reservation()
        for transition in ('queued', 'running', 'cancel-requested', 'finished', 'terminal'):
            with self.subTest(transition=transition):
                if transition == 'running':
                    self.store.runner_started(*self.runner(), authority_digest='b' * 64)
                elif transition == 'cancel-requested':
                    self.store.cancel('worker', job_id)
                elif transition == 'finished':
                    self.store.runner_finished(*self.runner(), 'cancelled')
                elif transition == 'terminal':
                    seed_terminal_delegation_fixture(self.store, *self.runner(), 'Seeded terminal state')
                self.assertEqual(self.start(), job_id)
                for field, value in [('role', 'other'), ('task', 'Different task'),
                                     ('profile', 'fast'), ('source_snapshot', 'b' * 64),
                                     ('authority_digest', 'c' * 64)]:
                    with self.subTest(field=field), self.assertRaises(Denied):
                        self.store.start('worker', 'first', {**self.request, field: value})
        self.assertEqual(self.store.result('worker', job_id)['result'], 'Seeded terminal state')
        self.assertIsNone(self.store.reservation())
        self.assertNotIn('token', self.store.status('worker', job_id))
        self.assertNotIn('request', self.store.start('worker', 'first', self.request))
        self.assertEqual(original['request'], self.request)

    def test_bind_and_claim_require_current_matching_authority(self):
        self.start()
        reservation = self.store.reservation()
        credentials = reservation['id'], reservation['token']
        directory = self.root / reservation['run_id']
        directory.mkdir()
        scope = self.scope()
        for bound in (False, True):
            if bound:
                self.store.runner_bind(*credentials, str(directory), scope, [], authority_digest='b' * 64)
            before = self.path.read_bytes()
            for value in (None, '', 'c' * 64):
                for method, args in ((self.store.runner_bind, (str(directory), scope, [])),
                                     (self.store.runner_started, ())):
                    with self.subTest(bound=bound, value=value), self.assertRaises(Denied):
                        method(*credentials, *args, authority_digest=value)
                    self.assertEqual(before, self.path.read_bytes())
        self.store.runner_started(*credentials, authority_digest='b' * 64)

    def test_legacy_unbound_record_stays_held_and_cannot_gain_launch_binding(self):
        request = {key: value for key, value in self.request.items() if key != 'authority_digest'}
        request['profile'] = None
        self.store.start('worker', 'legacy', request)
        before = self.path.read_bytes()
        store = jobs.DelegationJobs(self.root)
        saved = store.reservation()
        self.assertEqual(saved['request'], request)
        with self.assertRaises(Denied):
            store.runner_bind(saved['id'], saved['token'], str(self.root),
                              self.scope(), [],
                              authority_digest='b' * 64)
        with self.assertRaises(Denied):
            jobs.DelegationJobs.guard(self.root)
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_bound_record_stays_held_and_cannot_launch(self):
        self.start()
        credentials = self.runner()
        cleanup = self.cleanup()
        # Exact legacy-shaped v5 record, including already saved resource
        # identities. Reading never rewrites it or turns it into launch authority.
        state = loads_finite(self.path.read_bytes())
        job = state['jobs'][credentials[0]]
        del job['request']['authority_digest']
        job['request']['profile'] = None
        legacy_request = copy.deepcopy(job['request'])
        job['request_digest'] = digest(canonical(legacy_request))
        self.path.write_bytes(canonical(state))
        before = self.path.read_bytes()
        store = jobs.DelegationJobs(self.root)
        self.assertEqual(store.reservation()['request'], legacy_request)
        self.assertEqual(store.start('worker', 'first', legacy_request)['id'], credentials[0])
        binding = cleanup['binding']
        for value in (None, 'b' * 64):
            with self.assertRaises(Denied):
                store.runner_bind(*credentials, binding['directory']['path'], binding['scope'], [],
                                  authority_digest=value)
            with self.assertRaises(Denied):
                store.runner_started(*credentials, authority_digest=value)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(Denied):
            store.start('worker', 'first', self.request)
        store.cancel('worker', credentials[0])
        store.runner_finished(*credentials, 'cancelled')
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            store.runner_quiescent(*credentials, 'Claimed cleanup', cleanup=cleanup)
        self.assertIsNotNone(store.reservation())

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
            self.store.runner_started(*binding, authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*binding, 'Not terminal yet', cleanup=self.cleanup())
        self.store.runner_finished(*binding, 'cancelled')
        self.assertTrue(self.store.status('worker', job_id)['reserved'])
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.runner(), binding)
        with self.assertRaises(Denied):
            self.start('second')
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(*binding, 'No child launched; claimed cleanup', cleanup=self.cleanup())
        self.assertTrue(self.store.status('worker', job_id)['reserved'])
        with self.assertRaises(Denied):
            self.start('second')

    def test_runner_failure_requires_explicit_quiescence_and_cannot_replay(self):
        job_id = self.start()
        binding = self.runner()
        self.store.runner_started(*binding, authority_digest='b' * 64)
        for _ in range(2):
            self.store = jobs.DelegationJobs(self.root)
            self.assertEqual(self.store.reservation()['status'], 'running')
            self.assertEqual(self.start(), job_id)
            with self.assertRaises(Denied):
                self.store.runner_started(*binding, authority_digest='b' * 64)
            with self.assertRaises(Denied):
                self.start('second')
        self.store.runner_finished(*binding, 'failed')
        self.store.runner_finished(*binding, 'failed')  # report replay is harmless
        with self.assertRaises(Denied):
            self.store.runner_finished(*binding, 'succeeded')
        # Terminal execution is not release: neither the same job nor a new
        # job may acquire launch authority while scope reconciliation is pending.
        with self.assertRaises(Denied):
            self.store.runner_started(*binding, authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.start('second')
        self.store.cancel('worker', job_id)
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.store.reservation()['execution'], 'failed')
        with self.assertRaises(Denied):
            self.store.result('worker', job_id)
        self.assertTrue(self.store.status('worker', job_id)['reserved'])
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(*binding, 'Claimed cleanup', cleanup=self.cleanup())
        self.assertIsNotNone(self.store.reservation())
        with self.assertRaises(Denied):
            self.store.result('worker', job_id)
        with self.assertRaises(Denied):
            self.start('second')

    def test_runner_binding_checks_and_queued_failure(self):
        self.start()
        job_id, token = self.runner()
        before = self.path.read_bytes()
        for binding in [(job_id, '0' * 64), ('b' * 64, token), (job_id, '../unsafe')]:
            for method, args in [(self.store.runner_started, ()),
                                 (self.store.runner_binding, ()),
                                 (self.store.runner_finished, ('failed',)),
                                 (self.store.runner_quiescent, ('evidence',))]:
                with self.subTest(method=method.__name__, binding=binding), self.assertRaises(Denied):
                    method(*binding, *args)
        with self.assertRaises(Denied):
            self.store.runner_finished(job_id, token, 'succeeded')
        with self.assertRaises(Denied):
            self.store.runner_bind(job_id, '0' * 64, str(self.root),
                                   {'unit': 'other.service', 'cgroup': '/system.slice/other.service'}, [], authority_digest='b' * 64)
        self.assertEqual(before, self.path.read_bytes())
        self.store.runner_finished(job_id, token, 'failed')
        self.assertIsNotNone(self.store.reservation())
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(job_id, token, 'Launch failed; teardown verified', cleanup=self.cleanup())
        self.assertIsNotNone(self.store.reservation())

    def test_result_bound_and_eviction_leave_idempotency_tombstones(self):
        first = self.start()
        binding = self.runner()
        self.store.runner_started(*binding, authority_digest='b' * 64)
        self.store.runner_finished(*binding, 'succeeded')
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*binding, 'é' * (jobs.MAX_RESULT_BYTES // 2 + 1), cleanup=self.cleanup())
        self.assertIsNotNone(self.store.reservation())
        result = 'é' * (jobs.MAX_RESULT_BYTES // 2)
        seed_terminal_delegation_fixture(self.store, *binding, result)
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
        self.assert_tombstone(tombstone)

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
                self.store.runner_started(*binding, authority_digest='b' * 64)
        self.store = jobs.DelegationJobs(self.root)
        self.assertEqual(self.store.status('worker', job_id)['status'], 'running')
        with self.assertRaises(Denied):
            self.store.runner_started(*binding, authority_digest='b' * 64)
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
                   {**self.request, 'authority_digest': None},
                   {**self.request, 'authority_digest': 'B' * 64},
                   {**self.request, 'profile': None},
                   {**self.request, 'profile': []}, {**self.request, 'task': '  '},
                   {**self.request, 'task': '\ud800'},
                   {**self.request, 'task': 'é' * (jobs.MAX_TASK_BYTES // 2 + 1)},
                   {**self.request, 'task': '\x00' * jobs.MAX_TASK_BYTES}]
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
                    valid[:-2], valid + b' ', valid.replace(b'"version":5', b'"version":5,"version":5')]
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
        self.store.runner_started(*self.runner(), authority_digest='b' * 64)
        self.store.runner_finished(*self.runner(), 'failed')
        for _ in range(2):
            self.assertEqual(self.start(), job_id)
            self.assertEqual(self.store.reservation()['run_id'], run_id)
            self.store = jobs.DelegationJobs(self.root)
            self.assertEqual(self.store.reservation()['run_id'], run_id)
            self.assertEqual(self.store.reservation()['id'], job_id)
        seed_terminal_delegation_fixture(self.store, *self.runner(), 'evidence')
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
            self.assertNotIn('binding', view)
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
        self.assert_tombstone(state['jobs'][first])
        self.assert_tombstone(state['jobs'][second])

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

    def test_binding_durability_conflicts_and_uncertain_acknowledgement(self):
        self.start()
        reservation = self.store.reservation()
        credentials = reservation['id'], reservation['token']
        directory = self.root / 'actual-child-run'
        directory.mkdir()
        scope = self.scope()
        containers = [{'name': 'fixture', 'id': 'c' * 64, 'uid': 1000}]
        real_write = jobs.safe_write
        for committed in (False, True):
            def interrupted(*args):
                if committed:
                    real_write(*args)
                raise OSError('Lost binding acknowledgement')
            with patch.object(jobs, 'safe_write', side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.store.runner_bind(*credentials, str(directory), scope, containers, authority_digest='b' * 64)
            self.store = jobs.DelegationJobs(self.root)
            self.assertEqual(self.store.reservation()['binding'] is not None, committed)
        original = self.store.runner_bind(*credentials, str(directory), scope, containers, authority_digest='b' * 64)
        self.assertEqual(original['run_id'], reservation['run_id'])
        self.assertEqual(original['directory']['path'], str(directory))
        self.assertEqual(original['directory']['inode'], directory.stat().st_ino)
        before = self.path.read_bytes()
        for other_scope, other_containers in [
                ({'unit': 'other.service', 'cgroup': '/system.slice/other.service'}, containers),
                (scope, [{**containers[0], 'id': 'd' * 64}]),
                (scope, [{**containers[0], 'uid': 1001}]), (scope, [])]:
            with self.assertRaises(Denied):
                self.store.runner_bind(*credentials, str(directory), other_scope, other_containers, authority_digest='b' * 64)
            self.assertEqual(self.path.read_bytes(), before)
        original['containers'].clear()
        containers[0]['name'] = 'changed'
        self.assertEqual(self.store.reservation()['binding']['containers'][0]['name'], 'fixture')
        self.store.runner_started(*credentials, authority_digest='b' * 64)
        saved = self.store.reservation()['binding']
        self.assertEqual(self.store.runner_bind(*credentials, str(directory), scope,
                                               saved['containers'], authority_digest='b' * 64), saved)
        with self.assertRaises(Denied):
            self.store.runner_started(*credentials, authority_digest='b' * 64)

    def test_cancel_before_binding_cannot_infer_safe_handback(self):
        job_id = self.start()
        reservation = self.store.reservation()
        credentials = reservation['id'], reservation['token']
        self.assertEqual(self.store.cancel('worker', job_id)['status'], 'cancel-requested')
        directory = self.root / reservation['run_id']
        directory.mkdir()
        scope = self.scope()
        binding = self.store.runner_bind(*credentials, str(directory), scope, [], authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.store.runner_started(*credentials, authority_digest='b' * 64)
        self.store.runner_finished(*credentials, 'cancelled')
        cleanup = {'binding': binding, 'scope_clean': True, 'containers_absent': []}
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(*credentials, 'Claimed no launch', cleanup=cleanup)
        self.assertIsNotNone(self.store.reservation())

    def test_missing_binding_and_incomplete_or_mismatched_proofs_keep_reservation(self):
        self.start()
        reservation = self.store.reservation()
        credentials = reservation['id'], reservation['token']
        with self.assertRaises(Denied):
            self.store.runner_started(*credentials, authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*credentials, 'Unbound', cleanup={})
        directory = self.root / 'child'
        directory.mkdir()
        scope = self.scope()
        containers = [{'name': 'one', 'id': 'c' * 64, 'uid': 1000},
                      {'name': 'two', 'id': 'd' * 64, 'uid': 1000}]
        self.store.runner_bind(*credentials, str(directory), scope, containers, authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*credentials, 'Not terminal', cleanup=self.cleanup())
        self.store.runner_finished(*credentials, 'failed')
        proof = self.cleanup()
        variants = [proof, None, {}, {**proof, 'scope_clean': 1}, {**proof, 'scope_clean': None},
                    {**proof, 'containers_absent': ['c' * 64]},
                    {**proof, 'containers_absent': ['c' * 64, 'c' * 64]},
                    {**proof, 'extra': True}]
        bad = copy.deepcopy(proof)
        bad['binding']['run_id'] = '0' * 32
        variants.append(bad)
        bad = copy.deepcopy(proof)
        bad['binding']['containers'][0]['uid'] = 1000.0
        variants.append(bad)
        before = self.path.read_bytes()
        for evidence in variants:
            with self.subTest(evidence=evidence), self.assertRaises(Denied):
                self.store.runner_quiescent(*credentials, 'Evidence', cleanup=evidence)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIsNotNone(jobs.DelegationJobs.existing_reservation(self.root))
        seed_terminal_delegation_fixture(self.store, *credentials, 'Seed next fixture')
        # Terminal execution without a binding is still held, even if a
        # supervisor supplies an apparently positive but unrelated proof.
        self.start('unbound')
        reservation = self.store.reservation()
        credentials = reservation['id'], reservation['token']
        self.store.runner_finished(*credentials, 'failed')
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*credentials, 'No binding', cleanup=proof)
        with self.assertRaises(Denied):
            self.store.runner_bind(*credentials, str(directory), scope, containers, authority_digest='b' * 64)
        self.assertIsNotNone(self.store.reservation())

    def test_binding_directory_missing_replaced_renamed_or_unobservable_keeps_hold(self):
        self.start()
        credentials = self.runner()
        binding = self.store.reservation()['binding']
        directory = Path(binding['directory']['path'])
        self.store.runner_finished(*credentials, 'failed')
        proof = self.cleanup()
        moved = directory.with_name('moved')
        directory.rename(moved)
        with self.assertRaises(OSError):
            self.store.runner_quiescent(*credentials, 'Missing', cleanup=proof)
        directory.mkdir()
        with self.assertRaises(Denied):
            self.store.runner_quiescent(*credentials, 'Replaced', cleanup=proof)
        with patch.object(jobs, '_directory_identity', side_effect=PermissionError('Unknown')):
            with self.assertRaises(OSError):
                self.store.runner_quiescent(*credentials, 'Unobservable', cleanup=proof)
        self.assertIsNotNone(self.store.reservation())
        directory.rmdir()
        moved.rename(directory)
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(*credentials, 'Restored exact directory', cleanup=proof)
        self.assertIsNotNone(self.store.reservation())

    def test_malformed_ambiguous_oversized_bindings_and_corruption_fail_closed(self):
        self.start()
        credentials = self.runner()
        valid = self.path.read_bytes()
        binding = self.store.reservation()['binding']
        container = {'name': 'fixture', 'id': 'c' * 64, 'uid': 1000}
        variants = [None, {}, {**binding, 'run_id': '0' * 32},
                    {**binding, 'containers': [container, container]},
                    {**binding, 'containers': [container, {**container, 'name': 'other'}]},
                    {**binding, 'containers': [{**container, 'id': 'short'}]},
                    {**binding, 'containers': [{**container, 'uid': True}]},
                    {**binding, 'scope': {**binding['scope'], 'cgroup': '/wrong'}},
                    {**binding, 'containers': [{**container, 'name': 'x' * jobs.MAX_BINDING_BYTES}]}]
        state = loads_finite(valid)
        for malformed in variants[1:]:
            bad = copy.deepcopy(state)
            bad['jobs'][credentials[0]]['binding'] = malformed
            raw = canonical(bad)
            self.path.write_bytes(raw)
            with self.assertRaises(Denied):
                jobs.DelegationJobs.existing_reservation(self.root)
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_bytes(valid)
        # The prior schema must not silently become an empty/unbound store.
        prior = copy.deepcopy(state)
        prior['version'] = 2
        del prior['jobs'][credentials[0]]['binding']
        self.path.write_bytes(canonical(prior))
        with self.assertRaises(Denied):
            jobs.DelegationJobs(self.root)
        self.path.write_bytes(valid)
        # A missing mandatory schema field is not an unbound job.
        del state['jobs'][credentials[0]]['binding']
        self.path.write_bytes(canonical(state))
        with self.assertRaises(Denied):
            self.store.reservation()
        self.path.write_bytes(valid)
        alias = self.root / 'alias'
        try:
            alias.symlink_to(binding['directory']['path'], target_is_directory=True)
        except OSError:
            return  # Symlink creation is unavailable on some Windows workers.
        with self.assertRaises(Denied):
            self.store.runner_bind(*credentials, str(alias), binding['scope'], [], authority_digest='b' * 64)

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

    def prepare(self):
        job_id = self.start()
        reservation = self.store.reservation()
        directory = self.root / reservation['run_id']
        directory.mkdir()
        scope = self.scope()
        container = {'name': 'mizu-recovery-' + reservation['run_id'], 'uid': 1000,
                     'marker': reservation['run_id'], 'image': 'b' * 64,
                     'config': {'Cmd': ['/bin/sleep', '90']}}
        credentials = job_id, reservation['token']
        intent = self.store.runner_prepare(*credentials, str(directory), scope, container)
        return credentials, intent

    def test_preparation_is_durable_bounded_private_and_exactly_retryable(self):
        credentials, intent = self.prepare()
        def retry(container):
            return self.store.runner_prepare(*credentials, intent['directory']['path'],
                                             intent['scope'], container)
        self.assertEqual(retry(intent['container']), intent)
        self.assertEqual(jobs.DelegationJobs(self.root).reservation()['intent'], intent)
        self.assertNotIn('intent', self.store.status('worker', credentials[0]))
        for key, value in [('name', '../evil'), ('name', ''), ('marker', 'c' * 32), ('uid', True),
                           ('image', 'tag'), ('config', {}),
                           ('config', {'large': 'x' * jobs.MAX_BINDING_BYTES})]:
            with self.subTest(key=key), self.assertRaises(Denied):
                retry({**intent['container'], key: value})
        with self.assertRaises(Denied):
            self.store.runner_started(*credentials, authority_digest='b' * 64)
        # A schema-v3 ledger is not silently upgraded or reset.
        raw = self.path.read_bytes()
        self.path.write_bytes(raw.replace(b'"version":5', b'"version":3'))
        with self.assertRaises(Denied):
            self.store.reservation()
        self.path.write_bytes(raw)

    def test_prepared_recovery_fences_bind_claim_and_requires_exact_cleanup(self):
        credentials, intent = self.prepare()
        container = intent['container']
        saved = [{'name': container['name'], 'id': 'd' * 64, 'uid': container['uid']}]
        def bind():
            return self.store.runner_bind(*credentials, intent['directory']['path'], intent['scope'], saved, authority_digest='b' * 64)
        fenced = self.store.runner_recover_prepared(*credentials)
        self.assertTrue(fenced['recovering'])
        self.assertFalse(fenced['claimed'])
        self.assertEqual(self.store.runner_recover_prepared(*credentials), fenced)
        for operation in (bind, lambda: self.store.runner_started(*credentials, authority_digest='b' * 64),
                          lambda: self.store.runner_finished(*credentials, 'failed'),
                          lambda: self.store.runner_prepared_quiescent(*credentials, 'clean', cleanup={})):
            with self.assertRaises(Denied):
                operation()
        fenced = self.store.runner_recover_prepared(*credentials, container_id='d' * 64)
        proof = {'intent': fenced, 'scope_unstarted': True, 'container_absent': 'd' * 64}
        for wrong in ({}, {**proof, 'scope_unstarted': 1}, {**proof, 'container_absent': 'e' * 64},
                      {**proof, 'intent': intent},
                      {**proof, 'intent': {**fenced, 'claimed': 0}}):
            with self.assertRaises(Denied):
                self.store.runner_prepared_quiescent(*credentials, 'clean', cleanup=wrong)
        with self.assertRaises(Denied):
            self.store.runner_recover_prepared(*credentials, container_id='e' * 64)
        # Even exact fabricated cleanup assertions cannot authorize product handback.
        before = self.path.read_bytes()
        saved_reservation = self.store.reservation()
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_prepared_quiescent(*credentials, 'clean', cleanup=proof)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.reservation(), saved_reservation)
        self.assertIsNotNone(self.store.reservation())
        # The durable fence remains: bind/claim/finish still refuse without clearing.
        for operation in (bind, lambda: self.store.runner_started(*credentials, authority_digest='b' * 64),
                          lambda: self.store.runner_finished(*credentials, 'failed')):
            with self.assertRaises(Denied):
                operation()
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(hasattr(os, 'getuid'), 'User-manager binding requires a POSIX uid')
    def test_intent_accepts_any_bounded_safe_name_but_pins_marker(self):
        # Names are persisted in the intent, not reconstructed during recovery;
        # the marker remains bound to the stable run identity.
        base = {'uid': 1000, 'image': 'b' * 64, 'config': {'Cmd': ['/bin/sleep', '90']}}
        for name in ('custom-safe.name_1', 'fixture-0', 'a'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                store = jobs.DelegationJobs(root)
                store.start('worker', 'k-' + name.replace('.', '-'), self.request)
                reservation = store.reservation()
                directory = root / reservation['run_id']
                directory.mkdir()
                unit = 'mizu-delegation-' + reservation['run_id'] + '.service'
                scope = {'unit': unit, 'cgroup': jobs.user_cgroup(unit)}
                container = {**base, 'name': name, 'marker': reservation['run_id']}
                intent = store.runner_prepare(reservation['id'], reservation['token'],
                                              str(directory), scope, container)
                self.assertEqual(intent['container'], container)
                self.assertEqual(store.runner_prepare(
                    reservation['id'], reservation['token'], str(directory), scope, container),
                    intent)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = jobs.DelegationJobs(root)
            store.start('worker', 'unsafe', self.request)
            reservation = store.reservation()
            directory = root / reservation['run_id']
            directory.mkdir()
            unit = 'mizu-delegation-' + reservation['run_id'] + '.service'
            scope = {'unit': unit, 'cgroup': jobs.user_cgroup(unit)}
            for bad in ('', '../evil', '/abs', 'a/b', 'a:b', 'x' * 9000):
                with self.subTest(bad=bad), self.assertRaises(Denied):
                    store.runner_prepare(
                        reservation['id'], reservation['token'], str(directory), scope,
                        {**base, 'name': bad, 'marker': reservation['run_id']})
            with self.assertRaises(Denied):
                store.runner_prepare(
                    reservation['id'], reservation['token'], str(directory), scope,
                    {**base, 'name': 'valid-name', 'marker': 'c' * 32})

    def test_authority_digest_overhead_preserves_active_ledger_capacity(self):
        self.start()
        bound = loads_finite(self.path.read_text())
        unbound = copy.deepcopy(bound)
        del unbound['jobs'][unbound['active']]['request']['authority_digest']
        delta = len(canonical(bound)) - len(canonical(unbound))
        self.assertEqual(delta, 86)
        self.assertEqual(jobs._AUTHORITY_DIGEST_OVERHEAD, delta)
        # Exactly one active allowance, not one per retained tombstone.
        self.assertEqual(jobs.MAX_STATE_BYTES,
                         1024 * 1024 + jobs.MAX_BINDING_BYTES
                         + jobs._INTENT_NULL_OVERHEAD * jobs.MAX_JOBS + delta)
        self.complete()
        self.assert_tombstone(loads_finite(self.path.read_text())['jobs'][bound['active']])

    def test_intent_null_overhead_preserves_tombstone_capacity(self):
        # Schema v5 retains one explicit intent field in every job. Terminal
        # tombstones carry `"intent":null` (14 bytes: comma + `"intent":null`),
        # so the ledger ceiling adds exactly that per-record overhead to keep
        # the prior 1024-tombstone capacity.
        self.assertEqual(jobs._INTENT_NULL_OVERHEAD, len(b',\"intent\":null'))
        self.assertEqual(jobs.MAX_STATE_BYTES,
                         1024 * 1024 + jobs.MAX_BINDING_BYTES + 14 * jobs.MAX_JOBS + 86)
        base = {'owner': 'w', 'key': 'k', 'request_digest': 'a' * 64, 'status': 'terminal',
                'token': None, 'run_id': 'b' * 32, 'execution': 'failed', 'request': None,
                'result': None, 'result_digest': 'c' * 64, 'binding': None}
        from mizu.fs import canonical as _canon
        without = dict(base)
        with_intent = dict(base, intent=None)
        self.assertEqual(len(_canon(with_intent)) - len(_canon(without)), 14)
        # A full tombstone stays well under the per-record budget and the new
        # ceiling still admits 1024 minimal tombstones plus the active allowance.
        self.assertLess(len(_canon(with_intent)), 600)
        self.assertLessEqual(1024 * len(_canon(with_intent)) + jobs.MAX_BINDING_BYTES,
                             jobs.MAX_STATE_BYTES)

    def test_claimed_then_cancelled_preparation_can_never_use_prelaunch_proof(self):
        credentials, intent = self.prepare()
        container = intent['container']
        saved = [{'name': container['name'], 'id': 'd' * 64, 'uid': container['uid']}]
        self.store.runner_bind(*credentials, intent['directory']['path'], intent['scope'], saved, authority_digest='b' * 64)
        self.store.runner_started(*credentials, authority_digest='b' * 64)
        self.store.cancel('worker', credentials[0])
        self.assertTrue(self.store.reservation()['intent']['claimed'])
        with self.assertRaises(Denied):
            self.store.runner_recover_prepared(*credentials)
        self.assertIsNotNone(self.store.reservation())
        self.store.runner_finished(*credentials, 'cancelled')
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_quiescent(*credentials, 'Claimed teardown', cleanup=self.cleanup())
        self.assertIsNotNone(self.store.reservation())


if __name__ == '__main__':
    unittest.main()
