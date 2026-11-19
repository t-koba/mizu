"""Offline writer wait fallback: bounded local evidence, no provider calls."""
import dataclasses
import datetime as dt
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver, forget_ledger, stored_question_insight
from test_ci_waits import CiWaitFixture, SHA
from mizu import operator_records as records
from mizu.ci_requests import CiRequests, request_id
from mizu.config import wait_writer_roles
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import now, read_json
from support import history_write_json as write_json
from mizu.runtime import (Engine, _progress_self_admits, _writer_wait_liveness,
                           _writer_wait_sources, prompt_for, should_run)
from mizu.runtime import _deferred_until_due
from mizu.work_item_waits import holder_only


class WriterWaitTests(Fixture):
    def role(self):
        return self.config.roles['worker']

    def block(self, key, wait):
        store = self.project.work_items
        item = store.create(actor='worker', idempotency_key=key, title=key)['record']['work_item']
        return store.transition(item['id'], expected_rev=1, actor='worker',
                                status='blocked', wait=wait)

    def delegated_writers(self):
        for name in ('searcher', 'reviewer', 'consult'):
            self.config.roles[name] = dataclasses.replace(
                self.config.roles[name], workspace='write', event_wakes=(),
                daemon=False, interval_seconds=0, calendar=())

    def test_delegated_code_wait_is_holder_ready_without_satisfying_wait(self):
        self.delegated_writers()
        self.config.roles['worker'] = dataclasses.replace(self.role(), on_change=True)
        self.assertEqual(wait_writer_roles(self.config, self.project.roles), frozenset({'worker'}))
        # A configured scheduled writer not assigned to this project is not its authority.
        self.assertEqual(self.config.roles['maintainer'].workspace, 'write')
        self.assertNotIn('maintainer', self.project.roles)
        item = self.block('self code', {'kind': 'code_change'})
        store = self.project.work_items
        self.assertEqual(item['wait']['digest'], self.project.snapshots.get()['code_digest'])
        self.assertEqual(_writer_wait_sources(self.project, self.role()), False)
        page = store.page_ready(role_name='worker')
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['id'], item['id'])
        self.assert_holder_only_open(item)
        projected = json.loads(prompt_for(self.context()))['ready_work']['items'][0]
        self.assertEqual(projected['status'], 'open')
        self.assertNotIn('wait_satisfied', projected)
        # Ordinary held-open visibility: the holder's page and the unscoped
        # read see it; other named roles do not.
        self.assertEqual(store.page_ready()['total'], 1)
        self.assertEqual(store.page_ready(role_name='reviewer')['total'], 0)
        # Bare stores retain their old behavior unless authority is supplied.
        from mizu.work_items import WorkItems
        bare = WorkItems(store.root, self.project.snapshots, self.project.insights)
        self.assertEqual(bare.page_ready(role_name='worker')['total'], 0)
        self.assertEqual(bare.page_ready(role_name='worker', writer_roles={'worker'})['total'], 1)

        for outcome in ('wait', 'blocked'):
            with self.subTest(outcome=outcome):
                def finish(ctx, *_):
                    result = ctx.handle('finish', {'outcome': outcome, 'summary': 'Replan code work',
                                                   'state': 'Now: prerequisite work remains'})
                    self.assertTrue(result['sealed'])
                result = Engine(self.config, driver=ScriptDriver(finish)).run(self.project, 'worker')
                self.assertEqual(result['status'], 'completed')
                if outcome == 'wait':
                    self.assertIs(result['finish']['wait_liveness'], False)
                snapshot = self.project.snapshots.get()
                self.assertIs(snapshot['progress']['ready_work'], True)
                self.assertIsNone(snapshot['progress']['ready_work_signature'])
                self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
                self.assert_holder_only_open(item)

        # Editing alone does not satisfy the wait; only a published digest does.
        (self.project.workspace / 'changed.txt').write_text('changed\n')
        self.assert_holder_only_open(item)
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state='Changed code', run=None, outcome='wait', summary='Publish prerequisite work',
            parent=snapshot['id'])
        self.project.snapshots.publish(snapshot)
        self.assertEqual(self.blocked_row(item)['state'], 'due')
        self.assertEqual(store.page_ready(role_name='worker')['items'][0]['wait_liveness']['state'], 'due')
        self.assertEqual(store.get(item['id']), item)

    def test_independent_writer_prevents_holder_actionability(self):
        self.delegated_writers()
        item = self.block('external code', {'kind': 'code_change'})
        child = self.config.roles['reviewer']
        for route in ({'event_wakes': self.role().event_wakes}, {'daemon': True},
                      {'interval_seconds': 60}, {'calendar': ('09:00',)}):
            with self.subTest(route=route):
                self.config.roles['reviewer'] = dataclasses.replace(child, **route)
                self.assertEqual(wait_writer_roles(self.config, self.project.roles), frozenset({'worker', 'reviewer'}))
                self.assertIsNone(self.blocked_row(item)['self_resolvable'])
                self.assertEqual(self.blocked_row(item)['state'], 'pending')
                self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
                self.assertEqual(self.project.work_items.page_ready(role_name='worker')['total'], 0)
                self.assertEqual(self.project.work_items.get(item['id']), item)

    def blocked_row(self, item):
        return next(row for row in self.project.status()['blocked_work']['items']
                    if row['id'] == item['id'])

    def assert_holder_only_open(self, item):
        """Holder-only waits read as ordinary held-open work, never as blocked."""
        store = self.project.work_items
        self.assertEqual(store.get(item['id'])['status'], 'blocked')
        ready = store.page_ready(role_name='worker')
        row = next(row for row in ready['items'] if row['id'] == item['id'])
        self.assertEqual(row['status'], 'open')
        for absent in ('wait', 'wait_liveness', 'blocked_since', 'blocked_age_seconds'):
            self.assertNotIn(absent, row)
        self.assertNotIn(item['id'], [row['id'] for row in
                                      self.project.status()['blocked_work']['items']])
        self.assertEqual(store.get(item['id']), item)
        return row

    def test_legacy_question_has_no_special_writer_liveness_or_wait_resolution(self):
        question = stored_question_insight(self.project)
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        item = self.block('legacy insight wait', {'kind': 'insight_decided', 'insight': question['id']})
        records.write(self.project, event_id='legacy-answer', kind='answer', body='Operator answer',
                      subject={'type': 'insight', 'id': question['id'], 'rev': 1})
        store = self.project.work_items
        with patch.object(records, 'catalog', side_effect=AssertionError('unexpected answer scan')):
            self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
            row = self.blocked_row(item)
            self.assertEqual((row['state'], row['self_resolvable']), ('pending', None))
            self.assertEqual(store.page_ready(role_name='worker')['total'], 0)
        self.assertIsNone(read_json(self.project.root / 'decisions' / f"{question['id']}.json"))
        self.project.insights.decide(question['id'], 'reject', 'Substantive decision', '', 'fixture', expected_rev=1)
        row = self.blocked_row(item)
        self.assertEqual((row['state'], row['self_resolvable']), ('due', None))
        self.assertIs(_writer_wait_liveness(self.project, self.role()), False)
        ready = store.page_ready(role_name='worker')['items']
        self.assertEqual([(row['id'], row['status']) for row in ready], [(item['id'], 'blocked')])
        self.assertEqual(store.get(item['id']), item)

    def test_malformed_insight_metadata_cannot_prove_wait_resolution(self):
        question = stored_question_insight(self.project)
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

    def test_external_waits_remain_gated_while_holder_only_code_is_ready(self):
        code = self.block('self code', {'kind': 'code_change'})
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        self.assert_holder_only_open(code)
        other = self.project.insights.submit(
            source='reviewer', title='External proposal', body='Fixture proposal', base_snapshot=None)
        for key, wait in (
                ('dependency', {'kind': 'dependency', 'recipient': 'reviewer',
                                'requires': {'insight': other['id'], 'rev': 1, 'action': 'accept'}}),
                ('other insight', {'kind': 'insight_decided', 'insight': other['id']})):
            with self.subTest(key=key):
                item = self.block(key, wait)
                self.assertIs(_writer_wait_liveness(self.project, self.role()), True)
                ready_ids = [row['id'] for row in
                             self.project.work_items.page_ready(role_name='worker')['items']]
                self.assertIn(code['id'], ready_ids)
                self.assertNotIn(item['id'], ready_ids)
                self.assertEqual(self.blocked_row(item)['state'], 'pending')
                self.project.work_items.transition(item['id'], expected_rev=item['rev'],
                                                   actor='worker', status='dropped', reason='Fixture settled')

    def test_self_wait_inventory_must_be_complete_and_safe(self):
        item = self.block('self code', {'kind': 'code_change'})
        Engine(self.config, driver=ScriptDriver()).run(self.project, 'worker')
        status = self.project.work_items.open_state_readonly(
            limit=self.config.limits.work_items_prompt, writer_roles={'worker'})
        for field, value in (('coverage_complete', False), ('truncated', True)):
            blocked = {**status['blocked_work'], field: value}
            with self.subTest(field=field), patch.object(
                    self.project.work_items, 'open_state_readonly',
                    return_value={**status, 'blocked_work': blocked}):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
        (self.project.root / 'work-items' / item['id'] / 'broken.json').write_text('{')
        forget_ledger(self.project.root)
        self.assertIsNone(_writer_wait_liveness(self.project, self.role()))

    def test_status_reports_holder_only_as_open_without_transition_or_extra_scan(self):
        code = self.block('self code', {'kind': 'code_change'})
        question = stored_question_insight(self.project)
        answer = self.block('answer', {'kind': 'insight_decided', 'insight': question['id']})
        self.project.insights.decide(question['id'], 'reject', 'Substantive answer', '', 'fixture',
                                     expected_rev=1)
        store = self.project.work_items
        with patch.object(store, '_page_open_impl', wraps=store._page_open_impl) as scan:
            blocked = self.project.status()['blocked_work']
            self.assertEqual(scan.call_count, 1)
        self.assertTrue(blocked['coverage_complete'])
        self.assertFalse(blocked['truncated'])
        # The pending holder-only code wait is operationally open, so only the
        # decided (due) answer remains a blocked row; no stored record was
        # rewritten in the process.
        self.assertEqual(len(blocked['items']), 1)
        due = blocked['items'][0]
        self.assertEqual(due['id'], answer['id'])
        self.assertEqual((due['state'], due['self_resolvable']), ('due', None))
        self.assertEqual(blocked['counts'], {'absent': 0, 'pending': 0, 'due': 1,
                                             'impossible': 0, 'receipt_bound': 0,
                                             'terminal': 0, 'unknown': 0})
        self.assert_holder_only_open(code)
        self.assertEqual(store.get(answer['id']), answer)
        bounded = store.open_state_readonly(limit=1, writer_roles={'worker'})['blocked_work']
        self.assertEqual(len(bounded['items']), 1)
        self.assertFalse(bounded['truncated'])
        self.assertTrue(bounded['coverage_complete'])
        self.config.roles['reviewer'] = dataclasses.replace(
            self.config.roles['reviewer'], workspace='write')
        rows = {row['id']: row for row in self.project.status()['blocked_work']['items']}
        self.assertIsNone(rows[code['id']]['self_resolvable'])
        self.assertIsNone(rows[answer['id']]['self_resolvable'])
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

    def test_disabled_override_malformed_progress_and_partial_inventory_fail_closed(self):
        engine = Engine(self.config, driver=ScriptDriver())
        engine.run(self.project, 'worker')
        snapshot = self.project.snapshots.get()
        stamp = dt.datetime.fromisoformat(snapshot['progress']['recorded_at']).timestamp()
        due = stamp + 600
        # A wait outcome never self-admits, with any backoff override or role shape.
        for role in (dataclasses.replace(self.role(), no_progress_backoff_seconds=0),
                     dataclasses.replace(self.role(), workspace='read')):
            self.assertFalse(_progress_self_admits(snapshot, self.config, 'worker', due))
        self.assertFalse(_progress_self_admits(
            {**snapshot, 'progress': {}}, self.config, 'worker', due))
        self.assertFalse(_progress_self_admits(
            {**snapshot, 'outcome': 'continue', 'progress': {}}, self.config, 'worker', due))
        for key in ('truncated', 'coverage_complete'):
            status = self.project.work_items.open_state_readonly(
                limit=self.config.limits.work_items_prompt, writer_roles={'worker'})
            status['blocked_work'][key] = key == 'truncated'
            with patch.object(self.project.work_items, 'open_state_readonly', return_value=status):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))
        for error in (OSError, Busy, LimitExceeded):
            with patch.object(self.project.work_items, 'open_state_readonly', side_effect=error('fixture')):
                self.assertIsNone(_writer_wait_liveness(self.project, self.role()))


class HolderOnlyTests(Fixture):
    """Positive holder-only witness shared by write path and readiness."""

    def block(self, key, wait):
        store = self.project.work_items
        item = store.create(actor='worker', idempotency_key=key, title=key)['record']['work_item']
        return store.transition(item['id'], expected_rev=1, actor='worker',
                                status='blocked', wait=wait)

    def test_holder_only_sole_writer_code_change(self):
        item = self.block('holder-only code', {'kind': 'code_change'})
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        fresh = store.get(item['id'])
        self.assertEqual(fresh['holder'], 'worker')
        self.assertEqual(holder_only(store, fresh, snapshot, writer_roles={'worker'}),
                         'sole_writer_code_change')
        self.assertEqual(holder_only(store, fresh, snapshot, writer_roles=frozenset({'worker'})),
                         'sole_writer_code_change')

    def test_holder_only_deadline_is_deferred_work_with_its_deadline(self):
        at = '2099-01-01T00:00:00+00:00'
        item = self.block('deferred deadline', {'kind': 'deadline', 'at': at})
        store = self.project.work_items
        fresh = store.get(item['id'])
        self.assertEqual(holder_only(store, fresh, self.project.snapshots.get(),
                                     writer_roles={'worker'}), 'sole_holder_deadline')
        # Deferred work is the holder's own schedule: visible as open work in
        # its ready page with the deadline, never as blocked backpressure.
        ready = store.page_ready(role_name='worker')
        row = next(row for row in ready['items'] if row['id'] == item['id'])
        self.assertEqual(row['status'], 'open')
        self.assertEqual(row['deferred_until'], at)
        self.assertEqual(ready['_earliest_deferred_until'], at)
        self.assertNotIn('_earliest_deferred_until', store.page_ready(role_name='reviewer'))
        for absent in ('wait', 'wait_liveness', 'blocked_since', 'blocked_age_seconds'):
            self.assertNotIn(absent, row)
        blocked_ids = [row['id'] for row in self.project.status()['blocked_work']['items']]
        self.assertNotIn(item['id'], blocked_ids)
        # The deadline reaches the model's opening, not just the read page.
        projected = json.loads(prompt_for(self.context()))['ready_work']['items']
        shown = next(row for row in projected if row['id'] == item['id'])
        self.assertEqual(shown['deferred_until'], at)
        self.assertEqual(store.get(item['id']), item)

    def test_sole_holder_deadline_is_not_external_wait_liveness(self):
        at = '2099-01-01T00:00:00+00:00'
        deadline = self.block('deferred deadline liveness', {'kind': 'deadline', 'at': at})
        role = self.config.roles['worker']
        # A sole-holder deadline is deferred self-resolvable work, so it is not
        # an external wait_liveness source even though time will pass its bound.
        self.assertIs(_writer_wait_liveness(self.project, role), False)
        # Its deferred form still reaches the holder's ready inventory.
        ready_ids = [row['id'] for row in
                     self.project.work_items.page_ready(role_name='worker')['items']]
        self.assertIn(deadline['id'], ready_ids)
        # A genuinely external wait for the same holder restores liveness.
        question = stored_question_insight(self.project)
        self.block('external insight liveness',
                              {'kind': 'insight_decided', 'insight': question['id']})
        self.assertIs(_writer_wait_liveness(self.project, role), True)

    def test_deadline_with_a_second_writer_stays_blocked(self):
        at = '2099-01-01T00:00:00+00:00'
        item = self.block('shared deadline', {'kind': 'deadline', 'at': at})
        store = self.project.work_items
        fresh = store.get(item['id'])
        self.assertIsNone(holder_only(store, fresh, self.project.snapshots.get(),
                                      writer_roles={'worker', 'reviewer'}))
        page = store._page_open_impl(cursor=None, limit=25, readonly=True,
                                     writer_roles={'worker', 'reviewer'})
        row = next(row for row in page['items'] if row['id'] == item['id'])
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['wait'], {'kind': 'deadline', 'at': at})
        self.assertNotIn('deferred_until', row)

    def test_holder_only_second_writer_denies(self):
        item = self.block('holder-only shared', {'kind': 'code_change'})
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        fresh = store.get(item['id'])
        self.assertIsNone(holder_only(store, fresh, snapshot,
                                       writer_roles={'worker', 'reviewer'}))
        self.assertIsNone(holder_only(store, fresh, snapshot,
                                       writer_roles=frozenset({'worker', 'reviewer'})))

    def test_holder_only_due_code_change_is_none(self):
        item = self.block('holder-only due', {'kind': 'code_change'})
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        fresh = store.get(item['id'])
        self.assertEqual(holder_only(store, fresh, snapshot, writer_roles={'worker'}),
                         'sole_writer_code_change')
        (self.project.workspace / 'changed.txt').write_text('changed\n')
        published = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state='Changed code', run=None, outcome='wait', summary='Publish prerequisite work',
            parent=snapshot['id'])
        self.project.snapshots.publish(published)
        current = self.project.snapshots.get()
        fresh = store.get(item['id'])
        from mizu.work_item_waits import liveness as wait_liveness
        self.assertEqual(wait_liveness(store, fresh['wait'], current, item=fresh,
                                       writer_roles={'worker'})['state'], 'due')
        self.assertIsNone(holder_only(store, fresh, current, writer_roles={'worker'}))

    def test_holder_only_ci_requires_more_than_no_terminal_evidence(self):
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        waits = [
            {'kind': 'ci', 'publication_id': 'a' * 40, 'checks': ['unit']},
            {'kind': 'publication_ci', 'snapshot_id': 'a' * 64, 'code_digest': 'b' * 64,
             'branch': 'main', 'checks': ['unit']},
        ]
        for wait in waits:
            with self.subTest(wait_kind=wait['kind']):
                item = {'status': 'blocked', 'holder': 'worker', 'wait': wait}
                self.assertIsNone(holder_only(store, item, snapshot, writer_roles={'worker'}))

    def test_holder_only_status_holder_wait_is_none(self):
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        item = self.block('holder-only shape', {'kind': 'code_change'})
        fresh = store.get(item['id'])
        valid_wait = fresh['wait']
        cases = {
            'open': {**fresh, 'status': 'open'},
            'doing': {**fresh, 'status': 'doing'},
            'no_holder': {**fresh, 'holder': None},
            'no_wait': {'status': 'blocked', 'holder': 'worker', 'wait': None},
            'missing_wait': {'status': 'blocked', 'holder': 'worker'},
            'not_dict': None,
        }
        for name, candidate in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(holder_only(store, candidate, snapshot,
                                               writer_roles={'worker'}))
        for name, roles in (('none', None), ('empty', set()), ('missing_holder', {'reviewer'}),
                            ('list', ['worker'])):
            with self.subTest(roles=name):
                self.assertIsNone(holder_only(store, fresh, snapshot, writer_roles=roles))
        self.assertIsNone(holder_only(store, {'status': 'blocked', 'holder': 'worker',
                                               'wait': valid_wait},
                                       snapshot, writer_roles={'worker', 'reviewer'}))

    def test_holder_only_blocked_reads_as_open_and_external_stays_gated(self):
        from mizu.work_items import WorkItems
        store = self.project.work_items
        held = self.block('holder-only code', {'kind': 'code_change'})
        question = stored_question_insight(self.project)
        external = self.block('external insight',
                              {'kind': 'insight_decided', 'insight': question['id']})
        # Operational effective status in every authority-bearing projection:
        # the holder's ready set carries the item as ordinary held-open work.
        ready = store.page_ready(role_name='worker')
        self.assertEqual([row['id'] for row in ready['items']], [held['id']])
        row = ready['items'][0]
        self.assertEqual(row['status'], 'open')
        for absent in ('wait', 'wait_liveness', 'blocked_since', 'blocked_age_seconds'):
            self.assertNotIn(absent, row)
        page = store._page_open_impl(cursor=None, limit=25, readonly=True,
                                      writer_roles={'worker'})
        grown = next(row for row in page['items'] if row['id'] == held['id'])
        self.assertEqual(grown['status'], 'open')
        for absent in ('wait', 'wait_liveness', 'blocked_since', 'blocked_age_seconds'):
            self.assertNotIn(absent, grown)
        # The open projection counts holder-only work as open, and the blocked
        # diagnostic excludes it while keeping the genuinely external wait.
        state = store.open_state_readonly(writer_roles={'worker'})
        self.assertNotIn(held['id'], [row['id'] for row in state['blocked_work']['items']])
        self.assertIn(external['id'], [row['id'] for row in state['blocked_work']['items']])
        blocked = self.project.status()['blocked_work']
        self.assertNotIn(held['id'], [row['id'] for row in blocked['items']])
        self.assertIn(external['id'], [row['id'] for row in blocked['items']])
        # Ordinary held-open visibility: the unscoped read sees the held item
        # but not the gated external wait; other named roles see neither.
        unscoped = [row['id'] for row in store.page_ready()['items']]
        self.assertEqual(unscoped, [held['id']])
        self.assertEqual(store.page_ready(role_name='reviewer')['total'], 0)
        # The raw ledger records are untouched: still blocked with their waits.
        self.assertEqual(store.get(held['id']), held)
        self.assertEqual(store.get(external['id']), external)
        # The classification derives from stored bytes, not from a new write
        # path: rebuilding the derived index from the same raw records keeps it.
        forget_ledger(self.project.root)
        fresh = WorkItems(self.project.root, self.project.snapshots, self.project.insights)
        rebuilt = fresh.page_ready(role_name='worker', writer_roles={'worker'})
        self.assertEqual([row['id'] for row in rebuilt['items']], [held['id']])
        self.assertEqual(rebuilt['items'][0]['status'], 'open')
        self.assertEqual(fresh.get(held['id']), held)

    def test_holder_only_unreadable_evidence_fails_closed(self):
        item = self.block('holder-only unreadable', {'kind': 'code_change'})
        store = self.project.work_items
        snapshot = self.project.snapshots.get()
        fresh = store.get(item['id'])
        self.assertEqual(holder_only(store, fresh, snapshot, writer_roles={'worker'}),
                         'sole_writer_code_change')
        # An invalid snapshot digest is unknown, never holder-only.
        self.assertIsNone(holder_only(store, fresh, {'code_digest': 'bad'},
                                       writer_roles={'worker'}))
        # A store that cannot be read also fails closed.
        with patch.object(store.snapshots, 'get', side_effect=OSError('unavailable')):
            self.assertIsNone(holder_only(store, fresh, None, writer_roles={'worker'}))
        self.assertIsNone(holder_only(None, fresh, snapshot, writer_roles={'worker'}))


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
        self.assertIs(_writer_wait_sources(
            self.project, self.config.roles["worker"]), None)

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
        # Keep code waits self-resolvable so they do not mask unknown CI evidence.
        self.config.roles.pop('maintainer')
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

    def quiet_baseline(self):
        """Publish the current ready set as the input-quiet opening snapshot."""
        current = self.project.snapshots.get()
        baseline = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace), goal=self.project.goal,
            state="Now: Ready work is visible.\nNext: Inspect it", run=None, outcome="wait",
            summary="Input-quiet baseline", wake_at=dt.datetime.now(dt.timezone.utc).timestamp() + 3600,
            inbox_seen=self.project.insights.generation(),
            wake_generation=self.project.control()["wake_generation"], parent=current["id"])
        self.project.snapshots.publish(baseline)

    def finish(self, ctx, outcome="wait", **extra):
        return ctx.handle("finish", {"outcome": outcome, "summary": "Disposition check",
                                    "state": "Now: observed.\nNext: Inspect the remaining work",
                                    **({"next_step": "Inspect the remaining work"} if outcome == "continue" else {}),
                                    **extra})

    def assert_unsealed(self, ctx):
        self.assertIsNone(ctx.finished)
        self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

    def test_wait_and_blocked_accept_ready_work_while_continue_refuses(self):
        item = self.ready("ready")
        for status in ("open", "doing"):
            if status == "doing":
                item = self.project.work_items.transition(
                    item["id"], expected_rev=item["rev"], actor="worker", status=status)
            for outcome in ("wait", "blocked"):
                with self.subTest(status=status, outcome=outcome):
                    ctx = self.context()
                    self.assertTrue(self.finish(ctx, outcome)["sealed"])
            ctx = self.context()
            with self.subTest(status=status, outcome="continue"):
                with self.assertRaisesRegex(Denied, "ready item"):
                    self.finish(ctx, "continue")
                self.assert_unsealed(ctx)
        self.assertEqual(self.project.work_items.get(item["id"])["status"], "doing")

    def test_wait_with_ready_work_reopens_without_item_transition(self):
        item = self.ready("pending work")
        runs = []
        def finish_wait(ctx, *_):
            runs.append(ctx.run_dir.name)
            self.finish(ctx, "wait")
        engine = Engine(self.config, driver=ScriptDriver(finish_wait))
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        snapshot = self.project.snapshots.get()
        self.assertIs(snapshot["progress"]["ready_work"], True)
        # No empty-set signature is anchored, so the ready-work observation
        # re-admits this role without a procedural disposition requirement.
        self.assertIsNone(snapshot["progress"]["ready_work_signature"])
        self.assertNotIn('_earliest_deferred_until',
                         self.project.work_items.page_ready(role_name="worker"))
        self.assertEqual(self.project.work_items.get(item["id"])["status"], "open")
        self.assertTrue(should_run(self.project, snapshot, role_name="worker"))
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        self.assertEqual(len(runs), 2)
        self.assertEqual(self.project.work_items.get(item["id"])["status"], "open")

    def test_repeated_wait_with_ready_work_counts_and_stays_visible(self):
        from mizu.project import idle_with_open_work
        self.ready("repeated ready")
        self.quiet_baseline()
        def finish_wait(ctx, *_):
            self.finish(ctx, "wait", state=ctx.snapshot["state"])
        engine = Engine(self.config, driver=ScriptDriver(finish_wait))
        engine.run(self.project, "worker")
        first = self.project.snapshots.get()
        engine.run(self.project, "worker")
        second = self.project.snapshots.get()
        self.assertEqual((first["progress"]["no_progress_count"],
                          second["progress"]["no_progress_count"]), (1, 2))
        for snapshot in (first, second):
            self.assertIs(snapshot["progress"]["ready_work"], True)
            self.assertIsNone(snapshot["progress"]["ready_work_signature"])
            idle = idle_with_open_work(snapshot)
            self.assertTrue(idle["active"])
            self.assertEqual(idle["reason"], "no_progress_with_ready_work")
        # The counted streak does not consume the ready work or its wake.
        self.assertTrue(should_run(self.project, second, role_name="worker"))

    def test_ready_set_change_starts_new_streak(self):
        item = self.ready("then gone")
        self.quiet_baseline()
        def finish_wait(ctx, *_):
            self.finish(ctx, "wait", state=ctx.snapshot["state"])
        engine = Engine(self.config, driver=ScriptDriver(finish_wait))
        engine.run(self.project, "worker")
        first = self.project.snapshots.get()
        self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                           status="dropped", reason="Handled")
        engine.run(self.project, "worker")
        second = self.project.snapshots.get()
        self.assertEqual(first["progress"]["no_progress_count"], 1)
        self.assertEqual(second["progress"]["no_progress_count"], 1)
        self.assertIs(first["progress"]["ready_work"], True)
        self.assertIs(second["progress"]["ready_work"], False)
        self.assertNotEqual(first["progress"]["ready_work_signature"],
                            second["progress"]["ready_work_signature"])

    def block_deadline(self, key, at):
        item = self.ready(key)
        return self.project.work_items.transition(
            item["id"], expected_rev=item["rev"], actor="worker",
            status="blocked", wait={"kind": "deadline", "at": at})

    def test_future_holder_deadline_clears_signature_and_counts_no_progress(self):
        from mizu.project import idle_with_open_work
        from mizu.snapshot import no_progress_recovery_due
        from mizu.work_item_waits import holder_only
        at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(
            timespec="seconds")
        item = self.block_deadline("future deadline", at)
        store = self.project.work_items
        self.quiet_baseline()
        before = store.page_ready(role_name="worker")
        self.assertEqual([row["id"] for row in before["items"]], [item["id"]])
        self.assertEqual(before["_earliest_deferred_until"], at)
        runs = []

        def finish_wait(ctx, *_):
            runs.append(ctx.run_dir.name)
            self.finish(ctx, "wait", state=ctx.snapshot["state"])

        engine = Engine(self.config, driver=ScriptDriver(finish_wait))
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        first = self.project.snapshots.get()
        self.assertIs(first["progress"]["ready_work"], True)
        self.assertIsNone(first["progress"]["ready_work_signature"])
        self.assertEqual(first["progress"]["no_progress_count"], 1)

        # The same deadline remains visible under an unchanged membership
        # signature, so the second unchanged finish advances the streak just
        # like any other ready work instead of parking until the instant.
        self.assertEqual(engine.run(self.project, "worker")["status"], "completed")
        second = self.project.snapshots.get()
        self.assertEqual(second["progress"]["no_progress_count"], 2)
        self.assertIsNone(second["progress"]["ready_work_signature"])
        self.assertTrue(no_progress_recovery_due(second["progress"], "wait", self.config, "worker"))
        self.assertTrue(should_run(self.project, second, role_name="worker"))
        idle = idle_with_open_work(second)
        self.assertTrue(idle["active"])
        self.assertEqual(idle["reason"], "no_progress_with_ready_work")
        after = store.page_ready(role_name="worker")
        self.assertEqual(after["signature"], before["signature"])
        self.assertEqual([row["id"] for row in after["items"]], [item["id"]])
        self.assertEqual(after["items"][0]["deferred_until"], at)
        self.assertEqual(after["_earliest_deferred_until"], at)
        self.assertEqual(store.get(item["id"])["status"], "blocked")
        self.assertEqual(holder_only(store, store.get(item["id"]), second,
                                     writer_roles={"worker"}), "sole_holder_deadline")
        self.assertEqual(len(runs), 2)

    def test_malformed_deferred_instant_is_due(self):
        for value in (None, "", "not-a-time", "2030-01-01T00:00:00"):
            with self.subTest(value=value):
                self.assertTrue(_deferred_until_due(value))
        self.assertFalse(_deferred_until_due("2099-01-01T00:00:00+00:00"))

    def test_wait_accepts_a_remaining_ready_item_without_disposition(self):
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
        # One completion and one named observable blocker leave a ready item.
        store.transition(items[0]["id"], expected_rev=binding["rev"], actor="worker", status="done",
                         snapshot_id=snapshot["id"])
        question = stored_question_insight(self.project)
        store.transition(items[1]["id"], expected_rev=1, actor="worker", status="blocked",
                         wait={"kind": "insight_decided", "insight": question["id"]})
        self.assertTrue(self.finish(ctx)["sealed"])
        self.assertEqual(store.get(items[2]["id"])["status"], "open")
        # A handoff-free continue still names the failed ready-set condition.
        ctx = self.context()
        with self.assertRaisesRegex(Denied, "1 ready item"):
            self.finish(ctx, "continue")
        self.assert_unsealed(ctx)
        store.transition(items[2]["id"], expected_rev=1, actor="worker", status="dropped",
                         reason="No longer needed")
        self.assertTrue(self.finish(self.context(), "continue")["sealed"])

    def test_finish_empty_signature_does_not_rewake_for_own_dispositions(self):
        item = self.ready("owned disposition")
        question = stored_question_insight(self.project)
        def dispose_and_wait(ctx, *_):
            ctx.handle("work_items_transition", {
                "id": item["id"], "expected_rev": 1, "status": "blocked",
                "wait": {"kind": "dependency", "recipient": "reviewer",
                         "requires": {"insight": question["id"], "rev": 1, "action": "accept"}}})
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
            self.finish(self.context(), "continue")

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
            self.config.limits, work_items_prompt=1))
        self.project.config = self.config
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
                with patch("mizu.runtime._writer_wait_sources", return_value=live):
                    self.assertFalse(_progress_self_admits(snapshot, self.config, "worker", due))
                    self.assertFalse(should_run(self.project, snapshot, role_name="worker", current_time=due))
