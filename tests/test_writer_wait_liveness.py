"""Offline writer wait fallback: bounded local evidence, no provider calls."""
import dataclasses
import datetime as dt
from unittest.mock import patch

from support import Fixture, ScriptDriver, forget_ledger
from test_ci_waits import CiWaitFixture, SHA
from mizu import operator_records as records
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import canonical, now, read_json
from support import history_write_json as write_json
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

    def answer_record(self, question, event='answer'):
        return records.write(self.project, event_id=event, kind='answer', body='Operator answer',
                             subject={'type': 'insight', 'id': question['id'], 'rev': question['rev']})

    def blocked_row(self, item):
        return next(row for row in self.project.status()['blocked_work']['items']
                    if row['id'] == item['id'])

    def test_operator_answer_annotates_pending_wait_without_transition_or_decision(self):
        question = self.question()
        item = self.block('operator answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        answer = self.answer_record(question)
        # A replay is the same immutable record, not ambiguous evidence.
        self.assertEqual(self.answer_record(question), answer)
        row = self.blocked_row(item)
        self.assertEqual((row['state'], row['reason'], row['self_resolvable']),
                         ('pending', 'undecided', 'answered_own_question'))
        self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)
        self.assertEqual(self.project.work_items.get(item['id']), item)
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{question['id']}.json"))
        # The exact answer suppresses the open-question source for this revision;
        # the held wait itself is now reported as self-resolvable, without
        # deciding for its writer.
        self.assertEqual(_writer_wait_sources(self.project, self.role(), include_structured_wait=True),
                         (False, True, None))
        self.assertEqual(_writer_wait_sources(self.project, self.role()),
                         (False, True, None))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        self.project.insights.decide(question['id'], 'defer', 'Await answer handling',
                                     'After reading the answer', 'fixture', expected_rev=1)
        self.assertEqual((self.blocked_row(item)['state'], self.blocked_row(item)['self_resolvable']),
                         ('pending', 'answered_own_question'))
        self.project.insights.decide(question['id'], 'reject', 'Handled answer', '', 'fixture', expected_rev=1)
        self.assertEqual(self.blocked_row(item)['state'], 'due')

    def test_answered_own_question_suppresses_live_source_and_backoff_resumes(self):
        question = self.question()
        item = self.block('answered backoff', {'kind': 'insight_decided', 'insight': question['id']})
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.answer_record(question)
        row = self.blocked_row(item)
        self.assertEqual((row['state'], row['reason'], row['self_resolvable']),
                         ('pending', 'undecided', 'answered_own_question'))
        self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)
        self.assertEqual(self.project.work_items.get(item['id']), item)
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{question['id']}.json"))
        self.assertEqual(_writer_wait_sources(self.project, self.role(), include_structured_wait=True),
                         (False, True, None))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        engine = Engine(self.config, driver=ScriptDriver())
        result = engine.run(self.project, 'worker')
        self.assertIs(result['finish']['wait_liveness'], False)
        snapshot = self.project.snapshots.get()
        due = self.backoff_time(snapshot)
        self.assertFalse(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, due - 1, project=self.project))
        self.assertTrue(_writer_progress_fallback(
            snapshot, self.config, self.role(), False, due, project=self.project))
        self.assertTrue(should_run(self.project, snapshot, role_name='worker', current_time=due))
        self.assertEqual(self.blocked_row(item)['state'], 'pending')
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{question['id']}.json"))
        self.assertEqual(self.project.work_items.get(item['id'])['status'], 'blocked')

    def test_unanswered_own_and_foreign_questions_remain_live(self):
        question = self.question()
        item = self.block('answered holder', {'kind': 'insight_decided', 'insight': question['id']})
        self.answer_record(question)
        self.assertEqual(_writer_wait_sources(self.project, self.role(), include_structured_wait=True),
                         (False, True, None))
        # An unrelated unanswered own question stays an external live source.
        other = self.project.insights.submit(
            source='worker', title='Other fixture question', body='Q: Other question?',
            base_snapshot=None, kind='question', addressee='operator')
        self.assertEqual(_writer_wait_sources(self.project, self.role(), include_structured_wait=True),
                         (True, True, None))
        self.assertEqual(_writer_wait_sources(self.project, self.role()),
                         (True, False, None))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        engine = Engine(self.config, driver=ScriptDriver())
        snapshot = engine.run(self.project, 'worker')['finish']
        # Snapshot is not needed for the live check; use the stored snapshot.
        stored = self.project.snapshots.get()
        due = self.backoff_time(stored)
        self.assertFalse(_writer_progress_fallback(
            stored, self.config, self.role(), False, due, project=self.project))
        self.assertEqual(self.blocked_row(item)['self_resolvable'], 'answered_own_question')
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{question['id']}.json"))
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{other['id']}.json"))
        # A held wait on a foreign unanswered question is also external.
        foreign = self.question('reviewer')
        foreign_item = self.block('foreign holder', {'kind': 'insight_decided', 'insight': foreign['id']})
        self.assertIsNone(self.blocked_row(foreign_item)['self_resolvable'])
        self.assertEqual(self.blocked_row(foreign_item)['state'], 'pending')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertFalse(_writer_progress_fallback(
            stored, self.config, self.role(), False, due, project=self.project))

    def test_answered_liveness_stays_live_on_unavailable_or_ambiguous_evidence(self):
        question = self.question()
        item = self.block('fail-closed live', {'kind': 'insight_decided', 'insight': question['id']})
        self.answer_record(question)
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        with patch.object(records, 'catalog', side_effect=Denied('Unavailable')):
            self.assertIsNone(self.blocked_row(item)['self_resolvable'])
            self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
            stored = self.project.snapshots.get() if self.project.snapshots.get() else None
            # Fallback needs a wait snapshot; use a fresh engine run under the failure.
            # The live question alone suppresses the self-wait backoff.
            self.assertEqual(_writer_wait_sources(self.project, self.role()), (True, False, None))
        self.assertEqual(self.blocked_row(item)['self_resolvable'], 'answered_own_question')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        current = self.project.insights.read(question['id'])
        self.answer_record(current, 'second-answer')
        row = self.blocked_row(item)
        self.assertIsNone(row['self_resolvable'])
        self.assertEqual(row['state'], 'pending')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.assertEqual(_writer_wait_sources(self.project, self.role(), include_structured_wait=True)[0], True)

    def test_stale_decision_does_not_settle_current_revision_and_defer_alone_stays_live(self):
        question = self.question()
        item = self.block('stale decision', {'kind': 'insight_decided', 'insight': question['id']})
        self.project.insights.decide(question['id'], 'accept', 'Old answer', '', 'fixture', expected_rev=1)
        self.assertEqual(self.blocked_row(item)['state'], 'due')
        self.project.insights.revise(question['id'], source='worker', title='Follow-up',
                                     body='Q: New revision?', base_snapshot=None, expected_rev=1)
        current = self.project.insights.read(question['id'])
        # The old answer (rev 1) does not settle the new revision.
        self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
        self.answer_record(current, 'current-answer')
        row = self.blocked_row(item)
        self.assertEqual((row['state'], row['self_resolvable']), ('pending', 'answered_own_question'))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        # Defer alone, without an exact answer, is not self-resolvable and stays live.
        other = self.project.insights.submit(
            source='worker', title='Defer-alone question', body='Q: Defer without answer?',
            base_snapshot=None, kind='question', addressee='operator')
        other_item = self.block('defer alone', {'kind': 'insight_decided', 'insight': other['id']})
        self.project.insights.decide(other['id'], 'defer', 'Await operator', 'Later', 'fixture', expected_rev=1)
        self.assertIsNone(self.blocked_row(other_item)['self_resolvable'])
        self.assertEqual(self.blocked_row(other_item)['state'], 'pending')
        self.assertIs(_writer_wait_liveness(self.project, self.role()), True)

    def test_answer_evidence_is_projection_local_lazy_and_readonly(self):
        question = self.question()
        items = [self.block(f'wait {i}', {'kind': 'insight_decided', 'insight': question['id']})
                 for i in range(2)]
        answer = self.answer_record(question)
        # A missing disposable reference must be validated without writing it.
        from mizu import operator_record_index as index
        shared = records.store(self.project)
        ref = self.project.root / index.DIRECTORY / shared._record_name(answer['record']['id']).split('/')[-1]
        ref.unlink()
        with patch.object(records, 'catalog', wraps=records.catalog) as scan, \
             patch.object(index, 'atomic_write', side_effect=AssertionError('Read-only evidence wrote')):
            state = self.project.work_items.open_state_readonly(writer_roles={'worker'})
            self.assertEqual(scan.call_count, 1)
            scan.assert_called_once_with(scan.call_args.args[0], readonly=True)
            self.assertEqual([r['self_resolvable'] for r in state['blocked_work']['items']],
                             ['answered_own_question'] * 2)
            self.project.work_items.open_state_readonly()  # No writer annotation requested.
            self.assertEqual(scan.call_count, 1)
        self.assertFalse(ref.exists())
        # Changed evidence is not retained across projections; failures are not
        # retried per blocked item within one projection either.
        with patch.object(records, 'catalog', side_effect=Denied('Unavailable')) as scan:
            state = self.project.work_items.open_state_readonly(writer_roles={'worker'})
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(all(r['self_resolvable'] is None for r in state['blocked_work']['items']))
            self.assertTrue(all(r['state'] == 'pending' for r in state['blocked_work']['items']))
        self.assertEqual(self.project.work_items.get(items[0]['id']), items[0])

    def test_stale_changed_or_ambiguous_operator_answers_do_not_prove_self_wait(self):
        question = self.question()
        item = self.block('revision answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.answer_record(question)
        # A current revision change between the liveness read and route read
        # cannot use the observed old revision as a current-answer witness.
        with patch.object(self.project.insights, 'read', return_value={**question, 'rev': 2}):
            self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        self.project.insights.revise(question['id'], source='worker', title='Follow-up',
                                     body='Q: New revision?', base_snapshot=None, expected_rev=1)
        self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        current = self.project.insights.read(question['id'])
        self.answer_record(current, 'current-answer')
        self.assertEqual(self.blocked_row(item)['self_resolvable'], 'answered_own_question')
        self.answer_record(current, 'another-current-answer')
        row = self.blocked_row(item)
        self.assertIsNone(row['self_resolvable'])
        self.assertEqual(row['state'], 'pending')

    def test_answer_policy_corruption_and_route_fail_closed(self):
        question = self.question()
        item = self.block('unsafe answer', {'kind': 'insight_decided', 'insight': question['id']})
        answer = self.answer_record(question)
        policy = self.config.operator_records['answer']
        with patch.dict(policy, enabled=False), patch.object(records, 'catalog') as scan:
            self.assertIsNone(self.blocked_row(item)['self_resolvable'])
            scan.assert_not_called()
        # Wake policy does not govern whether authenticated answer evidence exists.
        with patch.dict(policy, wake=False):
            self.assertEqual(self.blocked_row(item)['self_resolvable'], 'answered_own_question')
        with patch.object(self.project.insights, 'read', side_effect=Denied('Body unavailable')):
            self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        shared = records.store(self.project)
        path = self.project.root / shared._record_name(answer['record']['id'])
        original = path.read_bytes()
        for field, value in (('writer', 'worker'), ('payload', {**answer['record']['payload'],
                                                               'addressee': 'reviewer'})):
            with self.subTest(field=field):
                path.write_bytes(canonical({**answer['record'], field: value}))
                row = self.blocked_row(item)
                self.assertIsNone(row['self_resolvable'])
                self.assertEqual(row['state'], 'pending')
        path.write_bytes(b'{')
        self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        path.unlink()
        self.assertIsNone(self.blocked_row(item)['self_resolvable'])
        path.write_bytes(original)
        self.assertEqual(self.blocked_row(item)['self_resolvable'], 'answered_own_question')

    def test_non_own_question_waits_never_scan_operator_answers(self):
        targets = [self.question('reviewer'), self.project.insights.submit(
            source='worker', title='Own proposal', body='Proposal, not a question',
            base_snapshot=None, kind='proposal', addressee='operator')]
        for i, target in enumerate(targets):
            self.answer_record(target, f'answer-{i}')
            self.block(f'negative {i}', {'kind': 'insight_decided', 'insight': target['id']})
        self.block('dependency answer', {'kind': 'dependency', 'recipient': 'reviewer',
            'requires': {'insight': targets[0]['id'], 'rev': 1, 'action': 'accept'}})
        self.block('code only', {'kind': 'code_change'})
        with patch.object(records, 'catalog') as scan:
            state = self.project.work_items.open_state_readonly(writer_roles={'worker'})
            scan.assert_not_called()
        rows = [row for row in state['blocked_work']['items'] if row['wait_kind'] != 'code_change']
        self.assertTrue(all(row['self_resolvable'] is None for row in rows))

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
            self.project.work_items.transition(item['id'], expected_rev=item['rev'], actor='worker',
                                               status='dropped', reason='Fixture settled')

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
        forget_ledger(self.project.root)
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
        forget_ledger(self.project.root)
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
        forget_ledger(self.project.root)
        self.assertIsNone(_writer_wait_liveness(self.project, self.role()))

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

    def test_terminal_no_dispatch_does_not_claim_remote_ci_wait_liveness(self):
        self.block()
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        store = CiRequests(self.project.root, self.config.ci_requests)
        marker = {"request_id": identity, "publication_id": self.receipt["publication_id"],
                  "sha": SHA, "workflows": ["ci"], "dispatched_at": now(), "attempt": 1}
        store.mark_dispatch(identity, marker)
        store.record_outcome(identity, {
            "request_id": identity, "publication_id": self.receipt["publication_id"],
            "sha": SHA, "workflows": ["ci"], "runs": [], "requested_at": now(),
            "attempt": 1, "outcome": "not_dispatched", "reason_code": "provider_rejected"})
        self.assertEqual(_writer_wait_sources(
            self.project, self.config.roles["worker"], include_structured_wait=True),
            (None, False, None))

    def test_unknown_ci_inventory_refuses_parking_only_for_its_holder(self):
        self.block(key="other-unknown", actor="reviewer")
        args = {"outcome": "wait", "summary": "Waiting on external results",
                "state": "Now: no eligible ready work"}
        unknown = {"state": "unknown", "reason": "unavailable", "wait_kind": "ci"}
        with patch("mizu.work_items.liveness", return_value=unknown):
            self.assertTrue(self.context().handle("finish", args)["sealed"])
            self.block(key="own-unknown")
            ctx = self.context()
            page = self.store.page_ready(role_name="worker")
            self.assertEqual(page["total"], 0)
            self.assertFalse(page["coverage_complete"])
            with self.assertRaisesRegex(Denied, "evidence unavailable or incomplete"):
                ctx.handle("finish", args)
            self.assertIsNone(ctx.finished)
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

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


class WriterParkingTests(Fixture):
    def ready(self, key):
        return self.project.work_items.create(
            actor="worker", idempotency_key=key, title=key)["record"]["work_item"]

    def finish(self, ctx, outcome="wait", **extra):
        return ctx.handle("finish", {"outcome": outcome, "summary": "Disposition check",
                                    "state": "Now: observed.\nNext: Inspect the remaining work", **extra})

    def assert_unsealed(self, ctx):
        self.assertIsNone(ctx.finished)
        self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

    def test_all_parking_outcomes_refuse_open_and_held_doing(self):
        item = self.ready("ready")
        ctx = self.context()
        for status in ("open", "doing"):
            if status == "doing":
                item = self.project.work_items.transition(
                    item["id"], expected_rev=item["rev"], actor="worker", status=status)
            for outcome in ("wait", "blocked", "continue", "bounded"):
                with self.subTest(status=status, outcome=outcome):
                    ctx.bound_hit = "drain" if outcome == "bounded" else None
                    ctx.wrap_up_attempted = outcome == "bounded"
                    with self.assertRaisesRegex(Denied, "held doing item is still ready"):
                        self.finish(ctx, "wait" if outcome == "bounded" else outcome)
                    self.assert_unsealed(ctx)
        self.assertEqual(self.project.work_items.get(item["id"]), item)

    def test_every_ready_item_needs_a_durable_disposition(self):
        from support import bind_delivery
        store = self.project.work_items
        captured = self.project.snapshots.capture_files(self.project.workspace)
        snapshot = self.project.snapshots.create(
            captured, goal=self.project.goal, state="Verified delivery", run=None,
            outcome="done", summary="Fixture verification", verification={
                "passed": True, "code_digest": captured["code_digest"], "run": "fixture"})
        self.project.snapshots.publish(snapshot)
        ctx = self.context()
        items = [self.ready(str(index)) for index in range(3)]
        binding = bind_delivery(self.project, items[0]["id"], snapshot["id"])
        # One completion, one named observable blocker and one reasoned drop.
        store.transition(items[0]["id"], expected_rev=binding["rev"], actor="worker", status="done",
                         snapshot_id=snapshot["id"])
        store.transition(items[1]["id"], expected_rev=1, actor="worker", status="blocked",
                         wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        with self.assertRaisesRegex(Denied, "1 ready item"):
            self.finish(ctx)
        self.assert_unsealed(ctx)
        store.transition(items[2]["id"], expected_rev=1, actor="worker", status="dropped",
                         reason="No longer needed")
        self.assertTrue(self.finish(ctx)["sealed"])

    def test_finish_empty_signature_does_not_rewake_for_own_dispositions(self):
        item = self.ready("owned disposition")
        def dispose_and_wait(ctx, *_):
            ctx.handle("work_items_transition", {
                "id": item["id"], "expected_rev": 1, "status": "blocked",
                "wait": {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"}})
            self.finish(ctx)
        engine = Engine(self.config, driver=ScriptDriver(dispose_and_wait))
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        snapshot = self.project.snapshots.get()
        self.assertEqual(snapshot["progress"]["ready_work_signature"],
                         self.project.work_items.page_ready(role_name="worker")["signature"])
        self.assertFalse(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(engine.run(self.project, "worker", event_trigger=True)["skipped"], "no-event")

    def test_finish_inventory_uses_api_bound_not_smaller_prompt_page(self):
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=1))
        self.project.config = self.config
        self.ready("first ready")
        self.ready("second ready")
        with self.assertRaisesRegex(Denied, r"2 ready item\(s\)"):
            self.finish(self.context())

    def test_other_role_held_work_does_not_block_writer(self):
        item = self.ready("other holder")
        self.project.work_items.transition(item["id"], expected_rev=1, actor="reviewer", status="doing")
        self.assertTrue(self.finish(self.context())["sealed"])

    def test_unknown_incomplete_truncated_and_unreadable_inventory_refuse(self):
        ctx = self.context()
        store = self.project.work_items
        page = store.page_ready(role_name="worker")
        for bad in ({**page, "coverage_complete": False},
                    {**page, "truncated": True}, {**page, "total": None},
                    {**page, "items": [], "total": 1}, {**page, "signature": None}, {}):
            with self.subTest(page=bad), patch.object(store, "page_ready", return_value=bad):
                with self.assertRaisesRegex(Denied, "evidence unavailable or incomplete"):
                    self.finish(ctx)
                self.assert_unsealed(ctx)
        for error in (Denied, Busy, LimitExceeded, OSError):
            with self.subTest(error=error), patch.object(store, "page_ready", side_effect=error("fixture")):
                with self.assertRaisesRegex(Denied, "evidence unavailable or incomplete"):
                    self.finish(ctx)
                self.assert_unsealed(ctx)
        # The finish scan uses the bounded page API maximum, not the smaller
        # prompt display setting; a real 101-row ready set is incomplete.
        from mizu.work_items import MAX_OPEN_PAGE_ROWS
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=1, open_work_items=MAX_OPEN_PAGE_ROWS + 1))
        self.project.config = self.config
        self.project.insights.open_work_items = MAX_OPEN_PAGE_ROWS + 1
        for index in range(MAX_OPEN_PAGE_ROWS + 1):
            self.ready(f"overflow-{index}")
        with self.assertRaisesRegex(Denied, "evidence unavailable or incomplete"):
            self.finish(ctx)
        self.assert_unsealed(ctx)

    def test_refusal_does_not_ack_events_and_retry_can_seal(self):
        records.write(self.project, event_id="parking-event", kind="operator.notice", body="Handle this",
                      addressee="worker")
        item = self.ready("ack retry")
        def run(ctx, prompt, *_):
            tokens = [entry.ack_token for entry in ctx.operator_record_manifest]
            self.assertTrue(tokens)
            with self.assertRaisesRegex(Denied, "1 ready item"):
                self.finish(ctx, ack_events=tokens)
            self.assert_unsealed(ctx)
            self.assertTrue(records.pending(self.project, ctx.role)[0]["wake_pending"])
            self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                               status="dropped", reason="Handled")
            self.finish(ctx, ack_events=tokens)
        self.assertEqual(Engine(self.config, driver=ScriptDriver(run)).run(
            self.project, "worker")["status"], "completed")
        self.assertFalse(records.pending(self.project, self.config.roles["worker"])[0]["wake_pending"])
        self.assertNotIn("wait_conversion", self.project.snapshots.get()["progress"])

    def test_progress_backed_new_next_permits_ready_work_and_is_immediate(self):
        from mizu.runtime import _writer_next_task
        self.ready("handoff")
        def run(ctx, *_):
            (ctx.workspace / "new.py").write_text("VALUE = 1\n")
            self.finish(ctx, "continue")
        self.assertEqual(Engine(self.config, driver=ScriptDriver(run)).run(
            self.project, "worker")["status"], "completed")
        snapshot = self.project.snapshots.get()
        self.assertEqual(_writer_next_task(self.project, snapshot, "worker"), "Inspect the remaining work")
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        # Repeating a handoff is parking, even with fresh code progress.
        ctx = self.context()
        (ctx.workspace / "new.py").write_text("VALUE = 2\n")
        with self.assertRaisesRegex(Denied, "1 ready item"):
            self.finish(ctx, "continue")
        self.assert_unsealed(ctx)

    def test_historical_conversions_validate_but_never_authorize_ready_only_timers(self):
        from mizu.runtime import _progress_record
        from mizu.snapshot import PROGRESS_SIGNALS, validate_progress
        self.config.roles["worker"] = dataclasses.replace(self.config.roles["worker"], on_change=True)
        self.ready("historical ready")
        base = self.project.snapshots.get()
        signature = self.project.work_items.page_ready(role_name="worker")["signature"]
        for schema, kind, reason in (
                (6, "ci", "ready_work_with_pending_structured_wait"),
                (7, None, "ready_work_without_proven_structured_wait")):
            conversion = {"status": "converted", "reason": reason, "wait_kind": kind,
                          "backoff_seconds": 300}
            progress = _progress_record(
                base, "worker", self.config.limits.progress_signals,
                {name: False for name in PROGRESS_SIGNALS}, "wait", ready_work=True,
                ready_work_signature=signature, wait_resolution_tokens=[])
            progress.update(schema=schema, wait_conversion=conversion)
            self.assertEqual(validate_progress(progress)["schema"], schema)
            with self.assertRaises(Denied):
                validate_progress({**progress, "wait_conversion": {**conversion, "backoff_seconds": 0}})
            snapshot = self.project.snapshots.create(
                self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
                state="Historical parked wait", run=None, outcome="wait", summary="Old snapshot",
                wake_at=4102444800, inbox_seen=self.project.insights.generation(),
                wake_generation=self.project.control()["wake_generation"], progress=progress)
            self.project.snapshots.publish(snapshot)
            due = dt.datetime.fromisoformat(progress["recorded_at"]).timestamp() + 301
            self.assertFalse(should_run(self.project, snapshot, role_name="worker", current_time=due))
            for live in (True, False, None):
                with patch("mizu.runtime._writer_wait_sources", return_value=(live, False, None)):
                    self.assertFalse(_writer_progress_fallback(
                        snapshot, self.config, self.config.roles["worker"], True, due, project=self.project))
