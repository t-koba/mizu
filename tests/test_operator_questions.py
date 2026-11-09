"""Opt-in host question listing and exact authenticated role-record replies."""
import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import fs, host_records, operator_records as records
from mizu.cli import execute, parser
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import ATOMIC_WRITE_TEMP_PREFIX, canonical
from mizu.project import Project
from mizu.shared_records import SharedRecords

DECLARATION = '''
[record_kinds."role.question"]
writer = "role.worker"
route = "operator_question"
wake = false
retention = "permanent"
acknowledgement = "explicit"
subject = "object"
maximum = 8192
'''
ARGS = dict(kind='role.question', id='question1', subject='{}',
            payload='{"body":"Which option?","source":"reviewer"}')


class OperatorQuestionTests(Fixture):
    def setUp(self):
        super().setUp()
        text = self.file.read_text()
        index = text.index('capabilities = [', text.index('[roles.worker]')) + len('capabilities = [')
        self.file.write_text(text[:index] + '"record_write", "record_read", ' + text[index:] + DECLARATION)
        self.config = load(self.file)
        self.project = Project(self.config, 'sample')

    def cli(self, *args):
        return execute(parser().parse_args(['--config', str(self.file), *args]))

    def question(self, **args):
        return self.context().handle('record_write', {**ARGS, **args})

    def subject(self, result):
        return dict(type='record', kind=result['record']['kind'],
                    id=result['record']['id'], digest=result['record_digest'])

    def answer(self, subject, **kwargs):
        return records.write(self.project, event_id='answer1', kind='answer', body='Use option A',
                             subject=subject, **kwargs)

    def shared(self):
        return host_records.store(self.project.root, self.project.snapshots, self.config.record_kinds)

    def test_cli_exact_replay_delivery_and_explicit_acknowledgement(self):
        question = self.question()
        subject = self.subject(question)
        page = self.cli('record-questions', 'sample', '--limit', '1', '--scan-limit', '10')
        self.assertEqual(page, dict(questions=[dict(subject=subject, **question)], scanned=1, truncated=False))
        argv = ['record', 'sample', 'publish', '--id', 'answer1', '--kind', 'answer',
                '--body', 'Use option A', '--subject-kind', subject['kind'],
                '--subject-id', subject['id'], '--subject-digest', subject['digest']]
        answer = self.cli(*argv)
        self.assertEqual(answer['record']['payload']['addressee'], 'worker')
        self.assertEqual(answer['record']['subject'], subject)
        with patch('mizu.shared_records.atomic_write') as write:
            self.assertEqual(self.cli(*argv), answer)
            self.assertEqual(self.question(), question)
            write.assert_not_called()
        with self.assertRaises(Denied):
            records.write(self.project, event_id='answer1', kind='answer', body='Other', subject=subject)
        page, manifest = records.pending(self.project, self.config.roles['worker'])
        self.assertEqual(page['events'][0]['subject'], subject)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(records.pending(self.project, self.config.roles['reviewer'])[0]['events'], [])
        context = self.context()
        records.bind(context, (page, manifest))
        read = records.read(context, {'ack_token': manifest[0].ack_token})
        self.assertIn('Use option A', read['text'])
        records.acknowledge(context, [])
        self.assertEqual(records.pending(self.project, self.config.roles['worker'])[0]['pending_count'], 1)
        records.acknowledge(context, [manifest[0].ack_token])
        self.assertEqual(records.pending(self.project, self.config.roles['worker'])[0]['pending_count'], 0)
        # Answering/acknowledging never closes, supersedes or acknowledges the question.
        self.assertEqual(records.questions(self.project, limit=20, scan_limit=1000)['questions'][0]['subject'], subject)
        self.assertFalse(self.shared().received('worker', subject['id'], subject['digest']))

    def test_bounded_listing_and_disabled_route_performs_no_scan(self):
        for index in range(4):
            self.question(id=f'q{index}')
        shared = self.shared()
        with patch.object(shared, '_read', wraps=shared._read) as read, \
                patch('mizu.host_records.store', return_value=shared):
            page = records.questions(self.project, limit=1, scan_limit=2)
        self.assertEqual((len(page['questions']), page['scanned'], page['truncated']), (1, 2, True))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(records.questions(self.project, limit=10, scan_limit=1)['scanned'], 1)
        full = records.questions(self.project, limit=10, scan_limit=10)
        self.assertEqual((len(full['questions']), full['truncated']), (4, False))
        self.assertEqual(full, records.questions(self.project, limit=10, scan_limit=10))
        with patch.object(Path, 'glob', side_effect=AssertionError('bounded scan must not sort all files')):
            bounded = records.questions(self.project, limit=1, scan_limit=2)
        self.assertEqual(bounded['scanned'], 2)
        for key in ('limit', 'scan_limit'):
            for value in (0, -1, True, '2'):
                args = {'limit': 20, 'scan_limit': 1000}
                args[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(Denied):
                    records.questions(self.project, **args)
        with self.assertRaises(SystemExit):
            self.cli('record-questions', 'sample')
        policies = copy.deepcopy(self.config.record_kinds)
        policies['role.question']['route'] = 'none'
        with patch.object(self.project, 'config', replace(self.config, record_kinds=policies)), \
                patch.object(SharedRecords, 'records', side_effect=AssertionError('must not scan')):
            self.assertEqual(records.questions(self.project, limit=20, scan_limit=1000)['questions'], [])

    def test_listing_skips_a_live_atomic_write_stage(self):
        shared = self.shared()
        record_id = 'role/worker/role.question/question-active'
        record_path = shared.root / shared._record_name(record_id)
        original_posix_publish = fs._rename_exclusive_posix
        original_windows_publish = fs.os.rename
        observed = []

        def list_before_posix_publish(source, destination):
            if Path(destination) == record_path:
                self.assertTrue(Path(source).name.startswith(ATOMIC_WRITE_TEMP_PREFIX))
                observed.append(records.questions(self.project, limit=20, scan_limit=1))
            return original_posix_publish(source, destination)

        def list_before_windows_publish(source, destination):
            if Path(destination) == record_path:
                self.assertTrue(Path(source).name.startswith(ATOMIC_WRITE_TEMP_PREFIX))
                observed.append(records.questions(self.project, limit=20, scan_limit=1))
            return original_windows_publish(source, destination)

        with patch.object(fs, '_rename_exclusive_posix', side_effect=list_before_posix_publish), \
                patch.object(fs.os, 'rename', side_effect=list_before_windows_publish):
            question = self.question(id='question-active')

        self.assertEqual(observed, [{'questions': [], 'scanned': 0, 'truncated': True}])
        self.assertEqual(records.questions(self.project, limit=20, scan_limit=10), {
            'questions': [dict(subject=self.subject(question), **question)],
            'scanned': 1, 'truncated': False})

    def test_listing_skips_orphan_atomic_stages_and_counts_them_in_scan_budget(self):
        question = self.question()
        shared = self.shared()
        record_path = shared.root / shared._record_name(question['record']['id'])
        # Simulate a process interruption after atomic_write created its stage.
        staging = record_path.parent / f"{ATOMIC_WRITE_TEMP_PREFIX}interrupted"
        staging.write_bytes(b"partial record bytes")

        one_budget_short = records.questions(self.project, limit=20, scan_limit=2)
        self.assertEqual(one_budget_short, {
            'questions': [dict(subject=self.subject(question), **question)],
            'scanned': 1, 'truncated': True})
        enough_budget = records.questions(self.project, limit=20, scan_limit=3)
        self.assertEqual(enough_budget, {
            'questions': [dict(subject=self.subject(question), **question)],
            'scanned': 1, 'truncated': False})

        # The staging exception is narrow; an unrelated unexpected entry still
        # refuses the listing instead of being mistaken for an ignorable stage.
        unexpected = record_path.parent / 'unexpected-entry'
        unexpected.write_bytes(b'not a record')
        with self.assertRaises(Denied):
            records.questions(self.project, limit=20, scan_limit=10)
        unexpected.unlink()
        # The reserved prefix alone is not enough: a directory is not a
        # regular, singly linked atomic_write stage.
        (record_path.parent / f"{ATOMIC_WRITE_TEMP_PREFIX}unexpected-directory").mkdir()
        with self.assertRaises(Denied):
            records.questions(self.project, limit=20, scan_limit=10)

    def test_listing_filters_unrouted_data_and_does_not_read_other_namespaces(self):
        self.config.record_kinds['role.note'] = {
            **self.config.record_kinds['role.question'], 'route': 'none'}
        self.question(kind='role.note', id='not-a-question')
        question = self.question()
        shared = self.shared()
        other = self.project.root / 'shared-records' / 'host'
        other.mkdir()
        (other / 'unrelated.json').write_text('not a record')
        with patch.object(shared, '_read', wraps=shared._read) as read, \
                patch('mizu.host_records.store', return_value=shared):
            page = records.questions(self.project, limit=20, scan_limit=1000)
        self.assertEqual(page['scanned'], 2)
        self.assertEqual([row['subject'] for row in page['questions']], [self.subject(question)])
        self.assertEqual(read.call_count, 2)
        # A corrupt *scanned* source never becomes a complete empty result.
        path = shared.root / shared._record_name(question['record']['id'])
        path.write_text('{}')
        with self.assertRaises(Denied):
            records.questions(self.project, limit=20, scan_limit=1000)

    def test_missing_changed_undeclared_or_spoofed_targets_are_refused(self):
        subject = self.subject(self.question())
        for change in ({'digest': '0' * 64}, {'id': subject['id'] + '/missing'},
                       {'kind': 'undeclared'}, {'kind': 'operator.notice'},
                       {'id': subject['id'].replace('/worker/', '/reviewer/')},
                       {'source': 'reviewer'}, {'digest': True}, {'type': 'other'}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.answer({**subject, **change})
        with self.assertRaises(Denied):
            self.answer(subject, addressee='reviewer')
        with self.assertRaises(Denied):
            records.write(self.project, event_id='notice1', kind='operator.notice', body='Notice',
                          subject=subject, addressee='worker')
        self.assertEqual(records.pending(self.project, self.config.roles['worker'])[0]['events'], [])
        self.answer(subject)
        context = self.context()
        records.bind(context)
        token = context.operator_record_manifest[0].ack_token
        shared = self.shared()
        path = shared.root / shared._record_name(subject['id'])
        envelope, _ = shared.get(subject['id'])
        envelope['payload']['body'] = 'Changed after answer'
        path.write_bytes(canonical(envelope))
        with self.assertRaises(Denied):
            self.answer(subject)
        with self.assertRaises(Denied):
            records.pending(self.project, self.config.roles['worker'])
        # The unit's frozen reply manifest remains valid after later target
        # corruption and route removal; pending delivery validated the binding.
        policies = copy.deepcopy(self.config.record_kinds)
        policies['role.question']['route'] = 'none'
        with patch.object(self.project, 'config', replace(self.config, record_kinds=policies)):
            self.assertIn('Use option A', records.read(context, {'ack_token': token})['text'])
            records.acknowledge(context, [token])

    def test_current_route_writer_and_role_authority_are_required(self):
        subject = self.subject(self.question())
        for changes in ({'route': 'none'}, {'writer': 'role.reviewer'}, {'writer': 'role.absent'}):
            policies = copy.deepcopy(self.config.record_kinds)
            policies['role.question'].update(changes)
            with self.subTest(changes=changes), \
                    patch.object(self.project, 'config', replace(self.config, record_kinds=policies)), \
                    self.assertRaises(Denied):
                self.answer(subject)
        with patch.object(self.project, 'roles', ['reviewer']), self.assertRaises(Denied):
            self.answer(subject)
        policies = copy.deepcopy(self.config.operator_records)
        policies['answer']['enabled'] = False
        with patch.object(self.project, 'config', replace(self.config, operator_records=policies)), \
                self.assertRaises(Denied):
            self.answer(subject)
        envelope, _ = self.shared().get(subject['id'])
        envelope['writer'] = 'role.reviewer'
        with self.assertRaises(Denied):
            self.shared().put(envelope, writer='role.reviewer')
        envelope['writer'] = 'role.worker'
        envelope['id'] = 'role/reviewer/role.question/forged'
        computed = self.shared().put(envelope, writer='role.worker')
        with self.assertRaises(Denied):
            self.answer(dict(type='record', kind='role.question', id=envelope['id'], digest=computed))
        with self.assertRaises(Denied):
            records.questions(self.project, limit=20, scan_limit=1000)

    def test_opt_in_wake_and_closed_policy(self):
        with patch('mizu.host_records._wake') as wake:
            self.question()
            wake.assert_not_called()
            self.config.record_kinds['role.question']['wake'] = True
            self.question()
            wake.assert_called_once_with(self.project)
        for field, value in (('wake', 1), ('route', 'operator'), ('writer', 'operator.cli')):
            row = {**self.config.record_kinds['role.question'], field: value}
            with self.subTest(field=field), self.assertRaises(ConfigError):
                host_records.configuration({'role.question': row})
        self.assertEqual(host_records.configuration(), {})

    def test_cli_mixed_incomplete_subjects_and_insight_answer_coexistence(self):
        subject = self.subject(self.question())
        args = ['record', 'sample', 'publish', '--kind', 'answer', '--id', 'a', '--body', 'A']
        for tail in (['--subject-kind', subject['kind']],
                     ['--subject-digest', subject['digest']],
                     ['--subject-id', subject['id'], '--subject-kind', subject['kind'],
                      '--subject-digest', subject['digest'], '--subject-rev', '1']):
            with self.subTest(tail=tail), self.assertRaises(ConfigError):
                self.cli(*args, *tail)
        self.answer(subject)
        question = self.project.insights.submit(source='worker', title='Legacy question',
                                               body='Q: Which?', base_snapshot=None, kind='question', addressee='operator')
        self.assertFalse(records.answer_lookup(self.project)(question))
        insight_subject = dict(type='insight', id=question['id'], rev=1)
        records.write(self.project, event_id='legacy', kind='answer', body='Legacy answer', subject=insight_subject)
        self.assertTrue(records.answer_lookup(self.project)(question))
        events = records.pending(self.project, self.config.roles['worker'])[0]['events']
        self.assertEqual({row['subject']['type'] for row in events}, {'record', 'insight'})
