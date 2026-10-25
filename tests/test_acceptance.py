"""Bounded fake-runner tests; no claim about a real container runtime."""
import dataclasses
import json
import os
import shutil
import threading
from unittest.mock import patch

from support import Fixture, bind_delivery
from test_check_jobs import FakeSandbox
from mizu import acceptance, check_jobs, storage
from mizu.errors import Busy, Denied, LimitExceeded
from mizu.fs import canonical, digest, held, lock, write_json
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
        item = self.project.work_items.create(actor='worker', idempotency_key='late-delivery',
            title='Close the frozen delivered tree', scope='Retained delivery')['record']['work_item']
        binding = bind_delivery(self.project, item['id'], snapshot['id'])
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
        completed = self.project.work_items.transition(item['id'], expected_rev=binding['rev'],
            status='done', actor='worker', snapshot_id=snapshot['id'])
        self.assertEqual(completed['verification'], proof)
        self.assertEqual(self.project.work_items.get(item['id']), completed)
        self.assertEqual(manifest.read_bytes(), raw)
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

    def test_config_legal_empty_commands_cannot_project(self):
        from mizu.selection import work_inputs
        ctx, _, _ = self.produce()
        settings = self.project.root / 'project.toml'
        original = settings.read_text()
        for commands in ([''], ['true', '']):
            with self.subTest(commands=commands):
                settings.write_text(original.replace(
                    'verify = ' + json.dumps(list(self.project.verify)),
                    'verify = ' + json.dumps(commands)))
                project = Project(self.config, self.project.name)
                self.assertEqual(project.verify, tuple(commands))
                self.assertIsNone(acceptance.scope(project))
                self.assertIsNone(acceptance.effective(project, ctx.snapshot))
                self.assertIsNone(project.status()['effective_verification'])
                value = work_inputs(project, ctx.role, ctx.snapshot,
                    {'max_proposals': 1, 'max_decisions': 1, 'max_text_bytes': 512})
                self.assertFalse(value['evidence']['snapshot']['verified'])
                self.assertIsNone(value['evidence']['snapshot']['retained_verification'])

    def test_scope_does_not_hide_environment_or_execution_failures(self):
        for function in ('environment', 'execution_key'):
            with self.subTest(function=function), patch(
                    'mizu.check_jobs.' + function, side_effect=Denied('computation failed')):
                with self.assertRaisesRegex(Denied, 'computation failed'):
                    acceptance.scope(self.project)

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

    def test_digest_consistent_invalid_stored_scope_still_refuses(self):
        from mizu.selection import work_inputs
        ctx, _, proof = self.produce()
        receipt = acceptance.get(self.project.root, proof['receipt_id'])
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        for commands in ([''], ['true', '']):
            with self.subTest(commands=commands):
                value = {**receipt, 'scope': {**receipt['scope'], 'commands': commands}}
                raw = canonical(value)
                receipt_id = digest(raw)
                path = self.object_path(receipt_id)
                path.write_bytes(raw)
                write_json(index, {'schema': 1, 'receipt_id': receipt_id})
                index_raw = index.read_bytes()
                readers = (
                    lambda: acceptance.get(self.project.root, receipt_id),
                    lambda: acceptance.effective(self.project, ctx.snapshot),
                    self.project.status,
                    lambda: work_inputs(self.project, ctx.role, ctx.snapshot,
                        {'max_proposals': 1, 'max_decisions': 1, 'max_text_bytes': 512}),
                    lambda: acceptance.audit(self.project.root),
                )
                for reader in readers:
                    # The filename matches the bytes: scope validation, not
                    # a receipt identity/hash mismatch, must refuse these.
                    with self.assertRaisesRegex(Denied, 'Invalid acceptance scope'):
                        reader()
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(index.read_bytes(), index_raw)
                path.unlink()

    def test_malformed_stored_index_still_refuses_projection(self):
        from mizu.selection import work_inputs
        ctx, _, proof = self.produce()
        path = self.object_path(proof['receipt_id'])
        raw = path.read_bytes()
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        for invalid in (b'not json', b'{}', canonical(
                {'schema': True, 'receipt_id': proof['receipt_id']})):
            with self.subTest(invalid=invalid):
                index.write_bytes(invalid)
                # Exact-ID access does not use the index or hide its corruption.
                self.assertEqual(acceptance.get(self.project.root, proof['receipt_id']), json.loads(raw))
                for reader in (
                        lambda: acceptance.effective(self.project, ctx.snapshot),
                        self.project.status,
                        lambda: work_inputs(self.project, ctx.role, ctx.snapshot,
                            {'max_proposals': 1, 'max_decisions': 1, 'max_text_bytes': 512}),
                        lambda: acceptance.audit(self.project.root)):
                    with self.assertRaises(Denied):
                        reader()
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(index.read_bytes(), invalid)

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

    def pending_producer(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with patch('mizu.acceptance._retain', side_effect=OSError('simulated retention interruption')):
            with self.assertRaises(OSError):
                self.run_job()
        path = check_jobs.root(self.project) / job['id']
        self.assertIn('acceptance_scope', check_jobs.raw_read(self.project, job['id']))
        self.assertEqual(acceptance.audit(self.project.root), 0)
        return ctx, job, path

    def queued_other(self, ctx, command='healthy'):
        self.project.verify = (command,)
        return check_jobs.submit(ctx, purpose=command)

    def assert_producer_isolated(self, filename, content, code):
        ctx, suspect, path = self.pending_producer()
        healthy = self.queued_other(ctx)
        if callable(content):
            content = canonical(content(json.loads((path / filename).read_bytes())))
        (path / filename).write_bytes(content)
        before = {name: (path / name).read_bytes() for name in ('job.json', 'result.json')}
        result = self.run_job()
        self.assertEqual(result['id'], healthy['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['invalid_producers'], {'entries': [{'job_id': suspect['id'], 'code': code}],
                                                     'total': 1, 'omitted': 0, 'coverage_complete': False})
        self.assertEqual(acceptance.audit(self.project.root), 1)
        receipt = acceptance.get(self.project.root,
                                 check_jobs.raw_read(self.project, healthy['id'])['acceptance_receipt_id'])
        self.assertEqual(receipt['producer']['id'], healthy['id'])
        for name, raw in before.items():
            self.assertEqual((path / name).read_bytes(), raw)
        self.assertEqual(acceptance.recover_producers(self.project), {suspect['id']: code})
        self.assertEqual(self.run_job()['invalid_producers'], result['invalid_producers'])
        self.assertEqual(acceptance.audit(self.project.root), 1)
        self.assertFalse((path / 'quarantine.json').exists())

    def test_isolate_result_json_and_run_unrelated_job(self):
        self.assert_producer_isolated('result.json', b'not json', 'result_json')

    def test_isolate_result_nonobject_and_run_unrelated_job(self):
        self.assert_producer_isolated('result.json', b'[]', 'result_integrity')

    def test_isolate_result_digest_and_run_unrelated_job(self):
        self.assert_producer_isolated('result.json', lambda r: {**r, 'code_digest': 'b' * 64},
                                     'result_integrity')

    def test_isolate_job_json_before_scheduler_sort(self):
        self.assert_producer_isolated('job.json', b'not json', 'job_json')

    def test_isolate_job_nonobject_before_scheduler_sort(self):
        self.assert_producer_isolated('job.json', b'[]', 'job_schema')

    def test_isolate_job_scope_and_run_unrelated_job(self):
        self.assert_producer_isolated('job.json', lambda j: {**j, 'acceptance_scope':
                                     {**j['acceptance_scope'], 'image': 'invalid'}}, 'job_scope')

    def test_isolate_unbound_job_list_timestamp_before_scheduler_sort(self):
        self.assert_producer_isolated('job.json', lambda j:
            {**{k: v for k, v in j.items() if k != 'acceptance_scope'}, 'created_at': []},
            'job_schema')

    def test_isolate_unbound_job_object_timestamp_before_scheduler_sort(self):
        self.assert_producer_isolated('job.json', lambda j:
            {**{k: v for k, v in j.items() if k != 'acceptance_scope'}, 'created_at': {}},
            'job_schema')

    def test_isolate_job_json_parser_recursion_before_scheduler_sort(self):
        payload = b'[' * 10000 + b'0' + b']' * 10000
        try:
            parsed = json.loads(payload)
        except RecursionError:
            expected = 'job_json'
        else:
            self.assertIsInstance(parsed, list)
            expected = 'job_schema'
        self.assert_producer_isolated('job.json', payload, expected)

    def test_isolate_result_json_parser_recursion_and_run_unrelated_job(self):
        payload = b'[' * 10000 + b'0' + b']' * 10000
        try:
            parsed = json.loads(payload)
        except RecursionError:
            expected = 'result_json'
        else:
            self.assertIsInstance(parsed, list)
            expected = 'result_integrity'
        self.assert_producer_isolated('result.json', payload, expected)

    def _recursion_shim(self, suspect):
        import types
        real = json
        original_loads = real.loads

        def fake_loads(s, *args, **kwargs):
            if s == suspect:
                raise RecursionError('simulated parser recursion')
            if isinstance(s, str) and isinstance(suspect, bytes):
                try:
                    if s.encode('utf-8') == suspect:
                        raise RecursionError('simulated parser recursion')
                except UnicodeEncodeError:
                    pass
            return original_loads(s, *args, **kwargs)

        class _Shim(types.SimpleNamespace):
            def __getattr__(self, name):
                return getattr(real, name)
        return _Shim(loads=fake_loads)

    def _assert_recursion_path_isolated(self, filename, code):
        suspect = b'[' * 10000 + b'0' + b']' * 10000
        ctx, suspect_job, path = self.pending_producer()
        healthy = self.queued_other(ctx)
        (path / filename).write_bytes(suspect)
        before = {name: (path / name).read_bytes() for name in ('job.json', 'result.json')}
        shim = self._recursion_shim(suspect)
        with patch('mizu.acceptance.json', shim):
            result = self.run_job()
            self.assertEqual(result['id'], healthy['id'])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['invalid_producers'], {'entries': [{'job_id': suspect_job['id'], 'code': code}],
                                                         'total': 1, 'omitted': 0, 'coverage_complete': False})
            self.assertEqual(acceptance.audit(self.project.root), 1)
            receipt = acceptance.get(self.project.root,
                                     check_jobs.raw_read(self.project, healthy['id'])['acceptance_receipt_id'])
            self.assertEqual(receipt['producer']['id'], healthy['id'])
            for name, raw in before.items():
                self.assertEqual((path / name).read_bytes(), raw)
            self.assertEqual(acceptance.recover_producers(self.project), {suspect_job['id']: code})
            self.assertEqual(self.run_job()['invalid_producers'], result['invalid_producers'])
            self.assertEqual(acceptance.audit(self.project.root), 1)
            self.assertFalse((path / 'quarantine.json').exists())

    def test_isolate_job_json_recursion_error_path_before_scheduler_sort(self):
        self._assert_recursion_path_isolated('job.json', 'job_json')

    def test_isolate_result_json_recursion_error_path_and_run_unrelated_job(self):
        self._assert_recursion_path_isolated('result.json', 'result_json')

    def test_invalid_producer_bound_filters_all_and_batch_deduplicates(self):
        ctx = self.context()
        healthy = self.queued_other(ctx)
        second = check_jobs.submit(self.context('reviewer'), 'printf second', kind='exec')
        invalid = {}
        for number in range(35):
            job_id = f'{number:064x}'
            path = check_jobs.root(self.project) / job_id
            path.mkdir()
            (path / 'job.json').write_bytes(b'not json')
            invalid[job_id] = 'job_json'
        result = check_jobs.run_pending(self.project, threading.Event(), sandbox_factory=FakeSandbox)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['invalid_producers'], {'entries': [{'job_id': key, 'code': invalid[key]}
                                         for key in sorted(invalid)[:32]], 'total': 35, 'omitted': 3, 'coverage_complete': False})
        self.assertEqual(acceptance.recover_producers(self.project), invalid)
        self.assertEqual(len(FakeSandbox.calls), 2)
        self.assertIn('acceptance_receipt_id', check_jobs.raw_read(self.project, healthy['id']))
        self.assertEqual(check_jobs.raw_read(self.project, second['id'])['status'], 'completed')
        self.project.set_control(paused=True)
        self.assertEqual(self.run_job(), {'status': 'deferred', 'invalid_producers': result['invalid_producers']})

    def test_post_finalization_content_failure_is_reported_without_mutation(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        original = acceptance.recover_job
        before = {}
        def corrupt_then_recover(project, job_id):
            path = check_jobs.root(project) / job_id
            if (path / 'result.json').exists():
                (path / 'result.json').write_bytes(b'not json')
                before.update({name: (path / name).read_bytes() for name in ('job.json', 'result.json')})
            return original(project, job_id)
        with patch('mizu.acceptance.recover_job', side_effect=corrupt_then_recover):
            result = self.run_job()
        self.assertEqual(result['id'], job['id'])
        self.assertEqual(result['invalid_producers']['entries'], [{'job_id': job['id'], 'code': 'result_json'}])
        for name, raw in before.items():
            self.assertEqual((check_jobs.root(self.project) / job['id'] / name).read_bytes(), raw)
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_invalid_producer_keeps_pending_proof_eviction_pin(self):
        from pathlib import Path
        from mizu.errors import Busy
        ctx, job, path = self.pending_producer()
        value = check_jobs.raw_read(self.project, job['id'])
        value['acknowledged'] = True
        write_json(path / 'job.json', value)
        (path / 'result.json').write_bytes(b'not json')
        pending = (path / 'job.json').read_bytes()
        self.run_job()
        with patch.object(Path, 'glob', return_value=[path / 'job.json'] * 1024):
            with self.assertRaises(Busy):
                check_jobs._make_room(self.project)
        self.assertEqual((path / 'job.json').read_bytes(), pending)
        self.assertTrue(path.exists())

    def test_valid_durable_proof_survives_corrupt_producer_cache(self):
        ctx, job, proof = self.produce()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        path.write_bytes(b'not json')
        self.assertEqual(self.run_job()['invalid_producers']['total'], 1)
        self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        self.assertEqual(acceptance.audit(self.project.root), 1)
        self.assertEqual(path.read_bytes(), b'not json')

    def test_backup_recovers_healthy_proof_but_refuses_unresolved_producer(self):
        ctx, suspect, path = self.pending_producer()
        healthy = self.queued_other(ctx)
        (path / 'result.json').write_bytes(b'not json')
        with patch('mizu.acceptance._retain', side_effect=OSError('simulated retention interruption')):
            with self.assertRaises(OSError):
                self.run_job()
        before = {name: (path / name).read_bytes() for name in ('job.json', 'result.json')}
        self.project.set_control(paused=True)
        archive = self.root / 'invalid-backup.tar.gz'
        with self.assertRaisesRegex(Denied, 'Backup refused: unresolved invalid acceptance producers') as caught:
            storage.backup(self.project, archive)
        self.assertIn(suspect['id'], str(caught.exception))
        self.assertFalse(archive.exists())
        self.assertIn('acceptance_receipt_id', check_jobs.raw_read(self.project, healthy['id']))
        self.assertEqual(acceptance.audit(self.project.root), 1)
        for name, raw in before.items():
            self.assertEqual((path / name).read_bytes(), raw)

    def test_producer_isolation_does_not_swallow_path_io_or_durable_failures(self):
        ctx, job, path = self.pending_producer()
        pending = (path / 'job.json').read_bytes()
        result_raw = (path / 'result.json').read_bytes()
        for error in (Denied('unsafe read'), OSError('systemic read failure'),
                      RecursionError('read failure outside JSON parsing')):
            with self.subTest(error=type(error).__name__):
                with patch('mizu.acceptance.safe_read', side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        self.run_job()
                    self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
        for target in ('job.json', 'result.json'):
            source = path / target
            linked = self.root / 'linked-producer'
            os.link(source, linked)
            try:
                with self.assertRaises(Denied) as caught:
                    self.run_job()
                self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
            finally:
                linked.unlink()
            source.rename(linked)
            self.symlink(linked, source)
            try:
                with self.assertRaises(Denied) as caught:
                    self.run_job()
                self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
            finally:
                source.unlink()
                linked.rename(source)
        with patch('mizu.acceptance.atomic_write', side_effect=Denied('write refusal')):
            with self.assertRaises(Denied) as caught:
                self.run_job()
            self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
        self.assertEqual((path / 'job.json').read_bytes(), pending)
        self.assertEqual((path / 'result.json').read_bytes(), result_raw)
        proof = acceptance.recover_job(self.project, job['id'])
        self.object_path(proof).write_bytes(b'not json')
        with self.assertRaises(Denied) as caught:
            self.run_job()
        self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
        with self.assertRaises(Denied):
            acceptance.audit(self.project.root)

    def test_producer_scan_refuses_shared_directory_links_and_io(self):
        from pathlib import Path
        self.produce()
        base = check_jobs.root(self.project)
        moved = self.project.root / 'moved-check-jobs'
        base.rename(moved)
        self.symlink(moved, base, target_is_directory=True)
        try:
            with self.assertRaises(Denied):
                self.run_job()
        finally:
            base.unlink()
            moved.rename(base)
        original = Path.iterdir
        def broken_iterdir(path):
            if path == base:
                raise PermissionError('shared directory unreadable')
            return original(path)
        with patch.object(Path, 'iterdir', broken_iterdir):
            with self.assertRaises(PermissionError):
                self.run_job()

    def test_backup_run_recovery_stays_strict(self):
        ctx = self.context()
        (ctx.run_dir / 'verification.json').write_bytes(b'not json')
        with self.assertRaises(Denied):
            acceptance.recover_producers(self.project, include_runs=True)

    def test_isolate_normalization_failure_without_retaining_proof(self):
        self.assert_producer_isolated('result.json', lambda r: {**r, 'commands': []}, 'result_integrity')

    def test_isolate_finalized_job_binding_failure(self):
        self.assert_producer_isolated('job.json', lambda j: {**j, 'commands': ['mismatched']}, 'job_integrity')

    def test_durable_index_corruption_is_not_a_producer_diagnostic(self):
        ctx, job, proof = self.produce()
        index = next((self.project.root / 'evidence/acceptance/index').glob('*/*.json'))
        index.write_bytes(b'not json')
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        value = check_jobs.raw_read(self.project, job['id'])
        value.pop('acceptance_receipt_id')  # Retry the durable object/index boundary.
        write_json(path, value)
        pending = path.read_bytes()
        with self.assertRaises(Denied) as caught:
            self.run_job()
        self.assertNotIsInstance(caught.exception, acceptance.InvalidProducer)
        self.assertEqual(path.read_bytes(), pending)
        self.assertEqual(index.read_bytes(), b'not json')
        with self.assertRaises(Denied):
            acceptance.audit(self.project.root)

    def configure_cache(self):
        self.configure(check_cache_paths=('target',))
        self.config = dataclasses.replace(self.config, exclude=(*self.config.exclude, 'target'))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude
        return self.project.root / 'check-cache' / check_jobs.execution_key(self.config) / 'target'

    def test_scope_has_no_caller_controlled_cache_lock_bypass(self):
        self.configure_cache()
        cache_lock = self.project.root / 'locks/check-cache.lock'
        original = check_jobs.cache_input

        def fingerprint(project):
            self.assertTrue(held(cache_lock))
            return original(project)

        with patch('mizu.check_jobs.cache_input', side_effect=fingerprint):
            self.assertIsNotNone(acceptance.scope(self.project))
        with self.assertRaises(TypeError):
            acceptance.scope(self.project, _cache_locked=True)

    def test_cache_input_is_bound_to_retained_scope_and_current_import(self):
        cache = self.configure_cache()
        ctx, job, proof = self.produce()
        receipt = acceptance.get(self.project.root, proof['receipt_id'])
        original = check_jobs.read(self.project, job['id'])
        self.assertEqual(receipt['scope']['cache_input'], original['cache_input'])
        self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        cache.mkdir(parents=True)
        (cache / 'object').write_text('different input')
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        later = self.context()
        self.assertFalse(check_jobs.result(later, job['id'])['accepted_for_current_source'])
        self.assertIsNone(later.verification)
        # The old receipt remains auditable but cannot attest the new inputs.
        self.assertEqual(acceptance.get(self.project.root, proof['receipt_id']), receipt)
        self.assertEqual(acceptance.audit(self.project.root), 1)

    def test_retained_selection_excludes_cache_mutation_until_receipt_selected(self):
        cache = self.configure_cache()
        ctx, job, proof = self.produce()
        attempted, mutated = threading.Event(), threading.Event()
        outcomes = []
        cache_lock = self.project.root / 'locks/check-cache.lock'

        def mutate():
            try:
                with lock(cache_lock, blocking=False):
                    outcomes.append('unlocked')
            except Busy:
                outcomes.append('blocked')
            finally:
                attempted.set()
            with check_jobs.cache_guard(self.project):
                cache.mkdir(parents=True)
                (cache / 'object').write_text('changed by another cache owner')
                mutated.set()

        worker = threading.Thread(target=mutate)
        original_index = acceptance._index
        original_present = acceptance._present
        original_selected = acceptance._selected

        def index(*args):
            # Start a competing owner immediately after the fingerprint, before
            # even looking up the immutable index for that old cache input.
            worker.start()
            self.assertTrue(attempted.wait(10), 'cache contender did not run')
            self.assertEqual(outcomes, ['blocked'])
            self.assertFalse(mutated.is_set())
            return original_index(*args)

        def present(*args):
            self.assertTrue(held(cache_lock))
            self.assertFalse(mutated.is_set())
            return original_present(*args)

        def selected(*args):
            self.assertTrue(held(cache_lock))
            self.assertFalse(mutated.is_set())
            result = original_selected(*args)
            self.assertFalse(mutated.is_set())
            return result

        try:
            with patch('mizu.acceptance._index', side_effect=index), \
                    patch('mizu.acceptance._present', side_effect=present), \
                    patch('mizu.acceptance._selected', side_effect=selected):
                self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        finally:
            if worker.ident is not None:
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(mutated.is_set())
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))

    def test_no_cache_scope_and_selection_need_no_cache_lock_or_fingerprint(self):
        ctx, job, proof = self.produce()
        original_selected = acceptance._selected

        def selected(*args):
            self.assertFalse(held(self.project.root / 'locks/check-cache.lock'))
            return original_selected(*args)

        with patch('mizu.check_jobs.cache_input', side_effect=AssertionError('unexpected cache fingerprint')), \
                patch('mizu.acceptance._selected', side_effect=selected):
            self.assertNotIn('cache_input', acceptance.scope(self.project))
            self.assertEqual(acceptance.effective(self.project, ctx.snapshot), proof)
        self.assertFalse((self.project.root / 'locks/check-cache.lock').exists())

    def test_present_cache_pass_reuses_only_unchanged_cache_bytes(self):
        cache = self.configure_cache()
        cache.mkdir(parents=True)
        (cache / 'object').write_text('one')
        ctx, job, proof = self.produce()
        self.assertTrue(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertEqual(check_jobs.submit(self.context())['shared_job'], job['id'])
        (cache / 'object').write_text('two')
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        new = check_jobs.submit(self.context())
        self.assertNotIn('shared_job', new)
        self.assertEqual(self.run_job()['status'], 'completed')
        new_proof = acceptance.effective(self.project, ctx.snapshot)
        self.assertNotEqual(new_proof['receipt_id'], proof['receipt_id'])

    def test_mutated_cache_retains_pre_execution_proof_not_post_execution_pass(self):
        cache = self.configure_cache()
        ctx = self.context()
        job = check_jobs.submit(ctx)
        before = check_jobs.read(self.project, job['id'])['cache_input']
        class Compiler(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                (workspace / 'target').mkdir()
                (workspace / 'target/object').write_text('compiled')
                return super().execute(workspace, script, **kwargs)
        self.assertTrue(self.run_job(Compiler)['passed'])
        original = check_jobs.read(self.project, job['id'])
        receipt = acceptance.get(self.project.root, original['acceptance_receipt_id'])
        self.assertEqual(receipt['scope']['cache_input'], before)
        self.assertNotEqual(acceptance.scope(self.project)['cache_input'], before)
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.assertEqual((cache / 'object').read_text(), 'compiled')
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])

    def test_legacy_unbound_scope_does_not_match_configured_cache(self):
        self.configure_cache()
        ctx, job, proof = self.produce()
        # An old proof is valid historical evidence, but omits this input.
        raw = check_jobs.read(self.project, job['id'])
        legacy = dict(raw['acceptance_scope'])
        legacy.pop('cache_input')
        record = json.loads((check_jobs.root(self.project) / job['id'] / 'result.json').read_bytes())
        legacy_id = acceptance.retain(self.project, record, legacy,
            producer={'kind': 'check_verify', 'id': job['id'], 'run': ctx.run_dir.name})
        self.assertNotEqual(proof['receipt_id'], legacy_id)
        current_index = self.project.root / acceptance._index(self.project.root, raw['code_digest'], raw['acceptance_scope'])
        current_index.unlink()
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.assertIsNotNone(acceptance.get(self.project.root, legacy_id))

    def test_unbound_cache_disables_scope_and_current_acceptance(self):
        cache = self.configure_cache()
        ctx, job, proof = self.produce()
        cache.mkdir(parents=True)
        (cache / 'object').write_text('too big')
        self.configure(check_input_bytes=1)
        self.assertIsNone(acceptance.scope(self.project))
        self.assertIsNone(acceptance.effective(self.project, ctx.snapshot))
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])
        self.assertIsNotNone(acceptance.get(self.project.root, proof['receipt_id']))
        self.configure(check_input_bytes=8589934592)
        (cache / 'object').unlink()
        self.symlink(self.root / 'outside', cache / 'object')
        self.assertIsNone(acceptance.scope(self.project))
        self.assertFalse(check_jobs.result(ctx, job['id'])['accepted_for_current_source'])

    def test_cache_scope_must_match_producer_and_be_a_digest(self):
        self.configure_cache()
        ctx, job, proof = self.produce()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        raw = check_jobs.raw_read(self.project, job['id'])
        raw['acceptance_scope']['cache_input'] = 'f' * 64
        write_json(path, raw)
        with self.assertRaises(acceptance.InvalidProducer):
            acceptance.recover_job(self.project, job['id'])
        bound = acceptance.scope(self.project)
        for bad in (None, 'invalid', 2):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                acceptance._scope({**bound, 'cache_input': bad})
