"""Bounded fake-runner tests; no claim about a real container runtime."""
import dataclasses
import json
import os
import shutil
import threading
from unittest.mock import patch

from support import Fixture
from test_check_jobs import FakeSandbox
from mizu import acceptance, check_jobs, storage
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, held, write_json
from mizu.project import Project
from mizu.runtime import prompt_for, prompt_delta_for


class AcceptanceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.configure(reuse=True)
        FakeSandbox.calls = []

    def configure(self, *, reuse=True, detached=True, **sandbox):
        roles = {k: dataclasses.replace(v, capabilities=tuple(c for c in v.capabilities if c != 'check_job') +
                                        (('check_job',) if detached else ()))
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=reuse, **sandbox))
        self.project.config = self.config

    def run_job(self, factory=FakeSandbox):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=factory)

    def produce(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        self.run_job()
        snap = ctx.snapshot
        proof = acceptance.effective(self.project, snap)
        self.assertIsNotNone(proof)
        return ctx, job, proof

    def symlink(self, source, target, **kwargs):
        try:
            os.symlink(source, target, **kwargs)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symlinks unavailable: ' + str(exc))

    def object_path(self, receipt_id):
        return self.project.root / f'evidence/acceptance/objects/{receipt_id}.json'

    def test_frozen_a_finishes_after_live_b_without_collection_or_republication(self):
        ctx = self.context()
        snapshot = ctx.snapshot
        manifest = self.project.root / 'snapshots' / (snapshot['id'] + '.json')
        raw = manifest.read_bytes()
        job = check_jobs.submit(ctx)
        (ctx.workspace / 'app.py').write_bytes(b'VALUE = 3\n')
        self.run_job()
        self.assertIsNone(ctx.verification)
        self.assertEqual(manifest.read_bytes(), raw)
        self.assertIsNone(self.project.snapshots.get()['verification'])
        proof = self.project.status()['effective_verification']
        self.assertEqual(proof['code_digest'], snapshot['code_digest'])
        self.assertEqual(proof['source'], 'retained_acceptance')
        receipt = acceptance.get(self.project.root, proof['receipt_id'])
        self.assertEqual(receipt['producer']['id'], job['id'])
        self.assertNotIn('stdout', str(receipt))
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertIsNone(ctx.verification)
        b = self.project.snapshots.capture_files(ctx.workspace)
        self.assertIsNone(acceptance.effective(self.project, b))
        # Both prompt modes distinguish immutable publication proof from late proof.
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            header = json.loads(text)['published_snapshot']
            self.assertEqual(set(header), {'id', 'code_digest', 'created_at', 'state', 'summary',
                                           'verification', 'effective_verification'})
            self.assertIsNone(header['verification'])
            self.assertEqual(header['effective_verification'], proof)

    def test_sync_producer_retains_full_commands_not_preview(self):
        self.configure(detached=False)
        command = 'echo ' + 'a' * 800
        self.project.verify = (command,)
        ctx = self.context()
        with patch.object(ctx.sandbox, 'execute', side_effect=lambda w, s, **kw: FakeSandbox(None, None, None, None).execute(w, s)):
            ctx._op_verify({})
        proof = acceptance.effective(self.project, ctx.snapshot)
        receipt = acceptance.get(self.project.root, proof['receipt_id'])
        self.assertEqual(receipt['scope']['commands'], [command])
        self.assertEqual(receipt['producer'], {'kind': 'verify', 'id': ctx.run_dir.name, 'run': ctx.run_dir.name})
        self.assertEqual(acceptance.recover_run(self.project, ctx.run_dir.name), proof['receipt_id'])
        self.assertIsNone(ctx.snapshot['verification'])
        self.assertTrue(ctx.verification['passed'])

    def test_shared_result_uses_original_immutable_receipt_and_idempotence(self):
        ctx, job, proof = self.produce()
        later = self.context()
        shared = check_jobs.submit(later)
        self.assertEqual(shared['shared_job'], job['id'])
        self.run_job()
        self.assertTrue(check_jobs.result(later, shared['id'])['accepted_for_current_source'])
        self.assertEqual(len(FakeSandbox.calls), 1)
        self.assertEqual(acceptance.recover_job(self.project, job['id']), proof['receipt_id'])
        acceptance.recover_producers(self.project)
        self.assertEqual(acceptance.audit(self.project.root), 1)
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)

    def test_scope_and_digest_changes_do_not_project(self):
        ctx, job, proof = self.produce()
        self.assertIsNone(acceptance.effective(self.project, {'code_digest': 'b' * 64}))
        original = self.project.verify
        self.project.verify = (*original, 'true')
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.project.verify = original
        self.configure(image='sha256:' + 'c' * 64)
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.configure(image='sha256:' + 'a' * 64)
        dependency = self.root / 'dependency'
        dependency.write_text('original')
        self.configure(mounts=({'source': str(dependency), 'target': '/dependency'},))
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.configure(mounts=())
        self.configure(reuse=False)
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))

    def test_unbound_and_custom_jobs_never_mint(self):
        for kind in ('experiment', 'exec'):
            ctx = self.context('reviewer')
            check_jobs.submit(ctx, 'true', kind=kind)
            self.run_job()
            self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.configure(reuse=False)
        ctx = self.context()
        check_jobs.submit(ctx)
        self.run_job()
        self.assertEqual(acceptance.audit(self.project.root), 0)
        self.assertIsNone(ctx.verification)

    def test_fail_cancel_source_change_and_cleanup_failure_never_mint(self):
        def factory(mode):
            class Bad(FakeSandbox):
                def execute(inner, workspace, script, **kwargs):
                    value = super().execute(workspace, script, **kwargs)
                    if mode == 'failure':
                        value['exit_code'] = 1
                    if mode == 'source':
                        (workspace / 'app.py').write_bytes(b'changed')
                    if mode == 'cancel':
                        check_jobs.cancel(ctx, job['id'])
                    if mode == 'cleanup':
                        raise Denied('cleanup failed')
                    return value
            return Bad
        for mode in ('failure', 'source', 'cancel', 'cleanup'):
            with self.subTest(mode=mode):
                ctx = self.context()
                job = check_jobs.submit(ctx, fresh=True)
                with patch('mizu.check_jobs.cleanup', side_effect=Denied('cleanup failed')):
                    self.run_job(factory(mode))
                self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
                self.assertNotIn('acceptance_scope', check_jobs.raw_read(self.project, job['id']))
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_changed_dependency_during_execution_refuses_proof(self):
        dependency = self.root / 'dependency'
        dependency.write_text('original')
        self.configure(mounts=({'source': str(dependency), 'target': '/dependency'},))
        class ChangesDependency(FakeSandbox):
            def execute(inner, *args, **kwargs):
                dependency.write_text('changed')
                return super().execute(*args, **kwargs)
        ctx = self.context()
        job = check_jobs.submit(ctx)
        self.run_job(ChangesDependency)
        self.assertFalse(check_jobs.raw_read(self.project, job['id'])['passed'])
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def oversized_result_job(self):
        # Four short commands keep the job plus scope below the read limit;
        # JSON-escaped output previews, not the scripts, overflow the result.
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, file_bytes=6000))
        self.project.config = self.config
        self.project.verify = ('true',) * 4
        ctx = self.context()
        job = check_jobs.submit(ctx)
        class Verbose(FakeSandbox):
            def execute(inner, *args, **kwargs):
                return {**super().execute(*args, **kwargs),
                        'stdout': '\x01' * 512, 'stderr': '\x02' * 512}
        self.run_job(Verbose)
        path = check_jobs.root(self.project) / job['id']
        value = check_jobs.raw_read(self.project, job['id'])
        self.assertTrue(value['passed'])
        self.assertEqual(value['status'], 'completed')
        self.assertLessEqual(len(canonical({**value, 'acceptance_scope': acceptance.scope(self.project)})),
                             self.config.limits.file_bytes)
        self.assertGreater((path / 'result.json').stat().st_size, self.config.limits.file_bytes)
        return ctx, job, path

    def assert_size_refusal(self, job):
        value = check_jobs.raw_read(self.project, job['id'])
        self.assertEqual(value.get('acceptance_retention_refusal'), 'result_file_bytes')
        self.assertNotIn('acceptance_scope', value)
        self.assertNotIn('acceptance_receipt_id', value)
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_oversized_result_does_not_pin_or_block_runner_and_backup(self):
        from pathlib import Path
        ctx, job, path = self.oversized_result_job()
        self.assert_size_refusal(job)
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        acceptance.recover_producers(self.project)
        self.project.set_control(paused=True)
        archive = self.root / 'oversized-backup.tar.gz'
        storage.backup(self.project, archive)
        self.assertTrue(archive.is_file())
        self.project.set_control(paused=False)
        self.project.verify = ('other',)
        unrelated = check_jobs.submit(ctx)
        self.assertEqual(self.run_job()['id'], unrelated['id'])
        self.assertTrue(check_jobs.raw_read(self.project, unrelated['id'])['passed'])
        value = check_jobs.raw_read(self.project, job['id'])
        value['acknowledged'] = True
        write_json(path / 'job.json', value)
        # Fixed cache cap, without constructing 1024 full source trees.
        with patch.object(Path, 'glob', return_value=[path / 'job.json'] * 1024):
            check_jobs._make_room(self.project)
        self.assertFalse(path.exists())

    def test_marked_oversized_result_recovery_disposes_without_promoting_raw_proof(self):
        from pathlib import Path
        ctx, job, path = self.oversized_result_job()
        raw = (path / 'result.json').read_bytes()
        value = check_jobs.raw_read(self.project, job['id'])
        value.pop('acceptance_retention_refusal')
        value['acceptance_scope'] = acceptance.scope(self.project)
        value['acknowledged'] = True
        write_json(path / 'job.json', value)  # Earlier finalizer's pending producer.
        self.assertIsNone(acceptance.recover_job(self.project, job['id']))
        self.assert_size_refusal(job)
        self.assertEqual((path / 'result.json').read_bytes(), raw)
        disposed = (path / 'job.json').read_bytes()
        acceptance.recover_producers(self.project)
        self.assertEqual((path / 'job.json').read_bytes(), disposed)
        # Enlarging the bound must never upgrade a disposed raw producer.
        self.project.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, file_bytes=len(raw)))
        self.assertIsNone(acceptance.recover_job(self.project, job['id']))
        self.assert_size_refusal(job)
        self.project.set_control(paused=True)
        storage.backup(self.project, self.root / 'recovered-backup.tar.gz')
        with patch.object(Path, 'glob', return_value=[path / 'job.json'] * 1024):
            check_jobs._make_room(self.project)
        self.assertFalse(path.exists())

    def test_recovery_size_disposition_does_not_mask_invalid_evidence(self):
        ctx, job, path = self.oversized_result_job()
        result = path / 'result.json'
        raw = result.read_bytes()
        value = check_jobs.raw_read(self.project, job['id'])
        value.pop('acceptance_retention_refusal')
        value['acceptance_scope'] = acceptance.scope(self.project)
        write_json(path / 'job.json', value)
        pending = (path / 'job.json').read_bytes()
        for invalid in (b'not json', b'{}', canonical({**json.loads(raw), 'code_digest': 'b' * 64,
                                                    'commands': []})):
            with self.subTest(invalid=len(invalid)):
                result.write_bytes(invalid)
                with self.assertRaises(Denied):
                    acceptance.recover_job(self.project, job['id'])
                self.assertEqual((path / 'job.json').read_bytes(), pending)
        result.write_bytes(raw)
        linked = self.root / 'linked-result'
        os.link(result, linked)
        try:
            with self.assertRaises(Denied):
                acceptance.recover_job(self.project, job['id'])
            self.assertEqual((path / 'job.json').read_bytes(), pending)
        finally:
            linked.unlink()
        result.rename(linked)
        self.symlink(linked, result)
        with self.assertRaises(Denied):
            acceptance.recover_job(self.project, job['id'])
        self.assertEqual((path / 'job.json').read_bytes(), pending)
        result.unlink()
        linked.rename(result)
        value['acceptance_scope']['image'] = 'invalid'
        write_json(path / 'job.json', value)
        with self.assertRaises(Denied):
            acceptance.recover_job(self.project, job['id'])
        self.assertNotIn('acceptance_retention_refusal', check_jobs.raw_read(self.project, job['id']))

    def test_recovery_after_object_before_index_and_finalized_job_before_object(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        observed = []
        def fail_once(root, value):
            self.assertFalse(held(root / 'locks/check-jobs.lock'))
            observed.append(value)
            raise OSError('simulated evidence interruption')
        with patch('mizu.acceptance._retain', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.run_job()
        self.assertEqual(check_jobs.raw_read(self.project, job['id'])['status'], 'completed')
        self.assertNotIn('acceptance_receipt_id', check_jobs.raw_read(self.project, job['id']))
        self.assertEqual(len(observed), 1)
        # Recover directly from producer, then remove only the index to model
        # the durable-object/index crash boundary and recover from the object.
        receipt_id = acceptance.recover_job(self.project, job['id'])
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        index.unlink()
        acceptance.recover(self.project.root)
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot)['receipt_id'], receipt_id)
        acceptance.recover_producers(self.project)
        self.assertEqual(acceptance.audit(self.project.root), 1)

    def test_backup_restore_and_job_eviction_keep_proof(self):
        ctx, job, proof = self.produce()
        shutil.rmtree(self.project.root / 'check-jobs')
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        self.project.set_control(paused=True)
        archive = self.root / 'backup.tar.gz'
        storage.backup(self.project, archive)
        storage.restore(self.config, 'restored', archive)
        restored = Project(self.config, 'restored')
        self.assertEqual(acceptance.effective(restored, ctx.snapshot), proof)
        self.assertEqual(acceptance.audit(restored.root), 1)
        storage.audit(restored)

    def test_invalid_objects_indexes_and_dangling_references_fail_closed(self):
        ctx, job, proof = self.produce()
        path = self.object_path(proof['receipt_id'])
        raw = path.read_bytes()
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        index_raw = index.read_bytes()
        for invalid in (b'not json', b'{}', raw.replace(b'"passed":true', b'"passed":false'), b'x' * (acceptance.MAX_BYTES + 1)):
            with self.subTest(invalid=len(invalid)):
                path.write_bytes(invalid)
                with self.assertRaises((Denied, LimitExceeded)):
                    acceptance.effective(self.project, ctx.snapshot)
                with self.assertRaises((Denied, LimitExceeded)):
                    storage.audit(self.project)
        path.write_bytes(raw)
        for value in ({'schema': 1, 'receipt_id': '../outside'}, {'schema': 1, 'receipt_id': 'b' * 64}, {'schema': True, 'receipt_id': proof['receipt_id']}):
            write_json(index, value)
            with self.assertRaises(Denied):
                acceptance.effective(self.project, ctx.snapshot)
            with self.assertRaises(Denied):
                acceptance.audit(self.project.root)
        index.write_bytes(index_raw)
        path.unlink()
        with self.assertRaises(Denied):
            acceptance.audit(self.project.root)

    def test_unsafe_receipt_and_parent_paths_fail_closed(self):
        ctx, job, proof = self.produce()
        path = self.object_path(proof['receipt_id'])
        outside = self.root / 'outside.json'
        path.replace(outside)
        self.symlink(outside, path)
        with self.assertRaises(Denied):
            acceptance.effective(self.project, ctx.snapshot)
        with self.assertRaises(Denied):
            acceptance.audit(self.project.root)
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(Denied):
            acceptance.get(self.project.root, proof['receipt_id'])
        path.unlink()
        outside.replace(path)
        store = self.project.root / 'evidence/acceptance'
        store.rename(self.root / 'moved-store')
        self.symlink(self.root / 'moved-store', store, target_is_directory=True)
        with self.assertRaises(Denied):
            acceptance.effective(self.project, ctx.snapshot)
        with self.assertRaises(Denied):
            acceptance.recover_producers(self.project)

    def test_normalization_refuses_invalid_producer_results(self):
        ctx, job, proof = self.produce()
        record = json.loads((check_jobs.root(self.project) / job['id'] / 'result.json').read_bytes())
        bound = acceptance.scope(self.project)
        producer = {'kind': 'check_verify', 'id': job['id'], 'run': ctx.run_dir.name}
        for key, value in (('passed', False), ('snapshot_representable', False), ('unchanged_during_verification', False),
                           ('dependencies_unchanged', False), ('code_digest', '../bad'), ('image', 'sha256:' + 'b' * 64),
                           ('commands', []), ('run', 'different'), ('job', 'b' * 64)):
            bad = {**record, key: value}
            with self.subTest(key=key), self.assertRaises(Denied):
                acceptance.retain(self.project, bad, bound, producer=producer)
        self.assertIsNone(acceptance.retain(self.project, record, None, producer=producer))
        for key, value in (('commands', ['other']), ('execution', 'b' * 64), ('environment', 'c' * 64)):
            # Exact scope is indexed separately; changing the selector cannot
            # resolve the original proof, even if its short preview is equal.
            changed = {**bound, key: value}
            self.assertNotEqual(acceptance._scope(changed), acceptance._scope(bound))

    def test_first_index_never_changes_and_exact_ids_survive_new_success(self):
        ctx, first, proof = self.produce()
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        raw = index.read_bytes()
        later = self.context()
        second = check_jobs.submit(later, fresh=True)
        self.run_job()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(index.read_bytes(), raw)
        self.assertEqual(acceptance.audit(self.project.root), 2)
        self.assertEqual(acceptance.get(self.project.root, proof['receipt_id'])['producer']['id'], first['id'])
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        self.assertIsNone(acceptance.effective(self.project, {**ctx.snapshot, 'skipped': ['unsupported']}))

    def test_sync_failure_mutation_and_changed_dependencies_do_not_retain(self):
        self.configure(detached=False)
        dependency = self.root / 'dependency'
        dependency.write_text('original')
        self.configure(detached=False, mounts=({'source': str(dependency), 'target': '/dependency'},))
        for mode in ('failure', 'source', 'dependency'):
            ctx = self.context()
            def execute(workspace, script, **kwargs):
                result = FakeSandbox(None, None, None, None).execute(workspace, script)
                if mode == 'failure':
                    result['exit_code'] = 1
                if mode == 'source':
                    (workspace / 'app.py').write_bytes(b'changed')
                if mode == 'dependency':
                    dependency.write_text('changed')
                return result
            with self.subTest(mode=mode), patch.object(ctx.sandbox, 'execute', side_effect=execute):
                self.assertFalse(ctx._op_verify({})['passed'])
            self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_sync_changed_execution_inputs_fail_current_verdict_too(self):
        self.configure(detached=False)
        config, commands = self.project.config, self.project.verify
        for mode in ('execution', 'commands'):
            self.project.config, self.project.verify = config, commands
            ctx = self.context()
            def execute(workspace, script, **kwargs):
                value = FakeSandbox(None, None, None, None).execute(workspace, script)
                if mode == 'execution':
                    self.project.config = dataclasses.replace(config,
                        limits=dataclasses.replace(config.limits,
                            command_seconds=config.limits.command_seconds + 1))
                else:
                    self.project.verify = ('different command',)
                return value
            with self.subTest(mode=mode), patch.object(ctx.sandbox, 'execute', side_effect=execute):
                self.assertFalse(ctx._op_verify({})['passed'])
                self.assertFalse(ctx.verification['dependencies_unchanged'])
                self.assertTrue(ctx.verification['unchanged_during_verification'])
                self.assertEqual(acceptance.audit(self.project.root), 0)
        self.project.config, self.project.verify = config, commands

    def test_unbound_network_image_and_oversized_scope_cannot_project(self):
        ctx, _, _ = self.produce()
        self.configure(network='bridge')
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.configure(network='none', image='mutable:latest')
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.configure(image='sha256:' + 'a' * 64)
        self.project.verify = ('echo ' + 'x' * acceptance.MAX_BYTES,)
        self.assertIsNone(acceptance.scope(self.project))
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))

    def test_unsafe_store_rejects_new_producer_without_writing_outside(self):
        outside = self.root / 'outside'
        outside.mkdir()
        evidence = self.project.root / 'evidence'
        self.symlink(outside, evidence, target_is_directory=True)
        ctx = self.context()
        job = check_jobs.submit(ctx)
        # The producer refuses the unsafe ancestor before writing evidence.
        with self.assertRaises(Denied):
            self.run_job()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn('acceptance_receipt_id', check_jobs.raw_read(self.project, job['id']))

    def test_restore_refuses_dangling_index_and_backup_refuses_unsafe_proof(self):
        import tarfile
        ctx, job, proof = self.produce()
        self.project.set_control(paused=True)
        archive = self.root / 'good.tar.gz'
        storage.backup(self.project, archive)
        damaged = self.root / 'bad.tar.gz'
        with tarfile.open(archive, 'r:gz') as source, tarfile.open(damaged, 'w:gz') as output:
            for entry in source:
                if entry.name == f'evidence/acceptance/objects/{proof["receipt_id"]}.json':
                    continue
                data = source.extractfile(entry) if entry.isfile() else None
                output.addfile(entry, data)
        with self.assertRaises(Denied):
            storage.restore(self.config, 'bad-restore', damaged)
        self.assertFalse((self.config.data / 'projects/bad-restore').exists())
        self.object_path(proof['receipt_id']).write_bytes(b'invalid')
        with self.assertRaises(Denied):
            storage.backup(self.project, self.root / 'refused.tar.gz')
        self.assertFalse((self.root / 'refused.tar.gz').exists())

    def test_selection_carries_exact_receipt_separate_from_raw_verdict(self):
        from mizu.selection import work_inputs
        ctx, _, proof = self.produce()
        bounds = {'max_proposals': 1, 'max_decisions': 1, 'max_text_bytes': 512}
        value = work_inputs(self.project, ctx.role, ctx.snapshot, bounds)
        self.assertFalse(value['evidence']['snapshot']['verified'])
        self.assertEqual(value['evidence']['snapshot']['retained_verification'], proof)

    def test_unrepresentable_frozen_result_never_mints_proof(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        capture = self.project.snapshots.capture_files
        calls = 0
        def unsupported_after(workspace):
            nonlocal calls
            value = capture(workspace)
            calls += 1
            if calls == 2:
                value['skipped'] = ['unsupported']
            return value
        with patch.object(self.project.snapshots, 'capture_files', side_effect=unsupported_after):
            self.run_job()
        self.assertFalse(check_jobs.raw_read(self.project, job['id'])['passed'])
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_finalization_race_cancel_after_command_and_before_cleanup(self):
        self.configure(check_cache_paths=('build',))
        ctx = self.context()
        # Use a configured excluded build directory, not a source mutation.
        ctx.project.snapshots.excludes += ('build',)
        job = check_jobs.submit(ctx)
        original_rmtree = shutil.rmtree
        def cancel_during_cleanup(path, *args, **kwargs):
            if path.name == 'input':
                check_jobs.cancel(ctx, job['id'])
            return original_rmtree(path, *args, **kwargs)
        with patch('mizu.check_jobs.shutil.rmtree', side_effect=cancel_during_cleanup):
            self.run_job()
        self.assertEqual(check_jobs.raw_read(self.project, job['id'])['status'], 'cancelled')
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_runner_stop_after_last_command_prevents_receipt(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        stop = threading.Event()
        class Stops(FakeSandbox):
            def execute(inner, *args, **kwargs):
                value = super().execute(*args, **kwargs)
                stop.set()
                return value
        check_jobs.run_one(self.project, stop, sandbox_factory=Stops)
        self.assertEqual(check_jobs.raw_read(self.project, job['id'])['status'], 'cancelled')
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_eviction_waits_for_retention_then_ignores_cache_loss(self):
        from pathlib import Path
        from mizu.errors import Busy
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with patch('mizu.acceptance._retain', side_effect=OSError('receipt not yet durable')):
            with self.assertRaises(OSError):
                self.run_job()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        value = check_jobs.raw_read(self.project, job['id'])
        value['acknowledged'] = True
        write_json(path, value)
        # Model the fixed cache cap without constructing 1024 source trees.
        with patch.object(Path, 'glob', return_value=[path] * 1024):
            with self.assertRaises(Busy):
                check_jobs._make_room(self.project)
        receipt_id = acceptance.recover_job(self.project, job['id'])
        with patch.object(Path, 'glob', return_value=[path] * 1024):
            check_jobs._make_room(self.project)
        self.assertFalse(path.exists())
        self.assertIsNone(acceptance.recover_job(self.project, job['id']))
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot)['receipt_id'], receipt_id)

    def test_concurrent_recovery_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        ctx, _, proof = self.produce()
        next((self.project.root / 'evidence/acceptance/index').glob('*/*.json')).unlink()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(acceptance.recover, self.project.root) for _ in range(4)]
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        self.assertEqual(acceptance.audit(self.project.root), 1)

    def test_index_cannot_substitute_valid_proof_from_another_scope(self):
        ctx, _, first = self.produce()
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        commands = self.project.verify
        self.project.verify = ('different acceptance command',)
        _, _, second = self.produce()
        self.project.verify = commands
        self.assertNotEqual(first['receipt_id'], second['receipt_id'])
        write_json(index, {'schema': 1, 'receipt_id': second['receipt_id']})
        with self.assertRaises(Denied):
            acceptance.effective(self.project, ctx.snapshot)
        with self.assertRaises(Denied):
            acceptance.audit(self.project.root)
