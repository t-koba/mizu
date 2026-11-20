"""Synthetic evidence only: no provider calls or installed/private state."""
import datetime as dt
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu import smoke_retention as retention
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import write_json
from mizu.ledger import Ledger
from mizu.smoke_refs import shared_lock
from mizu.work_items import WorkItems


class SmokeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data / 'validation').mkdir()
        self.moment = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        self.a, self.b = 'a' * 32, 'b' * 32

    def pair(self, key=None, *, age=32, project=False, status='pass'):
        key = key or self.a
        tree = None
        if project:
            tree = self.scratch_project(key)
            (tree / 'probe.txt').write_text('synthetic')
        report = retention.publish(self.data, key, {'status': status,
            'time': (self.moment - dt.timedelta(days=age)).isoformat(), 'result': {'synthetic': True}}, tree)
        return Path(report['pair'])

    def scratch_project(self, key):
        root = self.data / 'validation' / 'smoke-runs' / key / 'projects' / ('smoke-' + key)
        root.mkdir(parents=True)
        return root

    def owner(self, name='one'):
        root = self.data / 'projects' / name
        root.mkdir(parents=True)
        items = WorkItems(root, None, Ledger(root))
        return items, items

    def insight(self, store, refs=None):
        """A live record that pins ``refs``: an open work item carrying them."""
        import uuid
        return store.create(idempotency_key=uuid.uuid4().hex, title='A topic', actor='worker',
                            smoke_refs=refs)['record']['work_item']

    def run_retention(self, **kwargs):
        return retention.run(self.data, moment=self.moment, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.data).as_posix():
                ('dir' if p.is_dir() else p.read_bytes()) for p in self.data.rglob('*')}

    def test_publish_refuses_ids_reserved_by_quarantine_recovery(self):
        for smoke_id, state in ((self.a, 'pending'), (self.b, 'committed')):
            with self.subTest(state=state):
                self.pair(smoke_id)
                quarantine = self.data / 'validation' / 'quarantine'
                quarantine.mkdir(exist_ok=True)
                os.rename(self.data / 'validation' / 'pairs' / smoke_id,
                          quarantine / f'{smoke_id}.{state}')
                before = self.snapshot()
                report = {'status': 'pass', 'time': self.moment.isoformat(),
                          'result': {'replacement': True}}
                with self.assertRaises(Denied):
                    retention.publish(self.data, smoke_id, report, None)
                self.assertEqual(self.snapshot(), before)

    def test_pending_admission_refuses_existing_quarantine_without_moving_pair(self):
        pair = self.pair()
        identity = retention.project_identity(pair)
        quarantine = self.data / 'validation' / 'quarantine'
        quarantine.mkdir()
        marker = quarantine / 'unrecognized'
        marker.write_text('untrusted')
        with self.assertRaises(Busy):
            retention.admit_retirement_candidates(self.data, {self.a: identity})
        self.assertTrue(pair.is_dir())
        self.assertEqual(marker.read_text(), 'untrusted')

    def test_crash_after_candidate_admission_recovers_pending_pair(self):
        pair = self.pair()
        rename = os.rename

        def die_after_admission(source, destination):
            result = rename(source, destination)
            if (Path(source).parent.name == 'pairs'
                    and Path(destination).name == self.a + '.pending'):
                raise KeyboardInterrupt('synthetic process death after admission')
            return result

        with patch.object(retention.os, 'rename', side_effect=die_after_admission):
            with self.assertRaises(KeyboardInterrupt):
                self.run_retention(apply=True)
        self.assertFalse(pair.exists())
        pending = self.data / 'validation/quarantine' / (self.a + '.pending')
        self.assertTrue(pending.is_dir())
        result = self.run_retention(apply=True)
        self.assertEqual(result['recovered'], [self.a])
        self.assertEqual(result['retired'], [])
        self.assertTrue(pair.is_dir())

    def test_pending_recovery_charges_its_report_once(self):
        pair = self.pair()
        qroot = self.data / 'validation/quarantine'
        qroot.mkdir()
        pending = qroot / (self.a + '.pending')
        pair.rename(pending)
        report_bytes = (pending / 'report.json').stat().st_size
        result = self.run_retention(apply=True,
                                    budgets=retention.Budgets(report_bytes=report_bytes))
        self.assertTrue(result['complete'])
        self.assertEqual(result['recovered'], [self.a])
        self.assertEqual(result['report_bytes'], report_bytes)
        self.assertTrue(pair.is_dir())

    def test_crash_after_commit_rename_finishes_only_committed_deletion(self):
        pair = self.pair()
        rename = os.rename

        def die_after_commit(source, destination):
            result = rename(source, destination)
            if Path(destination).name == self.a + '.committed':
                raise KeyboardInterrupt('synthetic process death after commit')
            return result

        with patch.object(retention.os, 'rename', side_effect=die_after_commit):
            with self.assertRaises(KeyboardInterrupt):
                self.run_retention(apply=True)
        self.assertFalse(pair.exists())
        committed = self.data / 'validation/quarantine' / (self.a + '.committed')
        self.assertTrue(committed.is_dir())
        result = self.run_retention(apply=True)
        self.assertEqual(result['recovered'], [self.a])
        self.assertTrue(result['complete'])
        self.assertFalse(committed.exists())
        self.assertFalse(pair.exists())

    def test_atomic_pair_identity_and_project(self):
        project = self.scratch_project(self.a)
        (project / 'probe.txt').write_text('synthetic')
        identity = retention.project_identity(project)
        report = retention.publish(self.data, self.a, {'status': 'pass',
            'time': self.moment.isoformat(), 'result': {'synthetic': True}}, project)
        pair = Path(report['pair'])
        manifest = retention.read_pair(retention.Metadata(self.data), self.a)
        self.assertEqual(manifest['project'], identity)
        self.assertEqual((pair / 'report.json').read_bytes(), retention.canonical(report))
        self.assertEqual(set(p.name for p in pair.iterdir()), {'manifest.json', 'report.json', 'project'})
        self.assertEqual(manifest['smoke_id'], self.a)
        self.assertEqual((pair / 'project' / 'probe.txt').read_text(), 'synthetic')
        self.assertFalse(project.exists())
        (pair / 'report.json').write_text('{}')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_publish_does_not_stage_on_busy_and_same_id_retry_succeeds(self):
        project = self.scratch_project(self.a)
        report = {'status': 'pass', 'time': self.moment.isoformat()}
        with shared_lock(self.data):
            with self.assertRaises(Busy):
                retention.publish(self.data, self.a, report, project)
        stage = self.data / 'validation' / 'staging' / self.a
        self.assertTrue(project.is_dir())
        self.assertFalse(stage.exists())
        result = retention.publish(self.data, self.a, report, project)
        self.assertTrue(Path(result['pair']).is_dir())
        self.assertFalse(project.exists())

    def test_publish_refuses_managed_and_mismatched_scratch_roots(self):
        managed = self.data / 'projects' / ('smoke-' + self.a)
        managed.mkdir(parents=True)
        scratch = self.scratch_project(self.b)
        wrong_name = scratch.parent / ('smoke-' + self.a)
        wrong_name.mkdir()
        report = {'status': 'pass', 'time': self.moment.isoformat()}
        for project in (managed, scratch, wrong_name):
            with self.subTest(project=project), self.assertRaisesRegex(Denied, 'isolated smoke scratch layout'):
                retention.publish(self.data, self.a, report, project)
            self.assertTrue(project.is_dir())
            self.assertEqual(list(project.iterdir()), [])
        self.assertFalse((self.data / 'validation/staging' / self.a).exists())
        self.assertFalse((self.data / 'validation/pairs' / self.a).exists())

    def test_publish_refuses_scratch_alias_to_managed_projects(self):
        managed = self.data / 'projects'
        (managed / ('smoke-' + self.a)).mkdir(parents=True)
        scratch = self.data / 'validation/smoke-runs' / self.a
        scratch.mkdir(parents=True)
        try:
            (scratch / 'projects').symlink_to(managed, target_is_directory=True)
        except OSError:
            self.skipTest('directory symlinks unavailable')
        with self.assertRaisesRegex(Denied, 'Unsafe smoke retention directory'):
            retention.publish(self.data, self.a, {'status': 'pass',
                'time': self.moment.isoformat()}, scratch / 'projects' / ('smoke-' + self.a))
        self.assertTrue((managed / ('smoke-' + self.a)).is_dir())

    def test_publish_refuses_preexisting_staging_without_mutation(self):
        stage = self.data / 'validation' / 'staging' / self.a
        stage.mkdir(parents=True)
        evidence = stage / 'report.json'
        evidence.write_bytes(b'suspect staging evidence')
        before = evidence.read_bytes()
        with self.assertRaises(Denied):
            retention.publish(self.data, self.a, {'status': 'pass',
                'time': self.moment.isoformat()}, None)
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual({path.name for path in stage.iterdir()}, {'report.json'})

    def test_publish_is_one_directory_rename_and_failure_never_partial_pair(self):
        rename = os.rename
        observed = []
        def observe(source, destination):
            if Path(destination).parent.name == 'pairs':
                observed.append(set(p.name for p in Path(source).iterdir()))
                self.assertFalse(Path(destination).exists())
            return rename(source, destination)
        with patch.object(retention.os, 'rename', side_effect=observe):
            self.pair(project=True)
        self.assertEqual(observed, [{'manifest.json', 'report.json', 'project'}])
        with patch.object(retention.os, 'rename', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):
                self.pair(self.b)
        stage = self.data / 'validation' / 'staging' / self.b
        self.assertFalse((self.data / 'validation/pairs' / self.b).exists())
        self.assertFalse(stage.exists())

        project = self.scratch_project(self.b)
        (project / 'probe.txt').write_text('synthetic')
        report = {'status': 'pass', 'time': self.moment.isoformat()}
        def fail_pair(source, destination):
            if Path(destination).parent.name == 'pairs':
                raise OSError('synthetic final rename failure')
            return rename(source, destination)
        with patch.object(retention.os, 'rename', side_effect=fail_pair):
            with self.assertRaises(OSError):
                retention.publish(self.data, self.b, report, project)
        self.assertTrue(project.is_dir())
        self.assertEqual((project / 'probe.txt').read_text(), 'synthetic')
        self.assertFalse(stage.exists())
        self.assertFalse((self.data / 'validation/pairs' / self.b).exists())
        result = retention.publish(self.data, self.b, report, project)
        self.assertTrue(Path(result['pair']).is_dir())
        self.assertFalse(project.exists())

    def test_default_preview_is_immutable_and_legacy_is_untouched(self):
        pair = self.pair()
        legacy = self.data / 'validation/smoke-0123456789abcdef.json'
        legacy.write_text('legacy')
        (legacy.parent / 'smoke-0123456789abcdef.project').mkdir()
        (legacy.parent / 'smoke-probe-policy.md').write_text('fixed')
        before = self.snapshot()
        result = self.run_retention()
        self.assertEqual(result['days'], 31)
        self.assertEqual(result['candidates'], [self.a])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.run_retention(apply=True)['retired'], [self.a])
        self.assertFalse(pair.exists())
        self.assertEqual(legacy.read_text(), 'legacy')
        self.assertTrue((legacy.parent / 'smoke-0123456789abcdef.project').is_dir())
        self.assertEqual((legacy.parent / 'smoke-probe-policy.md').read_text(), 'fixed')




    def test_same_status_ref_update_is_a_cas_revision(self):
        self.pair()
        _, work = self.owner()
        item = work.create(idempotency_key='same-status', title='Task', actor='worker')['record']['work_item']
        added = work.transition(item['id'], expected_rev=1, status='open', actor='worker', smoke_refs=[self.a])
        self.assertEqual(added['rev'], 2)
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        work.transition(item['id'], expected_rev=2, status='open', actor='worker', smoke_refs=[])
        self.assertEqual(self.run_retention()['candidates'], [self.a])
        with self.assertRaises(Denied):
            work.transition(item['id'], expected_rev=3, status='open', actor='worker', smoke_refs=[])
        wait = {'kind': 'deadline', 'at': '2027-01-01T00:00:00+00:00'}
        work.transition(item['id'], expected_rev=3, status='blocked', actor='worker', wait=wait)
        work.transition(item['id'], expected_rev=4, status='blocked', actor='worker',
                        wait={**wait, 'smoke_refs': [self.a]})
        self.assertEqual(self.run_retention()['pinned'], [self.a])

    def test_policy_age_and_zero(self):
        self.pair(age=31); self.pair(self.b, age=-1)
        self.assertEqual(self.run_retention()['candidates'], [])
        retention.set_days(self.data, 30)
        self.assertEqual(self.run_retention()['candidates'], [self.a])
        retention.set_days(self.data, 0)
        # Zero skips even invalid new-expiry metadata.
        (self.data / 'validation/pairs' / self.a / 'manifest.json').write_text('bad')
        self.assertEqual(self.run_retention(apply=True)['retired'], [])
        for bad in ({'version': 2, 'days': 31}, {'version': 1, 'days': True},
                    {'version': 1, 'days': -1}, {'version': 1, 'days': 36501}, {'version': 1, 'days': 31, 'extra': 1}):
            write_json(self.data / 'validation/retention-policy.json', bad)
            with self.assertRaises(Denied):
                self.run_retention(apply=True)

    def test_metadata_budget_is_shared_across_policy_and_pair_scan(self):
        pair = self.pair()
        retention.set_days(self.data, 31)
        policy_size = (self.data / 'validation/retention-policy.json').stat().st_size
        manifest_size = (pair / 'manifest.json').stat().st_size
        bound = max(policy_size, manifest_size)
        self.assertGreater(policy_size + manifest_size, bound)
        with self.assertRaises(LimitExceeded):
            self.run_retention(apply=True, budgets=retention.Budgets(metadata_bytes=bound))
        self.assertTrue(pair.exists())

    def test_report_payload_is_not_charged_to_metadata_budget(self):
        self.pair()
        retention.set_days(self.data, 31)
        preview = self.run_retention()
        self.assertGreater(preview["report_bytes"], 0)
        bound = preview["metadata_bytes"]
        self.assertEqual(
            self.run_retention(budgets=retention.Budgets(metadata_bytes=bound))["candidates"],
            [self.a])
        with self.assertRaises(LimitExceeded):
            self.run_retention(budgets=retention.Budgets(metadata_bytes=bound - 1))

    def test_published_and_pending_report_bytes_are_charged_once(self):
        published = self.pair(self.a)
        pending_source = self.pair(self.b)
        quarantine = self.data / 'validation' / 'quarantine'
        quarantine.mkdir()
        pending = quarantine / (self.b + '.pending')
        os.rename(pending_source, pending)
        bound = sum((pair / 'report.json').stat().st_size
                    for pair in (published, pending))
        result = self.run_retention(budgets=retention.Budgets(report_bytes=bound))
        self.assertEqual(result['report_bytes'], bound)

    def test_published_and_committed_quarantine_report_bytes_are_charged_once(self):
        published = self.pair(self.a)
        committed_source = self.pair(self.b)
        quarantine = self.data / 'validation' / 'quarantine'
        quarantine.mkdir()
        committed = quarantine / (self.b + '.committed')
        os.rename(committed_source, committed)
        bound = sum((pair / 'report.json').stat().st_size
                    for pair in (published, committed))
        result = self.run_retention(budgets=retention.Budgets(report_bytes=bound))
        self.assertEqual(result['report_bytes'], bound)

    def test_scan_budgets_follow_limits_and_default_to_current_values(self):
        from mizu.config import Limits
        self.assertEqual(retention.Budgets.from_limits(Limits()), retention.Budgets())
        self.assertEqual(retention.Budgets.from_limits(None), retention.Budgets())
        self.assertEqual(retention.Budgets().projects, 5000)
        self.assertEqual(retention.Budgets().pairs, 5000)
        self.assertEqual(retention.Budgets().refs, 20000)
        self.assertEqual(retention.Budgets().entries, 100000)
        self.assertEqual(retention.Budgets().metadata_bytes, 64 * 1024 * 1024)
        self.assertEqual(retention.Budgets().report_bytes, 5000 * 16 * 1024 * 1024)
        for limits in (SimpleNamespace(smoke_retention_pairs=0),
                       SimpleNamespace(smoke_retention_entries=True),
                       SimpleNamespace(smoke_retention_metadata_bytes='1'),
                       SimpleNamespace(smoke_retention_report_bytes=0)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                retention.Budgets.from_limits(limits)

    def test_malformed_missing_oversized_and_unknown_manifests(self):
        pair = self.pair()
        path = pair / 'manifest.json'
        original = path.read_bytes()
        for content in (b'{}', b'{', b'x'*4097, original.replace(b'"version":1', b'"version":2'),
                        original.replace(b'+00:00', b''), original.replace(b'"pass"', b'"running"')):
            path.write_bytes(content)
            with self.assertRaises((Denied, LimitExceeded)):
                self.run_retention(apply=True)
            self.assertTrue(pair.exists())
        path.unlink()
        with self.assertRaises((Denied, FileNotFoundError)):
            self.run_retention(apply=True)


    def test_unrecognized_committed_directory_is_preserved(self):
        self.pair()
        qroot = self.data / 'validation/quarantine'
        qroot.mkdir()
        committed = qroot / (self.a + '.committed')
        committed.mkdir()
        payload = committed / 'operator-data.txt'
        payload.write_text('not a retention commit')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertEqual(payload.read_text(), 'not a retention commit')

    def test_work_item_transition_inherits_or_explicitly_releases_refs(self):
        self.pair()
        _, store = self.owner()
        created = store.create(idempotency_key='refs', title='Pinned', actor='worker',
                               smoke_refs=[self.a])
        item_id = created['record']['work_item']['id']
        doing = store.transition(item_id, expected_rev=1, status='doing', actor='worker')
        self.assertEqual(doing['smoke_refs'], [self.a])
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        opened = store.transition(item_id, expected_rev=2, status='open', actor='worker',
                                  smoke_refs=[])
        self.assertEqual(opened['smoke_refs'], [])
        self.assertEqual(self.run_retention()['candidates'], [self.a])

    def test_precommit_interruption_rolls_back_on_next_apply(self):
        pair = self.pair()
        rename = os.rename
        def interrupt(source, destination):
            if str(destination).endswith('.committed'):
                raise KeyboardInterrupt('synthetic process death before commit')
            return rename(source, destination)
        with patch.object(retention.os, 'rename', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_retention(apply=True)
        self.assertFalse(pair.exists())
        result = self.run_retention(apply=True)
        self.assertEqual(result['recovered'], [self.a])
        self.assertEqual(result['retired'], [])
        self.assertTrue(pair.exists())

    def test_policy_and_pair_directory_links_are_refused(self):
        pair = self.pair()
        original = self.data / 'validation/policy-original.json'
        write_json(original, {'version': 1, 'days': 31})
        policy = self.data / 'validation/retention-policy.json'
        try:
            policy.symlink_to(original)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        policy.unlink()
        moved = pair.parent / 'moved'
        pair.rename(moved); pair.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(moved.is_dir())


    def test_audit_is_bounded_and_failure_reports_deleted_payload_honestly(self):
        self.pair()
        with patch.object(retention, 'audit', side_effect=OSError('synthetic audit failure')):
            result = self.run_retention(apply=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['retired'], [self.a])
        self.assertEqual(result['failed_id'], self.a)
        self.assertFalse((self.data / 'validation/retention-audit.json').exists())
        with patch.object(retention, 'AUDIT_LIMIT', 2):
            for key in ('a'*32, 'b'*32, 'c'*32):
                retention.audit(self.data, key, 'deleted')
            self.assertEqual(len(retention.audit_rows(self.data)), 2)
        self.assertEqual([row['smoke_id'] for row in retention.audit_rows(self.data)], ['b'*32, 'c'*32])

    def test_duplicate_policy_fields_and_contradictory_quarantine_refuse(self):
        pair = self.pair()
        path = self.data / 'validation/retention-policy.json'
        path.write_text('{"version":1,"days":31,"days":31}')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        path.unlink()
        qroot = self.data / 'validation/quarantine'; qroot.mkdir()
        pair.rename(qroot / (self.a + '.pending'))
        (qroot / (self.a + '.committed')).mkdir()
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue((qroot / (self.a + '.pending')).is_dir())

    def test_partial_io_failure_is_not_success_and_is_recoverable(self):
        pair = self.pair(); other = self.pair(self.b)
        original_unlink = Path.unlink
        def fail_report_unlink(path, *args, **kwargs):
            if path.name == 'report.json' and path.parent.name.endswith('.committed'):
                raise OSError('synthetic I/O failure')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_report_unlink):
            result = self.run_retention(apply=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['retired'], [])
        self.assertTrue(other.exists())
        self.assertFalse(pair.exists())
        self.assertFalse((self.data / 'validation/retention-audit.json').exists())
        result = self.run_retention(apply=True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['recovered'], [self.a])
        self.assertEqual(result['retired'], [self.b])


if __name__ == '__main__':
    unittest.main()
