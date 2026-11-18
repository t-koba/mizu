"""Promotion against synthetic projects and staged metadata only."""
import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture, ROOT
from mizu import install_migration, operator_records, record_migration, storage, work_item_migration
from mizu.errors import Denied
from mizu.fs import canonical, digest, held, lock, read_json, write_json

spec = importlib.util.spec_from_file_location('migration_installer', ROOT / 'scripts/install.py')
INSTALLER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(INSTALLER)


class InstallMigrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project.set_control(paused=True)
        self.args = SimpleNamespace(prefix=self.root / 'prefix', bin_dir=self.root / 'bin', config=self.file)
        self.release = self.args.prefix / 'releases' / 'candidate'
        self.marker = self.release / 'src/mizu/install_migration.py'
        self.marker.parent.mkdir(parents=True)
        self.marker.touch()
        self.backups = self.config.data / 'migration-backups'
        self.workspace_lock = self.project.root / 'locks/workspace.lock'
        # Force a real overlap with stopped() even when no run has used this project.
        with lock(self.workspace_lock):
            pass

    def promote(self):
        manifest = INSTALLER.source_manifest(self.release)
        write_json(self.release / 'source-manifest.json', manifest)
        write_json(self.release / 'installation.json',
                   dict(mode='core-only', source_sha256=digest(canonical(manifest))))
        with (patch.object(INSTALLER, 'command'),
              patch.object(INSTALLER, 'active_service_units', return_value=[])):
            return INSTALLER.promote(self.args, self.release)

    def flat_record(self):
        record = operator_records.write(self.project, event_id='notice', kind='operator.notice',
                                        body='Notice', addressee='worker')['record']
        current = self.project.root / operator_records.store(self.project).record_name(record['id'])
        flat = self.project.root / 'shared-records' / f"{digest(record['id'].encode())}.json"
        current.rename(flat)
        return flat, current, canonical(record)

    def tracked_preflight(self):
        calls = []
        real = storage.preflight_restore_archive

        def record(config, archive):
            calls.append((config, archive))
            return real(config, archive)

        return calls, record

    def legacy_resolution(self):
        event_id = '1' * 32
        write_json(self.project.root / 'decision-history' / f'{event_id}.json',
                   dict(id='synthetic-insight', action='accept', rev=1, reason='Legacy assessment',
                        revisit='', run='test-run', created_at='2026-01-01T00:00:00+00:00'))
        self.project.work_items.reconcile(event_id, outcome='drop', reason='No action', actor='operator')
        current = self.project.root / 'work-items' / f'wi_{event_id}' / 'decision-resolution.json'
        old = current.with_name('legacy.json')
        raw = current.read_bytes()
        current.rename(old)
        return old, current, raw

    def test_no_affected_projects_does_not_back_up_or_apply(self):
        for prepare in (self.flat_record, self.legacy_resolution):
            old, current, _ = prepare()
            old.rename(current)
        insights = self.project.insights
        live = insights.submit(source='reviewer', title='Pending finding', body='Synthetic finding',
                               base_snapshot=None)
        decided = insights.submit(source='reviewer', title='Decided finding', body='Synthetic finding',
                                  base_snapshot=None)
        insights.decide(decided['id'], 'reject', 'No action', 'New evidence', 'test-run', expected_rev=1)
        event, = insights.decision_events('reviewer', ['reject'])
        insights.acknowledge_decisions('reviewer', [event])
        identity = self.project.root / 'insight-ids' / f"{live['id']}.json"
        receipt = self.project.root / 'decision-receipts/reviewer' / f"{event['ack_token']}.json"
        source_bytes = {path: path.read_bytes() for path in (identity, receipt)}
        with (patch.object(storage, 'backup') as backup, patch.object(record_migration, 'apply') as records,
              patch.object(work_item_migration, 'apply') as work):
            result = self.promote()
        backup.assert_not_called()
        records.assert_not_called()
        work.assert_not_called()
        self.assertEqual(result['status'], 'promoted')
        self.assertEqual(result['migrations'], [])
        self.assertEqual(set(result['uncovered_families']),
                         {'insight-identities-and-tombstones', 'decision-receipts'})
        for path, raw in source_bytes.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertIn('data, side effects and budgets are unchanged', result['note'])
        self.assertFalse(self.backups.exists())

    def test_flat_records_and_legacy_resolution_share_one_restorable_backup(self):
        flat, sharded, flat_raw = self.flat_record()
        old, current, old_raw = self.legacy_resolution()
        records_apply, work_apply = record_migration.apply, work_item_migration.apply
        link_atomically = INSTALLER.link_atomically
        calls, record_preflight = self.tracked_preflight()
        def link(destination, target):
            self.assertTrue(held(self.workspace_lock))
            self.assertFalse(flat.exists())
            self.assertFalse(old.exists())
            return link_atomically(destination, target)

        def assert_backup(command, source, raw, apply, project):
            self.assertTrue(held(self.workspace_lock))
            archive, = self.backups.glob('*/*.tar.gz')
            self.assertNotIn(project.root, archive.parents)
            row = read_json(archive.with_suffix('.json'))
            self.assertEqual(row['conversions'][command]['status'], 'applying')
            self.assertEqual(row['archive'], str(archive))
            with tarfile.open(archive, 'r:gz') as saved:
                with saved.extractfile(source.relative_to(project.root).as_posix()) as member:
                    self.assertEqual(member.read(), raw)
            self.assertEqual(source.read_bytes(), raw)
            return apply(project)

        with (patch.object(record_migration, 'apply', side_effect=lambda p: assert_backup(
                  'migrate-records', flat, flat_raw, records_apply, p)),
              patch.object(work_item_migration, 'apply', side_effect=lambda p: assert_backup(
                  'migrate-work-items', old, old_raw, work_apply, p)),
              patch.object(INSTALLER, 'link_atomically', side_effect=link),
              patch.object(storage, 'preflight_restore_archive', side_effect=record_preflight)):
            result = self.promote()
        row, = result['migrations']
        archive, = self.backups.glob('*/*.tar.gz')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], self.config)
        self.assertEqual(calls[0][1], archive)
        self.assertIn('Listed migrations were applied after verified backups', result['note'])
        self.assertNotIn('Only code', result['note'])
        self.assertEqual(result['status'], 'promoted')
        self.assertEqual(set(result['uncovered_families']),
                         {'insight-identities-and-tombstones', 'decision-receipts'})
        self.assertEqual(row['status'], 'converted')
        receipt = read_json(Path(row['receipt']))
        self.assertEqual(receipt, row)
        self.assertEqual(set(receipt['conversions']), {'migrate-records', 'migrate-work-items'})
        archive = Path(row['archive'])
        self.assertEqual(row['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        for path, destination, raw, command in ((flat, sharded, flat_raw, 'migrate-records'),
                                                 (old, current, old_raw, 'migrate-work-items')):
            self.assertFalse(path.exists())
            self.assertEqual(destination.read_bytes(), raw)
            self.assertTrue(row['conversions'][command]['ok'])
            self.assertEqual(row['conversions'][command]['converted'], 1)
        storage.restore(self.config, 'restored', archive)
        for path, raw in ((flat, flat_raw), (old, old_raw)):
            self.assertEqual((self.project.root.with_name('restored') /
                              path.relative_to(self.project.root)).read_bytes(), raw)

    def test_historical_object_gap_denies_promotion_before_conversion(self):
        workspace_file = self.project.workspace / 'app.py'
        first = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state='first snapshot', run=None,
            outcome='wait', summary='First synthetic snapshot')
        self.project.snapshots.publish(first)
        workspace_file.write_bytes(b'VALUE = 99\n')
        second = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state='second snapshot', run=None,
            outcome='wait', summary='Second synthetic snapshot')
        self.project.snapshots.publish(second)
        old_object = first['files']['app.py']['sha256']
        self.assertNotEqual(old_object, second['files']['app.py']['sha256'])
        missing = self.project.root / 'objects' / old_object
        self.assertTrue(missing.is_file())
        missing.unlink()  # Now referenced only by the retained older snapshot.
        old, current, old_raw = self.legacy_resolution()
        calls, record_preflight = self.tracked_preflight()
        with (patch.object(storage, 'preflight_restore_archive', side_effect=record_preflight),
              patch.object(work_item_migration, 'apply') as work,
              patch.object(record_migration, 'apply') as records,
              patch.object(INSTALLER, 'link_atomically') as link):
            with self.assertRaisesRegex(Denied, r'verified backup and receipt failed.*mizu migrate-work-items sample.*archive:') as raised:
                self.promote()
        self.assertIn(old_object, str(raised.exception))
        self.assertEqual(len(calls), 1)
        work.assert_not_called()
        records.assert_not_called()
        link.assert_not_called()
        self.assertTrue(old.is_file())
        self.assertFalse(current.exists())
        self.assertEqual(old.read_bytes(), old_raw)
        self.assertEqual(list(self.backups.glob('*/*.json')), [])
        staging = self.config.data / 'staging/projects'
        self.assertEqual(list(staging.glob('.restore-*')), [])
        self.assertEqual([p for p in (self.config.data / 'projects').iterdir()
                          if p.name.startswith('preflight-')], [])
        archive, = self.backups.glob('*/*.tar.gz')
        with tarfile.open(archive, 'r:gz') as saved:
            self.assertNotIn(f'objects/{old_object}', saved.getnames())

    def test_incomplete_scan_refuses_before_backup_or_any_conversion(self):
        flat, _, _ = self.flat_record()
        items = self.project.root / 'work-items'
        for name in ('wi_' + '1' * 32, 'wi_' + '2' * 32):
            (items / name).mkdir(parents=True)
        detect = work_item_migration.legacy_resolutions_present
        with (patch.object(work_item_migration, 'legacy_resolutions_present',
                           side_effect=lambda project: detect(project, scan_limit=1)),
              patch.object(storage, 'backup') as backup, patch.object(record_migration, 'apply') as records,
              patch.object(INSTALLER, 'link_atomically') as link):
            with self.assertRaisesRegex(Denied, r'entry bound.*mizu migrate-work-items sample.*archive:'):
                self.promote()
        backup.assert_not_called()
        records.assert_not_called()
        link.assert_not_called()
        self.assertTrue(flat.is_file())
        self.assertFalse(self.backups.exists())

    def test_failed_backup_or_receipt_refuses_before_conversion(self):
        flat, _, _ = self.flat_record()
        with (patch.object(record_migration, 'apply') as records, patch.object(INSTALLER, 'link_atomically') as link):
            with patch.object(storage, 'backup', return_value={'ok': False}) as backup:
                with self.assertRaisesRegex(Denied, r'backup.*mizu migrate-records sample.*archive:'):
                    self.promote()
                self.assertEqual(backup.call_args.kwargs, {'verify': True})
            with patch.object(install_migration, 'write_json', side_effect=OSError('receipt write failed')):
                with self.assertRaisesRegex(Denied, r'receipt write failed.*mizu migrate-records sample.*archive:'):
                    self.promote()
        records.assert_not_called()
        link.assert_not_called()
        self.assertTrue(flat.is_file())

    def test_unrestorable_archive_refuses_before_any_conversion(self):
        flat, _, _ = self.flat_record()
        # A literal backslash is a path separator on Windows, so a fixture
        # file cannot have that name there. Insert the member into the real
        # archive instead: backup verification and restore must both refuse
        # it on every platform, without depending on OS filename semantics.
        bad_name = 'sessions/worker/weird/a\\b.json'
        add = tarfile.TarFile.add
        inserted = []

        def add_unrestorable_member(archive, *args, **kwargs):
            result = add(archive, *args, **kwargs)
            if not inserted:
                info = tarfile.TarInfo(bad_name)
                payload = b'{}'
                info.size, info.mode = len(payload), 0o600
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                archive.addfile(info, io.BytesIO(payload))
                inserted.append(bad_name)
            return result

        calls, record_preflight = self.tracked_preflight()
        with (patch.object(record_migration, 'apply') as records,
              patch.object(INSTALLER, 'link_atomically') as link,
              patch.object(storage, 'preflight_restore_archive', side_effect=record_preflight),
              patch.object(tarfile.TarFile, 'add', add_unrestorable_member)):
            with self.assertRaisesRegex(Denied, r'verified backup and receipt failed.*Invalid relative path.*mizu migrate-records sample.*archive:'):
                self.promote()
        records.assert_not_called()
        link.assert_not_called()
        self.assertTrue(flat.is_file())
        archive, = self.backups.glob('*/*.tar.gz')
        self.assertEqual([call[1] for call in calls], [archive])
        self.assertEqual(list(self.backups.glob('*/*.json')), [])
        with tarfile.open(archive, 'r:gz') as saved:
            self.assertIn(bad_name, saved.getnames())

    def test_failed_apply_reports_partial_progress_and_retry_keeps_first_archive(self):
        flat, _, _ = self.flat_record()
        old, _, _ = self.legacy_resolution()
        with (patch.object(work_item_migration, 'apply', return_value={'ok': False, 'renamed': 1, 'converted': 0}),
              patch.object(INSTALLER, 'link_atomically') as link):
            with self.assertRaisesRegex(Denied, r'conversion failed.*mizu migrate-work-items sample.*archive:'):
                self.promote()
        link.assert_not_called()
        archive, = self.backups.glob('*/*.tar.gz')
        receipt = archive.with_suffix('.json')
        failed = read_json(receipt)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['conversions']['migrate-records']['status'], 'converted')
        self.assertEqual(failed['conversions']['migrate-work-items']['renamed'], 1)
        self.assertFalse(flat.exists())
        self.assertTrue(old.exists())
        retry, = self.promote()['migrations']
        self.assertNotEqual(retry['archive'], str(archive))
        self.assertEqual(read_json(receipt), failed)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), failed['sha256'])

    def test_older_candidate_does_not_trigger_conversion(self):
        flat, _, _ = self.flat_record()
        self.marker.unlink()
        self.assertEqual(self.promote()['migrations'], [])
        self.assertTrue(flat.is_file())
        self.assertFalse(self.backups.exists())
