"""Configured safe-point handoff over synthetic driver protocols only."""
import dataclasses
import sys
from unittest.mock import patch

from support import Fixture, ROOT
from mizu.continuations import ready
from mizu.errors import Denied
from mizu.drivers import DriverCheckpoint
from mizu.project import Project
from mizu.runtime import Engine


class SafePointPreemptionTests(Fixture):
    def configured(self, scenario, *, quantum=1, outcome_engine="claude"):
        profile = self.config.roles["worker"].profile
        profiles = dict(self.config.profiles)
        profiles[profile] = {**profiles[profile], "engine": outcome_engine,
                             "provider": "synthetic", "model": "offline",
                             "session": "persistent", "options": {}}
        engines = dict(self.config.engines)
        engines[outcome_engine] = {**engines[outcome_engine],
            "command": (sys.executable, str(ROOT / "tests/fake_engines.py"),
                        outcome_engine, scenario)}
        roles = dict(self.config.roles)
        roles["worker"] = dataclasses.replace(roles["worker"],
            capabilities=tuple(c for c in roles["worker"].capabilities
                               if c not in ("exec", "experiment", "verify")))
        limits = dataclasses.replace(self.config.limits,
            safe_point_yield_quantum=quantum, missing_finish_followups=0)
        config = dataclasses.replace(self.config, profiles=profiles, roles=roles,
                                     engines=engines, limits=limits)
        return config, Project(config, "sample")

    def test_continue_yield_prepares_before_publication_and_commits_once(self):
        config, project = self.configured("seal-continue")
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            result = Engine(config).run(project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "continue")
        self.assertEqual(demand.call_count, 1)
        snapshot = project.snapshots.get()
        self.assertEqual(snapshot["id"], result["snapshot"])
        self.assertEqual(snapshot["outcome"], "continue")
        intent = ready(project.snapshots, "worker")
        self.assertIsNotNone(intent)
        self.assertEqual(intent["state"], "committed")
        self.assertEqual(intent["source_run"], result["run"])
        self.assertEqual(intent["snapshot"], snapshot["id"])
        self.assertNotEqual(intent["successor"], result["run"])

    def test_unpublished_prepared_intent_is_not_runnable(self):
        config, project = self.configured("seal-continue")
        before = project.snapshots.get()["id"]
        with patch("mizu.runtime.has_live_demand", return_value=True), \
             patch.object(project.snapshots, "publish", side_effect=OSError("synthetic publish failure")):
            with self.assertRaises(OSError):
                Engine(config).run(project, "worker")
        self.assertEqual(project.snapshots.get()["id"], before)
        intent = ready(project.snapshots, "worker")
        self.assertIsNone(intent)
        from mizu.continuations import pending
        prepared = pending(project.snapshots, "worker")
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["state"], "prepared")

    def test_failed_finish_only_seal_does_not_prepare_or_publish_handoff(self):
        config, project = self.configured("seal-failed")
        before = project.snapshots.get()["id"]
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            with self.assertRaises(Denied):
                Engine(config).run(project, "worker")
        self.assertEqual(demand.call_count, 1)
        self.assertEqual(project.snapshots.get()["id"], before)
        self.assertIsNone(ready(project.snapshots, "worker"))

    def test_deadline_during_safe_point_seal_does_not_start_another_dispatch(self):
        from mizu import safe_point
        from mizu.errors import DeadlineWrapUp

        config, project = self.configured("seal-wait")
        engine = Engine(config)
        dispatch = engine._dispatch
        attempts = []

        def deadline_on_seal(context, *args, **kwargs):
            attempts.append(context.checkpoint_seal_only)
            if context.checkpoint_seal_only:
                context.bound_hit = "deadline"
                raise DeadlineWrapUp("synthetic safe-point deadline")
            return dispatch(context, *args, **kwargs)

        with patch("mizu.runtime.has_live_demand", return_value=True), \
             patch.object(engine, "_dispatch", side_effect=deadline_on_seal):
            result = engine.run(project, "worker")

        self.assertEqual(attempts, [False, True])
        self.assertEqual(result["status"], "bounded")
        self.assertEqual(result["bound"], "deadline")
        self.assertEqual(result["publication"], "not_published")
        self.assertIsNotNone(safe_point.read(project.snapshots))

    def test_deadline_bounded_finish_publishes_without_continuation_or_fence(self):
        from mizu import safe_point
        from mizu.runtime import Context

        config, project = self.configured("seal-wait")
        finish = Context._DISPATCH["finish"]

        def bounded_finish(context, args):
            if context.checkpoint_seal_only:
                context.bound_hit = "deadline"
            return finish(context, args)

        with patch("mizu.runtime.has_live_demand", return_value=True), \
             patch.dict(Context._DISPATCH, {"finish": bounded_finish}):
            result = Engine(config).run(project, "worker")

        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertEqual(project.snapshots.get()["outcome"], "bounded")
        self.assertIsNone(safe_point.read(project.snapshots))
        self.assertIsNone(ready(project.snapshots, "worker"))

    def test_wait_outcome_is_not_forced_into_continuation(self):
        config, project = self.configured("seal-wait")
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            result = Engine(config).run(project, "worker")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["finish"]["outcome"], "wait")
        self.assertEqual(demand.call_count, 1)
        self.assertEqual(project.snapshots.get()["outcome"], "wait")
        self.assertIsNone(ready(project.snapshots, "worker"))

    def test_quantum_requires_matching_completed_unit_boundary(self):
        config, project = self.configured("seal-wait", quantum=2)
        context = self.context("worker")
        context.config = config
        context.project = project
        context.completed_driver_units = 1
        context.driver_checkpoint = DriverCheckpoint("claude", "query", 0, 1)
        engine = Engine(config)
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand, \
             patch.object(engine, "_seal_driver_checkpoint") as seal:
            self.assertIsNone(engine._maybe_safe_point_yield(context))
            demand.assert_not_called()
            seal.assert_not_called()
            context.completed_driver_units = 2
            seal.return_value = {"outcome": "wait"}
            self.assertEqual(engine._maybe_safe_point_yield(context), {"outcome": "wait"})
        demand.assert_called_once()
        seal.assert_called_once_with(context, context.driver_checkpoint,
                                     profile=context.role.profile, safe_point_yield=True)

    def test_deadline_winning_checkpoint_claim_clears_unsealed_fence(self):
        import time
        from mizu import safe_point
        from mizu.errors import DeadlineWrapUp

        config, project = self.configured("seal-wait")
        context = self.context("worker")
        context.config = config
        context.project = project
        context.snapshot = project.snapshots.get()
        context.role = config.roles["worker"]
        context.completed_driver_units = 1
        context.driver_checkpoint = DriverCheckpoint(
            "claude", "query", context.execution_attempt, context.request_count)
        context.operations_stopped.set()
        context.wrap_up_at = time.monotonic() - 1
        context.deadline = time.monotonic() + 60

        with patch("mizu.runtime.has_live_demand", return_value=True):
            with self.assertRaises(DeadlineWrapUp):
                Engine(config)._maybe_safe_point_yield(context)

        self.assertFalse(context.safe_point_yield_requested)
        self.assertIsNone(safe_point.read(project.snapshots))

    def test_default_disabled_and_drain_skip_demand_probe(self):
        config, project = self.configured("seal-wait", quantum=0)
        context = self.context("worker")
        context.config = config
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            self.assertIsNone(Engine(config)._maybe_safe_point_yield(context))
            demand.assert_not_called()

        config, project = self.configured("seal-wait", quantum=1)
        context = self.context("worker")
        context.config = config
        context.driver_checkpoint = object()
        context.completed_driver_units = 1
        project.set_control(armed=True, paused=False, draining=True)
        context.project = project
        with patch("mizu.runtime.has_live_demand", return_value=True) as demand:
            self.assertIsNone(Engine(config)._maybe_safe_point_yield(context))
            demand.assert_not_called()

    def fail_dirty_seal(self, *, scenario='seal-failed', publication=False):
        from mizu import safe_point
        config, project = self.configured(scenario)
        engine = Engine(config)
        seal = engine._seal_driver_checkpoint

        def dirty(context, checkpoint, **kwargs):
            (project.workspace / 'app.py').write_bytes(b'UNSEALED = True\n')
            (project.workspace / 'untracked.txt').write_bytes(b'preserve me\n')
            return seal(context, checkpoint, **kwargs)

        with patch.object(engine, '_seal_driver_checkpoint', side_effect=dirty), \
             patch('mizu.runtime.has_live_demand', return_value=True):
            if publication:
                with patch.object(project.snapshots, 'publish', side_effect=OSError('publish failed')):
                    with self.assertRaisesRegex(OSError, 'publish failed'):
                        engine.run(project, 'worker')
            else:
                with self.assertRaises(Denied):
                    engine.run(project, 'worker')
        marker = safe_point.read(project.snapshots)
        self.assertIsNotNone(marker)
        return config, project, marker

    def assert_dirty(self, project):
        self.assertEqual((project.workspace / 'app.py').read_bytes(), b'UNSEALED = True\n')
        self.assertEqual((project.workspace / 'untracked.txt').read_bytes(), b'preserve me\n')

    def other_writer(self, config, project):
        roles = dict(config.roles)
        roles['other'] = dataclasses.replace(roles['worker'], name='other', on_change=False)
        config = dataclasses.replace(config, roles=roles)
        project.roles = (*project.roles, 'other')
        return Engine(config)

    def test_failed_seal_fences_other_writer_before_dispatch_and_preserves_files(self):
        config, project, marker = self.fail_dirty_seal()
        other = self.other_writer(config, project)
        before = set((project.root / 'runs').iterdir())
        with patch.object(other, 'resolve') as resolve:
            with self.assertRaisesRegex(Denied, 'Workspace fenced'):
                other.run(project, 'other')
            resolve.assert_not_called()
        self.assertEqual(set((project.root / 'runs').iterdir()), before)
        self.assert_dirty(project)
        from mizu.fs import read_json
        error = read_json(project.root / 'runs' / marker['source_run'] / 'error.json')
        self.assertIn('error', error)
        self.assertEqual(error['publication'], 'not_published')

    def test_same_role_recovery_is_read_finish_only_and_unblocks_other_writer(self):
        from mizu import safe_point
        from mizu.fs import read_json
        config, project, marker = self.fail_dirty_seal()
        error_path = project.root / 'runs' / marker['source_run'] / 'error.json'
        original_error = error_path.read_bytes()
        config, restarted = self.configured('recover-wait')
        # New Project and Engine simulate a restarted caller, not session replay.
        result = Engine(config).run(restarted, 'worker', event_trigger=True)
        self.assertEqual(result['finish']['outcome'], 'wait')
        self.assertIsNone(safe_point.read(restarted.snapshots))
        self.assertIsNone(ready(restarted.snapshots, 'worker'))
        self.assertNotEqual(result['run'], marker['run'])
        self.assertEqual(error_path.read_bytes(), original_error)
        self.assert_dirty(restarted)
        snapshot = restarted.snapshots.get()
        self.assertIn('untracked.txt', snapshot['files'])
        self.assertEqual(read_json(restarted.root / 'runs' / result['run'] / 'finish-intent.json')['outcome'], 'wait')
        other = self.other_writer(config, restarted)
        with patch.object(other, '_dispatch', side_effect=RuntimeError('admitted other writer')):
            with self.assertRaisesRegex(RuntimeError, 'admitted other writer'):
                other.run(restarted, 'other')

    def test_unpublished_continue_recovery_replaces_only_uncommitted_intent(self):
        from mizu import safe_point
        from mizu.continuations import pending
        config, project, marker = self.fail_dirty_seal(scenario='seal-continue', publication=True)
        old_intent = pending(project.snapshots, 'worker')
        self.assertEqual(old_intent['state'], 'prepared')
        config, project = self.configured('recover-continue')
        result = Engine(config).run(project, 'worker')
        intent = ready(project.snapshots, 'worker')
        self.assertEqual(intent['source_run'], result['run'])
        self.assertNotEqual(intent['successor'], old_intent['successor'])
        self.assertEqual(intent['state'], 'committed')
        self.assertIsNone(safe_point.read(project.snapshots))
        self.assert_dirty(project)
        # Existing continuation admission owns its exactly-once successor.
        config, project = self.configured('normal')
        result2 = Engine(config).run(project, 'worker')
        self.assertEqual(result2['run'], intent['successor'])
        self.assertIsNone(ready(project.snapshots, 'worker'))

    def test_crash_after_publication_retires_only_exact_commit_without_recovery_dispatch(self):
        from mizu import safe_point
        config, project = self.configured('seal-continue')
        real_guard = safe_point.guard
        def crash_after_publish(store, role):
            marker = safe_point.read(store)
            if marker and marker['snapshot'] == store.get()['id']:
                raise OSError('crash before clear')
            return real_guard(store, role)
        with patch('mizu.runtime.has_live_demand', return_value=True), \
             patch('mizu.runtime.safe_point.guard', side_effect=crash_after_publish):
            with self.assertRaisesRegex(OSError, 'crash before clear'):
                Engine(config).run(project, 'worker')
        marker = safe_point.read(project.snapshots)
        self.assertEqual(marker['snapshot'], project.snapshots.get()['id'])
        restarted = Project(config, 'sample')
        other = self.other_writer(config, restarted)
        with patch.object(other, '_dispatch', side_effect=RuntimeError('admitted after commitment')):
            with self.assertRaisesRegex(RuntimeError, 'admitted after commitment'):
                other.run(restarted, 'other')
        self.assertIsNone(safe_point.read(project.snapshots))

    def test_failed_recovery_has_no_work_followup_and_retains_fence(self):
        from mizu import safe_point
        self.fail_dirty_seal()
        config, project = self.configured('recover-missing')
        engine = Engine(config)
        with patch.object(engine, '_dispatch', wraps=engine._dispatch) as dispatch:
            with self.assertRaisesRegex(Exception, 'recovery ended without finish'):
                engine.run(project, 'worker')
        self.assertEqual(dispatch.call_count, 1)
        self.assertIsNotNone(safe_point.read(project.snapshots))
        self.assert_dirty(project)

    def test_malformed_fence_or_missing_commitment_never_authorizes_takeover(self):
        from mizu import safe_point
        from mizu.fs import read_json, write_json
        config, project, marker = self.fail_dirty_seal(scenario='seal-continue', publication=True)
        write_json(project.root / safe_point.NAME, {**marker, 'run': 'bad'})
        with self.assertRaisesRegex(Denied, 'Malformed'):
            Engine(config).run(project, 'worker')
        write_json(project.root / safe_point.NAME, marker)
        snapshot = project.snapshots.get(marker['snapshot'])
        project.snapshots.publish(snapshot)
        # Valid publication without the required atomic continuation commitment
        # is ambiguous, even if a finish and snapshot are present.
        pointer = read_json(project.root / 'current.json')
        pointer['continuations']['entries'] = []
        write_json(project.root / 'current.json', pointer)
        other = self.other_writer(config, project)
        with self.assertRaisesRegex(Denied, 'commitment is unconfirmed'):
            other.run(project, 'other')
        self.assertIsNotNone(safe_point.read(project.snapshots))
        self.assert_dirty(project)

    def test_malformed_fence_still_reaches_fifo_admission_before_denial(self):
        import contextlib
        from mizu import safe_point
        from mizu.fs import write_json

        config, project, marker = self.fail_dirty_seal()
        write_json(project.root / safe_point.NAME, {**marker, "run": "bad"})
        other = self.other_writer(config, project)
        admitted = []

        @contextlib.contextmanager
        def fake_admission(*args, **kwargs):
            admitted.append("enter")
            try:
                yield 0
            finally:
                admitted.append("exit")

        with patch("mizu.runtime.admission", fake_admission):
            with self.assertRaisesRegex(Denied, "Malformed safe-point"):
                other.run(project, "other", event_trigger=True)
        self.assertEqual(admitted, ["enter", "exit"])

    def test_preparation_failure_recovers_even_when_quantum_is_later_disabled(self):
        from mizu import safe_point
        config, project = self.configured('seal-continue')
        with patch('mizu.runtime.has_live_demand', return_value=True), \
             patch('mizu.continuations.prepare', side_effect=OSError('prepare failed')):
            with self.assertRaisesRegex(OSError, 'prepare failed'):
                Engine(config).run(project, 'worker')
        self.assertIsNotNone(safe_point.read(project.snapshots)['snapshot'])
        config, project = self.configured('recover-wait', quantum=0)
        self.assertEqual(Engine(config).run(project, 'worker')['finish']['outcome'], 'wait')
        self.assertIsNone(safe_point.read(project.snapshots))

    def test_fence_rejection_occurs_after_normal_admission_not_instead_of_it(self):
        import contextlib
        from mizu import safe_point
        config, project, marker = self.fail_dirty_seal()
        other = self.other_writer(config, project)
        admitted = []
        @contextlib.contextmanager
        def fake_admission(*args, **kwargs):
            admitted.append('enter')
            try:
                yield 0
            finally:
                admitted.append('exit')
        with patch('mizu.runtime.admission', fake_admission):
            with self.assertRaisesRegex(Denied, 'Workspace fenced'):
                other.run(project, 'other')
        self.assertEqual(admitted, ['enter', 'exit'])
        self.assertEqual(safe_point.read(project.snapshots), marker)

    def test_fence_pins_evidence_and_suppresses_workspace_cache_deletion(self):
        from mizu import storage_runs, workspace_cache
        from mizu.fs import write_json
        config, project, marker = self.fail_dirty_seal()
        plan = storage_runs.plan(project)
        self.assertTrue(plan['complete'], plan.get('reason'))
        self.assertGreaterEqual(plan['pinned_runs'], 1)
        cache_config = dataclasses.replace(config, workspace_cache=dataclasses.replace(
            config.workspace_cache, roots=('cache',), max_bytes=1))
        cached = Project(cache_config, 'sample')
        with patch.object(workspace_cache, '_preflight') as preflight:
            self.assertEqual(workspace_cache.maintain(cached)['status'], 'fenced')
            preflight.assert_not_called()
        # A corrupt marker blocks maintenance instead of discarding references.
        from mizu import safe_point
        write_json(project.root / safe_point.NAME, None)
        self.assertFalse(storage_runs.plan(project)['complete'])
        self.assertEqual(workspace_cache.maintain(cached)['status'], 'unavailable')
        self.assert_dirty(project)

    def test_ordinary_nonpreempted_failure_does_not_create_a_fence(self):
        from mizu import safe_point
        config, project = self.configured('normal', quantum=0)
        engine = Engine(config)
        with patch.object(engine, '_dispatch', side_effect=OSError('ordinary failure')):
            with self.assertRaisesRegex(OSError, 'ordinary failure'):
                engine.run(project, 'worker')
        self.assertIsNone(safe_point.read(project.snapshots))

    def test_interrupted_continuation_settles_before_finish_only_recovery(self):
        from mizu import safe_point
        config, project = self.configured('seal-continue')
        with patch('mizu.runtime.has_live_demand', return_value=True):
            Engine(config).run(project, 'worker')
        config, project = self.configured('seal-failed')
        with patch('mizu.runtime.has_live_demand', return_value=True):
            with self.assertRaises(Denied):
                Engine(config).run(project, 'worker')
        self.assertIsNotNone(safe_point.read(project.snapshots))
        config, project = self.configured('recover-wait')
        engine = Engine(config)
        # Existing admission reconciles the interrupted successor without
        # dispatching it again; a subsequent ordinary FIFO wake recovers.
        interrupted = engine.run(project, 'worker')
        self.assertEqual(interrupted['status'], 'interrupted')
        recovered = engine.run(project, 'worker')
        self.assertEqual(recovered['finish']['outcome'], 'wait')
        self.assertIsNone(safe_point.read(project.snapshots))
