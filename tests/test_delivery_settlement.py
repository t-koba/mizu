"""Exact delivery lifetimes; offline stores and synthetic drivers, not OCI proof."""
import dataclasses
import os
import time
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver
from mizu import ci_waits, delivery_settlement as settlement, storage_runs
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import ATOMIC_WRITE_TEMP_PREFIX, now, write_json
from mizu.project import Project
from mizu.publication import Publication
from mizu.runtime import Engine
from mizu.storage import backup, prune


class DeliverySettlementTests(Fixture):
    def setUp(self):
        super().setUp()
        roles = dict(self.config.roles)
        roles['reviewer'] = dataclasses.replace(roles['reviewer'], on_change=False)
        self.config = dataclasses.replace(self.config, roles=roles,
            vcs={**self.config.vcs, 'poll_enabled': True, 'command': ['configured-adapter']})
        self.project = Project(self.config, 'sample')
        publication = Publication(self.project.root, 'a' * 40, 'c' * 64, 'published', 'push',
                                  projection=public_source_identity({}))
        intent, _ = publication.prepare('approval', 'run')
        publication.mark_dispatch()
        publication.complete(intent, 'b' * 40)
        store = self.project.work_items
        seed = store.create(actor='reviewer', idempotency_key='ci', title='ci')['record']['work_item']
        store.transition(seed['id'], expected_rev=1, status='blocked', actor='reviewer',
                         wait={'kind': 'ci', 'publication_id': publication.id, 'checks': ['unit']})
        ci_waits.poll(self.config, self.project, {}, now=1000, interval=15,
            reader=Mock(return_value={'checks': [{'check': 'unit', 'sha': 'b' * 40,
                                                  'state': 'success', 'run': 'unit-run'}]}))
        self.contexts = []

    def driver(self, ctx, prompt, profile=None):
        self.contexts.append(ctx)
        row = settlement._read(self.project.root, ctx.run_dir.name)
        self.assertFalse(row['closed'])
        self.assertTrue(all(row['manifests'].values()))
        tokens = [entry.ack_token for entry in ctx.ci_wait_manifest]
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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

    def test_backup_refuses_broken_protected_evidence_root(self):
        with patch('mizu.runtime.ci_waits.complete', side_effect=OSError('receipt failed')):
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

