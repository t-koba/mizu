"""Bounded cross-process fence fixtures, not Linux/Podman settlement evidence."""
import copy
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mizu.delegation_jobs import DelegationJobs
from mizu.errors import Denied
from mizu.fs import canonical, loads_finite


def _effect(root, credentials, intent, phase, entered, resume, completed, crash=False):
    store = DelegationJobs(Path(root))

    def operation():
        entered.set()
        if crash:
            os._exit(23)  # kernel unlock is not settlement or permission to replay
        if not resume.wait(5):
            raise RuntimeError('fixture synchronization timed out')
        if phase == 'submit':
            return f"mizu-delegation-{intent['run_id']}.service"
        return 'd' * 64

    store.runner_effect(*credentials, intent, phase, operation, authority_digest='b' * 64)
    completed.set()


def _cancel(root, credentials, ready, cancelled):
    # Construction also takes store.lock; don't hide the attempted cancellation
    # behind that read. This is the same persistent store, without initialization.
    store = DelegationJobs.__new__(DelegationJobs)
    store.root = Path(root) / 'delegation-jobs'

    def systemd(config, run_id):
        # This independent store-lock acquisition would block if systemd work
        # were performed while runner_cancel_effects still held the fence lock.
        reservation = store.reservation()
        assert reservation['id'] == credentials[0]
        assert reservation['status'] == 'cancel-requested'
        cancelled.set()

    with mock.patch('mizu.delegation_systemd._cancel_stop', side_effect=systemd):
        ready.set()
        store.runner_cancel_effects(*credentials, None)


def _delayed(root, credentials, intent, phase, ready, resume, operated, denied):
    store = DelegationJobs(Path(root))
    ready.set()
    if not resume.wait(5):
        raise RuntimeError('fixture synchronization timed out')

    def operation():
        operated.set()
        if phase == 'submit':
            refreshed = store.reservation()['intent']
            return f"mizu-delegation-{refreshed['run_id']}.service"
        return 'd' * 64

    try:
        # Refresh the attempt inventory so neither a stale snapshot nor an
        # already-completed phase can mask a missing fence check.
        intent = store.reservation()['intent']
        store.runner_effect(*credentials, intent, phase, operation, authority_digest='b' * 64)
    except Denied:
        denied.set()


class DelegationFenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = DelegationJobs(self.root)
        self.job_id = self.store.start('worker', 'fixture', {
            'role': 'helper', 'task': 'bounded fixture', 'profile': 'fixture',
            'source_snapshot': 'a' * 64, 'authority_digest': 'b' * 64})['id']
        reservation = self.store.reservation()
        self.credentials = self.job_id, reservation['token']
        self.run_id = reservation['run_id']
        self.unit = f'mizu-delegation-{self.run_id}.service'
        directory = self.root / self.run_id
        directory.mkdir()
        self.intent = self.store.runner_prepare(*self.credentials, str(directory),
            {'unit': self.unit, 'cgroup': '/system.slice/' + self.unit},
            {'name': 'fixture-' + self.run_id, 'uid': 1000, 'marker': self.run_id,
             'image': 'c' * 64, 'config': {'Cmd': ['true']}})
        self.ctx = multiprocessing.get_context('spawn')

    def spawn(self, target, *args):
        process = self.ctx.Process(target=target, args=(str(self.root), self.credentials, *args))
        process.start()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)
            self.assertFalse(process.is_alive())
        self.addCleanup(cleanup)
        return process

    def joined(self, process, code=0):
        process.join(5)
        self.assertFalse(process.is_alive(), 'fixture child did not settle')
        self.assertEqual(process.exitcode, code)

    def effect(self, phase, operation, intent=None, **kwargs):
        return self.store.runner_effect(*self.credentials,
            self.store.reservation()['intent'] if intent is None else intent,
            phase, operation, authority_digest=kwargs.get('authority_digest', 'b' * 64))

    def bind_and_claim(self, create_id):
        intent = self.store.reservation()['intent']
        container = intent['container']
        binding = self.store.runner_bind(*self.credentials, intent['directory']['path'],
            intent['scope'], [{'name': container['name'], 'id': create_id, 'uid': container['uid']}],
            authority_digest='b' * 64)
        self.store.runner_started(*self.credentials, authority_digest='b' * 64)
        return binding

    def test_cancel_waits_for_admitted_effect_and_delayed_helper_is_denied(self):
        # Admitted submit holds the lock; delayed create would be the valid
        # next phase after submit completes, but the fence denies it first.
        delayed_ready, delayed_resume, operated, denied = [self.ctx.Event() for _ in range(4)]
        delayed = self.spawn(_delayed, self.intent, 'create', delayed_ready, delayed_resume, operated, denied)
        self.assertTrue(delayed_ready.wait(5))
        entered, resume, completed, ready, cancelled = [self.ctx.Event() for _ in range(5)]
        child = self.spawn(_effect, self.intent, 'submit', entered, resume, completed)
        self.assertTrue(entered.wait(5))
        # Reading the file (not acquiring store.lock) sees issued BEFORE work.
        state = loads_finite((self.store.root / 'state.json').read_bytes())
        self.assertEqual(state['jobs'][self.job_id]['intent']['attempts']['submit']['state'], 'issued')
        cancel = self.spawn(_cancel, ready, cancelled)
        self.assertTrue(ready.wait(5))
        self.assertFalse(cancelled.wait(0.2), 'cancellation overtook admitted work')
        resume.set()
        self.joined(child)
        self.joined(cancel)
        self.assertTrue(completed.is_set())
        self.assertTrue(cancelled.is_set())
        reservation = self.store.reservation()
        self.assertEqual(reservation['intent']['attempts']['submit'], {'state': 'completed', 'result': self.unit})
        self.assertEqual(reservation['status'], 'cancel-requested')

        # A separate delayed helper refreshed after admission, but the durable
        # fence must deny the would-be valid create before its callback.
        delayed_resume.set()
        self.joined(delayed)
        self.assertTrue(denied.is_set())
        self.assertFalse(operated.is_set())
        self.assertIsNotNone(self.store.reservation())

    def test_death_after_issued_reconstruction_never_replays_or_releases(self):
        self.assertEqual(self.effect('submit', lambda: self.unit), self.unit)
        intent = self.store.reservation()['intent']
        entered, resume, completed = [self.ctx.Event() for _ in range(3)]
        child = self.spawn(_effect, intent, 'create', entered, resume, completed, True)
        self.assertTrue(entered.wait(5))
        self.joined(child, 23)
        self.store = DelegationJobs(self.root)
        self.assertEqual(self.store.reservation()['intent']['attempts']['create'],
                         {'state': 'issued', 'result': None})
        operation = mock.Mock(return_value='e' * 64)
        for phase in ('submit', 'create', 'start'):
            with self.assertRaises(Denied):
                self.effect(phase, operation)
        operation.assert_not_called()
        self.store.cancel('worker', self.job_id)
        self.store.runner_recover_prepared(*self.credentials, container_id='d' * 64)
        intent = self.store.reservation()['intent']
        with self.assertRaisesRegex(Denied, 'handback unsupported'):
            self.store.runner_prepared_quiescent(*self.credentials, 'not proof', cleanup={
                'intent': intent, 'scope_unstarted': True, 'container_absent': 'd' * 64})
        self.assertIsNotNone(DelegationJobs(self.root).reservation())

    def test_results_exact_intent_authority_execution_and_no_retry(self):
        operation = mock.Mock(return_value=self.unit)
        wrong = copy.deepcopy(self.intent)
        wrong['container']['image'] = 'e' * 64
        for intent, authority in ((wrong, 'b' * 64), (self.intent, 'e' * 64)):
            with self.assertRaises(Denied):
                self.effect('submit', operation, intent, authority_digest=authority)
        operation.assert_not_called()
        self.assertEqual(self.effect('submit', operation), self.unit)
        with self.assertRaises(Denied):
            self.effect('submit', operation)
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.effect('create', lambda: 'd' * 64), 'd' * 64)
        self.bind_and_claim('d' * 64)
        self.assertEqual(self.effect('start', lambda: 'd' * 64), 'd' * 64)
        with self.assertRaises(Denied):
            self.effect('start', lambda: 'e' * 64)
        self.store.runner_finished(*self.credentials, 'failed')
        operation.reset_mock()
        with self.assertRaises(Denied):
            self.effect('submit', operation)
        operation.assert_not_called()
        self.assertIsNotNone(self.store.reservation())

    def test_out_of_order_create_and_start_denied_before_operation(self):
        operation = mock.Mock(return_value='d' * 64)
        submit_op = mock.Mock(return_value=self.unit)
        # Create before submit and start before submit/create are out of order.
        with self.assertRaises(Denied):
            self.effect('create', operation)
        with self.assertRaises(Denied):
            self.effect('start', operation)
        operation.assert_not_called()
        self.assertEqual(self.effect('submit', submit_op), self.unit)
        submit_op.assert_called_once()
        # Start before create is still out of order even with a valid submit.
        with self.assertRaises(Denied):
            self.effect('start', operation)
        operation.assert_not_called()
        self.assertEqual(self.effect('create', operation), 'd' * 64)
        self.assertEqual(operation.call_count, 1)
        # Start remains unclaimed until bind/claim; still denied before callback.
        with self.assertRaises(Denied):
            self.effect('start', operation)
        self.assertEqual(operation.call_count, 1)
        self.assertIsNotNone(self.store.reservation())

    def test_unclaimed_start_and_binding_id_mismatch_denied(self):
        self.assertEqual(self.effect('submit', lambda: self.unit), self.unit)
        self.assertEqual(self.effect('create', lambda: 'd' * 64), 'd' * 64)
        operation = mock.Mock(return_value='d' * 64)
        # Unclaimed start while still queued/unbound is denied before callback.
        with self.assertRaises(Denied):
            self.effect('start', operation)
        operation.assert_not_called()
        # Bind with a mismatched ID, then claim: start still mismatches create.
        intent = self.store.reservation()['intent']
        container = intent['container']
        self.store.runner_bind(*self.credentials, intent['directory']['path'], intent['scope'],
            [{'name': container['name'], 'id': 'e' * 64, 'uid': container['uid']}],
            authority_digest='b' * 64)
        self.store.runner_started(*self.credentials, authority_digest='b' * 64)
        with self.assertRaises(Denied):
            self.effect('start', operation)
        operation.assert_not_called()
        self.assertIsNotNone(self.store.reservation())

    def test_terminal_execution_denies_all_unissued_effects(self):
        self.store.runner_finished(*self.credentials, 'failed')
        operation = mock.Mock(return_value='d' * 64)
        for phase in ('submit', 'create', 'start'):
            with self.assertRaises(Denied):
                self.effect(phase, operation)
        operation.assert_not_called()
        self.assertEqual(self.store.reservation()['intent'], self.intent)

    def test_invalid_result_leaves_issued_and_v4_refuses_without_migration(self):
        self.assertEqual(self.effect('submit', lambda: self.unit), self.unit)
        with self.assertRaises(Denied):
            self.effect('create', lambda: 'short-id')
        self.assertEqual(self.store.reservation()['intent']['attempts']['create']['state'], 'issued')
        path = self.store.root / 'state.json'
        state = loads_finite(path.read_bytes())
        state['version'] = 4
        path.write_bytes(canonical(state))
        with self.assertRaises(Denied):
            DelegationJobs(self.root)

    def test_systemd_error_retains_durable_fence_and_reservation(self):
        with mock.patch('mizu.delegation_systemd._cancel_stop', side_effect=Denied('unknown')):
            with self.assertRaises(Denied):
                self.store.runner_cancel_effects(*self.credentials, None)
        self.store = DelegationJobs(self.root)
        self.assertEqual(self.store.reservation()['status'], 'cancel-requested')
        operation = mock.Mock()
        for phase in ('submit', 'create', 'start'):
            with self.assertRaises(Denied):
                self.effect(phase, operation)
        operation.assert_not_called()

    def test_cancel_effects_refuses_terminal_without_mutation(self):
        self.assertEqual(self.effect('submit', lambda: self.unit), self.unit)
        self.assertEqual(self.effect('create', lambda: 'd' * 64), 'd' * 64)
        self.bind_and_claim('d' * 64)
        self.assertEqual(self.effect('start', lambda: 'd' * 64), 'd' * 64)
        self.store.runner_finished(*self.credentials, 'succeeded')
        from tests.support import seed_terminal_delegation_fixture
        seed_terminal_delegation_fixture(self.store, *self.credentials, 'done')
        before = (self.store.root / 'state.json').read_bytes()
        with self.assertRaises(Denied):
            self.store.runner_cancel_effects(*self.credentials, None)
        self.assertEqual((self.store.root / 'state.json').read_bytes(), before)
