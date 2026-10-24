"""Synthetic evidence only: no provider calls or installed/private state."""
import datetime as dt
import os
import shutil
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from mizu import smoke_retention as retention
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import digest, read_json, write_json
from mizu.insights import Insights
from mizu.smoke_refs import shared_lock, validate_refs
from mizu.work_items import WorkItems
from mizu.work_item_waits import normalize_wait, stored_wait


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
            tree = self.data / 'projects' / ('source-' + key)
            tree.mkdir(parents=True)
            (tree / 'probe.txt').write_text('synthetic')
        report = retention.publish(self.data, key, {'status': status,
            'time': (self.moment - dt.timedelta(days=age)).isoformat(), 'result': {'synthetic': True}}, tree)
        return Path(report['pair'])

    def owner(self, name='one'):
        root = self.data / 'projects' / name
        root.mkdir(parents=True)
        insights = Insights(root)
        return insights, WorkItems(root, None, insights)

    def insight(self, store, refs=None):
        record = store.submit(source='worker', title='A topic', body='A body', base_snapshot=None)
        if refs is not None:
            store.attach_evidence(record['id'], source='worker', text='Synthetic evidence',
                                  expected_rev=1, smoke_refs=refs)
        return record

    def run_retention(self, **kwargs):
        return retention.run(self.data, moment=self.moment, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.data).as_posix():
                ('dir' if p.is_dir() else p.read_bytes()) for p in self.data.rglob('*')}

    def test_atomic_pair_identity_and_project(self):
        pair = self.pair(project=True)
        manifest = retention.read_pair(retention.Metadata(self.data), self.a)
        self.assertEqual(set(p.name for p in pair.iterdir()), {'manifest.json', 'report.json', 'project'})
        self.assertEqual(manifest['smoke_id'], self.a)
        self.assertEqual((pair / 'project' / 'probe.txt').read_text(), 'synthetic')
        self.assertFalse((self.data / 'projects' / ('source-' + self.a)).exists())
        (pair / 'report.json').write_text('{}')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_publish_does_not_move_owner_project_when_retention_lock_is_busy(self):
        project = self.data / 'projects' / 'smoke-owner'
        project.mkdir(parents=True)
        with shared_lock(self.data):
            with self.assertRaises(Busy):
                retention.publish(self.data, self.a, {'status': 'pass',
                    'time': self.moment.isoformat()}, project)
        self.assertTrue(project.is_dir())
        self.assertFalse((self.data / 'validation/staging' / self.a / 'project').exists())

    def test_publish_is_one_directory_rename_and_failure_never_partial_pair(self):
        rename = os.rename
        observed = []
        def observe(source, destination):
            if Path(destination).parent.name == 'pairs':
                observed.append(set(p.name for p in Path(source).iterdir()))
                self.assertFalse(Path(destination).exists())
            return rename(source, destination)
        with patch.object(retention.os, 'rename', side_effect=observe):
            self.pair()
        self.assertEqual(observed, [{'manifest.json', 'report.json'}])
        with patch.object(retention.os, 'rename', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):
                self.pair(self.b)
        self.assertFalse((self.data / 'validation/pairs' / self.b).exists())
        self.assertTrue((self.data / 'validation/staging' / self.b).is_dir())

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

    def test_cross_project_pins_and_terminal_insight_release(self):
        self.pair(); self.pair(self.b, status='fail')
        one, _ = self.owner('one'); two, _ = self.owner('two')
        record = self.insight(two, [self.a])
        self.insight(one)  # Prose containing an ID is deliberately not a pin.
        self.assertEqual(self.run_retention()['candidates'], [self.b])
        two.decide(record['id'], 'reject', 'Not needed', '', 'run', expected_rev=1)
        self.assertEqual(self.run_retention()['candidates'], [self.a, self.b])

    def test_defer_revision_and_wait_pins(self):
        self.pair()
        insights, _ = self.owner()
        record = self.insight(insights)
        insights.decide(record['id'], 'defer', 'Wait', 'At deadline', 'run', expected_rev=1,
                        wait={'kind': 'deadline', 'at': '2027-01-01T00:00:00+00:00', 'smoke_refs': [self.a]})
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        insights.revise(record['id'], source='worker', title='Revised', body='New body',
                        base_snapshot=None, expected_rev=1)
        # Old revision waits are history, not pins for the revised owner.
        self.assertEqual(self.run_retention()['candidates'], [self.a])

    def test_insight_seed_preserves_pins_then_work_item_terminal_releases(self):
        self.pair()
        insights, work = self.owner()
        record = self.insight(insights, [self.a])
        decision = insights.decide(record['id'], 'accept', 'Implement', '', 'run', expected_rev=1)
        item = decision['work_item']
        self.assertEqual(item['smoke_refs'], [self.a])
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        work.transition(item['id'], expected_rev=1, status='dropped', actor='worker', reason='Not needed')
        self.assertEqual(self.run_retention()['candidates'], [self.a])
        self.assertEqual(work.get(item['id'])['smoke_refs'], [self.a])

    def test_task_seed_and_blocked_wait_refs(self):
        self.pair(); self.pair(self.b)
        insights, work = self.owner()
        record = self.insight(insights, [self.a])
        event = insights.decide(record['id'], 'modify', 'Implement these', '', 'run', expected_rev=1,
            work_items=[{'title': 'Task', 'description': 'Do it', 'wait': {
                'kind': 'deadline', 'at': '2027-01-01T00:00:00+00:00', 'smoke_refs': [self.b]}}])
        item = event['work_items'][0]
        self.assertEqual(self.run_retention()['pinned'], [self.a, self.b])
        work.transition(item['id'], expected_rev=1, status='doing', actor='worker', smoke_refs=[])
        self.assertEqual(self.run_retention()['candidates'], [self.a, self.b])

    def test_direct_refs_cas_and_missing_pair_refusal(self):
        self.pair()
        insights, work = self.owner()
        record = work.create(idempotency_key='direct', title='Task', actor='worker', smoke_refs=[self.a])
        item = record['record']['work_item']
        self.assertFalse(work.create(idempotency_key='direct', title='Task', actor='worker', smoke_refs=[self.a])['created'])
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        with self.assertRaises(Denied):
            work.transition(item['id'], expected_rev=2, status='doing', actor='worker', smoke_refs=[])
        with self.assertRaises((Denied, FileNotFoundError)):
            work.transition(item['id'], expected_rev=1, status='doing', actor='worker', smoke_refs=[self.b])
        work.transition(item['id'], expected_rev=1, status='blocked', actor='worker', smoke_refs=[],
                        wait={'kind': 'deadline', 'at': '2027-01-01T00:00:00+00:00', 'smoke_refs': [self.a]})
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        work.transition(item['id'], expected_rev=2, status='dropped', actor='worker', reason='Stop')
        self.assertEqual(self.run_retention()['candidates'], [self.a])


    def test_direct_insight_origin_inherits_and_retries_do_not_reconsult_origin(self):
        self.pair(); self.pair(self.b)
        insights, work = self.owner()
        record = self.insight(insights, [self.a])
        request = dict(idempotency_key='from-insight', title='Task', actor='worker',
                       origin={'insight_id': record['id'], 'rev': 1}, smoke_refs=[self.b])
        created = work.create(**request)
        self.assertEqual(created['record']['work_item']['smoke_refs'], [self.a, self.b])
        insights.revise(record['id'], source='worker', title='Revised', body='New body',
                        base_snapshot=None, expected_rev=1)
        self.assertEqual(work.create(**request), {'created': False, 'record': created['record']})
        with self.assertRaises(Denied):
            work.create(**{**request, 'smoke_refs': []})
        self.assertEqual(self.run_retention()['pinned'], [self.a, self.b])


    def test_missing_current_insight_cannot_silently_drop_its_pin(self):
        pair = self.pair()
        insights, _ = self.owner()
        record = self.insight(insights, [self.a])
        (insights.root / 'inbox' / (record['id'] + '.json')).unlink()
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.is_dir())


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

    def test_archived_terminal_evidence_keeps_ids_without_pinning(self):
        self.pair()
        insights, _ = self.owner()
        record = self.insight(insights, [self.a])
        insights.decide(record['id'], 'reject', 'Not needed', '', 'run', expected_rev=1)
        with patch('mizu.insights.time.time', return_value=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc).timestamp()):
            self.assertEqual(insights.gc_decided(keep_days=1), 1)
        self.assertEqual(self.run_retention()['candidates'], [self.a])
        self.assertEqual(insights.read(record['id'])['evidence'][0]['smoke_refs'], [self.a])

    def test_legacy_open_reconciliation_preserves_typed_decision_refs(self):
        self.pair()
        insights, work = self.owner()
        event_id = 'c' * 32
        event = {'id': 'legacy-topic', 'action': 'accept', 'reason': 'Accepted',
                 'revisit': None, 'run': 'run', 'created_at': self.moment.isoformat(),
                 'rev': 1, 'smoke_refs': [self.a]}
        write_json(insights.root / 'decision-history' / f'{event_id}.json', event)
        result = work.reconcile(event_id, expected_action='accept', expected_rev=1,
                                outcome='open', reason='Open the accepted task',
                                actor='operator', title='Follow up')
        item = work.get(result['record']['work_item']['id'])
        self.assertEqual(item['smoke_refs'], [self.a])
        self.assertEqual(self.run_retention()['pinned'], [self.a])

    def test_reference_bounds_and_normalized_wait(self):
        for bad in (None, {}, 'a'*32, ['A'*32], ['a'*16], [self.a, self.a], [f'{x:032x}' for x in range(65)]):
            with self.subTest(bad=repr(bad)[:80]), self.assertRaises(Denied):
                validate_refs(bad)
        for kind in ({'kind': 'code_change'}, {'kind': 'insight_decided', 'insight': 'topic'},
                     {'kind': 'dependency', 'recipient': 'worker', 'requires': {'insight': 'topic', 'rev': 1, 'action': 'accept'}}):
            raw = {**kind, 'smoke_refs': [self.a]}
            normalized = normalize_wait(raw, 'defer', lambda: 'a'*64)
            self.assertEqual(stored_wait(normalized), normalized)

    def test_exact_evidence_revision_and_unknown_pair_fail_closed(self):
        pair = self.pair()
        insights, _ = self.owner()
        record = self.insight(insights, [self.a])
        path = insights.root / 'inbox' / (record['id'] + '.json')
        current = read_json(path)
        current['evidence'][0]['rev'] = 2
        write_json(path, current)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        current['evidence'][0]['rev'] = 1
        current['evidence'][0]['smoke_refs'] = [self.b]
        write_json(path, current)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_report_duplicate_keys_and_empty_reference_revision_fail_closed(self):
        pair = self.pair()
        manifest_path = pair / 'manifest.json'
        manifest = read_json(manifest_path)
        raw = ('{"status":"pass","status":"pass","time":"'
               + manifest["completed_at"] + '","smoke_id":"' + self.a + '"}').encode()
        (pair / 'report.json').write_bytes(raw)
        manifest['report'] = {'sha256': digest(raw), 'bytes': len(raw)}
        write_json(manifest_path, manifest)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        shutil.rmtree(pair)

        # Empty lists are still typed references and must bind an exact owner revision.
        from mizu.smoke_retention import publish
        pair = Path(publish(self.data, self.b, {'status': 'pass',
                    'time': self.moment.isoformat()}, None)['pair'])
        insights, _ = self.owner()
        record = self.insight(insights)
        path = insights.root / 'inbox' / (record['id'] + '.json')
        current = read_json(path)
        current['evidence'] = [{'author': 'worker', 'run': 'run',
            'created_at': self.moment.isoformat(), 'text': 'empty typed refs',
            'smoke_refs': []}]
        write_json(path, current)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_nested_or_unbound_insight_smoke_refs_fail_closed(self):
        self.pair()
        insights, _ = self.owner()
        record = self.insight(insights, [self.a])
        path = insights.root / 'inbox' / (record['id'] + '.json')
        current = read_json(path)
        current['evidence'][0]['run'] = {'smoke_refs': [self.a]}
        write_json(path, current)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        current['evidence'][0]['run'] = 'run'
        current['evidence'][0].pop('smoke_refs')
        write_json(path, current)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)

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
        with patch.object(retention, 'MAX_METADATA', bound), self.assertRaises(LimitExceeded):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_scan_bounds_all_refuse_before_retirement(self):
        pair = self.pair()
        insights, _ = self.owner()
        self.insight(insights, [self.a])
        for key, limit in [('MAX_PROJECTS', 0), ('MAX_PAIRS', 0), ('MAX_REFS', 0), ('MAX_METADATA', 1), ('MAX_ENTRIES', 0)]:
            with self.subTest(key=key), patch.object(retention, key, limit), self.assertRaises(LimitExceeded):
                self.run_retention(apply=True)
            self.assertTrue(pair.exists())

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

    def test_unsafe_metadata_links_refuse(self):
        pair = self.pair()
        insights, _ = self.owner()
        record = self.insight(insights, [self.a])
        path = insights.root / 'inbox' / (record['id'] + '.json')
        other = self.data / 'other.json'
        path.rename(other)
        try:
            path.symlink_to(other)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        path.unlink(); os.link(other, path)
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        path.unlink(); other.rename(path)
        path.write_bytes(b'x'*(retention.MAX_RECORD+1))
        with self.assertRaises((Denied, LimitExceeded)):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_busy_prune_ref_write_and_publication(self):
        pair = self.pair()
        insights, _ = self.owner()
        record = self.insight(insights)
        with shared_lock(self.data):
            with self.assertRaises(Busy):
                self.run_retention(apply=True)
            with patch("mizu.fs._LOCAL_BLOCKING_TIMEOUT", 0), self.assertRaises(Busy):
                insights.attach_evidence(record['id'], source='worker', text='pin', expected_rev=1, smoke_refs=[self.a])
            with self.assertRaises(Busy):
                self.pair(self.b)
        self.assertTrue(pair.exists())
        self.assertEqual(insights.read(record['id'])['evidence'], [])

    def test_pending_rollback_and_committed_partial_deletion_recovery(self):
        pair = self.pair()
        insights, _ = self.owner()
        self.insight(insights, [self.a])
        qroot = self.data / 'validation/quarantine'; qroot.mkdir()
        pending = qroot / (self.a + '.pending')
        pair.rename(pending)
        before = self.snapshot()
        self.assertEqual(self.run_retention()['recovery'], [pending.name])
        self.assertEqual(before, self.snapshot())
        result = self.run_retention(apply=True)
        self.assertEqual(result['recovered'], [self.a])
        self.assertTrue(pair.exists())
        retention.set_days(self.data, 0)
        committed = qroot / (self.a + '.committed')
        pair.rename(committed)
        (committed / 'report.json').unlink()
        result = self.run_retention(apply=True)
        self.assertTrue(result['complete'])
        self.assertFalse(committed.exists())
        self.assertEqual(read_json(self.data / 'validation/retention-audit.json')['events'][-1]['action'], 'recovered-deletion')


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

    def test_apply_recomputes_preview_and_writer_serializes_before_prune(self):
        self.pair()
        insights, _ = self.owner()
        record = self.insight(insights)
        self.assertEqual(self.run_retention()['candidates'], [self.a])
        entered, release = threading.Event(), threading.Event()
        from mizu.smoke_refs import require_pairs
        def paused_check(root, refs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test coordination timed out')
            return require_pairs(root, refs)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch('mizu.insights.require_pairs', side_effect=paused_check):
                future = pool.submit(insights.attach_evidence, record['id'], source='worker',
                                     text='pin', expected_rev=1, smoke_refs=[self.a])
                try:
                    self.assertTrue(entered.wait(10))
                    with self.assertRaises(Busy):
                        self.run_retention(apply=True)
                finally:
                    release.set()
                future.result(timeout=10)
        self.assertEqual(self.run_retention(apply=True)['retired'], [])
        insights.decide(record['id'], 'reject', 'Not needed', '', 'run', expected_rev=1)
        self.run_retention(apply=True)
        # Retirement winning the race cannot create a dangling new pin.
        other = insights.submit(source='worker', title='New topic', body='Body', base_snapshot=None)
        with self.assertRaises(Denied):
            insights.attach_evidence(other['id'], source='worker', text='late pin', expected_rev=1, smoke_refs=[self.a])

    def test_prune_does_not_take_workspace_or_role_locks(self):
        self.pair()
        insights, _ = self.owner()
        from mizu.fs import lock
        with lock(insights.root / 'locks/workspace.lock'), lock(insights.root / 'locks/role-worker.lock'):
            self.assertEqual(self.run_retention(apply=True)['retired'], [self.a])

    def test_missing_owner_unknown_store_and_unreadable_metadata_fail_closed(self):
        pair = self.pair()
        insights, _ = self.owner()
        record = self.insight(insights)
        insights.decide(record['id'], 'defer', 'Wait', 'At deadline', 'run', expected_rev=1,
                        wait={'kind': 'deadline', 'at': '2027-01-01T00:00:00+00:00', 'smoke_refs': [self.a]})
        path = insights.root / 'inbox' / (record['id'] + '.json')
        path.unlink()
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        self.assertTrue(pair.exists())
        (insights.root / 'waits' / (record['id'] + '.json')).write_text('{}')
        with self.assertRaises(Denied):
            self.run_retention(apply=True)
        with patch.object(retention, 'safe_read', side_effect=PermissionError('synthetic unreadable metadata')):
            with self.assertRaises(PermissionError):
                self.run_retention(apply=True)
        self.assertTrue(pair.exists())

    def test_precommit_interruption_rolls_back_on_next_apply(self):
        pair = self.pair()
        rename = os.rename
        def interrupt(source, destination):
            if str(destination).endswith('.committed'):
                raise OSError('synthetic interruption before commit')
            return rename(source, destination)
        with patch.object(retention.os, 'rename', side_effect=interrupt):
            result = self.run_retention(apply=True)
        self.assertFalse(result['complete'])
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

    def test_direct_origin_implicit_refs_are_valid_and_restore_checks_live_pairs(self):
        self.pair()
        insights, work = self.owner()
        record = self.insight(insights, [self.a])
        work.create(idempotency_key='implicit', title='Task', actor='worker',
                    origin={'insight_id': record['id'], 'rev': 1})
        self.assertEqual(self.run_retention()['pinned'], [self.a])
        retention.validate_restored_refs(self.data, insights.root)
        (self.data / 'validation/pairs' / self.a / 'report.json').unlink()
        with self.assertRaises(Denied):
            retention.validate_restored_refs(self.data, insights.root)

    def test_host_schemas_and_project_free_cli(self):
        from mizu.cli import parser, execute
        from mizu.protocol import DEFINITIONS, validate
        from types import SimpleNamespace
        for operation, args in [
            ('attach_evidence', {'id': 'topic', 'rev': 1, 'text': 'Evidence'}),
            ('work_items_create', {'idempotency_key': 'key', 'title': 'Task'}),
            ('work_items_transition', {'id': 'wi_' + 'c'*32, 'expected_rev': 1, 'status': 'doing'})]:
            validate({**args, 'smoke_refs': [self.a]}, DEFINITIONS[operation][1])
            with self.assertRaises(Denied):
                validate({**args, 'smoke_refs': [self.a, self.a]}, DEFINITIONS[operation][1])
        with patch('mizu.cli.load', return_value=SimpleNamespace(data=self.data)):
            self.assertEqual(execute(parser().parse_args(['smoke-retention']))['days'], 31)
            self.assertEqual(execute(parser().parse_args(['smoke-retention', '--set-days', '0'])), {'days': 0})
        self.assertNotIn('smoke-retention', DEFINITIONS)


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
