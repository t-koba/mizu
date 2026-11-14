"""Exact delivery lifetimes; offline stores and synthetic drivers, not OCI proof."""
import dataclasses
import os
import time
from itertools import product
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver
from mizu import (ci_waits, decision_delivery, decision_reconciliation, shared_records,
                  delivery_settlement as settlement, operator_records, proposal_waits, storage_runs)
from mizu.distribution import public_source_identity
from mizu.errors import Denied, LimitExceeded
from mizu.fs import ATOMIC_WRITE_TEMP_PREFIX, now, read_json, write_json
from mizu.project import Project
from mizu.publication import Publication
from mizu.runtime import Engine
from mizu.storage import backup, prune, restore
from mizu.vcs_proposal_poll import poll_proposals


class DeliverySettlementTests(Fixture):
    def setUp(self):
        super().setUp()
        roles = {name: dataclasses.replace(role, decision_page_bytes=8192, decision_events=('reject',))
                 for name, role in self.config.roles.items()}
        roles['reviewer'] = dataclasses.replace(roles['reviewer'], on_change=False)
        self.config = dataclasses.replace(self.config, roles=roles,
            vcs={**self.config.vcs, 'poll_enabled': True, 'poll_proposals_enabled': True,
                 'command': ['configured-adapter']})
        self.project = Project(self.config, 'sample')
        decision_reconciliation.activate_decision_delivery(self.project)
        item = self.project.insights.submit(source='reviewer', title='Finding', body='Proposal', base_snapshot=None)
        self.project.insights.decide(item['id'], 'reject', 'Reason', '', 'test', expected_rev=1)
        operator_records.write(self.project, event_id='notice', kind='operator.notice',
                               body='Exact notice', addressee='reviewer')
        publication = Publication(self.project.root, 'a' * 40, 'c' * 64, 'published', 'push',
                                  projection=public_source_identity({}))
        intent, _ = publication.prepare('approval', 'run')
        publication.mark_dispatch()
        publication.complete(intent, 'b' * 40)
        row = {'id': 'forge:owner/repo#1', 'state': 'open', 'draft': False,
               'head': {'repo': 'owner/repo', 'ref': 'refs/heads/published', 'sha': 'b' * 40},
               'base': {'repo': 'owner/repo', 'ref': 'main', 'sha': 'd' * 40}}
        state = {}
        def poll_proposal(at):
            return poll_proposals(self.config, self.project, state, now=at, interval=15,
                reader=Mock(return_value={'proposals': [row], 'complete': True}))
        self.assertTrue(poll_proposal(1000)['ok'])
        store = self.project.work_items
        for key, wait in (
            ('proposal', {'kind': 'proposal_merged', 'proposal_id': row['id'], 'publication_id': publication.id}),
            ('ci', {'kind': 'ci', 'publication_id': publication.id, 'checks': ['unit']})):
            seed = store.create(actor='reviewer', idempotency_key=key, title=key)['record']['work_item']
            store.transition(seed['id'], expected_rev=1, status='blocked', actor='reviewer', wait=wait)
        row['state'] = 'merged'
        self.assertTrue(poll_proposal(1030)['ok'])
        ci_waits.poll(self.config, self.project, {}, now=1000, interval=15,
            reader=Mock(return_value={'checks': [{'check': 'unit', 'sha': 'b' * 40,
                                                  'state': 'success', 'run': 'unit-run'}]}))
        self.contexts = []

    def driver(self, ctx, prompt, profile=None):
        self.contexts.append(ctx)
        row = settlement._read(self.project.root, ctx.run_dir.name)
        self.assertFalse(row['closed'])
        self.assertTrue(all(row['manifests'].values()))
        tokens = [entry.ack_token for entries in (
            ctx.decision_manifest, ctx.operator_record_manifest, ctx.ci_wait_manifest,
            ctx.proposal_wait_manifest) for entry in entries]
        ctx.handle('finish', {'outcome': 'wait', 'summary': 'Observed exact deliveries',
                              'ack_events': tokens})

    def run_role(self, driver=None):
        return Engine(self.config, driver=ScriptDriver(driver or self.driver)).run(self.project, 'reviewer')

    def inventory(self):
        return settlement.inventory(self.project.root, storage_runs.Scan(self.project.root))

    def assert_retained(self, run):
        self.assertIn(run, self.inventory()[0])
        self.assertTrue((self.project.root / 'runs' / run / 'started.json').exists())
        plan = storage_runs.plan(self.project)
        self.assertTrue(plan['complete'], plan)
        self.assertFalse(any(run in c for c in plan['components']))

    def seed_expired_run(self, run='f' * 32):
        directory = self.project.root / 'runs' / run
        directory.mkdir()
        snapshot = self.project.snapshots.get()['id']
        write_json(directory / 'started.json', {
            'run': run, 'role': 'reviewer', 'snapshot': snapshot,
            'started_at': '2020-01-01T00:00:00+00:00'})
        write_json(directory / 'result.json', {
            'run': run, 'role': 'reviewer', 'snapshot': snapshot, 'finished_at': now(),
            'status': 'completed', 'finish': {
                'outcome': 'wait', 'summary': 'Retained fixture', 'state': 'Next: explicit.'}})
        return directory

    def test_success_binds_all_exact_manifests_and_cleans_only_after_close(self):
        original = settlement.cleanup
        seen = []
        def cleanup(root, run):
            row = settlement._read(root, run)
            self.assertTrue(self.contexts[0].closed)
            self.assertTrue(settlement.terminal(row))
            self.assertTrue(all(state == 'succeeded' for effects in row['effects'].values()
                                for state in effects.values()))
            seen.append(run)
            original(root, run)
        with patch.object(settlement, 'cleanup', side_effect=cleanup):
            result = self.run_role()
        self.assertEqual(seen, [result['run']])
        self.assertEqual(self.inventory(), (set(), [], []))

    def test_registration_fault_prevents_dispatch_and_ambiguous_commit_stays_pinned(self):
        original = settlement._write
        def fail(row_root, row, **kwargs):
            original(row_root, row, **kwargs)
            raise OSError('registration committed but caller interrupted')
        with patch.object(settlement, '_write', side_effect=fail), \
                patch('mizu.runtime.check_jobs.mark_wake_delivered') as mark_wake, \
                self.assertRaises(OSError):
            self.run_role()
        mark_wake.assert_not_called()
        self.assertFalse(self.contexts)
        pins, _, _ = self.inventory()
        self.assertEqual(len(pins), 1)
        self.assert_retained(next(iter(pins)))

    def test_result_publication_fault_leaves_selection_unresolved(self):
        def fail(path, value, **kwargs):
            if path.name == 'result.json' and value.get('status') == 'completed':
                raise OSError('result publication failed')
            return write_json(path, value, **kwargs)
        with patch('mizu.runtime.write_json', side_effect=fail), self.assertRaises(OSError):
            self.run_role()
        run = self.contexts[0].run_dir.name
        row = settlement._read(self.project.root, run)
        self.assertIsNone(row['selected'])
        self.assertTrue(row['closed'])
        self.assert_retained(run)

    def test_each_shared_receipt_fault_keeps_its_exact_effect_unresolved(self):
        # One run selects all four families. Decision/operator errors propagate;
        # CI/proposal errors remain suppressed under existing semantics.
        for family, commit in product(('operator', 'decision', 'ci', 'proposal'), (False, True)):
            with self.subTest(family=family, committed=commit):
                # Fresh independent fixture per fault, so a prior receipt cannot
                # hide an effect that this run actually selected.
                case = type(self)('test_success_binds_all_exact_manifests_and_cleans_only_after_close')
                case.setUp()
                try:
                    original = shared_records.atomic_write
                    def fail_receipt(path, raw, **kwargs):
                        if case.contexts:
                            ctx = case.contexts[0]
                            manifest = {'operator': ctx.operator_record_manifest, 'decision': ctx.decision_manifest,
                                        'ci': ctx.ci_wait_manifest, 'proposal': ctx.proposal_wait_manifest}[family]
                            entry = manifest[0]
                            receipt_path = case.project.root / shared_records.SharedRecords(case.project.root)._receipt_name(
                                ctx.role.name, entry.record_id)
                            if path == receipt_path:
                                if commit:
                                    original(path, raw, **kwargs)
                                raise OSError('receipt write failed')
                        return original(path, raw, **kwargs)
                    with patch('mizu.shared_records.atomic_write', side_effect=fail_receipt):
                        if family in ('operator', 'decision'):
                            with case.assertRaises(OSError):
                                case.run_role()
                        else:
                            case.assertEqual(case.run_role()['status'], 'completed')
                    run = case.contexts[0].run_dir.name
                    row = settlement._read(case.project.root, run)
                    case.assertTrue(row['selected'][family])
                    case.assertEqual(set(row['effects'][family].values()), {'unresolved'})
                    case.assert_retained(run)
                    # A missing active pointer, completed result or repeat cleanup
                    # cannot turn uncertainty into a terminal settlement.
                    settlement.cleanup(case.project.root, run)
                    case.assert_retained(run)
                finally:
                    case.doCleanups()

    def test_post_context_record_fault_and_terminal_cleanup_replay(self):
        original = settlement._write
        def fail(root, row, **kwargs):
            if row['closed']:
                raise OSError('closure record failed')
            original(root, row, **kwargs)
        with patch.object(settlement, '_write', side_effect=fail):
            result = self.run_role()
        row = settlement._read(self.project.root, result['run'])
        self.assertFalse(row['closed'])
        self.assertTrue(row['finalized'])
        self.assert_retained(result['run'])
        # This is not recovery inference: the original closed Context explicitly
        # records its already-completed cleanup, then unlink is interrupted.
        with patch.object(settlement, 'cleanup', side_effect=OSError('unlink failed')):
            with self.assertRaises(OSError):
                settlement.closed(self.contexts[0])
        row = settlement._read(self.project.root, result['run'])
        self.assertTrue(settlement.terminal(row))
        self.assertEqual(self.inventory(), (set(), [result['run']], []))
        settlement.cleanup(self.project.root, result['run'])
        settlement.cleanup(self.project.root, result['run'])
        self.assertEqual(self.inventory(), (set(), [], []))

    def test_unresolved_and_corrupt_evidence_survive_prune_backup_restore(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        run = result['run']
        run_dir = self.project.root / 'runs' / run
        # Age the lifecycle, not mtimes; no active pointer is needed for the root.
        started = read_json(run_dir / 'started.json')
        started['started_at'] = '2020-01-01T00:00:00+00:00'
        write_json(run_dir / 'started.json', started)
        (run_dir / 'events.jsonl').write_text('necessary run evidence\n')
        prune(self.project, apply=True)
        self.assert_retained(run)
        self.assertTrue((run_dir / 'events.jsonl').exists())
        path = settlement._path(self.project.root, run)
        raw = path.read_bytes()
        self.project.set_control(paused=True)
        archive = self.root / 'settlement.tar.gz'
        backup(self.project, archive)
        restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertEqual(settlement._path(restored.root, run).read_bytes(), raw)
        self.assertEqual(settlement.inventory(restored.root, storage_runs.Scan(restored.root))[0], {run})
        self.assertTrue((restored.root / 'runs' / run / 'events.jsonl').exists())
        restored_row = settlement._read(restored.root, run)
        restored_stores = {
            'decision': shared_records.SharedRecords(restored.root, decision_delivery.INSIGHT_DELIVERY_POLICIES),
            'operator': operator_records.store(restored),
            'ci': ci_waits.shared_store(restored.work_items),
            'proposal': proposal_waits.shared_store(restored.work_items),
        }
        for family, entries in restored_row['manifests'].items():
            source = restored_stores[family]
            for entry in entries:
                source.get(entry['record_id'], expected_digest=entry['record_digest'])
                if restored_row['effects'][family][entry['ack_token']] == 'succeeded':
                    self.assertTrue(source.received(entry['role'], entry['record_id'],
                                                    entry['record_digest']))
        # Corruption is itself retained, not silently ignored or reconstructed.
        corrupt_row = read_json(path)
        corrupt_row['record']['closed'] = False
        write_json(path, corrupt_row)  # Schema-valid mutation, stale checksum.
        with self.assertRaisesRegex(Denied, 'digest mismatch'):
            prune(self.project, apply=True)
        path.write_bytes(b'{broken')
        with self.assertRaises((Denied, ValueError)):
            prune(self.project, apply=True)
        self.assertTrue(run_dir.exists())
        archive2 = self.root / 'corrupt-settlement.tar.gz'
        backup(self.project, archive2)
        restore(self.config, 'corrupt-restored', archive2)
        corrupt = Project(self.config, 'corrupt-restored')
        self.assertEqual(settlement._path(corrupt.root, run).read_bytes(), b'{broken')

    def test_existing_manifest_and_registry_bounds_fail_closed(self):
        ctx = self.context('reviewer')
        operator_records.bind(ctx)
        ctx.operator_record_manifest *= 513
        with self.assertRaises(Denied):
            settlement.register(ctx)
        self.assertFalse(settlement._path(self.project.root, ctx.run_dir.name).exists())
        ctx = self.context('reviewer')
        operator_records.bind(ctx)
        with patch.object(settlement, 'MAX_FRAME', 1), self.assertRaises(LimitExceeded):
            settlement.register(ctx)
        self.assertFalse(settlement._path(self.project.root, ctx.run_dir.name).exists())
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        with patch('mizu.storage_runs.MAX_ENTRIES', 0), self.assertRaises(LimitExceeded):
            self.inventory()
        self.assertTrue(settlement._path(self.project.root, result['run']).exists())

    def bound_context(self):
        ctx = self.context('reviewer')
        decision_delivery.bind(ctx, decision_delivery.pending(self.project, ctx.role, actions=('reject',)))
        operator_records.bind(ctx)
        _, ctx.ci_wait_manifest = ci_waits.delivery(self.project.work_items, 'reviewer')
        _, ctx.proposal_wait_manifest = proposal_waits.delivery(self.project.work_items, 'reviewer')
        settlement.register(ctx)
        return ctx

    def test_explicit_skips_preserve_check_join_bounded_and_omitted_selection_semantics(self):
        for flags in ({'wait_for_check': 'check-id'}, {'outcome': 'bounded'}, {'bound_hit': 'requests'}, {}):
            with self.subTest(flags=flags):
                ctx = self.bound_context()
                ctx.bound_hit = flags.get('bound_hit')
                tokens = [e.ack_token for entries in (ctx.ci_wait_manifest, ctx.proposal_wait_manifest)
                          for e in entries]
                finished = {'outcome': 'wait', 'ack_events': tokens, **flags}
                # The final case explicitly selects nothing. This alone is not
                # settlement: finalization and Context closure still must commit.
                if not flags:
                    finished['ack_events'] = []
                settlement.begin(ctx, finished)
                ci_waits.complete(ctx, finished)
                proposal_waits.complete(ctx, finished)
                settlement.succeeded(ctx, 'ci')
                settlement.succeeded(ctx, 'proposal')
                row = settlement._read(self.project.root, ctx.run_dir.name)
                expected = ('check-join' if 'wait_for_check' in flags else 'bounded') if flags else 'not-selected'
                for family in ('ci', 'proposal'):
                    self.assertEqual(set(row['effects'][family].values()), {expected})
                self.assertFalse(settlement.terminal(row))
                settlement.finalized(ctx)
                with self.assertRaises(Denied):
                    settlement.closed(ctx)
                ctx.closed = True
                settlement.closed(ctx)
                self.assertFalse(settlement._path(self.project.root, ctx.run_dir.name).exists())
        self.assertTrue(ci_waits.pending(self.project.work_items, 'reviewer'))
        self.assertTrue(proposal_waits.pending(self.project.work_items, 'reviewer'))

    def test_readiness_preview_and_non_dispatched_selector_waits_never_pin(self):
        from test_selection import specification, cond
        from mizu.runtime import should_run
        spec = specification()
        for candidate in spec['rules'][0]['candidates']:
            candidate['when'] = cond('attributes.missing', 'eq', True)
        role = dataclasses.replace(self.config.roles['reviewer'], profile='', selector='dynamic')
        self.config = dataclasses.replace(self.config, selectors={'dynamic': spec},
                                          roles={**self.config.roles, 'reviewer': role})
        self.project.config = self.config
        driver = ScriptDriver()
        engine = Engine(self.config, driver=driver)
        with patch.object(settlement, 'register', side_effect=AssertionError('must not register')):
            engine.preview(self.project, 'reviewer')
            should_run(self.project, self.project.snapshots.get(), role_name='reviewer')
            self.assertEqual(engine.run(self.project, 'reviewer')['status'], 'waiting')
            self.assertEqual(engine.run(self.project, 'reviewer')['status'], 'waiting')
        self.assertFalse(driver.calls)
        self.assertFalse((self.project.root / settlement.DIRECTORY).exists())

    def test_missing_pinned_run_blocks_pruning_other_expired_runs(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        pinned_run = result['run']
        pinned_path = self.project.root / 'runs' / pinned_run
        pinned_record = settlement._path(self.project.root, pinned_run)
        raw_pin = pinned_record.read_bytes()
        candidate = self.seed_expired_run()
        before = storage_runs.plan(self.project)
        self.assertTrue(before['complete'], before)
        self.assertTrue(any(candidate.name in component for component in before['components']))

        missing_copy = self.root / 'missing-pinned-run'
        pinned_path.rename(missing_copy)
        incomplete = storage_runs.plan(self.project)
        self.assertFalse(incomplete['complete'])
        self.assertIn('Missing referenced run member', incomplete['reason'])
        with self.assertRaisesRegex(Denied, 'Incomplete run-reference scan'):
            prune(self.project, apply=True)
        self.assertTrue(candidate.exists())
        self.assertTrue(missing_copy.exists())
        self.assertEqual(pinned_record.read_bytes(), raw_pin)

    def test_staging_recovery_refuses_missing_pinned_run_without_pruning(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        pinned_run = result['run']
        pinned_path = self.project.root / 'runs' / pinned_run
        pinned_record = settlement._path(self.project.root, pinned_run)
        raw_pin = pinned_record.read_bytes()
        candidate = self.seed_expired_run()
        missing_copy = self.root / 'missing-staged-run'
        pinned_path.rename(missing_copy)
        staging = self.project.root / 'maintenance' / 'run-quarantine' / ('staging-' + 'a' * 32)
        (staging / 'runs').mkdir(parents=True)
        write_json(staging / 'manifest.json', {
            'version': 1, 'runs': [pinned_run], 'days': 31, 'scanned_at': time.time(),
            'plan_sha256': 'b' * 64})

        with self.assertRaisesRegex(Denied, 'Missing run during quarantine recovery'):
            prune(self.project, apply=True)
        self.assertTrue(candidate.exists())
        self.assertTrue(missing_copy.exists())
        self.assertTrue(staging.exists())
        self.assertEqual(pinned_record.read_bytes(), raw_pin)

    def test_staging_recovery_refuses_symlinked_pinned_run_without_pruning(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        pinned_run = result['run']
        pinned_path = self.project.root / 'runs' / pinned_run
        pinned_record = settlement._path(self.project.root, pinned_run)
        raw_pin = pinned_record.read_bytes()
        candidate = self.seed_expired_run()
        outside = self.root / 'outside-pinned-run'
        pinned_path.rename(outside)
        staging = self.project.root / 'maintenance' / 'run-quarantine' / ('staging-' + 'a' * 32)
        staged_runs = staging / 'runs'
        staged_runs.mkdir(parents=True)
        try:
            (staged_runs / pinned_run).symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create symbolic links')
        write_json(staging / 'manifest.json', {
            'version': 1, 'runs': [pinned_run], 'days': 31, 'scanned_at': time.time(),
            'plan_sha256': 'b' * 64})

        with self.assertRaisesRegex(Denied, 'Unsafe run-reference subtree entry'):
            prune(self.project, apply=True)
        self.assertTrue(candidate.exists())
        self.assertTrue(outside.exists())
        self.assertTrue((staged_runs / pinned_run).is_symlink())
        self.assertEqual(pinned_record.read_bytes(), raw_pin)

    def test_zero_retention_recovers_staged_pin_and_keeps_its_old_event_log(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        pinned_run = result['run']
        pinned_path = self.project.root / 'runs' / pinned_run
        pinned_record = settlement._path(self.project.root, pinned_run)
        raw_pin = pinned_record.read_bytes()
        candidate = self.seed_expired_run()
        event_log = pinned_path / 'pinned-events.jsonl'
        event_log.write_text('unresolved pinned run evidence\n', encoding='utf-8')
        old = time.time() - 40 * 86400
        os.utime(event_log, (old, old))
        staging = self.project.root / 'maintenance' / 'run-quarantine' / ('staging-' + 'a' * 32)
        (staging / 'runs').mkdir(parents=True)
        write_json(staging / 'manifest.json', {
            'version': 1, 'runs': [pinned_run], 'days': 31, 'scanned_at': time.time(),
            'plan_sha256': 'b' * 64})
        pinned_path.rename(staging / 'runs' / pinned_run)
        self.config = dataclasses.replace(
            self.config, limits=dataclasses.replace(self.config.limits, run_retention_days=0))
        self.project.config = self.config

        prune(self.project, apply=True)

        restored_run = self.project.root / 'runs' / pinned_run
        self.assertTrue(restored_run.is_dir())
        self.assertEqual((restored_run / 'pinned-events.jsonl').read_text(encoding='utf-8'),
                         'unresolved pinned run evidence\n')
        self.assertTrue(candidate.exists())
        self.assertEqual(pinned_record.read_bytes(), raw_pin)
        self.assertFalse(staging.exists())
        self.assertTrue((staging.parent.parent / (staging.name + '.json')).exists())
        self.assert_retained(pinned_run)

    def test_committed_quarantine_is_protected_and_staging_recovers_with_pin(self):
        import time
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        run = result['run']
        directory = self.project.root / 'maintenance' / 'run-quarantine' / ('retired-' + 'a' * 32)
        (directory / 'runs').mkdir(parents=True)
        write_json(directory / 'manifest.json', {'version': 1, 'runs': [run], 'days': 31,
                    'scanned_at': time.time(), 'plan_sha256': 'b' * 64})
        (self.project.root / 'runs' / run).rename(directory / 'runs' / run)
        with self.assertRaisesRegex(Denied, 'Unresolved delivery settlement'):
            prune(self.project, apply=True)
        self.assertTrue((directory / 'runs' / run / 'result.json').exists())
        directory.rename(directory.with_name('staging-' + 'a' * 32))
        prune(self.project, apply=True)
        self.assert_retained(run)
        self.assertEqual(storage_runs.quarantines(self.project), [])

    def test_crash_temporary_cleanup_and_closure_failure_preserve_committed_registration(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        run = result['run']
        temp = self.project.root / settlement.DIRECTORY / (ATOMIC_WRITE_TEMP_PREFIX + 'interrupted')
        temp.write_bytes(b'{partial uncommitted update')
        self.assertEqual(self.inventory()[2], [temp.name])
        self.assertTrue(temp.exists())
        prune(self.project, apply=True)
        self.assertFalse(temp.exists())
        self.assert_retained(run)
        # A poisoned execution session cannot claim the post-Context boundary.
        ctx = self.contexts[0]
        ctx.exec_session = object()
        with self.assertRaises(Denied):
            settlement.closed(ctx)
        self.assert_retained(run)

    def test_continuation_recovery_does_not_settle_delivery_from_terminal_result(self):
        import uuid
        from mizu import continuations
        from mizu.fs import lock, now
        from mizu.runtime import Context
        role = 'worker'
        store = self.project.snapshots
        source = uuid.uuid4().hex
        current = store.get()
        write_json(self.project.root / 'runs' / source / 'started.json',
                   {'run': source, 'role': role, 'snapshot': current['id'], 'started_at': now()})
        snap = store.create(store.capture_files(self.project.workspace), goal=current['goal'],
                            state='state', run=source, outcome='continue', summary='Yield', parent=current['id'])
        with lock(self.project.root / 'locks' / 'run-worker.lock'), lock(self.project.root / 'locks' / 'workspace.lock'):
            continuations.prepare(store, role=role, source_run=source, snapshot=snap['id'])
        store.publish(snap)
        operator_records.write(self.project, event_id='worker-notice', kind='operator.notice',
                               body='Exact worker notice', addressee=role)
        engine = Engine(self.config, driver=ScriptDriver())
        with continuations.reservation(engine, self.project, role) as row:
            run = row['successor']
            started = {'run': run, 'role': role, 'snapshot': row['current_snapshot'], 'started_at': now()}
            continuations.start(store, row, started)
            ctx = Context(self.config, self.project, self.config.roles[role], self.project.root / 'runs' / run,
                          store.get(), self.project.workspace)
            operator_records.bind(ctx)
            settlement.register(ctx)
            # Recovery accepts an interrupted result under existing continuation
            # semantics, but it cannot manufacture selection/effect/closure proof.
            write_json(ctx.run_dir / 'result.json', {'run': run, 'role': role, 'status': 'interrupted',
                       'error': 'crash', 'finished_at': now(), 'snapshot': started['snapshot']})
            self.assertEqual(continuations.recover(store, row)['status'], 'interrupted')
            self.assertIsNone(continuations.pending(store, role))
        self.assert_retained(run)
        row = settlement._read(self.project.root, run)
        self.assertFalse(row['closed'])
        self.assertIsNone(row['selected'])


    def test_effect_and_finalization_record_faults_do_not_infer_success_from_receipts(self):
        original = settlement._write
        for boundary in ('effect', 'finalization'):
            with self.subTest(boundary=boundary):
                case = type(self)('test_success_binds_all_exact_manifests_and_cleans_only_after_close')
                case.setUp()
                try:
                    def fail(root, row, **kwargs):
                        if ((boundary == 'effect' and any(state == 'succeeded'
                                for effects in row['effects'].values() for state in effects.values()))
                                or (boundary == 'finalization' and row['finalized'])):
                            raise OSError('settlement record interrupted')
                        original(root, row, **kwargs)
                    with patch.object(settlement, '_write', side_effect=fail), case.assertRaises(OSError):
                        case.run_role()
                    run = case.contexts[0].run_dir.name
                    entry = case.contexts[0].operator_record_manifest[0]
                    case.assertTrue(operator_records.store(case.project).received(
                        'reviewer', entry.record_id, entry.record_digest))
                    case.assert_retained(run)
                    row = settlement._read(case.project.root, run)
                    case.assertFalse(row['finalized'])
                finally:
                    case.doCleanups()

    def test_execution_cleanup_fault_never_records_closure_or_releases_pin(self):
        from mizu.runtime import Context
        original = Context.close_exec
        def fail(ctx):
            if ctx.closed:
                raise OSError('execution cleanup failed')
            return original(ctx)
        with patch.object(Context, 'close_exec', fail), self.assertRaises(OSError):
            self.run_role()
        run = self.contexts[0].run_dir.name
        row = settlement._read(self.project.root, run)
        self.assertFalse(row['closed'])
        self.assertTrue(row['finalized'])
        (self.project.root / 'active' / 'reviewer.json').unlink(missing_ok=True)
        settlement.cleanup(self.project.root, run)
        self.assert_retained(run)


    def test_backup_refuses_unsafe_registry_instead_of_dropping_pin(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        path = settlement._path(self.project.root, result['run'])
        other = path.with_name('retained-original')
        path.rename(other)
        try:
            path.symlink_to(other.name)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create symbolic links')
        self.project.set_control(paused=True)
        archive = self.root / 'unsafe-settlement.tar.gz'
        with self.assertRaises(Denied):
            backup(self.project, archive)
        self.assertFalse(archive.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.project.root / 'runs' / result['run']).exists())

    def test_backup_refuses_missing_pinned_run_target(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        run = result['run']
        run_dir = self.project.root / 'runs' / run
        outside = self.root / 'missing-target-evidence'
        run_dir.rename(outside)
        self.project.set_control(paused=True)
        archive = self.root / 'missing-pinned-run.tar.gz'

        with self.assertRaisesRegex(Denied, 'missing settlement run evidence'):
            backup(self.project, archive)

        self.assertFalse(archive.exists())
        self.assertTrue(outside.is_dir())
        self.assertTrue(settlement._path(self.project.root, run).exists())

    def test_backup_refuses_missing_receipt_for_succeeded_settlement_effect(self):
        with patch('mizu.runtime.decision_delivery.acknowledge',
                   side_effect=OSError('decision receipt failed')):
            with self.assertRaisesRegex(OSError, 'decision receipt failed'):
                self.run_role()
        run = self.contexts[0].run_dir.name
        row = settlement._read(self.project.root, run)
        token = row['selected']['operator'][0]
        entry = next(item for item in row['manifests']['operator'] if item['ack_token'] == token)
        store = shared_records.SharedRecords(self.project.root)
        receipt = self.project.root / store._receipt_name(entry['role'], entry['record_id'])
        self.assertEqual(row['effects']['operator'][token], 'succeeded')
        self.assertTrue(receipt.exists())
        self.project.set_control(paused=True)
        mismatched = read_json(receipt)
        mismatched['record_digest'] = 'f' * 64
        write_json(receipt, mismatched)
        wrong_archive = self.root / 'mismatched-success-receipt.tar.gz'
        with self.assertRaisesRegex(Denied, 'mismatched settlement success receipt'):
            backup(self.project, wrong_archive)
        self.assertFalse(wrong_archive.exists())

        receipt.unlink()
        archive = self.root / 'missing-success-receipt.tar.gz'
        with self.assertRaisesRegex(Denied, 'missing settlement success receipt'):
            backup(self.project, archive)
        self.assertFalse(archive.exists())
        self.assertTrue(settlement._path(self.project.root, run).exists())

    def test_backup_refuses_missing_or_changed_manifest_source(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        row = settlement._read(self.project.root, result['run'])
        entry = row['manifests']['operator'][0]
        store = shared_records.SharedRecords(self.project.root)
        source = self.project.root / store._record_name(entry['record_id'])
        original = source.read_bytes()
        self.project.set_control(paused=True)

        source.unlink()
        missing_archive = self.root / 'missing-source.tar.gz'
        with self.assertRaisesRegex(Denied, 'missing or changed settlement source'):
            backup(self.project, missing_archive)
        self.assertFalse(missing_archive.exists())

        source.write_bytes(original + b' ')
        changed_archive = self.root / 'changed-source.tar.gz'
        with self.assertRaisesRegex(Denied, 'missing or changed settlement source'):
            backup(self.project, changed_archive)
        self.assertFalse(changed_archive.exists())
        self.assertTrue(settlement._path(self.project.root, result['run']).exists())

    def test_backup_refuses_broken_protected_evidence_root(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            self.run_role()
        run_root = self.project.root / 'runs'
        outside = self.root / 'protected-run-root'
        run_root.rename(outside)
        try:
            run_root.symlink_to('absent-runs', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create symbolic links')
        self.project.set_control(paused=True)
        archive = self.root / 'broken-run-root.tar.gz'

        with self.assertRaisesRegex(Denied, 'unsafe evidence directory'):
            backup(self.project, archive)

        self.assertFalse(archive.exists())
        self.assertTrue(run_root.is_symlink())
        self.assertTrue(outside.is_dir())

    def test_backup_refuses_unsafe_referenced_run_and_shared_record_members(self):
        with patch('mizu.runtime.proposal_waits.complete', side_effect=OSError('receipt failed')):
            result = self.run_role()
        run = result['run']
        row = settlement._read(self.project.root, run)
        store = shared_records.SharedRecords(self.project.root)
        record = self.project.root / store._record_name(row['manifests']['operator'][0]['record_id'])
        run_link = self.project.root / 'runs' / run / 'pinned-evidence-link'
        self.project.set_control(paused=True)
        cases = ((run_link, self.root / 'run-link-target', 'run-link.tar.gz'),
                 (record, self.root / 'record-link-target', 'record-link.tar.gz'))
        for link, target, archive_name in cases:
            with self.subTest(member=link.name):
                try:
                    target.write_text('target stays outside archive', encoding='utf-8')
                    if link == record:
                        target.write_bytes(record.read_bytes())
                        record.unlink()
                    link.symlink_to(target)
                except (OSError, NotImplementedError):
                    self.skipTest('Host cannot create symbolic links')
                archive = self.root / archive_name
                try:
                    if link == record:
                        with self.assertRaises(Denied):
                            backup(self.project, archive)
                    else:
                        with self.assertRaisesRegex(Denied, 'unsafe evidence member'):
                            backup(self.project, archive)
                    self.assertFalse(archive.exists())
                    self.assertTrue(link.is_symlink())
                finally:
                    link.unlink(missing_ok=True)
                    target.unlink(missing_ok=True)
