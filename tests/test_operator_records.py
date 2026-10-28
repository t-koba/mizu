"""Trusted host CLI and synthetic-driver delivery; no provider/network calls."""
import copy
import json
import contextlib
import io
from dataclasses import replace
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import operator_records as records, decision_delivery
from mizu.cli import parser, execute
from mizu.config import load, CAPABILITIES
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, read_json
from mizu.protocol import DEFINITIONS as TOOLS
from mizu.runtime import Engine, should_run
from mizu.project import Project
from mizu.storage import backup, restore
from mizu.unit_prompts import prompt_delta_for
from mizu import decision_reconciliation
from mizu.shared_records import SharedRecords
from mizu.storage import MEMBERS
from mizu.services import event_watches, render
from mizu.scheduling import DaemonFacts, DispatchFacts, evaluate


class OperatorRecordTests(Fixture):
    def setUp(self):
        super().setUp()
        self.question = self.project.insights.submit(source='reviewer', title='Question',
            body='Q: Which behavior?', base_snapshot=None, origin='not-the-asking-role')
        self.subject = {'type': 'insight', 'id': self.question['id'], 'rev': 1}

    def cli(self, *args):
        return execute(parser().parse_args(['--config', str(self.file), 'record', 'sample', *args]))

    def answer(self, event='answer1', **kwargs):
        return records.write(self.project, event_id=event, kind='answer', body='Exact answer',
                             subject=self.subject, **kwargs)

    def notice(self, event='notice1', **kwargs):
        return records.write(self.project, event_id=event, kind='operator.notice',
                             body='Exact notice', addressee='reviewer', **kwargs)

    def pending(self, role='reviewer'):
        return records.pending(self.project, self.config.roles[role])[0]

    def target(self, result):
        return {'id': result['record']['id'], 'digest': result['record_digest']}

    def test_cli_identity_and_no_model_writer(self):
        result = self.cli('publish', '--kind', 'answer', '--id', 'answer1', '--body', 'Exact answer',
                          '--subject-id', self.question['id'], '--subject-rev', '1')
        self.assertEqual(result, self.answer())
        self.assertEqual(result['record']['writer'], 'operator.cli')
        self.assertEqual(result['record']['payload']['addressee'], 'reviewer')
        self.assertNotIn('record_write', CAPABILITIES | TOOLS.keys())
        self.assertNotIn('record', TOOLS)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['record', 'sample', 'publish', '--kind', 'answer',
                                 '--id', 'forged', '--writer', 'role.worker'])
        with self.assertRaises(Denied):
            self.context('reviewer').handle('record_write', {})
        with self.assertRaises(TypeError):
            records.write(self.project, event_id='fake', kind='answer', writer='reviewer')
        forged = copy.deepcopy(result['record'])
        forged['writer'] = 'role.reviewer'
        with self.assertRaises(Denied):
            records.store(self.project).put(forged, writer='role.reviewer')
        with self.assertRaises(Denied):
            records.store(self.project).put(result['record'], writer='role.reviewer')
        # An origin label and body text never determine the kind or route.
        self.assertEqual(self.pending('worker')['events'], [])
        with self.assertRaises(Denied):
            self.answer('wrong', addressee='worker')
        self.assertFalse(decision_delivery.authority(self.project.root))
        self.assertIsNone(self.project.insights.read(self.question['id']).get('decision'))

    def test_exact_subject_and_historical_revision(self):
        for subject in ({}, {'type': 'insight', 'id': self.question['id'][:8], 'rev': 1},
                        {**self.subject, 'rev': True}, {**self.subject, 'rev': 0},
                        {**self.subject, 'type': 'question'}, {**self.subject, 'extra': 1}):
            with self.subTest(subject=subject), self.assertRaises(Denied):
                records.write(self.project, event_id='bad', kind='answer', body='No', subject=subject)
        with self.assertRaises((Denied, OSError)):
            records.write(self.project, event_id='bad', kind='answer', body='No',
                          subject={**self.subject, 'rev': 2})
        original = self.answer()
        self.project.insights.revise(self.question['id'], source='reviewer', expected_rev=1,
                                     title='Question revised', body='Q: new', base_snapshot=None)
        self.assertEqual(self.answer(), original)  # exact revision remains answerable
        self.assertEqual(self.pending()['events'][0]['subject'], self.subject)
        archive = self.project.insights.root / 'insight-revisions' / f"{self.question['id']}.r1.json"
        archived = json.loads(archive.read_text())
        archived['source'] = 'worker'
        archive.write_text(json.dumps(archived))
        with self.assertRaises(Denied):
            records.write(self.project, event_id='tampered-history', kind='answer', body='No',
                          subject=self.subject)
        archive.write_text(canonical({**archived, 'source': 'reviewer'}).decode())
        for source in ('operator', 'editor', 'vcs'):
            question = self.project.insights.submit(source=source, title='Q', body='Q', base_snapshot=None)
            with self.assertRaises(Denied):
                records.write(self.project, event_id='bad', kind='answer', body='No',
                              subject={'type': 'insight', 'id': question['id'], 'rev': 1})
        with self.assertRaises(ConfigError):
            self.cli('publish', '--kind', 'answer', '--id', 'bad', '--subject-id', self.question['id'])

    def test_notice_validation_and_no_free_text_inference(self):
        result = self.cli('publish', '--kind', 'operator.notice', '--id', 'n1', '--role', 'worker',
                          '--body', 'Q: This is still a notice')
        self.assertEqual(result['record']['kind'], 'operator.notice')
        self.assertEqual(len(self.pending('worker')['events']), 1)
        self.assertEqual(self.pending()['events'], [])
        for role in ('unknown', 'operator', '../reviewer', ''):
            with self.subTest(role=role), self.assertRaises(Denied):
                records.write(self.project, event_id='bad', kind='operator.notice', body='No', addressee=role)
        self.notice(subject=self.subject)
        # The notice subject is validated on write, but an unavailable optional
        # reference cannot redirect or suppress an independently addressed notice.
        notice_subject = records.write(self.project, event_id='subject-notice',
            kind='operator.notice', body='Reference notice', addressee='reviewer', subject=self.subject)
        with patch.object(records, '_question', side_effect=Denied('source no longer available')):
            self.assertEqual(self.pending()['events'][-1]['record_id'], notice_subject['record']['id'])
        bad = copy.deepcopy(result['record'])
        shared = records.store(self.project)
        mutations = [lambda r: r.update(schema=True), lambda r: r.update(extra='x'),
                     lambda r: r['payload'].update(writer='operator.cli'),
                     lambda r: r['payload'].update(body=''),
                     lambda r: r['payload'].update(action='decide'),
                     lambda r: r['payload'].update(target={}),
                     lambda r: r['payload'].update(body='x' * 32769)]
        for mutate in mutations:
            row = copy.deepcopy(bad)
            mutate(row)
            with self.subTest(row=str(row)[:80]), self.assertRaises(Denied):
                shared.put(row, writer=records.WRITER)

    def test_immutable_replay_and_interrupted_response(self):
        original = SharedRecords.put
        def lost_response(shared, *args, **kwargs):
            original(shared, *args, **kwargs)
            raise OSError('lost CLI response')
        with patch.object(SharedRecords, 'put', lost_response), self.assertRaises(OSError):
            self.answer()
        shared = records.store(self.project)
        before = {str(p.relative_to(self.project.root)): p.read_bytes() for p in (self.project.root / 'shared-records').rglob('*.json')}
        result = self.answer()
        self.assertEqual(self.answer(), result)
        with self.assertRaises(Denied):
            records.write(self.project, event_id='answer1', kind='answer', body='Conflicting', subject=self.subject)
        self.assertEqual(before, {str(p.relative_to(self.project.root)): p.read_bytes() for p in (self.project.root / 'shared-records').rglob('*.json')})
        path = self.project.root / shared._record_name(result['record']['id'])
        path.write_bytes(b'{}\n')
        with self.assertRaises(Denied):
            self.answer()
        with self.assertRaises(Denied):
            self.pending()

    def test_notice_withdraw_supersede_and_frozen_manifest(self):
        first = self.notice()
        context = self.context('reviewer')
        records.bind(context)
        token = context.operator_record_manifest[0].ack_token
        target = self.target(first)
        second = self.notice('notice2', action='supersede', target=target)
        self.assertEqual(self.pending()['events'][0]['record_id'], second['record']['id'])
        self.assertEqual(self.notice('notice2', action='supersede', target=target), second)
        with self.assertRaises(Denied):
            self.notice('notice3', action='supersede', target=target)
        withdrawal = records.write(self.project, event_id='close', kind='operator.notice',
            addressee='reviewer', action='withdraw', target=self.target(second))
        self.assertEqual(self.pending()['events'], [])
        self.assertEqual(records.write(self.project, event_id='close', kind='operator.notice',
            addressee='reviewer', action='withdraw', target=self.target(second)), withdrawal)
        # An already delivered immutable record is still readable/ackable after closure.
        self.assertIn('Exact notice', records.read(context, {'ack_token': token})['text'])
        context._op_finish({'outcome': 'wait', 'summary': 'Handled', 'ack_events': [token]})
        records.acknowledge(context, [token])
        records.acknowledge(context, [token])
        entry = context.operator_record_manifest[0]
        self.assertTrue(records.store(self.project).received('reviewer', entry.record_id, entry.record_digest))
        self.assertEqual(records.store(self.project).get(first['record']['id'])[0], first['record'])
        self.assertIn('shared-records', MEMBERS)
        self.assertIn('shared-record-receipts', MEMBERS)
        with self.assertRaises(Denied):
            records.write(self.project, event_id='no-answer-close', kind='answer', action='withdraw',
                          subject=self.subject, target=target)
        for invalid in ({**target, 'digest': '0' * 64}, self.target(self.answer())):
            with self.assertRaises(Denied):
                records.write(self.project, event_id='badclose', kind='operator.notice', action='withdraw',
                              addressee='reviewer', target=invalid)
        with self.assertRaises(Denied):
            records.write(self.project, event_id='other-role', kind='operator.notice', action='withdraw',
                          addressee='worker', target=target)

    def test_config_defaults_and_disable_retains_frozen_authority(self):
        expected = records.configuration(self.config.operator_records)
        self.assertEqual(self.config.operator_records, expected)
        for raw in ({'unknown': {}}, {'answer': {'route': 'addressee'}},
                    {'answer': {'retention': '30days'}}, {'answer': {'enabled': 1}},
                    {'answer': {'writer': 'role.worker'}}, {'answer': {'wake': 1}}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                records.configuration(raw)
        answer = self.answer()
        context = self.context('reviewer')
        records.bind(context)
        self.file.write_text(self.file.read_text().replace('[operator_records.answer]\nenabled = true',
            '[operator_records.answer]\nenabled = false'))
        config = load(self.file)
        self.project.config = config
        self.assertEqual(self.pending()['events'], [])
        with self.assertRaises(Denied):
            self.answer('new')
        token = context.operator_record_manifest[0].ack_token
        records.acknowledge(context, [token])
        self.assertEqual(records.store(self.project).get(answer['record']['id'])[0], answer['record'])
        self.project.config = self.config
        self.assertEqual(self.pending()['events'], [])

    def test_runtime_prompt_retrieve_finish_explicit_partial_ack(self):
        self.answer()
        self.notice()
        seen = []
        def driver(context, prompt, *_):
            document = json.loads(prompt)
            page = document['operator_records']
            self.assertEqual(json.loads(prompt_delta_for(context, []))['operator_records'], page)
            self.assertEqual(page['pending_count'], 2)
            self.assertIn('mizu_record_read', document['unit_tool_surface']['tools'])
            self.assertNotIn('record_write', context.role.capabilities)
            for row in page['events']:
                response = context.handle('record_read', {'ack_token': row['ack_token'], 'limit': 32})
                self.assertIsNotNone(response['next_offset'])
                self.assertEqual(response['record_digest'], row['record_digest'])
            tokens = [row['ack_token'] for row in page['events']]
            seen.extend(tokens)
            # Omission is not acknowledgement; finish selects only one exact binding.
            context.handle('finish', {'outcome': 'wait', 'summary': 'One handled', 'ack_events': tokens[:1]})
        result = Engine(self.config, driver=ScriptDriver(driver)).run(self.project, 'reviewer')
        self.assertEqual(result['status'], 'completed')
        pending = self.pending()
        self.assertEqual([row['ack_token'] for row in pending['events']], seen[1:])
        self.assertTrue((self.project.root / 'runs' / result['run'] / 'operator-record-delivery.json').is_file())
        before = pending
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'reviewer')
        self.assertEqual(self.pending(), before)

    def test_backup_restore_and_decision_cutover_coexist(self):
        self.answer()
        notice = self.notice()
        context = self.context('reviewer')
        records.bind(context)
        answer_entry = next(entry for entry in context.operator_record_manifest
                            if entry.record_id != notice['record']['id'])
        records.acknowledge(context, [answer_entry.ack_token])
        self.config = replace(self.config, roles={name: replace(role, decision_page_bytes=8192, decision_events=('reject',))
                              for name, role in self.config.roles.items()})
        self.project.config = self.config
        # An operator receipt does not become an unexplained decision receipt.
        decision_reconciliation.activate_decision_delivery(self.project)
        item = self.project.insights.submit(source='reviewer', title='Finding', body='Proposal', base_snapshot=None)
        self.project.insights.decide(item['id'], 'reject', 'Reason', '', 'test', expected_rev=1)
        state = decision_delivery.pending(self.project, self.config.roles['reviewer'], actions=('reject',))
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(self.pending()['events'][0]['record_id'], notice['record']['id'])
        self.project.set_control(paused=True)
        archive = self.root / 'operator-records.tar.gz'
        backup(self.project, archive)
        restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        shared = records.store(restored)
        self.assertTrue(shared.received('reviewer', answer_entry.record_id, answer_entry.record_digest))
        self.assertEqual(records.pending(restored, self.config.roles['reviewer'])[0], self.pending())
        self.assertEqual(shared.get(notice['record']['id'])[0], notice['record'])

    def test_duplicate_notice_closures_fail_closed(self):
        target = self.notice('closure-target')
        shared = records.store(self.project)
        for event_id in ('closure-one', 'closure-two'):
            closure = {'schema': 1, 'id': f"operator/{self.project.name}/{event_id}",
                'kind': 'operator.notice', 'created_at': target['record']['created_at'],
                'writer': records.WRITER, 'subject': {},
                'payload': {'action': 'withdraw', 'body': '', 'addressee': 'reviewer',
                    'target': {'id': target['record']['id'], 'digest': target['record_digest']}}}
            shared.put(closure, writer=records.WRITER)
        with self.assertRaises(Denied):
            records.pending(self.project, self.config.roles['reviewer'])

    def test_manifest_isolation_tamper_and_holes(self):
        for i in range(22):
            self.answer(f'a{i:02}')
        page, manifest = records.pending(self.project, self.config.roles['reviewer'])
        self.assertEqual(len(page['events']), 20)
        self.assertEqual(page['pending_count'], 22)
        self.assertTrue(page['truncated'])
        self.assertLess(len(canonical(page)), 20000)
        context = self.context('reviewer')
        records.bind(context)
        other = self.context('worker')
        records.bind(other)
        token = manifest[1].ack_token
        with self.assertRaises(Denied):
            records.read(other, {'ack_token': token})
        with self.assertRaises(Denied):
            other._op_finish({'outcome': 'wait', 'summary': 'No', 'ack_events': [token]})
        self.assertFalse((other.run_dir / 'finish-intent.json').exists())
        with self.assertRaises(Denied):
            context._op_finish({'outcome': 'wait', 'summary': 'No', 'ack_events': [token, token]})
        records.acknowledge(context, [token])
        after = self.pending()
        self.assertEqual(after['pending_count'], 21)
        self.assertEqual(after['events'][0]['ack_token'], manifest[0].ack_token)
        self.assertEqual(after['events'][1]['ack_token'], manifest[2].ack_token)
        # Corrupt receipts never count as handled or get repaired.
        shared = records.store(self.project)
        receipt = self.project.root / shared._receipt_name('reviewer', manifest[1].record_id)
        receipt.write_bytes(b'{}\n')
        with self.assertRaises(Denied):
            self.pending()

    def test_record_admits_exact_recipient_and_ack_stops_wake(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, 'reviewer')
        engine.run(self.project, 'searcher')
        self.assertEqual(engine.run(self.project, 'reviewer')['skipped'], 'unchanged')
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True)['skipped'], 'no-event')
        answer = self.answer()
        self.assertEqual(engine.run(self.project, 'searcher', event_trigger=True)['skipped'], 'no-event')
        result = engine.run(self.project, 'reviewer', event_trigger=True)
        self.assertEqual(result['status'], 'completed')
        started = read_json(self.project.root / 'runs' / result['run'] / 'started.json')
        self.assertEqual(started['admission'], 'operator_record')
        # Unacknowledged input survives the run, rather than becoming an observed cursor.
        self.assertEqual(self.pending()['events'][0]['record_id'], answer['record']['id'])
        def handle(context, *_):
            page = context.operator_record_page
            context.handle('finish', {'outcome': 'wait', 'summary': 'Read and handled',
                                     'ack_events': [page['events'][0]['ack_token']]})
        result = Engine(self.config, driver=ScriptDriver(handle)).run(self.project, 'reviewer')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True)['skipped'], 'no-event')
        notice = self.notice()
        self.assertTrue(records.due(self.project, 'reviewer'))
        records.write(self.project, event_id='withdraw-wake', kind='operator.notice',
                      addressee='reviewer', action='withdraw', target=self.target(notice))
        self.assertFalse(records.due(self.project, 'reviewer'))
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True)['skipped'], 'no-event')

    def test_record_daemon_readiness_control_done_and_corruption(self):
        snap = {**self.project.snapshots.get(), 'outcome': 'done', 'wake_generation': 'test-wake'}
        self.assertFalse(should_run(self.project, snap, role_name='worker'))
        # An answer is not a question migration or an inbox-generation update.
        generation = self.project.insights.generation()
        question = self.project.insights.read(self.question['id'])
        result = records.write(self.project, event_id='daemon-notice', kind='operator.notice',
                               body='Operator input', addressee='worker')
        self.assertEqual(self.project.insights.generation(), generation)
        self.assertEqual(self.project.insights.read(self.question['id']), question)
        self.assertTrue(should_run(self.project, snap, role_name='worker'))
        self.assertFalse(records.due(self.project, 'reviewer'))
        for change in ({'armed': False}, {'paused': True}, {'draining': True}):
            self.project.set_control(**change)
            self.assertFalse(should_run(self.project, snap, role_name='worker'))
            self.project.set_control(armed=True, paused=False, draining=False)
        with self.assertRaises(Denied):
            records.due(self.project, 'missing-role')
        context = self.context('worker')
        records.bind(context)
        row = result['record']
        path = self.project.root / records.store(self.project)._record_name(row['id'])
        row['payload']['body'] = 'Changed immutable content'
        path.write_bytes(canonical(row))
        token = context.operator_record_manifest[0].ack_token
        with self.assertRaises(Denied):
            context.handle('record_read', {'ack_token': token})
        with self.assertRaises(Denied):
            context._op_finish({'outcome': 'wait', 'summary': 'No', 'ack_events': [token]})
        self.assertFalse((context.run_dir / 'finish-intent.json').exists())
        path.write_bytes(b'{}\n')
        with self.assertRaises(Denied):
            should_run(self.project, snap, role_name='worker')
        self.assertEqual(evaluate(DispatchFacts(event_trigger=True, has_record=True)).admission,
                         'operator_record')
        facts = DaemonFacts(armed=True, goal_changed=False, operator_woke=False,
                           ready_work_changed=False, ci_wait_due=False,
                           operator_records_due=True, outcome='done')
        self.assertEqual(evaluate(facts).reason, 'operator_record')

    def test_record_service_hints_keep_explicit_roles_and_disable_policy(self):
        watches = event_watches(self.config, self.project)
        shared = str(self.project.root / 'shared-records' / 'operator')
        self.assertIn(shared, watches['reviewer'])
        self.assertIn(shared, watches['searcher'])  # scheduled observer without subscriptions
        self.assertNotIn('consult', watches)  # explicit-only remains explicit-only
        self.assertNotIn('worker', watches)  # daemon polls authoritative state directly
        executable = self.root / 'mizu-entry'
        executable.write_text('')
        for system, marker in (('linux', 'PathChanged=' + shared),
                               ('macos', shared), ('windows', '<CalendarTrigger>')):
            output = render(replace(self.config, timezone='local'), self.project, executable, system=system)
            self.assertIn(marker, '\n'.join(output.values()))
        absent = records.configuration({})
        self.assertFalse(any(row['enabled'] for row in absent.values()))
        closed_config = replace(self.config, operator_records={})
        self.project.config = closed_config
        with self.assertRaises(Denied):
            self.notice('undeclared')
        self.project.config = self.config
        disabled = {kind: {**row, 'enabled': False}
                    for kind, row in records.configuration(self.config.operator_records).items()}
        config = replace(self.config, operator_records=disabled)
        self.assertFalse(any(shared in paths for paths in event_watches(config, self.project).values()))
        original = self.file.read_text()
        for replacement in ('retention = "ttl"', 'retention = 30', 'route = "any"',
                            'writer = "model"'):
            self.file.write_text(original.replace('retention = "permanent"', replacement, 1))
            with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                load(self.file)

    def test_operator_partition_isolation_and_watch(self):
        answer = self.answer()
        notice = self.notice()
        shared = records.store(self.project)
        for result in (answer, notice):
            canonical = shared._record_name(result['record']['id'])
            self.assertTrue(canonical.startswith('shared-records/operator/'))
            self.assertTrue((self.project.root / canonical).is_file())
            self.assertFalse((self.project.root / shared._legacy_record_name(result['record']['id'])).exists())
        self.assertEqual({row[0]['id'] for row in shared.records(namespace='operator')},
                         {answer['record']['id'], notice['record']['id']})
        # A corrupt file outside the operator partition must not break pending.
        foreign = self.project.root / 'shared-records' / 'insight' / 'corrupt.json'
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(b'{broken')
        try:
            self.assertEqual(self.pending()['pending_count'], 2)
        finally:
            foreign.unlink()
        # Enumeration validates once without a second read.
        from mizu.shared_records import SharedRecords as _Shared
        from unittest.mock import patch as _patch
        with _patch.object(_Shared, 'get', side_effect=AssertionError('second read')):
            self.assertEqual(len(list(shared.records(namespace='operator'))), 2)
        watches = event_watches(self.config, self.project)
        operator_watch = str(self.project.root / 'shared-records' / 'operator')
        parent_watch = str(self.project.root / 'shared-records')
        self.assertIn(operator_watch, watches['reviewer'])
        self.assertNotIn(parent_watch, watches['reviewer'])

    def test_wake_policy_is_independent_of_delivery(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, 'reviewer')
        self.answer()
        policy = records.configuration(self.config.operator_records)
        policy['answer']['wake'] = False
        self.config = replace(self.config, operator_records=policy)
        self.project.config = self.config
        page = self.pending()
        self.assertEqual(page['pending_count'], 1)
        self.assertFalse(page['wake_pending'])
        self.assertFalse(records.due(self.project, 'reviewer'))
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, 'reviewer', event_trigger=True)
        self.assertEqual(result['skipped'], 'no-event')
        context = self.context('reviewer')
        records.bind(context)
        token = context.operator_record_manifest[0].ack_token
        records.acknowledge(context, [token])
        self.assertEqual(self.pending()['pending_count'], 0)
