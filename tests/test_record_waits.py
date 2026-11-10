"""Work-item-only exact record closure; no delivery or status side effects."""
import copy
from dataclasses import replace
from unittest.mock import patch

from support import Fixture
from test_operator_questions import ARGS, DECLARATION
from mizu import operator_records as records
from mizu.config import load
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.project import Project
from mizu.protocol import WORK_ITEM_WAIT_SCHEMA, WAIT_SCHEMA, validate
from mizu.work_item_waits import liveness, normalize_record_wait, stored_wait


class RecordWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        text = self.file.read_text()
        index = text.index('capabilities = [', text.index('[roles.worker]')) + len('capabilities = [')
        self.file.write_text(text[:index] + '"record_write", ' + text[index:] + DECLARATION)
        self.config = load(self.file)
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        question = self.context().handle('record_write', ARGS)
        self.subject = dict(type='record', kind=question['record']['kind'],
                            id=question['record']['id'], digest=question['record_digest'])
        self.wait = dict(kind='record_closed', subject=self.subject)

    def held(self, key='wait', actor='worker'):
        seed = self.store.create(actor=actor, idempotency_key=key, title=key)['record']['work_item']
        return self.store.transition(seed['id'], expected_rev=1, actor=actor, status='doing')

    def block(self, item=None, wait=None, actor='worker'):
        item = self.held() if item is None else item
        return self.store.transition(item['id'], expected_rev=item['rev'], actor=actor,
                                     status='blocked', wait=self.wait if wait is None else wait)

    def answer(self, event_id='answer1'):
        return records.write(self.project, event_id=event_id, kind='answer', body='Use A',
                             subject=self.subject)

    def state(self, item):
        return liveness(self.store, item['wait'], None, item=item)['state']

    def test_exact_answer_is_due_without_status_delivery_or_ack_mutation(self):
        item = self.block()
        self.assertEqual(self.state(item), 'pending')
        self.assertEqual(self.store.page_ready(role_name='worker')['total'], 0)
        answer = self.answer()
        self.assertEqual(self.state(item), 'due')
        ready = self.store.page_ready(role_name='worker')
        self.assertEqual(ready['items'][0]['id'], item['id'])
        self.assertNotIn('Use A', str(ready))
        self.assertEqual(self.store.get(item['id']), item)
        self.assertEqual(self.store.explain(item['id'])['item']['wait'], self.wait)
        self.assertEqual(records.pending(self.project, self.config.roles['worker'])[0]['pending_count'], 1)
        self.assertFalse(records.store(self.project).received('worker', answer['record']['id'], answer['record_digest']))
        self.answer('answer2')
        self.config.operator_records['answer']['enabled'] = False
        self.assertEqual(self.state(item), 'due')
        self.assertTrue(records.questions(self.project, limit=10, scan_limit=100)['questions'][0]['closed'])
        current = self.store.transition(item['id'], expected_rev=item['rev'], actor='worker', status='doing')
        self.assertEqual(current['status'], 'doing')
        with self.assertRaisesRegex(Denied, 'changed'):
            self.store.transition(item['id'], expected_rev=item['rev'], actor='worker', status='doing')

    def test_registration_requires_current_holder_exact_question_and_route(self):
        item = self.held()
        for change in ({'id': 'role/worker/role.question/missing'}, {'digest': 'f' * 64},
                       {'kind': 'undeclared.question'}, {'type': 'insight'}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.block(item, dict(kind='record_closed', subject={**self.subject, **change}))
        with self.assertRaisesRegex(Denied, 'holder'):
            self.block(item, actor='reviewer')
        self.config.record_kinds['role.question']['route'] = 'none'
        with self.assertRaises(Denied):
            self.block(item)
        self.config.record_kinds['role.question']['route'] = 'operator_question'
        self.config.operator_records['answer']['enabled'] = False
        with self.assertRaisesRegex(Denied, 'answer route'):
            self.block(item)
        self.config.operator_records['answer']['enabled'] = True
        foreign = self.held('foreign', 'reviewer')
        with self.assertRaisesRegex(Denied, 'writer'):
            self.block(foreign, actor='reviewer')
        seed = self.store.create(actor='worker', idempotency_key='unheld', title='Unheld')['record']['work_item']
        with self.assertRaisesRegex(Denied, 'holder'):
            self.block(seed)
        self.assertEqual(self.store.get(item['id']), item)

    def test_normalized_history_and_work_item_only_schema(self):
        self.assertEqual(normalize_record_wait(self.wait), self.wait)
        self.assertEqual(stored_wait(self.wait), self.wait)
        validate(self.wait, WORK_ITEM_WAIT_SCHEMA)
        with self.assertRaises(Denied):
            validate(self.wait, WAIT_SCHEMA)
        with self.assertRaises(Denied):
            self.project.insights._normalize_wait(self.wait, 'defer')
        for bad in ({**self.wait, 'extra': True}, dict(kind='record_closed'),
                    dict(kind='record_closed', subject={**self.subject, 'extra': True}),
                    dict(kind='record_closed', subject={**self.subject, 'digest': 'bad'}),
                    dict(kind='record_closed', subject=dict(type='insight', id='question', rev=1))):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                stored_wait(bad)
        item = self.block()
        reopened = Project(self.config, 'sample').work_items.get(item['id'])
        self.assertEqual(reopened['wait'], self.wait)

    def test_mismatched_answers_do_not_close_and_malformed_catalog_is_unknown(self):
        item = self.block()
        answer = self.answer()['record']
        shared = records.store(self.project)
        name = shared._record_name(answer['id'])
        path = shared.root / name
        index = shared.root / 'operator-record-index' / name.rsplit('/', 1)[-1]
        # Authenticate alternate fixtures through canonical validation, not a mocked lookup.
        for label, change in (
                ('id', lambda row: row['subject'].update(id='role/worker/role.question/other')),
                ('kind', lambda row: row['subject'].update(kind='other.question')),
                ('digest', lambda row: row['subject'].update(digest='f' * 64))):
            with self.subTest(label=label):
                row = copy.deepcopy(answer)
                change(row)
                path.write_bytes(canonical(row))
                index.unlink(missing_ok=True)
                self.assertEqual(self.state(item), 'pending')
        row = copy.deepcopy(answer)
        row['payload']['addressee'] = 'reviewer'
        path.write_bytes(canonical(row))
        index.unlink(missing_ok=True)
        self.assertEqual(self.state(item), 'unknown', 'a misrouted exact-subject answer is unsafe evidence')
        for change in (lambda row: row.update(writer='role.worker'),
                       lambda row: row['subject'].update(digest='bad'),
                       lambda row: row['payload'].update(action='withdraw'),
                       lambda row: row['payload'].update(body='')):
            row = copy.deepcopy(answer)
            change(row)
            path.write_bytes(canonical(row))
            self.assertEqual(self.state(item), 'unknown')
        path.unlink()
        self.assertEqual(self.state(item), 'pending')

    def test_bound_unknown_and_projection_local_lazy_cache(self):
        first = self.block()
        second = self.block(self.held('second'))
        self.answer()
        self.project.config = replace(self.project.config, limits=replace(
            self.project.config.limits, operator_record_catalog_scan_entries=1))
        with patch.object(records, 'catalog', wraps=records.catalog) as catalog:
            page = self.store.blocked_liveness_readonly()
            self.assertEqual(catalog.call_count, 1)
            self.assertEqual(catalog.call_args.kwargs, dict(readonly=True, scan_limit=1))
        self.assertEqual({row['state'] for row in page['items']}, {'unknown'})
        self.assertIsNone(page['counts'])
        aged = {**first, 'blocked_since': '2000-01-01T00:00:00+00:00'}
        self.assertEqual(self.state(aged), 'unknown')
        self.assertEqual(self.store.page_ready(role_name='worker')['total'], 0)
        self.project.config = replace(self.project.config, limits=replace(
            self.project.config.limits, operator_record_catalog_scan_entries=2048))
        with patch.object(records, 'catalog', wraps=records.catalog) as catalog:
            self.assertEqual(self.store.page_ready(role_name='worker')['total'], 2)
            self.assertEqual(catalog.call_count, 1)
        self.assertEqual(self.store.get(first['id']), first)
        self.assertEqual(self.store.get(second['id']), second)
        with patch.object(records, 'catalog', side_effect=OSError('unavailable')) as catalog:
            self.assertEqual({row['state'] for row in self.store.blocked_liveness_readonly()['items']}, {'unknown'})
            self.assertEqual(catalog.call_count, 1)
        self.config.record_kinds.clear()
        self.assertEqual(self.state(first), 'unknown')

    def test_lazy_catalog_and_existing_insight_answer_lookup_are_independent(self):
        with patch.object(records, 'catalog', side_effect=AssertionError('not lazy')):
            self.store.page_ready(role_name='worker')
            records.record_question_lookup(self.project)
        question = self.project.insights.submit(source='worker', title='Legacy question',
            body='Q: Which?', base_snapshot=None, kind='question', addressee='operator')
        records.write(self.project, event_id='legacy', kind='answer', body='Legacy answer',
                      subject=dict(type='insight', id=question['id'], rev=1))
        self.project.config = replace(self.project.config, limits=replace(
            self.project.config.limits, operator_record_catalog_scan_entries=1))
        with patch.object(records, 'catalog', wraps=records.catalog) as catalog:
            self.assertTrue(records.answer_lookup(self.project)(question))
            self.assertEqual(catalog.call_args.kwargs, dict(readonly=True))
        self.assertIsNone(records.record_question_lookup(self.project)(self.subject))
