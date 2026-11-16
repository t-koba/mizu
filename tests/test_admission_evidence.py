"""Run-local shadow evidence with real host locks and a fake model driver."""
import contextlib
import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.admission import _Queue, role_exclusion
from mizu.errors import Busy, Denied
from mizu.dispatch_evidence import AdmissionSource
from mizu.evidence_records import read_retained
from mizu.fs import canonical, lock, read_json, write_json
from mizu.runtime import (Engine, MAX_STARTED_EVIDENCE_BYTES,
                         _bound_started_evidence)


class AdmissionEvidenceTests(Fixture):
    @contextlib.contextmanager
    def blocked_head(self):
        resource = self.project.root / 'locks/workspace.lock'
        with contextlib.closing(_Queue(self.config, (resource,))) as head:
            with lock(resource, blocking=False):
                with self.assertRaises(Busy):
                    head.attempt(enqueue=True)
                yield head

    def queue(self, role_name, *, observe=False, role=None, metadata=True):
        role = role or self.config.roles[role_name]
        resources = [role_exclusion(self.project.root, role_name)]
        if role.workspace == 'write':
            resources.append(self.project.root / 'locks/workspace.lock')
        source = AdmissionSource(self.config, self.project, role) if metadata else None
        queue = _Queue(self.config, resources, observe=observe, evidence_source=source)
        self.addCleanup(queue.close)
        return queue

    def test_evidence_source_factory_is_lazy_until_contention_or_enqueue(self):
        role = self.config.roles['reporter']
        resources = [role_exclusion(self.project.root, 'reporter')]
        calls = []
        queue = _Queue(self.config, resources, observe=True,
                       evidence_source_factory=lambda: (calls.append(True) or
                           AdmissionSource(self.config, self.project, role)))
        self.addCleanup(queue.close)
        with queue.attempt(enqueue=False):
            pass
        self.assertEqual(calls, [])
        with lock(resources[0], blocking=False):
            with self.assertRaises(Busy):
                queue.attempt(enqueue=True)
        self.assertEqual(calls, [True])
        tickets = read_json(self.config.data / 'slots/queue.json')['tickets']
        self.assertEqual(tickets[0]['evidence'], queue.evidence_source.metadata)

    def test_disabled_choice_capture_keeps_only_bounded_contention_count(self):
        current = self.queue('reporter', observe=True)
        current.capture_choices = False
        with self.blocked_head() as head:
            with patch.object(current, '_choices', side_effect=AssertionError('shadow probes disabled')):
                with current.attempt(enqueue=False):
                    choice = current.observation['choice_set']
                    self.assertEqual(current.observation['bypassed_busy_ticket_ids'], [head.identity])
        self.assertEqual(choice, {'candidates': [], 'total': 2, 'omitted': 2, 'complete': False})

    def test_complete_choice_set_probes_later_candidates_without_changing_fifo_or_locks(self):
        self.config = dataclasses.replace(self.config,
            limits=dataclasses.replace(self.config.limits, parallel_runs=1))
        head, current, later = (self.queue(name, observe=True)
                                for name in ('worker', 'reporter', 'maintainer'))
        # Enqueue all three while their own exclusions are unavailable, so
        # even an older ready ticket cannot prevent the fixture from queuing.
        with contextlib.ExitStack() as held:
            for queue in (head, current, later):
                held.enter_context(lock(next(iter(queue.paths.values())), blocking=False))
                with self.assertRaises(Busy):
                    queue.attempt(enqueue=True)
        with lock(role_exclusion(self.project.root, 'worker'), blocking=False):
            selected = current.attempt(enqueue=False)
            with selected:
                view = current.observation['choice_set']
                self.assertTrue(view['complete'])
                self.assertEqual(view['omitted'], 0)
                self.assertEqual(view['total'], 3)
                self.assertEqual([r['admission_id'] for r in view['candidates']],
                                 [head.identity, current.identity, later.identity])
                self.assertEqual([r['readiness'] for r in view['candidates']],
                                 ['busy', 'ready', 'ready'])
                self.assertEqual(current.observation['bypassed_busy_ticket_ids'], [head.identity])
                # Only the original selected exclusions/slot survive. All
                # temporary alternative and workspace probes have been released.
                with self.assertRaises(Busy), lock(role_exclusion(self.project.root, 'reporter'), blocking=False):
                    pass
                with self.assertRaises(Busy), lock(self.config.data / 'slots/0.lock', blocking=False):
                    pass
                with lock(role_exclusion(self.project.root, 'maintainer'), blocking=False):
                    pass
                with lock(self.project.root / 'locks/workspace.lock', blocking=False):
                    pass
                with self.assertRaises(Busy):
                    later.attempt(enqueue=False)
        # Once the original head is ready FIFO still refuses later admission,
        # even though the earlier shadow pass found the later candidate ready.
        with self.assertRaisesRegex(Busy, 'Older execution admission is ready'):
            later.attempt(enqueue=False)
        with lock(role_exclusion(self.project.root, 'reporter'), blocking=False):
            pass
        with lock(self.config.data / 'slots/0.lock', blocking=False):
            pass

    def test_persistent_generation_is_read_at_admission_and_matches_dispatch_identity(self):
        queue = self.queue('reporter', observe=True)
        ctx = self.context('reporter')
        from mizu.engine_config import effective, session_record
        path, _ = session_record(ctx, ctx.role.profile, effective(self.config, ctx.role, ctx.role.profile))
        write_json(path.parent / 'generation.json', {'generation': 7})
        with self.blocked_head(), queue.attempt(enqueue=False):
            current = queue.observation['choice_set']['candidates'][-1]
        self.assertEqual(current['readiness'], 'ready')
        self.assertEqual(current['session'], {'key': path.parent.name, 'generation': 7, 'unknown_reason': None})
        self.assertFalse((path.parent / 'dispatch-order.json').exists())

    def test_unknown_candidates_are_not_eligible_and_completeness_is_explicit(self):
        selector = dataclasses.replace(self.config.roles['searcher'], selector='not-executed')
        selected = self.queue('reporter', observe=True)
        legacy = self.queue('worker', metadata=False)
        unresolved = self.queue('searcher', role=selector)
        nonpersistent = self.queue('consult')
        # Overridden selector identity cannot be reproduced by this observer;
        # configured nonpersistent sessions are explicitly unknown as well.
        with contextlib.ExitStack() as held:
            for queue in (legacy, selected, unresolved, nonpersistent):
                held.enter_context(lock(next(iter(queue.paths.values())), blocking=False))
                with self.assertRaises(Busy):
                    queue.attempt(enqueue=True)
        with lock(role_exclusion(self.project.root, 'worker'), blocking=False), selected.attempt(enqueue=False):
            view = selected.observation['choice_set']
        self.assertFalse(view['complete'])
        self.assertEqual(view['omitted'], 0)
        self.assertEqual([r['readiness'] for r in view['candidates']], ['unknown', 'ready', 'unknown', 'unknown'])
        self.assertEqual(view['candidates'][0]['unknown_reason'], 'legacy_metadata_missing')
        # Test same-source selector/nonpersistent reasons independently of a
        # deliberately mismatched observer configuration above.
        for queue, reason in ((unresolved, 'selector_not_resolved_at_admission'),
                              (nonpersistent, 'nonpersistent_session')):
            row = queue._candidate({'id': queue.identity, 'resources': list(queue.resources),
                                    'evidence': queue.evidence_source.metadata})
            self.assertEqual(row['readiness'], 'unknown')
            self.assertEqual(row['unknown_reason'], reason)

    def test_current_legacy_ticket_is_not_silently_upgraded(self):
        queue = self.queue('reporter', observe=True, metadata=False)
        with lock(role_exclusion(self.project.root, 'reporter'), blocking=False):
            with self.assertRaises(Busy):
                queue.attempt(enqueue=True)
        queue.evidence_source = AdmissionSource(self.config, self.project, self.config.roles['reporter'])
        with queue.attempt(enqueue=False):
            row, = queue.observation['choice_set']['candidates']
            self.assertEqual(row['readiness'], 'unknown')
            self.assertEqual(row['unknown_reason'], 'legacy_metadata_missing')
            self.assertFalse(queue.observation['choice_set']['complete'])

    def test_slot_busy_is_probed_and_ephemeral_current_remains_unknown(self):
        queue = self.queue('reporter')
        ticket = {'id': queue.identity, 'resources': list(queue.resources),
                  'evidence': queue.evidence_source.metadata}
        with contextlib.ExitStack() as held:
            for index in range(self.config.limits.parallel_runs):
                held.enter_context(lock(self.config.data / f'slots/{index}.lock', blocking=False))
            row = queue._candidate(ticket)
        self.assertEqual(row['readiness'], 'busy')
        self.assertIsNotNone(row['session']['key'])
        self.assertEqual(row['session']['generation'], 0)
        self.assertEqual(queue._candidate(ticket)['readiness'], 'ready')
        queue.evidence_source = AdmissionSource(self.config, self.project,
                                               self.config.roles['reporter'], ephemeral=True)
        ticket['evidence'] = queue.evidence_source.metadata
        self.assertEqual(queue._candidate(ticket)['unknown_reason'], 'nonpersistent_session')

    def test_external_and_unsafe_or_stale_sessions_stay_unknown(self):
        selected = self.queue('reporter', observe=True)
        external_root = self.root / 'isolated' / 'foreign'
        external = _Queue(self.config, [role_exclusion(external_root, 'worker')])
        self.addCleanup(external.close)
        with lock(role_exclusion(external_root, 'worker'), blocking=False):
            with self.assertRaises(Busy):
                external.attempt(enqueue=True)
        with selected.attempt(enqueue=False):
            view = selected.observation['choice_set']
            self.assertEqual(view['candidates'][0]['unknown_reason'], 'external_project')
            self.assertEqual(selected.observation['bypassed_busy_ticket_ids'], [])
        foreign = selected._candidate({'id': selected.identity, 'resources': ['other/locks/run-reporter.lock'],
                                       'evidence': selected.evidence_source.metadata})
        self.assertEqual(foreign['readiness'], 'unknown')
        self.assertEqual(foreign['unknown_reason'], 'project_or_role_identity_unavailable')
        ticket = {'id': selected.identity, 'resources': list(selected.resources),
                  'evidence': {'binding': '0' * 64}}
        self.assertEqual(selected._candidate(ticket)['readiness'], 'unknown')
        ticket['evidence'] = selected.evidence_source.metadata
        key = selected.evidence_source._prepare(self.config.roles['reporter'])['key']
        directory = self.project.root / 'sessions/reporter' / key
        directory.mkdir(parents=True)
        (directory / 'generation.json').write_text('not a generation')
        row = selected._candidate(ticket)
        self.assertEqual(row['readiness'], 'unknown')
        self.assertIsNone(row['session']['key'])
        with lock(role_exclusion(self.project.root, 'reporter'), blocking=False):
            pass

    def test_goal_bound_native_and_family_identity_are_unknown_without_selection(self):
        role = self.config.roles['reporter']
        with patch('mizu.engine_config.effective', return_value={'engine': 'codex', 'session': 'persistent'}), \
                patch('mizu.engine_config.conversation_identity', side_effect=AssertionError('must not guess goal')):
            source = AdmissionSource(self.config, self.project, role)
            queue = self.queue('reporter', metadata=False)
            queue.evidence_source = source
            row = queue._candidate({'id': queue.identity, 'resources': list(queue.resources),
                                    'evidence': source.metadata})
            self.assertEqual(row['readiness'], 'unknown')
            self.assertEqual(row['unknown_reason'], 'session_identity_unavailable_at_admission')
        queue = self.queue('reporter')
        key = queue.evidence_source._prepare(role)['key']
        directory = self.project.root / 'sessions/reporter' / key
        directory.mkdir(parents=True)
        write_json(directory / 'family-owner.json', {})
        row = queue._candidate({'id': queue.identity, 'resources': list(queue.resources),
                                'evidence': queue.evidence_source.metadata})
        self.assertEqual(row['readiness'], 'unknown')
        self.assertIsNone(row['session']['key'])
        self.assertFalse((directory / 'dispatch-order.json').exists())
        self.assertFalse((directory / 'session.json').exists())

    def test_choice_candidate_omissions_are_counted_at_byte_cap(self):
        selected = self.queue('reporter', observe=True)
        with self.blocked_head(), selected.attempt(enqueue=False):
            row = selected.observation
        queue = {'version': 1, 'observations': [row], 'omitted': 0}
        # Enough room for the base observation and empty choice summary, but
        # not for the detailed candidate rows.
        empty = {**row, 'choice_set': {**row['choice_set'], 'candidates': [], 'omitted': 2, 'complete': False}}
        cap = len(canonical({'queue_observations': {'version': 1, 'observations': [empty], 'omitted': 0}})) + 8
        started = {}
        _bound_started_evidence(started, queue, cap)
        self.assertLessEqual(len(canonical(started)), cap)
        view = started['queue_observations']['observations'][0]['choice_set']
        self.assertFalse(view['complete'])
        self.assertEqual(view['total'], len(view['candidates']) + view['omitted'])
        self.assertEqual(view['omitted'], 2)

    def test_started_joins_dispatch_by_run_without_paths_or_payloads(self):
        driver = ScriptDriver()
        with self.blocked_head() as head:
            engine = Engine(self.config, driver=driver)
            with lock(role_exclusion(self.project.root, 'reporter'), blocking=False):
                with self.assertRaises(Busy):
                    engine.run(self.project, 'reporter', enqueue_busy=True)
            tickets = read_json(self.config.data / 'slots/queue.json')['tickets']
            self.assertEqual(len(tickets), 2)
            ticket_id = tickets[1]['id']
            self.assertEqual(driver.calls, [])
            self.assertFalse((self.project.root / 'runs').exists())
            result = engine.run(self.project, 'reporter', enqueue_busy=True)
            run_dir = self.project.root / 'runs' / result['run']
            started = read_json(run_dir / 'started.json')
            dispatch = read_json(run_dir / 'dispatch-attempts.json')
            row, = started['queue_observations']['observations']
            self.assertEqual(started['run'], result['run'])
            self.assertEqual(dispatch['attempts'][0]['run'], started['run'])
            self.assertEqual(started['queue_observations']['omitted'], 0)
            self.assertEqual(row['live_ticket_ids'], [head.identity, ticket_id])
            self.assertEqual(row['admission_id'], ticket_id)
            self.assertEqual(row['bypassed_busy_ticket_ids'], [head.identity])
            self.assertTrue(row['queued'])
            self.assertEqual(row['fifo_position'], 2)
            self.assertEqual(row['profile'], self.config.roles['reporter'].profile)
            self.assertEqual(row['engine'], self.config.engine(row['profile']))
            self.assertIsNone(row['profile_engine_reason'])
            self.assertIsNone(row['affinity'])
            self.assertEqual(row['affinity_reason'], 'not_available_before_dispatch')
            self.assertEqual(round(row['elapsed_seconds'], 6), started['admission_elapsed_seconds'])
            self.assertEqual(set(row), {
                'admission_id', 'queued', 'fifo_position', 'live_ticket_ids',
                'bypassed_busy_ticket_ids', 'later_tickets_unexamined', 'elapsed_seconds',
                'profile', 'engine', 'profile_engine_reason', 'affinity', 'affinity_reason', 'choice_set'})
            raw = canonical(row)
            for forbidden in (str(self.root).encode(), b'sample', b'workspace.lock',
                              b'prompt', b'payload', b'test-provider', b'test-model'):
                self.assertNotIn(forbidden, raw)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(len(dispatch['attempts']), 1)

    def test_selector_hints_unknown_before_admission_without_selecting(self):
        role = dataclasses.replace(self.config.roles['reporter'], selector='not-executed')
        rows = []
        engine = Engine(self.config, driver=ScriptDriver())
        with self.blocked_head(), contextlib.ExitStack() as stack:
            engine._admit(self.project, 'reporter', role, stack, wait=False,
                          observation_sink=rows.append)
        row, = rows
        self.assertIsNone(row['profile'])
        self.assertIsNone(row['engine'])
        self.assertEqual(row['profile_engine_reason'], 'selector_not_resolved_at_admission')
        self.assertIsNone(row['affinity'])
        self.assertEqual(row['affinity_reason'], 'not_available_before_dispatch')
        self.assertEqual(engine.driver.calls, [])

    def test_readmissions_are_bounded_and_evidence_write_failure_is_nonfatal(self):
        # Drive the actual retry release/re-admission mechanism, without a
        # provider error or selector sleep. This is not a model retry test.
        for fail_write in (False, True):
            with self.subTest(fail_write=fail_write):
                engine = Engine(self.config)
                held_stack = []
                original = engine._admit
                def admit(*args, **kwargs):
                    held_stack[:] = [args[3]]
                    return original(*args, **kwargs)
                def callback(ctx, *_):
                    role = dataclasses.replace(ctx.role, selector='not-executed')
                    from mizu.runtime import write_json
                    def write(path, value):
                        if fail_write and path.name == 'started.json':
                            raise OSError('synthetic evidence write failure')
                        return write_json(path, value)
                    with patch('mizu.runtime.write_json', side_effect=write):
                        for _ in range(10):
                            engine._selection_retry_wait(ctx, self.project, held_stack[0],
                                                         role, 'reporter', 0.0)
                            self.assertTrue(ctx._slot_lease)
                driver = ScriptDriver(callback)
                engine.driver = driver
                with self.blocked_head(), patch.object(engine, '_admit', side_effect=admit):
                    result = engine.run(self.project, 'reporter')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(len(driver.calls), 1)
                started = read_json(self.project.root / 'runs' / result['run'] / 'started.json')
                view = started['queue_observations']
                if fail_write:
                    self.assertEqual(len(view['observations']), 1)
                    self.assertEqual(view['omitted'], 0)
                else:
                    self.assertEqual(len(view['observations']), 8)
                    self.assertEqual(view['omitted'], 3)
                    self.assertEqual(len({r['admission_id'] for r in view['observations']}), 8)
                    for row in view['observations'][1:]:
                        self.assertIsNone(row['profile'])
                        self.assertEqual(row['profile_engine_reason'], 'selector_not_resolved_at_admission')

    def test_configured_queue_observation_sample_bounds_started_evidence(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, queue_observations=2))
        engine = Engine(config)
        held_stack = []
        original = engine._admit
        def admit(*args, **kwargs):
            held_stack[:] = [args[3]]
            return original(*args, **kwargs)
        def callback(ctx, *_):
            role = dataclasses.replace(ctx.role, selector='not-executed')
            for _ in range(10):
                engine._selection_retry_wait(ctx, self.project, held_stack[0],
                                             role, 'reporter', 0.0)
                self.assertTrue(ctx._slot_lease)
        engine.driver = ScriptDriver(callback)
        with self.blocked_head(), patch.object(engine, '_admit', side_effect=admit):
            result = engine.run(self.project, 'reporter')
        self.assertEqual(result['status'], 'completed')
        started = read_json(self.project.root / 'runs' / result['run'] / 'started.json')
        view = started['queue_observations']
        self.assertEqual(len(view['observations']), 2)
        self.assertEqual(view['omitted'], 9)

    def test_queue_sample_is_trimmed_to_started_evidence_read_cap(self):
        rows = [{"admission_id": f"{i:032x}", "queued": True, "fifo_position": i + 1,
                 "live_ticket_ids": [f"{i:032x}" for _ in range(16)],
                 "bypassed_busy_ticket_ids": [f"{i:032x}" for _ in range(15)],
                 "later_tickets_unexamined": 1, "affinity": None,
                 "affinity_reason": "not_available_before_dispatch"}
                for i in range(100)]
        queue = {"version": 1, "observations": rows, "omitted": 0}
        started = {"run": "a" * 32, "role": "reporter", "snapshot": "b" * 64,
                   "started_at": "2026-01-01T00:00:00+00:00"}
        _bound_started_evidence(started, queue)
        self.assertLessEqual(len(canonical(started)), MAX_STARTED_EVIDENCE_BYTES)
        self.assertGreater(queue["omitted"], 0)
        self.assertLess(len(queue["observations"]), 100)
        kept = [row["admission_id"] for row in queue["observations"]]
        self.assertEqual(kept, [row["admission_id"] for row in rows[:len(kept)]])

    def test_configured_queue_sample_never_exceeds_started_evidence_read(self):
        config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, queue_observations=100))
        engine = Engine(config)
        held_stack = []
        original = engine._admit
        def admit(*args, **kwargs):
            held_stack[:] = [args[3]]
            return original(*args, **kwargs)
        def callback(ctx, *_):
            role = dataclasses.replace(ctx.role, selector='not-executed')
            for _ in range(100):
                engine._selection_retry_wait(ctx, self.project, held_stack[0],
                                             role, 'reporter', 0.0)
                self.assertTrue(ctx._slot_lease)
        engine.driver = ScriptDriver(callback)
        with self.blocked_head(), patch.object(engine, '_admit', side_effect=admit):
            result = engine.run(self.project, 'reporter')
        self.assertEqual(result['status'], 'completed')
        path = self.project.root / 'runs' / result['run'] / 'started.json'
        self.assertLessEqual(path.stat().st_size, MAX_STARTED_EVIDENCE_BYTES)
        started = read_json(path, max_bytes=MAX_STARTED_EVIDENCE_BYTES)
        view = started['queue_observations']
        self.assertGreater(view['omitted'], 0)
        self.assertLessEqual(len(view['observations']), 100)
        evidence_id = 'c' * 32
        write_json(self.project.root / 'runs' / result['run'] / 'commands' /
                   f'{evidence_id}.json',
                   {'id': evidence_id, 'kind': 'command', 'role': 'reporter'})
        context = SimpleNamespace(project=self.project, config=config,
                                  role=SimpleNamespace(name='reporter'))
        args = {'run': result['run'], 'evidence_id': evidence_id}
        self.assertEqual(read_retained(context, 'commands', args)['run'], result['run'])

        # A role-like context predating the optional source policy keeps the
        # old own-role-only default instead of raising or widening authority.
        write_json(self.project.root / 'runs' / result['run'] / 'started.json',
                   {'run': result['run'], 'role': 'worker'})
        write_json(self.project.root / 'runs' / result['run'] / 'commands' /
                   f'{evidence_id}.json',
                   {'id': evidence_id, 'kind': 'command', 'role': 'worker'})
        with self.assertRaisesRegex(Denied, 'role'):
            read_retained(context, 'commands', args)

    def test_first_contended_readmission_attaches_to_uncontended_start(self):
        engine = Engine(self.config)
        stacks = []
        original = engine._admit
        def admit(*args, **kwargs):
            stacks[:] = [args[3]]
            return original(*args, **kwargs)
        def callback(ctx, *_):
            self.assertNotIn('queue_observations', read_json(ctx.run_dir / 'started.json'))
            with self.blocked_head():
                engine._selection_retry_wait(ctx, self.project, stacks[0], ctx.role, 'reporter', 0.0)
        engine.driver = ScriptDriver(callback)
        with patch.object(engine, '_admit', side_effect=admit):
            result = engine.run(self.project, 'reporter')
        started = read_json(self.project.root / 'runs' / result['run'] / 'started.json')
        row, = started['queue_observations']['observations']
        self.assertFalse(row['queued'])
        self.assertEqual(started['queue_observations']['omitted'], 0)

    def test_uncontended_run_has_no_queue_observations(self):
        result = Engine(self.config, driver=ScriptDriver()).run(self.project, 'reporter')
        started = read_json(self.project.root / 'runs' / result['run'] / 'started.json')
        self.assertNotIn('queue_observations', started)
