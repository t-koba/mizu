"""Check-result notifications steered into one running Pi unit, never polled."""
import dataclasses
import datetime as dt
import itertools
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch

from support import Fixture, ROOT, ScriptDriver
from mizu import acceptance, check_jobs, pi as pi_module
from mizu.errors import Denied
from mizu.pi import CheckSteer, PiDriver
from mizu.runtime import Engine
from mizu.unit_prompts import prompt_for



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
        self.assertEqual(context.check_steer_seq, check_jobs.completion_high_water(self.project))
        self.assertGreater(context.check_steer_seq, 0)
        self.assertEqual(context.check_steer_seen,
                         frozenset(row['id'] for row in prompt['check_jobs']
                                   if row['status'] in check_jobs.TERMINAL_WAKE_STATUSES))
        self.assertIn(job['id'], context.check_steer_seen)
        channel = RecordingChannel()
        pump = CheckSteer(context, channel, context.check_steer_seen)
        pump.observe({'type': 'agent_start'})
        self.assertEqual(channel.sent, [])

    def test_completion_during_prompt_capture_is_seeded_not_steered(self):
        self.job()
        from mizu.runtime import session_prompt as real_prompt
        captured = {}

        def racing_prompt(context, role, run_dir):
            raced = self.job('printf raced')
            captured['raced'] = raced['id']
            return real_prompt(context, role, run_dir)

        engine = Engine(self.config, driver=ScriptDriver(
            lambda ctx, prompt, profile: captured.setdefault('context', ctx)))
        with patch('mizu.runtime.session_prompt', side_effect=racing_prompt):
            self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        context = captured['context']
        # The cursor seals the pre-opening high; the raced completion is in the
        # bounded prompt, so `already` filters it instead of a duplicate steer.
        self.assertEqual(context.check_steer_seq, 1)
        self.assertIn(captured['raced'], context.check_steer_seen)
        channel = RecordingChannel()
        pump = CheckSteer(context, channel, context.check_steer_seen)
        pump.observe({'type': 'agent_start'})
        self.assertEqual(channel.sent, [])

    def test_unchanged_boundaries_read_no_jobs_and_new_completions_stay_bounded(self):
        first = self.job()
        channel = RecordingChannel()
        pump = CheckSteer(self.context(), channel)
        with patch.object(acceptance, 'discover', wraps=acceptance.discover) as discover:
            pump.observe({'type': 'agent_start'})
            self.assertEqual(discover.call_args.kwargs['ids'], [first['id']])
            self.assertEqual(len(channel.sent), 1)
            discover.reset_mock()
            for kind in ('turn_start', 'message_start', 'tool_execution_end',
                         'turn_end', 'message_end', 'agent_start'):
                pump.observe({'type': kind})
            discover.assert_not_called()
            self.assertEqual(len(channel.sent), 1)
            second = self.job('printf second')
            pump.observe({'type': 'turn_end'})
            self.assertEqual(discover.call_args.kwargs['ids'], [second['id']])
            self.assertEqual(len(channel.sent), 2)

    def test_large_completion_interval_delivers_every_change_in_bounded_batches(self):
        count = check_jobs.MAX_CHECK_STEER_CHANGE_ROWS + 8
        jobs = [self.job(f'printf large-{index}') for index in range(count)]
        expected = {job['id'] for job in jobs}
        cursor = check_jobs.CheckSteerCursor()
        presented, delivered = set(), []
        pages = []
        real_read = check_jobs._steer_changes

        def bounded_read(*args):
            page = real_read(*args)
            pages.append(len(page))
            return page

        with patch.object(check_jobs, '_steer_changes', bounded_read):
            for _ in range(count + 2):
                batch = check_jobs.steer_pending(self.project, 'worker', presented, cursor=cursor)
                if not batch:
                    break
                self.assertLessEqual(len(batch), check_jobs.MAX_CHECK_STEER_NOTIFICATIONS)
                for item in batch:
                    self.assertNotIn(item['id'], presented)
                    presented.add(item['id'])
                    delivered.append(item['id'])
        self.assertEqual(set(delivered), expected)
        self.assertEqual(len(delivered), count)
        self.assertTrue(pages)
        self.assertLessEqual(max(pages), check_jobs.MAX_CHECK_STEER_CHANGE_ROWS)
        self.assertEqual(sum(pages), count)

    def test_exact_full_page_seals_its_window_without_rereading(self):
        count = check_jobs.MAX_CHECK_STEER_CHANGE_ROWS
        jobs = [self.job(f'printf exact-{index}') for index in range(count)]
        cursor = check_jobs.CheckSteerCursor()
        presented, delivered = set(), []
        for _ in range(count):
            batch = check_jobs.steer_pending(self.project, 'worker', presented, cursor=cursor)
            for row in batch:
                self.assertNotIn(row['id'], presented)
                presented.add(row['id'])
                delivered.append(row['id'])
        self.assertEqual(delivered, [job['id'] for job in reversed(jobs)])
        with patch.object(acceptance, 'discover', wraps=acceptance.discover) as discover:
            self.assertEqual(check_jobs.steer_pending(
                self.project, 'worker', presented, cursor=cursor), [])
            discover.assert_not_called()

    def test_window_with_only_other_role_rows_follows_a_later_new_high(self):
        self.job(role='reviewer')  # Advances the shared index, not this role.
        cursor = check_jobs.CheckSteerCursor()
        self.assertEqual(check_jobs.steer_pending(self.project, 'worker', (), cursor=cursor), [])
        fresh = self.job('printf fresh')
        self.assertEqual([row['id'] for row in check_jobs.steer_pending(
            self.project, 'worker', (), cursor=cursor)], [fresh['id']])

    def test_preopening_history_cannot_delay_or_repeat_a_fresh_result(self):
        count = check_jobs.MAX_CHECK_STEER_CHANGE_ROWS + 5
        old = [self.job(f'printf old-{index}') for index in range(count)]
        captured = check_jobs.completion_high_water(self.project)
        self.assertEqual(captured, count)
        # Both prompt seeding and explicit acknowledgement are older history;
        # neither consumes this unit's first incremental boundary.
        old_ids = {job['id'] for job in old}
        check_jobs.acknowledge(self.project, 'worker', old_ids)
        presented = set(old_ids)
        fresh = self.job('printf fresh')
        cursor = check_jobs.CheckSteerCursor(floor=captured)
        batch = check_jobs.steer_pending(self.project, 'worker', presented, cursor=cursor)
        self.assertEqual([row['id'] for row in batch], [fresh['id']])
        presented.add(fresh['id'])
        for _ in range(3):
            self.assertEqual(check_jobs.steer_pending(
                self.project, 'worker', presented, cursor=cursor), [])
        # Steering is presentation only: the independent wake and the unread
        # result survive, and a seeded fresh ID is never steered twice.
        wake = check_jobs.root(self.project) / fresh['id'] / 'wake.json'
        self.assertFalse(wake.exists())
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertFalse(check_jobs.read(self.project, fresh['id'])['acknowledged'])
        context = self.context()
        context.check_steer_seq = captured
        channel = RecordingChannel()
        pump = CheckSteer(context, channel, presented)
        self.assertEqual(pump.cursor.floor, captured)
        pump.observe({'type': 'agent_start'})
        self.assertEqual(channel.sent, [])

    def test_newest_first_pages_match_full_discovery_and_drain_in_order(self):
        count = check_jobs.MAX_CHECK_STEER_CHANGE_ROWS + 9
        captured = check_jobs.completion_high_water(self.project)
        self.assertEqual(captured, 0)
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        ticks = itertools.count()

        def distinct_now():
            return (base + dt.timedelta(seconds=next(ticks))).isoformat(timespec='seconds')

        with patch.object(check_jobs, 'now', distinct_now):
            jobs = [self.job(f'printf page-{index}') for index in range(count)]
        expected = [job['id'] for job in reversed(jobs)]
        full = check_jobs.steer_pending(self.project, 'worker', ())
        self.assertEqual(len(full), check_jobs.MAX_CHECK_STEER_NOTIFICATIONS)
        cursor = check_jobs.CheckSteerCursor(floor=captured)
        first = check_jobs.steer_pending(self.project, 'worker', (), cursor=cursor)
        self.assertEqual([row['id'] for row in first], [row['id'] for row in full])
        presented = {row['id'] for row in first}
        delivered = [row['id'] for row in first]
        for _ in range(count + 2):
            batch = check_jobs.steer_pending(self.project, 'worker', presented, cursor=cursor)
            if not batch:
                break
            self.assertLessEqual(len(batch), check_jobs.MAX_CHECK_STEER_NOTIFICATIONS)
            for row in batch:
                self.assertNotIn(row['id'], presented)
                presented.add(row['id'])
                delivered.append(row['id'])
        self.assertEqual(delivered, expected)

    def test_completion_change_index_covers_overlay_and_rebuild(self):
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        original = check_jobs.submit(self.context())
        self.assertEqual(check_jobs.run_one(self.project, threading.Event(),
                                            sandbox_factory=FakeSandbox)['status'], 'completed')
        shared = check_jobs.submit(self.context())
        with self.project.ledger.read('check_jobs', locked=False) as db:
            changes = db.execute('SELECT role, job_id FROM check_completion_changes '
                                 'ORDER BY seq').fetchall()
        self.assertEqual(changes, [('worker', original['id']), ('worker', shared['id'])])
        self.project.ledger.rebuild('check_jobs')
        with self.project.ledger.read('check_jobs', locked=False) as db:
            rebuilt = db.execute('SELECT role, job_id FROM check_completion_changes '
                                 'ORDER BY seq').fetchall()
        self.assertEqual(set(rebuilt), set(changes))
        self.assertEqual(len(rebuilt), 2)
        projected, _ = acceptance.discover(self.project, ids=[shared['id']])
        self.assertEqual(projected[shared['id']]['status'], 'completed')

    def test_detached_subscription_is_indexed_and_steered_like_full_discovery(self):
        self.config = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        original = self.job()
        shared = check_jobs.submit(self.context())
        path = check_jobs.root(self.project) / shared['id'] / 'job.json'
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['detached'] = True
        raw['acknowledged'] = False
        path.write_text(json.dumps(raw), encoding='utf-8')
        self.project.ledger.rebuild('check_jobs')
        self.assertEqual(check_jobs.read(self.project, shared['id'])['status'], 'cancelled')
        with self.project.ledger.read('check_jobs', locked=False) as db:
            rows = db.execute('SELECT role, job_id FROM check_completion_changes WHERE job_id=?',
                              (shared['id'],)).fetchall()
        self.assertEqual(rows, [('worker', shared['id'])])
        full = {row['id'] for row in check_jobs.steer_pending(
            self.project, 'worker', (), discovery=acceptance.discover(self.project))}
        incremental = {row['id'] for row in check_jobs.steer_pending(
            self.project, 'worker', (), cursor=check_jobs.CheckSteerCursor())}
        self.assertIn(shared['id'], full)
        self.assertIn(shared['id'], incremental)

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
    """Bounded fake-RPC timing seam; this is not actual Pi SDK timing evidence.

    The separate real-project measurement item owns SDK timing claims. Here the
    test peer holds the same adapter run open at a turn boundary until a real
    check completes, so the delivery path is proved across a live completion
    boundary without pretending the peer is a model.
    """

    def test_completion_during_the_opening_prompt_reaches_the_next_turn(self):
        job = self.job(run=False)
        context = self.context()
        prompt = prompt_for(context)  # The same opening projection the runtime sends.
        decoded = json.loads(prompt)
        terminal = frozenset(row['id'] for row in decoded.get('check_jobs', [])
                             if row.get('status') in check_jobs.TERMINAL_WAKE_STATUSES)
        self.assertNotIn(job['id'], terminal)
        opening_rows = [row for row in decoded.get('check_jobs', []) if row.get('id') == job['id']]
        self.assertEqual(len(opening_rows), 1)
        self.assertIn(opening_rows[0]['status'], ('queued', 'running'))
        context.check_steer_seen = terminal
        profile = context.role.profile
        raw = {**self.config.profiles[profile], 'engine': 'pi', 'provider': 'custom',
               'options': {}, 'session': 'ephemeral'}
        engines = {**self.config.engines, 'pi': {**self.config.engines['pi'],
            'command': (sys.executable, str(ROOT / 'tests/fake_pi.py'), '--fake-scenario', 'live-steer')}}
        config = dataclasses.replace(self.config,
                                     profiles={**self.config.profiles, profile: raw}, engines=engines)
        context.config = config
        prompt_path = self.root / 'live-opening-prompt.json'
        continue_path = self.root / 'live-continue'
        commands_path = self.root / 'live-commands.jsonl'
        turn_path = self.root / 'live-next-turn.jsonl'
        env = {'MIZU_FAKE_PI_LIVE_PROMPT': str(prompt_path),
               'MIZU_FAKE_PI_LIVE_CONTINUE': str(continue_path),
               'MIZU_FAKE_PI_LIVE_COMMANDS': str(commands_path),
               'MIZU_FAKE_PI_LIVE_TURN': str(turn_path)}
        real_environment = pi_module.environment

        def child_environment(*, extra=None):
            return {**real_environment(extra=extra), **env}

        errors = []
        done = threading.Event()

        def drive():
            try:
                PiDriver(config).execute(context, prompt)
            except BaseException as exc:  # Re-raised on the test thread below.
                errors.append(exc)
            finally:
                done.set()

        with patch.object(pi_module, 'environment', child_environment):
            worker = threading.Thread(target=drive, daemon=True)
            worker.start()
            deadline = time.monotonic() + 10
            while not prompt_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(prompt_path.exists(), 'The adapter never captured the opening prompt')
            self.assertEqual(prompt_path.read_text(encoding='utf-8'), prompt)
            self.assertIn(job['id'], prompt)  # Present, but still nonterminal.
            self.assertIn(check_jobs.read(self.project, job['id'])['status'], ('queued', 'running'))
            # The real check finishes while this adapter run is still active.
            self.assertEqual(check_jobs.run_one(self.project, threading.Event(),
                                                sandbox_factory=FakeSandbox)['status'], 'completed')
            continue_path.write_text('go', encoding='utf-8')
            self.assertTrue(done.wait(15), 'The adapter run did not settle after the steer')
        worker.join(timeout=5)
        if errors:
            raise errors[0]
        self.assertFalse(worker.is_alive())
        delivered = (context.run_dir / 'steers.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(delivered), 1)
        self.assertIn(job['id'], delivered[0])
        self.assertLessEqual(len(delivered[0].encode('utf-8')), check_jobs.MAX_CHECK_STEER_MESSAGE_BYTES)
        turn = turn_path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(turn), 1)
        self.assertIn(job['id'], turn[0])  # The next model turn saw the result.
        commands = commands_path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(commands.count('prompt'), 1)  # No result polling or redispatch.
        self.assertEqual(commands.count('steer'), 1)
        self.assertEqual(context.check_steer_seen, terminal)
        self.assertFalse((check_jobs.root(self.project) / job['id'] / 'wake.json').exists())
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])



class CheckSteerBridgeTests(SteerFixture):
    """Host-owned _check_steer for the managed pi-durable lane."""

    def test_bridge_returns_only_owned_unacknowledged_terminal_results(self):
        job = self.job()
        other = self.job('printf reviewer', role='reviewer')
        queued = self.job('printf pending', run=False)
        context = self.context()
        response = context.handle('_check_steer', {})
        self.assertEqual([row['id'] for row in response['messages']], [job['id']])
        message = response['messages'][0]
        self.assertEqual(set(message), {'id', 'message'})
        self.assertEqual(message['id'], job['id'])
        self.assertNotIn('printf ok', message['message'])
        self.assertLessEqual(len(message['message'].encode('utf-8')),
                             check_jobs.MAX_CHECK_STEER_MESSAGE_BYTES)
        # The same capability and policy gate as dispatch wakes still applies.
        self.set_role('worker', capabilities=tuple(c for c in self.config.roles['worker'].capabilities
                                                    if c != 'check_job'))
        self.assertEqual(self.context().handle('_check_steer', {}), {'messages': []})
        self.set_role('worker', capabilities=(*self.config.roles['worker'].capabilities, 'check_job'),
                      check_result_wake=False)
        self.assertEqual(self.context().handle('_check_steer', {}), {'messages': []})
        self.set_role('worker', check_result_wake=True)
        check_jobs.acknowledge(self.project, 'worker', {job['id']})
        self.assertEqual(self.context().handle('_check_steer', {}), {'messages': []})
        self.assertTrue(check_jobs.read(self.project, queued['id'])['status'] in ('queued', 'running'))
        self.assertTrue(check_jobs.read(self.project, other['id'])['status'] in check_jobs.TERMINAL_WAKE_STATUSES)

    def test_bridge_response_is_bounded_by_the_named_notification_bound(self):
        for index in range(check_jobs.MAX_CHECK_STEER_NOTIFICATIONS + 2):
            self.job(f'printf bound-{index}')
        context = self.context()
        response = context.handle('_check_steer', {})
        self.assertEqual(len(response['messages']), check_jobs.MAX_CHECK_STEER_NOTIFICATIONS)
        ids = [row['id'] for row in response['messages']]
        self.assertEqual(len(set(ids)), len(ids))

    def test_bridge_keeps_wake_ready_until_durable_delivery_is_acknowledged(self):
        job = self.job()
        context = self.context()
        # The job completed after this unit's opening prompt was captured.
        context.check_steer_seen = frozenset()
        wake = check_jobs.root(self.project) / job['id'] / 'wake.json'
        self.assertFalse(wake.exists())
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        first = context.handle('_check_steer', {})
        self.assertEqual([row['id'] for row in first['messages']], [job['id']])
        # Presentation alone leaves the automatic wake available.
        self.assertFalse(wake.exists())
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertIn(job['id'], context.check_steer_seen)
        self.assertEqual(context._check_steer_pending, {job['id']})
        # Result retrieval is independent of both the notification and the ack.
        result = check_jobs.result(context, job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertEqual(context.handle('_check_steer_delivered', {'ids': [job['id']]}),
                         {'recorded': True})
        self.assertTrue(wake.exists())
        self.assertEqual(json.loads(wake.read_text())['run'], context.run_dir.name)
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))
        self.assertEqual(context._check_steer_pending, set())
        self.assertEqual(context.handle('_check_steer', {}), {'messages': []})
        # The ack consumes only the dispatch wake: unread output still needs
        # action=result and the ordinary successful-finish acknowledgement.
        self.assertFalse(check_jobs.read(self.project, job['id'])['acknowledged'])
        self.assertEqual(check_jobs.result(context, job['id'])['status'], 'completed')
        # Pending IDs are retired; a replayed ack cannot consume another wake.
        with self.assertRaises(Denied):
            context.handle('_check_steer_delivered', {'ids': [job['id']]})

    def test_bridge_pending_ids_are_replaced_not_accumulated(self):
        first = self.job()
        context = self.context()
        context.check_steer_seen = frozenset()
        self.assertEqual([row['id'] for row in context.handle('_check_steer', {})['messages']],
                         [first['id']])
        self.assertEqual(context._check_steer_pending, {first['id']})
        second = self.job('printf second')
        self.assertEqual([row['id'] for row in context.handle('_check_steer', {})['messages']],
                         [second['id']])
        self.assertEqual(context._check_steer_pending, {second['id']})
        self.assertEqual(context.handle('_check_steer_delivered', {'ids': [second['id']]}),
                         {'recorded': True})
        first_wake = check_jobs.root(self.project) / first['id'] / 'wake.json'
        second_wake = check_jobs.root(self.project) / second['id'] / 'wake.json'
        self.assertFalse(first_wake.exists())
        self.assertTrue(second_wake.exists())
        with self.assertRaises(Denied):
            context.handle('_check_steer_delivered', {'ids': [first['id']]})

    def test_bridge_refuses_malformed_arguments(self):
        job = self.job()
        context = self.context()
        for args in ({'id': job['id']}, {'messages': []}, {'sequence': 1}):
            with self.assertRaises(Denied):
                context.handle('_check_steer', args)
        self.assertFalse((check_jobs.root(self.project) / job['id'] / 'wake.json').exists())

    def test_bridge_refuses_malformed_delivery_arguments(self):
        job = self.job()
        unknown = self.job('printf unknown', run=False)
        context = self.context()
        response = context.handle('_check_steer', {})
        ids = [row['id'] for row in response['messages']]
        self.assertEqual(ids, [job['id']])
        wake = check_jobs.root(self.project) / job['id'] / 'wake.json'
        oversized = [f'unknown-{index}' for index in range(check_jobs.MAX_CHECK_STEER_NOTIFICATIONS)]
        for args in ({}, {'ids': []}, {'ids': job['id']}, {'ids': (job['id'],)},
                     {'ids': ids + oversized}, {'ids': [1]}, {'ids': ['never-returned']},
                     {'ids': [unknown['id']]}, {'ids': ids + [unknown['id']]},
                     {'ids': ids, 'extra': True}):
            with self.assertRaises(Denied):
                context.handle('_check_steer_delivered', args)
            self.assertFalse(wake.exists())
            self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
        self.assertEqual(context._check_steer_pending, set(ids))
        # Every refusal left the pending acknowledgement usable.
        self.assertEqual(context.handle('_check_steer_delivered', {'ids': ids}),
                         {'recorded': True})
        self.assertTrue(wake.exists())


if __name__ == '__main__':
    unittest.main()
