"""Reference-aware snapshot GC at the existing safe maintenance boundary."""
import contextlib
import dataclasses
import datetime as dt
import io
import json
import multiprocessing
import os
import uuid
from types import SimpleNamespace
from unittest import mock

from support import Fixture, bind_delivery
from mizu import cli, continuations, storage
from mizu.admission import check_slot_exclusion
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied
from mizu.fs import canonical, digest, lock, read_json, write_json

DAY = 86400
CLOCK = 2_000_000_000


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def _hold_reference_locks(paths, ready, release):
    """Separate process: exercise kernel contention, not the local lock guard."""
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.enter_context(lock(path, blocking=False))
        ready.set()
        if not release.wait(600):
            raise AssertionError('reference-lock holder release timed out')


class SnapshotRetentionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.project.snapshots.history_index = 2
        self.captured = self.project.snapshots.capture_files(self.project.workspace)

    def snapshot(self, age=40, *, publish=False, captured=None, time_value=None, **kwargs):
        captured = captured or self.captured
        with mock.patch('mizu.snapshot.now', return_value=time_value or stamp(CLOCK - age * DAY)):
            snap = self.project.snapshots.create(
                captured, goal=self.project.goal, state=uuid.uuid4().hex, run=None,
                outcome='done', summary='Retention fixture',
                verification={'passed': True, 'code_digest': captured['code_digest'], 'run': 'fixture'},
                **kwargs)
        if publish:
            self.project.snapshots.publish(snap)
        return snap

    def path(self, snapshot):
        return self.project.root / 'snapshots' / (snapshot['id'] + '.json')

    def age_histories(self, age=40):
        for path in (self.project.root / 'histories').iterdir():
            os.utime(path, (CLOCK - age * DAY,) * 2)

    def collect(self):
        return storage._automatic_snapshot_gc(self.project, CLOCK)

    def test_policy_default_bounds_and_zero(self):
        original = self.file.read_text()
        self.assertEqual(self.config.limits.snapshot_retention_days, 31)
        self.file.write_text('\n'.join(line for line in original.splitlines()
                                        if not line.startswith('snapshot_retention_days =')))
        self.assertEqual(load(self.file).limits.snapshot_retention_days, 31)
        for value in ('0', '3650'):
            with self.subTest(value=value):
                self.file.write_text(original.replace('snapshot_retention_days = 31',
                                                     'snapshot_retention_days = ' + value))
                self.assertEqual(load(self.file).limits.snapshot_retention_days, int(value))
        for value in ('-1', '3651', 'true', '31.0', '"31"'):
            with self.subTest(value=value):
                self.file.write_text(original.replace('snapshot_retention_days = 31',
                                                     'snapshot_retention_days = ' + value))
                with self.assertRaises(ConfigError):
                    load(self.file)
        self.file.write_text(original)
        self.project.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, snapshot_retention_days=0))
        old = self.snapshot()
        with mock.patch.object(storage, '_audit_collect', side_effect=AssertionError):
            self.assertEqual(self.collect(), {'status': 'disabled'})
        self.assertTrue(self.path(old).exists())
        # Manual preview/apply still works and still requires pause.
        self.assertIn('snapshots/' + old['id'] + '.json', storage.reclaim(self.project)['snapshots'])
        with self.assertRaises(Denied):
            storage.reclaim(self.project, apply=True)
        self.project.set_control(paused=True)
        storage.reclaim(self.project, apply=True)
        self.assertFalse(self.path(old).exists())

    def test_active_history_is_bounded_and_current_pointer_unchanged(self):
        snaps = [self.snapshot(publish=True) for _ in range(4)]
        self.age_histories()
        pointer_path = self.project.root / 'current.json'
        pointer = pointer_path.read_bytes()
        active = read_json(pointer_path)['history']
        self.assertEqual(storage.reclaim(self.project)['snapshots'], [])
        result = self.collect()
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(result['histories'], 4)  # includes initialized history
        self.assertEqual(result['snapshots'], 1)  # only the initialized manifest; merge ancestry stays
        self.assertEqual(pointer_path.read_bytes(), pointer)
        self.assertEqual([p.stem for p in (self.project.root / 'histories').iterdir()], [active])
        self.assertEqual([self.path(s).exists() for s in snaps], [True, True, True, True])
        self.assertEqual(self.collect()['snapshots'], 0)

    def test_recent_unreferenced_branches_keep_evicted_base_for_merge(self):
        base = self.snapshot(publish=True)
        # Both branches cite an old base that will leave the active history.
        head = self.snapshot(age=1, parent=base['id'])
        target = self.snapshot(age=1, parent=base['id'])
        for _ in range(4):
            self.snapshot(publish=True)
        self.age_histories()
        pointer = read_json(self.project.root / 'current.json')
        history = read_json(self.project.root / 'histories' / (pointer['history'] + '.json'))
        self.assertNotIn(base['id'], history)
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertTrue(self.path(base).exists())
        merged, conflicts = self.project.snapshots.merge(base['id'], head['id'], target['id'])
        self.assertEqual(conflicts, [])
        self.assertEqual(merged['files'], base['files'])
        # Repeating the scan must not lose a bounded dependency either.
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertTrue(self.path(base).exists())

    def test_durable_branch_and_manual_reclaim_keep_bounded_merge_ancestry(self):
        chain = [self.snapshot()]
        for _ in range(4):
            chain.append(self.snapshot(parent=chain[-1]['id']))
        write_json(self.project.root / 'runs/branch/started.json', {'snapshot': chain[-1]['id']})
        candidates = storage.reclaim(self.project)['snapshots']
        for snap in chain[-3:]:  # root and history_index=2 parent edges
            self.assertNotIn('snapshots/' + snap['id'] + '.json', candidates)
        for snap in chain[:-3]:
            self.assertIn('snapshots/' + snap['id'] + '.json', candidates)
        self.project.set_control(paused=True)
        storage.reclaim(self.project, apply=True)
        self.assertEqual([self.path(snap).exists() for snap in chain], [False, False, True, True, True])
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertTrue(self.path(chain[-3]).exists())

    def test_parent_scan_is_bounded_cycle_safe_and_does_not_protect_missing_ids(self):
        ids = [digest(str(i).encode()) for i in range(5)]
        manifests = {sid: {'parent': ids[i - 1] if i else None} for i, sid in enumerate(ids)}
        protected, incomplete = storage._snapshot_ancestry(manifests, {ids[-1]}, 2)
        self.assertEqual(protected, set(ids[-3:]))
        self.assertEqual(incomplete, set())
        manifests[ids[0]]['parent'] = ids[-1]
        protected, incomplete = storage._snapshot_ancestry(manifests, {ids[-1]}, 1_000_000)
        self.assertEqual(protected, set(ids))
        self.assertEqual(incomplete, set())
        for invalid in ('missing', digest(b'absent')):
            manifests[ids[-1]]['parent'] = invalid
            protected, incomplete = storage._snapshot_ancestry(manifests, {ids[-1]}, 2)
            self.assertEqual(protected, {ids[-1]})
            self.assertEqual(incomplete, {ids[-1]})

    def test_missing_or_invalid_required_parent_refuses_deletion(self):
        old = self.snapshot()
        parent = self.snapshot()
        child = self.snapshot(age=1, parent=parent['id'])
        write_json(self.project.root / 'runs/branch/started.json', {'snapshot': child['id']})
        saved = self.path(parent).read_bytes()
        for invalid in (None, b'{}'):
            with self.subTest(parent=invalid):
                if invalid is None:
                    self.path(parent).unlink()
                else:
                    self.path(parent).write_bytes(invalid)
                result = self.collect()
                self.assertEqual(result['status'], 'refused', result)
                self.assertTrue(self.path(old).exists())
                self.assertTrue(self.path(child).exists())
                preview = storage.reclaim(self.project)
                self.assertGreater(preview['reference_records_skipped'], 0)
                self.assertNotIn('snapshots/' + parent['id'] + '.json', preview['snapshots'])
                self.project.set_control(paused=True)
                with self.assertRaises(Denied):
                    storage.reclaim(self.project, apply=True)
                self.path(parent).write_bytes(saved)

    def test_cutoff_is_strict_and_recent_histories_pin_old_snapshots(self):
        old = self.snapshot()
        edge = self.snapshot(age=31)
        recent = self.snapshot(age=30)
        published = self.snapshot(publish=True)
        # Leave both the current history window and its merge ancestry.
        for _ in range(4):
            self.snapshot(publish=True)
        self.age_histories()
        history = [published['id']]
        generation = self.project.root / 'histories' / (digest(canonical(history)) + '.json')
        write_json(generation, history)
        os.utime(generation, (CLOCK - 31 * DAY,) * 2)
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertFalse(self.path(old).exists())
        for snap in (edge, recent, published):
            self.assertTrue(self.path(snap).exists())
        self.assertTrue(generation.exists())
        os.utime(generation, (CLOCK - 31 * DAY - 1,) * 2)
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertFalse(self.path(published).exists())

    def test_only_objects_unused_by_every_retained_manifest_are_removed(self):
        (self.project.workspace / 'only-old.txt').write_bytes(b'old-only')
        captured = self.project.snapshots.capture_files(self.project.workspace)
        old = self.snapshot(captured=captured)
        (self.project.workspace / 'only-old.txt').unlink()
        (self.project.workspace / 'only-recent.txt').write_bytes(b'recent-only')
        recent_capture = self.project.snapshots.capture_files(self.project.workspace)
        recent = self.snapshot(age=1, captured=recent_capture)
        recent_object = recent_capture['files']['only-recent.txt']['sha256']
        shared = self.captured['files']['app.py']['sha256']
        exclusive = captured['files']['only-old.txt']['sha256']
        result = self.collect()
        self.assertEqual(result['status'], 'collected')
        self.assertFalse(self.path(old).exists())
        self.assertFalse((self.project.root / 'objects' / exclusive).exists())
        self.assertTrue(self.path(recent).exists())
        self.assertTrue((self.project.root / 'objects' / recent_object).exists())
        self.assertTrue((self.project.root / 'objects' / shared).exists())
        self.project.snapshots.materialize(self.project.snapshots.get(), self.root / 'materialized')

    def test_durable_roots_and_unreferenced_control(self):
        snaps = [self.snapshot() for _ in range(12)]
        root = self.project.root
        write_json(root / 'runs/probe/started.json', {'snapshot': snaps[0]['id']})
        write_json(root / 'runs/probe/delegation.json', {'snapshot': snaps[1]['id']})
        from mizu.report import publish
        publish(self.project, snaps[2])
        insight = self.project.insights.submit(source='worker', title='Pinned', body='old revision',
                                               base_snapshot=snaps[3]['id'])
        self.project.insights.revise(insight['id'], source='worker', title='Pinned', body='live revision',
                                     base_snapshot=snaps[4]['id'], expected_rev=1)
        archived = self.project.insights.submit(source='worker', title='Archived', body='permanent',
                                                base_snapshot=snaps[5]['id'])
        self.project.insights.decide(archived['id'], 'reject', 'reviewed', '', 'operator', expected_rev=1)
        with mock.patch('mizu.insights.time.time', return_value=CLOCK):
            self.project.insights.gc_decided(keep_days=1)
        self.assertFalse((root / 'inbox' / (archived['id'] + '.json')).exists())
        write_json(root / 'spool/editor/queued.json', {'base_snapshot': snaps[6]['id']})
        write_json(root / '.ingest/claimed.json', {'base_snapshot': snaps[7]['id']})
        write_json(root / 'runs/probe/consultations/reference.json', {'snapshot': snaps[9]['id']})
        write_json(root / 'runs/probe/delegations/reference.json', {'snapshot': snaps[10]['id']})
        pointer = read_json(root / 'current.json')
        source = '1' * 32
        pointer['continuations'] = {'version': 1, 'entries': [{
            'role': 'worker', 'source_run': source, 'snapshot': snaps[8]['id'],
            'successor': continuations._successor('worker', source, snaps[8]['id']), 'state': 'committed'}]}
        write_json(root / 'current.json', pointer)
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertTrue(all(self.path(s).exists() for s in snaps[:-1]))
        self.assertFalse(self.path(snaps[-1]).exists())

    def test_historical_delivery_and_done_bindings_survive_history_expiry(self):
        proposal = self.project.insights.submit(source='worker', title='Delivery', body='Keep all bindings',
                                                 base_snapshot=None)
        decision = self.project.insights.decide(proposal['id'], 'accept', 'reviewed', '', 'operator', expected_rev=1)
        item = decision['work_item']['id']
        first = self.snapshot(publish=True)
        bind_delivery(self.project, item, first['id'])
        second = self.snapshot(publish=True)
        bind_delivery(self.project, item, second['id'], expected_rev=3)
        self.project.work_items.transition(item, expected_rev=4, status='done', actor='worker',
                                           snapshot_id=second['id'])
        self.snapshot(publish=True)
        self.snapshot(publish=True)
        self.age_histories()
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertTrue(self.path(first).exists())
        self.assertTrue(self.path(second).exists())
        self.assertEqual(self.project.work_items.get(item)['status'], 'done')

    def test_safe_point_fence_pins_both_snapshots(self):
        base, sealed, garbage = [self.snapshot() for _ in range(3)]
        write_json(self.project.root / 'safe-point.json', {
            'version': 1, 'role': 'worker', 'profile': 'primary', 'source_run': '1' * 32,
            'run': '2' * 32, 'base': base['id'], 'snapshot': sealed['id']})
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertTrue(self.path(base).exists())
        self.assertTrue(self.path(sealed).exists())
        self.assertFalse(self.path(garbage).exists())

    def test_malformed_or_incomplete_scans_delete_nothing(self):
        old = self.snapshot()
        root = self.project.root
        cases = [(root / 'inbox/bad.json', None),
                 (root / 'insight-revisions/bad.json', {'base_snapshot': 'invalid'}),
                 (root / 'runs/probe/result.json', {'snapshot': 'invalid'}),
                 (root / 'histories' / ('f' * 64 + '.json'), [old['id']]),
                 (root / 'artifacts/probe/evidence.json', []),
                 (root / 'current.json', None)]
        pointer = (root / 'current.json').read_bytes()
        for path, value in cases:
            with self.subTest(path=path.relative_to(root)):
                write_json(path, value)
                os.utime(path, (CLOCK - 50 * DAY,) * 2)
                self.assertEqual(self.collect()['status'], 'refused')
                self.assertTrue(self.path(old).exists())
                path.unlink()
                if path.parent.name == 'probe':
                    path.parent.rmdir()
                (root / 'current.json').write_bytes(pointer)
        listing = storage.os.scandir
        def fail(path):
            if path == root / 'inbox':
                raise OSError('unavailable')
            return listing(path)
        with mock.patch.object(storage.os, 'scandir', side_effect=fail):
            self.assertEqual(self.collect()['status'], 'refused')
        with mock.patch.object(storage.insight_archive, 'snapshot_references', side_effect=ValueError):
            self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())

    def test_oversized_records_invalid_times_and_item_history_refuse_gc(self):
        old = self.snapshot()
        bad = self.project.root / 'inbox/bad.json'
        write_json(bad, {'padding': 'x' * storage._AUDIT_JSON_BYTES})
        self.assertEqual(self.collect()['status'], 'refused')
        bad.unlink()
        for value in ('invalid', '2024-01-01T00:00:00'):
            malformed = self.snapshot(time_value=value)
            self.assertEqual(self.collect()['status'], 'refused')
            self.path(malformed).unlink()
        write_json(self.project.root / 'work-items/unknown/2.json', {})
        self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())

    def test_nonblocking_exclusions_and_busy_does_not_consume_cadence(self):
        old = self.snapshot()
        paths = [self.project.root / ('locks/' + name + '.lock') for name in
                 ('workspace', 'publication', 'editor-ingest', 'insights', 'acceptance', 'run-worker')]
        paths.append(self.project.root / "spool/editor/.submit.lock")
        paths.append(check_slot_exclusion(self.project.root, self.config.limits.parallel_checks - 1))
        for path in paths:
            with self.subTest(lock=path.name), lock(path):
                with self.assertRaises(Busy):
                    self.collect()
                with mock.patch.object(storage, 'prune'), mock.patch.object(storage.time, 'time', return_value=CLOCK):
                    self.assertEqual(storage.maintain_automatic_prune(self.project), {'status': 'busy'})
                self.assertFalse((self.project.root / 'maintenance/automatic-prune.json').exists())
                self.assertTrue(self.path(old).exists())
        self.assertEqual(self.collect()['status'], 'collected')

    def test_publication_and_new_slot_cannot_enter_between_scan_and_unlink(self):
        old = self.snapshot()
        original = storage._audit_collect
        def collected(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.assertRaises(Busy), lock(self.project.root / 'locks/publication.lock', blocking=False):
                pass
            with self.assertRaises(Busy), lock(check_slot_exclusion(self.project.root, 0), blocking=False):
                pass
            return result
        with mock.patch.object(storage, '_audit_collect', side_effect=collected):
            self.assertEqual(self.collect()['status'], 'collected')
        self.assertFalse(self.path(old).exists())

    def test_cli_reference_writers_hold_publication_from_read_through_write(self):
        args = cli.parser().parse_args(['insight', 'submit', 'sample', '--title', 'Test', '--body', '-'])
        original_get = self.project.snapshots.get
        def probe_get(*a, **kw):
            with self.assertRaises(Busy):
                self.collect()
            return original_get(*a, **kw)
        def submitted(*a, **kw):
            with self.assertRaises(Busy):
                self.collect()
            return {'ok': True}
        with mock.patch.object(self.project.snapshots, 'get', side_effect=probe_get), \
             mock.patch.object(self.project.insights, 'submit', side_effect=submitted), \
             mock.patch('sys.stdin', new=io.StringIO('body')):
            self.assertEqual(cli._cmd_insight(self.config, self.project, args), {'ok': True})
        args.action, args.id, args.expected_rev = 'revise', 'probe', 1
        with mock.patch.object(self.project.snapshots, 'get', side_effect=probe_get), \
             mock.patch.object(self.project.insights, 'revise', side_effect=submitted), \
             mock.patch('sys.stdin', new=io.StringIO('revision')):
            self.assertEqual(cli._cmd_insight(self.config, self.project, args), {'ok': True})
        with mock.patch.object(self.project.snapshots, 'get', side_effect=probe_get), \
             mock.patch('mizu.report.publish', side_effect=submitted):
            self.assertEqual(cli._cmd_report(self.config, self.project, SimpleNamespace()), {'ok': True})
        with mock.patch('mizu.editor.export', side_effect=submitted):
            args = SimpleNamespace(destination=self.root / 'export')
            self.assertEqual(cli._cmd_editor_export(self.config, self.project, args), {'ok': True})

    def test_cli_and_capsule_writers_refuse_busy_kernel_locks_without_references(self):
        from mizu import editor, fs
        bundle = self.root / 'bundle'
        editor.export(self.project, bundle)
        capsule = editor.Capsule(bundle, self.project.root / 'spool/editor')
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        paths = [self.project.root / 'locks/publication.lock', capsule.outbox / '.submit.lock']
        holder = ctx.Process(target=_hold_reference_locks, args=(paths, ready, release))
        holder.start()
        try:
            self.assertTrue(ready.wait(60), 'reference-lock holder did not start')
            args = cli.parser().parse_args(['insight', 'submit', 'sample', '--title', 'Test', '--body', '-'])
            revised = cli.parser().parse_args(['insight', 'revise', 'sample', '--id', 'probe',
                                             '--title', 'Test', '--body', '-'])
            calls = [
                lambda: cli._cmd_insight(self.config, self.project, args),
                lambda: cli._cmd_insight(self.config, self.project, revised),
                lambda: cli._cmd_report(self.config, self.project, SimpleNamespace()),
                lambda: cli._cmd_editor_export(self.config, self.project,
                                               SimpleNamespace(destination=self.root / 'busy-export')),
                lambda: capsule.call('submit_insight', {'title': 'Queue', 'body': 'Citation'}),
            ]
            # Assert nonblocking before entering the real kernel acquire, so a
            # regression cannot hang the test on POSIX. The holder remains locked
            # throughout: each call must return Busy before it is released.
            acquire = fs._platform.lock_fd
            def nonblocking(fd, *, blocking):
                self.assertFalse(blocking)
                return acquire(fd, blocking=blocking)
            with mock.patch.object(fs._platform, 'lock_fd', side_effect=nonblocking), \
                 mock.patch.object(self.project.snapshots, 'get', side_effect=AssertionError('snapshot read')), \
                 mock.patch('sys.stdin', new=io.StringIO('body')):
                for i, call in enumerate(calls):
                    with self.subTest(writer=i), self.assertRaises(Busy):
                        call()
            self.assertEqual(list((self.project.root / 'inbox').glob('*.json')), [])
            self.assertEqual(list(capsule.outbox.glob('*.json')), [])
            self.assertFalse((self.project.root / 'artifacts').exists())
            self.assertFalse((self.root / 'busy-export').exists())
        finally:
            release.set()
            holder.join(60)
            if holder.is_alive():
                holder.terminate()
                holder.join(60)
        self.assertEqual(holder.exitcode, 0)

    def test_due_boundary_reuses_cadence_and_records_bounded_result(self):
        old = self.snapshot()
        with mock.patch.object(storage.time, 'time', return_value=CLOCK):
            result = storage.maintain_automatic_prune(self.project)
            self.assertEqual(result['status'], 'pruned', result)
            self.assertEqual(result['snapshots']['status'], 'collected')
            self.assertFalse(self.path(old).exists())
            self.assertEqual(storage.maintain_automatic_prune(self.project), {'status': 'not-due'})
        self.assertLess(len(json.dumps(result)), storage._AUTOMATIC_PRUNE_STATE_BYTES)
        self.assertFalse(self.project.control()['paused'])

    def test_capsule_submission_serializes_its_durable_outbox_reference(self):
        from mizu import editor
        bundle = self.root / 'bundle'
        editor.export(self.project, bundle)
        capsule = editor.Capsule(bundle, self.project.root / 'spool/editor')
        original = editor.write_json
        def write(*a, **kw):
            with self.assertRaises(Busy):
                self.collect()
            return original(*a, **kw)
        with mock.patch.object(editor, 'write_json', side_effect=write):
            result = capsule.call('submit_insight', {'title': 'Queue', 'body': 'Citation'})
        self.assertTrue((self.project.root / 'spool/editor' / (result['id'] + '.json')).exists())

    def test_snapshot_writer_finishes_capture_and_publication_before_gc(self):
        # Controlled interleaving: no sleeps and no host-speed assumption.
        import threading
        entered, release = threading.Event(), threading.Event()
        outcome = []
        def writer():
            try:
                with lock(self.project.root / 'locks/workspace.lock'):
                    captured = self.project.snapshots.capture_files(self.project.workspace)
                    snap = self.snapshot(publish=False, captured=captured)
                    entered.set()
                    if not release.wait(60):
                        raise AssertionError('writer release timed out')
                    self.project.snapshots.publish(snap)
                    outcome.append(snap)
            except BaseException as exc:
                outcome.append(exc)
                entered.set()
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(entered.wait(60))
            with self.assertRaises(Busy):
                self.collect()
        finally:
            release.set()
            thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        self.age_histories()
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertEqual(self.project.snapshots.get()['id'], outcome[0]['id'])
        self.project.snapshots.materialize(outcome[0], self.root / 'writer-result')

    def test_refusal_and_interruption_never_fail_the_requested_boundary(self):
        old = self.snapshot()
        bad = self.project.root / 'histories' / ('f' * 64 + '.json')
        write_json(bad, None)
        with mock.patch.object(storage.time, 'time', return_value=CLOCK):
            result = storage.maintain_automatic_prune(self.project)
        # Run pruning remains independent; the snapshot refusal is explicit.
        self.assertEqual(result['status'], 'pruned')
        self.assertEqual(result['snapshots']['status'], 'refused')
        self.assertTrue(self.path(old).exists())
        bad.unlink()
        # Simulate failure before unlink; a later sweep safely converges.
        from pathlib import Path
        original = Path.unlink
        def interrupted(path, *args, **kwargs):
            if path == self.path(old):
                raise OSError('interrupted unlink')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', new=interrupted):
            self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertFalse(self.path(old).exists())

    def test_missing_active_generation_and_unsafe_roots_refuse_all_deletion(self):
        old = self.snapshot()
        pointer = read_json(self.project.root / 'current.json')
        generation = self.project.root / 'histories' / (pointer['history'] + '.json')
        data = generation.read_bytes()
        generation.unlink()
        self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())
        generation.write_bytes(data)
        for name in ('snapshots', 'objects', 'histories', 'spool'):
            with self.subTest(root=name):
                source = self.project.root / name
                moved = self.root / ('moved-' + name)
                source.rename(moved)
                source.write_bytes(b'not a directory')
                try:
                    self.assertEqual(self.collect()['status'], 'refused')
                finally:
                    source.unlink()
                    moved.rename(source)
                self.assertTrue(self.path(old).exists())
        self.assertEqual(self.collect()['status'], 'collected')

    def test_disappearing_enumerated_history_and_insight_refuse_gc(self):
        old = self.snapshot()
        history = [old['id']]
        path = self.project.root / 'histories' / (digest(canonical(history)) + '.json')
        write_json(path, history)
        self.age_histories()
        original = storage._audit_ref_list
        def missing_history(root, candidate):
            if candidate == path:
                return None, False
            return original(root, candidate)
        with mock.patch.object(storage, '_audit_ref_list', side_effect=missing_history):
            self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())
        path.unlink()
        insight = self.project.root / 'inbox/citation.json'
        write_json(insight, {'base_snapshot': old['id']})
        original = storage._audit_ref_dict
        def missing_insight(root, candidate):
            if candidate == insight:
                return None, False
            return original(root, candidate)
        with mock.patch.object(storage, '_audit_ref_dict', side_effect=missing_insight):
            self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())
