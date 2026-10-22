"""Offline admission tests: real locks and independent spawned processes.

Spawn, rather than fork, avoids inheriting lock descriptors and also exercises
Windows-compatible process setup. These are host admission tests, not provider
or sandbox integration tests.
"""
import contextlib
import dataclasses
import multiprocessing
import os
import queue
import stat
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from support import Fixture, ScriptDriver
from mizu.admission import _Full, _Queue, admission, role_exclusion, waiting
from mizu.budget import Budget
from mizu.config import load
from mizu.errors import Busy, Cancelled, Denied, InfraExceeded
from mizu.fs import canonical, lock, read_json, write_json
from mizu.project import Project
from mizu.runtime import Engine, slot


#: The wait a test gives a spawned child. The child is a fresh interpreter
#: that re-imports the product's modules, so its start-up is a host cost:
#: the bound is generous enough for a loaded host and still detects a
#: child that never reports.
CHILD_WAIT_SECONDS = 60
#: The backstop a child waits for the test's release. The test releases it
#: only after its own waits, so this covers several of them; it fires only
#: when the test has already failed.
CHILD_RELEASE_SECONDS = 600


def _process(file, capacity, number, release, stop, results, active, peak):
    """Exercise Engine's actual dispatch locks without launching a model."""
    try:
        config = load(file)
        config = dataclasses.replace(config, limits=dataclasses.replace(
            config.limits, parallel_runs=capacity, idle_seconds=0.02, busy_retry_seconds=15))
        project = Project(config, 'sample')
        engine = Engine(config, stop=stop)
        with contextlib.ExitStack() as stack:
            engine._admit(project, f'caller-{number}', config.roles['reporter'], stack, wait=True)
            with active.get_lock():
                active.value += 1
                peak.value = max(peak.value, active.value)
            results.put(('entered', number))
            try:
                if not release.wait(CHILD_RELEASE_SECONDS):
                    raise RuntimeError('test release timed out')
            finally:
                with active.get_lock():
                    active.value -= 1
        results.put(('exited', number))
    except BaseException as exc:
        results.put((type(exc).__name__, number))


def _probe_queue_state(file, results):
    """Use a killable process: a FIFO-open regression must not hang the suite."""
    try:
        with admission(load(file)):
            pass
    except Busy as exc:
        results.put(('Busy', str(exc)))
    else:
        results.put(('admitted', ''))


class QueueTests(Fixture):
    def setUp(self):
        super().setUp()
        self.tune()
        self.mp = multiprocessing.get_context('spawn')
        self.results = self.mp.Queue()
        self.addCleanup(self.results.close)
        self.active = self.mp.Value('i', 0)
        self.peak = self.mp.Value('i', 0)
        self.messages = []

    def tune(self, **changes):
        limits = dict(parallel_runs=2, idle_seconds=0.02, busy_retry_seconds=15, admission_wait_seconds=0)
        limits.update(changes)
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(self.config.limits, **limits))

    def child_deadline(self):
        """The deadline a test gives a spawned child to report.

        A spawned child is a fresh interpreter that re-imports the product's
        modules, so its start-up is a host cost rather than a bound the
        product sets. Every wait for a child uses this one deadline, so a
        loaded host cannot turn a healthy child into a failed test.
        """
        return time.monotonic() + CHILD_WAIT_SECONDS

    def tickets(self, *, synchronized=False):
        path = self.config.data / 'slots/queue.json'
        if not synchronized:
            return read_json(path, {'tickets': []})['tickets']
        lock_path = self.config.data / 'slots/queue.lock'
        deadline = self.child_deadline()
        while True:
            try:
                with lock(lock_path, blocking=False):
                    return read_json(path, {'tickets': []})['tickets']
            except Busy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    def until(self, predicate):
        deadline = self.child_deadline()
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('admission test synchronization timed out')
            time.sleep(0.01)

    def message(self, expected):
        deadline = self.child_deadline()
        while expected not in self.messages:
            self.messages.append(self.results.get(timeout=max(0.01, deadline - time.monotonic())))
        self.messages.remove(expected)

    def process(self, number):
        release, stop = self.mp.Event(), self.mp.Event()
        process = self.mp.Process(target=_process, args=(
            self.file, self.config.limits.parallel_runs, number, release, stop,
            self.results, self.active, self.peak))
        def cleanup():
            # terminate() can strand multiprocessing.Event internals. Never
            # touch a dead child's events during test cleanup.
            if not process.is_alive():
                process.join(5)
                return
            release.set()
            stop.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        process.start()
        self.addCleanup(cleanup)
        return process, release, stop

    def probe_queue_state(self):
        # The probe child is a fresh interpreter, so its start-up is a host
        # cost: the bound below is generous enough for a loaded host and
        # still detects a queue state that blocks the open or the read.
        process = self.mp.Process(target=_probe_queue_state, args=(self.file, self.results))
        process.start()
        try:
            process.join(60)
            self.assertFalse(process.is_alive(), 'queue state open/read blocked')
            self.assertEqual(process.exitcode, 0)
            return self.results.get(timeout=30)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()

    def waiter(self, *, resources=(), stop=None, check=None, config=None, capture_elapsed=False,
               wait=True, enqueue=False):
        stop = stop if stop is not None else threading.Event()
        result = []
        def work():
            try:
                with admission(config or self.config, resources, wait=wait, enqueue=enqueue,
                               stop=stop, check=check) as elapsed:
                    result.append(('admitted', elapsed) if capture_elapsed else 'admitted')
            except BaseException as exc:
                result.append(exc)
        thread = threading.Thread(target=work)
        thread.start()
        def cleanup():
            stop.set()
            thread.join(5)
        self.addCleanup(cleanup)
        return thread, stop, result

    def queued_waiter(self, **kwargs):
        """Start a blocked waiter and signal only after its ticket is written."""
        written = threading.Event()
        original_enqueue = _Queue._enqueue
        def enqueue_and_signal(queue_obj, tickets):
            original_enqueue(queue_obj, tickets)
            written.set()
        with patch.object(_Queue, '_enqueue', enqueue_and_signal):
            thread, stop, result = self.waiter(**kwargs)
            self.assertTrue(written.wait(5), 'waiting caller did not serialize its ticket')
        return thread, stop, result

    def raw_slots(self):
        stack = contextlib.ExitStack()
        try:
            for index in range(self.config.limits.parallel_runs):
                stack.enter_context(lock(self.config.data / 'slots' / f'{index}.lock', blocking=False))
            return stack
        except BaseException:
            stack.close()
            raise

    def test_status_names_the_roles_waiting_for_a_slot(self):
        # A worker that is waiting for a slot must be distinguishable from
        # a worker that is running and from one that was never admitted.
        self.assertEqual(self.waiting_view(), [])

        # The waiter queues for the only slot while this thread holds it.
        self.tune(parallel_runs=1)

        def waiter():
            with admission(self.config, self.resources('worker'), wait=True):
                pass

        with admission(self.config):
            thread = threading.Thread(target=waiter)
            thread.start()
            self.until(lambda: len(self.waiting_view()) == 1)
            view = self.waiting_view()[0]
            self.assertEqual(view["project"], "sample")
            self.assertEqual(view["roles"], ["worker"])
            self.assertEqual(view["slot"], 0)
            self.assertIn("enqueued_at", view)
        # The slot frees, the waiter takes it and leaves: the slot is no
        # longer waited for.
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.until(lambda: self.waiting_view() == [])

    def test_status_does_not_name_a_ticket_whose_owner_died(self):
        # A ticket whose pending lock is free names no waiter: the owner is
        # gone, so the slot is not being waited for.
        with self.queue_ticket(0, ["worker"]):
            self.assertEqual(self.waiting_view(), [])

    def test_status_reports_no_waiter_for_unreadable_queue_state(self):
        # State nobody can read names no waiter.
        path = self.config.data / 'slots/queue.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        try:
            self.assertEqual(self.waiting_view(), [])
        finally:
            path.unlink(missing_ok=True)

    def waiting_view(self):
        """The project's own view of the roles waiting for a slot."""
        return Project(self.config, "sample").status()["admission"]

    def resources(self, role):
        """The dispatch locks one role's admission holds."""
        return [role_exclusion(self.project.root, role)]

    @contextlib.contextmanager
    def pending_slot(self, owner):
        """Hold a pending slot's lock, as a waiting owner does."""
        with lock(self.config.data / 'slots' / f'pending-{owner}.lock', blocking=False):
            yield

    @contextlib.contextmanager
    def queue_ticket(self, owner, roles):
        """Write one admission ticket for the given roles."""
        path = self.config.data / 'slots/queue.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        resources = [f"sample/locks/run-{role}.lock" for role in roles]
        write_json(path, {"version": 1, "tickets": [
            {"id": "a" * 32, "owner": owner, "resources": resources,
             "enqueued_at": "2026-10-22T00:00:00.000000+00:00"}]})
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def test_the_child_report_wait_is_a_host_cost_bound(self):
        # A spawned child is a fresh interpreter that re-imports the
        # product's modules, so its start-up is a host cost. Every wait for
        # a child uses one shared bound instead of a short literal that a
        # loaded host can exceed, and the bound still detects a child that
        # never reports.
        self.assertGreaterEqual(CHILD_WAIT_SECONDS, 60)
        self.assertGreater(self.child_deadline() - time.monotonic(), CHILD_WAIT_SECONDS - 1)
        self.assertGreaterEqual(CHILD_RELEASE_SECONDS, CHILD_WAIT_SECONDS)

    def test_multiprocess_fifo_overflow_and_active_slot_cap(self):
        held = [contextlib.ExitStack() for _ in range(2)]
        with contextlib.ExitStack() as initial:
            for i, manager in enumerate(held):
                initial.enter_context(manager)
                manager.enter_context(lock(self.config.data / 'slots' / f'{i}.lock', blocking=False))
            first, release_first, _ = self.process(0)
            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
            second, release_second, _ = self.process(1)
            self.until(lambda: len(self.tickets(synchronized=True)) == 2)
            overflow, _, _ = self.process(2)
            self.message(('Busy', 2))
            overflow.join(CHILD_WAIT_SECONDS)
            self.assertEqual(len(self.tickets(synchronized=True)), 2)
            held[0].close()
            self.message(('entered', 0))
            self.assertEqual(len(self.tickets(synchronized=True)), 1)
            with self.assertRaises(queue.Empty):
                self.results.get(timeout=0.1)
            held[1].close()
            self.message(('entered', 1))
            self.assertEqual(self.tickets(synchronized=True), [])
            # Both slots remain held by callers. A new ticket cannot leak a
            # third active slot, and runs after the first active caller exits.
            third, release_third, _ = self.process(3)
            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
            self.assertEqual(self.active.value, 2)
            with self.assertRaises(queue.Empty):
                self.results.get(timeout=0.1)
            release_first.set()
            self.message(('entered', 3))
            self.assertLessEqual(self.peak.value, 2)
            release_second.set()
            release_third.set()
            for process in (first, second, third):
                process.join(CHILD_WAIT_SECONDS)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.tickets(synchronized=True), [])
        self.assertEqual(self.active.value, 0)
        with self.raw_slots():
            pass

    def test_admission_yields_elapsed_time_after_waiting(self):
        self.tune(parallel_runs=1, idle_seconds=0.01)
        with self.raw_slots():
            thread, stop, outcome = self.waiter(capture_elapsed=True)
            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
            time.sleep(0.05)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0][0], 'admitted')
        self.assertGreaterEqual(outcome[0][1], 0.05)
        self.assertLess(outcome[0][1], 5)

    def test_success_releases_single_pending_place_before_caller_cleanup(self):
        self.tune(parallel_runs=1)
        first, second = _Queue(self.config, ()), _Queue(self.config, ())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with self.raw_slots():
            with self.assertRaises(Busy):
                first.attempt(enqueue=True)
            self.assertEqual(len(self.tickets()), 1)
        # Stop exactly between attempt() returning dispatch locks and the
        # caller's cleanup. The first run still holds the only active slot,
        # but its former pending place must already be reusable.
        with first.attempt(enqueue=True):
            self.assertEqual(self.tickets(), [])
            with self.assertRaises(Busy) as caught:
                second.attempt(enqueue=True)
            self.assertNotIsInstance(caught.exception, _Full)
            self.assertEqual([t['id'] for t in self.tickets()], [second.identity])
            self.assertEqual(self.tickets()[0]['owner'], 0)
        with second.attempt(enqueue=True):
            self.assertEqual(self.tickets(), [])

    def test_dead_process_ticket_is_reclaimed_before_capacity_check(self):
        self.tune(parallel_runs=1)
        with self.raw_slots():
            process, _, _ = self.process(0)
            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
            old = self.tickets(synchronized=True)[0]['id']
            process.terminate()
            process.join(CHILD_WAIT_SECONDS)
            self.assertFalse(process.is_alive())
            replacement, release, _ = self.process(1)
            self.until(lambda: len(self.tickets(synchronized=True)) == 1 and self.tickets(synchronized=True)[0]['id'] != old)
        self.message(('entered', 1))
        release.set()
        replacement.join(CHILD_WAIT_SECONDS)
        self.assertFalse(replacement.is_alive())
        self.assertEqual(self.tickets(synchronized=True), [])
        self.assertEqual(len(list((self.config.data / 'slots').glob('pending-*.lock'))), 1)

    def test_fail_fast_cannot_bypass_sleeping_ticket_even_with_a_free_slot(self):
        self.tune(parallel_runs=1, idle_seconds=30)
        enqueued = threading.Event()
        sleeping = threading.Event()
        class WaitSignal(threading.Event):
            def wait(self, timeout=None):
                sleeping.set()
                return super().wait(timeout)
        stop = WaitSignal()
        original_enqueue = _Queue._enqueue
        def enqueue_and_signal(queue_obj, tickets):
            original_enqueue(queue_obj, tickets)
            enqueued.set()
        with self.raw_slots():
            with patch.object(_Queue, '_enqueue', enqueue_and_signal):
                thread, stop, outcome = self.waiter(stop=stop)
                self.assertTrue(enqueued.wait(5), 'waiting caller did not serialize its ticket')
                self.assertTrue(sleeping.wait(5), 'waiting caller did not enter its retry sleep')
        with self.assertRaises(Busy), slot(self.config):
            pass
        with self.assertRaises(Busy), admission(self.config, wait=False):
            pass
        driver = ScriptDriver()
        with self.assertRaises(Busy):
            Engine(self.config, driver=driver).run(self.project, 'reporter')
        self.assertEqual(driver.calls, [])
        with self.raw_slots():
            pass  # A queue refusal does not reserve a slot.
        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], Busy)
        self.assertEqual(self.tickets(), [])

    def test_resource_blocked_head_is_skipped_without_dispatch_lock_leakage(self):
        # Test both partial acquisition (workspace busy after role acquired)
        # and role contention. A later queued ticket has unrelated resources.
        for blocked in ('workspace', 'role'):
            with self.subTest(blocked=blocked):
                role_lock = self.project.root / 'locks/run-worker.lock'
                workspace_lock = self.project.root / 'locks/workspace.lock'
                resources = (role_lock, workspace_lock)
                path = workspace_lock if blocked == 'workspace' else role_lock
                resource_names = tuple(p.relative_to(self.config.data / 'projects').as_posix()
                                       for p in resources)
                resource_attempted = threading.Event()
                retry_allowed = threading.Event()
                resource_check_seen = False
                original_attempt = _Queue.attempt
                original_resources = _Queue._resources

                def resources_and_mark(queue_obj, stack, paths):
                    nonlocal resource_check_seen
                    if queue_obj.resources == resource_names and tuple(paths) == resource_names:
                        resource_check_seen = True
                    return original_resources(queue_obj, stack, paths)

                def attempt_and_signal(queue_obj, *, enqueue):
                    nonlocal resource_check_seen
                    if queue_obj.resources == resource_names:
                        resource_check_seen = False
                    try:
                        return original_attempt(queue_obj, enqueue=enqueue)
                    except Busy:
                        # Signal only after a resource check failed and
                        # attempt() closed its partial ExitStack/transaction.
                        if (queue_obj.resources == resource_names and queue_obj.alive is not None
                                and resource_check_seen):
                            resource_attempted.set()
                            # Keep the waiter between attempts until the main
                            # thread finishes its nonblocking leakage probes.
                            # A signal alone does not prevent a fresh retry
                            # from legitimately holding the partial role lock.
                            if not retry_allowed.wait(10):
                                raise RuntimeError('resource probe synchronization timed out')
                        raise

                with lock(path, blocking=False):
                    try:
                        with patch.object(_Queue, '_resources', resources_and_mark), \
                                patch.object(_Queue, 'attempt', attempt_and_signal):
                            thread, stop, outcome = self.waiter(resources=resources)
                            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
                            self.assertTrue(resource_attempted.wait(5),
                                            'waiter did not finish its blocked resource attempt')
                        with self.raw_slots():
                            pass  # Sleeping writer consumes no active capacity.
                        if blocked == 'workspace':
                            with lock(role_lock, blocking=False):
                                pass  # Partial acquisition also released.
                        with admission(self.config, (self.project.root / 'locks/run-reporter.lock',), wait=True):
                            self.assertEqual(len(self.tickets(synchronized=True)), 1)
                        self.assertTrue(thread.is_alive())
                    finally:
                        retry_allowed.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome, ['admitted'])
                self.assertEqual(self.tickets(synchronized=True), [])
                with self.raw_slots(), lock(role_lock, blocking=False), lock(workspace_lock, blocking=False):
                    pass

    def test_blocked_full_queue_does_not_prevent_ready_dispatch(self):
        # One pending place is full, but its owner cannot use the free slot.
        # Exercise daemon/fail-fast and retrying dispatch without timing races.
        self.tune(parallel_runs=1)
        for blocked in ('run-worker.lock', 'workspace.lock'):
            for wait in (False, True):
                with self.subTest(blocked=blocked, wait=wait):
                    resources = (self.project.root / 'locks/run-worker.lock',
                                 self.project.root / 'locks/workspace.lock')
                    head = _Queue(self.config, resources)
                    try:
                        with lock(self.project.root / 'locks' / blocked, blocking=False):
                            with self.assertRaises(Busy):
                                head.attempt(enqueue=True)
                            before = self.tickets()
                            self.assertEqual(len(before), 1)
                            with admission(self.config,
                                           (self.project.root / 'locks/run-reporter.lock',),
                                           wait=wait):
                                self.assertEqual(self.tickets(), before)
                                with self.assertRaises(Busy), self.raw_slots():
                                    pass
                            self.assertEqual(self.tickets(), before)
                        # Once ready, the older waiter retains priority.
                        with self.assertRaises(Busy), admission(self.config, wait=False):
                            pass
                        with head.attempt(enqueue=True):
                            self.assertEqual(self.tickets(), [])
                    finally:
                        head.close()
                    with self.raw_slots():
                        pass

    def test_wait_keeps_its_ticket_beyond_old_deadline_and_dispatches(self):
        self.tune(busy_retry_seconds=0.01)
        with self.raw_slots():
            thread, stop, outcome = self.queued_waiter()
            original = self.tickets()
            # Advance only admission's clock; the real test stays bounded.
            with patch('mizu.admission.time.monotonic', return_value=time.monotonic() + 86400):
                time.sleep(0.08)
                self.assertTrue(thread.is_alive())
                self.assertEqual(outcome, [])
                self.assertEqual(self.tickets(), original)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ['admitted'])
        self.assertEqual(self.tickets(), [])

    def test_enqueue_time_metadata_is_bounded_and_aware(self):
        candidate = _Queue(self.config, ())
        for value in (None, 0, True, '', 'not-a-time', '2026-01-01T01:02:03', 'x' * 33):
            with self.subTest(value=value):
                candidate._write([{'id': 'f' * 32, 'owner': 0, 'resources': [], 'enqueued_at': value}])
                with self.assertRaisesRegex(Busy, 'queue is invalid'):
                    candidate._read()

    def test_enqueue_time_is_recorded_once_and_survives_queue_updates(self):
        with self.raw_slots():
            with patch('mizu.admission.now', return_value='2026-01-01T01:02:03+00:00'):
                first, _, first_result = self.queued_waiter()
            original = self.tickets()[0]
            self.assertEqual(original['enqueued_at'], '2026-01-01T01:02:03+00:00')
            with patch('mizu.admission.now', return_value='2026-01-01T04:05:06+00:00'):
                second, _, second_result = self.queued_waiter()
            rows = self.tickets()
            self.assertEqual(rows[0], original)
            self.assertEqual(rows[1]['enqueued_at'], '2026-01-01T04:05:06+00:00')
        first.join(5); second.join(5)
        self.assertEqual(first_result, ['admitted'])
        self.assertEqual(second_result, ['admitted'])

    def test_configured_deadline_retires_ticket_without_dispatch(self):
        self.tune(admission_wait_seconds=0.06)
        with self.raw_slots():
            with self.assertRaisesRegex(Busy, 'admission wait expired'), admission(self.config, wait=True):
                self.fail('deadline admitted without capacity')
            self.assertEqual(self.tickets(), [])

    def test_caller_that_never_waited_is_not_refused_by_an_expired_wait(self):
        # The knob bounds the wait for capacity. A caller that is admitted
        # without waiting has no wait to expire, so a slow revalidation on a
        # loaded host must not refuse the run as an expired wait.
        self.tune(admission_wait_seconds=1)
        clock = {'now': 1000.0}
        def slow_check():
            clock['now'] += 5.0
        with patch('mizu.admission.time.monotonic', lambda: clock['now']):
            with admission(self.config, wait=True, check=slow_check):
                pass
        self.assertEqual(self.tickets(), [])

    def test_admitted_run_is_not_refused_by_a_late_dispatch_revalidation(self):
        # The ticket is retired and the slot is taken before the dispatch
        # revalidation, so an expired wait must not lose an admitted run.
        self.tune(parallel_runs=1, admission_wait_seconds=10)
        clock = {'now': 1000.0}
        admitted = {'value': False}
        original = _Queue.attempt
        def attempt(queue_obj, *, enqueue):
            result = original(queue_obj, enqueue=enqueue)
            admitted['value'] = True
            return result
        def late_check():
            if admitted['value']:
                clock['now'] += 100.0
        with patch('mizu.admission.time.monotonic', lambda: clock['now']), \
                patch.object(_Queue, 'attempt', attempt):
            with admission(self.config, wait=True, check=late_check):
                pass
        self.assertEqual(self.tickets(), [])

    def test_concurrent_fail_fast_admissions_share_one_process_ticket(self):
        self.tune(parallel_runs=2)
        resource = self.resources('worker')[0]
        barrier = threading.Barrier(2)
        outcomes = []
        def fail_fast():
            try:
                with admission(self.config, (resource,), wait=False, enqueue=True,
                               check=lambda: barrier.wait(5)):
                    outcomes.append('admitted')
            except BaseException as exc:
                outcomes.append(exc)
        with lock(resource):
            threads = [threading.Thread(target=fail_fast) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive(), 'concurrent fail-fast admission did not finish')
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(all(isinstance(outcome, Busy) for outcome in outcomes))
            self.assertEqual(len(self.tickets(synchronized=True)), 1)
        # A later attempt reuses and retires the same ticket and pending lock.
        with admission(self.config, (resource,), wait=False, enqueue=True):
            pass
        self.assertEqual(self.tickets(synchronized=True), [])

    def test_pending_daemon_tickets_are_scoped_to_their_queue_root(self):
        self.tune(parallel_runs=2)
        other_data = self.config.data.parent / 'other-admission-state'
        other_config = dataclasses.replace(self.config, data=other_data)
        resource_a = self.resources('worker')[0]
        resource_b = role_exclusion(other_data / 'projects' / 'sample', 'worker')
        first = _Queue(self.config, (resource_a,), reuse_pending=True)
        second = _Queue(other_config, (resource_b,), reuse_pending=True)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertEqual(first.resources, second.resources)
        with contextlib.ExitStack() as held:
            held.enter_context(lock(resource_a))
            held.enter_context(lock(resource_b))
            for queue_obj in (first, second):
                with self.assertRaises(Busy):
                    queue_obj.attempt(enqueue=True)
            self.assertEqual(len(read_json(first.root / 'queue.json')['tickets']), 1)
            self.assertEqual(len(read_json(second.root / 'queue.json')['tickets']), 1)
            self.assertNotEqual(first.identity, second.identity)
        first.close()
        second.close()

    def test_fail_fast_daemon_ticket_survives_continuous_one_shot_arrivals(self):
        # A daemon worker that cannot dispatch enqueues one pending ticket and
        # defers. Continuous one-shot arrivals queue behind it instead of
        # pushing it out of reach, and retrying admission dispatches that same
        # ticket once; no ticket is dispatched twice.
        self.tune(parallel_runs=3, idle_seconds=0.01, admission_wait_seconds=1)
        daemon_outcome = []
        with self.raw_slots():
            def first_daemon_attempt():
                try:
                    with admission(self.config, self.resources('worker'), wait=False, enqueue=True):
                        daemon_outcome.append('admitted')
                except BaseException as exc:
                    daemon_outcome.append(exc)
            thread = threading.Thread(target=first_daemon_attempt)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(daemon_outcome), 1)
            self.assertIsInstance(daemon_outcome[0], Busy)
            self.assertEqual(len(self.tickets(synchronized=True)), 1)
            daemon_ticket = self.tickets(synchronized=True)[0]['id']

            oneshots = []
            for _ in range(2):
                waiter_thread, stop, result = self.waiter(
                    resources=self.resources('reporter'), wait=True, enqueue=False)
                oneshots.append((waiter_thread, stop, result))
                self.until(lambda: len(self.tickets(synchronized=True)) >= len(oneshots) + 1)

        # The daemon ticket kept its place ahead of every one-shot arrival.
        self.assertEqual(self.tickets(synchronized=True)[0]['id'], daemon_ticket)

        # Retry the same daemon ticket; it is admitted without a second enqueue.
        retry_outcome = []
        with admission(self.config, self.resources('worker'), wait=False, enqueue=True):
            retry_outcome.append('admitted')
        self.assertEqual(retry_outcome, ['admitted'])

        for waiter_thread, stop, result in oneshots:
            waiter_thread.join(5)
            self.assertFalse(waiter_thread.is_alive())
            self.assertEqual(result, ['admitted'])

        self.assertEqual(self.tickets(synchronized=True), [])
        with self.raw_slots():
            pass

    def test_wait_fails_fast_on_corrupt_queue(self):
        write_json(self.config.data / 'slots/queue.json', {'version': 99, 'tickets': []})
        with self.assertRaisesRegex(Busy, 'queue is invalid'), admission(self.config, wait=True):
            self.fail('corrupt state admitted')

    def test_stop_and_callback_cancellation_remove_tickets(self):
        with self.raw_slots():
            self.tune(idle_seconds=30, busy_retry_seconds=15)
            thread, stop, outcome = self.queued_waiter()
            stop.set()
            thread.join(5)
            self.assertIsInstance(outcome[0], Busy)
            self.assertEqual(self.tickets(), [])
            cancelled = threading.Event()
            def check():
                if cancelled.is_set():
                    raise Cancelled('offline admission cancellation')
            self.tune()
            thread, _, outcome = self.queued_waiter(check=check)
            cancelled.set()
            thread.join(5)
            self.assertIsInstance(outcome[0], Cancelled)
            self.assertEqual(self.tickets(), [])

    def test_cleanup_contending_with_transaction_is_reaped_on_next_admission(self):
        # A closing ticket invalidates its liveness even when it cannot edit
        # the list. A subsequent admission must reap it *before* cap/order.
        with self.raw_slots():
            thread, stop, outcome = self.waiter()
            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
            # The waiter may still be finishing the enqueue transaction
            # after its row becomes visible. Wait for that transaction, then
            # hold the lock so close() deterministically exercises its
            # nonblocking cleanup path.
            with lock(self.config.data / 'slots/queue.lock'):
                stop.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(outcome[0], Busy)
                self.assertEqual(len(self.tickets()), 1)
        with slot(self.config):
            self.assertEqual(self.tickets(), [])

    def test_error_at_dispatch_releases_slot_and_resources(self):
        resource = self.project.root / 'locks/run-worker.lock'
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise Denied('offline revoked authority at dispatch')
        with self.assertRaises(Denied), admission(self.config, (resource,), check=check):
            pass
        with self.raw_slots(), lock(resource, blocking=False):
            pass
        self.assertEqual(self.tickets(), [])

    def test_malformed_queue_fails_closed_without_leaking_locks(self):
        for state in ({'version': 7, 'tickets': []}, {'version': 1, 'tickets': [{}]}):
            write_json(self.config.data / 'slots/queue.json', state)
            with self.assertRaises(Busy), slot(self.config):
                pass
            with self.raw_slots():
                pass
        (self.config.data / 'slots/queue.json').write_text('{')
        with self.assertRaises(Busy), slot(self.config):
            pass

    def test_regular_queue_file_and_missing_state_are_readable(self):
        candidate = _Queue(self.config, ())
        # Also exercise the portable fallback used without O_NOFOLLOW.
        for nofollow in (getattr(os, 'O_NOFOLLOW', 0), 0):
            with self.subTest(nofollow=nofollow), \
                    patch('mizu.admission.os.O_NOFOLLOW', nofollow, create=True):
                with lock(candidate.root / 'queue.lock', blocking=False):
                    self.assertEqual(candidate._read(), [])
                    candidate._write([])
                    self.assertEqual(candidate._read(), [])
                (candidate.root / 'queue.json').unlink()
        candidate._write([])
        self.assertEqual(self.probe_queue_state(), ('admitted', ''))

    def test_directory_queue_state_fails_closed(self):
        candidate = _Queue(self.config, ())
        (candidate.root / 'queue.json').mkdir(parents=True)
        self.assertEqual(self.probe_queue_state(), ('Busy', 'Execution admission queue is unreadable'))
        with lock(candidate.root / 'queue.lock', blocking=False), self.raw_slots():
            pass

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'mkfifo'), 'requires POSIX FIFOs')
    def test_fifo_queue_state_is_rejected_without_blocking_or_leaking_locks(self):
        candidate = _Queue(self.config, ())
        candidate.root.mkdir(parents=True, exist_ok=True)
        path = candidate.root / 'queue.json'
        os.mkfifo(path)
        # There is deliberately no writer; a blocking open would never return.
        self.assertEqual(self.probe_queue_state(), ('Busy', 'Execution admission queue is unreadable'))
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
        self.assertEqual(list(candidate.root.glob('pending-*.lock')), [])
        with lock(candidate.root / 'queue.lock', blocking=False), self.raw_slots():
            pass

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX symlinks')
    def test_symlink_queue_state_is_rejected_without_following_target(self):
        candidate = _Queue(self.config, ())
        candidate.root.mkdir(parents=True, exist_ok=True)
        path = candidate.root / 'queue.json'
        regular, missing, fifo = (self.root / name for name in ('regular.json', 'missing.json', 'fifo'))
        raw = canonical({'version': 1, 'tickets': []})
        regular.write_bytes(raw)
        os.mkfifo(fifo)
        for target in (regular, missing, fifo):
            with self.subTest(target=target.name):
                path.symlink_to(target)
                try:
                    self.assertEqual(self.probe_queue_state(), ('Busy', 'Execution admission queue is unreadable'))
                    self.assertTrue(path.is_symlink())
                    with lock(candidate.root / 'queue.lock', blocking=False), self.raw_slots():
                        pass
                finally:
                    path.unlink()
        self.assertEqual(regular.read_bytes(), raw)
        self.assertFalse(missing.exists())
        self.assertEqual(list(candidate.root.glob('pending-*.lock')), [])
        # The fallback must reject an existing symlink even without O_NOFOLLOW.
        for target in (regular, missing):
            path.symlink_to(target)
            try:
                with patch('mizu.admission.os.O_NOFOLLOW', 0, create=True), \
                        patch('mizu.admission.os.open') as opened:
                    with self.assertRaisesRegex(Busy, '^Execution admission queue is unreadable$'):
                        candidate._read()
                    opened.assert_not_called()
            finally:
                path.unlink()

    def test_nonregular_descriptor_is_rejected_before_reading_or_parsing(self):
        candidate = _Queue(self.config, ())
        candidate._write([])
        for mode in (stat.S_IFIFO, stat.S_IFDIR, stat.S_IFSOCK, stat.S_IFCHR):
            with self.subTest(mode=mode):
                with patch('mizu.admission.os.fstat', return_value=Mock(st_mode=mode, st_size=0)), \
                        patch('mizu.admission.os.read', return_value=b'') as read, \
                        patch('mizu.admission.json.loads') as parse:
                    with self.assertRaisesRegex(Busy, '^Execution admission queue is unreadable$'):
                        candidate._read()
                    read.assert_not_called()
                    parse.assert_not_called()

    def test_queue_open_stat_and_read_errors_are_generic_busy(self):
        candidate = _Queue(self.config, ())
        candidate._write([])
        for operation in ('open', 'fstat', 'read'):
            # ENOENT is empty state only at open, not after obtaining a descriptor.
            errors = (PermissionError, OSError) if operation == 'open' else (PermissionError, FileNotFoundError)
            for error in errors:
                with self.subTest(operation=operation, error=error.__name__):
                    with lock(candidate.root / 'queue.lock', blocking=False):
                        with patch('mizu.admission.os.' + operation, side_effect=error('invalid local state')), \
                                patch('mizu.admission.os.close', wraps=os.close) as closed:
                            with self.assertRaisesRegex(Busy, '^Execution admission queue is unreadable$'):
                                candidate._read()
                            if operation == 'open':
                                closed.assert_not_called()
                            else:
                                closed.assert_called_once()
                        if operation != 'open':
                            with self.assertRaises(OSError):
                                os.fstat(closed.call_args.args[0])
                    with lock(candidate.root / 'queue.lock', blocking=False), self.raw_slots():
                        pass

    def test_corrupt_tickets_fail_before_liveness_or_resource_filesystem_effects(self):
        queue_root = self.config.data / 'slots'
        unexpected = self.root / 'unexpected/created.lock'
        ticket = {'id': 'a' * 32, 'owner': 0,
                  'resources': ['sample/locks/run-worker.lock']}
        bad_resources = [
            str(unexpected),
            str(self.project.root / 'locks/run-worker.lock'),  # Raw absolute paths are not state identifiers.
            '../unexpected/created.lock',
            'sample/locks/../../../unexpected/created.lock',
            'sample/locks/control.lock',
            'sample/workspace/created.lock',
            'sample/locks/run-../created.lock',
            'sample/locks/run-.lock',
            'sample/locks/run-' + 'a' * 64 + '.lock',
            'a' * 64 + '/locks/workspace.lock',
            'sample\\locks\\run-worker.lock',
            'sample//locks/workspace.lock',
            'sample/locks/./workspace.lock',
            'sample/locks/workspace.lock\x00',
            7,
        ]
        states = [
            {'version': 1, 'tickets': [dict(ticket, owner=owner, resources=[])]}
            for owner in (-1, self.config.limits.parallel_runs, 1000000, True, '0')
        ]
        states.extend({'version': 1, 'tickets': [dict(ticket, resources=[resource])]}
                      for resource in bad_resources)
        states.extend([
            {'version': 1, 'tickets': [dict(ticket, id=f'{i:032x}', owner=i, resources=[])
                                     for i in range(self.config.limits.parallel_runs + 1)]},
            {'version': 1, 'tickets': [ticket, dict(ticket, id='b' * 32)]},
            {'version': 1, 'tickets': [ticket, dict(ticket, owner=1)]},
            {'version': 1, 'tickets': [dict(ticket, id='z' * 32)]},
            {'version': True, 'tickets': []},
            {'version': 1, 'tickets': [], 'extra': []},
            {'version': 1, 'tickets': [dict(ticket, extra=[])]},
            # Validate the entire list before probing even the first valid ticket.
            {'version': 1, 'tickets': [ticket, dict(ticket, id='b' * 32, owner=1,
                                                   resources=[str(unexpected)])]},
        ])
        with lock(queue_root / 'pending-0.lock', blocking=False):
            for state in states:
                with self.subTest(state=state):
                    write_json(queue_root / 'queue.json', state)
                    before = (queue_root / 'queue.json').read_bytes()
                    candidate = _Queue(self.config, ())
                    try:
                        with patch('mizu.admission.lock', wraps=lock) as locks:
                            with self.assertRaises(Busy), contextlib.ExitStack() as stack:
                                stack.enter_context(candidate.attempt(enqueue=True))
                            self.assertEqual(locks.call_args_list,
                                             [call(queue_root / 'queue.lock', blocking=False)])
                        self.assertEqual((queue_root / 'queue.json').read_bytes(), before)
                        self.assertFalse(unexpected.parent.exists())
                        self.assertFalse((queue_root / 'pending-1.lock').exists())
                    finally:
                        candidate.close()

    def test_oversized_queue_is_rejected_before_parsing(self):
        candidate = _Queue(self.config, ())
        path = self.config.data / 'slots/queue.json'
        write_json(path, {'version': 1, 'tickets': []})
        with path.open('ab') as stream:
            stream.write(b' ' * candidate.state_bytes)
        with patch('mizu.admission.os.read', wraps=os.read) as read:
            with patch('mizu.admission.json.loads') as parse, patch('mizu.admission.lock', wraps=lock) as locks:
                with self.assertRaises(Busy):
                    candidate.attempt(enqueue=False)
                read.assert_not_called()
                parse.assert_not_called()
                self.assertEqual(locks.call_args_list, [call(path.parent / 'queue.lock', blocking=False)])

    def test_huge_slot_limit_with_tiny_queue_uses_only_bounded_reads(self):
        for capacity in (1_000_000, 1_073_741_824):
            self.tune(parallel_runs=capacity)
            candidate = _Queue(self.config, ())
            self.assertEqual(candidate.capacity, capacity)
            self.assertGreater(candidate.state_bytes, 64 * 1024)
            path = candidate.root / 'queue.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            for raw in (canonical({'version': 1, 'tickets': []}), b'{'):
                with self.subTest(capacity=capacity, raw=raw):
                    path.write_bytes(raw)
                    read = os.read
                    def bounded_read(fd, size):
                        self.assertGreater(size, 0)
                        self.assertLessEqual(size, 64 * 1024)
                        return read(fd, size)
                    with patch('mizu.admission.os.read', side_effect=bounded_read) as reader:
                        if raw == b'{':
                            with self.assertRaises(Busy):
                                candidate._read()
                        else:
                            self.assertEqual(candidate._read(), [])
                        self.assertTrue(reader.called)
                    self.assertEqual(path.read_bytes(), raw)

    def test_queue_byte_boundary_round_trips_in_bounded_chunks(self):
        self.tune(parallel_runs=512)
        candidate = _Queue(self.config, ())
        path = candidate.root / 'queue.json'
        raw = canonical({'version': 1, 'tickets': []})
        raw += b' ' * (candidate.state_bytes - len(raw))
        self.assertGreater(len(raw), 64 * 1024)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        with patch('mizu.admission.os.read', wraps=os.read) as read:
            self.assertEqual(candidate._read(), [])
            sizes = [args[0][1] for args in read.call_args_list]
            self.assertGreater(len(sizes), 2)
            self.assertTrue(all(0 < size <= 64 * 1024 for size in sizes))
        self.assertEqual(path.read_bytes(), raw)

    def test_queue_growth_past_byte_bound_is_rejected_before_parsing(self):
        self.tune(parallel_runs=512)
        candidate = _Queue(self.config, ())
        path = candidate.root / 'queue.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b' ' * (candidate.state_bytes + 1))
        received = 0
        read = os.read
        def growing_read(fd, size):
            nonlocal received
            chunk = read(fd, size)
            received += len(chunk)
            return chunk
        with patch('mizu.admission.os.read', side_effect=growing_read) as reader:
            # A size check alone is not sufficient if the file grows afterwards.
            with patch('mizu.admission.os.fstat', return_value=Mock(
                    st_size=candidate.state_bytes, st_mode=stat.S_IFREG)), \
                    patch('mizu.admission.json.loads') as parse:
                with self.assertRaises(Busy):
                    candidate._read()
                parse.assert_not_called()
            self.assertEqual(received, candidate.state_bytes + 1)
            self.assertTrue(all(0 < args[0][1] <= 64 * 1024 for args in reader.call_args_list))

    def test_recursive_queue_json_fails_closed(self):
        self.tune(parallel_runs=8)
        candidate = _Queue(self.config, ())
        raw = b'[' * 1100 + b']' * 1100
        self.assertLess(len(raw), candidate.state_bytes)
        path = candidate.root / 'queue.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        with self.assertRaises(Busy):
            candidate.attempt(enqueue=False)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(candidate.root.glob('pending-*.lock')), [])

    def test_queue_byte_bound_fits_maximum_valid_tickets(self):
        # Multi-digit owner indices, longest project/role identifiers and two
        # resources per ticket must still round-trip at full pending capacity.
        self.tune(parallel_runs=12)
        candidate = _Queue(self.config, ())
        project, role = 'a' * 63, 'b' * 63
        resources = [f'{project}/locks/run-{role}.lock', f'{project}/locks/workspace.lock']
        tickets = [{'id': f'{i:032x}', 'owner': i, 'resources': resources,
                    'enqueued_at': '2099-01-01T00:00:00.000000+00:00'}
                   for i in range(candidate.capacity)]
        candidate._write(tickets)
        self.assertLessEqual((candidate.root / 'queue.json').stat().st_size, candidate.state_bytes)
        with contextlib.ExitStack() as held:
            for owner in range(candidate.capacity):
                held.enter_context(lock(candidate.root / f'pending-{owner}.lock', blocking=False))
            with lock(candidate.root / 'queue.lock', blocking=False):
                self.assertEqual(candidate._read(), tickets)

    def test_engine_revalidates_control_budget_and_disk_before_dispatch(self):
        for change in ('pause', 'unarm', 'drain', 'budget', 'disk'):
            with self.subTest(change=change):
                self.project.set_control(armed=True, paused=False, draining=False)
                self.tune(daily_requests=1)
                driver = ScriptDriver()
                outcome = []
                engine = Engine(self.config, driver=driver)
                ticket_seen = threading.Event()
                dispatch_check = threading.Event()
                continue_check = threading.Event()
                original_check = engine._check_admission

                def check_at_dispatch(project):
                    # After the test has observed the queued ticket, its
                    # disappearance means attempt() acquired the slot and is
                    # performing the final check before provider dispatch.
                    if ticket_seen.is_set() and not self.tickets(synchronized=True):
                        dispatch_check.set()
                        if not continue_check.wait(5):
                            raise RuntimeError('dispatch-check test barrier timed out')
                    original_check(project)

                engine._check_admission = check_at_dispatch

                def run():
                    try:
                        engine.run(self.project, 'worker', retry_busy=True)
                    except BaseException as exc:
                        outcome.append(exc)

                thread = None
                try:
                    with patch('mizu.runtime.shutil.disk_usage') as disk:
                        disk.return_value.free = 1048576
                        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
                            self.config.limits, free_disk_mb=1))
                        engine.config = self.config
                        held = self.raw_slots()
                        try:
                            thread = threading.Thread(target=run)
                            thread.start()
                            self.until(lambda: len(self.tickets(synchronized=True)) == 1)
                            ticket_seen.set()
                        finally:
                            held.close()
                        self.assertTrue(dispatch_check.wait(5), 'engine did not reach dispatch revalidation')
                        if change == 'pause':
                            self.project.set_control(paused=True)
                        elif change == 'unarm':
                            self.project.set_control(armed=False)
                        elif change == 'drain':
                            self.project.set_control(draining=True)
                        elif change == 'budget':
                            Budget(self.config.data / 'budget', 1).take('offline', project='sample')
                        elif change == 'disk':
                            disk.return_value.free = 0
                        continue_check.set()
                        thread.join(5)
                finally:
                    # Never let a failed assertion strand a retrying Engine
                    # thread or carry its queue ticket into the next subtest.
                    continue_check.set()
                    engine.stop.set()
                    if thread is not None:
                        thread.join(5)
                    for path in (self.config.data / 'budget').glob('*.json'):
                        path.unlink()
                self.assertIsNotNone(thread)
                self.assertFalse(thread.is_alive())
                expected = Busy if change == 'drain' else Denied if change in ('pause', 'unarm') else InfraExceeded
                self.assertIsInstance(outcome[0], expected)
                self.assertEqual(driver.calls, [])
                self.assertEqual(self.tickets(synchronized=True), [])

    def test_admit_requests_role_lock_and_workspace_lock_for_writes(self):
        import contextlib
        from mizu.runtime import Engine
        engine = Engine(self.config)
        for role_name, expected in (
                ('reporter', ('run-reporter.lock',)),
                ('worker', ('run-worker.lock', 'workspace.lock'))):
            with self.subTest(role=role_name):
                role = self.config.roles[role_name]
                seen = {}

                def fake_admission(config, resources=(), *, wait=False, enqueue=False, stop=None, check=None):
                    seen['resources'] = tuple(resources)
                    return contextlib.nullcontext(0.0)

                with contextlib.ExitStack() as stack, patch('mizu.runtime.admission', side_effect=fake_admission):
                    self.assertEqual(engine._admit(self.project, role_name, role, stack, wait=False), 0.0)
                names = tuple(path.name for path in seen['resources'])
                self.assertEqual(names, expected)
                self.assertTrue(all(path.parent == self.project.root / 'locks' for path in seen['resources']))
