"""Run-local shadow evidence with real host locks and a fake model driver."""
import contextlib
import dataclasses
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.admission import _Queue, role_exclusion
from mizu.errors import Busy
from mizu.fs import canonical, lock, read_json
from mizu.runtime import Engine


class AdmissionEvidenceTests(Fixture):
    @contextlib.contextmanager
    def blocked_head(self):
        resource = self.project.root / 'locks/workspace.lock'
        with contextlib.closing(_Queue(self.config, (resource,))) as head:
            with lock(resource, blocking=False):
                with self.assertRaises(Busy):
                    head.attempt(enqueue=True)
                yield head

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
                'profile', 'engine', 'profile_engine_reason', 'affinity', 'affinity_reason'})
            raw = canonical(row)
            for forbidden in (str(self.root).encode(), b'sample', b'workspace.lock',
                              b'prompt', b'payload', b'test-provider', b'test-model', b'session'):
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
