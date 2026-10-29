"""Configured insight attention uses the existing local exact delivery boundary.

Synthetic drivers only: no model, service manager, network or deployment.
"""
from dataclasses import replace
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import decision_delivery as delivery
from mizu.decision_reconciliation import activate_decision_delivery
from mizu.errors import Denied, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.insights import INSIGHT_DELIVERY_POLICIES
from mizu.runtime import Engine, _role_events_due
from mizu.services import event_watches
from mizu.shared_records import SharedRecords


class InsightRoutingLegacyTests(Fixture):
    shared = False

    def setUp(self):
        super().setUp()
        roles = {name: replace(role, decision_events=(), decision_page_bytes=None)
                 for name, role in self.config.roles.items()}
        roles['reviewer'] = replace(roles['reviewer'], daemon=False, interval_seconds=0,
                                    calendar=(), on_change=False)
        self.config = replace(self.config, roles=roles)
        self.project.config = self.config
        if self.shared:
            activate_decision_delivery(self.project)
        self.marker = (self.project.root / delivery.AUTHORITY_FILE).read_bytes() if self.shared else None

    def submit(self, title='Attention', **metadata):
        return self.project.insights.submit(source=metadata.pop('source', 'searcher'),
            title=title, body='Exact current body', base_snapshot=None,
            **dict(kind='question', addressee='reviewer', urgency='high') | metadata)

    def pending(self, name='reviewer', **kwargs):
        return delivery.pending(self.project, self.config.roles[name], **kwargs)

    def context_with_delivery(self, name='reviewer'):
        ctx = self.context(name)
        delivery.bind(ctx, self.pending(name))
        return ctx

    def acknowledge(self, ctx, tokens):
        ctx._op_finish({'outcome': 'wait', 'summary': 'Handled selected exact events', 'ack_events': tokens})
        # Unit host application boundary (runtime durability tested separately).
        if self.shared:
            delivery.acknowledge(ctx, set(tokens))
        else:
            self.project.insights.acknowledge_decisions(ctx.role.name,
                [e for e in ctx.decision_events if e['ack_token'] in tokens])

    def test_target_operator_writer_and_legacy_omissions(self):
        addressed = self.submit()
        own = self.submit('Self attention', addressee='writer')
        human = self.submit('Human attention', addressee='operator')
        human_own = self.submit('Human writer', source='operator', addressee='writer')
        legacy = self.submit('Q: prose grants no route')
        path = self.project.root / 'inbox' / f"{legacy['id']}.json"
        record = read_json(path)
        for key in ('kind', 'addressee', 'urgency'):
            record.pop(key)
        write_json(path, record)
        self.assertEqual([e['insight'] for e in self.pending()['events']], [addressed['id']])
        self.assertEqual([e['insight'] for e in self.pending('searcher')['events']], [own['id']])
        self.assertEqual(self.pending('worker')['events'], [])
        self.assertEqual(self.pending('reporter')['events'], [])
        row = self.pending()['events'][0]
        self.assertEqual((row['kind'], row['insight_kind'], row['urgency'], row['target']),
                         ('insight.pending', 'question', 'high', 'reviewer'))
        self.assertEqual({human['id'], human_own['id']} & {e['insight'] for e in self.pending()['events']}, set())
        with self.assertRaises(Denied):
            delivery.pending(self.project, replace(self.config.roles['reviewer'], name='operator'))
        if self.shared:
            self.assertEqual((self.project.root / delivery.AUTHORITY_FILE).read_bytes(), self.marker)
        else:
            self.assertFalse((self.project.root / delivery.AUTHORITY_FILE).exists())
            self.assertFalse((self.project.root / 'shared-records').exists())

    def test_exact_selected_target_token_replay_and_no_status_change(self):
        first, second = self.submit('First'), self.submit('Second')
        ctx = self.context_with_delivery()
        events = ctx.decision_events
        self.assertEqual(self.pending()['events'], events)
        bad = self.context_with_delivery('searcher')
        with self.assertRaises(Denied):
            bad._op_finish({'outcome': 'wait', 'summary': 'Foreign', 'ack_events': [events[0]['ack_token']]})
        with self.assertRaises(Denied):
            ctx._op_finish({'outcome': 'wait', 'summary': 'Duplicate',
                            'ack_events': [events[0]['ack_token']] * 2})
        with self.assertRaises(Denied):
            ctx._op_finish({'outcome': 'wait', 'summary': 'Undelivered', 'ack_events': ['0' * 64]})
        self.acknowledge(ctx, [events[0]['ack_token']])
        self.assertEqual(self.pending()['events'], events[1:])
        for item in (first, second):
            self.assertEqual(self.project.insights.read(item['id'])['rev'], 1)
            self.assertFalse((self.project.root / 'decisions' / f"{item['id']}.json").exists())
        self.assertEqual(self.project.work_items.page_open()['total'], 0)

    def test_revision_reroute_retires_old_selection_but_not_frozen_binding(self):
        item = self.submit()
        old = self.context_with_delivery()
        revised = self.project.insights.revise(item['id'], source='searcher', expected_rev=1,
            title=item['title'], body=item['body'], base_snapshot=None, addressee='reporter')
        self.assertEqual(self.pending()['events'], [])
        current = self.pending('reporter')['events'][0]
        self.assertEqual((current['rev'], current['target']), (2, 'reporter'))
        self.assertNotEqual(current['ack_token'], old.decision_events[0]['ack_token'])
        fresh = self.context_with_delivery('reporter')
        with self.assertRaises(Denied):
            fresh._op_finish({'outcome': 'wait', 'summary': 'Stale route',
                              'ack_events': [old.decision_events[0]['ack_token']]})
        self.acknowledge(old, [old.decision_events[0]['ack_token']])
        self.assertEqual(self.pending('reporter')['events'], [current])
        self.assertEqual(revised['rev'], 2)

    def test_unknown_removed_and_malformed_destinations_remain_unresolved(self):
        item = self.submit(addressee='missing')  # core stores syntax; host delivery validates membership
        # Isolated bad route: no delivery, no wake, no fallback, still readable.
        self.assertEqual(self.pending()['events'], [])
        self.assertEqual(self.pending('searcher')['events'], [])
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        self.assertEqual(self.project.insights.read(item['id'])['addressee'], 'missing')
        self.project.insights.revise(item['id'], source='searcher', expected_rev=1,
            title=item['title'], body=item['body'], base_snapshot=None, addressee='reviewer')
        self.assertEqual(len(self.pending()['events']), 1)
        self.project.roles = [name for name in self.project.roles if name != 'reviewer']
        # Removed destination stays unresolved without blocking other roles.
        self.assertEqual(self.pending('searcher')['events'], [])
        path = self.project.root / 'inbox' / f"{item['id']}.json"
        record = read_json(path)
        record['addressee'] = '../escape'
        write_json(path, record)
        with self.assertRaises(Denied):
            self.pending('searcher')

    def test_submit_and_revise_refuse_non_project_role_targets(self):
        from mizu.insight_tools import submit_insight, revise_insight
        ctx = self.context('searcher')
        self.project.roles = [name for name in self.project.roles if name != 'reporter']
        args = {'title': 'Invalid route', 'body': 'No arbitrary role', 'addressee': 'reporter'}
        with self.assertRaisesRegex(Denied, 'configured role'):
            submit_insight(ctx, args)
        item = self.submit()
        with self.assertRaisesRegex(Denied, 'configured role'):
            revise_insight(ctx, dict(args, id=item['id'], rev=1))
        self.assertEqual(self.project.insights.read(item['id'])['rev'], 1)

    def test_retry_and_evidence_do_not_replace_revision_bound_token(self):
        item = self.submit()
        old = self.context_with_delivery()
        token = old.decision_events[0]['ack_token']
        self.assertEqual(self.submit()['rev'], 1)
        self.project.insights.attach_evidence(item['id'], source='worker', text='New evidence note',
                                             run='evidence', expected_rev=1)
        self.assertEqual(self.pending()['events'][0]['ack_token'], token)
        self.acknowledge(old, [token])
        self.project.insights.revise(item['id'], source='searcher', expected_rev=1,
            title=item['title'], body='Meaningfully improved', base_snapshot=None)
        current = self.pending()['events'][0]
        self.assertEqual(current['rev'], 2)
        self.assertNotEqual(current['ack_token'], token)

    def test_corrupt_and_unsafe_receipts_never_suppress_as_handled(self):
        self.submit()
        state = self.pending()
        event = state['events'][0]
        if self.shared:
            store = SharedRecords(self.project.root, INSIGHT_DELIVERY_POLICIES)
            path = self.project.root / store._receipt_name('reviewer', state['manifest'][0].record_id)
        else:
            path = self.project.root / 'decision-receipts' / 'reviewer' / f"{event['ack_token']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{invalid')
        if self.shared:
            with self.assertRaises(Denied):
                self.pending()
        else:
            pending = self.pending()
            self.assertEqual(pending['events'], [])
            self.assertEqual(pending['diagnostics'][0]['ack_token'], event['ack_token'])
            self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        self.assertEqual(path.read_bytes(), b'{invalid')
        path.unlink()
        with patch('mizu.insights.safe_read', side_effect=Denied('unsafe read')):
            with self.assertRaises(Denied):
                self.pending()

    def test_event_dispatch_replay_explicit_receipt_and_unchanged_policy(self):
        role = self.config.roles['reviewer']
        self.assertEqual(role.decision_events, ())
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        # Settle initial operator wake; no implicit insight decision grant.
        engine.run(self.project, 'reviewer')
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True).get('skipped'), 'no-event')
        item = self.submit()
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        self.assertEqual(engine.run(self.project, 'reviewer', event_trigger=True)['status'], 'completed')
        self.assertEqual(json.loads(driver.calls[-1][1])['decision_events'][0]['insight'], item['id'])
        self.assertEqual(len(self.pending()['events']), 1)  # finish omission replays
        def handle(ctx, prompt, *_):
            token = json.loads(prompt)['decision_events'][0]['ack_token']
            ctx.handle('finish', {'outcome': 'wait', 'summary': 'Handled attention', 'ack_events': [token]})
        result = Engine(self.config, driver=ScriptDriver(handle)).run(self.project, 'reviewer', event_trigger=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.pending()['events'], [])
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        self.assertEqual(self.config.roles['reviewer'], role)
        self.assertNotIn('decide_insight', driver.calls[-1][0].role.capabilities)

    def test_bounded_count_and_existing_manifest_reader(self):
        for i in range(3):
            self.submit(str(i))
        state = self.pending(limit=1)
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(state['events'], self.pending(limit=1)['events'])
        if self.shared:
            self.assertEqual(state['page']['pending_count'], 3)
            self.assertTrue(state['page']['count_truncated'])
            ctx = self.context_with_delivery()
            token = ctx.decision_events[0]['ack_token']
            envelope = delivery.read(ctx, {'ack_token': token})
            self.assertIn('insight.pending', str(envelope))
        else:
            self.assertEqual(state['manifest'], ())
        with self.assertRaises(Denied):
            self.pending(limit=101)

    def test_decisions_keep_subscriptions_and_deferral_admission_unchanged(self):
        item = self.submit()
        self.project.insights.decide(item['id'], 'defer', 'Need evidence', 'Observe later', 'test', expected_rev=1)
        self.assertEqual(self.pending()['events'], [])
        self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        self.project.insights.revise(item['id'], source='searcher', expected_rev=1,
            title=item['title'], body='New evidence', base_snapshot=None)
        self.assertEqual(self.pending()['events'][0]['rev'], 2)

    def test_failed_finish_and_writer_publication_do_not_ack_early(self):
        self.submit(addressee='worker')
        delivered = []
        def handle(ctx, prompt, *_):
            row = json.loads(prompt)['decision_events'][0]
            delivered.append(row['ack_token'])
            ctx.handle('finish', {'outcome': 'wait', 'summary': 'Handled but not yet published',
                                  'state': 'Next step is explicit.', 'ack_events': [row['ack_token']]})
            self.assertEqual(self.pending('worker')['events'][0]['ack_token'], row['ack_token'])
        # A publication failure cannot apply an insight receipt.
        with patch.object(self.project.snapshots, 'publish', side_effect=Denied('publication refused')):
            with self.assertRaisesRegex(Denied, 'publication refused'):
                Engine(self.config, driver=ScriptDriver(handle)).run(self.project, 'worker')
        self.assertEqual(self.pending('worker')['events'][0]['ack_token'], delivered[0])
        result = Engine(self.config, driver=ScriptDriver(handle)).run(self.project, 'worker')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.pending('worker')['events'], [])

    def test_urgency_is_not_ordering_or_an_extra_grant(self):
        with patch('mizu.insights.now', return_value='2026-01-01T00:00:00+00:00'):
            first = self.submit('Low first', urgency='low')
        with patch('mizu.insights.now', return_value='2026-01-02T00:00:00+00:00'):
            second = self.submit('High later', urgency='high')
        ctx = self.context_with_delivery()
        self.assertEqual([e['insight'] for e in ctx.decision_events], [first['id'], second['id']])
        extra = set(ctx.role.capabilities) - set(self.config.roles['reviewer'].capabilities)
        self.assertEqual(extra, {'decision_read'} if self.shared else set())

    def test_inbox_watch_uses_existing_coarse_event_dispatch(self):
        watches = event_watches(self.config, self.project)
        for name, role in self.config.roles.items():
            if name not in self.project.roles or role.daemon:
                continue
            self.assertIn(str(self.project.root / 'inbox'), watches[name])
        self.assertEqual(watches['consult'], [str(self.project.root / 'inbox')])
        self.assertNotIn('operator', watches)

    def test_irrelevant_retained_inbox_does_not_block_routed_attention(self):
        from mizu.fs import write_json
        base = "2026-01-01T00:00:00+00:00"
        for i in range(513):
            iid = f"{i:032x}"
            record = {"id": iid, "source": "searcher", "title": f"Human item {i}",
                      "body": "Exact current body", "base_snapshot": None, "run": None,
                      "origin": None, "kind": "question", "addressee": "operator",
                      "urgency": "high", "created_at": base, "rev": 1,
                      "evidence": [], "updated_at": base}
            write_json(self.project.root / "inbox" / f"{iid}.json", record, exclusive=True)
        self.assertEqual(self.pending(limit=100)['events'], [])
        # The role still finds a matching source after unrelated retained items.
        routed = self.submit('Routed past human history')
        state = self.pending(limit=100)
        self.assertEqual([e['insight'] for e in state['events']], [routed['id']])
        if self.shared:
            self.assertEqual(state['page']['pending_count'], 1)
        else:
            self.assertEqual(state['manifest'], ())

    def test_combined_payload_bounded_to_max_frame_oldest_first(self):
        import json
        from mizu.fs import MAX_FRAME
        if self.shared:
            for i in range(10):
                self.project.insights.submit(source='searcher', title=f'Large {i}',
                    body='S' * 110000, base_snapshot=None,
                    kind='question', addressee='reviewer', urgency='high')
            budget = 1000
            role = replace(self.config.roles['reviewer'], decision_page_bytes=budget)
            state = delivery.pending(self.project, role)
            events, page = state['events'], state['page']
            self.assertLess(len(events), 10)
            self.assertGreater(len(events), 0)
            self.assertTrue(page['byte_truncated'])
            self.assertTrue(page['truncated'])
            self.assertIn('ref', events[0])
            fields = {'decision_events': events,
                      'decision_page': {k: v for k, v in page.items() if k != 'events'}}
            self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), budget)
            self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), MAX_FRAME)
            # Stable manifest tokens; omitted tail stays pending and replays.
            self.assertEqual(events, delivery.pending(self.project, role)['events'])
            # Ack the delivered prefix; the omitted tail must replay.
            ctx = self.context('reviewer')
            delivery.bind(ctx, state)
            ctx._op_finish({'outcome': 'wait', 'summary': 'Handled prefix', 'ack_events': [e['ack_token'] for e in events]})
            delivery.acknowledge(ctx, {e['ack_token'] for e in events})
            self.assertGreater(len(delivery.pending(self.project, role)['events']), 0)
            return
        # Legacy: preserve the small decision while truncating newest attention.
        reviewer = replace(self.config.roles['reviewer'], decision_events=('accept',))
        self.config = replace(self.config, roles={**self.config.roles, 'reviewer': reviewer})
        self.project.config = self.config
        decided = self.project.insights.submit(source='reviewer', title='Decided',
            body='Small decision body', base_snapshot=None,
            kind='proposal', addressee='writer', urgency='normal')
        self.project.insights.decide(decided['id'], 'accept', 'Good', '', 'test', expected_rev=1)
        for i in range(10):
            self.project.insights.submit(source='searcher', title=f'Large {i}',
                body='L' * 120000, base_snapshot=None,
                kind='question', addressee='reviewer', urgency='high')
        state = delivery.pending(self.project, self.config.roles['reviewer'])
        events, page = state['events'], state['page']
        decisions = [e for e in events if e.get('action') == 'accept']
        attentions = [e for e in events if e.get('kind') == 'insight.pending']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['insight'], decided['id'])
        self.assertLess(len(attentions), 10)
        self.assertIsNotNone(page)
        self.assertTrue(page['byte_truncated'])
        self.assertTrue(page['truncated'])
        fields = {'decision_events': events,
                  'decision_page': {k: v for k, v in page.items() if k != 'events'}}
        self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), MAX_FRAME)
        # Oldest attention prefix kept with exact stable tokens.
        self.assertEqual([e['created_at'] for e in attentions],
                         sorted(e['created_at'] for e in attentions))
        self.assertEqual(events, delivery.pending(self.project, self.config.roles['reviewer'])['events'])
        # Omitted attention rows remain pending and replay after acking the prefix.
        before_ids = {e['insight'] for e in attentions}
        ctx = self.context('reviewer')
        delivery.bind(ctx, state)
        from mizu.unit_prompts import _decision_task
        self.assertIn('decision_page marks omitted newest attention rows', _decision_task(ctx))
        tokens = [e['ack_token'] for e in events]
        ctx._op_finish({'outcome': 'wait', 'summary': 'Handled prefix', 'ack_events': tokens})
        self.project.insights.acknowledge_decisions('reviewer', events)
        after = delivery.pending(self.project, self.config.roles['reviewer'])['events']
        self.assertGreater(len(after), 0)
        self.assertTrue({e['insight'] for e in after} - before_ids)

    def test_legacy_decision_only_over_bound_omits_all_attention_without_new_failure(self):
        # Legacy decision delivery had no byte bound; attention must not newly
        # block it. Enough large accepted decisions that the decision-only
        # payload itself exceeds MAX_FRAME, plus one pending attention row.
        if self.shared:
            self.skipTest("legacy-only transport bound")
        import json
        reviewer = replace(self.config.roles['reviewer'], decision_events=('accept',))
        self.config = replace(self.config, roles={**self.config.roles, 'reviewer': reviewer})
        self.project.config = self.config
        for i in range(10):
            item = self.project.insights.submit(source='reviewer', title=f'Decided {i}',
                body='D' * 110000, base_snapshot=None,
                kind='proposal', addressee='writer', urgency='normal')
            self.project.insights.decide(item['id'], 'accept', 'Good reason', '', 'test', expected_rev=1)
        attention = self.submit('Pending attention')
        # The pre-existing decision-only path stays deliverable.
        decision_only = delivery.pending(self.project, self.config.roles['reviewer'],
                                         limit=100, include_insights=False)
        self.assertEqual(len(decision_only['events']), 10)
        self.assertIsNone(decision_only['page'])
        # With attention present the same decision rows stay available; no new
        # LimitExceeded is raised because of insight attention. All attention
        # is omitted here and stays pending.
        state = delivery.pending(self.project, self.config.roles['reviewer'], limit=100)
        events, page = state['events'], state['page']
        decisions = [e for e in events if e.get('action') == 'accept']
        attentions = [e for e in events if e.get('kind') == 'insight.pending']
        self.assertEqual(len(decisions), 10)
        self.assertEqual([e['event'] for e in decisions],
                         [e['event'] for e in decision_only['events']])
        self.assertEqual([e['ack_token'] for e in decisions],
                         [e['ack_token'] for e in decision_only['events']])
        self.assertEqual(attentions, [])
        self.assertIsNotNone(page)
        self.assertTrue(page['byte_truncated'])
        pending, _ = self.project.insights.pending_insight_events(
            'reviewer', set(self.project.roles) & set(self.config.roles.keys()), limit=100)
        self.assertEqual([e['insight'] for e in pending], [attention['id']])

    def test_legacy_page_marks_unknown_count_when_source_capped(self):
        # Two individually limited candidate lists can hide rows behind a
        # source cap: exactly `limit` rows may mean more. Never present that
        # incomplete total as exact.
        if self.shared:
            self.skipTest("legacy-only count summary")
        import json
        from mizu.fs import MAX_FRAME
        reviewer = replace(self.config.roles['reviewer'], decision_events=('accept',))
        self.config = replace(self.config, roles={**self.config.roles, 'reviewer': reviewer})
        self.project.config = self.config
        for i in range(11):
            self.project.insights.submit(source='searcher', title=f'Large {i}',
                body='L' * 110000, base_snapshot=None,
                kind='question', addressee='reviewer', urgency='high')
        # Capped source (exactly `limit` rows, true total larger): count is
        # unavailable, truncation is conservative, output stays bounded.
        capped = delivery.pending(self.project, self.config.roles['reviewer'], limit=10)
        self.assertIsNotNone(capped['page'])
        self.assertIsNone(capped['page']['pending_count'])
        self.assertTrue(capped['page']['count_truncated'])
        self.assertTrue(capped['page']['truncated'])
        self.assertTrue(capped['page']['byte_truncated'])
        fields = {'decision_events': capped['events'],
                  'decision_page': {k: v for k, v in capped['page'].items() if k != 'events'}}
        self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), MAX_FRAME)
        # Same rows without a source cap: the exact total is retained.
        exact = delivery.pending(self.project, self.config.roles['reviewer'], limit=100)
        self.assertIsNotNone(exact['page'])
        self.assertEqual(exact['page']['pending_count'], 11)
        self.assertFalse(exact['page']['count_truncated'])
        self.assertTrue(exact['page']['truncated'])
        self.assertTrue(exact['page']['byte_truncated'])
        fields = {'decision_events': exact['events'],
                  'decision_page': {k: v for k, v in exact['page'].items() if k != 'events'}}
        self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), MAX_FRAME)

    def test_unconfigured_addressee_isolated_for_other_recipients(self):
        bad = self.submit('Bad route', addressee='missing')
        good = self.submit('Good route', addressee='reviewer')
        other = self.submit('Other route', addressee='reporter')
        # Valid routes deliver despite the bad route; bad never falls back.
        reviewer_events = self.pending()['events']
        self.assertEqual([e['insight'] for e in reviewer_events], [good['id']])
        self.assertEqual([e['insight'] for e in self.pending('reporter')['events']], [other['id']])
        self.assertEqual(self.pending('searcher')['events'], [])
        self.assertEqual(self.pending('worker')['events'], [])
        for name in ('reviewer', 'reporter', 'searcher', 'worker'):
            for e in self.pending(name)['events']:
                self.assertNotEqual(e['insight'], bad['id'])
        # Bad stays visibly unresolved in the host inbox, never delivered.
        self.assertEqual(self.project.insights.read(bad['id'])['addressee'], 'missing')
        # Event-trigger admission wakes only for the addressed recipients.
        snap = self.project.snapshots.get()
        self.assertTrue(_role_events_due(self.project, snap, 'reviewer'))
        self.assertTrue(_role_events_due(self.project, snap, 'reporter'))
        self.assertFalse(_role_events_due(self.project, snap, 'searcher'))
        # Repairing the bad route restores its delivery without touching others.
        self.project.insights.revise(bad['id'], source='searcher', expected_rev=1,
            title=bad['title'], body=bad['body'], base_snapshot=None, addressee='reviewer')
        self.assertEqual({e['insight'] for e in self.pending()['events']}, {good['id'], bad['id']})


class InsightRoutingSharedTests(InsightRoutingLegacyTests):
    shared = True

    def test_byte_budget_references_use_only_frozen_manifest(self):
        item = self.submit()
        self.project.insights.revise(item['id'], source='searcher', expected_rev=1,
            title=item['title'], body='x' * 10000, base_snapshot=None)
        role = replace(self.config.roles['reviewer'], decision_page_bytes=1000)
        state = delivery.pending(self.project, role)
        self.assertIn('ref', state['events'][0])
        fields = {'decision_events': state['events'],
                  'decision_page': {k: v for k, v in state['page'].items() if k != 'events'}}
        self.assertLessEqual(len(json.dumps(fields, ensure_ascii=True).encode()), 1000)
        ctx = self.context('reviewer')
        delivery.bind(ctx, state)
        self.assertIn('decision_read', ctx.role.capabilities)
        token = state['events'][0]['ack_token']
        self.project.insights.revise(item['id'], source='searcher', expected_rev=2,
            title=item['title'], body='New route', base_snapshot=None, addressee='reporter')
        self.assertIn('x' * 100, str(delivery.read(ctx, {'ack_token': token})))
        with self.assertRaises(Denied):
            delivery.read(self.context_with_delivery('reporter'), {'ack_token': token})
        with self.assertRaises(LimitExceeded):
            delivery.pending(self.project, replace(role, decision_page_bytes=1))

    def test_acknowledged_shared_history_does_not_consume_live_projection_bound(self):
        from dataclasses import asdict
        from mizu.smoke_refs import record_lock

        from mizu.fs import write_json
        base = "2026-01-01T00:00:00+00:00"
        for i in range(513):
            iid = f"{i:032x}"
            record = {"id": iid, "source": "searcher", "title": f"Previously acknowledged {i}",
                      "body": "Exact current body", "base_snapshot": None, "run": None,
                      "origin": None, "kind": "question", "addressee": "reviewer",
                      "urgency": "high", "created_at": base, "rev": 1,
                      "evidence": [], "updated_at": base}
            write_json(self.project.root / "inbox" / f"{iid}.json", record, exclusive=True)
        configured = set(self.project.roles) & self.config.roles.keys()
        with record_lock(self.project.root):
            envelopes = [envelope for _, envelope in
                         self.project.insights._pending_insight_envelopes_locked(
                             configured, 'reviewer')]
        self.assertEqual(len(envelopes), 513)
        store = SharedRecords(self.project.root, INSIGHT_DELIVERY_POLICIES)
        for start in range(0, len(envelopes), 128):
            batch = envelopes[start:start + 128]
            for envelope in batch:
                store.put(envelope, writer='host.insights')
            manifest = store.freeze('reviewer', [envelope['id'] for envelope in batch])
            store.acknowledge('reviewer', [asdict(entry) for entry in manifest],
                              manifest=manifest)

        newest = self.submit('New attention after acknowledged history')
        state = self.pending(limit=100)
        self.assertEqual([event['insight'] for event in state['events']], [newest['id']])
        self.assertEqual(state['page']['pending_count'], 1)
