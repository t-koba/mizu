"""Offline writer wait fallback: bounded local evidence, no provider calls."""
import dataclasses
import datetime as dt
from unittest.mock import patch

from support import Fixture, ScriptDriver
from test_ci_waits import CiWaitFixture, SHA
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import now, read_json
from mizu.runtime import Engine, _writer_progress_fallback, _writer_wait_liveness, should_run


class WriterWaitTests(Fixture):
    def role(self):
        return self.config.roles['worker']

    def test_owned_check_liveness_and_terminal_check_finish(self):
        role = dataclasses.replace(self.role(), capabilities=(*self.role().capabilities, 'check_job'))
        identity = 'a' * 64
        for owner, status, live in (
                ('reviewer', 'running', False), ('worker', 'queued', True),
                ('worker', 'running', True), ('worker', 'completed', False),
                ('worker', 'cancelled', False), ('worker', 'interrupted', False)):
            job = {'id': identity, 'role': owner, 'status': status}
            with self.subTest(owner=owner, status=status), patch(
                    'mizu.acceptance.discover', return_value=({identity: job}, {})), patch(
                    'mizu.runtime.check_jobs.read', return_value=job):
                self.assertIs(_writer_wait_liveness(self.project, role), live)
                ctx = self.context()
                ctx.role = role
                args = {'outcome': 'wait', 'summary': 'Save check wait', 'state': 'Waiting',
                        'wait_for_check': identity}
                if owner != role.name:
                    with self.assertRaises(Denied):
                        ctx.handle('finish', args)
                else:
                    ctx.handle('finish', args)
                    self.assertIs(ctx.finished['wait_liveness'], live)
                    self.assertEqual(ctx.finished['wait_seconds'], self.config.limits.default_wait_seconds)
                    self.assertIs(read_json(ctx.run_dir / 'finish-intent.json')['wait_liveness'], live)
                    if live:
                        self.assertIs(_writer_wait_liveness(self.project, role, releases=[identity]), False)
        with patch('mizu.acceptance.discover', return_value=({}, {identity: 'invalid'})):
            self.assertIsNone(_writer_wait_liveness(self.project, role))

    def test_only_own_open_operator_questions_are_live_and_corruption_is_unknown(self):
        for source, addressee, kind in (
                ('reviewer', 'operator', 'question'), ('worker', 'writer', 'question'),
                ('worker', 'operator', 'proposal')):
            self.project.insights.submit(source=source, title='Not a wait', body='A proposal or another route',
                                         base_snapshot=None, kind=kind, addressee=addressee)
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        question = self.project.insights.submit(
            source='worker', title='Operator question', body='Q: Provide a fixture answer?',
            base_snapshot=None, kind='question', addressee='operator')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.project.insights.decide(question['id'], 'reject', 'Fixture answer', '', 'fixture',
                                     expected_rev=question['rev'])
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        (self.project.root / 'inbox' / 'broken.json').write_text('{')
        self.assertIsNone(_writer_wait_liveness(self.project, self.role()))

    def test_wait_backoff_requires_current_absence_and_eligible_ready_work(self):
        self.config.roles['worker'] = dataclasses.replace(self.role(), on_change=True)
        item = self.project.work_items.create(
            actor='worker', idempotency_key='ready', title='Persistent ready work')['record']['work_item']
        engine = Engine(self.config, driver=ScriptDriver())
        result = engine.run(self.project, 'worker')
        self.assertIs(result['finish']['wait_liveness'], False)
        snapshot = self.project.snapshots.get()
        stamp = dt.datetime.fromisoformat(snapshot['progress']['recorded_at']).timestamp()
        due = stamp + self.config.limits.writer_no_progress_backoff_seconds
        self.assertFalse(should_run(self.project, snapshot, role_name='worker', current_time=due - 1))
        for live in (True, None, False):
            with self.subTest(live=live), patch('mizu.runtime._writer_wait_liveness', return_value=live):
                self.assertEqual(should_run(self.project, snapshot, role_name='worker', current_time=due),
                                 live is False)
                self.assertFalse(_writer_progress_fallback(
                    snapshot, self.config, self.role(), False, due, project=self.project))
        with patch('mizu.runtime.time.time', return_value=due), patch(
                'mizu.runtime._writer_wait_liveness', return_value=True):
            self.assertEqual(engine.run(self.project, 'worker')['skipped'], 'unchanged')
            # Neither a fallback timer nor plain wait becomes an event subscription.
            self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
            self.project.set_control(wake_generation='independent-event')
            self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
        self.project.set_control(wake_generation=snapshot['wake_generation'])
        with patch('mizu.runtime.time.time', return_value=due):
            self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        self.project.work_items.transition(item['id'], expected_rev=1, actor='worker',
                                           status='dropped', reason='Fixture settled')
        # Consume the changed-ready-set event before testing the timer alone.
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.assertFalse(should_run(self.project, snapshot, role_name='worker', current_time=due + 301))

    def test_disabled_override_malformed_progress_and_partial_inventory_fail_closed(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        stamp = dt.datetime.fromisoformat(snapshot['progress']['recorded_at']).timestamp()
        due = stamp + 600
        for role in (dataclasses.replace(self.role(), no_progress_backoff_seconds=0),
                     dataclasses.replace(self.role(), workspace='read')):
            self.assertFalse(_writer_progress_fallback(
                snapshot, self.config, role, True, due, project=self.project))
        self.assertFalse(_writer_progress_fallback(
            {**snapshot, 'progress': {}}, self.config, self.role(), True, due, project=self.project))
        for key in ('truncated', 'coverage_complete'):
            page = self.project.work_items.page_open_readonly()
            page[key] = key == 'truncated'
            with patch.object(self.project.work_items, 'page_open_readonly', return_value=page):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
        for error in (OSError, Busy, LimitExceeded):
            with patch.object(self.project.work_items, 'page_open_readonly', side_effect=error('fixture')):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))


class WriterCiWaitTests(CiWaitFixture):
    def test_exact_held_ci_request_and_terminal_evidence(self):
        role = self.config.roles['worker']
        item = self.block()
        self.assertIsNone(_writer_wait_liveness(self.project, role))
        requests = CiRequests(self.project.root)
        for sha in ('b' * 40, SHA):
            identity = request_id(self.receipt['publication_id'], sha, ['ci'])
            requests.mark_dispatch(identity, {
                'request_id': identity, 'publication_id': self.receipt['publication_id'],
                'sha': sha, 'workflows': ['ci'], 'dispatched_at': now()})
            self.assertIs(_writer_wait_liveness(self.project, role), None if sha != SHA else True)
        # A request on another role's held publication is not this writer's source.
        self.assertIs(_writer_wait_liveness(self.project, self.config.roles['reviewer']), False)
        self.poll()
        self.assertIs(_writer_wait_liveness(self.project, role), False)
        self.assertEqual(self.store.get(item['id'])['status'], 'blocked')
