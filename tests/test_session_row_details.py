"""Bounded row detail elision; fake session selection is not adapter integration."""
import copy
import dataclasses
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import check_jobs
from mizu.engine_config import (PROMPT_STATE_BYTES, effective, read_prompt_state,
                                save_session, session_record)
from mizu.fs import read_json, write_json
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.runtime import Engine, session_prompt
from mizu.unit_prompts import _row_detail_projection


class RowProjectionTests(Fixture):
    def test_complete_rows_age_only_changes_and_full_override(self):
        ctx = self.context('worker')
        work = {'items': [{'id': 'wi_' + 'a' * 32, 'title': 'Work', 'status': 'blocked',
                          'rev': 3, 'wait': {'kind': 'deadline', 'at': '2099-01-01T00:00:00+00:00'},
                          'description_preview': 'Long scope ' * 100, 'description_truncated': False,
                          'age_seconds': 5, 'doing_age_seconds': 10, 'blocked_age_seconds': 20, 'doing_stale': False}],
                'total': 9, 'truncated': True, 'overflow_count': 8, 'coverage_complete': False}
        job = {'id': 'b' * 32, 'kind': 'verify', 'status': 'queued',
               'command_preview': ['echo test'], 'command_count': 1,
               'wait': {'arguments': {'wait_for_check': 'b' * 32}}}
        checks = {'check_jobs': [job], 'shared_checks': [{**job, 'id': 'c' * 32}],
                  'check_continuation': {'id': job['id']},
                  'check_discovery': {'coverage_complete': False, 'summaries_omitted': 2}}
        original = copy.deepcopy((work, checks))
        full = _row_detail_projection(ctx, work, checks)
        self.assertEqual(full, original)
        prior = ctx.prompt_row_fingerprints
        work['items'][0].update(age_seconds=6, doing_age_seconds=11, blocked_age_seconds=21)
        small_work, small_checks = _row_detail_projection(ctx, work, checks, prior)
        for source, small in ((work['items'], small_work['items']),
                              (checks['check_jobs'], small_checks['check_jobs']),
                              (checks['shared_checks'], small_checks['shared_checks'])):
            self.assertEqual(len(source), len(small))
            row = small[0]
            self.assertIs(row['details_unchanged'], True)
            ref = row['details_reference']
            validate(ref['arguments'], DEFINITIONS[ref['tool'].removeprefix('mizu_')][1])
            # Removing only declared bulky text preserves every other field.
            self.assertEqual({k: v for k, v in row.items() if not k.startswith('details_')},
                             {k: v for k, v in source[0].items()
                              if k not in {'description_preview', 'command_preview'}})
        self.assertEqual({k: v for k, v in small_work.items() if k != 'items'},
                         {k: v for k, v in work.items() if k != 'items'})
        for field in ('check_continuation', 'check_discovery'):
            self.assertEqual(small_checks[field], checks[field])
        self.assertLess(len(json.dumps((small_work, small_checks))), len(json.dumps((work, checks))))
        # Liveness changes count as content, unlike raw ages.
        for field, value in (('doing_stale', True), ('title', 'Changed'), ('rev', 4),
                             ('description_preview', 'Different'), ('wait', None)):
            changed = copy.deepcopy(work)
            changed['items'][0][field] = value
            self.assertEqual(_row_detail_projection(ctx, changed, checks, prior)[0], changed)
        changed_checks = copy.deepcopy(checks)
        changed_checks['check_jobs'][0]['status'] = 'running'
        changed_checks['shared_checks'].append({**job, 'id': 'd' * 32})
        _, projected = _row_detail_projection(ctx, work, changed_checks, prior)
        self.assertEqual(projected['check_jobs'], changed_checks['check_jobs'])
        self.assertEqual(projected['shared_checks'][1], changed_checks['shared_checks'][1])
        ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
            ctx.config.limits, session_delta_details='full'))
        self.assertEqual(_row_detail_projection(ctx, work, checks, prior), (work, checks))
        self.assertEqual(checks, original[1])  # projection never mutates discovery.

    def test_work_item_details_stay_full_without_exact_read_capability(self):
        ctx = self.context('worker')
        ctx.role = dataclasses.replace(
            ctx.role, capabilities=tuple(cap for cap in ctx.role.capabilities
                                         if cap != 'work_items_read'))
        work = {'items': [{'id': 'wi_' + 'a' * 32, 'title': 'Work', 'status': 'open', 'rev': 1,
                           'description_preview': 'Full details'}], 'total': 1,
                'truncated': False, 'overflow_count': 0, 'coverage_complete': True}
        checks = {'check_jobs': [], 'shared_checks': []}
        _row_detail_projection(ctx, work, checks)
        prior = ctx.prompt_row_fingerprints
        projected, _ = _row_detail_projection(ctx, work, checks, prior)
        self.assertIn('description_preview', projected['items'][0])
        self.assertNotIn('details_unchanged', projected['items'][0])


class SessionRowDetailsTests(Fixture):
    def setUp(self):
        super().setUp()
        roles = dict(self.config.roles)
        roles['worker'] = dataclasses.replace(
            roles['worker'], capabilities=tuple(dict.fromkeys(
                (*roles['worker'].capabilities, 'check_job', 'work_items_read'))))
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project = Project(self.config, 'sample')
        self.item = self.project.work_items.create(
            idempotency_key='row', title='Review scope', actor='worker',
            description='Complete description ' * 80)['record']['work_item']
        ctx = self.context('worker')
        self.job = check_jobs.submit(ctx, script='echo queued details')
        settings = effective(ctx.config, ctx.role, ctx.role.profile)
        self.path, _ = session_record(ctx, ctx.role.profile, settings)
        save_session(self.path, 'row-session', {})

    def run_observed(self, *, settled=True, observe=True):
        prompts = []
        def callback(ctx, prompt, profile):
            prompts.append(json.loads(prompt))
            if observe:
                profile = profile or ctx.role.profile
                session_record(ctx, profile, effective(ctx.config, ctx.role, profile))
        class Driver(ScriptDriver):
            def execute(driver, *args, **kwargs):
                return {**super().execute(*args, **kwargs), 'settled': settled}
        result = Engine(self.config, driver=Driver(callback)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        return prompts[0]

    def peek(self):
        ctx = self.context('worker')
        prompt, mode, *_ = session_prompt(ctx, ctx.role, ctx.run_dir)
        return ctx, json.loads(prompt), mode

    def assert_full_details(self, prompt):
        self.assertIn('description_preview', prompt['open_work_items']['items'][0])
        self.assertIn('command_preview', prompt['check_jobs'][0])
        self.assertNotIn('details_unchanged', prompt['open_work_items']['items'][0])

    def test_settled_cursor_repetition_changed_new_and_exact_recovery(self):
        self.assert_full_details(self.run_observed())
        cursor = read_prompt_state(self.path.parent)
        self.assertIn('row_details', cursor)
        self.assertIn('broker_grant', cursor)
        ctx, prompt, mode = self.peek()
        self.assertEqual(mode, 'delta')
        row = prompt['open_work_items']['items'][0]
        self.assertTrue(row['details_unchanged'])
        self.assertEqual(row['id'], self.item['id'])
        ref = row['details_reference']
        detail = ctx.handle(ref['tool'].removeprefix('mizu_'), ref['arguments'])
        self.assertIn(self.item['description'], json.dumps(detail))
        ref = prompt['check_jobs'][0]['details_reference']
        detail = ctx.handle(ref['tool'].removeprefix('mizu_'), ref['arguments'])
        self.assertEqual(detail['command_preview'], ['echo queued details'])
        self.assertEqual(read_prompt_state(self.path.parent), cursor)  # peeking never writes.
        self.project.work_items.transition(self.item['id'], expected_rev=1, status='doing', actor='worker')
        self.project.work_items.create(idempotency_key='new', title='New', actor='worker', description='New scope')
        _, changed, _ = self.peek()
        self.assertEqual(len(changed['open_work_items']['items']), 2)
        for work in changed['open_work_items']['items']:
            self.assertIn('description_preview', work)
            self.assertNotIn('details_unchanged', work)
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, session_delta_details='full'))
        self.project.config = self.config
        self.assert_full_details(self.peek()[1])

    def test_invalid_cursor_and_session_resets_resend_full_details(self):
        self.run_observed()
        cursor = read_prompt_state(self.path.parent)
        bad_digest = copy.deepcopy(cursor)
        bad_digest['row_details']['rows']['check_jobs'][self.job['id']] = 'invalid'
        over_rows = copy.deepcopy(cursor)
        over_rows['row_details']['rows']['check_jobs'] = {str(n): 'a' * 64 for n in range(33)}
        bad_key = copy.deepcopy(cursor)
        bad_key['row_details']['key'] = 'other-key'
        bad_inbox = copy.deepcopy(cursor)
        bad_inbox['inbox_generation'] = 'not-a-digest'
        bad_grant = copy.deepcopy(cursor)
        bad_grant['broker_grant']['session'] = '0' * 64
        for invalid in (None, {}, [], {**cursor, 'row_details': []}, bad_digest, over_rows,
                        bad_key, bad_inbox, bad_grant):
            with self.subTest(invalid=invalid):
                write_json(self.path.parent / 'prompt_state.json', invalid)
                self.assert_full_details(self.peek()[1])
        (self.path.parent / 'prompt_state.json').unlink()
        self.assert_full_details(self.peek()[1])
        (self.path.parent / 'prompt_state.json').write_text('{')
        self.assert_full_details(self.peek()[1])
        (self.path.parent / 'prompt_state.json').write_text('x' * (PROMPT_STATE_BYTES + 1))
        self.assertIsNone(read_prompt_state(self.path.parent))
        self.assert_full_details(self.peek()[1])
        write_json(self.path.parent / 'prompt_state.json', cursor)
        write_json(self.path.parent / 'generation.json', {'generation': 1})
        self.assert_full_details(self.peek()[1])
        (self.path.parent / 'generation.json').unlink()
        save_session(self.path, 'replacement-session', {})
        self.assert_full_details(self.peek()[1])
        self.path.unlink()
        _, fresh, mode = self.peek()
        self.assertEqual(mode, 'full')
        self.assert_full_details(fresh)
        # Even a copied cursor/session in a different selected key is not reused.
        new_path = self.path.parent.with_name('new-key') / 'session.json'
        new_path.parent.mkdir()
        save_session(new_path, 'row-session', {})
        write_json(new_path.parent / 'prompt_state.json', cursor)
        with patch('mizu.engine_config.session_record', return_value=(new_path, read_json(new_path))):
            self.assert_full_details(self.peek()[1])

    def test_unsettled_or_unobserved_dispatch_cannot_supply_fingerprints(self):
        self.run_observed()
        cursor = (self.path.parent / 'prompt_state.json').read_bytes()
        self.run_observed(settled=False)
        self.assertEqual((self.path.parent / 'prompt_state.json').read_bytes(), cursor)
        self.run_observed(observe=False)
        self.assertNotIn('row_details', read_prompt_state(self.path.parent))
        self.assert_full_details(self.peek()[1])
