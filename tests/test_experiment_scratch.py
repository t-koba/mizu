"""Small staged-cache fixtures, not container-runtime or build benchmarks."""
import dataclasses
import threading
from unittest.mock import patch

from support import Fixture
from mizu import acceptance, check_jobs, platform as host_platform
from mizu.errors import Denied
from mizu.fs import held, lock, read_json, write_json
from mizu.process import Result
from mizu.protocol import DEFINITIONS, validate
from mizu.sandbox import Sandbox, bind_args


class FakeSandbox:
    def __init__(self, config, project, role, directory, **kwargs):
        pass

    def execute(self, workspace, script, **kwargs):
        return {'id': 'a' * 32, 'exit_code': 0, 'reason': 'exited', 'script': script}


class ScratchTests(Fixture):
    def setUp(self):
        super().setUp()
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, 'check_job'))
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles,
            exclude=(*self.config.exclude, 'target', 'cache'),
            sandbox=dataclasses.replace(self.config.sandbox,
                check_cache_paths=('target', 'cache'), check_reuse=True))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude
        self.cache = self.project.root / 'check-cache' / check_jobs.execution_key(self.config)

    def submit(self, ctx=None, **kwargs):
        return check_jobs.submit(ctx or self.context(), 'compile', scratch_paths=['target'], **kwargs)

    def run_job(self, factory=FakeSandbox, stop=None):
        return check_jobs.run_one(self.project, stop or threading.Event(), sandbox_factory=factory)

    def test_schema_and_subset_validation(self):
        for op, base in [('experiment', {'script': 'compile'}),
                         ('check_job', {'action': 'start', 'script': 'compile'})]:
            schema = DEFINITIONS[op][1]
            validate({**base, 'scratch_paths': ['target']}, schema)
            validate({**base, 'scratch_paths': []}, schema)
            for names in [['target', 'target'], ['../target'], 'target', [1], None,
                          ['name' + str(i) for i in range(33)], ['x' * 256]]:
                with self.subTest(op=op, names=names), self.assertRaises(Denied):
                    validate({**base, 'scratch_paths': names}, schema)
        ctx = self.context()
        for names in [['unknown'], ['target', 'target'], ['../target'], [1], 'target', ['x' * 256]]:
            with self.subTest(names=names), self.assertRaises(Denied):
                check_jobs.submit(ctx, 'compile', scratch_paths=names)
        from mizu.sandbox import scratch_selection
        many = tuple('cache' + str(index) for index in range(33))
        too_many = dataclasses.replace(self.config, sandbox=dataclasses.replace(
            self.config.sandbox, check_cache_paths=many))
        with self.assertRaises(Denied):
            scratch_selection(too_many, many)
        first = check_jobs.submit(ctx, 'compile')
        self.assertEqual(first['id'], check_jobs.submit(ctx, 'compile', scratch_paths=[])['id'])
        selected = check_jobs.submit(ctx, 'compile', scratch_paths=['target', 'cache'])
        self.assertEqual(selected['id'], check_jobs.submit(ctx, 'compile', scratch_paths=['cache', 'target'])['id'])
        self.assertNotEqual(first['id'], selected['id'])
        self.assertNotEqual(selected['id'], self.submit(ctx)['id'])
        # Empty selection remains a no-op on the generic check_job start.
        verification = self.context()._op_check_job({'action': 'start', 'scratch_paths': []})
        self.assertEqual(verification['kind'], 'verify')
        with self.assertRaises(Denied):
            self.context()._op_check_job({'action': 'list', 'scratch_paths': []})

    def test_only_queued_custom_experiments_accept_selection(self):
        ctx = self.context()
        reader = self.context('reviewer')
        for who, op, extra in [(ctx, 'experiment', {}),
                               (ctx, 'experiment', {'mode': 'sync'}),
                               (ctx, 'experiment', {'source_receipt': 'a' * 64}),
                               (ctx, 'experiment', {'mode': 'async', 'source_receipt': 'a' * 64}),
                               (ctx, 'exec', {}), (reader, 'exec', {'mode': 'async'})]:
            with self.subTest(op=op, extra=extra), self.assertRaises(Denied):
                who._op_exec_experiment(op, {'script': 'compile', 'scratch_paths': ['target'], **extra})
        # An empty selection is equivalent to omission and preserves direct routing.
        direct_result = {
                'id': 'b' * 32, 'kind': 'experiment', 'exit_code': 0, 'reason': 'exited',
                'seconds': 0.1, 'writable': False, 'script': 'compile', 'stdout': '', 'stderr': ''}
        with patch.object(ctx.sandbox, 'execute', return_value=dict(direct_result)) as spy:
            result = ctx._op_exec_experiment('experiment', {'script': 'compile', 'mode': 'sync', 'scratch_paths': []})
            self.assertNotIn('status', result)
            self.assertNotIn('scratch_paths', spy.call_args.kwargs)
        with patch.object(ctx.sandbox, 'execute', return_value=dict(direct_result)) as spy:
            result = ctx._op_exec_experiment('experiment', {'script': 'compile', 'scratch_paths': []})
            self.assertNotIn('status', result)
            self.assertNotIn('scratch_paths', spy.call_args.kwargs)
        ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(c for c in ctx.role.capabilities if c != 'check_job'))
        with self.assertRaises(Denied):
            ctx._op_exec_experiment('experiment', {'script': 'compile', 'mode': 'async', 'scratch_paths': ['target']})
        for op in ('exec', 'verify'):
            with self.assertRaises(Denied):
                validate({'script': 'compile', 'scratch_paths': ['target']} if op == 'exec'
                         else {'scratch_paths': ['target']}, DEFINITIONS[op][1])
        for script, kind in [(None, 'experiment'), ('compile', 'exec'), ('compile', 'verify')]:
            with self.assertRaises(Denied):
                check_jobs.submit(reader, script, kind=kind, scratch_paths=['target'])
        for args in [{'action': 'start'}, {'action': 'list'}, {'action': 'join', 'id': 'a' * 64}]:
            with self.assertRaises(Denied):
                reader._op_check_job({**args, 'scratch_paths': ['target']})
        first = self.context()._op_exec_experiment('experiment', {'script': 'compile', 'mode': 'async', 'scratch_paths': ['target']})
        self.assertEqual(first['scratch_paths'], ['target'])
        second = self.context()._op_check_job({'action': 'start', 'script': 'compile', 'scratch_paths': ['target']})
        self.assertNotEqual(first['id'], second['id'])

    def test_two_runs_stage_only_selected_artifacts_and_never_import_acceptance(self):
        observed = []
        (self.cache / 'cache').mkdir(parents=True)
        (self.cache / 'cache/unselected').write_text('untouched')
        before = self.project.snapshots.capture_files(self.project.workspace)['code_digest']
        test = self

        class Compiler(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                test.assertEqual(kwargs, {'writable': False, 'experiment': True, 'scratch_paths': ('target',)})
                test.assertTrue(held(test.project.root / 'locks/check-cache.lock'))
                test.assertFalse((test.cache / 'target').exists())
                test.assertFalse((workspace / 'cache').exists())
                artifact = workspace / 'target/object'
                observed.append(artifact.read_text() if artifact.exists() else None)
                artifact.write_text('compiled')
                return super().execute(workspace, script, **kwargs)

        for _ in range(2):
            ctx = self.context()
            job = self.submit(ctx)
            self.assertEqual(self.run_job(Compiler)['status'], 'completed')
            result = check_jobs.result(ctx, job['id'])
            self.assertTrue(result['result']['passed'])
            self.assertNotIn('accepted_for_current_source', result)
            self.assertIsNone(ctx.verification)
            self.assertFalse((ctx.run_dir / 'verification.json').exists())
            self.assertNotIn('acceptance_scope', check_jobs.read(self.project, job['id']))
            self.assertFalse((check_jobs.root(self.project) / job['id'] / 'input').exists())
        self.assertEqual(observed, [None, 'compiled'])
        self.assertEqual((self.cache / 'cache/unselected').read_text(), 'untouched')
        self.assertFalse((self.project.workspace / 'target').exists())
        self.assertEqual(self.project.snapshots.capture_files(self.project.workspace)['code_digest'], before)

    def test_scratch_never_shares_results_even_when_reuse_is_enabled(self):
        ctx = self.context()
        first = self.submit(ctx)
        queued = self.submit()
        self.assertNotEqual(queued['id'], first['id'])
        self.assertNotIn('shared_job', queued)
        self.assertIsNotNone(check_jobs.read(self.project, first['id'])['environment'])
        self.assertIsNone(check_jobs.read(self.project, first['id'])['shared_key'])
        self.run_job()
        third = self.submit()
        self.assertNotIn('shared_job', third)
        self.assertFalse(check_jobs.available(ctx))
        with self.assertRaises(Denied):
            check_jobs.join(self.context(), first['id'])
        # Same request in one run remains one demand, not a cross-job cache hit.
        self.assertEqual(self.submit(ctx)['id'], first['id'])

    def test_cache_lock_precedes_slot_and_does_not_block_unrelated_work(self):
        scratch = self.submit()
        ordinary = check_jobs.submit(self.context(), 'unrelated')
        cache_lock = self.project.root / 'locks/check-cache.lock'
        attempts = []

        def traced(path, **kwargs):
            attempts.append(path)
            if path.parent == self.config.data / 'check-slots':
                self.assertTrue(held(cache_lock))
            return lock(path, **kwargs)

        with lock(cache_lock):
            self.assertEqual(self.run_job()['id'], ordinary['id'])
            self.assertEqual(self.run_job()['status'], 'busy')
            self.assertFalse(held(self.config.data / 'check-slots/0.lock'))
            self.assertEqual(check_jobs.read(self.project, scratch['id'])['status'], 'queued')
        with patch('mizu.check_jobs.lock', side_effect=traced):
            self.assertEqual(self.run_job()['id'], scratch['id'])
        slot = self.config.data / 'check-slots/0.lock'
        self.assertLess(attempts.index(cache_lock), attempts.index(slot))

    def test_cancellation_interruption_and_timeout_discard_staged_scratch(self):
        for action in ('cancel', 'stop', 'raise', 'timeout'):
            with self.subTest(action=action):
                (self.cache / 'target').mkdir(parents=True, exist_ok=True)
                (self.cache / 'target/old').write_text('old')
                ctx = self.context()
                job = self.submit(ctx)
                stop = threading.Event()

                class Aborted(FakeSandbox):
                    def execute(inner, workspace, script, **kwargs):
                        (workspace / 'target/partial').write_text('partial')
                        if action == 'cancel':
                            check_jobs.cancel(ctx, job['id'])
                        elif action == 'stop':
                            stop.set()
                        elif action == 'raise':
                            raise Denied('synthetic interruption')
                        result = super().execute(workspace, script, **kwargs)
                        if action == 'timeout':
                            result.update(reason='timeout', exit_code=None)
                        return result

                with patch('mizu.check_jobs.cleanup'):
                    result = self.run_job(Aborted, stop)
                self.assertIn(result['status'], ('cancelled', 'interrupted', 'completed'))
                self.assertFalse((self.cache / 'target').exists())
                self.assertFalse((check_jobs.root(self.project) / job['id'] / 'input').exists())

    def test_crashed_runner_discards_scratch_without_retry(self):
        job = self.submit()
        directory = check_jobs.root(self.project) / job['id']
        (directory / 'input/target').mkdir()
        (directory / 'input/target/partial').write_text('partial')
        record = check_jobs.read(self.project, job['id'])
        write_json(directory / 'job.json', {**record, 'status': 'running'})
        with patch('mizu.check_jobs.cleanup'):
            self.assertEqual(self.run_job()['status'], 'interrupted')
        self.assertFalse((directory / 'input').exists())
        self.assertFalse((self.cache / 'target').exists())

    def test_cleanup_failure_retains_staged_scratch_for_recovery(self):
        job = self.submit()

        class Unsafe(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                (workspace / 'target/partial').write_text('partial')
                raise Denied('container cleanup failed')

        with patch('mizu.check_jobs.cleanup', side_effect=Denied('still running')):
            self.assertEqual(self.run_job(Unsafe)['status'], 'cleanup_failed')
        self.assertTrue((check_jobs.root(self.project) / job['id'] / 'input/target/partial').exists())
        self.assertFalse((self.cache / 'target').exists())

    def test_source_changes_and_unexcluded_cache_are_refused(self):
        job = self.submit()

        class Mutator(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                (workspace / 'app.py').write_text('changed source')
                return super().execute(workspace, script, **kwargs)

        result = self.run_job(Mutator)
        self.assertFalse(result['passed'])
        record = read_json(check_jobs.root(self.project) / job['id'] / 'result.json')
        self.assertFalse(record['unchanged_during_verification'])
        self.project.snapshots.excludes = tuple(p for p in self.config.exclude if p != 'target')
        pending_before = {item['id'] for item in check_jobs.pending(self.project, 'worker')}
        with self.assertRaises(Denied):
            self.submit()
        self.assertEqual({item['id'] for item in check_jobs.pending(self.project, 'worker')}, pending_before)

    def test_late_cancel_during_source_cleanup_cannot_commit_scratch(self):
        ctx = self.context()
        job = self.submit(ctx)
        unlink = type(self.root).unlink
        source = check_jobs.root(self.project) / job['id'] / 'input/app.py'

        def cancel_then_unlink(path, *args, **kwargs):
            if path == source:
                self.assertFalse(held(self.project.root / 'locks/check-jobs.lock'))
                check_jobs.cancel(ctx, job['id'])
            return unlink(path, *args, **kwargs)

        with patch.object(type(source), 'unlink', cancel_then_unlink):
            self.assertEqual(self.run_job()['status'], 'cancelled')
        self.assertFalse((self.cache / 'target').exists())
        self.assertFalse(source.parent.exists())

    def test_source_cleanup_failure_keeps_artifacts_private(self):
        job = self.submit()
        directory = check_jobs.root(self.project) / job['id']
        unlink = type(self.root).unlink

        def fail_source_cleanup(path, *args, **kwargs):
            if path == directory / 'input/app.py':
                raise OSError('synthetic source cleanup failure')
            return unlink(path, *args, **kwargs)

        with patch.object(type(self.root), 'unlink', fail_source_cleanup):
            self.assertEqual(self.run_job()['status'], 'cleanup_failed')
        self.assertTrue((directory / 'input/target').is_dir())
        self.assertFalse((self.cache / 'target').exists())

    def test_failed_exited_command_can_preserve_artifacts_not_acceptance(self):
        class Failed(FakeSandbox):
            def execute(inner, workspace, script, **kwargs):
                result = super().execute(workspace, script, **kwargs)
                return {**result, 'exit_code': 1}

        ctx = self.context()
        job = self.submit(ctx)
        result = self.run_job(Failed)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['passed'])
        self.assertTrue((self.cache / 'target').is_dir())
        check_jobs.result(ctx, job['id'])
        self.assertIsNone(ctx.verification)

    def test_durable_scratch_jobs_refuse_malformed_or_acceptance_fields(self):
        job = self.submit()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        record = read_json(path)
        for changes in [{'scratch_paths': 'target'}, {'scratch_paths': ['target', 'target']},
                        {'scratch_paths': [False]}, {'scratch_paths': ['name' + str(i) for i in range(33)]},
                        {'scratch_paths': ['x' * 256]}, {'kind': 'verify'},
                        {'shared_key': 'a' * 64}, {'acceptance_scope': {}}]:
            with self.subTest(changes=changes), self.assertRaises(acceptance.InvalidProducer):
                write_json(path, {**record, **changes})
                acceptance.read_job(self.project, job['id'])
        write_json(path, record)

    def test_command_evidence_records_selected_scratch(self):
        job = self.submit()
        directory = check_jobs.root(self.project) / job['id']
        workspace = directory / 'input'
        (workspace / 'target').mkdir()
        box = Sandbox(self.config, self.project.root, check_jobs.job_role(job['id']), directory)
        with patch('mizu.sandbox.assert_no_checkpoint'), patch('mizu.sandbox.run',
                return_value=Result(0, 'output', '', 'exited', 0.01)):
            record = box.execute(workspace, 'compile', writable=False, experiment=True,
                                 scratch_paths=('target',))
        self.assertEqual(record['scratch_paths'], ['target'])
        self.assertFalse(record['writable'])
        self.assertEqual(record['kind'], 'experiment')
        self.assertEqual(read_json(directory / 'commands' / (record['id'] + '.json'))['scratch_paths'], ['target'])

    def test_bind_args_uses_separator_safe_platform_spellings(self):
        self.assertEqual(bind_args('/host/input', '/workspace', readonly=True,
                                   selinux=True, linux=True),
                         ['--volume', '/host/input:/workspace:ro,z'])
        self.assertEqual(bind_args(r'C:\host\input', '/workspace', readonly=True,
                                   selinux=True, linux=False),
                         ['--mount', r'type=bind,src=C:\host\input,dst=/workspace,readonly'])

    def test_argv_binds_private_staging_only_and_keeps_source_readonly(self):
        job = self.submit()
        directory = check_jobs.root(self.project) / job['id']
        workspace = directory / 'input'
        (workspace / 'target').mkdir()
        box = Sandbox(self.config, self.project.root, check_jobs.job_role(job['id']), directory)
        linux = host_platform.IS_LINUX
        args = box.argv('test', workspace, 'compile', writable=False, experiment=True,
                        scratch_paths=('target',))
        for readonly, source, target in [(True, workspace, '/workspace'),
                                          (False, workspace / 'target', '/work/target')]:
            expected = bind_args(str(source), target, readonly=readonly,
                                 selinux=bool(self.config.sandbox.selinux_label) and linux, linux=linux)
            self.assertIn(expected[1], args)
        self.assertIn(f'/work:rw,nosuid,nodev,size={self.config.sandbox.temporary_mb}m,mode=1777', args)
        self.assertFalse(any(str(self.cache) in arg for arg in args))
        self.assertFalse(any('/work/cache' in arg for arg in args))
        plain = box.argv('test', workspace, 'compile', writable=False, experiment=True)
        self.assertFalse(any('/work/target' in arg for arg in plain))
        for extra in ({'experiment': False}, {'experiment': True, 'source_receipt': 'a' * 64}):
            with self.assertRaises(Denied):
                box.argv('test', workspace, 'compile', writable=False, scratch_paths=('target',), **extra)
