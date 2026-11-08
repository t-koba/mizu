"""Offline writer wait fallback: bounded local evidence, no provider calls."""
import dataclasses
import datetime as dt
from unittest.mock import patch

from support import Fixture, ScriptDriver
from test_ci_waits import CiWaitFixture, SHA
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import now, read_json, write_json
from mizu.runtime import (Engine, _writer_progress_fallback, _writer_wait_liveness,
                           _writer_wait_sources, should_run)


class WriterWaitTests(Fixture):
    def role(self):
        return self.config.roles['worker']

    def block(self, key, wait):
        store = self.project.work_items
        item = store.create(actor='worker', idempotency_key=key, title=key)['record']['work_item']
        return store.transition(item['id'], expected_rev=1, actor='worker',
                                status='blocked', wait=wait)

    def question(self, source='worker'):
        return self.project.insights.submit(
            source=source, title='Fixture question', body='Q: What should happen?',
            base_snapshot=None, kind='question', addressee='operator')

    def backoff_time(self, snapshot):
        return (dt.datetime.fromisoformat(snapshot['progress']['recorded_at']).timestamp()
                + self.config.limits.writer_no_progress_backoff_seconds)

    def test_solo_code_wait_backoff_without_ready_work_is_not_an_event(self):
        self.config.roles['worker'] = dataclasses.replace(self.role(), on_change=True)
        item = self.block('self code', {'kind': 'code_change'})
        engine = Engine(self.config, driver=ScriptDriver())
        result = engine.run(self.project, 'worker')
        self.assertIs(result['finish']['wait_liveness'], False)
        snapshot = self.project.snapshots.get()
        due = self.backoff_time(snapshot)
        self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)
        self.assertFalse(should_run(self.project, snapshot, role_name='worker', current_time=due - 1))
        self.assertTrue(should_run(self.project, snapshot, role_name='worker', current_time=due))
        with patch('mizu.runtime.time.time', return_value=due):
            self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
            self.assertEqual(engine.run(self.project, 'worker')['status'], 'completed')
        current = self.project.work_items.get(item['id'])
        self.assertEqual((current['status'], current['rev']), ('blocked', item['rev']))
        # Configuration, not the current run/holder count, determines sole-writer authority.
        self.config.roles['reviewer'] = dataclasses.replace(
            self.config.roles['reviewer'], workspace='write')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertFalse(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, due, project=self.project))

    def test_own_answered_current_question_remains_holder_ready(self):
        question = self.question()
        item = self.block('answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)
        self.assertIsNone(self.project.status()['blocked_work']['items'][0]['self_resolvable'])
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        self.project.insights.decide(question['id'], 'reject', 'Substantive answer', '', 'fixture',
                                     expected_rev=1)
        self.assertTrue(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, self.backoff_time(snapshot), project=self.project))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        ready = self.project.work_items.page_ready(role_name='worker')
        self.assertIn(item['id'], [row['id'] for row in ready['items']])
        self.assertEqual(self.project.work_items.page_ready(role_name='reviewer')['total'], 0)
        self.assertEqual(self.project.work_items.get(item['id'])['status'], 'blocked')
        # An answer to an older revision is not an answer to the current question.
        self.project.insights.revise(question['id'], source='worker', title='Follow-up',
                                     body='Q: A new question?', base_snapshot=None, expected_rev=1)
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)

    def test_malformed_question_metadata_never_proves_a_self_wait(self):
        question = self.question()
        item = self.block('malformed answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.project.insights.decide(question['id'], 'reject', 'Substantive answer', '', 'fixture',
                                     expected_rev=1)
        path = self.project.root / 'inbox' / f"{question['id']}.json"
        target = read_json(path)
        target['addressee'] = 'not a valid role'
        write_json(path, target)
        blocked = self.project.status()['blocked_work']
        row = next(row for row in blocked['items'] if row['id'] == item['id'])
        self.assertEqual(row['state'], 'unknown')
        self.assertIsNone(row['self_resolvable'])
        self.assertFalse(blocked['coverage_complete'])

    def test_answered_foreign_question_or_own_proposal_is_not_a_self_wait(self):
        for source, kind in (('reviewer', 'question'), ('worker', 'proposal')):
            target = self.project.insights.submit(
                source=source, title='External target', body='Fixture target',
                base_snapshot=None, kind=kind, addressee='operator')
            item = self.block(kind, {'kind': 'insight_decided', 'insight': target['id']})
            Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
            snapshot = self.project.snapshots.get()
            self.project.insights.decide(target['id'], 'reject', 'Settled', '', 'fixture', expected_rev=1)
            self.assertFalse(_writer_progress_fallback(
                snapshot, self.config, self.role(), False, self.backoff_time(snapshot), project=self.project))
            row = next(row for row in self.project.status()['blocked_work']['items'] if row['id'] == item['id'])
            self.assertIsNone(row['self_resolvable'])

    def test_external_waits_suppress_self_wait_backoff(self):
        self.block('self code', {'kind': 'code_change'})
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        due = self.backoff_time(snapshot)
        other = self.question('reviewer')
        for key, wait in (
                ('deadline', {'kind': 'deadline', 'at': '2099-01-01T00:00:00+00:00'}),
                ('dependency', {'kind': 'dependency', 'recipient': 'reviewer',
                                'requires': {'insight': other['id'], 'rev': 1, 'action': 'accept'}}),
                ('other question', {'kind': 'insight_decided', 'insight': other['id']})):
            with self.subTest(key=key):
                item = self.block(key, wait)
                self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
                self.assertFalse(_writer_progress_fallback(
                    snapshot, self.config, self.role(), False, due, project=self.project))
                self.project.work_items.transition(item['id'], expected_rev=item['rev'],
                                                   actor='worker', status='dropped', reason='Fixture settled')
        # A free-standing unresolved own operator question is also external liveness.
        self.question()
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertFalse(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, due, project=self.project))

    def test_self_wait_inventory_must_be_complete_and_safe(self):
        self.block('self code', {'kind': 'code_change'})
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        due = self.backoff_time(snapshot)
        status = self.project.work_items.open_state_readonly(
            limit=self.config.limits.work_items_prompt, writer_roles={'worker'})
        for field, value in (('coverage_complete', False), ('truncated', True)):
            blocked = {**status['blocked_work'], field: value}
            with self.subTest(field=field), patch.object(
                    self.project.work_items, 'open_state_readonly',
                    return_value={**status, 'blocked_work': blocked}):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
                self.assertFalse(_writer_progress_fallback(
                    snapshot, self.config, self.role(), False, due, project=self.project))
        (self.project.root / 'inbox').mkdir(exist_ok=True)
        (self.project.root / 'inbox' / 'broken.json').write_text('{')
        self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
        self.assertFalse(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, due, project=self.project))

    def test_status_reports_bounded_self_causes_without_transition_or_extra_scan(self):
        code = self.block('self code', {'kind': 'code_change'})
        question = self.question()
        answer = self.block('answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.project.insights.decide(question['id'], 'reject', 'Substantive answer', '', 'fixture',
                                     expected_rev=1)
        store = self.project.work_items
        with patch.object(store, '_page_open_impl', wraps=store._page_open_impl) as scan:
            blocked = self.project.status()['blocked_work']
            self.assertEqual(scan.call_count, 1)
        self.assertTrue(blocked['coverage_complete'])
        self.assertFalse(blocked['truncated'])
        causes = {row['id']: row['self_resolvable'] for row in blocked['items']}
        self.assertEqual(causes, {code['id']: 'sole_writer_code_change',
                                  answer['id']: 'answered_own_question'})
        # The same bounded scan exposes row truncation independently of inventory coverage.
        bounded = store.open_state_readonly(limit=1, writer_roles={'worker'})['blocked_work']
        self.assertEqual(len(bounded['items']), 1)
        self.assertTrue(bounded['truncated'])
        self.assertTrue(bounded['coverage_complete'])
        self.config.roles['reviewer'] = dataclasses.replace(
            self.config.roles['reviewer'], workspace='write')
        rows = {row['id']: row for row in self.project.status()['blocked_work']['items']}
        self.assertIsNone(rows[code['id']]['self_resolvable'])
        self.assertEqual(rows[answer['id']]['self_resolvable'], 'answered_own_question')
        for item in (code, answer):
            self.assertEqual(store.get(item['id'])['rev'], item['rev'])
        (self.project.root / 'work-items' / code['id'] / 'broken.json').write_text('{')
        unavailable = self.project.status()['blocked_work']
        self.assertFalse(unavailable['coverage_complete'])
        self.assertIsNone(unavailable['total'])

    def test_owned_check_liveness_and_terminal_check_finish(self):
        self.block('self code', {'kind': 'code_change'})
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
            with self.subTest(live=live), patch(
                    'mizu.runtime._writer_wait_sources', return_value=(live, False, None)):
                self.assertEqual(should_run(self.project, snapshot, role_name='worker', current_time=due),
                                 live is False)
                self.assertFalse(_writer_progress_fallback(
                    snapshot, self.config, self.role(), False, due, project=self.project))
        with patch('mizu.runtime.time.time', return_value=due), patch(
                'mizu.runtime._writer_wait_sources', return_value=(True, False, None)):
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
            status = self.project.work_items.open_state_readonly(
                limit=self.config.limits.work_items_prompt, writer_roles={'worker'})
            status['blocked_work'][key] = key == 'truncated'
            with patch.object(self.project.work_items, 'open_state_readonly', return_value=status):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
        for error in (OSError, Busy, LimitExceeded):
            with patch.object(self.project.work_items, 'open_state_readonly', side_effect=error('fixture')):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))


class WriterCiWaitTests(CiWaitFixture):
    def dispatch(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root, self.config.ci_requests).mark_dispatch(identity, {
            "request_id": identity, "publication_id": self.receipt["publication_id"],
            "sha": SHA, "workflows": ["ci"], "dispatched_at": now()})
        return identity

    def ready_item(self, key="independent-ready"):
        return self.store.create(actor="worker", idempotency_key=key,
                                 title="Independent ready work")["record"]["work_item"]

    def test_live_held_ci_wait_reenters_after_backoff_and_reports_conversion(self):
        item = self.block()
        for index in range(32):
            self.ready_item(f"independent-ready-{index}")
        self.dispatch()
        self.assertEqual(_writer_wait_sources(
            self.project, self.config.roles["worker"], include_structured_wait=True),
            (True, False, "ci"))
        engine = Engine(self.config, driver=ScriptDriver())
        result = engine.run(self.project, "worker")
        snapshot = self.project.snapshots.get()
        conversion = {"status": "converted",
                      "reason": "ready_work_with_pending_structured_wait",
                      "wait_kind": "ci", "backoff_seconds": 300}
        self.assertIs(result["finish"]["wait_liveness"], True)
        self.assertNotIn("wait_conversion", result["finish"])
        self.assertEqual(snapshot["progress"]["schema"], 6)
        self.assertEqual(snapshot["progress"]["wait_conversion"], conversion)
        self.assertEqual(self.project.status()["progress"]["record"]["wait_conversion"], conversion)
        from mizu.snapshot import validate_progress
        for bad in (
                {**conversion, "status": "not-converted"},
                {**conversion, "wait_kind": "unknown"},
                {**conversion, "backoff_seconds": 0},
                {**conversion, "extra": True}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                validate_progress({**snapshot["progress"], "wait_conversion": bad})
        with self.assertRaises(Denied):
            validate_progress({key: value for key, value in snapshot["progress"].items()
                               if key != "wait_conversion"})
        current = self.store.get(item["id"])
        self.assertEqual((current["status"], current["holder"], current["wait"]["kind"]),
                         ("blocked", "worker", "ci"))
        stamp = dt.datetime.fromisoformat(snapshot["progress"]["recorded_at"]).timestamp()
        due = stamp + conversion["backoff_seconds"]
        self.assertFalse(should_run(self.project, snapshot, role_name="worker", current_time=due - 1))
        self.assertTrue(should_run(self.project, snapshot, role_name="worker", current_time=due))
        for update in ({"wait_kind": "deadline"}, {"backoff_seconds": 299}):
            altered = dict(snapshot)
            altered["progress"] = dict(snapshot["progress"])
            altered["progress"]["wait_conversion"] = {**conversion, **update}
            with self.subTest(update=update):
                self.assertFalse(_writer_progress_fallback(
                    altered, self.config, self.config.roles["worker"], True,
                    due, project=self.project))

    def test_future_deadline_item_wait_can_be_converted_without_ci_dispatch(self):
        item = self.store.create(actor="worker", idempotency_key="deadline-held",
                                 title="Deadline-held work")["record"]["work_item"]
        deadline = {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}
        self.store.transition(item["id"], expected_rev=1, actor="worker",
                              status="blocked", wait=deadline)
        self.ready_item("deadline-independent-ready")
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertEqual(self.project.snapshots.get()["progress"]["wait_conversion"]["wait_kind"],
                         "deadline")
        current = self.store.get(item["id"])
        self.assertEqual((current["status"], current["holder"], current["wait"]),
                         ("blocked", "worker", deadline))

    def test_live_wait_conversion_requires_ready_work_and_complete_held_item_evidence(self):
        item = self.block()
        self.dispatch()
        engine = Engine(self.config, driver=ScriptDriver())
        no_ready = engine.run(self.project, "worker")
        self.assertIs(no_ready["finish"]["wait_liveness"], True)
        self.assertIs(no_ready["finish"].get("wait_conversion"), None)
        self.assertNotIn("wait_conversion", self.project.snapshots.get()["progress"])
        self.assertEqual(self.store.get(item["id"])["status"], "blocked")

        self.ready_item("incomplete-ready")
        snapshot_before = self.project.snapshots.get()
        original = self.store.open_state_readonly
        def incomplete_page(*args, **kwargs):
            status = original(*args, **kwargs)
            status["blocked_work"]["coverage_complete"] = False
            return status
        with patch.object(self.store, "open_state_readonly", side_effect=incomplete_page):
            incomplete = engine.run(self.project, "worker")
            self.assertIs(incomplete["finish"]["wait_liveness"], True)
            current = self.project.snapshots.get()
            self.assertEqual(current["parent"], snapshot_before["id"])
            self.assertNotIn("wait_conversion", current["progress"])
            stamp = dt.datetime.fromisoformat(current["progress"]["recorded_at"]).timestamp()
            due = stamp + self.config.limits.writer_no_progress_backoff_seconds
            self.assertFalse(should_run(self.project, current, role_name="worker", current_time=due))
        # Restored completeness cannot retroactively authorize a conversion
        # that finish-time evidence did not record.
        self.assertFalse(should_run(self.project, current, role_name="worker", current_time=due))

    def test_free_standing_question_and_disabled_policy_do_not_convert_live_wait(self):
        self.ready_item()
        question = self.project.insights.submit(
            source="worker", title="External wait", body="Q: Await operator input?",
            base_snapshot=None, kind="question", addressee="operator")
        engine = Engine(self.config, driver=ScriptDriver())
        result = engine.run(self.project, "worker")
        self.assertIs(result["finish"]["wait_liveness"], True)
        self.assertNotIn("wait_conversion", self.project.snapshots.get()["progress"])
        self.assertIs(_writer_wait_liveness(self.project, self.config.roles["worker"]), True)

        # A held CI wait is a structured source, but the explicit policy can
        # preserve the requested run-level wait without changing the item.
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, writer_wait_with_ready_work=False))
        self.project.config = self.config
        item = self.block(key="disabled-policy")
        self.dispatch()
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertIs(result["finish"]["wait_liveness"], True)
        self.assertNotIn("wait_conversion", self.project.snapshots.get()["progress"])
        current = self.store.get(item["id"])
        self.assertEqual((current["status"], current["wait"]["kind"]), ("blocked", "ci"))

    def test_other_holders_wait_does_not_count_as_conversion_witness(self):
        item = self.block(key="other-holder", actor="reviewer")
        self.ready_item("writer-ready")
        self.dispatch()
        self.assertEqual(_writer_wait_sources(
            self.project, self.config.roles["worker"], include_structured_wait=True),
            (False, False, None))
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        self.assertIs(result["finish"]["wait_liveness"], False)
        self.assertNotIn("wait_conversion", self.project.snapshots.get()["progress"])
        self.assertEqual(self.store.get(item["id"])["holder"], "reviewer")

    def test_exact_held_ci_request_and_terminal_evidence(self):
        role = self.config.roles['worker']
        item = self.block()
        code = self.store.create(actor='worker', idempotency_key='self-code',
                                 title='Self code wait')['record']['work_item']
        self.store.transition(code['id'], expected_rev=1, actor='worker', status='blocked',
                              wait={'kind': 'code_change'})
        self.assertIsNone(_writer_wait_liveness(self.project, role))
        requests = CiRequests(self.project.root)
        wrong_sha = 'a' * 40
        self.assertNotEqual(wrong_sha, SHA)
        for sha in (wrong_sha, SHA):
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
