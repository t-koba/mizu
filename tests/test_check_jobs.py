import dataclasses
import threading
from unittest.mock import patch
from support import Fixture
from mizu import check_jobs
from mizu.errors import Denied
from mizu.fs import lock, read_json, write_json

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
        job = ctx._op_exec_experiment('experiment', {'script': 'test -f /workspace/app.py'})
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(self.run_job()['status'], 'completed')
    def test_event_only_role_admits_completed_check(self):
        from mizu.services import render
        from pathlib import Path
        ctx, job = self.submit()
        self.run_job()
        from mizu.runtime import _role_events_due
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
        self.assertFalse((self.project.root / 'observed/worker.json').exists())
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.assertFalse(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
        self.assertFalse((self.project.root / 'active/worker.json').exists())
        self.run_job()
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
        self.assertTrue(should_run(self.project, self.project.snapshots.get(), role_name='worker'))
    def test_new_operator_input_interrupts_check_only_wait(self):
        ctx, job = self.submit()
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(check_jobs.waiting(self.project, 'worker'))
        self.project.set_control(wake_generation='new-independent-work')
        self.assertFalse(check_jobs.waiting(self.project, 'worker'))
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
        self.assertEqual(reader._op_exec_experiment('exec', {'script': 'echo hello'})['status'], 'queued')
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
