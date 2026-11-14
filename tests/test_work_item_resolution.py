"""Local wait lifecycle: immutable proof, holder authority, no CI requests."""
import copy
import dataclasses
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture, ScriptDriver, bind_delivery, index_items, forget_ledger
from mizu import work_item_resolution as resolution, work_order
from mizu.distribution import public_source_identity
from mizu.errors import Busy, ConfigError, Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, digest, read_json
from support import history_write_json as write_json
from mizu.project import Project, check_project_settings
from mizu.publication import Publication
from mizu.unit_prompts import prompt_for
from mizu.work_items import MAX_EVENT_BYTES, IS_WINDOWS


class WaitResolutionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            'poll_enabled': True, 'command': ['configured-adapter'], 'ci_branch': 'published'})
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.baseline = self.project.snapshots.get()

    def block(self, key='item', kind='code_change', actor='worker', delivery=True):
        item = self.store.create(actor=actor, idempotency_key=key, title='Wait for evidence',
                                 scope='Exact accepted scope')['record']['work_item']
        if kind == 'publication_ci':
            if delivery:
                item = bind_delivery(self.project, item['id'], self.baseline['id'])
            wait = {'kind': kind, 'snapshot_id': self.baseline['id'], 'checks': ['unit']}
        else:
            wait = {'kind': kind}
        return self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
                                     actor=actor, wait=wait)

    def snapshot(self, label='changed', *, parent=None, publish=True):
        snapshots = self.project.snapshots
        (self.project.workspace / 'change.txt').write_text(label)
        snapshot = snapshots.create(snapshots.capture_files(self.project.workspace),
            goal='Goal', state='State', run=None, outcome='wait', summary=label,
            **({'parent': parent} if parent else {}))
        if publish:
            snapshots.publish(snapshot)
        return snapshot

    def receipt(self, snapshot, *, source='d' * 40, branch='published'):
        publication = Publication(self.project.root, source, snapshot['code_digest'], branch,
                                  'push', projection=public_source_identity(snapshot['files']))
        intent, _ = publication.prepare('approval', 'run')
        publication.mark_dispatch()
        return publication.complete(intent, 'b' * 40)

    def reconcile(self, *, role='worker', limit=20):
        resolution.reconcile(self.project, role, limit=limit)

    def test_reconcile_requires_a_positive_page_limit(self):
        with self.assertRaises(Denied):
            self.reconcile(limit=0)

    def test_default_and_manual_policy_and_refresh_validation(self):
        check_project_settings({'roles': ['worker']})
        item = self.block()
        self.snapshot()
        path = self.project.root / 'project.toml'
        original = path.read_text()
        path.write_text('work_item_wait_resolution = "manual"\n' + original)
        self.project.refresh_settings()
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        path.write_text(original)
        self.project.refresh_settings()
        self.reconcile()
        self.assertEqual(self.store.get(item['id'])['status'], 'open')
        for bad in ('unknown', True, [], {}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                check_project_settings({'roles': ['worker'], 'work_item_wait_resolution': bad})

    def test_due_code_change_records_exact_proof_and_preserves_history_scope_and_holder(self):
        item = self.block()
        original = canonical(item)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        snapshot = self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        proof = opened['wait_resolution']
        self.assertEqual((opened['status'], opened['holder'], opened['scope']),
                         ('open', 'worker', item['scope']))
        self.assertEqual(proof, {'wait_rev': item['rev'], 'wait_digest': digest(canonical(item['wait'])),
            'kind': 'due', 'resolved_at': opened['updated_at'],
            'evidence': {'snapshot_id': snapshot['id'], 'code_digest': snapshot['code_digest']}})
        self.assertIsNone(opened['wait'])
        self.assertEqual(canonical(read_json(self.project.root / 'work-items' / item['id'] / '2.json')), original)
        self.assertEqual(self.store.explain(item['id'])['history'][-1], opened)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), opened)
        self.assertEqual(self.store.page_open()['items'][0]['wait_resolution'], proof)

    def test_read_projections_and_tool_reads_do_not_resolve(self):
        item = self.block()
        self.snapshot()
        before = {p.name: p.read_bytes() for p in (self.project.root / 'work-items' / item['id']).iterdir()}
        self.store.page_ready(role_name='worker')
        self.store.page_open()
        self.store.page_open_readonly()
        self.store.explain(item['id'])
        self.context().handle('work_items_read', {'id': item['id']})
        after = {p.name: p.read_bytes() for p in (self.project.root / 'work-items' / item['id']).iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(self.store.get(item['id']), item)

    def test_scheduler_reconciles_before_projections_once_and_requires_existing_grants(self):
        from mizu.runtime import Engine
        item = self.block()
        self.snapshot()
        context = self.context()
        with patch.object(self.store, 'transition', wraps=self.store.transition) as transition:
            dispatch = Engine(self.config)._dispatch_state(
                self.project, 'worker', context.role, event_trigger=False)
            opening = json.loads(prompt_for(context))
            prompt_for(context)
        self.assertTrue(dispatch['_resolved_wait'])
        self.assertEqual(transition.call_count, 1)
        self.assertEqual(opening['open_work_items']['items'][0]['status'], 'open')
        self.assertEqual(opening['ready_work']['items'][0]['id'], item['id'])
        self.assertTrue(work_order._is_actionable_locked(self.project, 'worker', item['id'], None))
        self.assertFalse(work_order._is_actionable_locked(self.project, 'reviewer', item['id'], None))
        second = self.block('second')
        self.snapshot('next')
        context = self.context()
        context.role = dataclasses.replace(context.role, capabilities=tuple(
            c for c in context.role.capabilities if c != 'work_items_transition'))
        prompt_for(context)
        self.assertEqual(self.store.get(second['id'])['status'], 'blocked')

    def test_unproven_publication_wait_does_not_become_an_event_wake(self):
        from mizu.runtime import Engine
        item = self.block(kind='publication_ci')
        role = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        dispatch = Engine(self.config)._dispatch_state(
            self.project, 'worker', role, event_trigger=True)
        self.assertEqual(dispatch['skip']['skipped'], 'no-event')
        self.assertEqual(self.store.get(item['id'])['status'], 'blocked')

    def test_superseded_wait_resolves_before_ready_page_and_is_an_exact_event_wake(self):
        from mizu.runtime import Engine
        item = self.block(kind='publication_ci')
        newer = self.snapshot()
        self.receipt(newer)
        self.assertNotIn(item['id'], {row['id'] for row in
                                      self.store.page_ready(role_name='worker')['items']})
        role = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        engine = Engine(self.config)
        pre_dispatch = engine._dispatch_state(
            self.project, 'worker', role, event_trigger=True)
        self.assertNotIn('skip', pre_dispatch)
        self.assertTrue(pre_dispatch['_resolved_wait'])
        self.assertTrue(pre_dispatch['_pending_wait_resolution'])
        self.assertTrue(pre_dispatch['_waits_reconciled'])
        dispatch = engine._dispatch_state(
            self.project, 'worker', role, event_trigger=True,
            prior_wait_resolution_pending=pre_dispatch['_pending_wait_resolution'],
            reconcile_waits=False)
        self.assertNotIn('skip', dispatch)
        self.assertTrue(dispatch['_resolved_wait'])
        opened = self.store.get(item['id'])
        self.assertEqual(opened['status'], 'open')
        self.assertEqual(opened['holder'], 'worker')
        self.assertEqual(opened['wait_resolution']['kind'], 'superseded')
        self.assertIn(item['id'], {row['id'] for row in
                                   self.store.page_ready(role_name='worker')['items']})

    def parked(self, signature):
        from mizu.fs import now
        from mizu.snapshot import PROGRESS_SIGNALS
        current = self.project.snapshots.get()
        progress = {"schema": 3, "signals": {name: False for name in PROGRESS_SIGNALS},
                    "selected": list(self.config.limits.progress_signals), "role": "worker",
                    "has_progress": False, "no_progress_count": 0,
                    "obligations_digest": "0" * 64, "recorded_at": now(),
                    "ready_work": True, "ready_work_signature": signature}
        snapshot = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Now: Parked", run=None, outcome="done",
            summary="Inputs already delivered", progress=progress,
            inbox_seen=self.project.insights.generation(),
            wake_generation=self.project.control()["wake_generation"], parent=current['id'])
        self.project.snapshots.publish(snapshot)
        return self.project.snapshots.get()

    def test_actual_daemon_gate_reconciles_hidden_wait_before_ready_observation(self):
        from mizu.runtime import should_run
        filler = self.store.create(actor='worker', idempotency_key='first', title='Earlier item')[
            'record']['work_item']
        item = self.block(kind='publication_ci')
        self.receipt(self.snapshot())
        page = self.store.page_ready(role_name='worker', limit=1)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['id'], filler['id'])
        parked = self.parked(page['signature'])
        self.assertTrue(should_run(self.project, parked, role_name='worker'))
        self.assertEqual(self.store.get(item['id'])['status'], 'open')
        page = self.store.page_ready(role_name='worker', limit=1)
        self.assertEqual(page['items'][0]['id'], filler['id'])
        self.assertEqual(page['total'], 2)
        self.assertTrue(page['_wait_resolution_pending'])

    def test_daemon_unproven_wait_and_reconciliation_failure_preserve_other_wakes(self):
        from mizu.runtime import should_run
        item = self.block(kind='publication_ci')
        parked = self.parked(self.store.page_ready(role_name='worker')['signature'])
        self.assertFalse(should_run(self.project, parked, role_name='worker'))
        self.assertEqual(self.store.get(item['id']), item)
        self.store.create(actor='worker', idempotency_key='unrelated', title='Unrelated work')
        with patch('mizu.work_item_resolution.reconcile', side_effect=Denied('Unavailable proof')):
            self.assertTrue(should_run(self.project, parked, role_name='worker'))
        self.assertEqual(self.store.get(item['id']), item)

    def test_daemon_reconciliation_requires_writer_and_both_existing_grants(self):
        from mizu.runtime import should_run
        item = self.block(kind='publication_ci')
        self.receipt(self.snapshot())
        parked = self.parked(self.store.page_ready(role_name='worker')['signature'])
        original = self.config.roles['worker']
        for missing in ('work_order', 'work_items_transition'):
            self.config.roles['worker'] = dataclasses.replace(original, capabilities=tuple(
                cap for cap in original.capabilities if cap != missing))
            self.assertFalse(should_run(self.project, parked, role_name='worker'))
            self.assertEqual(self.store.get(item['id']), item)
        self.config.roles['worker'] = dataclasses.replace(original, workspace='read')
        should_run(self.project, parked, role_name='worker')
        self.assertEqual(self.store.get(item['id']), item)

    def test_busy_event_admission_recovers_resolution_after_restart_with_stale_ready_signature(self):
        from mizu.runtime import Engine
        item = self.store.create(actor='worker', idempotency_key='busy', title='Exact wait')[
            'record']['work_item']
        item = bind_delivery(self.project, item['id'], self.baseline['id'])
        old_signature = self.store.page_ready(role_name='worker')['signature']
        item = self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
            actor='worker', wait={'kind': 'publication_ci', 'snapshot_id': self.baseline['id'],
                                  'checks': ['unit']})
        self.receipt(self.snapshot())
        parked = self.parked(old_signature)
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        engine = Engine(self.config, driver=ScriptDriver())
        with patch.object(engine, '_admit', side_effect=Busy('Workspace already held')) as admit:
            with self.assertRaises(Busy):
                engine.run(self.project, 'worker', event_trigger=True)
        admit.assert_called_once()
        self.assertFalse(engine.driver.calls)
        self.assertEqual(self.project.snapshots.get()['id'], parked['id'])
        opened = self.store.get(item['id'])
        self.assertEqual(opened['status'], 'open')
        page = self.store.page_ready(role_name='worker', limit=1)
        self.assertNotEqual(page['signature'], old_signature)
        self.assertFalse(self.store.page_ready(role_name='reviewer')['_wait_resolution_pending'])
        # Fresh project and engine discard every in-process reconciliation hint.
        self.project = Project(self.config, 'sample')
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        delivered = self.project.snapshots.get()
        self.assertEqual(delivered['progress']['ready_work_signature'], page['signature'])
        self.assertEqual(delivered['progress']['wait_resolution_tokens'],
                         [f"{item['id']}:{item['rev']}"])
        self.assertEqual(self.project.work_items.get(item['id']), opened)
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
        self.assertFalse(engine.driver.calls)
        self.store.create(actor='worker', idempotency_key='unrelated-after-delivery',
                          title='Unrelated open item')
        self.assertNotEqual(self.store.page_ready(role_name='worker')['signature'],
                            delivered['progress']['ready_work_signature'])
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
        self.assertFalse(engine.driver.calls)

        second = self.store.create(actor='worker', idempotency_key='second-resolution',
                                   title='A later wait')['record']['work_item']
        second = bind_delivery(self.project, second['id'], delivered['id'])
        second = self.store.transition(second['id'], expected_rev=second['rev'], status='blocked',
            actor='worker', wait={'kind': 'code_change'})
        self.snapshot('second wait becomes due')
        engine = Engine(self.config, driver=ScriptDriver())
        self.assertEqual(engine.run(self.project, 'worker', event_trigger=True)['status'], 'completed')
        self.assertTrue(engine.driver.calls)
        self.assertEqual(self.store.get(second['id'])['wait_resolution']['wait_rev'], second['rev'])

    def test_explicit_resolution_delivery_advances_across_bounded_prompt_pages(self):
        from mizu.runtime import Engine
        limits = dataclasses.replace(self.config.limits, work_items_prompt=1)
        self.config = dataclasses.replace(self.config, limits=limits)
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.baseline = self.project.snapshots.get()
        first = self.block('page-overflow-first')
        second = self.block('page-overflow-second')
        self.snapshot('both waits are due')

        driver = ScriptDriver()
        result = Engine(self.config, driver=driver).run(
            self.project, 'worker', event_trigger=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(driver.calls), 1)
        prompt = json.loads(driver.calls[0][1])
        open_id = prompt['open_work_items']['items'][0]['id']
        ready_id = prompt['ready_work']['items'][0]['id']
        ids = {first['id'], second['id']}
        self.assertIn(open_id, ids)
        self.assertIn(ready_id, ids)
        self.assertNotEqual(open_id, ready_id)
        delivered = self.project.snapshots.get()['progress']['wait_resolution_tokens']
        self.assertEqual(set(delivered), {
            f"{first['id']}:{first['rev']}", f"{second['id']}:{second['rev']}"})

        idle_driver = ScriptDriver()
        idle = Engine(self.config, driver=idle_driver).run(
            self.project, 'worker', event_trigger=True)
        self.assertEqual(idle['skipped'], 'no-event')
        self.assertFalse(idle_driver.calls)

    def test_explicit_resolution_delivery_accumulates_across_more_than_two_pages(self):
        from mizu.runtime import Engine
        limits = dataclasses.replace(self.config.limits, work_items_prompt=1)
        self.config = dataclasses.replace(self.config, limits=limits)
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.baseline = self.project.snapshots.get()
        # Both bounded prompt pages may advance distinct proofs in one run.
        # Five waits force progression over more than two page pairs while
        # avoiding an assumption about how many new proofs each pass presents.
        items = [self.block(f'page-overflow-{index}') for index in range(5)]
        self.snapshot('all five waits are due')
        expected = {f"{item['id']}:{item['rev']}" for item in items}
        item_ids = {item['id'] for item in items}
        delivered_ids = set()
        observed_idle = False

        for _ in range(len(items) + 1):
            driver = ScriptDriver()
            result = Engine(self.config, driver=driver).run(
                self.project, 'worker', event_trigger=True)
            if result.get('skipped') == 'no-event':
                self.assertFalse(driver.calls)
                observed_idle = True
                break
            self.assertEqual(result.get('status'), 'completed')
            self.assertEqual(len(driver.calls), 1)
            prompt = json.loads(driver.calls[0][1])
            shown_ids = {
                item['id']
                for page_name in ('open_work_items', 'ready_work')
                for item in prompt[page_name]['items']
            }
            active_resolution_ids = {
                item_id for item_id in item_ids
                if "wait_resolution" in self.store.get(item_id)
            }
            shown_resolution_ids = shown_ids & active_resolution_ids
            self.assertTrue(shown_resolution_ids - delivered_ids)
            delivered_ids.update(shown_resolution_ids)
            progress = set(self.project.snapshots.get()['progress']['wait_resolution_tokens'])
            self.assertEqual(progress, {
                token for token in expected if token.split(":", 1)[0] in delivered_ids
            })

        self.assertTrue(observed_idle, 'delivery did not reach a no-event pass within the bound')
        self.assertEqual(delivered_ids, item_ids)
        self.assertEqual(progress, expected)
        idle_driver = ScriptDriver()
        idle = Engine(self.config, driver=idle_driver).run(
            self.project, 'worker', event_trigger=True)
        self.assertEqual(idle.get('skipped'), 'no-event')
        self.assertFalse(idle_driver.calls)

    def test_resolution_high_water_advances_beyond_200_and_quiesces(self):
        from mizu.runtime import Engine, _progress_record, _ready_work_observation
        from mizu.snapshot import MAX_WAIT_RESOLUTION_TOKENS, PROGRESS_SIGNALS
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=1))
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.baseline = self.project.snapshots.get()
        template = self.block('overflow-template')
        self.snapshot('template wait is due')
        self.reconcile(limit=1)
        template = self.store.get(template['id'])
        # Synthetic current projections reuse one ledger-validated proof. Keep
        # the 203-pass regression cheap: exercise real scans, opening pages,
        # resolution admission and snapshot progress, not 203 history replays/pass.
        items = {f'wi_{index:032x}': {**template, 'id': f'wi_{index:032x}'}
                 for index in range(203)}

        index_items(self.project.root, items.values(), replace=True)
        expected = {f"{item_id}:{template['wait_resolution']['wait_rev']}" for item_id in items}
        shown = set()
        previous_boundary = None
        snapshot = self.project.snapshots.get()
        role = self.config.roles['worker']
        for _ in range(len(items)):
            if not _ready_work_observation(self.project, snapshot, role)[2]:
                break
            context = SimpleNamespace(config=self.config, project=self.project,
                role=role, snapshot=snapshot, goal_digest=digest(self.project.goal.encode()))
            prompt = work_order._capture_opening(context)
            current = set()
            for page in ('open_work_items', 'ready_work'):
                self.assertLessEqual(len(prompt[page]['items']), 1)
                current.update(f"{row['id']}:{row['wait_resolution']['wait_rev']}"
                               for row in prompt[page]['items'])
            self.assertTrue(current - shown)
            shown.update(current)
            progress = _progress_record(snapshot, 'worker', (),
                {name: False for name in PROGRESS_SIGNALS}, 'wait', ready_work=True,
                ready_work_signature=prompt['ready_signature'],
                wait_resolution_tokens=context.wait_resolution_tokens,
                wait_resolution_high_water=context.wait_resolution_high_water)
            snapshot = {**snapshot, 'progress': progress}
            self.assertEqual(progress['schema'], 5)
            self.assertLessEqual(len(progress['wait_resolution_tokens']), MAX_WAIT_RESOLUTION_TOKENS)
            boundary = progress['wait_resolution_high_water']['token']
            self.assertTrue({token for token in expected if token <= boundary} <= shown)
            if previous_boundary is not None:
                self.assertGreater(boundary, previous_boundary)
            previous_boundary = boundary
        self.assertEqual(shown, expected)
        # Restart and unrelated ready work cannot replay the overflow whose
        # exact receipt no longer fits in the 200-token list.
        before = snapshot['progress']
        durable = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal='Goal', state='State', run=None, outcome='wait', summary='Delivered overflow',
            progress=before)
        self.project.snapshots.publish(durable)
        ordinary_id = 'wi_' + 'f' * 32
        items[ordinary_id] = {key: value for key, value in
                              {**template, 'id': ordinary_id, 'holder': None}.items()
                              if key != 'wait_resolution'}
        for _ in range(2):
            driver = ScriptDriver()
            self.assertEqual(Engine(self.config, driver=driver).run(
                self.project, 'worker', event_trigger=True)['skipped'], 'no-event')
            self.assertFalse(driver.calls)
        after_page = self.store.page_ready(role_name='worker', limit=1,
            _delivered_wait_resolution_tokens=before['wait_resolution_tokens'],
            _wait_resolution_high_water=before['wait_resolution_high_water'])
        self.assertEqual(after_page['_wait_resolution_high_water'], before['wait_resolution_high_water'])

    def test_resolution_high_water_resets_for_changed_proofs_and_prunes(self):
        first = self.block('boundary-first')
        second = self.block('boundary-second')
        self.snapshot('both due')
        self.reconcile(limit=2)
        from mizu.work_items import wait_resolution_token
        proofs = sorted((self.store.get(item['id']) for item in (first, second)),
                        key=wait_resolution_token)
        earliest, latest = map(wait_resolution_token, proofs)
        # One out-of-order shown proof cannot cover an unseen earlier proof.
        page = self.store.page_ready(role_name='worker', limit=1,
                                    _delivered_wait_resolution_tokens=[latest])
        self.assertEqual(wait_resolution_token(page['items'][0]), earliest)
        boundary = page['_wait_resolution_high_water']
        self.assertEqual(boundary['token'], earliest)
        # A structurally valid but absent boundary cannot authorize skipping.
        absent = self.store.page_ready(role_name='worker', limit=1,
            _delivered_wait_resolution_tokens=[latest],
            _wait_resolution_high_water={**boundary, 'token': 'wi_' + 'f' * 32 + ':1'})
        self.assertEqual(wait_resolution_token(absent['items'][0]), earliest)
        complete = self.store.page_ready(role_name='worker', limit=1,
            _delivered_wait_resolution_tokens=[earliest, latest],
            _wait_resolution_high_water=boundary)
        self.assertFalse(complete['_wait_resolution_undelivered'])
        boundary = complete['_wait_resolution_high_water']
        self.assertEqual(boundary['token'], latest)
        # Same ID, new wait revision: even a token below the previous boundary
        # must be offered. Keep the boundary item eligible to test set binding,
        # not just the boundary-membership safeguard.
        item = proofs[0]
        self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
                              actor='worker', wait={'kind': 'code_change'})
        self.snapshot('new wait due')
        self.reconcile(limit=1)
        replacement = wait_resolution_token(self.store.get(item['id']))
        self.assertNotEqual(replacement, earliest)
        page = self.store.page_ready(role_name='worker', limit=1,
            _delivered_wait_resolution_tokens=[earliest, latest],
            _wait_resolution_high_water=boundary)
        self.assertTrue(page['_wait_resolution_undelivered'])
        self.assertEqual(wait_resolution_token(page['items'][0]), replacement)
        self.assertNotEqual(page['_wait_resolution_high_water']['signature'], boundary['signature'])
        self.assertEqual(page['_wait_resolution_delivered_current'], [latest])
        # Losing holder eligibility prunes both the stale exact token and the
        # prefix binding. An empty proof set has no boundary.
        for proof in proofs:
            current = self.store.get(proof['id'])
            self.store.transition(current['id'], expected_rev=current['rev'],
                                  status='dropped', actor='worker', reason='Prune resolved cause')
        empty = self.store.page_ready(role_name='worker', limit=1,
            _delivered_wait_resolution_tokens=[replacement, latest],
            _wait_resolution_high_water=page['_wait_resolution_high_water'])
        self.assertFalse(empty['_wait_resolution_undelivered'])
        self.assertEqual(empty['_wait_resolution_delivered_current'], [])
        self.assertIsNone(empty['_wait_resolution_high_water']['token'])

    def test_wait_resolution_progress_prunes_proofs_after_item_leaves_open_state(self):
        from mizu.runtime import Engine
        limits = dataclasses.replace(self.config.limits, work_items_prompt=1)
        self.config = dataclasses.replace(self.config, limits=limits)
        self.config.roles['worker'] = dataclasses.replace(
            self.config.roles['worker'], on_change=False, daemon=False,
            interval_seconds=0, calendar=(), decision_events=(), work_item_events=())
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.baseline = self.project.snapshots.get()
        first = self.block('prune-first')
        self.snapshot('first wait is due')
        first_driver = ScriptDriver()
        self.assertEqual(Engine(self.config, driver=first_driver).run(
            self.project, 'worker', event_trigger=True)['status'], 'completed')
        first_token = f"{first['id']}:{first['rev']}"
        self.assertEqual(self.project.snapshots.get()['progress']['wait_resolution_tokens'],
                         [first_token])

        current_first = self.store.get(first['id'])
        self.store.transition(first['id'], expected_rev=current_first['rev'],
                              status='doing', actor='worker')
        second = self.block('prune-second')
        self.snapshot('second wait is due')
        second_driver = ScriptDriver()
        self.assertEqual(Engine(self.config, driver=second_driver).run(
            self.project, 'worker', event_trigger=True)['status'], 'completed')
        progress = self.project.snapshots.get()['progress']['wait_resolution_tokens']
        self.assertIn(f"{second['id']}:{second['rev']}", progress)
        self.assertNotIn(first_token, progress)

    def test_held_open_visibility_authorization_and_ordinary_release(self):
        item = self.block()
        self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        self.assertEqual(self.store.page_ready(role_name='reviewer')['total'], 0)
        self.assertEqual(self.store.page_ready(role_name='worker')['items'][0]['holder'], 'worker')
        for status in ('open', 'doing', 'blocked', 'dropped'):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.store.transition(item['id'], expected_rev=opened['rev'], status=status,
                    actor='reviewer', reason='Cannot take held work', wait={'kind': 'code_change'} if status == 'blocked' else None)
        doing = self.store.transition(item['id'], expected_rev=opened['rev'], status='doing', actor='worker')
        self.assertNotIn('wait_resolution', doing)
        released = self.store.transition(item['id'], expected_rev=doing['rev'], status='open', actor='worker')
        self.assertIsNone(released['holder'])
        self.assertEqual(self.store.page_ready(role_name='reviewer')['total'], 1)

    def test_holder_can_explicitly_release_held_open_without_a_scope_change(self):
        item = self.block()
        self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        released = self.store.transition(item['id'], expected_rev=opened['rev'], status='open',
                                         actor='worker', reason='Release ownership')
        self.assertIsNone(released['holder'])
        self.assertNotIn('wait_resolution', released)
        self.assertEqual(released['scope'], item['scope'])
        self.assertEqual(self.store.page_ready(role_name='reviewer')['total'], 1)

    def test_reconciliation_bound_and_current_role_only(self):
        items = [self.block(str(n)) for n in range(3)]
        other = self.block('other', actor='reviewer')
        self.snapshot()
        with patch.object(self.store, 'transition', wraps=self.store.transition) as transition:
            self.reconcile(limit=1)
        self.assertEqual(transition.call_count, 1)
        self.assertEqual(sum(self.store.get(i['id'])['status'] == 'open' for i in items), 1)
        self.assertEqual(self.store.get(other['id']), other)

    def assert_pending_prefix_does_not_starve(self, kind):
        item = self.block('resolvable', kind=kind)
        newer = self.snapshot()
        self.receipt(newer)
        self.baseline = newer
        pending = [self.block(f'pending-{n}', kind=(
            'code_change' if n % 2 else 'publication_ci')) for n in range(4)]
        pending_ids = {row['id'] for row in pending}
        # Supersession is not a terminal CI result and is absent from ready pages.
        if kind == 'publication_ci':
            self.assertNotIn(item['id'], {row['id'] for row in self.store.page_ready()['items']})
        scan = self.store._iter_item_events_with_revisions

        def pending_first(**kwargs):
            rows = list(scan(**kwargs))
            rows.sort(key=lambda row: self.store._seed_for_event(
                row[0], row[1], readonly=True)[1]['id'] not in pending_ids)
            return iter(rows)

        with patch.object(self.store, '_iter_item_events_with_revisions', side_effect=pending_first), \
                patch.object(self.store, 'transition', wraps=self.store.transition) as transition:
            page, _ = resolution.reconcile(self.project, 'worker', limit=2)
        self.assertEqual(transition.call_count, 1)
        self.assertEqual(self.store.get(item['id'])['status'], 'open')
        self.assertEqual(len(page['items']), 2)
        for row in pending:
            self.assertEqual(self.store.get(row['id']), row)

    def test_pending_waiters_share_one_listing_per_candidate_digest_per_scan(self):
        items = [self.block(f'pending-{n}', kind='publication_ci') for n in range(5)]
        descendants = [self.snapshot(f'descendant-{n}') for n in range(6)]
        expected = {(items[0]['wait']['branch'], row['code_digest']) for row in descendants}
        # More pending waiters than the page limit: all are scanned, none resolve.
        # A second reconciliation must start with an empty advisory cache.
        for _ in range(2):
            with patch.object(Publication, 'list_receipts',
                              wraps=Publication.list_receipts) as listings:
                self.reconcile(limit=2)
            self.assertEqual(listings.call_count, len(expected))
            self.assertEqual({(call.kwargs['branch'], call.kwargs['code_digest'])
                              for call in listings.call_args_list}, expected)
            for item in items:
                self.assertEqual(self.store.get(item['id']), item)

    def test_observation_reuses_only_two_ancestry_checkers_for_multiple_candidates(self):
        item = self.block(kind='publication_ci')
        descendants = [self.snapshot(f'descendant-{n}') for n in range(4)]
        snapshots = self.project.snapshots
        # No receipts: every candidate goes through discovery, delivery and
        # receipt resolution. A new observation must build fresh checkers.
        for _ in range(2):
            with patch.object(snapshots, '_descendant_distance_from',
                              wraps=snapshots._descendant_distance_from) as checkers, \
                    patch.object(snapshots, 'get', wraps=snapshots.get) as manifests, \
                    patch.object(Publication, 'list_receipts',
                                 wraps=Publication.list_receipts) as listings:
                self.assertIsNone(resolution.observe(self.store, item))
            self.assertEqual(checkers.call_args_list, [
                unittest.mock.call(item['wait']['snapshot_id']),
                unittest.mock.call(item['delivery']['snapshot_id'])])
            self.assertEqual([call.kwargs['code_digest'] for call in listings.call_args_list],
                             [row['code_digest'] for row in reversed(descendants)])
            # Even cached ancestry never substitutes for candidate manifests.
            for row in descendants:
                self.assertGreaterEqual(manifests.call_args_list.count(
                    unittest.mock.call(row['id'])), 2)

    def test_advisory_cache_is_exact_bounded_and_does_not_cache_exceptions(self):
        from mizu.ci_waits import _resolved
        item = self.block(kind='publication_ci')
        newer = self.snapshot()
        observed = {'snapshot_id': newer['id'], 'code_digest': newer['code_digest']}
        cache = {}
        with patch.object(Publication, 'list_receipts', side_effect=Denied('Uncertain evidence')):
            with self.assertRaises(Denied):
                _resolved(self.store, item['wait'], observed, receipt_cache=cache)
        self.assertEqual(cache, {})
        with patch.object(self.store.snapshots, 'history_index', 1), \
                patch.object(Publication, 'list_receipts', wraps=Publication.list_receipts) as listings:
            for _ in range(2):
                self.assertIsNone(_resolved(self.store, item['wait'], observed, receipt_cache=cache))
            self.assertEqual(listings.call_count, 1)
            self.assertEqual(cache, {(item['wait']['branch'], newer['code_digest']): None})
            # Neither a different pinned branch nor a different digest can hit
            # that entry, and a full cache does not grow or authorize anything.
            self.assertIsNone(_resolved(self.store, {**item['wait'], 'branch': 'other'},
                                        observed, receipt_cache=cache))
            self.assertIsNone(_resolved(self.store, item['wait'], receipt_cache=cache))
            self.assertEqual(listings.call_count, 3)
            self.assertEqual(len(cache), 1)

    def test_locked_transition_rejects_new_ambiguity_after_advisory_cache_fill(self):
        item = self.block(kind='publication_ci')
        newer = self.snapshot()
        self.receipt(newer)
        transition = self.store.transition

        def race(item_id, **kwargs):
            self.receipt(newer, source='e' * 40)
            return transition(item_id, **kwargs)

        with patch.object(self.store, 'transition', side_effect=race), \
                patch.object(Publication, 'list_receipts', wraps=Publication.list_receipts) as listings:
            self.reconcile()
        self.assertEqual(listings.call_count, 2)
        self.assertEqual(self.store.get(item['id']), item)

    def test_supersession_lists_only_newest_receipt_per_observation(self):
        item = self.block(kind='publication_ci')
        for n in range(6):
            newest = self.snapshot(f'descendant-{n}')
        receipt = self.receipt(newest)
        with patch.object(Publication, 'list_receipts',
                          wraps=Publication.list_receipts) as listings:
            self.reconcile()
        # Advisory discovery and the locked transition each need one exact
        # listing, not a listing for every older unpublished descendant.
        self.assertEqual(listings.call_count, 2)
        for call in listings.call_args_list:
            self.assertEqual(call.kwargs['code_digest'], newest['code_digest'])
            self.assertEqual(call.kwargs['branch'], item['wait']['branch'])
        opened = self.store.get(item['id'])
        self.assertEqual(opened['status'], 'open')
        self.assertEqual(opened['wait_resolution']['evidence'], {
            'snapshot_id': newest['id'], 'code_digest': newest['code_digest'],
            'publication_id': receipt['publication_id'],
            'receipt_digest': digest(canonical(receipt))})

    def test_pending_prefix_does_not_starve_due_wait(self):
        self.assert_pending_prefix_does_not_starve('code_change')

    def test_pending_prefix_does_not_starve_superseded_wait(self):
        self.assert_pending_prefix_does_not_starve('publication_ci')

    @unittest.skipIf(IS_WINDOWS, 'Projection reuse is disabled on Windows')
    def test_missing_finish_probe_does_not_rotate_for_another_roles_held_open_work(self):
        from mizu.runtime import session_prompt
        item = self.block(actor='reviewer')
        self.snapshot()
        self.reconcile(role='reviewer')
        context = self.context()
        write_json(self.project.root / 'health/worker.json', {'consecutive_missing_finish': 2})
        run_dir = context.run_dir
        with patch('mizu.runtime.prompt_for', return_value='prompt'):
            self.assertIsNone(session_prompt(context, context.role, run_dir)[3])
            opened = self.store.get(item['id'])
            self.store.transition(item['id'], expected_rev=opened['rev'], status='open', actor='reviewer')
            rotation = session_prompt(context, context.role, run_dir)[3]
        self.assertIn('missing-finish', rotation['reason'])

    @unittest.skipIf(IS_WINDOWS, 'Projection reuse is disabled on Windows')
    @unittest.skipIf(IS_WINDOWS, 'Sidecar reuse is disabled on Windows')
    @unittest.skipIf(IS_WINDOWS, 'Projection reuse is disabled on Windows')
    def test_unknown_and_corrupt_snapshot_proof_stay_pending(self):
        item = self.block()
        with patch.object(self.project.snapshots, 'get', side_effect=Denied('Unavailable')):
            self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        snapshot = self.snapshot()
        path = self.project.root / 'snapshots' / (snapshot['id'] + '.json')
        record = read_json(path)
        write_json(path, {**record, 'code_digest': 'e' * 64})
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)

    def test_stale_cas_cannot_append_resolution(self):
        item = self.block()
        self.snapshot()
        transition = self.store.transition
        def race(item_id, **kwargs):
            transition(item_id, expected_rev=item['rev'], status='open', actor='worker', reason='Manual replan')
            return transition(item_id, **kwargs)
        with patch.object(self.store, 'transition', side_effect=race):
            page, _ = resolution.reconcile(self.project, 'worker', limit=20)
        current = self.store.get(item['id'])
        self.assertEqual(current['rev'], item['rev'] + 1)
        self.assertIsNone(current['holder'])
        self.assertNotIn('wait_resolution', current)
        row = next(row for row in page['items'] if row['id'] == item['id'])
        self.assertEqual((row['status'], row['rev'], row['holder']),
                         ('open', current['rev'], None))

    def test_concurrent_terminal_transition_is_not_returned_in_open_projection(self):
        item = self.block()
        self.snapshot()
        transition = self.store.transition

        def race(item_id, **kwargs):
            transition(item_id, expected_rev=item['rev'], status='dropped', actor='worker',
                       reason='Concurrent decision')
            return transition(item_id, **kwargs)

        with patch.object(self.store, 'transition', side_effect=race):
            page, _ = resolution.reconcile(self.project, 'worker', limit=20)
        self.assertEqual(self.store.get(item['id'])['status'], 'dropped')
        self.assertEqual(page['items'], [])

    def test_resolution_history_refuses_tampering_and_cannot_be_carried_forward(self):
        item = self.block()
        self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        path = self.project.root / 'work-items' / item['id'] / f"{opened['rev']}.json"
        for key, bad in [('wait_rev', True), ('wait_digest', 'e' * 64), ('kind', 'terminal'),
                         ('resolved_at', 'not-a-timestamp'),
                         ('resolved_at', '2026-01-01T00:00:00+00:0'),
                         ('resolved_at', '2026-01-01T00:00:00+00:000'), ('evidence', {})]:
            broken = copy.deepcopy(opened)
            broken['wait_resolution'][key] = bad
            write_json(path, broken)
            with self.subTest(key=key), self.assertRaises(Denied):
                Project(self.config, 'sample').work_items.explain(item['id'])
        write_json(path, opened)
        next_event = {**opened, 'rev': opened['rev'] + 1, 'status': 'doing',
                      'transition': {'from': 'open', 'to': 'doing'}}
        with self.assertRaises(Denied):
            self.store._validate_transition(opened, next_event)

    def test_superseded_publication_requires_delivery_descendant_and_exact_pinned_receipt(self):
        item = self.block(kind='publication_ci')
        snapshot = self.snapshot()
        self.receipt(snapshot, branch='wrong-branch')
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        receipt = self.receipt(snapshot)
        with patch('mizu.vcs.read_via', side_effect=AssertionError('No hosted CI request')):
            self.reconcile()
        opened = self.store.get(item['id'])
        self.assertEqual(opened['delivery'], item['delivery'])
        self.assertEqual(opened['wait_resolution']['evidence'], {
            'snapshot_id': snapshot['id'], 'code_digest': snapshot['code_digest'],
            'publication_id': receipt['publication_id'], 'receipt_digest': digest(canonical(receipt))})
        self.assertEqual(opened['wait_resolution']['kind'], 'superseded')
        # Replay refuses a changed exact receipt even after warming projections.
        self.store.page_ready(role_name='worker')
        path = self.project.root / 'evidence/publications' / f"{receipt['publication_id']}.receipt.json"
        raw = path.read_bytes()
        path.write_text('{}')
        forget_ledger(self.project.root)  # Rebuilding validates the pinned receipt again.
        with self.assertRaises(Denied):
            self.store.get(item['id'])
        path.write_bytes(raw)
        # Later ambiguity does not rewrite or invalidate the original immutable observation.
        self.receipt(snapshot, source='e' * 40)
        self.assertEqual(Project(self.config, 'sample').work_items.get(item['id']), opened)

    def test_unknown_newest_ancestry_does_not_fall_back_to_older_receipt(self):
        item = self.block(kind='publication_ci')
        older = self.snapshot('older-published-descendant')
        older_receipt = self.receipt(older)
        newest = self.snapshot('newest-published-descendant')
        self.receipt(newest)
        checker = self.project.snapshots._descendant_distance_from
        for uncertain in ('wait', 'delivery'):
            for error in (Denied, LimitExceeded, OSError, ValueError):
                with self.subTest(ancestry=uncertain, error=error):
                    calls = []

                    def uncertain_checker(ancestor):
                        distance = checker(ancestor)
                        kind = 'wait' if not calls else 'delivery'
                        calls.append(kind)

                        def check(candidate):
                            if kind == uncertain and candidate == newest['id']:
                                raise error('Ancestry is unverifiable')
                            return distance(candidate)

                        return check

                    with patch.object(self.project.snapshots, '_descendant_distance_from',
                                      side_effect=uncertain_checker):
                        self.reconcile()
                    self.assertEqual(self.store.get(item['id']), item)
                    self.assertEqual(calls, ['wait', 'delivery'])
        # The older receipt really could resolve the wait if it were the newest.
        with patch.object(self.project.snapshots, '_history_ids',
                          return_value=[self.baseline['id'], older['id']]):
            self.reconcile()
        self.assertEqual(self.store.get(item['id'])['wait_resolution']['evidence']['publication_id'],
                         older_receipt['publication_id'])

    def test_older_wait_skips_candidates_before_delivery_and_records_exact_supersession(self):
        before_delivery = self.snapshot('before-delivery')
        self.receipt(before_delivery)
        delivery = self.snapshot('delivery')
        item = self.store.create(actor='worker', idempotency_key='older-wait',
                                 title='Wait on older baseline')['record']['work_item']
        item = bind_delivery(self.project, item['id'], delivery['id'])
        item = self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
            actor='worker', wait={'kind': 'publication_ci',
                                 'snapshot_id': self.baseline['id'], 'checks': ['unit']})
        # A newer-than-wait but pre-delivery receipt is not supersession.
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        snapshot = self.snapshot('published-descendant')
        receipt = self.receipt(snapshot)
        self.reconcile()
        opened = self.store.get(item['id'])
        self.assertEqual((opened['status'], opened['holder'], opened['delivery']),
                         ('open', 'worker', item['delivery']))
        self.assertEqual(opened['wait_resolution'], {
            'wait_rev': item['rev'], 'wait_digest': digest(canonical(item['wait'])),
            'kind': 'superseded', 'resolved_at': opened['updated_at'],
            'evidence': {'snapshot_id': snapshot['id'], 'code_digest': snapshot['code_digest'],
                         'publication_id': receipt['publication_id'],
                         'receipt_digest': digest(canonical(receipt))}})
        self.assertEqual(Project(self.config, 'sample').work_items.explain(item['id'])['history'][-1],
                         opened)

    def test_same_tree_missing_delivery_false_descendant_and_unretained_candidate_remain_pending(self):
        item = self.block(kind='publication_ci')
        self.receipt(self.baseline)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        unretained = self.snapshot('unretained', publish=False)
        self.receipt(unretained)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        # A retained-list position is not ancestry; a fork cannot contain a later delivery.
        delivery = self.snapshot('delivery')
        self.baseline = delivery
        second = self.block('later-delivery', kind='publication_ci')
        fork = self.snapshot('fork', parent=item['wait']['snapshot_id'], publish=False)
        self.receipt(fork)
        with patch.object(self.project.snapshots, '_history_ids', return_value=[delivery['id'], fork['id']]):
            self.reconcile()
        self.assertEqual(self.store.get(second['id']), second)
        no_delivery = self.block('no-delivery', kind='publication_ci', delivery=False)
        snapshot = self.snapshot('newer')
        self.receipt(snapshot)
        self.reconcile()
        self.assertEqual(self.store.get(no_delivery['id']), no_delivery)

    def test_ambiguous_corrupt_and_incomplete_receipt_scans_remain_pending(self):
        item = self.block(kind='publication_ci')
        snapshot = self.snapshot()
        receipt = self.receipt(snapshot)
        self.receipt(snapshot, source='e' * 40)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        path = self.project.root / 'evidence/publications' / f"{receipt['publication_id']}.receipt.json"
        path.write_text('{}')
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        with patch.object(Publication, 'list_receipts', return_value={
                'publications': [], 'total': 0, 'truncated': True, 'scan_stopped': True}):
            self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)

    def test_exact_ci_result_and_unrelated_wait_kinds_are_unchanged(self):
        from mizu import ci_waits
        item = self.block(kind='publication_ci')
        self.receipt(self.baseline)
        data = {'checks': [{'check': 'unit', 'sha': 'b' * 40, 'state': 'success'}]}
        ci_waits.poll(self.config, self.project, {}, now=1000, interval=15,
                      reader=lambda *args: data)
        evidence = ci_waits.evidence(self.store, item)
        self.assertIsNotNone(evidence)
        self.reconcile()
        self.assertEqual(self.store.get(item['id']), item)
        self.assertEqual(ci_waits.evidence(self.store, item), evidence)
        deadline = self.store.create(actor='worker', idempotency_key='deadline', title='Deadline')['record']['work_item']
        deadline = self.store.transition(deadline['id'], expected_rev=1, actor='worker', status='blocked',
            wait={'kind': 'deadline', 'at': '2000-01-01T00:00:00+00:00'})
        self.reconcile()
        self.assertEqual(self.store.get(deadline['id']), deadline)

    def test_exhausted_held_open_can_only_be_retired_by_its_holder(self):
        item = self.block()
        # Exact fixture history without quadratic transition API replay.
        for rev in range(item['rev'] + 1, 256):
            item = {**item, 'rev': rev, 'scope': f'Scope {rev}', 'reason': 'Revise scope',
                    'transition': {'from': 'blocked', 'to': 'blocked'}}
            write_json(self.project.root / 'work-items' / item['id'] / f'{rev}.json', item)
        self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        self.assertEqual((opened['rev'], opened['status'], opened['holder']), (256, 'open', 'worker'))
        self.reconcile()
        with self.assertRaises(Denied):
            self.store.transition(item['id'], expected_rev=256, status='dropped', actor='reviewer', reason='Cannot take')
        retired = self.store.transition(item['id'], expected_rev=256, status='dropped', actor='worker', reason='Explicit retirement')
        self.assertNotIn('wait_resolution', retired)
        self.assertEqual(retired['status'], 'dropped')

    def test_resolution_growth_is_exact_and_snapshot_is_a_storage_root(self):
        from mizu.storage import audit
        item = self.block()
        snapshot = self.snapshot()
        self.reconcile()
        opened = self.store.get(item['id'])
        base = {key: value for key, value in opened.items() if key != 'wait_resolution'}
        self.assertLessEqual(len(canonical(opened)) - len(canonical(base)), resolution.MAX_RESOLUTION_BYTES)
        maximum = {'wait_rev': 255, 'wait_digest': 'f' * 64, 'kind': 'superseded',
                   'resolved_at': '9999-12-31T23:59:59+00:00',
                   'evidence': {key: 'f' * 64 for key in
                                ('snapshot_id', 'code_digest', 'publication_id', 'receipt_digest')}}
        self.assertEqual(len(canonical({'a': 1, 'wait_resolution': maximum})) - len(canonical({'a': 1})),
                         resolution.MAX_RESOLUTION_BYTES)
        self.assertEqual(MAX_EVENT_BYTES, PREVIEW_BYTES + resolution.MAX_RESOLUTION_BYTES)
        # A current pointer is not the only reason this exact proof must survive reclamation.
        self.snapshot('later')
        report = audit(self.project)
        self.assertGreaterEqual(report['references']['work_item_snapshots'], 1)
