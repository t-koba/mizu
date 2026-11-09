"""Admitted reservation handoff only: no child, runner, or quiescence claim."""
import concurrent.futures
import copy
import contextlib
import dataclasses
import threading
from unittest.mock import Mock, patch

from support import Fixture, ScriptDriver
from mizu import delegation_jobs as jobs
from mizu.config import role_policy_text
from mizu.engine_config import adapter_digest, child_authority_digest, effective, local_settings_digest
from mizu.errors import Busy, ConfigError, Denied
from mizu.fs import held, lock, read_json
from mizu.runtime import Context, Engine
from mizu.storage import quiescent


class EndParent(Exception):
    """Stop the fake driver without attempting an unsupported reserved finish."""


class DelegationHandoffTests(Fixture):
    def setUp(self):
        super().setUp()
        worker = dataclasses.replace(self.config.roles['worker'],
                                     capabilities=(*self.config.roles['worker'].capabilities, 'delegate'))
        helper = dataclasses.replace(self.config.roles['searcher'], name='helper',
                                     capabilities=('files', 'read', 'check_job'))
        self.config = dataclasses.replace(
            self.config, roles={**self.config.roles, 'worker': worker, 'helper': helper})
        self.engine = Engine(self.config)
        self.workspace_lock = self.project.root / 'locks' / 'workspace.lock'
        self.request = {'role': 'helper', 'task': 'Implement the focused slice',
                        'profile': None, 'source_snapshot': self.project.snapshots.get()['id']}

    def run_parent(self, work):
        def callback(ctx, *_):
            self.parent = ctx
            work(ctx)
            raise EndParent()
        self.engine.driver = ScriptDriver(callback)
        with self.assertRaises(EndParent):
            self.engine.run(self.project, 'worker')
        self.assertFalse(held(self.workspace_lock))
        self.assertFalse(self.parent._workspace_lease.active)

    def reserve(self, ctx, key='first', request=None):
        return self.engine._reserve_delegation(ctx, key, request or self.request)

    def contenders(self, expected):
        # A distinct role exclusion makes the writer contend on workspace.lock,
        # not the parent's role lock. Exercise real admission and maintenance.
        role = dataclasses.replace(self.config.roles['worker'], name='other_writer')
        with contextlib.ExitStack() as stack, self.assertRaises(expected):
            self.engine._admit(self.project, role.name, role, stack, wait=False)
        with self.assertRaises(expected):
            with quiescent(self.project, require_pause=False):
                self.fail('Maintenance crossed the handoff')

    def test_live_run_commits_under_original_lock_and_binds_minimal_gate(self):
        def work(ctx):
            self.assertTrue(held(self.workspace_lock))
            view = self.reserve(ctx)
            self.assertEqual(view, ctx._reservation_view)
            self.assertEqual(set(view), {'id', 'owner', 'status', 'request'})
            self.assertEqual(view['request'], {'source_snapshot': ctx.snapshot['id']})
            self.assertEqual(view['status'], 'queued')
            self.assertEqual(jobs.DelegationJobs(self.project.root).reservation()['id'], view['id'])
            self.assertTrue(held(self.workspace_lock))
            self.assertEqual(self.reserve(ctx), view)
            for key, request in [('second', self.request),
                                 ('first', {**self.request, 'task': 'Conflicting task'})]:
                with self.assertRaises(Denied):
                    self.reserve(ctx, key, request)
                self.assertEqual(ctx._reservation_view, view)
            with patch.dict(Context._DISPATCH, files=Mock(), insights=Mock(return_value={'ok': True})) as handlers:
                for dispatch in (ctx.handle, ctx.dispatch):
                    with self.assertRaises(Denied):
                        dispatch('files', {})
                    self.assertEqual(dispatch('insights', {}), {'ok': True})
                handlers['files'].assert_not_called()
            # Returned values cannot mutate the trusted Context binding.
            view['request']['source_snapshot'] = 'f' * 64
            self.assertEqual(ctx._reservation_view['request']['source_snapshot'], ctx.snapshot['id'])
        self.run_parent(work)
        self.contenders(Denied)

    def test_resolved_authority_is_exact_private_and_does_not_launch(self):
        def work(ctx):
            role = dataclasses.replace(self.config.roles['helper'], workspace='write',
                                       capabilities=('files', 'read', 'finish'))
            settings = effective(self.config, role, role.profile)
            expected = child_authority_digest(
                self.config, role, spend_role=ctx.role, settings=settings,
                policy=role_policy_text(role), goal_digest=ctx.goal_digest, workspace=ctx.workspace,
                adapter_identity=adapter_digest(settings['engine']),
                local_settings=local_settings_digest(self.config, settings['engine']))
            with patch.object(self.engine, 'resolve') as driver, \
                 patch.object(self.engine, 'delegate') as delegate, \
                 patch.object(jobs.DelegationJobs, 'runner_bind') as bind, \
                 patch.object(jobs.DelegationJobs, 'runner_started') as claim:
                view = self.reserve(ctx)
                driver.assert_not_called()
                delegate.assert_not_called()
                bind.assert_not_called()
                claim.assert_not_called()
            store = jobs.DelegationJobs(self.project.root)
            saved = store.reservation()
            self.assertEqual(saved['request'], {**self.request, 'profile': role.profile,
                                               'authority_digest': expected})
            for public in (view, ctx._reservation_view, store.status('worker', saved['id'])):
                self.assertNotIn(expected, str(public))
                self.assertNotIn(self.request['task'], str(public))
                self.assertNotIn(saved['token'], str(public))
        self.run_parent(work)

    def recover(self, engine, saved):
        # Recovery must be read-only, including when called by a fresh host.
        before = {p.relative_to(self.project.root): p.read_bytes()
                  for p in self.project.root.rglob('*') if p.is_file()}
        with contextlib.ExitStack() as stack:
            for target, method in ((engine, 'resolve'), (engine, 'delegate'), (engine, '_execute'),
                                   (jobs.DelegationJobs, 'start'), (jobs.DelegationJobs, 'runner_bind'),
                                   (jobs.DelegationJobs, 'runner_started')):
                stack.enter_context(patch.object(target, method, side_effect=AssertionError('launch/write')))
            try:
                return engine._resolve_delegation_request(self.project, saved['owner'], saved['request'])
            finally:
                self.assertEqual(before, {p.relative_to(self.project.root): p.read_bytes()
                                          for p in self.project.root.rglob('*') if p.is_file()})

    def test_recovery_after_parent_exit_uses_exact_pinned_selector_snapshot(self):
        from mizu.classification import prepare
        from mizu.selection import validate_selectors
        spec = {'retry_seconds': 30, 'rules': [
            {'when': {'path': 'task.state', 'op': 'contains', 'value': 'pinned marker'},
             'candidates': [{'profile': 'alternate'}]},
            {'candidates': [{'profile': 'primary'}]}]}
        validate_selectors({'dynamic': spec}, self.config.profiles, self.file.parent)
        helper = dataclasses.replace(self.config.roles['helper'], profile='', selector='dynamic')
        self.config = dataclasses.replace(self.config, roles={**self.config.roles, 'helper': helper},
                                          selectors={'dynamic': spec})
        self.engine.config = self.config
        snapshots = self.project.snapshots
        def publish(state):
            snap = snapshots.create(snapshots.capture_files(self.project.workspace), goal=self.project.goal,
                                    state=state, run=None, outcome='wait', summary='Selector fixture')
            snapshots.publish(snap)
            return snap
        pinned = publish('pinned marker')
        self.request['source_snapshot'] = pinned['id']
        def work(ctx):
            newer = publish('different current state')
            self.assertNotEqual(newer['id'], ctx.snapshot['id'])
            with patch('mizu.classification.prepare', wraps=prepare) as preview:
                self.reserve(ctx)
                self.assertEqual(preview.call_args.args[3], pinned)
        self.run_parent(work)
        del self.parent  # No parent Context or active admission lease is needed.
        saved = jobs.DelegationJobs(self.project.root).reservation()
        self.assertEqual(saved['request']['profile'], 'alternate')
        recovery = Engine(self.config)
        with patch('mizu.classification.prepare', wraps=prepare) as preview:
            self.assertEqual(self.recover(recovery, saved), saved['request'])
            self.assertEqual(preview.call_args.args[3], pinned)
            self.assertTrue(preview.call_args.kwargs['preview'])
        drift = copy.deepcopy(spec)
        drift['rules'][0]['candidates'] = [{'profile': 'primary'}]
        changed = dataclasses.replace(self.config, selectors={'dynamic': drift})
        with self.assertRaises(Denied):
            self.recover(Engine(changed), saved)

    def test_recovery_refuses_legacy_missing_or_changed_inputs_without_writes(self):
        self.run_parent(self.reserve)
        saved = jobs.DelegationJobs(self.project.root).reservation()
        self.assertEqual(self.recover(Engine(self.config), saved), saved['request'])
        legacy = {k: v for k, v in saved['request'].items() if k != 'authority_digest'}
        for request in (legacy, {**saved['request'], 'authority_digest': 'f' * 64},
                        {**saved['request'], 'source_snapshot': 'f' * 64},
                        {**saved['request'], 'profile': 'primary'}):
            with self.subTest(request=request), self.assertRaises(Denied):
                self.recover(Engine(self.config), {**saved, 'request': request})
        base = self.config
        helper = base.roles['helper']
        profile = {**base.profiles[helper.profile], 'model': 'changed-model'}
        for changed in (
                dataclasses.replace(base, profiles={**base.profiles, helper.profile: profile}),
                dataclasses.replace(base, roles={**base.roles, 'worker': dataclasses.replace(
                    base.roles['worker'], tools_per_run=base.limits.tools_per_run + 1)}),
                dataclasses.replace(base, roles={k: v for k, v in base.roles.items() if k != 'helper'}),
                dataclasses.replace(base, roles={**base.roles, 'helper': dataclasses.replace(
                    helper, capabilities=(*helper.capabilities, 'search'))}),
                dataclasses.replace(base, roles={**base.roles, 'helper': dataclasses.replace(
                    helper, policy=(self.root / 'missing-policy.md',))}),
                dataclasses.replace(base, limits=dataclasses.replace(
                    base.limits, tools_per_run=base.limits.tools_per_run + 1))):
            with self.assertRaises((Denied, ConfigError, OSError)):
                self.recover(Engine(changed), saved)
        manifest = self.project.root / 'snapshots' / (self.request['source_snapshot'] + '.json')
        original = manifest.read_bytes()
        try:
            for content in (None, original.replace(b'"goal":', b'"changed_goal":', 1)):
                if content is None:
                    manifest.unlink()
                else:
                    manifest.write_bytes(content)
                with self.assertRaises(Denied):
                    self.recover(Engine(self.config), saved)
        finally:
            manifest.write_bytes(original)
        self.assertEqual(self.recover(Engine(self.config), saved), saved['request'])

    def test_unreconstructible_admission_refuses_before_first_write(self):
        def work(ctx):
            for field, changed in (('goal_digest', 'f' * 64),
                                   ('workspace', self.root / 'other-workspace'),
                                   ('spend_role', self.config.roles['reporter'])):
                with self.subTest(field=field), patch.object(ctx, field, changed), self.assertRaises(Denied):
                    self.reserve(ctx)
                self.assertIsNone(ctx._workspace_lease.attempt)
                self.assertFalse((self.project.root / 'delegation-jobs').exists())
            self.reserve(ctx)
        self.run_parent(work)

    def test_owner_without_delegate_grant_refuses_before_store_mutation(self):
        def work(ctx):
            stripped = dataclasses.replace(
                self.config.roles['worker'],
                capabilities=tuple(c for c in self.config.roles['worker'].capabilities if c != 'delegate'))
            changed = dataclasses.replace(self.config, roles={**self.config.roles, 'worker': stripped})
            with patch.object(self.engine, 'config', changed), self.assertRaises(Denied):
                self.reserve(ctx)
            self.assertIsNone(ctx._workspace_lease.attempt)
            self.assertFalse((self.project.root / 'delegation-jobs').exists())
            self.reserve(ctx)
        self.run_parent(work)
        saved = jobs.DelegationJobs(self.project.root).reservation()
        before = (self.project.root / 'delegation-jobs/state.json').read_bytes()
        base = self.config
        stripped = dataclasses.replace(
            base.roles['worker'],
            capabilities=tuple(c for c in base.roles['worker'].capabilities if c != 'delegate'))
        revoked = dataclasses.replace(base, roles={**base.roles, 'worker': stripped})
        with self.assertRaises(Denied):
            self.recover(Engine(revoked), saved)
        self.assertEqual((self.project.root / 'delegation-jobs/state.json').read_bytes(), before)
        self.assertEqual(self.recover(Engine(self.config), saved), saved['request'])

    def test_authority_drift_refuses_exact_lease_retry_without_changing_hold(self):
        def work(ctx):
            view = self.reserve(ctx)
            path = self.project.root / 'delegation-jobs/state.json'
            before = path.read_bytes()
            base = self.config
            helper = base.roles['helper']
            profile = copy.deepcopy(base.profiles[helper.profile])
            profile['model'] = 'changed-model'
            for index, changed in enumerate((
                    dataclasses.replace(base, roles={**base.roles, 'helper': dataclasses.replace(
                        helper, capabilities=(*helper.capabilities, 'search'))}),
                    dataclasses.replace(base, roles={**base.roles, 'helper': dataclasses.replace(
                        helper, profile='primary')}),
                    dataclasses.replace(base, profiles={**base.profiles, helper.profile: profile}),
                    dataclasses.replace(base, limits=dataclasses.replace(
                        base.limits, tools_per_run=base.limits.tools_per_run + 1)))):
                with self.subTest(change=index), patch.object(self.engine, 'config', changed):
                    with self.assertRaises(Denied):
                        self.reserve(ctx)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(ctx._reservation_view, view)
            # Pinned goal/workspace and actual shared-spend authority participate.
            for field, changed in (('goal_digest', 'f' * 64),
                                   ('workspace', self.root / 'other-workspace'),
                                   ('spend_role', base.roles['reporter'])):
                with self.subTest(field=field), patch.object(ctx, field, changed), self.assertRaises(Denied):
                    self.reserve(ctx)
                self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.reserve(ctx), view)
        self.run_parent(work)

    def test_selector_uses_trusted_preview_and_unresolved_inputs_never_start(self):
        helper = dataclasses.replace(self.config.roles['helper'], profile='', selector='dynamic')
        self.config = dataclasses.replace(self.config, roles={**self.config.roles, 'helper': helper})
        self.engine.config = self.config
        def work(ctx):
            for decision in ({'reason': 'waiting'}, {'profile': 'missing-profile'}):
                with patch('mizu.classification.prepare', return_value=decision) as preview, \
                     self.assertRaises((Denied, ConfigError)):
                    self.reserve(ctx)
                self.assertTrue(preview.call_args.kwargs['preview'])
                self.assertIsNone(ctx._workspace_lease.attempt)
                self.assertIsNone(jobs.DelegationJobs.existing_reservation(self.project.root))
            with patch('mizu.classification.prepare', return_value={'profile': 'alternate'}) as preview:
                view = self.reserve(ctx)
                self.assertTrue(preview.call_args.kwargs['preview'])
                self.assertEqual(jobs.DelegationJobs(self.project.root).reservation()['request']['profile'],
                                 'alternate')
            before = (self.project.root / 'delegation-jobs/state.json').read_bytes()
            for decision in ({'profile': 'primary'}, {'reason': 'waiting'}):
                with patch('mizu.classification.prepare', return_value=decision), self.assertRaises(Denied):
                    self.reserve(ctx)
                self.assertEqual((self.project.root / 'delegation-jobs/state.json').read_bytes(), before)
                self.assertEqual(ctx._reservation_view, view)
        self.run_parent(work)

    def test_invalid_roles_profiles_and_injected_digest_refuse_before_persistence(self):
        def work(ctx):
            for overrides in ({'role': 'worker'}, {'role': 'missing'}, {'profile': 'primary'},
                              {'authority_digest': 'b' * 64}):
                with self.subTest(overrides=overrides), self.assertRaises((Denied, ConfigError)):
                    self.reserve(ctx, request={**self.request, **overrides})
                self.assertIsNone(ctx._workspace_lease.attempt)
            self.assertIsNone(jobs.DelegationJobs.existing_reservation(self.project.root))
        self.run_parent(work)

    def test_missing_foreign_role_project_context_engine_and_unheld_leases_refuse(self):
        def work(ctx):
            lease = ctx._workspace_lease
            other = self.context()
            root, owner = self.project.root, ctx.role.name
            for missing in (None, object()):
                ctx._workspace_lease = missing
                ctx._reservation_view = {}
                with self.assertRaises(Denied):
                    self.reserve(ctx)
                self.assertEqual(ctx._reservation_view, {})
            ctx._workspace_lease = lease
            cases = [(self.project.workspace, owner, self.engine, ctx),
                     (root, 'reviewer', self.engine, ctx),
                     (root, owner, Engine(self.config), ctx),
                     (root, owner, self.engine, other)]
            for args in cases:
                with self.subTest(args=args[:2]), self.assertRaises(Denied):
                    with lease.start_scope(*args, 'first', self.request):
                        self.fail('Foreign principal used the lease')
            with patch.object(jobs, 'held', return_value=False), self.assertRaises(Denied):
                self.reserve(ctx)
            role = ctx.role
            ctx.role = dataclasses.replace(role, workspace='read')
            with self.assertRaises(Denied):
                self.reserve(ctx)
            ctx.role = role
            # A still-held process lock does not revive ended parent authority.
            for field, value in [('closed', True), ('finished', {}), ('_slot_lease', False)]:
                original = getattr(ctx, field)
                with self.subTest(field=field), patch.object(ctx, field, value):
                    with self.assertRaises(Denied):
                        self.reserve(ctx)
                    with self.assertRaises(Denied):
                        with lease.start_scope(root, owner, self.engine, ctx, 'first', self.request):
                            self.fail('Ended parent used the lease directly')
                self.assertEqual(getattr(ctx, field), original)
            with self.assertRaises(Denied):
                self.reserve(ctx, request={**self.request, 'source_snapshot': 'f' * 64})
            self.assertFalse((root / 'delegation-jobs').exists())
        self.run_parent(work)
        # Even a different currently held exclusion cannot revive an old lease.
        with lock(self.workspace_lock, blocking=False), self.assertRaises(Denied):
            self.reserve(self.parent)
        self.assertIsNone(jobs.DelegationJobs.existing_reservation(self.project.root))

    def test_uncertain_acknowledgement_refreshes_from_store_and_exact_retry_is_stable(self):
        real_write = jobs.safe_write
        for committed in (False, True):
            def work(ctx):
                def fail_write(*args, **kwargs):
                    self.assertTrue(held(self.workspace_lock))
                    if committed:
                        real_write(*args, **kwargs)
                    raise OSError('uncertain acknowledgement')
                with patch.object(jobs, 'safe_write', side_effect=fail_write), self.assertRaises(OSError):
                    self.reserve(ctx)
                self.assertEqual(ctx._reservation_view,
                                 jobs.DelegationJobs.reservation_projection(self.project.root))
                if committed:
                    view = ctx._reservation_view
                    self.assertEqual(view['status'], 'queued')
                    self.assertEqual(self.reserve(ctx), view)
                else:
                    self.assertIsNone(ctx._reservation_view)
                    with self.assertRaises(Denied):
                        self.reserve(ctx, 'different')
            self.run_parent(work)
        self.contenders(Denied)

    def test_unreadable_refresh_keeps_unknown_gate_and_durable_reservation(self):
        def work(ctx):
            with patch.object(jobs.DelegationJobs, 'reservation_projection', side_effect=Denied('unreadable')):
                with self.assertRaises(Denied):
                    self.reserve(ctx)
            self.assertEqual(ctx._reservation_view, {})
            for dispatch in (ctx.handle, ctx.dispatch):
                with self.assertRaises(Denied):
                    dispatch('insights', {})
            self.assertIsNotNone(jobs.DelegationJobs.existing_reservation(self.project.root))
            self.assertEqual(self.reserve(ctx), ctx._reservation_view)
        self.run_parent(work)
        self.contenders(Denied)

    def test_writer_and_maintenance_see_lock_before_commit_and_reservation_after_release(self):
        real_save = jobs.DelegationJobs._save
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def work(ctx):
                pool.submit(self.contenders, Busy).result(timeout=10)
                def save(store, state):
                    self.assertTrue(ctx._workspace_lease.active)
                    self.assertTrue(held(self.workspace_lock))
                    self.assertIsNone(read_json(store.root / 'state.json')['active'])
                    pool.submit(self.contenders, Busy).result(timeout=10)
                    real_save(store, state)
                    # Read the durable bytes without trying to reenter store.lock.
                    self.assertEqual(read_json(store.root / 'state.json')['active'], state['active'])
                    pool.submit(self.contenders, Busy).result(timeout=10)
                with patch.object(jobs.DelegationJobs, '_save', save):
                    self.reserve(ctx)
                self.assertIsNotNone(jobs.DelegationJobs.existing_reservation(self.project.root))
            self.run_parent(work)
            pool.submit(self.contenders, Denied).result(timeout=10)

    def test_concurrent_direct_dispatch_waits_for_gate_refresh(self):
        entered, release, dispatching = threading.Event(), threading.Event(), threading.Event()
        real_start = jobs.DelegationJobs.start
        def work(ctx):
            def start(store, *args):
                result = real_start(store, *args)
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test did not release start')
                return result
            def dispatch():
                dispatching.set()
                return ctx.dispatch('files', {})
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, \
                    patch.object(jobs.DelegationJobs, 'start', start), \
                    patch.dict(Context._DISPATCH, files=Mock()) as handlers:
                pending = pool.submit(self.reserve, ctx)
                try:
                    self.assertTrue(entered.wait(10))
                    later = pool.submit(dispatch)
                    self.assertTrue(dispatching.wait(10))
                    self.assertFalse(later.done())
                finally:
                    release.set()
                pending.result(timeout=10)
                with self.assertRaises(Denied):
                    later.result(timeout=10)
                handlers['files'].assert_not_called()
        self.run_parent(work)

    def test_shutdown_revokes_before_context_close_and_workspace_release(self):
        closing, release = threading.Event(), threading.Event()
        real_close = Context.close_exec
        def close(ctx):
            # Deterministically hold the former close window: Context shutdown
            # has happened, but the run ExitStack still owns workspace.lock.
            closing.set()
            if not release.wait(10):
                raise AssertionError('test did not release shutdown')
            real_close(ctx)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool, \
                patch.object(Context, 'close_exec', close):
            pending = pool.submit(self.run_parent, lambda ctx: None)
            try:
                self.assertTrue(closing.wait(10))
                ctx = self.parent
                self.assertTrue(ctx.closed)
                self.assertFalse(ctx._slot_lease)
                with self.assertRaises(Denied):
                    self.reserve(ctx)
                self.assertFalse(ctx._workspace_lease.active)
                self.assertFalse((self.project.root / 'delegation-jobs').exists())
                self.assertTrue(held(self.workspace_lock))
                self.contenders(Busy)
            finally:
                release.set()
            pending.result(timeout=10)

    def test_shutdown_waits_for_inflight_commit_and_gate_refresh(self):
        refreshing, release, finalizing = threading.Event(), threading.Event(), threading.Event()
        real_projection = jobs.DelegationJobs.reservation_projection
        def projection(root):
            view = real_projection(root)
            refreshing.set()
            if not release.wait(10):
                raise AssertionError('test did not release refresh')
            return view
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, \
                patch.object(jobs.DelegationJobs, 'reservation_projection', projection):
            starts = []
            def work(ctx):
                mutex, run_thread = ctx.mutex, threading.get_ident()
                class ObservedMutex:
                    def __enter__(self):
                        if threading.get_ident() == run_thread:
                            finalizing.set()
                        return mutex.__enter__()
                    def __exit__(self, *args):
                        return mutex.__exit__(*args)
                ctx.mutex = ObservedMutex()
                starts.append(pool.submit(self.reserve, ctx))
                self.assertTrue(refreshing.wait(10))
                # Returning lets run_parent end the driver and enter finalization
                # while the start still owns both mutexes through gate refresh.
            pending = pool.submit(self.run_parent, work)
            try:
                self.assertTrue(finalizing.wait(10))
                ctx = self.parent
                self.assertFalse(ctx.closed)
                self.assertTrue(ctx._slot_lease)
                self.assertTrue(ctx._workspace_lease.active)
                self.assertTrue(held(self.workspace_lock))
                self.assertEqual(ctx._reservation_view, {})
                self.assertIsNotNone(jobs.DelegationJobs.existing_reservation(self.project.root))
                self.contenders(Busy)
            finally:
                release.set()
            view = starts[0].result(timeout=10)
            pending.result(timeout=10)
            self.assertEqual(ctx._reservation_view, view)
            self.assertTrue(ctx.closed)
            self.contenders(Denied)
