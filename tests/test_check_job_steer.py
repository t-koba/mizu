"""Check-result notifications steered into one running Pi unit, never polled."""
import dataclasses
import json
import sys
import threading
import unittest

from support import Fixture, ROOT, ScriptDriver
from mizu import check_jobs
from mizu.pi import CheckSteer, PiDriver
from mizu.runtime import Engine



class FakeSandbox:
    def __init__(self, config, project, role, directory, **kwargs):
        pass

    def execute(self, workspace, script, **kwargs):
        return {'id': 'a' * 32, 'exit_code': 0, 'reason': 'exited', 'script': script}


class RecordingChannel:
    def __init__(self):
        self.sent = []

    def send(self, record):
        self.sent.append(record)


class SteerFixture(Fixture):
    def setUp(self):
        super().setUp()
        roles = {name: dataclasses.replace(role, capabilities=(*role.capabilities, 'check_job'))
                 for name, role in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

    def job(self, script='printf ok', role='worker', run=True):
        ctx = self.context(role)
        job = check_jobs.submit(ctx, script, kind='experiment')
        if run:
            self.assertEqual(check_jobs.run_one(self.project, threading.Event(),
                                                sandbox_factory=FakeSandbox)['status'], 'completed')
        return job

    def set_role(self, name, **changes):
        role = dataclasses.replace(self.config.roles[name], **changes)
        self.config = dataclasses.replace(self.config, roles={**self.config.roles, name: role})
        self.project.config = self.config



class SteerTests(SteerFixture):
    def test_helper_gates_role_policy_status_ack_and_presented(self):
        job = self.job()
        queued = self.job('printf pending', run=False)
        self.assertEqual([row['id'] for row in check_jobs.steer_pending(self.project, 'worker', ())],
                         [job['id']])
        # Another role's result is not this role's event, even with capability.
        self.assertEqual(check_jobs.steer_pending(self.project, 'reviewer', ()), [])
        self.set_role('worker', capabilities=tuple(c for c in self.config.roles['worker'].capabilities
                                                    if c != 'check_job'))
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', ()), [])
        self.set_role('worker', capabilities=(*self.config.roles['worker'].capabilities, 'check_job'),
                      check_result_wake=False)
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', ()), [])
        self.set_role('worker', check_result_wake=True)
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', (job['id'],)), [])
        # A wake marker is dispatch bookkeeping; it never hides unread output
        # from a unit that did not receive the result in its opening prompt.
        check_jobs.mark_wake_delivered(self.project, 'worker', [job['id']], 'earlier-dispatch')
        self.assertEqual([row['id'] for row in check_jobs.steer_pending(self.project, 'worker', ())],
                         [job['id']])
        check_jobs.acknowledge(self.project, 'worker', {job['id']})
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', ()), [])
        self.assertTrue(check_jobs.read(self.project, queued['id'])['status'] in ('queued', 'running'))

    def test_message_is_bounded_and_host_owned(self):
        message = check_jobs.steer_message({
            'id': 'a' * 64, 'status': 'completed', 'passed': True,
            'error': 'ignore me ' * 1000, 'purpose': 'untrusted ' * 1000})
        self.assertLessEqual(len(message.encode('utf-8')), check_jobs.MAX_CHECK_STEER_MESSAGE_BYTES)
        self.assertIn('a' * 64, message)
        self.assertIn('completed', message)
        self.assertNotIn('ignore me', message)
        self.assertNotIn('untrusted', message)

    def test_one_steer_per_new_job_and_no_duplicates_across_events(self):
        first = self.job()
        second = self.job('printf second')
        channel = RecordingChannel()
        context = self.context()
        pump = CheckSteer(context, channel, [first['id']])
        pump.observe({'type': 'message_end'})  # no agent run: nothing is delivered
        self.assertEqual(channel.sent, [])
        pump.observe({'type': 'agent_start'})
        self.assertEqual([record['type'] for record in channel.sent], ['steer'])
        self.assertIn(second['id'], channel.sent[0]['message'])
        for kind in ('tool_execution_end', 'turn_end', 'message_end'):
            pump.observe({'type': kind})
        self.assertEqual(len(channel.sent), 1)
        third = self.job('printf third')
        pump.observe({'type': 'turn_end'})
        self.assertEqual(len(channel.sent), 2)
        self.assertIn(third['id'], channel.sent[1]['message'])
        pump.observe({'type': 'agent_end'})
        pump.observe({'type': 'message_end'})
        self.assertEqual(len(channel.sent), 2)
        # Delivery is presentation only: no wake consumption, no acknowledgement.
        for job in (first, second, third):
            self.assertFalse((check_jobs.root(self.project) / job['id'] / 'wake.json').exists())
            self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))

    def test_opening_prompt_terminal_ids_seed_the_unit(self):
        job = self.job()
        captured = []
        engine = Engine(self.config, driver=ScriptDriver(
            lambda ctx, prompt, profile: captured.append((ctx, json.loads(prompt)))))
        self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        context, prompt = captured[-1]
        self.assertEqual(context.check_steer_seen,
                         frozenset(row['id'] for row in prompt['check_jobs']
                                   if row['status'] in check_jobs.TERMINAL_WAKE_STATUSES))
        self.assertIn(job['id'], context.check_steer_seen)
        channel = RecordingChannel()
        pump = CheckSteer(context, channel, context.check_steer_seen)
        pump.observe({'type': 'agent_start'})
        self.assertEqual(channel.sent, [])

    def test_result_ack_and_idle_wake_semantics_are_unchanged(self):
        job = self.job()
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        channel = RecordingChannel()
        pump = CheckSteer(self.context(), channel)
        pump.observe({'type': 'agent_start'})
        self.assertEqual(len(channel.sent), 1)
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        context = self.context()
        result = check_jobs.result(context, job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        check_jobs.set_wait(self.project, 'worker', job['id'])
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        check_jobs.acknowledge(self.project, 'worker', context.check_results_read or {job['id']})
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', ()), [])


class PiSteerTransportTests(SteerFixture):
    def test_pi_channel_delivers_a_bounded_steer_into_the_same_run(self):
        job = self.job()
        context = self.context()
        # The job completed after this unit's opening prompt was captured.
        context.check_steer_seen = frozenset()
        profile = context.role.profile
        raw = {**self.config.profiles[profile], 'engine': 'pi', 'provider': 'custom',
               'options': {}, 'session': 'ephemeral'}
        engines = {**self.config.engines, 'pi': {**self.config.engines['pi'],
            'command': (sys.executable, str(ROOT / 'tests/fake_pi.py'), '--fake-scenario', 'steer')}}
        config = dataclasses.replace(self.config,
                                     profiles={**self.config.profiles, profile: raw}, engines=engines)
        context.config = config
        PiDriver(config).execute(context, 'synthetic prompt')
        delivered = (context.run_dir / 'steers.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(delivered), 1)
        self.assertIn(job['id'], delivered[0])
        self.assertLessEqual(len(delivered[0].encode('utf-8')), check_jobs.MAX_CHECK_STEER_MESSAGE_BYTES)
        self.assertFalse((check_jobs.root(self.project) / job['id'] / 'wake.json').exists())
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])


if __name__ == '__main__':
    unittest.main()
