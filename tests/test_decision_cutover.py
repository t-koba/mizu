"""Real local cutover/consumer protocol, synthetic model drivers, no provider CI."""
from dataclasses import replace
import json
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import decision_delivery as delivery
from mizu import decision_reconciliation as reconciliation
from mizu.admission import role_exclusion
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied, LimitExceeded
from mizu.fs import canonical, digest, held, lock, read_json
from mizu.insights import Insights, INSIGHT_DECISION_POLICY, INSIGHT_PENDING_POLICY
from mizu.project import Project
from mizu.runtime import Engine, _role_events_due, prompt_for
from mizu.selection import work_inputs
from mizu.shared_records import SharedRecords
from mizu.storage import MEMBERS, backup, restore
from mizu.unit_prompts import prompt_delta_for


class DecisionCutoverTests(Fixture):
    def setUp(self):
        super().setUp()
        roles = {name: replace(role, decision_events=(), decision_page_bytes=None)
                 for name, role in self.config.roles.items()}
        roles['reviewer'] = replace(roles['reviewer'], decision_events=('reject', 'defer'),
                                    decision_page_bytes=8192, decision_events_limit=2)
        roles['worker'] = replace(roles['worker'], decision_events=('reject', 'defer'),
                                  decision_sources='any', decision_page_bytes=8192)
        self.config = replace(self.config, roles=roles)
        self.project.config = self.config

    def role(self, name='reviewer', **changes):
        role = replace(self.config.roles[name], **changes)
        self.config = replace(self.config, roles={**self.config.roles, name: role})
        self.project.config = self.config
        return role

    def decide(self, index=0, *, source='reviewer', action='reject', body='Exact body'):
        item = self.project.insights.submit(source=source, title=f'Finding {index}', body=body,
                                            base_snapshot=None)
        self.project.insights.decide(item['id'], action, 'Exact reason', 'Revisit condition', 'test', expected_rev=1)
        return item

    def pending(self, name='reviewer', **kwargs):
        return delivery.pending(self.project, self.config.roles[name], **kwargs)

    def activate(self):
        return reconciliation.activate_decision_delivery(self.project)

    def receipts(self, family):
        return {p.relative_to(self.project.root).as_posix(): p.read_bytes()
                for p in (self.project.root / family).glob('*/*.json')}

    def context_with_delivery(self, name='reviewer'):
        ctx = self.context(name)
        delivery.bind(ctx, self.pending(name))
        return ctx

    def test_legacy_default_ignores_even_unexplained_shadow_receipts(self):
        self.decide()
        before = self.pending()
        page, manifest = self.project.insights.shared_decision_page(
            'reviewer', ('reject',), max_bytes=1600)
        store = SharedRecords(self.project.root, {'insight.decision': INSIGHT_DECISION_POLICY})
        from dataclasses import asdict
        store.acknowledge('reviewer', [asdict(manifest[0])], manifest=manifest)
        with patch.object(self.project.insights, 'shared_decision_page', side_effect=AssertionError('dual read')):
            self.assertFalse(self.pending()['shared'])
            self.assertEqual(self.pending()['events'], before['events'])
            self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        ctx = self.context_with_delivery()
        self.assertNotIn('decision_read', ctx.role.capabilities)
        with self.assertRaisesRegex(Denied, 'exceed exact legacy'):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))

    def test_full_parity_marker_last_retry_restart_and_legacy_preserved(self):
        for i in range(8):
            self.decide(i, source='reviewer' if i % 2 else 'searcher')
        legacy = self.project.insights.decision_events('worker', ('reject',), limit=100, sources='any')
        self.project.insights.acknowledge_decisions('worker', legacy[::2], sources='any')
        before = self.receipts('decision-receipts')
        original = reconciliation.atomic_write
        def write(path, raw, **kwargs):
            if path.name == delivery.AUTHORITY_FILE:
                self.assertIsNone(delivery.authority(self.project.root))
                report = read_json(self.project.root / delivery.REPORT_FILE)
                for row in report['roles'].values():
                    self.assertEqual(row['legacy_pending_ids'], row['shared_pending_ids'])
                self.assertEqual(len(report['roles']['worker']['current_ids']), 8)
                for name in self.project.roles:
                    self.assertTrue(held(role_exclusion(self.project.root, name)))
                    self.assertTrue(held(self.project.root / 'locks' /
                                         f'decision-receipts-{digest(name.encode())}.lock'))
                self.assertTrue(held(self.project.root / 'locks/insights.lock'))
            return original(path, raw, **kwargs)
        with patch.object(reconciliation, 'atomic_write', side_effect=write):
            marker = self.activate()
        self.assertEqual(self.receipts('decision-receipts'), before)
        self.assertEqual(len(self.pending('worker', limit=100)['events']), 4)
        self.assertEqual(len(self.pending(limit=100)['events']), 4)
        restarted = Project(self.config, 'sample')
        self.assertEqual(reconciliation.activate_decision_delivery(restarted), marker)
        with self.assertRaises(Denied):
            restarted.insights.acknowledge_decisions('worker', legacy[:1], sources='any')
        with self.assertRaises(Denied):
            reconciliation.preflight_decision_receipts(restarted.insights, {}, enabled_roles=[])

    def test_crash_before_marker_keeps_legacy_and_exact_prefix_retry(self):
        for i in range(3):
            self.decide(i)
        rows = self.project.insights.decision_events('reviewer', ('reject',), limit=100)
        self.project.insights.acknowledge_decisions('reviewer', rows[:2])
        original = SharedRecords.acknowledge
        def interrupt(store, role, selections=None, *, manifest):
            original(store, role, selections[:1], manifest=manifest)
            raise OSError('interrupted receipt prefix')
        with patch.object(SharedRecords, 'acknowledge', interrupt), self.assertRaises(OSError):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        self.assertFalse(self.pending()['shared'])
        prefix = self.receipts('shared-record-receipts')
        self.assertEqual(len(prefix), 1)
        self.project.insights = Insights(self.project.root)
        self.activate()
        self.assertTrue(all(self.receipts('shared-record-receipts')[p] == raw for p, raw in prefix.items()))
        self.assertEqual(len(self.pending()['events']), 1)

    def test_crash_at_marker_is_retryable_without_rewriting_receipts(self):
        self.decide()
        self.project.insights.acknowledge_decisions('reviewer', self.pending()['events'])
        original = reconciliation.atomic_write
        def interrupt(path, raw, **kwargs):
            if path.name == delivery.AUTHORITY_FILE:
                raise OSError('marker not published')
            return original(path, raw, **kwargs)
        with patch.object(reconciliation, 'atomic_write', side_effect=interrupt), self.assertRaises(OSError):
            self.activate()
        prefix = self.receipts('shared-record-receipts')
        self.assertFalse(self.pending()['shared'])
        self.activate()
        self.assertEqual(self.receipts('shared-record-receipts'), prefix)
        self.assertEqual(self.pending()['events'], [])

    def test_interrupted_plan_refuses_source_route_and_receipt_movement(self):
        item = self.decide()
        original = reconciliation.atomic_write
        def interrupt(path, raw, **kwargs):
            if path.name == delivery.AUTHORITY_FILE:
                raise OSError('crash')
            return original(path, raw, **kwargs)
        with patch.object(reconciliation, 'atomic_write', side_effect=interrupt), self.assertRaises(OSError):
            self.activate()
        # No receipts were acknowledged, so even an empty mapped prefix pins
        # source and route identity. A fresh process must not re-plan silently.
        self.role(decision_sources='any')
        with self.assertRaisesRegex(Denied, 'boundary changed'):
            self.activate()
        self.role(decision_sources='own')
        self.project.insights.acknowledge_decisions('reviewer', self.pending()['events'])
        with self.assertRaisesRegex(Denied, 'boundary changed'):
            self.activate()
        self.project.insights.revise(item['id'], source='reviewer', title='Moved', body='new',
                                     expected_rev=1, base_snapshot=None)
        with self.assertRaisesRegex(Denied, 'boundary changed'):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))

    def test_pending_parity_failure_never_publishes_marker(self):
        self.decide()
        scan = reconciliation._scan
        calls = 0
        def mismatch(*args):
            nonlocal calls
            result = scan(*args)
            calls += 1
            if calls == 3:  # complete post-apply scan, not a prompt page
                result[1]['reviewer']['shared_pending_ids'] = []
            return result
        with patch.object(reconciliation, '_scan', side_effect=mismatch), self.assertRaisesRegex(Denied, 'pending-set mismatch'):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        self.assertFalse((self.project.root / delivery.REPORT_FILE).exists())
        self.assertFalse(self.pending()['shared'])
        self.activate()

    def test_interrupted_report_corruption_and_atomic_residue(self):
        self.decide()
        self.project.insights.acknowledge_decisions('reviewer', self.pending()['events'])
        original = reconciliation.atomic_write
        def interrupt(path, raw, **kwargs):
            if path.name == delivery.AUTHORITY_FILE: raise OSError('crash')
            return original(path, raw, **kwargs)
        with patch.object(reconciliation, 'atomic_write', side_effect=interrupt), self.assertRaises(OSError):
            self.activate()
        directory = next((self.project.root / 'shared-record-receipts').iterdir())
        temporary = directory / '.new-abcdefgh'
        temporary.write_bytes(b'partial atomic receipt write')
        report = self.project.root / delivery.REPORT_FILE
        original_report = report.read_bytes()
        report.write_bytes(b'corrupt evidence')
        with self.assertRaisesRegex(Denied, 'report changed'):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        report.write_bytes(original_report)
        self.activate()  # safe crash residue is ignored, not interpreted/repaired
        self.assertTrue(temporary.exists())

    def test_every_enabled_role_exclusion_is_required_before_any_write(self):
        self.decide()
        for name in self.project.roles:
            with self.subTest(role=name), lock(role_exclusion(self.project.root, name), blocking=False):
                with self.assertRaises(Busy):
                    self.activate()
            self.assertFalse((self.project.root / delivery.PLAN_FILE).exists())
        self.activate()

    def test_marker_and_report_corruption_never_fall_back(self):
        self.decide()
        marker = self.activate()
        path = self.project.root / delivery.AUTHORITY_FILE
        good = path.read_bytes()
        report_path = self.project.root / delivery.REPORT_FILE
        report = report_path.read_bytes()
        for raw in (b'bad json', canonical(dict(marker, authority='legacy')),
                    canonical(dict(marker, plan_digest='0' * 64)), canonical(dict(marker, report_digest='0' * 64))):
            path.write_bytes(raw)
            with self.subTest(raw=raw), patch.object(self.project.insights, 'decision_events_with_diagnostics',
                                                   side_effect=AssertionError('fallback')):
                with self.assertRaises(Denied): self.pending()
                with self.assertRaises(Denied): self.activate()
                with self.assertRaises(Denied): _role_events_due(self.project, self.project.snapshots.get(), 'reviewer')
        path.write_bytes(good)
        report_path.write_bytes(report + b' ')
        with self.assertRaises(Denied): self.pending()
        report_path.write_bytes(report)
        path.unlink()
        path.symlink_to(report_path)
        with self.assertRaises(Denied): self.pending()

    def test_unexplained_receipt_for_removed_recipient_refuses_activation(self):
        self.decide()
        _, manifest = self.project.insights.shared_decision_page('retired', ('reject',), sources='any', max_bytes=1600)
        from dataclasses import asdict
        store = SharedRecords(self.project.root, {'insight.decision': INSIGHT_DECISION_POLICY})
        store.acknowledge('retired', [asdict(manifest[0])], manifest=manifest)
        with self.assertRaisesRegex(Denied, 'Unexplained shared-only'):
            self.activate()

    def test_shared_dispatch_wake_selection_and_finish_never_touch_legacy(self):
        self.decide(source='searcher')
        self.activate()
        bounds = {'max_proposals': 10, 'max_decisions': 10, 'max_text_bytes': 1024}
        with patch.object(self.project.insights, 'decision_events', side_effect=AssertionError('legacy selection')), \
             patch.object(self.project.insights, 'decision_events_with_diagnostics', side_effect=AssertionError('legacy prompt/wake')), \
             patch.object(self.project.insights, 'acknowledge_decisions', side_effect=AssertionError('legacy ack')):
            self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'worker'))
            self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
            evidence = work_inputs(self.project, self.config.roles['worker'], self.project.snapshots.get(), bounds)
            self.assertEqual(evidence['evidence']['decisions'][0]['reason'], 'Exact reason')
            zero = work_inputs(self.project, self.config.roles['worker'], self.project.snapshots.get(),
                               dict(bounds, max_decisions=0))
            self.assertEqual(zero['evidence']['decisions'], [])
            self.assertEqual(work_inputs(self.project, self.config.roles['consult'],
                                        self.project.snapshots.get(), bounds)['evidence']['decisions'], [])
            def finish(ctx, prompt, *_):
                doc = json.loads(prompt)
                self.assertEqual(doc['decision_page']['pending_count'], 1)
                self.assertIn('mizu_decision_read', doc['unit_tool_surface']['tools'])
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Handled exact decision', 'state': 'Handled the decision.',
                                      'ack_events': [doc['decision_events'][0]['ack_token']]})
            result = Engine(self.config, driver=ScriptDriver(finish)).run(self.project, 'worker')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(self.pending('worker')['events'], [])
            self.assertFalse(_role_events_due(self.project, self.project.snapshots.get(), 'worker'))
        self.assertEqual(self.receipts('decision-receipts'), {})
        self.assertTrue((self.project.root / 'runs' / result['run'] / 'decision-delivery.json').exists())

    def test_partial_omitted_cross_role_ack_and_source_movement(self):
        items = [self.decide(i) for i in range(3)]
        self.activate()
        def omitted(ctx, *_):
            ctx.handle('finish', {'outcome': 'wait', 'summary': 'No handling yet'})
        Engine(self.config, driver=ScriptDriver(omitted)).run(self.project, 'reviewer')
        self.assertEqual(len(self.pending(limit=100)['events']), 3)
        first = self.context_with_delivery()
        foreign = self.context_with_delivery('worker')
        with self.assertRaises(Denied):
            first._op_finish({'outcome': 'wait', 'summary': 'Wrong role',
                              'ack_events': [foreign.decision_events[0]['ack_token']]})
        self.assertFalse((first.run_dir / 'finish-intent.json').exists())
        handled = first.decision_events[1]['ack_token']
        item = next(i for i in items if i['id'] == first.decision_events[1]['insight'])
        self.project.insights.revise(item['id'], source='reviewer', title='Revised', body='Changed',
                                     expected_rev=1, base_snapshot=None)
        self.role(decision_events=())
        first._op_finish({'outcome': 'wait', 'summary': 'Handled original', 'ack_events': [handled]})
        delivery.acknowledge(first, {handled})
        self.role(decision_events=('reject', 'defer'))
        remaining = self.pending(limit=100)
        decisions = [e for e in remaining['events'] if e.get('kind') != 'insight.pending']
        addressed = [e for e in remaining['events'] if e.get('kind') == 'insight.pending']
        self.assertEqual(len(decisions), 2)
        self.assertEqual([(e['insight'], e['rev']) for e in addressed], [(item['id'], 2)])
        self.assertEqual(decisions[0]['ack_token'], first.decision_events[0]['ack_token'])
        self.assertEqual(len(self.pending('worker', limit=100)['events']), 2)  # stale source filtered
        self.assertEqual(self.receipts('decision-receipts'), {})

    def test_runtime_partial_finish_drains_only_selected_tokens(self):
        for i in range(4): self.decide(i)
        self.activate()
        seen = []
        def partial(ctx, prompt, *_):
            events = json.loads(prompt)['decision_events']
            seen.append([e['ack_token'] for e in events])
            ctx.handle('finish', {'outcome': 'wait', 'summary': 'Handled one', 'ack_events': [events[-1]['ack_token']]})
        engine = Engine(self.config, driver=ScriptDriver(partial))
        self.assertEqual(engine.run(self.project, 'reviewer')['status'], 'completed')
        self.assertEqual(len(self.pending(limit=100)['events']), 3)
        self.assertEqual(engine.run(self.project, 'reviewer')['status'], 'completed')
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertNotEqual(seen[0][1], seen[1][1])
        self.assertEqual(len(self.pending(limit=100)['events']), 2)

    def test_prompt_budget_uses_exact_utf8_unicode_bytes(self):
        body = 'é' * 120
        self.decide(body=body)
        self.role(decision_page_bytes=1000)
        self.activate()
        ctx = self.context_with_delivery()
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            doc = json.loads(prompt)
            contribution = {key: doc[key] for key in ('decision_events', 'decision_page')}
            # Full and delta prompts use json.dumps defaults, including ASCII
            # escaping; a full row would exceed this byte allowance.
            self.assertLessEqual(len(json.dumps(contribution).encode('utf-8')), 1000)
            self.assertIn('ref', doc['decision_events'][0])
            self.assertNotIn('body', doc['decision_events'][0])

    def test_reference_full_and_delta_prompts_reader_and_byte_budget(self):
        for i in range(3): self.decide(i, body='日本語 😀 "quote" \\ slash\n' * 800)
        self.role(decision_page_bytes=750)
        self.activate()
        ctx = self.context_with_delivery()
        row = ctx.decision_events[0]
        self.assertIn('ref', row)
        self.assertTrue(ctx.decision_page['byte_truncated'])
        self.assertEqual(ctx.decision_page['pending_count'], 3)
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            doc = json.loads(prompt)
            contribution = {key: doc[key] for key in ('decision_events', 'decision_page')}
            self.assertLessEqual(len(json.dumps(contribution).encode()), 750)
            self.assertEqual(doc['decision_events'][0], row)
            self.assertIn('decision_read', doc['decision_task'])
            self.assertNotIn('manifest', doc)
        # Read through the actual broker path, reconstructing a full digest.
        ctx.handle('_hello', {})
        ctx.handle('_budget', {'sequence': 1})
        chunks, offset = [], 0
        while True:
            part = ctx.handle('decision_read', {'ack_token': row['ack_token'], 'offset': offset, 'limit': 8192})
            chunks.append(part['text'])
            if part['next_offset'] is None: break
            offset = part['next_offset']
        self.assertEqual(digest(''.join(chunks).encode()), row['ref']['record_digest'])
        self.assertEqual(self.receipts('shared-record-receipts'), {})
        with self.assertRaises(Denied):
            self.context_with_delivery('worker')._op_decision_read({'ack_token': row['ack_token']})
        with self.assertRaises(Denied):
            ctx._op_decision_read({'ack_token': row['ack_token'], 'limit': 8193})
        # Frozen references survive source and route movement, but not tampering.
        original = ctx._op_decision_read({'ack_token': row['ack_token'], 'limit': 20})
        self.project.insights.withdraw(row['insight'], source='reviewer', reason='Handled')
        self.role(decision_events=())
        self.assertEqual(ctx._op_decision_read({'ack_token': row['ack_token'], 'limit': 20}), original)
        store = delivery.store(ctx)
        path = self.project.root / store._record_name(row['ref']['record_id'])
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(Denied): ctx._op_decision_read({'ack_token': row['ack_token']})

    def test_missing_tiny_and_removed_budget_is_visible_not_empty_or_fallback(self):
        self.decide(body='x' * 20000)
        self.role(decision_page_bytes=None)
        with self.assertRaisesRegex(Denied, 'explicit decision_page_bytes'): self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        self.role(decision_page_bytes=750)
        self.activate()
        self.role(decision_page_bytes=150)
        with self.assertRaisesRegex(LimitExceeded, 'reference exceeds'): self.pending()
        with self.assertRaises(LimitExceeded): _role_events_due(self.project, self.project.snapshots.get(), 'reviewer')
        with self.assertRaises(LimitExceeded):
            Engine(self.config, driver=ScriptDriver()).run(self.project, 'reviewer')
        self.role(decision_page_bytes=None)
        with self.assertRaises(Denied): self.pending()
        with self.assertRaises(Denied): self.activate()  # no implicit downgrade

    def test_activation_during_wake_cannot_hide_shared_budget_error(self):
        self.decide(body='x' * 20000)
        self.activate()
        self.role(decision_page_bytes=150)
        authority = delivery.authority
        calls = 0
        def switched(root):
            nonlocal calls
            calls += 1
            # Simulate the atomic marker becoming visible between the wake's
            # initial authority observation and its decision consumer read.
            return None if calls == 1 else authority(root)
        with patch.object(delivery, 'authority', side_effect=switched), self.assertRaises(LimitExceeded):
            _role_events_due(self.project, self.project.snapshots.get(), 'reviewer')

    def test_shared_receipt_corruption_is_not_legacy_pending(self):
        self.decide()
        self.activate()
        ctx = self.context_with_delivery()
        delivery.acknowledge(ctx, {ctx.decision_events[0]['ack_token']})
        path = next((self.project.root / 'shared-record-receipts').glob('*/*.json'))
        path.write_bytes(b'corrupt')
        with patch.object(self.project.insights, 'decision_events_with_diagnostics', side_effect=AssertionError('fallback')):
            with self.assertRaises(Denied): self.pending()
            with self.assertRaises(Denied): _role_events_due(self.project, self.project.snapshots.get(), 'reviewer')

    def test_deferral_window_does_not_hide_substantive_wake(self):
        self.role(decision_events_limit=1)
        # Source instants have second precision: fixture creation speed must
        # not decide which random stable ID wins a same-second tie.
        with patch('mizu.insights.now', return_value='2026-01-01T00:00:00+00:00'):
            self.decide(0, action='defer')
        with patch('mizu.insights.now', return_value='2026-01-01T00:00:01+00:00'):
            self.decide(1, action='reject')
        self.activate()
        self.assertEqual(self.pending()['events'][0]['action'], 'defer')
        self.assertTrue(_role_events_due(self.project, self.project.snapshots.get(), 'reviewer'))
        state = Engine(self.config, driver=ScriptDriver())._dispatch_state(
            self.project, 'reviewer', self.config.roles['reviewer'], event_trigger=True)
        self.assertNotIn('skip', state)
        self.assertEqual(state['decision_events'][0]['action'], 'defer')

    def test_backup_restore_preserves_single_authority_and_exact_receipts(self):
        self.decide()
        marker = self.activate()
        ctx = self.context_with_delivery()
        delivery.acknowledge(ctx, {ctx.decision_events[0]['ack_token']})
        for name in (delivery.AUTHORITY_FILE, delivery.PLAN_FILE, delivery.REPORT_FILE):
            self.assertIn(name, MEMBERS)
        self.project.set_control(paused=True)
        archive = self.root / 'cutover.tar.gz'
        backup(self.project, archive)
        restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertEqual(delivery.authority(restored.root), marker)
        self.assertEqual(delivery.pending(restored, self.config.roles['reviewer'])['events'], [])

    def test_config_budget_is_explicit_optional_and_bounded(self):
        text = self.file.read_text()
        for value in ('1', '750', '1048576'):
            self.file.write_text(text.replace('[roles.reviewer]', f'[roles.reviewer]\ndecision_page_bytes = {value}'))
            self.assertEqual(load(self.file).roles['reviewer'].decision_page_bytes, int(value))
        for value in ('0', '1048577', 'true', '1.0', '"750"'):
            self.file.write_text(text.replace('[roles.reviewer]', f'[roles.reviewer]\ndecision_page_bytes = {value}'))
            with self.subTest(value=value), self.assertRaises(ConfigError): load(self.file)

    def submit_pending(self, title="Attention", *, source="searcher", addressee="reviewer"):
        return self.project.insights.submit(source=source, title=title, body="Exact current body",
                                            base_snapshot=None, kind="question",
                                            addressee=addressee, urgency="high")

    def test_pending_activation_maps_acknowledged_and_preserves_legacy(self):
        self.submit_pending("First", addressee="reviewer")
        self.submit_pending("Second", addressee="reviewer")
        third = self.submit_pending("Third", addressee="worker")
        reviewer_rows, _ = self.project.insights.pending_insight_events(
            "reviewer", set(self.project.roles), limit=100)
        self.assertEqual(len(reviewer_rows), 2)
        acked = reviewer_rows[:1]
        unacked = reviewer_rows[1:]
        self.project.insights.acknowledge_decisions("reviewer", acked, sources="any")
        before = self.receipts("decision-receipts")
        self.assertEqual(len(before), 1)
        marker = self.activate()
        self.assertEqual(self.receipts("decision-receipts"), before)
        reviewer_state = self.pending("reviewer", limit=100)
        worker_state = self.pending("worker", limit=100)
        self.assertTrue(reviewer_state["shared"])
        self.assertTrue(worker_state["shared"])
        self.assertEqual(len(reviewer_state["events"]), 1)
        self.assertEqual(reviewer_state["events"][0]["insight"], unacked[0]["insight"])
        self.assertEqual(len(worker_state["events"]), 1)
        self.assertEqual(worker_state["events"][0]["insight"], third["id"])
        # Report carries separate pending parity alongside decision fields.
        report = read_json(self.project.root / delivery.REPORT_FILE)
        reviewer_report = report["roles"]["reviewer"]
        self.assertEqual(reviewer_report["current_ids"], [])
        pending = reviewer_report["pending_insights"]
        self.assertEqual(len(pending["current_ids"]), 2)
        self.assertEqual(len(pending["legacy_received_ids"]), 1)
        self.assertEqual(pending["shared_received_ids"], pending["legacy_received_ids"])
        self.assertEqual(pending["shared_pending_ids"], pending["legacy_pending_ids"])
        # Restart returns the marker without reimporting; legacy stays byte-identical.
        restarted = Project(self.config, "sample")
        self.assertEqual(reconciliation.activate_decision_delivery(restarted), marker)
        self.assertEqual(self.receipts("decision-receipts"), before)
        self.assertEqual(self.pending("reviewer", limit=100)["events"],
                         reviewer_state["events"])

    def test_pending_unexplained_and_removed_recipient_refuse_activation(self):
        self.submit_pending(addressee="reviewer")
        # Unexplained shared pending receipt with no legacy ack refuses.
        store = SharedRecords(self.project.root, {"insight.decision": INSIGHT_DECISION_POLICY,
                                                  "insight.pending": INSIGHT_PENDING_POLICY})
        envelopes = dict(self.project.insights._pending_insight_envelopes_locked(
            set(self.project.roles), "reviewer"))
        rid = next(iter(envelopes))
        store.put(envelopes[rid], writer="host.insights")
        from dataclasses import asdict
        manifest = store.freeze("reviewer", [rid])
        store.acknowledge("reviewer", [asdict(manifest[0])], manifest=manifest)
        before = self.receipts("shared-record-receipts")
        with self.assertRaises(Denied):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        self.assertEqual(self.receipts("shared-record-receipts"), before)
        # Remove the orphan and activate cleanly, then a removed-recipient receipt refuses.
        for path in (self.project.root / "shared-record-receipts").rglob("*.json"):
            path.unlink()
        for path in (self.project.root / "shared-records").rglob("*.json"):
            path.unlink()
        marker = self.activate()
        self.assertIsNotNone(marker)
        # A pending receipt for a disabled recipient is unexplained.
        store2 = SharedRecords(self.project.root, {"insight.decision": INSIGHT_DECISION_POLICY,
                                                   "insight.pending": INSIGHT_PENDING_POLICY})
        store2.put(envelopes[rid], writer="host.insights")
        manifest2 = store2.freeze("retired", [rid])
        store2.acknowledge("retired", [asdict(manifest2[0])], manifest=manifest2)
        # Corrupt the marker to force re-entry check via fresh project is not needed:
        # inventory runs before marker return, so remove marker files to re-exercise.
        # Instead verify a fresh project with the same retired receipt refuses.
        # Here the active marker already exists, so activation returns it; verify
        # inventory on a pre-marker clone by deleting marker and re-activating.
        (self.project.root / delivery.AUTHORITY_FILE).unlink()
        (self.project.root / delivery.PLAN_FILE).unlink()
        (self.project.root / delivery.REPORT_FILE).unlink()
        with self.assertRaisesRegex(Denied, "Unexplained shared-only"):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))

    def test_pending_interrupted_prefix_exact_retry_before_marker(self):
        for i in range(3):
            self.submit_pending(f"Pending {i}", addressee="reviewer")
        rows, _ = self.project.insights.pending_insight_events(
            "reviewer", set(self.project.roles), limit=100)
        self.project.insights.acknowledge_decisions("reviewer", rows, sources="any")
        before = self.receipts("decision-receipts")
        original = SharedRecords.acknowledge
        def interrupted(store, role, selections=None, *, manifest):
            original(store, role, selections[:1], manifest=manifest)
            raise OSError("interrupted pending prefix")
        with patch.object(SharedRecords, "acknowledge", interrupted), self.assertRaises(OSError):
            self.activate()
        self.assertIsNone(delivery.authority(self.project.root))
        prefix = self.receipts("shared-record-receipts")
        self.assertEqual(len(prefix), 1)
        self.project.insights = Insights(self.project.root)
        marker = self.activate()
        self.assertIsNotNone(marker)
        self.assertEqual(len(self.receipts("shared-record-receipts")), 3)
        self.assertEqual(self.receipts("decision-receipts"), before)
        for name, raw in prefix.items():
            self.assertEqual(self.receipts("shared-record-receipts")[name], raw)
        self.assertEqual(self.pending(limit=100)["events"], [])
