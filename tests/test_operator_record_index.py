"""Bounded synthetic history: metadata is derived, canonical bytes authoritative."""
import copy
import shutil
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import operator_records as records, operator_record_index as index, shared_records
from mizu.errors import Denied
from mizu.fs import canonical, loads_finite
from mizu.storage import MEMBERS


class OperatorRecordIndexTests(Fixture):
    def notice(self, event='notice', role='reviewer', body='Notice', **kwargs):
        return records.write(self.project, event_id=event, kind='operator.notice',
                             addressee=role, body=body, **kwargs)

    def pending(self, role='reviewer'):
        return records.pending(self.project, self.config.roles[role])[0]

    def paths(self, result):
        shared = records.store(self.project)
        name = shared._record_name(result['record']['id'])
        return self.project.root / name, self.project.root / index.DIRECTORY / Path(name).name

    def test_unrelated_large_bodies_and_closure_targets_are_not_decoded(self):
        rows = [self.notice(f'large-{i}', body='"\\\n界' * 8192) for i in range(8)]
        self.notice('replacement', action='supersede', target={
            'id': rows[0]['record']['id'], 'digest': rows[0]['record_digest']})
        # A warmed query validates canonical bytes and metadata, not full body
        # JSON. No selected worker records, so freeze need not decode any either.
        decoded, read_ids = [], []
        def decode(raw):
            decoded.append(len(raw))
            return loads_finite(raw)
        original = shared_records.SharedRecords.get
        def get(shared, record_id, **kwargs):
            read_ids.append(record_id)
            return original(shared, record_id, **kwargs)
        with patch.object(index, 'loads_finite', side_effect=decode), \
             patch.object(shared_records, 'loads_finite', side_effect=decode), \
             patch.object(shared_records.SharedRecords, 'get', get):
            for _ in range(2):
                self.assertEqual(self.pending('worker')['pending_count'], 0)
        self.assertTrue(decoded)
        self.assertLess(max(decoded), 2048)
        self.assertEqual(read_ids, [])
        self.assertEqual(self.pending()['pending_count'], 8)

    def test_missing_refs_recover_and_retry_addressee_conflicts_globally(self):
        original = index.atomic_write
        with patch.object(index, 'atomic_write', side_effect=OSError('interrupted index write')):
            with self.assertRaises(OSError):
                self.notice(body='Retained')
        canonical_files = list((self.project.root / 'shared-records/operator').glob('*.json'))
        self.assertEqual(len(canonical_files), 1)
        retained = canonical_files[0].read_bytes()
        with self.assertRaises(Denied):
            self.notice(role='worker', body='Retained')
        self.assertEqual(canonical_files[0].read_bytes(), retained)
        result = self.notice(body='Retained')
        canonical_path, ref = self.paths(result)
        self.assertTrue(ref.is_file())
        ref.unlink()
        with patch.object(index, 'atomic_write', wraps=original) as repair:
            self.assertEqual(self.notice(body='Retained'), result)
            self.assertEqual(repair.call_count, 1)
        ref.unlink()
        # A killed atomic writer may leave unpublished scratch. It is not a
        # reference and must not prevent reconstruction of the missing ref.
        ref.with_name('.new-abcdefgh').write_bytes(b'partial')
        first = self.pending()
        self.assertTrue(ref.is_file())
        self.assertEqual(self.pending(), first)
        self.assertEqual(canonical_path.read_bytes(), retained)
        self.assertEqual(len(canonical_files), 1)

    def test_metadata_corruption_never_redirects_or_repairs(self):
        row = self.notice()
        _, path = self.paths(row)
        good = path.read_bytes()
        mutations = [lambda r: r.update(extra=True), lambda r: r.update(digest='0' * 64),
            lambda r: r['envelope']['payload'].update(addressee='worker'),
            lambda r: r['envelope']['payload'].update(action='withdraw'),
            lambda r: r['envelope']['payload'].update(body='Not a projection'),
            lambda r: r['envelope'].update(id='operator/sample/different'),
            lambda r: r['envelope'].update(schema=True),
            lambda r: r['envelope']['payload'].update(target={'id': 'operator/sample/other',
                                                           'digest': '0' * 64})]
        for mutate in mutations:
            ref = loads_finite(good)
            mutate(ref)
            bad = canonical(ref)
            path.write_bytes(bad)
            with self.subTest(ref=ref), self.assertRaises(Denied):
                self.pending('worker')
            with self.assertRaises(Denied):
                self.notice()
            self.assertEqual(path.read_bytes(), bad)
        for bad in (b'{}', b'{', good[:-2] + b',"digest":"' + b'0' * 64 + b'"}\n'):
            path.write_bytes(bad)
            with self.assertRaises(Denied):
                self.pending()
        path.write_bytes(good)
        self.assertEqual(self.pending()['pending_count'], 1)

    def test_dangling_duplicate_and_unsafe_refs(self):
        row = self.notice()
        _, ref = self.paths(row)
        duplicate = ref.with_name('0' * 64 + '.json')
        duplicate.write_bytes(ref.read_bytes())
        with self.assertRaises(Denied):
            self.pending()
        duplicate.unlink()
        unsafe = ref.with_name('unexpected')
        unsafe.mkdir()
        with self.assertRaises(Denied):
            self.pending()
        unsafe.rmdir()
        raw = ref.read_bytes()
        ref.unlink()
        target = self.root / 'external-index.json'
        target.write_bytes(raw)
        try:
            ref.symlink_to(target)
        except (OSError, NotImplementedError):
            return  # Symlink creation is unavailable on some supported hosts.
        with self.assertRaises(Denied):
            self.pending()

    def test_canonical_corruption_and_filename_completeness(self):
        row = self.notice(body='Before')
        path, ref = self.paths(row)
        good = path.read_bytes()
        path.write_bytes(good.replace(b'Before', b'After!'))
        with self.assertRaises(Denied):
            self.pending('worker')
        path.write_bytes(good)
        path.unlink()
        with self.assertRaises(Denied):
            self.pending()
        path.write_bytes(good)
        stray = path.with_name('unexpected.json')
        stray.write_bytes(good)
        with self.assertRaises(Denied):
            self.pending()
        stray.unlink()
        # Missing refs are recovered, never silently omitted.
        ref.unlink()
        self.assertEqual(self.pending()['pending_count'], 1)

    def test_flat_legacy_replay_other_namespaces_and_duplicates(self):
        row = self.notice()
        path, ref = self.paths(row)
        shared = records.store(self.project)
        legacy = self.project.root / shared._legacy_record_name(row['record']['id'])
        path.rename(legacy)
        ref.unlink()
        # A flat file from another source must not be mistaken for an operator
        # event, but its filename/content identity must still be checked.
        other = copy.deepcopy(row['record'])
        other.update(id='decision/sample/event', kind='insight.decision', payload={'body': 'x' * 32000})
        other_path = self.project.root / shared._legacy_record_name(other['id'])
        other_path.write_bytes(canonical(other))
        self.assertEqual(self.pending()['pending_count'], 1)
        self.assertEqual(self.notice(), row)
        self.assertFalse(path.exists())  # exact legacy replay never copies it
        path.write_bytes(legacy.read_bytes())
        self.assertEqual(self.pending()['pending_count'], 1)
        path.write_bytes(legacy.read_bytes().replace(b'Notice', b'Change'))
        with self.assertRaises(Denied):
            self.pending()
        path.unlink()
        other_path.write_bytes(other_path.read_bytes().replace(b'event', b'wrong'))
        with self.assertRaises(Denied):
            self.pending()

    def test_disabled_kind_still_checked_when_another_kind_enabled(self):
        row = self.notice()
        path, _ = self.paths(row)
        self.config.operator_records['operator.notice']['enabled'] = False
        self.assertEqual(self.pending()['pending_count'], 0)
        path.write_bytes(path.read_bytes().replace(b'Notice', b'Change'))
        with self.assertRaises(Denied):
            self.pending('worker')

    def test_unrelated_closure_conflicts_and_target_route_fail_closed(self):
        result = self.notice()
        target = {'id': result['record']['id'], 'digest': result['record_digest']}
        shared = records.store(self.project)
        for event in ('close-one', 'close-two'):
            envelope = copy.deepcopy(result['record'])
            envelope['id'] = f'operator/sample/{event}'
            envelope['payload'].update(action='withdraw', body='', target=target)
            shared.put(envelope, writer=records.WRITER)
        with self.assertRaises(Denied):
            self.pending('worker')
        second = self.project.root / shared._record_name('operator/sample/close-two')
        second.unlink()
        (self.project.root / index.DIRECTORY / second.name).unlink()
        first = self.project.root / shared._record_name('operator/sample/close-one')
        first_ref = self.project.root / index.DIRECTORY / first.name
        changed = loads_finite(first.read_bytes())
        changed['payload']['addressee'] = 'worker'
        first.write_bytes(canonical(changed))
        first_ref.unlink()
        with self.assertRaises(Denied):
            self.pending('worker')
        # Rebuild is not an escape hatch for invalid canonical closure state.
        shutil.rmtree(self.project.root / index.DIRECTORY)
        with self.assertRaises(Denied):
            self.pending('worker')

    def test_growing_history_multi_role_and_rebuild_preserves_receipts(self):
        self.assertNotIn(index.DIRECTORY, MEMBERS)
        for i in range(26):
            self.notice(f'reviewer-{i:02}')
        for i in range(3):
            self.notice(f'worker-{i:02}', role='worker')
        page, manifest = records.pending(self.project, self.config.roles['reviewer'])
        self.assertEqual(page['pending_count'], 26)
        self.assertTrue(page['truncated'])
        self.assertTrue(page['wake_pending'])
        self.assertEqual([r['record_id'].rsplit('/', 1)[1] for r in page['events']],
                         [f'reviewer-{i:02}' for i in range(20)])
        shared = records.store(self.project)
        shared.acknowledge('reviewer', [vars(manifest[0])], manifest=manifest)
        expected = self.pending()
        self.assertEqual(expected['pending_count'], 25)
        shutil.rmtree(self.project.root / index.DIRECTORY)
        self.assertEqual(self.pending(), expected)
        self.assertEqual(self.pending('worker')['pending_count'], 3)
