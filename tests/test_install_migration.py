"""Promotion against synthetic projects and staged metadata only."""
import hashlib
import importlib.util
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
        write_json(self.release / 'installation.json', dict(validation='local-install-checks-passed',
                   mode='core-only', source_sha256=digest(canonical(manifest))))
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
        with (patch.object(storage, 'backup') as backup, patch.object(record_migration, 'apply') as records,
              patch.object(work_item_migration, 'apply') as work):
            result = self.promote()
        backup.assert_not_called()
        records.assert_not_called()
        work.assert_not_called()
        self.assertEqual(result['migrations'], [])
        self.assertIn('data, side effects and budgets are unchanged', result['note'])
        self.assertFalse(self.backups.exists())

    def test_flat_records_and_legacy_resolution_share_one_restorable_backup(self):
        flat, sharded, flat_raw = self.flat_record()
        old, current, old_raw = self.legacy_resolution()
        records_apply, work_apply = record_migration.apply, work_item_migration.apply
        link_atomically = INSTALLER.link_atomically

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
              patch.object(INSTALLER, 'link_atomically', side_effect=link)):
            result = self.promote()
        row, = result['migrations']
        self.assertIn('converted after verified backups', result['note'])
        self.assertNotIn('Only code', result['note'])
        self.assertEqual(result['status'], 'promoted')
        self.assertEqual(row['status'], 'converted')
        self.assertEqual(read_json(Path(row['receipt'])), row)
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
