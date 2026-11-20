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
from mizu.fs import ATOMIC_WRITE_TEMP_PREFIX, canonical, digest, lock, read_json, write_json

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

    def test_large_file_map_is_audited_and_expired_under_default_file_limit(self):
        self.assertEqual(self.project.snapshots.max_files, 20_000)
        entry = next(iter(self.captured['files'].values()))
        files = {f'{index:010d}': dict(entry) for index in range(10_000)}
        captured = dict(self.captured, files=files, code_digest=digest(canonical(files)))
        large = self.snapshot(age=40, captured=captured)
        path = self.path(large)
        self.assertGreater(path.stat().st_size, storage._AUDIT_JSON_BYTES)
        # Noncanonical map order/whitespace still hashes to the same identity.
        large['files'] = dict(reversed(list(files.items())))
        path.write_text(json.dumps(large, ensure_ascii=False, indent=1), encoding='utf-8')
        report = storage.audit(self.project)
        self.assertEqual(report['snapshots']['skipped'], 0, report)
        parsed = self.project.snapshots.audit_manifest(
            large['id'], metadata_bytes=storage._AUDIT_JSON_BYTES,
            maximum=storage._SNAPSHOT_AUDIT_BYTES)
        # Audit-only record carries no file map, goal, or names, only digests.
        self.assertNotIn('goal', parsed)
        self.assertNotIn('files', parsed)
        self.assertNotIn('file_names', parsed)
        self.assertEqual(len(parsed['file_shas']), len(files))
        self.assertEqual(set(parsed['file_shas']),
                         {entry['sha256'] for entry in files.values()})
        self.assertEqual(parsed['code_digest'], digest(canonical(files)))
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertFalse(path.exists())

    def test_historical_goal_manifest_is_audited_after_limit_decreases(self):
        goal_limit = 1_050_000
        self.project.config = dataclasses.replace(
            self.project.config,
            limits=dataclasses.replace(self.project.config.limits,
                                       project_goal_bytes=goal_limit))
        (self.project.root / 'PROJECT.md').write_bytes(b'g' * goal_limit)

        large_goal_snapshot = self.snapshot(age=40)
        manifest_path = self.path(large_goal_snapshot)
        self.assertGreater(manifest_path.stat().st_size, storage._AUDIT_JSON_BYTES)

        # Simulate a later configuration and goal reduction; old snapshots stay valid.
        (self.project.root / 'PROJECT.md').write_bytes(b'shortened goal')
        self.project.config = self.config
        self.assertEqual(self.project.goal, 'shortened goal')

        report = storage.audit(self.project)
        self.assertEqual(report['snapshots']['skipped'], 0)
        collected = storage._audit_collect(self.project)
        self.assertNotIn('goal', collected['manifests'][large_goal_snapshot['id']])

        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertFalse(manifest_path.exists())

    def test_escaped_goal_manifest_is_audited_and_expired(self):
        goal_limit = 1_050_000
        self.project.config = dataclasses.replace(
            self.project.config,
            limits=dataclasses.replace(self.project.config.limits,
                                       project_goal_bytes=goal_limit))
        (self.project.root / 'PROJECT.md').write_text(chr(1) * goal_limit, encoding='utf-8')
        large_goal_snapshot = self.snapshot(age=40)
        manifest_path = self.path(large_goal_snapshot)
        self.assertGreater(manifest_path.stat().st_size,
                           storage._AUDIT_JSON_BYTES + goal_limit * 5)

        (self.project.root / 'PROJECT.md').write_text('shortened goal', encoding='utf-8')
        self.project.config = self.config
        report = storage.audit(self.project)
        self.assertEqual(report['snapshots']['skipped'], 0)
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertFalse(manifest_path.exists())

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

    def test_safe_point_fence_pins_both_snapshots(self):
        base, sealed, garbage = [self.snapshot() for _ in range(3)]
        write_json(self.project.root / 'safe-point.json', {
            'version': 1, 'role': 'worker', 'profile': 'primary', 'source_run': '1' * 32,
            'run': '2' * 32, 'base': base['id'], 'snapshot': sealed['id']})
        self.assertEqual(self.collect()['status'], 'collected')
        self.assertTrue(self.path(base).exists())
        self.assertTrue(self.path(sealed).exists())
        self.assertFalse(self.path(garbage).exists())

    def test_product_staging_leftover_never_disables_retention(self):
        old = self.snapshot()
        objects = self.project.root / 'objects'
        staging = objects / (ATOMIC_WRITE_TEMP_PREFIX + 'abcdefgh')
        staging.write_bytes(b'staged')
        self.assertEqual(storage.audit(self.project)['objects']['skipped'], 0)
        result = self.collect()
        self.assertEqual(result['status'], 'collected', result)
        self.assertFalse(self.path(old).exists())
        self.assertEqual(staging.read_bytes(), b'staged')
        # An unrecognized foreign entry still makes the inventory incomplete.
        newer = self.snapshot()
        foreign = objects / 'README'
        foreign.write_bytes(b'x')
        self.assertEqual(storage.audit(self.project)['objects']['skipped'], 1)
        self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(newer).exists())

    def test_staging_prefixed_symlink_still_refuses(self):
        old = self.snapshot()
        link = self.project.root / 'objects' / (ATOMIC_WRITE_TEMP_PREFIX + 'link')
        try:
            os.symlink(self.project.root / 'objects' / 'missing', link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symlink creation unavailable: ' + str(exc))
        self.assertEqual(storage.audit(self.project)['objects']['skipped'], 1)
        self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())

    def test_staging_prefixed_directory_still_refuses(self):
        old = self.snapshot()
        (self.project.root / 'objects' / (ATOMIC_WRITE_TEMP_PREFIX + 'directory')).mkdir()
        self.assertEqual(storage.audit(self.project)['objects']['skipped'], 1)
        self.assertEqual(self.collect()['status'], 'refused')
        self.assertTrue(self.path(old).exists())

    def test_staging_prefixed_hard_link_still_refuses(self):
        old = self.snapshot()
        target = self.project.root / 'staging-target'
        target.write_bytes(b'x')
        link = self.project.root / 'objects' / (ATOMIC_WRITE_TEMP_PREFIX + 'hard')
        try:
            os.link(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Hard links unavailable: ' + str(exc))
        self.assertEqual(storage.audit(self.project)['objects']['skipped'], 1)
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
