import dataclasses
import os
import re
import threading
import time
from pathlib import Path
from unittest.mock import patch
from support import Fixture, ScriptDriver
from mizu import check_jobs
from mizu.admission import check_slot_exclusion
from mizu.errors import Busy, Denied
from mizu.fs import held, lock, read_json, write_json
from mizu.process import Result
from mizu.sandbox import cleanup

class FakeSandbox:
    calls = []
    def __init__(self, config, project, role, directory, **kwargs):
        self.role = role
    def execute(self, workspace, script, **kwargs):
        self.calls.append((workspace, script, self.role))
        return {'id': 'a' * 32, 'exit_code': 0, 'reason': 'exited', 'script': script}

class JobTests(Fixture):
    def setUp(self):
        super().setUp()
        FakeSandbox.calls = []
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job")) for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
    def submit(self):
        ctx = self.context()
        return ctx, check_jobs.submit(ctx)
    def run_job(self):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)
    def test_submission_does_not_execute_and_freezes_source(self):
        ctx, job = self.submit()
        self.assertEqual(job['status'], 'queued')
        self.assertFalse(FakeSandbox.calls)
        (ctx.workspace / 'app.py').write_text('VALUE = 3\n')
        self.assertEqual((check_jobs.root(self.project) / job['id'] / 'input/app.py').read_text(), 'VALUE = 2\n')
        self.run_job()
        result = check_jobs.result(ctx, job['id'])
        self.assertFalse(result['accepted_for_current_source'])
        self.assertIsNone(ctx.verification)
    def test_matching_completion_is_imported_in_later_unit(self):
        ctx, job = self.submit()
        self.run_job()
        later = self.context()
        result = check_jobs.result(later, job['id'])
        self.assertTrue(result['accepted_for_current_source'])
        self.assertTrue(later.verification['passed'])
        self.assertNotIn('cache_input', result['result'])
        self.assertNotIn('cache_output', result['result'])
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        check_jobs.acknowledge(self.project, 'worker', later.check_results_read)
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
    def test_submit_replay_is_idempotent(self):
        ctx, job = self.submit()
        self.assertEqual(check_jobs.submit(ctx)['id'], job['id'])
        self.run_job(); self.run_job()
        self.assertEqual(len(FakeSandbox.calls), 1)
    def test_completed_job_wakes_only_requester_until_read(self):
        ctx, job = self.submit()
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.run_job()
        self.assertEqual(check_jobs.pending(self.project, 'worker')[0]['id'], job['id'])
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
    def test_logs_without_command_id_lists_only_owned_recorded_evidence(self):
        ctx, job = self.submit()
        directory = check_jobs.root(self.project)/job['id']/'commands'
        write_json(directory/('a'*32+'.json'), {'stdout': 'recorded output'})
        write_json(directory/'not-evidence.json', {'secret': 'not exposed'})
        (directory/('b'*32+'.json')).symlink_to(directory/('a'*32+'.json'))
        listing = check_jobs.logs(ctx, job['id'])
        self.assertEqual([c['evidence_id'] for c in listing['commands']], ['a'*32])
        request = listing['commands'][0]['request']
        self.assertIn('recorded output', ctx._op_check_job(request)['text'])
        with self.assertRaises(Denied):
            check_jobs.logs(self.context('reviewer'), job['id'])
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])

    def test_model_slot_not_needed_and_job_cleanup_identity_is_separate(self):
        ctx, job = self.submit()
        # Runner doesn't acquire the role or live workspace exclusion.
        with lock(self.project.root / 'locks/run-worker.lock'), lock(self.project.root / 'locks/workspace.lock'):
            self.assertEqual(self.run_job()['status'], 'completed')
        self.assertTrue(FakeSandbox.calls[0][2].startswith('check-'))
    def test_compute_slot_bounds(self):
        self.submit()
        with lock(self.config.data / 'check-slots/0.lock'), lock(self.config.data / 'check-slots/1.lock'):
            self.assertEqual(self.run_job()['status'], 'busy')
        self.assertFalse(FakeSandbox.calls)
    def test_slot_fault_releases_only_the_faulted_slot_lock(self):
        self.config = dataclasses.replace(self.config,
            exclude=(*self.config.exclude, 'target'),
            limits=dataclasses.replace(self.config.limits, parallel_checks=2),
            sandbox=dataclasses.replace(self.config.sandbox, check_cache_paths=('target',)))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude
        ctx, job = self.submit()
        fault = self.config.data / 'check-slots/0.fault.json'
        locks = {
            'owner': check_jobs.root(self.project) / job['id'] / 'owner.lock',
            'cache': self.project.root / 'locks/check-cache.lock',
            'slot0': self.config.data / 'check-slots/0.lock',
            'slot1': self.config.data / 'check-slots/1.lock',
            'exclusion0': check_slot_exclusion(self.project.root, 0),
            'exclusion1': check_slot_exclusion(self.project.root, 1),
        }
        exists = Path.exists
        fault_checks = []

        def fault_exists(path):
            if path != fault:
                return exists(path)
            # Inject the race synchronously, between the two fault probes.
            fault_checks.append(held(locks['slot0']))
            if len(fault_checks) == 1:
                self.assertFalse(exists(fault))
                return False
            self.assertEqual(len(fault_checks), 2)
            self.assertTrue(held(locks['slot0']))
            write_json(fault, {'job': job['id'], 'error': 'Synthetic slot fault'})
            return True

        class Inspecting(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                for name, path in locks.items():
                    with self.subTest(during_execution=name):
                        self.assertEqual(held(path), name not in ('slot0', 'exclusion0'))
                return super().execute(workspace, script, **kwargs)

        with patch.object(Path, 'exists', fault_exists):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Inspecting)
        self.assertEqual(fault_checks, [False, True])
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['passed'])
        self.assertTrue(FakeSandbox.calls)
        self.assertTrue(fault.exists())
        for name, path in locks.items():
            with self.subTest(after_completion=name):
                self.assertFalse(held(path))

    def test_pending_response_exposes_owned_slot_releasing_continuation(self):
        ctx, job = self.submit()
        self.assertFalse(job['accepted_for_current_source'])
        wait = job['wait']
        self.assertEqual(wait['operation'], 'finish')
        self.assertEqual(wait['arguments'], {'outcome': 'wait', 'wait_for_check': job['id']})
        self.assertIsNone(ctx.finished)
        ctx._op_finish({**wait['arguments'], 'summary': 'Independent work complete', 'state': 'Await frozen result'})
        self.assertEqual(ctx.finished['wait_for_check'], job['id'])
        self.run_job()
        self.assertNotIn('wait', check_jobs.result(ctx, job['id']))

    def test_cache_blocked_verification_does_not_block_independent_check(self):
        self.project.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_cache_paths=('target',)))
        self.config = self.project.config
        ctx, verification = self.submit()
        read_job = check_jobs.submit(self.context('reviewer'), 'printf independent', kind='exec')
        with lock(self.project.root / 'locks/check-cache.lock'):
            result = self.run_job()
            self.assertEqual(result['id'], read_job['id'])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(self.run_job()['status'], 'busy')
            self.assertEqual(check_jobs.read(self.project, verification['id'])['status'], 'queued')
        self.assertEqual(self.run_job()['id'], verification['id'])

    def test_unrelated_checks_do_not_acquire_build_cache(self):
        self.submit()
        with lock(self.project.root / 'locks/check-cache.lock'):
            self.assertEqual(self.run_job()['status'], 'completed')

    def test_live_job_owner_does_not_block_later_check(self):
        ctx, first = self.submit()
        later = check_jobs.submit(self.context('reviewer'), 'printf later', kind='exec')
        with lock(check_jobs.root(self.project) / first['id'] / 'owner.lock'):
            result = self.run_job()
        self.assertEqual(result['id'], later['id'])
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'queued')

    def test_runner_drains_multiple_checks_without_timer_gap(self):
        self.submit()
        check_jobs.submit(self.context('reviewer'), 'printf second', kind='exec')
        result = check_jobs.run_pending(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(len(FakeSandbox.calls), 2)

    def test_batch_returns_on_contention_without_polling(self):
        self.submit()
        with lock(self.config.data / 'check-slots/0.lock'), lock(self.config.data / 'check-slots/1.lock'):
            result = check_jobs.run_pending(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        self.assertEqual(result, {'status': 'busy', 'processed': 0})
        self.assertFalse(FakeSandbox.calls)

    def test_batch_stop_prevents_next_job(self):
        self.submit()
        check_jobs.submit(self.context('reviewer'), 'printf second', kind='exec')
        stop = threading.Event()
        class Stopping(FakeSandbox):
            def execute(inner, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                stop.set()
                return result
        result = check_jobs.run_pending(self.project, stop, sandbox_factory=Stopping)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(len(FakeSandbox.calls), 1)

    def test_crashed_runner_is_not_success_or_automatic_reexecution(self):
        ctx, job = self.submit()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        record = read_json(path); record['status'] = 'running'; write_json(path, record)
        with patch('mizu.check_jobs.cleanup'):
            self.assertEqual(self.run_job()['status'], 'interrupted')
        self.assertFalse(FakeSandbox.calls)
        self.assertIsNone(ctx.verification)
    def test_configuration_drift_refuses_before_execution(self):
        ctx, job = self.submit()
        self.project.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, command_seconds=1))
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
    def test_paused_project_does_not_start_check(self):
        self.submit(); self.project.set_control(paused=True)
        self.assertEqual(self.run_job()['status'], 'deferred')
        self.assertFalse(FakeSandbox.calls)
    def test_read_role_cannot_claim_writer_acceptance(self):
        ctx, job = self.submit(); self.run_job()
        with self.assertRaises(Denied):
            check_jobs.result(self.context('reviewer'), job['id'])

    def test_verify_tool_returns_job_without_synchronous_execution(self):
        ctx = self.context()
        with patch.object(ctx.sandbox, 'execute', side_effect=AssertionError('synchronous execution')):
            self.assertEqual(ctx._op_verify({})['status'], 'queued')
    def test_read_role_experiment_uses_isolated_job(self):
        ctx = self.context('reviewer')
        ctx.role = dataclasses.replace(ctx.role, capabilities=(*ctx.role.capabilities, 'experiment'))
        self.project.config.roles['reviewer'] = ctx.role
        job = ctx._op_exec_experiment('experiment', {'script': 'test -f /workspace/app.py', 'mode': 'async'})
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(self.run_job()['status'], 'completed')
    def test_event_only_role_admits_owned_completion_and_preserves_explicit_wait(self):
        from mizu.services import render
        from pathlib import Path
        ctx, job = self.submit()
        self.run_job()
        from mizu.runtime import _role_events_due
        # The default automatic signal is one-shot, separate from exact waits.
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'worker'))
        check_jobs.mark_wake_delivered(self.project, 'worker', [job['id']], ctx.run_dir.name)
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'worker'))
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'worker'))
        units = render(self.config, self.project, Path('/usr/bin/mizu'), system='linux')
        self.assertIn('mizu-6-sample-checks.service', units)
        self.assertIn('mizu-6-sample-checks.timer', units)
        self.assertIn('check-jobs', units['mizu-6-sample-reviewer-event.path'])
    def test_finish_wait_releases_unit_and_completion_reawakens(self):
        from support import ScriptDriver
        from mizu.runtime import Engine, should_run
        def act(ctx, *_):
            job = ctx._op_verify({})
            ctx._op_finish({'outcome': 'wait', 'summary': 'Waiting for frozen check',
                            'state': 'Independent work complete', 'wait_for_check': job['id']})
        result = Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'waiting')
        self.assertIsInstance(result['next_evaluation_at'], (int, float))
        self.assertFalse((self.project.root / 'observed/worker.json').exists())
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertFalse((self.project.root / 'active/worker.json').exists())
        self.run_job()
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
    def test_waiting_for_next_check_acknowledges_consumed_result_and_carries_report(self):
        from support import ScriptDriver
        from mizu.runtime import Engine, prompt_for
        _, first = self.submit()
        self.run_job()
        def act(ctx, *_):
            ctx._op_check_job({'action': 'result', 'id': first['id']})
            next_job = ctx._op_check_job({'action': 'start', 'script': 'echo next'})
            ctx._op_finish({'outcome': 'wait', 'summary': 'Reviewed source; only next evidence remains',
                            'state': 'Continue from the check result', 'wait_for_check': next_job['id']})
        result = Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertEqual(check_jobs.pending(self.project, 'worker'), [])
        self.assertTrue(check_jobs.read(self.project, first['id'])['acknowledged'])
        self.assertNotIn(first['id'], [j['id'] for j in check_jobs.list_jobs(self.project, 'worker', include_acknowledged=False)])
        report = check_jobs.continuation(self.project, 'worker', self.project.snapshots.get())
        self.assertEqual(report['run'], result['run'])
        self.assertTrue(report['same_code'])
        self.assertEqual(report['summary'], 'Reviewed source; only next evidence remains')
        changed = dict(self.project.snapshots.get(), code_digest='f' * 64)
        self.assertFalse(check_jobs.continuation(self.project, 'worker', changed)['same_code'])
        self.assertIsNone(check_jobs.continuation(self.project, 'reviewer', changed))
        check_jobs.clear_wait(self.project, 'worker')
        self.assertIsNone(check_jobs.continuation(self.project, 'worker', changed))

    def test_timed_wait_releases_admission_and_workspace(self):
        from support import ScriptDriver
        from mizu.runtime import Engine, should_run
        def act(ctx, *_):
            ctx._op_finish({'outcome': 'wait', 'summary': 'External condition pending',
                            'state': 'Resume when due', 'wait_seconds': 420})
        Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        with lock(self.project.root/'locks/workspace.lock', blocking=False), \
                lock(self.project.root/'locks/run-worker.lock', blocking=False), \
                lock(self.config.data/'slots/0.lock', blocking=False):
            pass

    def test_bounded_finish_keeps_result_unacknowledged(self):
        from support import ScriptDriver
        from mizu.runtime import Engine
        _, job = self.submit(); self.run_job()
        def act(ctx, *_):
            ctx._op_check_job({'action': 'result', 'id': job['id']})
            ctx.bound_hit = 'requests'
            ctx._op_finish({'outcome': 'continue', 'summary': 'Bound reached', 'state': 'Unfinished work'})
        Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])

    def test_finish_releases_only_explicit_owned_checks(self):
        from support import ScriptDriver
        from mizu.runtime import Engine
        _, obsolete = self.submit()
        def act(ctx, *_):
            retained = ctx._op_check_job({'action': 'start', 'script': 'echo retained'})
            ctx._op_finish({'outcome': 'wait', 'summary': 'Retain only the needed check',
                            'state': 'Waiting', 'wait_for_check': retained['id'],
                            'release_checks': [obsolete['id']]})
        Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertEqual(check_jobs.read(self.project, obsolete['id'])['status'], 'cancelled')
        self.assertTrue(check_jobs.read(self.project, obsolete['id'])['acknowledged'])
        self.assertEqual(check_jobs.pending(self.project, 'worker'), [])
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))

    def test_retention_preserves_released_physical_job_until_runner_finishes(self):
        from mizu.errors import Busy
        ctx, job = self.submit()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        record = read_json(path); record['status'] = 'running'; write_json(path, record)
        check_jobs.cancel(ctx, job['id'])
        check_jobs.acknowledge(self.project, 'worker', {job['id']})
        with patch('pathlib.Path.glob', return_value=[path] * 1024):
            with self.assertRaises(Busy):
                check_jobs._make_room(self.project)
        self.assertTrue(path.exists())
        record = read_json(path); record['status'] = 'cancelled'; write_json(path, record)
        with patch('pathlib.Path.glob', return_value=[path] * 1024):
            check_jobs._make_room(self.project)
        self.assertFalse(path.exists())

    def test_finish_refuses_releasing_other_role_or_waited_check(self):
        _, job = self.submit()
        reader = self.context('reviewer')
        with self.assertRaises(Denied):
            reader._op_finish({'outcome': 'wait', 'summary': 'Invalid release', 'release_checks': [job['id']]})
        ctx = self.context()
        with self.assertRaises(Denied):
            ctx._op_finish({'outcome': 'wait', 'state': 'Wait', 'summary': 'Contradictory wait',
                            'release_checks': [job['id']], 'wait_for_check': job['id']})
        self.assertEqual(check_jobs.read(self.project, job['id'])['status'], 'queued')

    def test_new_operator_input_interrupts_check_only_wait(self):
        ctx, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.project.set_control(wake_generation='new-independent-work')
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
    def test_check_wait_holds_the_role_only_until_its_bound(self):
        from support import ScriptDriver
        from mizu.runtime import Engine, should_run
        finish_requested_at = []
        def act(ctx, *_):
            job = ctx._op_verify({})
            finish_requested_at.append(time.time())
            ctx._op_finish({'outcome': 'wait', 'summary': 'Waiting for frozen check',
                            'state': 'Independent work complete', 'wait_for_check': job['id'],
                            'wait_seconds': 60})
        result = Engine(self.config, driver=ScriptDriver(act)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'waiting')
        # The unit names the deferral it leaves behind and the bound that ends it.
        self.assertEqual(result['waiting_for_check']['id'], result['finish']['wait_for_check'])
        # The bound is the wait the unit sealed with, not the configured
        # default: the recorded wake_at is the truth the operator reads.
        requested_at = finish_requested_at[0]
        self.assertAlmostEqual(result['waiting_for_check']['wait_until'],
                               requested_at + 60, delta=1)
        self.assertNotAlmostEqual(result['waiting_for_check']['wait_until'],
                                   requested_at + self.config.limits.default_wait_seconds,
                                   delta=1)
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        snapshot = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, snapshot, role_name='worker'))
        # The bound passes while the job is still queued: the wait holds the
        # role no longer, so the ordinary wait expiry can admit it.
        path = check_jobs.root(self.project) / 'worker.wait.json'
        record = read_json(path)
        record['wait_until'] = time.time() - 1
        write_json(path, record)
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
        self.assertTrue(should_run(self.project, snapshot, current_time=snapshot['wake_at'] + 1,
                                   role_name='worker'))
    def test_operator_status_names_the_check_wait_and_its_bound(self):
        ctx, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        held = self.project.status()['check_waits']
        self.assertEqual(held['worker']['id'], job['id'])
        self.assertEqual(held['worker']['status'], 'queued')
        # A deferral recorded without a bound of its own carries the
        # configured default, so a recorded hold is never unbounded.
        self.assertAlmostEqual(held['worker']['wait_until'],
                               time.time() + self.config.limits.default_wait_seconds,
                               delta=1)
        path = check_jobs.root(self.project) / 'worker.wait.json'
        record = read_json(path)
        record['wait_until'] = time.time() - 1
        write_json(path, record)
        self.assertEqual(self.project.status()['check_waits'], {})
    def test_check_wait_without_a_bound_holds_nothing(self):
        ctx, job = self.submit()
        write_json(check_jobs.root(self.project) / 'worker.wait.json',
                   {'id': job['id'], 'inputs': check_jobs.wait_signature(self.project)})
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
    def test_check_wait_rejects_nonfinite_or_unrepresentable_bounds(self):
        _, job = self.submit()
        for deadline in (float('inf'), float('-inf'), float('nan'), 10 ** 400):
            with self.subTest(deadline=repr(deadline)):
                with self.assertRaises(Denied):
                    check_jobs.set_wait(self.project, 'worker', job['id'], wait_until=deadline)
    def test_overflow_check_wait_bound_neither_holds_nor_wakes(self):
        # Isolate the explicit wait from the independent automatic result policy.
        self.config.roles['worker'] = dataclasses.replace(self.config.roles['worker'],
                                                         check_result_wake=False)
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'], wait_until=time.time() + 60)
        path = check_jobs.root(self.project) / 'worker.wait.json'
        raw, count = re.subn(r'("wait_until"\s*:\s*)[^,}]+', r'\g<1>1e999',
                             path.read_text(), count=1)
        self.assertEqual(count, 1)
        path.write_text(raw)
        self.assertIsNone(check_jobs.deferral(self.project, 'worker'))
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
        self.assertNotIn('worker', self.project.status()['check_waits'])
        # A terminal result cannot turn the overflowing deadline into a wake.
        self.run_job()
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
    def test_failed_check_does_not_supply_acceptance(self):
        ctx, job = self.submit()
        class Failed(FakeSandbox):
            def execute(self, workspace, script, **kwargs):
                return {'id': 'b' * 32, 'exit_code': 1, 'reason': 'exited', 'script': script}
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Failed)
        self.assertFalse(check_jobs.result(ctx, job['id'])['result']['passed'])
        self.assertIsNone(ctx.verification)
    def test_removed_authority_refuses_job(self):
        ctx, job = self.submit()
        self.project.config.roles['worker'] = dataclasses.replace(ctx.role, capabilities=tuple(c for c in ctx.role.capabilities if c != 'check_job'))
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
    def test_changed_frozen_source_is_not_executed(self):
        ctx, job = self.submit()
        (check_jobs.root(self.project) / job['id'] / 'input/app.py').write_text('changed')
        with patch('mizu.check_jobs.cleanup'):
            self.assertEqual(self.run_job()['status'], 'interrupted')
        self.assertFalse(FakeSandbox.calls)

    def test_delegated_check_is_delivered_to_parent_role(self):
        parent = self.context()
        child = self.context('reviewer')
        child.role = dataclasses.replace(child.role, capabilities=(*child.role.capabilities, 'experiment'))
        self.project.config.roles['reviewer'] = child.role
        child.delegated_child = True
        child.spend_role = parent.role
        job = check_jobs.submit(child, 'test -f /workspace/app.py')
        self.assertEqual(job['role'], 'worker')
        self.run_job()
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        self.assertEqual(check_jobs.result(parent, job['id'])['status'], 'completed')

    def test_read_only_exec_returns_job_but_writer_exec_is_not_detached(self):
        reader = self.context('reviewer')
        reader.role = dataclasses.replace(reader.role, capabilities=(*reader.role.capabilities, 'exec'))
        self.assertEqual(reader._op_exec_experiment('exec', {'script': 'echo hello', 'mode': 'async'})['status'], 'queued')
        with self.assertRaises(Denied):
            check_jobs.submit(self.context(), 'echo hello', kind='exec')
    def test_matching_async_acceptance_can_complete_a_later_unit(self):
        from support import ScriptDriver
        from mizu.runtime import Engine
        ctx, job = self.submit()
        self.run_job()
        def finish(later, *_):
            later._op_check_job({'action': 'result', 'id': job['id']})
            later._op_finish({'outcome': 'done', 'summary': 'Verified frozen source', 'state': 'Complete'})
        result = Engine(self.config, driver=ScriptDriver(finish)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(self.project.snapshots.get()['verification']['passed'])
        self.assertFalse(check_jobs.pending(self.project, 'worker'))

    def test_queued_cancellation_prevents_execution_and_wakes_owner(self):
        ctx, job = self.submit()
        self.assertTrue(check_jobs.cancel(ctx, job['id'])['cancel_requested'])
        self.assertEqual(self.run_job()['status'], 'cancelled')
        self.assertFalse(FakeSandbox.calls)
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertNotIn('accepted_for_current_source', check_jobs.result(ctx, job['id']))

    def test_failed_container_cleanup_reserves_capacity_until_recovered(self):
        ctx, job = self.submit()
        class Broken(FakeSandbox):
            def execute(self, *args, **kwargs):
                raise Denied('Container cleanup failed')
        with patch('mizu.check_jobs.cleanup', side_effect=Denied('still running')):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Broken)
            self.assertEqual(result['status'], 'cleanup_failed')
            with self.assertRaises(Denied):
                check_jobs.recover(self.project)
        fault = self.config.data / 'check-slots/0.fault.json'
        self.assertTrue(fault.exists())
        with patch('mizu.check_jobs.cleanup'):
            self.assertEqual(check_jobs.recover(self.project)['recovered'], [job['id']])
        self.assertFalse(fault.exists())

    def test_a_refused_recovery_reports_the_job_and_keeps_the_reservation(self):
        ctx, job = self.submit()
        class Broken(FakeSandbox):
            def execute(self, *args, **kwargs):
                raise Denied('Container cleanup failed')
        with patch('mizu.check_jobs.cleanup', side_effect=Denied('still running')):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Broken)
            self.assertEqual(result['status'], 'cleanup_failed')
        fault = self.config.data / 'check-slots/0.fault.json'
        self.assertTrue(fault.exists())
        # A live invocation holds the slot's exclusion, so the recovery's
        # sweep is refused: the operator is told which job it could not
        # clean, and the reservation stays in place.
        with lock(check_slot_exclusion(self.project.root, 0)):
            recovered = check_jobs.recover(self.project)
        self.assertEqual(recovered['refused'], [job['id']])
        self.assertEqual(recovered['recovered'], [])
        self.assertTrue(fault.exists())

    def test_completed_jobs_release_their_private_build_trees(self):
        ctx, job = self.submit()
        directory = check_jobs.root(self.project) / job['id']
        self.assertTrue((directory / 'input').exists())
        self.run_job()
        self.assertFalse((directory / 'input').exists())
        self.assertTrue((directory / 'result.json').exists())
        self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])

    def test_build_artifacts_reused_without_acceptance_reuse(self):
        self.config = dataclasses.replace(self.config, exclude=(*self.config.exclude, 'target'), sandbox=dataclasses.replace(self.config.sandbox, check_cache_paths=('target',)))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude
        observed = []
        class Compiler(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                artifact = workspace / 'target/object'
                observed.append(artifact.exists())
                artifact.parent.mkdir(exist_ok=True)
                artifact.write_text('compiled')
                return super().execute(workspace, script, **kwargs)
        ctx, first = self.submit()
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Compiler)
        later = self.context()
        check_jobs.submit(later)
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Compiler)
        self.assertEqual(observed, [False, True])
        self.assertFalse((ctx.workspace / 'target').exists())

    def test_command_logs_are_paged_and_owner_bound(self):
        ctx, job = self.submit()
        path = check_jobs.root(self.project) / job['id'] / 'commands' / ('a'*32 + '.json')
        write_json(path, {'stdout': 'details' * 2000})
        first = ctx._op_check_job({'action': 'logs', 'id': job['id'], 'evidence_id': 'a'*32, 'limit': 20})
        second = check_jobs.logs(ctx, job['id'], 'a'*32, offset=20, limit=20, content_id=first['content_id'])
        self.assertEqual(second['offset'], 20)
        with self.assertRaises(Denied):
            check_jobs.logs(self.context('reviewer'), job['id'], 'a'*32)
        write_json(path, {'stdout': 'changed'})
        with self.assertRaises(Denied):
            check_jobs.logs(ctx, job['id'], 'a'*32, content_id=first['content_id'])


class PurposeTests(JobTests):
    """A purpose names the caller's own demand, so a later request replaces it."""

    def test_same_purpose_replaces_the_roles_earlier_job(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        second = check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        self.assertNotEqual(first['id'], second['id'])
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['replaced_by'], second['id'])
        self.assertEqual(check_jobs.read(self.project, second['id'])['purpose'], 'acceptance')

    def test_a_purpose_stated_while_the_runner_holds_the_record_survives_its_write_back(self):
        # The runner reads the record, and the purpose is stated on it
        # afterwards. The runner's write-back must not drop that label, or
        # the demand stops matching the later request that replaces it.
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment')
        runner_view = dict(check_jobs.raw_read(self.project, first['id']))
        runner_view['status'] = 'running'
        # The same request is stated again with its purpose, which lands on
        # the record while the runner holds its older view.
        check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        self.assertEqual(check_jobs.read(self.project, first['id'])['purpose'], 'acceptance')
        check_jobs.save_execution(self.project, runner_view)
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['purpose'], 'acceptance')
        self.assertEqual(record['status'], 'running')
        # A later request with that purpose replaces the earlier demand.
        second = check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['replaced_by'], second['id'])

    def test_replaced_job_frees_the_pending_capacity_it_held(self):
        self.config = dataclasses.replace(self.config,
                                          limits=dataclasses.replace(self.config.limits, check_pending=1))
        self.project.config = self.config
        ctx = self.context()
        check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        with self.assertRaises(Busy):
            check_jobs.submit(ctx, 'echo other', kind='experiment')
        second = check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        self.assertEqual(check_jobs.read(self.project, second['id'])['status'], 'queued')

    def test_different_purpose_keeps_the_earlier_job(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='experiment')
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['status'], 'queued')
        self.assertNotIn('replaced_by', record)

    def test_another_roles_job_with_the_same_purpose_is_untouched(self):
        worker = self.context()
        first = check_jobs.submit(worker, 'echo first', kind='experiment', purpose='acceptance')
        reviewer = self.context('reviewer')
        check_jobs.submit(reviewer, 'echo second', kind='exec', purpose='acceptance')
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['status'], 'queued')
        self.assertNotIn('replaced_by', record)

    def test_purpose_cannot_retire_a_job_owned_by_another_role(self):
        worker = self.context()
        first = check_jobs.submit(worker, 'echo first', kind='experiment', purpose='acceptance')
        reviewer = self.context('reviewer')
        review = check_jobs.submit(reviewer, 'echo second', kind='exec', purpose='acceptance')
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'queued')
        check_jobs.submit(worker, 'echo third', kind='experiment', purpose='acceptance')
        # The worker's own demand is replaced; the reviewer's is untouched.
        self.assertEqual(check_jobs.read(self.project, review['id'])['status'], 'queued')
        self.assertNotIn('replaced_by', check_jobs.read(self.project, review['id']))

    def test_replaced_record_keeps_its_evidence_and_names_its_replacement(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        directory = check_jobs.root(self.project) / first['id']
        self.assertTrue((directory / 'input').exists())
        second = check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        record = read_json(directory / 'job.json')
        self.assertEqual(record['replaced_by'], second['id'])
        self.assertEqual(record['code_digest'], first['code_digest'])
        self.assertTrue((directory / 'input').exists())
        # The replaced job never reaches the runner; its record stays readable.
        self.run_job()
        self.assertEqual(read_json(directory / 'job.json')['status'], 'cancelled')
        self.assertFalse((directory / 'result.json').exists())

    def test_replacement_does_not_credit_capacity_it_already_released(self):
        # A job that was replaced while it was still running holds no
        # capacity: crediting it again would let the queue grow past its cap
        # while that earlier cancellation is still outstanding.
        self.config = dataclasses.replace(self.config,
                                          limits=dataclasses.replace(self.config.limits, check_pending=3))
        self.project.config = self.config
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        second = check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        record = read_json(check_jobs.root(self.project) / first['id'] / 'job.json')
        record['status'] = 'running'
        record['detached'] = True
        record['replaced_by'] = second['id']
        write_json(check_jobs.root(self.project) / first['id'] / 'job.json', record)
        other = check_jobs.submit(self.context('reviewer'), 'echo other', kind='exec')
        self.assertEqual(check_jobs.read(self.project, other['id'])['status'], 'queued')
        # The queue counts the successor and the other role's job, and the
        # replaced job holds no capacity at all.
        self.config = dataclasses.replace(self.config,
                                          limits=dataclasses.replace(self.config.limits, check_pending=1))
        self.project.config = self.config
        with self.assertRaises(Busy):
            check_jobs.submit(self.context(), 'echo third', kind='experiment', purpose='acceptance')

    def test_acceptance_at_a_full_queue_is_refused(self):
        self.config = dataclasses.replace(self.config,
                                          limits=dataclasses.replace(self.config.limits, check_pending=1))
        self.project.config = self.config
        check_jobs.submit(self.context('reviewer'), 'echo other', kind='exec')
        with self.assertRaises(Busy):
            check_jobs.submit(self.context())

    def test_invalid_purpose_is_refused(self):
        ctx = self.context()
        for value in ('', '   ', 'a/b', 'a\\b', 'x' * 65, 'two\nlines', 7):
            with self.assertRaises(Denied):
                check_jobs.submit(ctx, 'echo check', kind='experiment', purpose=value)
        self.assertEqual(check_jobs.list_jobs(self.project, 'worker'), [])

    def test_job_without_a_purpose_is_never_replaced(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment')
        check_jobs.submit(ctx, 'echo second', kind='experiment', purpose='acceptance')
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['status'], 'queued')
        self.assertNotIn('replaced_by', record)

    def test_tool_carries_the_purpose_and_refuses_an_invalid_one(self):
        ctx = self.context()
        job = ctx._op_check_job({'action': 'start', 'script': 'echo tool', 'purpose': 'acceptance'})
        self.assertEqual(job['purpose'], 'acceptance')
        with self.assertRaises(Denied):
            ctx._op_check_job({'action': 'start', 'script': 'echo tool', 'purpose': 'bad/purpose'})


class SharingTests(PurposeTests):
    # Sharing is opt-in; retain the original no-sharing suite above.
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True))
        self.project.config = self.config

    def test_a_fresh_request_runs_its_own_execution_and_shares_its_result(self):
        ctx = self.context()
        fresh = check_jobs.submit(ctx, 'echo fresh', kind='experiment', fresh=True)
        self.assertEqual(check_jobs.read(self.project, fresh['id'])['status'], 'queued')
        # A later request with the same inputs reuses the fresh measurement
        # instead of running the execution again.
        later = self.context()
        shared = check_jobs.submit(later, 'echo fresh', kind='experiment')
        self.assertEqual(shared.get('shared_job'), fresh['id'])
        self.assertNotEqual(shared['id'], fresh['id'])

    def test_purpose_replaces_the_roles_earlier_job_when_the_request_shares(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        later = self.context()
        shared = check_jobs.submit(later, 'echo first', kind='experiment', purpose='acceptance')
        self.assertEqual(shared['shared_job'], first['id'])
        # The shared job is the surviving work, so it is never the replaced one.
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'queued')
        self.assertEqual(check_jobs.read(self.project, shared['id'])['purpose'], 'acceptance')

    def test_completed_result_reuse_is_synchronously_acknowledged(self):
        owner, original = self.submit()
        self.run_job()
        check_jobs.result(owner, original['id'])
        check_jobs.acknowledge(self.project, 'worker', owner.check_results_read)
        self.assertFalse(check_jobs.pending(self.project, 'worker'))

        # Both automatic reuse and explicit join return the terminal summary in
        # this call, so neither should manufacture another unread wake.
        later = self.context()
        shared = check_jobs.submit(later)
        self.assertEqual(shared['shared_job'], original['id'])
        self.assertEqual(shared['status'], 'completed')
        self.assertTrue(shared['passed'])
        self.assertTrue(check_jobs.read(self.project, shared['id'])['acknowledged'])
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assertTrue(check_jobs.result(later, shared['id'])['result']['passed'])

        reader = self.context('reviewer')
        joined = check_jobs.join(reader, original['id'])
        self.assertEqual(joined['status'], 'completed')
        self.assertTrue(check_jobs.read(self.project, joined['id'])['acknowledged'])
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        self.assertTrue(check_jobs.result(reader, joined['id'])['result']['passed'])

    def test_queued_join_then_completion_then_repeated_join_acks_same_subscription(self):
        _, original = self.submit()
        reader = self.context('reviewer')
        first = check_jobs.join(reader, original['id'])
        self.assertEqual(first['status'], 'queued')
        self.assertIn('wait', first)
        self.assertFalse(check_jobs.read(self.project, first['id'])['acknowledged'])
        self.run_job()
        self.assertEqual(check_jobs.read(self.project, original['id'])['status'], 'completed')
        repeated = check_jobs.join(reader, original['id'])
        self.assertEqual(repeated['id'], first['id'])
        self.assertEqual(repeated['status'], 'completed')
        self.assertTrue(repeated['passed'])
        self.assertNotIn('wait', repeated)
        self.assertTrue(check_jobs.read(self.project, repeated['id'])['acknowledged'])
        # The original execution and evidence are preserved for the owner.
        self.assertEqual(check_jobs.read(self.project, repeated['id'])['shared_job'], original['id'])
        self.assertFalse(check_jobs.read(self.project, original['id'])['acknowledged'])
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        self.assertTrue(check_jobs.result(reader, repeated['id'])['result']['passed'])

    def test_same_run_original_and_same_request_replay_are_acknowledged(self):
        ctx, original = self.submit()
        self.run_job()
        # Same-run original: joining the role's own completed job in the same
        # run returns it synchronously, so it must not remain an unread wake.
        same_run = check_jobs.join(ctx, original['id'])
        self.assertEqual(same_run['id'], original['id'])
        self.assertEqual(same_run['status'], 'completed')
        self.assertTrue(check_jobs.read(self.project, original['id'])['acknowledged'])
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        # Same-request replay: resubmitting the identical request in the same
        # run returns the same completed record, acknowledged the same way.
        # Use a distinct fresh measurement so the later submit does not share
        # the earlier completed execution.
        ctx2 = self.context()
        first = check_jobs.submit(ctx2, 'echo replay', kind='experiment', fresh=True)
        self.assertFalse(check_jobs.read(self.project, first['id'])['acknowledged'])
        self.run_job()
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        replayed = check_jobs.submit(ctx2, 'echo replay', kind='experiment', fresh=True)
        self.assertEqual(replayed['id'], first['id'])
        self.assertEqual(replayed['status'], 'completed')
        self.assertTrue(check_jobs.read(self.project, replayed['id'])['acknowledged'])
        self.assertFalse(check_jobs.pending(self.project, 'worker'))

    def test_queued_and_unsuccessful_results_are_not_auto_acknowledged(self):
        ctx, queued = self.submit()
        replayed_queued = check_jobs.submit(ctx)
        self.assertEqual(replayed_queued['id'], queued['id'])
        self.assertEqual(replayed_queued['status'], 'queued')
        self.assertFalse(check_jobs.read(self.project, queued['id'])['acknowledged'])
        reader = self.context('reviewer')
        subscription = check_jobs.join(reader, queued['id'])
        self.assertEqual(subscription['status'], 'queued')
        self.assertFalse(check_jobs.read(self.project, subscription['id'])['acknowledged'])
        repeated_queued = check_jobs.join(reader, queued['id'])
        self.assertEqual(repeated_queued['id'], subscription['id'])
        self.assertFalse(check_jobs.read(self.project, subscription['id'])['acknowledged'])
        # A completed but unsuccessful result stays unread until an explicit
        # acknowledgement, even when returned synchronously via replay.
        # Drain the earlier queued execution first so the failure below runs
        # against the intended record.
        self.run_job()
        check_jobs.acknowledge(self.project, 'worker', {queued['id']})
        failing = self.context()
        failed = check_jobs.submit(failing, 'echo fail-marker', kind='experiment')
        class Failed(FakeSandbox):
            def execute(self, workspace, script, **kwargs):
                return {'id': 'b' * 32, 'exit_code': 1, 'reason': 'exited', 'script': script}
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Failed)
        self.assertEqual(check_jobs.read(self.project, failed['id'])['status'], 'completed')
        self.assertFalse(check_jobs.read(self.project, failed['id'])['passed'])
        replayed_failed = check_jobs.submit(failing, 'echo fail-marker', kind='experiment')
        self.assertEqual(replayed_failed['id'], failed['id'])
        self.assertFalse(check_jobs.read(self.project, failed['id'])['acknowledged'])
        self.assertTrue(check_jobs.pending(self.project, 'worker'))

    def test_replaced_job_keeps_running_for_another_consumer(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, 'echo first', kind='experiment', purpose='acceptance')
        reader = self.context('reviewer')
        subscription = check_jobs.join(reader, first['id'])
        second = check_jobs.submit(self.context(), 'echo second', kind='experiment', purpose='acceptance')
        record = check_jobs.read(self.project, first['id'])
        self.assertEqual(record['replaced_by'], second['id'])
        # Another consumer still wants the execution, so the runner keeps it.
        self.assertFalse((check_jobs.root(self.project) / first['id'] / 'cancel.json').exists())
        for _ in range(3):
            self.run_job()
        self.assertEqual(check_jobs.result(reader, subscription['id'])['status'], 'completed')

    def test_producer_cannot_release_or_wait_on_another_subscription(self):
        producer, original = self.submit()
        reader = self.context('reviewer')
        subscription = check_jobs.join(reader, original['id'])
        with self.assertRaises(Denied):
            check_jobs.cancel(producer, subscription['id'])
        with self.assertRaises(Denied):
            producer._op_finish({'outcome': 'wait', 'state': 'Waiting', 'summary': 'Wrong subscription',
                                 'wait_for_check': subscription['id']})
        self.assertEqual(check_jobs.read(self.project, subscription['id'])['status'], 'queued')

    def test_two_roles_share_execution_and_ack_independently(self):
        ctx, first = self.submit()
        reader = self.context('reviewer')
        self.assertEqual(check_jobs.available(reader)[0]['id'], first['id'])
        second = check_jobs.join(reader, first['id'])
        check_jobs.set_wait(self.project, 'reviewer', second['id'])
        self.assertTrue(check_jobs.waiting(self.project, 'reviewer'))
        self.run_job(); self.run_job()
        self.assertEqual(len(FakeSandbox.calls), 1)
        self.assertFalse(check_jobs.waiting(self.project, 'reviewer'))
        result = check_jobs.result(reader, second['id'])
        self.assertTrue(result['result']['passed'])
        self.assertFalse(result['accepted_for_current_source'])  # No verify grant.
        check_jobs.acknowledge(self.project, 'reviewer', reader.check_results_read)
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        self.assertTrue(check_jobs.pending(self.project, 'worker'))

    def test_matching_later_verify_imports_shared_success_without_execution(self):
        ctx, first = self.submit(); self.run_job()
        later = self.context(); second = check_jobs.submit(later)
        self.assertEqual(second['shared_job'], first['id'])
        self.assertTrue(check_jobs.result(later, second['id'])['accepted_for_current_source'])
        self.run_job()
        self.assertEqual(len(FakeSandbox.calls), 1)

    def test_cancellation_does_not_cancel_other_subscriber(self):
        ctx, first = self.submit()
        reader = self.context('reviewer'); second = check_jobs.join(reader, first['id'])
        check_jobs.cancel(ctx, first['id'])
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'cancelled')
        self.assertFalse((check_jobs.root(self.project)/first['id']/'cancel.json').exists())
        self.run_job()
        self.assertEqual(check_jobs.result(reader, second['id'])['status'], 'completed')
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'cancelled')

    def test_last_subscriber_cancellation_stops_execution(self):
        ctx, first = self.submit(); reader = self.context('reviewer')
        second = check_jobs.join(reader, first['id'])
        check_jobs.cancel(reader, second['id']); check_jobs.cancel(ctx, first['id'])
        self.run_job()
        self.assertFalse(FakeSandbox.calls)
        self.assertEqual(check_jobs.read(self.project, second['id'])['status'], 'cancelled')

    def test_running_check_can_be_joined_and_logs_are_shared(self):
        ctx, first = self.submit(); reader = self.context('reviewer'); joined = []
        class Joining(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                joined.append(check_jobs.join(reader, first['id']))
                write_json(check_jobs.root(self.project)/first['id']/'commands'/('a'*32+'.json'), {'stdout':'shared output'})
                return super().execute(workspace, script, **kwargs)
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Joining)
        self.assertEqual(len(FakeSandbox.calls),1)
        self.assertIn('shared output',check_jobs.logs(reader,joined[0]['id'],'a'*32)['text'])
        request = check_jobs.logs(reader, joined[0]['id'])['commands'][0]['request']
        self.assertEqual(request['id'], joined[0]['id'])
        self.assertIn('shared output', reader._op_check_job(request)['text'])

    def test_changed_dependencies_refuse_queued_job_and_new_submission_is_distinct(self):
        dependency=self.root/'dependency'; dependency.mkdir(); file=dependency/'version'; file.write_text('one')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dependency),'target':'/dependency'},)))
        self.project.config=self.config
        ctx,first=self.submit(); file.write_text('two')
        self.assertEqual(self.run_job()['status'],'refused')
        later=check_jobs.submit(self.context());self.assertNotIn('shared_job',later)
        self.assertNotEqual(first['id'],later['id'])

    def test_dependency_change_during_execution_cannot_pass_or_be_reused(self):
        dependency=self.root/'version'; dependency.write_text('one')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dependency),'target':'/dependency'},)))
        self.project.config=self.config
        ctx,first=self.submit()
        class Changing(FakeSandbox):
            def execute(inner,*args,**kwargs):
                dependency.write_text('two')
                return super().execute(*args,**kwargs)
        check_jobs.run_one(self.project,threading.Event(),sandbox_factory=Changing)
        self.assertFalse(check_jobs.result(ctx,first['id'])['result']['passed'])
        self.assertNotIn('shared_job',check_jobs.submit(self.context()))

    def test_changed_source_commands_or_environment_do_not_share(self):
        ctx,first=self.submit()
        (ctx.workspace/'app.py').write_text('VALUE=4')
        self.assertNotIn('shared_job',check_jobs.submit(self.context()))
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,env={'EXAMPLE':'changed'}))
        self.project.config=self.config
        self.assertNotIn('shared_job',check_jobs.submit(self.context()))

    def test_network_input_disables_reuse(self):
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,network='bridge'))
        self.project.config=self.config
        self.submit(); later=check_jobs.submit(self.context())
        self.assertNotIn('shared_job',later)

    # Under explicit reuse the second identical acceptance uses evidence rather
    # than invoking the compiler. Artifact reuse is covered by JobTests.
    def test_build_artifacts_reused_without_acceptance_reuse(self):
        self.test_matching_later_verify_imports_shared_success_without_execution()

    def test_concurrent_submissions_execute_once(self):
        from concurrent.futures import ThreadPoolExecutor
        contexts=[self.context(),self.context()]
        with ThreadPoolExecutor(2) as pool:
            jobs=list(pool.map(check_jobs.submit,contexts))
        self.assertEqual(sum('shared_job' not in j for j in jobs),1)
        self.run_job();self.run_job()
        self.assertEqual(len(FakeSandbox.calls),1)

    def test_running_cancellation_preserves_other_subscription(self):
        ctx,first=self.submit();reader=self.context('reviewer')
        second=check_jobs.join(reader,first['id'])
        class Cancelling(FakeSandbox):
            def execute(inner,*args,**kwargs):
                check_jobs.cancel(ctx,first['id'])
                return super().execute(*args,**kwargs)
        check_jobs.run_one(self.project,threading.Event(),sandbox_factory=Cancelling)
        self.assertEqual(check_jobs.read(self.project,first['id'])['status'],'cancelled')
        self.assertTrue(check_jobs.result(reader,second['id'])['result']['passed'])

    def test_shared_ack_does_not_leave_same_role_duplicate_delivery(self):
        ctx,first=self.submit();self.run_job()
        later=self.context();second=check_jobs.submit(later)
        check_jobs.result(later,second['id'])
        check_jobs.acknowledge(self.project,'worker',later.check_results_read)
        self.assertFalse(check_jobs.pending(self.project,'worker'))

    def test_changed_mount_after_completion_cannot_authorize_acceptance(self):
        dep=self.root/'dependency';dep.write_text('one')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dep),'target':'/dep'},)))
        self.project.config=self.config
        ctx,job=self.submit();self.run_job();dep.write_text('two')
        self.assertFalse(check_jobs.result(ctx,job['id'])['accepted_for_current_source'])
        self.assertFalse(check_jobs.available(ctx))

    def test_input_limits_disable_reuse_without_disabling_custom_checks(self):
        dep=self.root/'dependency';dep.write_text('larger than bound')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dep),'target':'/dep'},),check_input_bytes=1))
        self.project.config=self.config
        check_jobs.submit(self.context(), 'echo bounded')
        second=check_jobs.submit(self.context(), 'echo bounded')
        self.assertNotIn('shared_job',second)
        self.assertEqual(self.run_job()['status'],'completed')

    def test_fresh_check_bypasses_completed_evidence(self):
        self.submit();self.run_job()
        fresh=check_jobs.submit(self.context(),fresh=True)
        self.assertNotIn('shared_job',fresh)
        self.run_job();self.assertEqual(len(FakeSandbox.calls),2)

    def test_internal_dependency_symlink_is_bound_to_its_target(self):
        import os
        dep=self.root/'deps';dep.mkdir();(dep/'data').write_text('one')
        try:os.symlink('data',dep/'link')
        except OSError:self.skipTest('Symlinks unavailable')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dep),'target':'/dep'},)))
        self.project.config=self.config
        before=check_jobs.environment(self.project);self.assertIsNotNone(before)
        (dep/'data').write_text('two')
        self.assertNotEqual(before,check_jobs.environment(self.project))

    def test_reaped_consumed_wait_does_not_block_future_work(self):
        import shutil
        ctx,job=self.submit();check_jobs.set_wait(self.project,'worker',job['id'])
        self.run_job();check_jobs.result(ctx,job['id'])
        check_jobs.acknowledge(self.project,'worker',ctx.check_results_read)
        shutil.rmtree(check_jobs.root(self.project)/job['id'])
        self.assertFalse(check_jobs.waiting(self.project,'worker'))

    def test_enabling_sharing_keeps_existing_queued_execution_valid(self):
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,check_reuse=False))
        self.project.config=self.config
        ctx,job=self.submit()
        self.project.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,check_reuse=True))
        self.assertEqual(self.run_job()['status'],'completed')
        self.assertTrue(check_jobs.result(ctx,job['id'])['accepted_for_current_source'])
        self.assertFalse(check_jobs.available(ctx))  # Old dependency inputs were not bound.

    def test_disabling_sharing_keeps_bound_queued_execution_valid(self):
        dep, ctx, raw = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False))
        self.assertIsNone(check_jobs.environment(self.project))
        self.assertEqual(check_jobs.execution_environment(self.project, raw), raw['environment'])
        self.assertEqual(self.run_job()['status'], 'completed')
        self.assertTrue(check_jobs.result(ctx, raw['id'])['accepted_for_current_source'])
        self.assertEqual(check_jobs.available(ctx), [])
        self.assertEqual(check_jobs.available(self.context('reviewer')), [])

    def bound_dependency(self):
        dep = self.root / 'dependency'
        dep.mkdir()
        (dep / 'version').write_text('one')
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, mounts=({'source': str(dep), 'target': '/dep'},),
            check_input_files=2, check_input_bytes=3))
        self.project.config = self.config
        ctx, job = self.submit()
        raw = check_jobs.read(self.project, job['id'])
        self.assertIsNotNone(raw['environment'])
        self.assertEqual(raw['environment_bounds'], {'files': 2, 'bytes': 3})
        return dep, ctx, raw

    def test_lowered_limits_use_original_bounds_for_all_revalidation(self):
        dep, ctx, job = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_input_files=1, check_input_bytes=1))
        self.assertIsNone(check_jobs.environment(self.project))
        with patch.object(check_jobs, '_mount_fingerprint', wraps=check_jobs._mount_fingerprint) as scan:
            self.assertEqual(self.run_job()['status'], 'completed')
            self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertEqual([call.args[1] for call in scan.call_args_list],
                         [job['environment_bounds']] * 3)  # Before, after, acceptance.
        self.assertEqual(check_jobs.available(self.context('reviewer')), [])

    def test_bounds_are_metadata_not_job_or_shared_identity(self):
        dep, ctx, job = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_input_files=10, check_input_bytes=100))
        ctx.config = self.project.config
        self.assertEqual(check_jobs.execution_key(self.project.config), job['execution'])
        self.assertEqual(check_jobs.submit(ctx)['id'], job['id'])
        shared = check_jobs.submit(self.context())
        self.assertEqual(shared['shared_job'], job['id'])
        raw = check_jobs.read(self.project, shared['id'])
        self.assertEqual(raw['shared_key'], job['shared_key'])
        self.assertEqual(raw['environment_bounds'], job['environment_bounds'])

    def test_changed_dependency_still_refuses_after_sharing_disabled(self):
        dep, ctx, job = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False, check_input_files=1, check_input_bytes=1))
        (dep / 'version').write_text('two')
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])

    def test_original_byte_limit_stops_before_hashing_grown_dependency(self):
        dep, ctx, job = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False, check_input_files=1, check_input_bytes=1))
        (dep / 'version').write_text('larger than original bound')
        with patch.object(check_jobs.hashlib, 'file_digest', side_effect=AssertionError('Over-bound hash')):
            self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)

    def test_original_file_limit_stops_directory_enumeration(self):
        dep, ctx, job = self.bound_dependency()
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False, check_input_files=1, check_input_bytes=1))
        original = Path.iterdir
        visited = []
        def children(path):
            if path != dep:
                yield from original(path)
                return
            for name in ('version', 'extra'):
                visited.append(name)
                yield path / name
            self.fail('Enumeration exceeded the original file bound')
        with patch.object(Path, 'iterdir', children):
            self.assertEqual(self.run_job()['status'], 'refused')
        self.assertEqual(visited, ['version', 'extra'])
        self.assertFalse(FakeSandbox.calls)

    def test_original_file_limit_is_global_across_directory_enumeration(self):
        dep = self.root / 'dependency'
        dep.mkdir()
        directories = [dep / name for name in ('a', 'b', 'c')]
        for directory in directories:
            directory.mkdir()
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, mounts=({'source': str(dep), 'target': '/dep'},),
            check_input_files=4, check_input_bytes=1024))
        self.project.config = self.config
        ctx, job = self.submit()
        raw = check_jobs.read(self.project, job['id'])
        self.assertEqual(raw['environment_bounds']['files'], 4)
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False))
        for directory in directories:
            for index in range(4):
                (directory / f'file{index}').write_text('x')
        original = Path.iterdir
        visited = []

        def children(path):
            for child in original(path):
                if path == dep or dep in path.parents:
                    visited.append(child)
                yield child

        with patch.object(Path, 'iterdir', children):
            self.assertEqual(self.run_job()['status'], 'refused')
        # Four bound paths exist at submission; revalidation stops as soon as
        # it observes a fifth, instead of enumerating each subtree up to four.
        self.assertLessEqual(len(visited), 5)
        self.assertFalse(FakeSandbox.calls)

    def test_malformed_retained_bounds_refuse_execution_and_acceptance(self):
        dep, ctx, job = self.bound_dependency()
        self.run_job()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        completed = read_json(path)
        invalid = [None, [], {}, {'files': 2}, {'files': 2, 'bytes': 3, 'extra': 1}]
        for key in ('files', 'bytes'):
            invalid.extend({**job['environment_bounds'], key: value}
                           for value in (True, False, '2', 2.0, 0, -1, 1099511627777))
        for bounds in invalid:
            with self.subTest(bounds=bounds):
                write_json(path, {**completed, 'environment_bounds': bounds})
                with patch.object(Path, 'lstat', side_effect=AssertionError('Invalid bounds must not scan')):
                    self.assertIsNone(check_jobs.execution_environment(self.project, read_json(path)))
                self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        write_json(path, {**job, 'environment_bounds': None})
        FakeSandbox.calls.clear()
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)

    def test_legacy_bound_jobs_use_current_limits_even_when_reuse_is_off(self):
        dep, ctx, job = self.bound_dependency()
        job.pop('environment_bounds')
        self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=False))
        self.assertEqual(check_jobs.execution_environment(self.project, job), job['environment'])
        self.project.config = dataclasses.replace(self.project.config, sandbox=dataclasses.replace(
            self.project.config.sandbox, check_input_bytes=1))
        self.assertIsNone(check_jobs.execution_environment(self.project, job))

    def test_bound_jobs_still_reject_execution_configuration_changes(self):
        ctx, job = self.submit()
        raw = check_jobs.read(self.project, job['id'])
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        for changes in ({'network': 'bridge'}, {'image': 'sha256:' + 'f' * 64},
                        {'env': {'EXAMPLE': 'changed'}}):
            with self.subTest(changes=changes):
                write_json(path, raw)
                self.project.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
                    self.config.sandbox, check_reuse=False, **changes))
                self.assertEqual(self.run_job()['status'], 'refused')
                self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertFalse(FakeSandbox.calls)

    def test_unavailable_dependency_identity_cannot_fall_back_to_acceptance(self):
        with patch('mizu.check_jobs.environment',return_value=None):
            ctx,job=self.submit()
        self.assertEqual(self.run_job()['status'],'refused')
        self.assertIsNone(ctx.verification)
        self.assertFalse(FakeSandbox.calls)

    def test_unbound_completed_record_is_not_accepted_under_sharing(self):
        with patch('mizu.check_jobs.environment',return_value=None):
            ctx,job=self.submit()
        directory=check_jobs.root(self.project)/job['id']
        raw=read_json(directory/'job.json');raw.update(status='completed',passed=True)
        write_json(directory/'job.json',raw);write_json(directory/'result.json',{'passed':True})
        self.assertFalse(check_jobs.result(ctx,job['id'])['accepted_for_current_source'])
        self.assertIsNone(ctx.verification)

    def test_handle_and_path_metadata_representations_need_not_match(self):
        import os
        from types import SimpleNamespace
        dep=self.root/'dependency';dep.write_text('one')
        self.config=dataclasses.replace(self.config,sandbox=dataclasses.replace(self.config.sandbox,mounts=({'source':str(dep),'target':'/dep'},)))
        self.project.config=self.config
        original=os.fstat
        def handle_stat(fd):
            value=original(fd)
            fields={name:getattr(value,name) for name in dir(value) if name.startswith('st_')}
            fields['st_ctime_ns']+=1
            return SimpleNamespace(**fields)
        with patch('mizu.check_jobs.os.fstat',side_effect=handle_stat):
            before=check_jobs.environment(self.project)
            self.assertIsNotNone(before)
            dep.write_text('two')
            after=check_jobs.environment(self.project)
            self.assertIsNotNone(after)
            self.assertNotEqual(before,after)


class SweepProtectionTests(Fixture):
    """A sweep never removes the container of a running check job.

    The container runtime is mocked, so no container is created, listed or
    removed on the host; only the argv the sweep would issue is observed.
    """

    def setUp(self):
        super().setUp()
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job")) for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
        FakeSandbox.calls = []

    def _runtime(self, calls):
        def fake(argv, **_):
            calls.append(list(argv))
            if "rm" in argv:
                return Result(0, "", "", "exited", 0.0)
            return Result(0, "abc123 def456\n", "", "exited", 0.0)
        return fake

    def _removals(self, calls):
        return [argv for argv in calls if "rm" in argv]

    def test_a_sweep_refuses_while_a_check_job_runs(self):
        ctx = self.context()
        check_jobs.submit(ctx)
        calls = []
        seen = {}
        runtime = self._runtime(calls)
        project_root = self.project.root
        config = self.config
        exclusion = check_slot_exclusion(project_root, 0)

        class Sweeping(FakeSandbox):
            def execute(self, workspace, script, **kwargs):
                # The sweep runs while the job's own container is live.
                seen['held'] = held(exclusion)
                with patch("mizu.sandbox.run", side_effect=runtime):
                    seen['sweep'] = cleanup(config, project_root, None)
                return super().execute(workspace, script, **kwargs)

        result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Sweeping)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(seen['held'])
        self.assertEqual(seen['sweep'], {"removed": 0, "skipped": "role-active"})
        self.assertEqual(self._removals(calls), [])

    def test_a_sweep_removes_leftovers_once_the_job_ended(self):
        ctx = self.context()
        check_jobs.submit(ctx)
        check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        calls = []
        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = cleanup(self.config, self.project.root, None)
        self.assertEqual(result, {"removed": 2})
        self.assertEqual(len(self._removals(calls)), 1)

    def test_the_runner_removes_its_own_container_after_a_failure(self):
        ctx = self.context()
        check_jobs.submit(ctx)
        calls = []

        class Broken(FakeSandbox):
            def execute(self, *args, **kwargs):
                raise Denied('Container cleanup failed')

        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Broken)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(len(self._removals(calls)), 1)

    def test_a_job_does_not_start_while_the_runner_slot_is_held(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with lock(check_slot_exclusion(self.project.root, 0), blocking=False):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        self.assertEqual(result['status'], 'busy')
        self.assertFalse(FakeSandbox.calls)
        self.assertEqual(check_jobs.read(self.project, job['id'])['status'], 'queued')

    def test_a_failed_job_leaves_no_exclusion_of_its_own(self):
        # One exclusion file per job would live as long as the project, and
        # every sweep would have to open all of them.
        ctx = self.context()
        check_jobs.submit(ctx)
        calls = []

        class Broken(FakeSandbox):
            def execute(self, *args, **kwargs):
                raise Denied('Container cleanup failed')

        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = check_jobs.run_one(self.project, threading.Event(), sandbox_factory=Broken)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(self._exclusions(), [])

    def test_the_recovery_leaves_no_exclusion_of_its_own(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        fault = self.config.data / 'check-slots/0.fault.json'
        fault.parent.mkdir(parents=True, exist_ok=True)
        write_json(fault, {'project': self.project.name, 'id': job['id']})
        calls = []
        with patch("mizu.sandbox.run", side_effect=self._runtime(calls)):
            result = check_jobs.recover(self.project)
        self.assertEqual(result['recovered'], [job['id']])
        self.assertEqual(self._exclusions(), [])

    def _exclusions(self):
        """The check exclusions that are not one of the compute slots."""
        locks = self.project.root / 'locks'
        found = sorted(path.name for path in locks.glob('run-check-*.lock'))
        return [name for name in found if not name.startswith('run-check-slot-')]

    def test_a_project_does_not_accumulate_one_exclusion_per_job(self):
        # The sweep takes every exclusion that exists, so a project that
        # keeps one file per job would eventually fail to sweep at all. The
        # runner's exclusions are bounded by the number of compute slots.
        ctx = self.context()
        for _ in range(3):
            check_jobs.submit(self.context())
            check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        locks = self.project.root / 'locks'
        found = [path.name for path in locks.glob('run-check-*.lock')]
        self.assertLessEqual(len(found), self.config.limits.parallel_checks)
        self.assertEqual(self._exclusions(), [])


class CacheInputTests(Fixture):
    """Bounded fake execution of preserved inputs, independent of dependencies."""
    def setUp(self):
        super().setUp()
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, 'check_job'))
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles,
            exclude=(*self.config.exclude, 'target'),
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True, check_cache_paths=('target',)))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude
        self.cache = self.project.root / 'check-cache' / check_jobs.execution_key(self.config) / 'target'
        FakeSandbox.calls = []

    def fingerprint(self):
        with check_jobs.cache_guard(self.project):
            return check_jobs.cache_input(self.project)

    def run_job(self, factory=FakeSandbox):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=factory)

    def seed(self, content='one'):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / 'object').write_text(content)

    def test_absent_empty_and_changed_bytes_are_distinct_inputs(self):
        environment = check_jobs.environment(self.project)
        absent = self.fingerprint()
        self.assertIsNotNone(absent)
        # Parent directory presence is not presence of a configured input.
        self.cache.parent.mkdir(parents=True)
        self.assertEqual(self.fingerprint(), absent)
        self.cache.mkdir()
        empty = self.fingerprint()
        self.seed()
        present = self.fingerprint()
        (self.cache / 'object').write_text('two')
        changed = self.fingerprint()
        self.assertEqual(len({absent, empty, present, changed}), 4)
        self.assertEqual(check_jobs.environment(self.project), environment)
        self.assertEqual(self.fingerprint(), changed)
        # Exercise the portable read fallback even on POSIX.
        with patch('mizu.platform.HAS_OPENAT', False):
            self.assertEqual(self.fingerprint(), changed)

    def test_owned_cache_queues_and_binds_before_staging(self):
        from mizu import acceptance
        self.seed('before')
        ctx = self.context()
        owner = check_jobs.submit(self.context())
        entered, release = threading.Event(), threading.Event()
        outcome = []
        class Owner(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                (workspace / 'target/object').write_text('execution-start')
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test barrier timed out')
                return super().execute(workspace, script, **kwargs)
        thread = threading.Thread(target=lambda: outcome.append(self.run_job(Owner)))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            with patch.object(check_jobs, 'cache_input', side_effect=AssertionError('unexpected fingerprint')):
                started = time.monotonic()
                pending = check_jobs.submit(ctx)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(check_jobs.submit(ctx)['id'], pending['id'])
            self.assertNotEqual(pending['id'], owner['id'])
            path = check_jobs.root(self.project) / pending['id'] / 'job.json'
            raw = acceptance.read_job(self.project, pending['id'])
            self.assertTrue(raw['cache_binding_pending'])
            self.assertNotIn('cache_input', raw)
            self.assertNotIn('shared_key', raw)
            self.assertFalse(check_jobs.reusable(raw))
            self.assertNotIn(pending['id'], [j['id'] for j in check_jobs.available(self.context('reviewer'), validate=False)])
            self.assertEqual(acceptance._inspect_evidence(self.project, raw), (None, None))
            self.assertEqual(len(list(check_jobs.root(self.project).glob('*/job.json'))), 2)
            self.assertEqual(check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)['status'], 'busy')
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0]['status'], 'completed')
        expected = self.fingerprint()
        self.assertNotIn(pending['id'], [j['id'] for j in check_jobs.available(self.context('reviewer'))])
        with self.assertRaises(Denied):
            check_jobs.join(self.context('reviewer'), pending['id'])
        # Replay before execution remains the same request even with the lock free.
        with patch.object(check_jobs, 'cache_input', side_effect=AssertionError('retry must not fingerprint')):
            self.assertEqual(check_jobs.submit(ctx)['id'], pending['id'])
        real_replace = Path.replace
        def observe_move(source, target):
            if source == self.cache:
                stored = read_json(path)
                self.assertEqual(stored['cache_input'], expected)
                self.assertNotIn('cache_binding_pending', stored)
                self.assertEqual(stored['status'], 'running')
                with self.assertRaises(check_jobs.CacheOwned):
                    with check_jobs.cache_guard(self.project, blocking=False):
                        pass
            return real_replace(source, target)
        with patch.object(Path, 'replace', observe_move):
            self.assertEqual(self.run_job()['status'], 'completed')
        result = check_jobs.result(ctx, pending['id'])
        self.assertEqual(result['result']['cache_input'], expected)
        self.assertTrue(result['accepted_for_current_source'])
        self.assertEqual((self.cache / 'object').read_text(), 'execution-start')
        (self.cache / 'object').write_text('later')
        later = check_jobs.submit(ctx)
        self.assertNotEqual(later['id'], pending['id'])
        self.assertNotIn('shared_job', later)

    def test_replay_after_binding_and_start_keeps_request_identity(self):
        from mizu import acceptance
        self.seed('initial')
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        request = acceptance.read_job(self.project, job['id'])['cache_request']
        self.seed('actual-start')
        expected = self.fingerprint()
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        class Held(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test barrier timed out')
                return super().execute(workspace, script, **kwargs)
        runner = threading.Thread(target=lambda: outcomes.append(self.run_job(Held)))
        runner.start()
        try:
            self.assertTrue(entered.wait(10))
            stored = acceptance.read_job(self.project, job['id'])
            self.assertEqual(stored['status'], 'running')
            self.assertEqual(stored['cache_input'], expected)
            self.assertEqual(stored['cache_request'], request)
            self.assertNotIn('cache_binding_pending', stored)
            with patch.object(check_jobs, 'cache_input', side_effect=AssertionError('retry fingerprinted')):
                self.assertEqual(check_jobs.submit(ctx)['id'], job['id'])
        finally:
            release.set()
            runner.join(10)
        self.assertFalse(runner.is_alive())
        self.assertEqual(outcomes[0]['status'], 'completed')
        self.assertEqual(read_json(path)['cache_request'], request)
        new = check_jobs.submit(ctx)
        self.assertNotEqual(new['id'], job['id'])
        self.assertEqual(new.get('shared_job'), job['id'])
        self.assertEqual(new['status'], 'completed')
        self.assertTrue(check_jobs.result(ctx, new['id'])['accepted_for_current_source'])
        self.assertEqual(check_jobs.result(ctx, job['id'])['status'], 'completed')

    def test_request_identity_distinguishes_inputs_and_owner(self):
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            first = check_jobs.submit(ctx)
            self.assertEqual(first['id'], check_jobs.submit(ctx)['id'])
            self.assertNotEqual(first['id'], check_jobs.submit(ctx, fresh=True)['id'])
            with patch.object(check_jobs, 'environment', return_value='a' * 64):
                self.assertNotEqual(first['id'], check_jobs.submit(ctx)['id'])
            previous = self.project.config
            try:
                self.project.config = dataclasses.replace(previous, sandbox=dataclasses.replace(
                    previous.sandbox, check_input_bytes=previous.sandbox.check_input_bytes - 1))
                self.assertNotEqual(first['id'], check_jobs.submit(ctx)['id'])
            finally:
                self.project.config = previous
            reviewer = self.context('reviewer')
            reviewer.run_dir = ctx.run_dir
            reviewer.role = dataclasses.replace(reviewer.role, capabilities=(*reviewer.role.capabilities, 'verify'))
            self.assertNotEqual(first['id'], check_jobs.submit(reviewer)['id'])
            (ctx.workspace / 'app.py').write_text('changed source\n')
            self.assertNotEqual(first['id'], check_jobs.submit(ctx)['id'])

    def test_reuse_disabled_terminal_resubmission_creates_new_demand(self):
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=False))
        self.project.config = self.config
        ctx = self.context()
        self.seed('stable')
        first = check_jobs.submit(ctx)
        self.assertNotIn('shared_job', first)
        self.assertIsNone(check_jobs.read(self.project, first['id'])['environment'])
        first_input = self.fingerprint()
        self.assertEqual(check_jobs.read(self.project, first['id'])['cache_input'], first_input)
        self.assertEqual(self.run_job()['status'], 'completed')

        # A terminal request is not replayed; without reuse, a new demand runs.
        second = check_jobs.submit(ctx)
        self.assertNotEqual(second['id'], first['id'])
        self.assertNotIn('shared_job', second)
        self.assertEqual(check_jobs.read(self.project, second['id'])['cache_input'], first_input)
        self.assertEqual(self.run_job()['status'], 'completed')

        # Changed cache after terminal creates another demand; retries of that
        # unresolved request coalesce to its stable request ID.
        (self.cache / 'object').write_text('changed')
        third = check_jobs.submit(ctx)
        self.assertNotEqual(third['id'], second['id'])
        self.assertNotIn('shared_job', third)
        self.assertEqual(check_jobs.submit(ctx)['id'], third['id'])
        stored = check_jobs.read(self.project, third['id'])
        self.assertNotEqual(stored['cache_input'], first_input)
        self.assertEqual(stored['fresh'], False)
        self.assertEqual(stored['environment_bounds'],
                         {'files': self.config.sandbox.check_input_files,
                          'bytes': self.config.sandbox.check_input_bytes})
        self.assertEqual(stored['inputs_bound_required'], False)

    def test_bound_distinct_requests_do_not_coalesce(self):
        self.seed('same-cache')
        ctx = self.context()
        first = check_jobs.submit(ctx)
        self.assertNotEqual(check_jobs.submit(ctx, fresh=True)['id'], first['id'])
        previous = self.project.config
        try:
            self.project.config = dataclasses.replace(previous, sandbox=dataclasses.replace(
                previous.sandbox, check_input_bytes=previous.sandbox.check_input_bytes - 1))
            self.assertNotEqual(check_jobs.submit(ctx)['id'], first['id'])
        finally:
            self.project.config = previous
        reviewer = self.context('reviewer')
        reviewer.run_dir = ctx.run_dir
        reviewer.role = dataclasses.replace(reviewer.role, capabilities=(*reviewer.role.capabilities, 'verify'))
        self.assertNotEqual(check_jobs.submit(reviewer)['id'], first['id'])
        try:
            self.project.config = dataclasses.replace(previous, sandbox=dataclasses.replace(
                previous.sandbox, check_reuse=False))
            self.assertNotEqual(check_jobs.submit(ctx)['id'], first['id'])
        finally:
            self.project.config = previous
        self.assertEqual(check_jobs.submit(ctx)['id'], first['id'])

    def test_lookup_refuses_corrupt_request_identity_fields(self):
        from mizu import acceptance
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
            path = check_jobs.root(self.project) / job['id'] / 'job.json'
            original = read_json(path)
            # A valid but different mode still cannot coalesce with this demand.
            write_json(path, {**original, 'fresh': True})
            self.assertEqual(acceptance.read_job(self.project, job['id'])['fresh'], True)
            with self.assertRaises(Denied):
                check_jobs.submit(ctx)
            write_json(path, original)
            for changes in ({'fresh': None}, {'fresh': 1}, {'fresh': 'yes'},
                            {'environment_bounds': None}, {'environment_bounds': {'files': 1}},
                            {'environment_bounds': {'files': True, 'bytes': 1}},
                            {'environment_bounds': {'files': 1, 'bytes': 1, 'extra': 1}},
                            {'inputs_bound_required': None}, {'inputs_bound_required': 1}):
                with self.subTest(changes=changes):
                    write_json(path, {**original, **changes})
                    with self.assertRaises(acceptance.InvalidProducer):
                        acceptance.read_job(self.project, job['id'])
                    with self.assertRaises(Denied):
                        check_jobs.submit(ctx)
            write_json(path, original)
            self.assertEqual(check_jobs.submit(ctx)['id'], job['id'])

    def test_pending_and_request_scoped_to_configured_cache(self):
        from mizu import acceptance
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
            path = check_jobs.root(self.project) / job['id'] / 'job.json'
            raw = read_json(path)
            previous = self.project.config
            try:
                self.project.config = dataclasses.replace(previous, sandbox=dataclasses.replace(
                    previous.sandbox, check_cache_paths=()))
                with self.assertRaises(acceptance.InvalidProducer):
                    acceptance.read_job(self.project, job['id'])
                bound = {k: v for k, v in raw.items() if k != 'cache_binding_pending'}
                bound.update(cache_input='a' * 64, status='queued')
                bound.pop('shared_key', None)
                write_json(path, bound)
                with self.assertRaises(acceptance.InvalidProducer):
                    acceptance.read_job(self.project, job['id'])
            finally:
                self.project.config = previous
            write_json(path, raw)
            self.assertTrue(acceptance.read_job(self.project, job['id'])['cache_binding_pending'])

    def test_shared_subscription_with_pending_or_unbound_original_is_refused(self):
        from mizu import acceptance
        self.seed('shared-original')
        first_ctx = self.context()
        first = check_jobs.submit(first_ctx)
        second_ctx = self.context()
        second = check_jobs.submit(second_ctx)
        self.assertEqual(second['shared_job'], first['id'])
        orig_path = check_jobs.root(self.project) / first['id'] / 'job.json'
        orig_raw = read_json(orig_path)
        # Corrupt the original to a pending marker: the subscription is impossible.
        pending = {**orig_raw}
        pending.pop('cache_input', None)
        pending.pop('shared_key', None)
        pending['cache_binding_pending'] = 1
        pending['status'] = 'queued'
        write_json(orig_path, pending)
        with self.assertRaises(Denied):
            check_jobs.submit(second_ctx)
        # Corrupt the original to an unbound legacy record: also refused, never reused.
        unbound = {**orig_raw}
        unbound.pop('cache_input', None)
        write_json(orig_path, unbound)
        with self.assertRaises(Denied):
            check_jobs.submit(second_ctx)
        # Restore and verify the subscription is usable again.
        write_json(orig_path, orig_raw)
        self.assertEqual(check_jobs.submit(second_ctx)['id'], second['id'])
        self.assertFalse(check_jobs.reusable(acceptance.read_job(self.project, second['id'])) is True and 'cache_binding_pending' in acceptance.read_job(self.project, second['id']))

    def test_subscription_stores_subscriber_request_identity(self):
        from mizu import acceptance
        self.seed('sub-fields')
        first_ctx = self.context()
        first = check_jobs.submit(first_ctx)
        first_raw = acceptance.read_job(self.project, first['id'])
        previous = self.project.config
        try:
            self.project.config = dataclasses.replace(previous, sandbox=dataclasses.replace(
                previous.sandbox, check_input_bytes=previous.sandbox.check_input_bytes - 1))
            second_ctx = self.context()
            second = check_jobs.submit(second_ctx)
            self.assertEqual(second.get('shared_job'), first['id'])
            sub = acceptance.read_job(self.project, second['id'])
            self.assertNotEqual(sub['cache_request'], first_raw['cache_request'])
            self.assertEqual(sub['fresh'], False)
            self.assertEqual(sub['environment_bounds'],
                             {'files': previous.sandbox.check_input_files,
                              'bytes': previous.sandbox.check_input_bytes - 1})
            self.assertEqual(sub['inputs_bound_required'], previous.sandbox.check_reuse)
            self.assertNotEqual(sub['environment_bounds'], first_raw['environment_bounds'])
        finally:
            self.project.config = previous
        # Fresh mode is also subscriber-owned: a fresh execution is reused by a later non-fresh demand.
        self.seed('fresh-sub')
        fresh_ctx = self.context()
        fresh_job = check_jobs.submit(fresh_ctx, fresh=True)
        later_ctx = self.context()
        shared = check_jobs.submit(later_ctx)
        self.assertEqual(shared['shared_job'], fresh_job['id'])
        sub = acceptance.read_job(self.project, shared['id'])
        orig = acceptance.read_job(self.project, fresh_job['id'])
        self.assertEqual(orig['fresh'], True)
        self.assertEqual(sub['fresh'], False)
        self.assertNotEqual(sub['cache_request'], orig['cache_request'])

    def test_changed_cache_binds_but_request_identity_stable(self):
        from mizu import acceptance
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=False))
        self.project.config = self.config
        ctx = self.context()
        self.seed('enqueue-identity')
        job = check_jobs.submit(ctx)
        before = acceptance.read_job(self.project, job['id'])
        enqueue_input = before['cache_input']
        request = before['cache_request']
        fresh = before['fresh']
        bounds = dict(before['environment_bounds'])
        reuse = before['inputs_bound_required']
        self.seed('execution-identity')
        execution_input = self.fingerprint()
        self.assertNotEqual(execution_input, enqueue_input)
        self.assertEqual(self.run_job()['status'], 'completed')
        after = acceptance.read_job(self.project, job['id'])
        self.assertEqual(after['cache_request'], request)
        self.assertEqual(after['fresh'], fresh)
        self.assertEqual(after['environment_bounds'], bounds)
        self.assertEqual(after['inputs_bound_required'], reuse)
        self.assertEqual(after['cache_input'], execution_input)
        self.assertNotIn('cache_binding_pending', after)

    def test_cache_subscription_tracks_execution_start_binding(self):
        self.seed('enqueue')
        first_ctx = self.context()
        first = check_jobs.submit(first_ctx)
        second_ctx = self.context()
        second = check_jobs.submit(second_ctx)
        self.assertEqual(second['shared_job'], first['id'])
        self.seed('start')
        expected = self.fingerprint()
        self.assertEqual(self.run_job()['status'], 'completed')
        self.assertEqual(check_jobs.read(self.project, second['id'])['cache_input'], expected)
        self.assertTrue(check_jobs.result(second_ctx, second['id'])['accepted_for_current_source'])

    def test_cancelled_request_is_not_replayed(self):
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            first = check_jobs.submit(ctx)
            check_jobs.cancel(ctx, first['id'])
            second = check_jobs.submit(ctx)
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(check_jobs.submit(ctx)['id'], second['id'])
        self.assertEqual(check_jobs.read(self.project, first['id'])['status'], 'cancelled')

    def test_started_request_is_interrupted_not_rebound_on_recovery(self):
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        raw = read_json(path)
        raw.pop('cache_binding_pending')
        raw.update(status='running', cache_input='a' * 64, shared_key='b' * 64)
        write_json(path, raw)
        with patch.object(check_jobs, 'cache_input', side_effect=AssertionError('rebound started execution')), \
                patch.object(check_jobs, 'cleanup'):
            self.assertEqual(self.run_job()['status'], 'interrupted')
        stored = read_json(path)
        self.assertEqual(stored['cache_input'], 'a' * 64)
        self.assertEqual(stored['shared_key'], 'b' * 64)
        self.assertFalse(FakeSandbox.calls)

    def test_lookup_refuses_mismatched_request_record(self):
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
            path = check_jobs.root(self.project) / job['id'] / 'job.json'
            raw = read_json(path)
            raw['code_digest'] = 'a' * 64
            write_json(path, raw)
            with self.assertRaises(Denied):
                check_jobs.submit(ctx)

    def test_legacy_missing_binding_without_request_marker_is_refused(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        raw = read_json(path)
        raw.pop('cache_input')
        raw.pop('cache_request')
        write_json(path, raw)
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
        self.assertNotIn('acceptance_scope', read_json(path))

    def test_lookup_refuses_malformed_unrelated_record(self):
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            first = check_jobs.submit(ctx)
            path = check_jobs.root(self.project) / first['id'] / 'job.json'
            path.write_text('{invalid')
            with self.assertRaises(Denied):
                check_jobs.submit(ctx, fresh=True)

    def test_pending_marker_validation_cancel_and_unbindable(self):
        from mizu import acceptance
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        raw = read_json(path)
        for changes in ({'cache_binding_pending': False}, {'cache_binding_pending': True}, {'cache_binding_pending': 2},
                        {'cache_input': None}, {'shared_key': None}, {'kind': 'experiment'},
                        {'status': 'completed'}, {'cache_request': None},
                        {'acceptance_scope': {}}):
            with self.subTest(changes=changes):
                write_json(path, {**raw, **changes})
                with self.assertRaises(acceptance.InvalidProducer):
                    acceptance.read_job(self.project, job['id'])
        write_json(path, raw)
        self.seed('unbounded')
        with patch.object(check_jobs, 'cache_input', return_value=None):
            self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
        self.assertNotIn('acceptance_scope', read_json(path))
        with check_jobs.cache_guard(self.project):
            other = check_jobs.submit(ctx)
            self.assertNotEqual(other['id'], job['id'])
            check_jobs.cancel(ctx, other['id'])
        self.assertEqual(check_jobs.read(self.project, other['id'])['status'], 'cancelled')

    def test_pending_requests_obey_queue_bound_and_replay(self):
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, check_pending=1))
        self.project.config = self.config
        ctx = self.context()
        with check_jobs.cache_guard(self.project):
            job = check_jobs.submit(ctx)
            self.assertEqual(check_jobs.submit(ctx)['id'], job['id'])
            with self.assertRaises(Busy):
                check_jobs.submit(ctx, fresh=True)
            check_jobs.cancel(ctx, job['id'])
            with self.assertRaises(Busy):
                check_jobs.submit(ctx)
            with self.assertRaises(Busy):
                check_jobs.submit(ctx, fresh=True)
        self.assertEqual(self.run_job()['status'], 'cancelled')
        self.assertNotEqual(check_jobs.submit(ctx, fresh=True)['id'], job['id'])

    def test_non_ownership_errors_do_not_queue(self):
        ctx = self.context()
        with patch.object(check_jobs, 'cache_input', side_effect=Denied('bad cache')):
            with self.assertRaises(Denied):
                check_jobs.submit(ctx)
        self.assertEqual(list(check_jobs.root(self.project).glob('*/job.json')), [])

    def test_same_run_replay_survives_cache_changes_until_terminal(self):
        ctx = self.context()
        absent = check_jobs.submit(ctx)
        self.seed()
        self.assertEqual(check_jobs.submit(ctx)['id'], absent['id'])
        self.assertEqual(self.run_job()['status'], 'completed')
        (self.cache / 'object').write_text('two')
        changed = check_jobs.submit(ctx)
        self.assertNotEqual(changed['id'], absent['id'])
        self.assertNotIn('shared_job', changed)
        self.assertEqual(check_jobs.submit(ctx)['id'], changed['id'])

    def test_shared_candidates_and_join_revalidate_cache_input(self):
        self.seed()
        first = check_jobs.submit(self.context())
        reader = self.context('reviewer')
        self.assertEqual(check_jobs.available(reader)[0]['id'], first['id'])
        self.assertEqual(check_jobs.join(reader, first['id'])['shared_job'], first['id'])
        (self.cache / 'object').write_text('two')
        self.assertEqual(check_jobs.available(reader), [])
        with self.assertRaises(Denied):
            check_jobs.join(reader, first['id'])
        new = check_jobs.submit(self.context())
        self.assertNotIn('shared_job', new)

    def test_join_rechecks_after_discovery_before_queue_lookup(self):
        first = check_jobs.submit(self.context())
        reader = self.context('reviewer')
        available = check_jobs.available
        def changed(*args, **kwargs):
            candidates = available(*args, **kwargs)
            self.seed()
            return candidates
        with patch.object(check_jobs, 'available', side_effect=changed):
            with self.assertRaises(Denied):
                check_jobs.join(reader, first['id'])

    def test_changed_queued_input_binds_at_execution_start(self):
        observed = []
        class Observing(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                artifact = workspace / 'target/object'
                observed.append(artifact.read_text() if artifact.exists() else None)
                return super().execute(workspace, script, **kwargs)
        self.seed('one')
        ctx = self.context()
        job = check_jobs.submit(ctx)
        enqueue_input = check_jobs.read(self.project, job['id'])['cache_input']
        self.assertEqual(enqueue_input, self.fingerprint())
        (self.cache / 'object').write_text('two')
        execution_input = self.fingerprint()
        self.assertNotEqual(execution_input, enqueue_input)
        self.assertEqual(self.run_job(Observing)['status'], 'completed')
        # The runner staged the changed cache, not the enqueue-time tree.
        self.assertEqual(observed, ['two'])
        record = check_jobs.read(self.project, job['id'])
        self.assertEqual(record['cache_input'], execution_input)
        self.assertEqual(record['status'], 'completed')
        result = check_jobs.result(ctx, job['id'])
        self.assertTrue(result['accepted_for_current_source'])
        self.assertEqual(result['result']['cache_input'], execution_input)
        self.assertEqual(record['acceptance_scope']['cache_input'], execution_input)
        self.assertEqual(record['shared_key'], check_jobs.read(self.project, job['id'])['shared_key'])
        # Matching current cache reuses the execution proof; a changed cache does not.
        reader = self.context('reviewer')
        self.assertEqual(check_jobs.available(reader)[0]['id'], job['id'])
        self.assertEqual(check_jobs.join(reader, job['id'])['shared_job'], job['id'])
        self.assertEqual(check_jobs.submit(self.context())['shared_job'], job['id'])
        (self.cache / 'object').write_text('three')
        self.assertEqual(check_jobs.available(reader), [])
        with self.assertRaises(Denied):
            check_jobs.join(reader, job['id'])
        fresh = check_jobs.submit(self.context())
        self.assertNotIn('shared_job', fresh)

    def test_execution_time_unbindable_cache_fails_closed(self):
        self.seed('one')
        job = check_jobs.submit(self.context())
        enqueue_input = check_jobs.read(self.project, job['id'])['cache_input']
        self.assertIsNotNone(enqueue_input)
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_input_bytes=2))
        self.project.config = self.config
        self.assertIsNone(self.fingerprint())
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
        self.assertNotIn('acceptance_scope', check_jobs.read(self.project, job['id']))

    def test_old_unbound_job_cannot_execute_or_join(self):
        owner = self.context()
        job = check_jobs.submit(owner)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        old = read_json(path)
        old.pop('cache_input')
        write_json(path, old)
        reader = self.context('reviewer')
        self.assertEqual(check_jobs.available(reader), [])
        with self.assertRaises(Denied):
            check_jobs.join(reader, job['id'])
        with self.assertRaises(Denied):
            check_jobs.submit(owner)
        # Run the old job before creating another queued demand: job timestamps
        # have second precision, so relying on their order would make this test
        # nondeterministic when both submissions happen in one second.
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(FakeSandbox.calls)
        later = self.context()
        independent = check_jobs.submit(later)
        self.assertNotIn('shared_job', independent)
        check_jobs.cancel(later, independent['id'])

    def test_over_bound_inputs_are_refused_before_queueing(self):
        self.seed()
        for limits in ({'check_input_files': 1}, {'check_input_bytes': 2}):
            with self.subTest(limits=limits):
                config = dataclasses.replace(self.config,
                    sandbox=dataclasses.replace(self.config.sandbox, **limits))
                self.project.config = config
                self.assertIsNone(self.fingerprint())
                ctx = self.context()
                before = list(check_jobs.root(self.project).glob('*/job.json'))
                with patch.object(check_jobs, '_make_room') as room, patch.object(self.project.snapshots, 'materialize') as materialize:
                    for fresh in (False, True):
                        with self.assertRaisesRegex(Denied, 'Cannot bind.*verification not queued'):
                            check_jobs.submit(ctx, fresh=fresh)
                    room.assert_not_called()
                    materialize.assert_not_called()
                self.assertEqual(list(check_jobs.root(self.project).glob('*/job.json')), before)
                # Custom jobs do not use the verification cache.
                custom = check_jobs.submit(ctx, 'echo ' + next(iter(limits)))
                self.assertEqual(self.run_job()['status'], 'completed')
                self.assertNotIn('acceptance_scope', check_jobs.read(self.project, custom['id']))

    def link(self, source, destination):
        try:
            os.link(source, destination)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Hardlinks unavailable: ' + str(exc))

    def test_internal_hardlinks_bind_topology_and_metadata_not_inode_numbers(self):
        self.seed()
        artifact = self.cache / 'object'
        directory = self.cache / 'incremental'
        directory.mkdir()
        alias = directory / 'object'
        self.link(artifact, alias)
        bound = self.fingerprint()
        self.assertIsNotNone(bound)
        self.assertEqual(self.fingerprint(), bound)
        with patch('mizu.platform.HAS_OPENAT', False):
            self.assertEqual(self.fingerprint(), bound)
        # Workspace moves change ctime, but not command-visible input identity.
        moved = self.cache.with_name('moved')
        self.cache.rename(moved)
        moved.rename(self.cache)
        self.assertEqual(self.fingerprint(), bound)
        info = artifact.stat()
        os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns + 2000000000))
        self.assertNotEqual(self.fingerprint(), bound)
        os.utime(artifact, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(self.fingerprint(), bound)
        if os.name == 'posix':
            artifact.chmod(0o700)
            self.assertNotEqual(self.fingerprint(), bound)
            artifact.chmod(info.st_mode & 0o7777)
            self.assertEqual(self.fingerprint(), bound)
        # A copy with identical bytes/mode/mtime is not the same topology.
        parent_info = directory.stat()
        cache_info = self.cache.stat()
        alias.unlink()
        alias.write_bytes(artifact.read_bytes())
        alias.chmod(info.st_mode & 0o7777)
        os.utime(alias, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.utime(directory, ns=(parent_info.st_atime_ns, parent_info.st_mtime_ns))
        copied = self.fingerprint()
        self.assertIsNotNone(copied)
        self.assertNotEqual(copied, bound)
        # Recreating an equivalent group on a new inode preserves the receipt.
        artifact.unlink()
        self.link(alias, artifact)
        os.utime(self.cache, ns=(cache_info.st_atime_ns, cache_info.st_mtime_ns))
        self.assertEqual(self.fingerprint(), bound)

    def test_hardlinks_across_configured_trees_are_wholly_contained(self):
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_cache_paths=('target', 'objects')))
        self.project.config = self.config
        self.cache = self.project.root / 'check-cache' / check_jobs.execution_key(self.config) / 'target'
        self.seed()
        objects = self.cache.with_name('objects')
        objects.mkdir()
        self.link(self.cache / 'object', objects / 'alias')
        self.assertIsNotNone(self.fingerprint())

    def test_additional_external_hardlink_is_unbound_and_not_queued(self):
        self.seed()
        self.link(self.cache / 'object', self.cache / 'alias')
        self.assertIsNotNone(self.fingerprint())
        # Even a name in an unconfigured tree beneath the same cache base escapes.
        outside = self.cache.with_name('unconfigured')
        outside.mkdir()
        self.link(self.cache / 'object', outside / 'alias')
        self.assertIsNone(self.fingerprint())
        for reuse in (True, False):
            self.config = dataclasses.replace(self.config,
                sandbox=dataclasses.replace(self.config.sandbox, check_reuse=reuse))
            self.project.config = self.config
            with self.assertRaisesRegex(Denied, 'Cannot bind.*verification not queued'):
                check_jobs.submit(self.context())
        self.assertEqual(list(check_jobs.root(self.project).glob('*/job.json')), [])
        self.assertFalse(FakeSandbox.calls)

    def test_hardlink_names_count_against_file_and_byte_bounds(self):
        self.seed()
        self.link(self.cache / 'object', self.cache / 'alias')
        for limits, expected in (({'check_input_files': 2}, False),
                                 ({'check_input_bytes': 5}, False),
                                 ({'check_input_files': 3, 'check_input_bytes': 6}, True)):
            with self.subTest(limits=limits):
                self.project.config = dataclasses.replace(self.config,
                    sandbox=dataclasses.replace(self.config.sandbox, **limits))
                self.assertEqual(self.fingerprint() is not None, expected)

    def test_hardlinked_input_keeps_source_and_execution_binding(self):
        self.seed()
        self.link(self.cache / 'object', self.cache / 'alias')
        bound = self.fingerprint()
        ctx = self.context()
        job = check_jobs.submit(ctx)
        raw = check_jobs.read(self.project, job['id'])
        self.assertEqual(raw['cache_input'], bound)
        self.assertEqual(raw['execution'], check_jobs.execution_key(self.config))
        self.assertEqual(raw['code_digest'], self.project.snapshots.capture_files(ctx.workspace)['code_digest'])
        self.assertEqual(self.run_job()['status'], 'completed')
        self.assertEqual(self.fingerprint(), bound)
        self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        (ctx.workspace / 'app.py').write_bytes(b'VALUE = 3\n')
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        (ctx.workspace / 'app.py').write_bytes(b'VALUE = 2\n')
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, command_seconds=self.config.limits.command_seconds + 1))
        self.project.config = self.config
        self.assertFalse(check_jobs.result(self.context(), job['id'])['accepted_for_current_source'])

    def test_external_link_added_during_traversal_is_unbound(self):
        self.seed()
        self.link(self.cache / 'object', self.cache / 'alias')
        (self.cache / 'z').mkdir()
        real_scandir = check_jobs.os.scandir
        calls = []
        def changed(node):
            calls.append(node)
            if len(calls) == 2:
                # Both names have been hashed before visiting z.
                os.link(self.cache / 'object', self.root / 'outside')
            return real_scandir(node)
        with patch.object(check_jobs.os, 'scandir', side_effect=changed):
            self.assertIsNone(self.fingerprint())
        self.assertEqual(len(calls), 2)

    def test_unsafe_and_unreadable_inputs_never_hash_as_absent(self):
        import os
        self.seed()
        with patch.object(check_jobs.os, 'scandir', side_effect=PermissionError('unreadable')):
            self.assertIsNone(self.fingerprint())
        artifact = self.cache / 'object'
        artifact.unlink()
        try:
            artifact.symlink_to(self.root / 'outside')
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symlinks unavailable: ' + str(exc))
        self.assertIsNone(self.fingerprint())
        artifact.unlink()
        external = self.root / 'outside'
        external.write_text('not a cache input')
        os.link(external, artifact)
        self.assertIsNone(self.fingerprint())
        artifact.unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(artifact)
            self.assertIsNone(self.fingerprint())
            artifact.unlink()
        self.cache.rmdir()
        self.cache.symlink_to(external.parent, target_is_directory=True)
        self.assertIsNone(self.fingerprint())

    def test_cache_lock_spans_input_hash_and_reuse_or_enqueue_decision(self):
        ctx = self.context()
        real_input = check_jobs.cache_input
        real_lock = check_jobs.lock
        cache_lock = self.project.root / 'locks/check-cache.lock'
        queue_lock = self.project.root / 'locks/check-jobs.lock'
        def fingerprint(project):
            self.assertTrue(held(cache_lock))
            self.assertFalse(held(queue_lock))
            return real_input(project)
        import contextlib
        @contextlib.contextmanager
        def guarded(path, **kwargs):
            if path == queue_lock:
                self.assertTrue(held(cache_lock))
                # A concurrent cache owner cannot slip in at queue lookup.
                with self.assertRaises(Busy):
                    with real_lock(cache_lock, blocking=False):
                        pass
            with real_lock(path, **kwargs):
                yield
        with patch.object(check_jobs, 'cache_input', side_effect=fingerprint), patch.object(check_jobs, 'lock', guarded):
            first = check_jobs.submit(ctx)
            self.assertEqual(check_jobs.submit(ctx)['id'], first['id'])
            self.assertEqual(check_jobs.submit(self.context())['shared_job'], first['id'])
        # Dispatch revalidates under cache exclusion, without queue exclusion.
        with patch.object(check_jobs, 'cache_input', side_effect=fingerprint):
            self.run_job()
        # Import checks dependencies outside queue exclusion, not live caches.
        real_environment = check_jobs.execution_environment
        def environment(project, job):
            self.assertFalse(held(queue_lock))
            return real_environment(project, job)
        with patch.object(check_jobs, 'cache_input', side_effect=AssertionError('Import must not hash outputs')), patch.object(check_jobs, 'execution_environment', side_effect=environment):
            self.assertTrue(check_jobs.result(ctx, first['id'])['accepted_for_current_source'])

    def test_mutating_build_cache_still_moves_back_and_is_next_input(self):
        observed = []
        class Compiler(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                artifact = workspace / 'target/object'
                observed.append(artifact.read_text() if artifact.exists() else None)
                artifact.parent.mkdir(exist_ok=True)
                artifact.write_text('built ' + str(len(observed)))
                return super().execute(workspace, script, **kwargs)
        ctx = self.context()
        initial_input = self.fingerprint()
        first = check_jobs.submit(ctx)
        self.assertTrue(self.run_job(Compiler)['passed'])
        result = check_jobs.result(ctx, first['id'])
        self.assertTrue(result['accepted_for_current_source'])
        record = result['result']
        self.assertEqual(record['cache_input'], initial_input)
        self.assertEqual(record['cache_output'], self.fingerprint())
        self.assertNotEqual(record['cache_input'], record['cache_output'])
        self.assertEqual(check_jobs.read(self.project, first['id'])['cache_input'], initial_input)
        self.assertEqual(ctx.verification, record)
        self.assertEqual(read_json(ctx.run_dir / 'verification.json'), record)
        self.assertEqual((self.cache / 'object').read_text(), 'built 1')
        reader = self.context('reviewer')
        self.assertEqual(check_jobs.available(reader), [])
        with self.assertRaises(Denied):
            check_jobs.join(reader, first['id'])
        second = check_jobs.submit(ctx)
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotIn('shared_job', second)
        self.assertEqual(check_jobs.read(self.project, second['id'])['cache_input'], record['cache_output'])
        self.assertTrue(self.run_job(Compiler)['passed'])
        result = check_jobs.result(ctx, second['id'])
        self.assertTrue(result['accepted_for_current_source'])
        self.assertEqual(result['result']['cache_input'], record['cache_output'])
        self.assertEqual(result['result']['cache_output'], self.fingerprint())
        self.assertEqual(observed, [None, 'built 1'])
        self.assertEqual((self.cache / 'object').read_text(), 'built 2')
        self.assertFalse((ctx.workspace / 'target').exists())

    def test_import_requires_bound_job_input_and_matching_echo_when_present(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        self.assertTrue(self.run_job()['passed'])
        directory = check_jobs.root(self.project) / job['id']
        original_job = read_json(directory / 'job.json')
        original_result = read_json(directory / 'result.json')
        legacy_result = {k: v for k, v in original_result.items()
                         if k not in ('cache_input', 'cache_output')}
        self.seed('later cache input')
        self.assertNotEqual(self.fingerprint(), original_job['cache_input'])
        for record in (original_result, legacy_result):
            write_json(directory / 'result.json', record)
            # The legacy result is usable only with its bound execution input.
            write_json(directory / 'job.json', original_job)
            self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
            for value in (None, '', 'unbound', 123):
                with self.subTest(echo='cache_input' in record, value=value):
                    write_json(directory / 'job.json', {**original_job, 'cache_input': value})
                    self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
            write_json(directory / 'job.json', {k: v for k, v in original_job.items() if k != 'cache_input'})
            self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        write_json(directory / 'job.json', original_job)
        for value in (None, 'f' * 64):
            write_json(directory / 'result.json', {**original_result, 'cache_input': value})
            self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])

    def test_unbindable_output_does_not_change_pass_or_input_identity(self):
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_input_bytes=4))
        self.project.config = self.config
        class Compiler(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                (workspace / 'target').mkdir()
                (workspace / 'target/object').write_text('built')
                return super().execute(workspace, script, **kwargs)
        ctx = self.context()
        initial_input = self.fingerprint()
        job = check_jobs.submit(ctx)
        self.assertTrue(self.run_job(Compiler)['passed'])
        result = check_jobs.result(ctx, job['id'])
        self.assertTrue(result['accepted_for_current_source'])
        self.assertTrue(result['result']['passed'])
        self.assertEqual(result['result']['cache_input'], initial_input)
        self.assertIsNone(result['result']['cache_output'])
        self.assertEqual(check_jobs.read(self.project, job['id'])['cache_input'], initial_input)
        self.assertEqual((self.cache / 'object').read_text(), 'built')
        self.assertEqual(check_jobs.available(self.context('reviewer')), [])
        with self.assertRaises(Denied):
            check_jobs.submit(ctx)

    def test_running_result_does_not_wait_for_its_cache_owner(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        write_json(path, {**read_json(path), 'status': 'running'})
        with lock(self.project.root / 'locks/check-cache.lock'):
            self.assertEqual(check_jobs.result(ctx, job['id'])['status'], 'running')

    def test_changed_cache_binds_at_execution_even_with_sharing_disabled(self):
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=False))
        self.project.config = self.config
        ctx = self.context()
        job = check_jobs.submit(ctx)
        self.assertIsNone(check_jobs.read(self.project, job['id'])['environment'])
        enqueue_input = check_jobs.read(self.project, job['id'])['cache_input']
        self.seed()
        execution_input = self.fingerprint()
        self.assertNotEqual(execution_input, enqueue_input)
        self.assertEqual(self.run_job()['status'], 'completed')
        self.assertTrue(FakeSandbox.calls)
        record = check_jobs.read(self.project, job['id'])
        self.assertEqual(record['cache_input'], execution_input)
        self.assertEqual(check_jobs.result(ctx, job['id'])['result']['cache_input'], execution_input)
        # Unrelated execution inputs still refuse even with sharing disabled.
        later_ctx = self.context()
        later = check_jobs.submit(later_ctx)
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, command_seconds=self.config.limits.command_seconds + 1))
        self.project.config = self.config
        self.assertEqual(self.run_job()['status'], 'refused')
        self.assertFalse(check_jobs.result(later_ctx, later['id'])['accepted_for_current_source'])
        self.assertNotIn('acceptance_scope', check_jobs.read(self.project, later['id']))

    def test_concurrent_edit_of_an_already_hashed_subtree_is_unbound(self):
        self.seed()
        (self.cache / 'z').mkdir()
        real_scandir = check_jobs.os.scandir
        calls = []
        def changed(node):
            calls.append(node)
            if len(calls) == 2:
                # object sorts before z, so this edit follows its file hash.
                (self.cache / 'object').write_text('changed during traversal')
            return real_scandir(node)
        with patch.object(check_jobs.os, 'scandir', side_effect=changed):
            self.assertIsNone(self.fingerprint())
        self.assertEqual(len(calls), 2)


class ExplicitWaitWakeTests(Fixture):
    """Opting out of automatic wakes preserves exact explicit-wait behavior."""

    def setUp(self):
        super().setUp()
        FakeSandbox.calls = []
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job"),
                                        check_result_wake=False)
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

    def submit(self):
        ctx = self.context()
        return ctx, check_jobs.submit(ctx)

    def run_job(self):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)

    def due(self, role='worker'):
        from mizu.runtime import _role_events_due
        return _role_events_due(self.project, self.project.snapshots.get(), role)

    def wait_path(self, role='worker'):
        return check_jobs.root(self.project) / (role + '.wait.json')

    def test_completed_job_without_wait_stays_visible_but_wakes_nobody(self):
        _, job = self.submit()
        self.run_job()
        self.assertEqual([entry['id'] for entry in check_jobs.pending(self.project, 'worker')], [job['id']])
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())

    def test_completed_exact_explicit_wait_wakes(self):
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.run_job()
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertTrue(self.due())

    def test_running_wait_is_a_hold_not_a_wake(self):
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())

    def test_expired_wait_wakes_nobody(self):
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'], wait_until=time.time() - 1)
        self.run_job()
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())

    def test_stale_wait_inputs_wake_nobody(self):
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.run_job()
        record = read_json(self.wait_path(), {})
        write_json(self.wait_path(), {**record, 'inputs': {'snapshot': 'stale'}})
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())

    def test_wait_on_another_job_ignores_unwaited_completion(self):
        _, first = self.submit()
        self.run_job()
        _, second = self.submit()
        check_jobs.set_wait(self.project, 'worker', second['id'])
        self.assertEqual([entry['id'] for entry in check_jobs.pending(self.project, 'worker')], [first['id']])
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())

    def test_wait_naming_another_roles_job_wakes_nobody(self):
        _, job = self.submit()
        check_jobs.submit(self.context('reviewer'), 'printf independent', kind='exec')
        self.run_job()
        self.run_job()
        record = {'id': job['id'], 'inputs': check_jobs.wait_signature(self.project),
                  'wait_until': time.time() + 600}
        write_json(self.wait_path('reviewer'), record)
        self.assertFalse(check_jobs.wake_ready(self.project, 'reviewer'))
        self.assertFalse(self.due('reviewer'))

    def test_acknowledged_completion_wakes_nobody(self):
        _, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.run_job()
        self.assertTrue(self.due())
        check_jobs.acknowledge(self.project, 'worker', {job['id']})
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(self.due())


class TerminalResultWakeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, roles={
            name: dataclasses.replace(role, capabilities=(*role.capabilities, 'check_job'))
            for name, role in self.config.roles.items()})
        self.project.config = self.config

    def submit(self, script='printf first', role='worker'):
        return check_jobs.submit(self.context(role), script, kind='experiment')

    def run_job(self):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)

    def mark(self, *jobs, role='worker'):
        check_jobs.mark_wake_delivered(self.project, role, [j['id'] for j in jobs], 'dispatch')

    def park(self):
        from mizu.runtime import Engine, should_run
        def finish(ctx, *_):
            ctx.handle('finish', {'outcome': 'continue', 'summary': 'No change',
                                  'state': ctx.snapshot['state']})
        driver = ScriptDriver(finish)
        engine = Engine(self.config, driver=driver)
        # Consume initial goal/operator inputs, then publish a no-progress finish.
        engine.run(self.project, 'worker')
        engine.run(self.project, 'worker')
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        return engine, driver

    def test_pending_check_reads_cannot_admit_a_followup_writer(self):
        from mizu.runtime import Engine, should_run
        self.park()
        # Even configured empty-unit allowances/timers cannot turn polling into work.
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, no_progress_continuations=100, no_progress_backoff_seconds=1))
        self.project.config = self.config
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                job = self.submit('printf ' + status)
                path = check_jobs.root(self.project) / job['id'] / 'job.json'
                write_json(path, {**read_json(path), 'status': status})
                def poll(ctx, *_):
                    self.assertEqual(ctx.handle('check_job', {
                        'action': 'result', 'id': job['id']})['status'], status)
                    ctx.handle('check_job', {'action': 'list'})
                    ctx.handle('check_job', {'action': 'logs', 'id': job['id']})
                    ctx.handle('files', {})
                    self.assertFalse(ctx.progress_tool)
                    self.assertFalse(getattr(ctx, "check_results_read", set()))
                    ctx.handle('finish', {'outcome': 'continue', 'summary': 'Still pending',
                        'state': 'Now: observed only.\nNext: Inspect pending result for ' + status})
                engine = Engine(self.config, driver=ScriptDriver(poll))
                self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
                snapshot = self.project.snapshots.get()
                self.assertFalse(snapshot['progress']['signals']['tool'])
                self.assertFalse(should_run(self.project, snapshot, role_name='worker',
                                            current_time=time.time() + 10000))
                self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
                role = dataclasses.replace(self.config.roles['worker'], on_change=True)
                self.assertEqual(engine._dispatch_state(self.project, 'worker', role,
                                                       event_trigger=False)['skip']['skipped'], 'unchanged')
        # A genuinely new trigger still admits the parked role.
        self.project.set_control(wake_generation='new-operator-input')
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))

    def test_no_progress_continue_wakes_once_without_acknowledging_unread_output(self):
        import json
        from mizu.runtime import should_run
        engine, driver = self.park()
        job = self.submit()
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.run_job()
        # Readiness checks are pure and an unrelated role gets no wake.
        for _ in range(2):
            self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
            self.assertFalse(check_jobs.wake_ready(self.project, 'reviewer'))
        status = self.project.status()
        self.assertEqual(status['progress']['disposition'], 'parked_no_progress')
        self.assertTrue(status['check_results']['worker'][0]['automatic_wake_pending'])
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        self.assertEqual(json.loads(driver.calls[-1][1])['check_jobs'][0]['id'], job['id'])
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        self.assertTrue(check_jobs.pending(self.project, 'worker'))
        self.assertFalse(self.project.status()['check_results']['worker'][0]['automatic_wake_pending'])
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
        # A wake marker cannot import verification or prevent ordinary retrieval/ack.
        ctx = self.context()
        self.assertIsNone(ctx.verification)
        check_jobs.result(ctx, job['id'])
        check_jobs.acknowledge(self.project, 'worker', ctx.check_results_read)
        self.assertNotIn('worker', self.project.status()['check_results'])
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))

    def test_multiple_results_and_arrival_during_dispatch_have_independent_markers(self):
        first = self.submit()
        self.run_job()
        second = self.submit('printf second')
        projection = check_jobs.prompt_projection(self.context())
        self.run_job()  # The second summary was queued in the captured prompt.
        terminal = [j['id'] for j in projection['check_jobs']
                    if j['status'] in check_jobs.TERMINAL_WAKE_STATUSES]
        check_jobs.mark_wake_delivered(self.project, 'worker', terminal, 'first-dispatch')
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.mark(second)
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.mark(first, second)  # Idempotent; unread output is never acknowledged.
        self.assertEqual(read_json(check_jobs.root(self.project) / first['id'] / 'wake.json')['run'],
                         'first-dispatch')
        self.assertEqual(len(check_jobs.pending(self.project, 'worker')), 2)
        third = self.submit('printf third')
        self.run_job()
        check_jobs.acknowledge(self.project, 'worker', {third['id']})
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))

    def test_undelivered_result_is_not_hidden_behind_a_full_page_of_seen_results(self):
        # Synthetic discovery page: no commands or sustained load needed.
        job = self.submit()
        self.run_job()
        original = check_jobs.read(self.project, job['id'])
        records = {}
        for i in range(33):
            identifier = f'{i:064x}'
            records[identifier] = {**original, 'id': identifier,
                                   'created_at': f'2026-01-01T00:00:{i:02d}+00:00'}
            if i:
                write_json(check_jobs.root(self.project) / identifier / 'wake.json', {})
        discovery = (records, [])
        with patch('mizu.acceptance.discover', return_value=discovery):
            projection = check_jobs.prompt_projection(self.context())
        self.assertEqual(len(projection['check_jobs']), 32)
        self.assertEqual(projection['check_jobs'][0]['id'], f'{0:064x}')
        self.assertNotIn(f'{0:064x}', [j['id'] for j in check_jobs.list_jobs(
            self.project, 'worker', discovery=discovery)])

    def test_result_wakes_for_every_finish_outcome_but_not_without_capability(self):
        from mizu.runtime import should_run
        self.park()
        job = self.submit()
        self.run_job()
        current = self.project.snapshots.get()
        snapshots = [{**current, 'outcome': outcome, 'wake_at': time.time() + 600}
                     for outcome in ('continue', 'bounded', 'wait', 'blocked', 'done')]
        for snapshot in snapshots:
            with self.subTest(outcome=snapshot['outcome']):
                self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
        roles = {**self.config.roles, 'worker': dataclasses.replace(self.config.roles['worker'],
                 capabilities=tuple(c for c in self.config.roles['worker'].capabilities if c != 'check_job'))}
        self.project.config = dataclasses.replace(self.config, roles=roles)
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.project.config = self.config
        self.mark(job)
        for snapshot in snapshots:
            with self.subTest(delivered_outcome=snapshot['outcome']):
                self.assertFalse(should_run(self.project, snapshot, role_name='worker'))

    def test_wake_delivery_neither_imports_nor_blocks_explicit_verification_claim(self):
        job = check_jobs.submit(self.context())
        self.run_job()
        self.mark(job)
        ctx = self.context()
        self.assertIsNone(ctx.verification)
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertTrue(ctx.verification['passed'])
        check_jobs.acknowledge(self.project, 'worker', ctx.check_results_read)
        self.assertFalse(check_jobs.pending(self.project, 'worker'))

    def test_opt_out_and_explicit_wait_after_automatic_delivery(self):
        job = self.submit()
        self.run_job()
        self.mark(job)
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        roles = {**self.config.roles, 'worker': dataclasses.replace(self.config.roles['worker'],
                                                                  check_result_wake=False)}
        self.project.config = dataclasses.replace(self.config, roles=roles)
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        check_jobs.clear_wait(self.project, 'worker')
        self.submit('printf other')
        self.run_job()
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertEqual(len(check_jobs.pending(self.project, 'worker')), 2)

    def test_live_explicit_wait_still_holds_admission_for_other_terminal_results(self):
        from mizu.runtime import should_run, Engine
        first = self.submit()
        self.run_job()
        second = self.submit('printf second')
        check_jobs.set_wait(self.project, 'worker', second['id'])
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker', event_trigger=True)
        self.assertFalse((check_jobs.root(self.project) / first['id'] / 'wake.json').exists())

    def test_markers_are_owner_bound_and_malformed_marker_cannot_acknowledge(self):
        job = self.submit()
        self.run_job()
        with self.assertRaises(Denied):
            self.mark(job, role='reviewer')
        path = check_jobs.root(self.project) / job['id'] / 'wake.json'
        path.write_text('not JSON')
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        ctx = self.context()
        self.assertEqual(check_jobs.result(ctx, job['id'])['status'], 'completed')
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])

    def test_terminal_statuses_and_shared_subscribers_wake_only_their_owners(self):
        # Shared execution retains independent owner-bound scheduling markers.
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        first = check_jobs.submit(self.context())
        second = check_jobs.join(self.context('reviewer'), first['id'])
        self.run_job()
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertTrue(check_jobs.wake_ready(self.project, 'reviewer'))
        self.mark(first)
        self.assertTrue(check_jobs.wake_ready(self.project, 'reviewer'))
        self.mark(second, role='reviewer')
        self.assertFalse(check_jobs.wake_ready(self.project, 'reviewer'))
        check_jobs.acknowledge(self.project, 'worker', {first['id']})
        for status in check_jobs.TERMINAL_WAKE_STATUSES:
            job = self.submit('printf ' + status)
            check_jobs.cancel(self.context(), job['id'])
            path = check_jobs.root(self.project) / job['id'] / 'job.json'
            write_json(path, {**read_json(path), 'status': status})
            with self.subTest(status=status):
                self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
                self.mark(job)
                self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))


class CheckResultWakeConfigTests(Fixture):
    def test_default_opt_out_and_boolean_validation(self):
        from mizu.config import load
        from mizu.errors import ConfigError
        self.assertTrue(self.config.roles['worker'].check_result_wake)
        raw = self.file.read_text()
        for value in ('true', 'false', '1', '"true"'):
            with self.subTest(value=value):
                self.file.write_text(raw.replace('[roles.worker]',
                    '[roles.worker]\ncheck_result_wake = ' + value))
                if value in ('true', 'false'):
                    self.assertEqual(load(self.file).roles['worker'].check_result_wake, value == 'true')
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
