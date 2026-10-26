"""Read-only cache discovery with fake jobs; no container/runtime claims."""
import dataclasses
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from test_check_jobs import FakeSandbox
from mizu import acceptance, check_jobs, storage
from mizu.errors import Denied
from mizu.fs import canonical, digest, held, write_json
from mizu.runtime import Engine, prompt_for, prompt_delta_for, should_run


class DiscoveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config,
            roles={k: dataclasses.replace(v, capabilities=(*v.capabilities, 'check_job'))
                   for k, v in self.config.roles.items()},
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        FakeSandbox.calls = []

    def queue(self, role='worker', script='healthy'):
        ctx = self.context(role)
        return ctx, check_jobs.submit(ctx, script, kind='experiment')

    def leaf(self, job):
        return check_jobs.root(self.project) / job['id'] / 'job.json'

    def edit(self, job, **fields):
        value = json.loads(self.leaf(job).read_bytes())
        write_json(self.leaf(job), {**value, **fields})

    def run_job(self):
        return check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)

    def cache_bytes(self):
        return {p.relative_to(self.project.root): p.read_bytes()
                for p in check_jobs.root(self.project).rglob('*.json')}

    def assert_diagnostic(self, view, total):
        self.assertFalse(view['coverage_complete'])
        diagnostic = view['invalid_producers']
        self.assertEqual(diagnostic['total'], total)
        self.assertEqual(len(diagnostic['entries']), min(32, total))
        self.assertEqual(diagnostic['omitted'], max(0, total - 32))
        self.assertFalse(diagnostic['coverage_complete'])

    def test_mixed_roles_full_delta_and_list_share_read_only_boundary(self):
        _, bad = self.queue(script='bad')
        reader, healthy = self.queue('reviewer')
        self.edit(healthy, status='completed', passed=True)
        self.leaf(bad).write_bytes(b'not json')
        before = self.cache_bytes()
        with patch('mizu.acceptance.recover_producers', side_effect=AssertionError('mutating recovery')), \
             patch('mizu.acceptance._retain', side_effect=AssertionError('proof mutation')):
            for role in ('worker', 'reviewer'):
                ctx = self.context(role)
                for build in (lambda: prompt_for(ctx), lambda: prompt_delta_for(ctx, [])):
                    with patch('mizu.acceptance.discover', wraps=acceptance.discover) as discover:
                        prompt = json.loads(build())
                    self.assertEqual(discover.call_count, 1)
                    self.assert_diagnostic(prompt['check_discovery'], 1)
                    self.assertNotIn(bad['id'], [j['id'] for j in prompt['check_jobs']])
                    self.assertEqual([j['id'] for j in prompt['check_jobs']],
                                     [healthy['id']] if role == 'reviewer' else [])
            listing = reader._op_check_job({'action': 'list'})
            self.assertEqual([j['id'] for j in listing['jobs']], [healthy['id']])
            self.assertEqual(listing['invalid_producers']['total'], 1)
            self.assertEqual([j['id'] for j in check_jobs.pending(self.project, 'reviewer')], [healthy['id']])
            self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assertEqual(self.cache_bytes(), before)
        self.assertFalse((self.project.root / 'evidence/acceptance').exists())
        self.assertFalse(list(check_jobs.root(self.project).rglob('quarantine.json')))

    def test_nonproducer_leaf_does_not_veto_runner_or_get_a_job_identity(self):
        _, healthy = self.queue()
        path = check_jobs.root(self.project) / 'ordinary-entry' / 'job.json'
        write_json(path, {'arbitrary': 'untouched'})
        raw = path.read_bytes()
        view = check_jobs.prompt_projection(self.context())
        entry = view['check_discovery']['invalid_producers']['entries'][0]
        self.assertEqual(entry, {'entry_digest': digest(b'mizu.check-job-entry.v1\0ordinary-entry'),
                                 'code': 'non_producer_entry'})
        self.assertNotIn('ordinary-entry', json.dumps(view))
        self.assertNotIn('job_id', entry)
        with self.assertRaises(Denied):
            acceptance.InvalidProducer('ordinary-entry', 'job_json')
        result = self.run_job()
        self.assertEqual(result['id'], healthy['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['invalid_producers']['entries'], [entry])
        self.assertEqual(path.read_bytes(), raw)

    def test_all_invalid_entries_are_excluded_beyond_diagnostic_cap(self):
        ctx, healthy = self.queue()
        for index in range(35):
            path = check_jobs.root(self.project) / f'{index:064x}' / 'job.json'
            path.parent.mkdir()
            path.write_bytes(b'bad json')
            write_json(check_jobs.root(self.project) / f'ordinary-{index}' / 'job.json', [])
        before = self.cache_bytes()
        jobs, invalid = acceptance.discover(self.project)
        self.assertEqual(set(jobs), {healthy['id']})
        self.assertEqual(len(invalid), 70)
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assert_diagnostic(json.loads(text)['check_discovery'], 70)
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assertEqual(self.cache_bytes(), before)
        result = self.run_job()
        self.assertEqual(result['id'], healthy['id'])
        self.assertEqual(result['invalid_producers']['total'], 70)
        self.assertEqual(result['invalid_producers']['omitted'], 38)

    def test_consumed_field_types_are_individual_content_failures(self):
        ctx, bad = self.queue(script='bad')
        _, healthy = self.queue(script='good')
        original = self.leaf(bad).read_bytes()
        fields = {'created_at': [], 'commands': {}, 'role': [], 'run': {}, 'kind': [],
                  'status': {}, 'passed': [], 'error': {}, 'shared_job': [], 'detached': [],
                  'acknowledged': [], 'environment': [], 'execution': {}, 'code_digest': {},
                  'purpose': [], 'finished_at': [], 'replaced_by': {}, 'shared_key': {}}
        for key, value in fields.items():
            with self.subTest(key=key):
                self.leaf(bad).write_bytes(original)
                self.edit(bad, **{key: value})
                jobs, invalid = acceptance.discover(self.project)
                self.assertEqual(set(jobs), {healthy['id']})
                self.assertEqual(invalid, {bad['id']: 'job_schema'})
                self.assertEqual([j['id'] for j in check_jobs.list_jobs(self.project, 'worker')], [healthy['id']])
                self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
                json.loads(prompt_delta_for(ctx, []))
        for commands in ([{}], [None], ['ok'] * 33):
            self.leaf(bad).write_bytes(original)
            self.edit(bad, commands=commands)
            self.assertEqual(acceptance.discover(self.project)[1], {bad['id']: 'job_schema'})
        self.leaf(bad).write_bytes(original)
        value = json.loads(original)
        self.leaf(bad).write_text(json.dumps({**value, 'error': '\ud800'}))
        self.assertEqual(acceptance.discover(self.project)[1], {bad['id']: 'job_schema'})

    def test_shared_overlay_is_one_hop_and_corrupt_original_is_not_delivered(self):
        ctx, original = self.queue()
        reader = self.context('reviewer')
        subscription = check_jobs.join(reader, original['id'])
        self.edit(original, status='completed', passed=True)
        self.assertEqual(check_jobs.pending(self.project, 'reviewer')[0]['id'], subscription['id'])
        _, another = self.queue(script='another')
        # A raw intermediate subscription's own fields are used, not a recursive overlay.
        self.edit(another, shared_job=subscription['id'], status='queued')
        self.assertEqual(acceptance.discover(self.project)[0][another['id']]['status'], 'queued')
        self.leaf(original).write_bytes(b'bad json')
        jobs, invalid = acceptance.discover(self.project)
        self.assertNotIn(subscription['id'], jobs)
        self.assertEqual(invalid[subscription['id']], 'job_subscription')
        self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        self.assertIsNone(check_jobs.continuation(self.project, 'reviewer', ctx.snapshot))
        self.assertFalse(check_jobs.available(reader))

    def test_reuse_keeps_source_environment_execution_and_workspace_filters(self):
        ctx, job = self.queue()
        self.assertEqual(check_jobs.available(ctx)[0]['id'], job['id'])
        raw = self.leaf(job).read_bytes()
        for field in ('environment', 'execution', 'code_digest'):
            self.edit(job, **{field: 'f' * 64})
            self.assertFalse(check_jobs.available(ctx))
            if field == 'environment':
                self.assertTrue(check_jobs.available(ctx, validate=False)[0]['requires_input_check'])
            else:
                self.assertFalse(check_jobs.available(ctx, validate=False))
            self.leaf(job).write_bytes(raw)
        (ctx.workspace / 'app.py').write_bytes(b'changed\n')
        self.assertFalse(check_jobs.available(ctx, validate=False))
        hidden = self.context('consult')
        hidden.role = dataclasses.replace(hidden.role, workspace='none')
        self.assertFalse(check_jobs.available(hidden, validate=False))

    def test_no_grant_does_not_read_or_expose_cache(self):
        _, job = self.queue()
        self.leaf(job).write_bytes(b'bad json')
        self.config = dataclasses.replace(self.config, roles={k: dataclasses.replace(v,
            capabilities=tuple(c for c in v.capabilities if c != 'check_job'))
            for k, v in self.config.roles.items()})
        self.project.config = self.config
        ctx = self.context()
        with patch('mizu.acceptance.discover', side_effect=AssertionError('no grant')):
            self.assertFalse(check_jobs.pending(self.project, 'worker'))
            self.assertFalse(check_jobs.list_jobs(self.project, 'worker'))
            self.assertFalse(check_jobs.available(ctx))
            for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
                value = json.loads(text)
                self.assertEqual(value['check_jobs'], [])
                self.assertEqual(value['shared_checks'], [])
                self.assertNotIn('check_discovery', value)

    def test_aggregate_prompt_budget_includes_owned_shared_and_diagnostics(self):
        ctx, base = self.queue()
        template = json.loads(self.leaf(base).read_bytes())
        for index in range(45):
            job_id = f'{index:064x}'
            write_json(check_jobs.root(self.project) / job_id / 'job.json',
                       {**template, 'id': job_id, 'error': '\0' * 900,
                        'commands': ['\0' * 900] * 4, 'created_at': '\0' * 900})
        view = check_jobs.prompt_projection(ctx)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False).encode()), 65536)
        self.assertLessEqual(len(view['check_jobs']), 32)
        self.assertLessEqual(len(view['shared_checks']), 32)
        self.assertGreater(view['check_discovery']['summaries_omitted'], 0)
        self.assertTrue(view['check_discovery']['coverage_complete'])

    def test_discovery_holds_queue_lock_and_never_writes_producer_state(self):
        _, job = self.queue()
        raw = self.leaf(job).read_bytes()
        original = acceptance.read_job
        def inspect(project, job_id):
            self.assertTrue(held(project.root / 'locks/check-jobs.lock'))
            return original(project, job_id)
        with patch('mizu.acceptance.read_job', side_effect=inspect):
            acceptance.discover(self.project)
        self.assertEqual(self.leaf(job).read_bytes(), raw)

    def test_valid_unretained_result_is_inspected_without_retention(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with patch('mizu.acceptance._retain', side_effect=OSError('interrupted retention')):
            with self.assertRaises(OSError):
                self.run_job()
        before = self.cache_bytes()
        self.assertNotIn('acceptance_receipt_id', json.loads(self.leaf(job).read_bytes()))
        with patch('mizu.acceptance._retain', side_effect=AssertionError('read must not retain')):
            self.assertEqual(acceptance.discover(self.project)[1], {})
            self.assertEqual(check_jobs.pending(self.project, 'worker')[0]['id'], job['id'])
            json.loads(prompt_for(ctx))
        self.assertEqual(self.cache_bytes(), before)
        self.assertEqual(acceptance.audit(self.project.root), 0)

    def test_corrupt_result_scope_and_size_do_not_mutate_or_mint_proof(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with patch('mizu.acceptance._retain', side_effect=OSError('interrupted retention')):
            with self.assertRaises(OSError):
                self.run_job()
        path = self.leaf(job).parent / 'result.json'
        job_bytes, result_bytes = self.leaf(job).read_bytes(), path.read_bytes()
        for raw, code in ((b'not json', 'result_json'), (b'{}', 'result_integrity'),
                          (b'x' * (self.config.limits.file_bytes + 1), 'result_file_bytes')):
            with self.subTest(code=code):
                path.write_bytes(raw)
                before = self.cache_bytes()
                self.assertEqual(acceptance.discover(self.project)[1], {job['id']: code})
                self.assertFalse(check_jobs.pending(self.project, 'worker'))
                self.assert_diagnostic(json.loads(prompt_for(ctx))['check_discovery'], 1)
                self.assertEqual(self.cache_bytes(), before)
                self.assertEqual(acceptance.audit(self.project.root), 0)
        path.write_bytes(result_bytes)
        value = json.loads(job_bytes)
        self.edit(job, acceptance_scope={**value['acceptance_scope'], 'image': 'invalid'})
        self.assertEqual(acceptance.discover(self.project)[1], {job['id']: 'job_scope'})
        self.assertFalse(check_jobs.pending(self.project, 'worker'))

    def test_oversized_job_is_visible_not_silently_drained(self):
        ctx, job = self.queue()
        self.leaf(job).write_bytes(b'x' * (self.config.limits.file_bytes + 1))
        before = self.leaf(job).read_bytes()
        self.assertEqual(acceptance.discover(self.project)[1], {job['id']: 'job_file_bytes'})
        self.assertFalse(check_jobs.pending(self.project, 'worker'))
        self.assert_diagnostic(json.loads(prompt_delta_for(ctx, []))['check_discovery'], 1)
        self.assertEqual(self.leaf(job).read_bytes(), before)

    def symlink(self, source, target, **kwargs):
        try:
            os.symlink(source, target, **kwargs)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Symlinks unavailable: ' + str(exc))

    def assert_unsafe(self):
        with self.assertRaises(Denied):
            acceptance.discover(self.project)
        with self.assertRaises(Denied):
            check_jobs.pending(self.project, 'reviewer')
        with self.assertRaises(Denied):
            check_jobs.prompt_projection(self.context())
        with self.assertRaises(Denied):
            self.run_job()

    def test_nonproducer_symlink_leaf_refuses_instead_of_classifying(self):
        self.queue()
        outside = self.root / 'outside.json'
        outside.write_bytes(b'not json')
        path = check_jobs.root(self.project) / 'ordinary-entry' / 'job.json'
        path.parent.mkdir()
        self.symlink(outside, path)
        self.assert_unsafe()
        self.assertEqual(outside.read_bytes(), b'not json')

    def test_nonproducer_hardlink_leaf_refuses_before_classifying(self):
        self.queue()
        outside = self.root / 'outside.json'
        outside.write_bytes(b'not json')
        path = check_jobs.root(self.project) / 'ordinary-entry' / 'job.json'
        path.parent.mkdir()
        os.link(outside, path)
        self.assert_unsafe()
        self.assertEqual(outside.read_bytes(), b'not json')

    def test_valid_id_hardlink_and_special_leaf_refuse(self):
        _, job = self.queue()
        outside = self.root / 'outside.json'
        self.leaf(job).replace(outside)
        os.link(outside, self.leaf(job))
        self.assert_unsafe()
        self.leaf(job).unlink()
        self.leaf(job).mkdir()
        self.assert_unsafe()

    def test_shared_base_and_child_directory_links_refuse(self):
        self.queue()
        base = check_jobs.root(self.project)
        moved = self.root / 'moved-jobs'
        base.rename(moved)
        self.symlink(moved, base, target_is_directory=True)
        self.assert_unsafe()
        base.unlink()
        moved.rename(base)
        self.symlink(self.root, base / 'ordinary-entry', target_is_directory=True)
        self.assert_unsafe()

    def test_enumeration_and_read_io_errors_are_not_content_diagnostics(self):
        self.queue()
        original = Path.iterdir
        def enumerate_paths(path):
            if path == check_jobs.root(self.project):
                raise OSError('simulated directory I/O failure')
            return original(path)
        with patch.object(Path, 'iterdir', enumerate_paths):
            with self.assertRaises(OSError):
                acceptance.discover(self.project)
            with self.assertRaises(OSError):
                self.run_job()
        with patch('mizu.acceptance.safe_read', side_effect=OSError('simulated read failure')):
            with self.assertRaises(OSError):
                acceptance.discover(self.project)
        with patch('mizu.acceptance.safe_read', side_effect=Denied('unsafe shared path')):
            with self.assertRaisesRegex(Denied, 'unsafe shared path'):
                acceptance.discover(self.project)

    def test_backup_still_refuses_nonproducer_entry_without_creating_archive(self):
        self.queue()
        path = check_jobs.root(self.project) / 'ordinary-entry' / 'job.json'
        write_json(path, {})
        self.project.set_control(paused=True)
        archive = self.root / 'refused.tar.gz'
        with self.assertRaisesRegex(Denied, 'Backup refused: unresolved invalid acceptance producers') as error:
            storage.backup(self.project, archive)
        self.assertNotIn('ordinary-entry', str(error.exception))
        self.assertIn('entry_digest', str(error.exception))
        self.assertFalse(archive.exists())
        self.assertEqual(path.read_bytes(), canonical({}))

    def test_dispatch_admission_and_daemon_wake_require_explicit_wait(self):
        ctx, bad = self.queue(script='bad')
        _, healthy = self.queue(script='healthy')
        self.leaf(bad).write_bytes(b'bad json')
        # A done, observed tree with no other wake source reaches pending checks.
        snapshot = {**ctx.snapshot, 'outcome': 'done', 'wake_generation': 'test-wake',
                    'ready_work': {'ids': [], 'digest': digest(canonical([]))}}
        engine = Engine(self.config)
        # A completed owned result stays visible but cannot admit without its
        # exact explicit wait. Malformed diagnostics do not supply a wake.
        with patch('mizu.runtime._operator_woke', return_value=False), \
             patch('mizu.runtime._ready_work_changed', return_value=False):
            self.assertFalse(should_run(self.project, snapshot, role_name='worker'))
            self.assertIn('skip', engine._dispatch_state(self.project, 'worker',
                                                         self.config.roles['worker'], event_trigger=True))
            self.edit(healthy, status='completed', passed=True)
            self.assertEqual([job['id'] for job in check_jobs.pending(self.project, 'worker')], [healthy['id']])
            self.assertFalse(should_run(self.project, snapshot, role_name='worker'))
            self.assertIn('skip', engine._dispatch_state(self.project, 'worker',
                                                         self.config.roles['worker'], event_trigger=True))
            check_jobs.set_wait(self.project, 'worker', healthy['id'])
            self.assertTrue(should_run(self.project, snapshot, role_name='worker'))
            self.assertNotIn('skip', engine._dispatch_state(self.project, 'worker',
                                                            self.config.roles['worker'], event_trigger=True))
            self.assertFalse(check_jobs.pending(self.project, 'reviewer'))
        # Admission/wake reads never acknowledge either record.
        self.assertFalse(json.loads(self.leaf(healthy).read_bytes())['acknowledged'])
        self.assertEqual(self.leaf(bad).read_bytes(), b'bad json')

    def test_result_links_and_durable_proof_corruption_remain_fatal(self):
        ctx = self.context()
        job = check_jobs.submit(ctx)
        with patch('mizu.acceptance._retain', side_effect=OSError('interrupted retention')):
            with self.assertRaises(OSError):
                self.run_job()
        result = self.leaf(job).parent / 'result.json'
        outside = self.root / 'outside-result.json'
        result.replace(outside)
        os.link(outside, result)
        with self.assertRaises(Denied):
            acceptance.discover(self.project)
        result.unlink()
        self.symlink(outside, result)
        with self.assertRaises(Denied):
            acceptance.discover(self.project)
        result.unlink()
        outside.replace(result)
        receipt_id = acceptance.recover_job(self.project, job['id'])
        (self.project.root / f'evidence/acceptance/objects/{receipt_id}.json').write_bytes(b'not json')
        with self.assertRaises(Denied):
            acceptance.discover(self.project)

    def test_continuation_uses_snapshot_and_does_not_deliver_suspect_job(self):
        ctx, job = self.queue()
        check_jobs.set_wait(self.project, 'worker', job['id'], continuation={
            'run': ctx.run_dir.name, 'snapshot': ctx.snapshot['id'],
            'code_digest': ctx.snapshot['code_digest'], 'summary': 'prior report', 'state': 'prior state'})
        report = check_jobs.prompt_projection(ctx)['check_continuation']
        self.assertEqual(report['summary'], 'prior report')
        self.assertEqual(report['check']['id'], job['id'])
        before = self.cache_bytes()
        self.assertEqual(check_jobs.continuation(self.project, 'reviewer', ctx.snapshot), None)
        self.assertEqual(self.cache_bytes(), before)
        self.leaf(job).write_bytes(b'not json')
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            self.assertIsNone(json.loads(text)['check_continuation'])
        self.assertIn('prior report', (check_jobs.root(self.project) / 'worker.wait.json').read_text())

    def test_escaped_continuation_cannot_exceed_aggregate_prompt_budget(self):
        ctx, job = self.queue()
        check_jobs.set_wait(self.project, 'worker', job['id'], continuation={
            'run': ctx.run_dir.name, 'snapshot': ctx.snapshot['id'],
            'code_digest': ctx.snapshot['code_digest'], 'summary': '\0' * 4000, 'state': '\0' * 4000})
        self.edit(job, **{k: '\0' * 900 for k in ('run', 'purpose', 'created_at', 'finished_at', 'error')},
                  commands=['\0' * 900] * 4)
        before = self.cache_bytes()
        for text in (prompt_for(ctx), prompt_delta_for(ctx, [])):
            prompt = json.loads(text)
            view = {k: prompt[k] for k in ('check_jobs', 'shared_checks', 'check_continuation', 'check_discovery')}
            self.assertLessEqual(len(json.dumps(view, ensure_ascii=False).encode()), 65536)
            self.assertIsNone(prompt['check_continuation'])
            self.assertTrue(prompt['check_discovery']['continuation_omitted'])
        self.assertEqual(self.cache_bytes(), before)
