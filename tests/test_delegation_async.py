"""Opt-in single-child async delegation: start, join and in-process handback."""
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import delegation_async
from mizu.admission import role_exclusion
from mizu.delegation_jobs import DelegationJobs, digest, canonical
from mizu.errors import Busy, Denied
from mizu.fs import lock
from mizu.config import load
from mizu.project import Project
from mizu.protocol import tool_definitions
from mizu.runtime import Engine


def _configured(fixture):
    roles = dict(fixture.config.roles)
    worker = dataclasses.replace(roles['worker'], tools_per_run=2000,
                                 capabilities=(*roles['worker'].capabilities,
                                               'delegate', 'delegate_join'))
    helper = dataclasses.replace(roles['searcher'], name='helper',
                                 capabilities=('files', 'read', 'finish'))
    return dataclasses.replace(fixture.config,
                               roles={**roles, 'worker': worker, 'helper': helper})


class _Driver:
    requires_sandbox = False

    def __init__(self, work):
        self.work = work

    def execute(self, context, prompt, *, profile=None):
        self.work(context, prompt, profile)
        if context.finished is None:
            context.handle('finish', {'outcome': 'wait', 'summary': 'Parent sealed', 'state': 'done'})
        return {'requests': context.request_count, 'profile': profile or context.role.profile}


def _join_until_terminal(ctx, job_id):
    for _ in range(150):
        result = ctx.handle('delegate_join', {'job_id': job_id})
        if 'result_available' not in result:
            return result
        time.sleep(.02)
    raise AssertionError('async delegation did not hand back')


class AsyncDelegationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = _configured(self)
        self.task = 'Implement the focused slice'

    def run_parent(self, work):
        return Engine(self.config, driver=_Driver(work)).run(self.project, 'worker')

    def test_default_delegate_is_unchanged_and_creates_no_job_ledger(self):
        seen = {}
        path = self.project.root / 'delegation-jobs'

        def work(ctx, *_):
            if ctx.role.name == 'worker':
                seen['before'] = path.exists()
                seen['result'] = ctx.handle('delegate', {'role': 'helper', 'task': self.task})
                seen['after'] = path.exists()
            else:
                seen['finish'] = ctx.finished is None
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Sync child done', 'state': 'x'})

        result = self.run_parent(work)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(seen['before'])
        self.assertFalse(seen['after'])
        self.assertEqual(seen['result']['answer'], 'Sync child done')
        self.assertEqual(seen['result']['role'], 'helper')
        self.assertTrue(seen['finish'])

    def test_async_mode_without_parent_delegate_join_is_denied_before_reservation(self):
        roles = dict(self.config.roles)
        worker = roles['worker']
        self.config = dataclasses.replace(self.config, roles={
            **roles, 'worker': dataclasses.replace(
                worker, capabilities=tuple(c for c in worker.capabilities if c != 'delegate_join'))})
        ledger = self.project.root / 'delegation-jobs'
        seen = {}

        def work(ctx, *_):
            if ctx.role.name != 'worker':
                return
            seen['capabilities'] = ctx.role.capabilities
            seen['before'] = ledger.exists()
            with self.assertRaisesRegex(Denied, 'requires the delegate_join capability'):
                ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                        'async_mode': True})
            seen['after'] = ledger.exists()
            seen['view'] = ctx._reservation_view

        result = self.run_parent(work)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('delegate', seen['capabilities'])
        self.assertNotIn('delegate_join', seen['capabilities'])
        self.assertFalse(seen['before'])
        self.assertFalse(seen['after'])
        self.assertIsNone(seen['view'])
        self.assertIsNone(DelegationJobs.existing_reservation(self.project.root))

    def test_async_start_is_prompt_join_is_pending_then_exact_and_idempotent(self):
        started, release = threading.Event(), threading.Event()
        seen = {}

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                started.set()
                seen['child_run'] = ctx.run_dir.name
                if not release.wait(5):
                    raise AssertionError('child release timed out')
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Async child done',
                                      'state': 'child state'})
                return
            began = time.monotonic()
            seen['snapshot'] = ctx.snapshot['id']
            accepted = ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                               'async_mode': True})
            seen['elapsed'] = time.monotonic() - began
            seen['accepted'] = accepted
            self.assertTrue(started.wait(5))
            seen['pending'] = ctx.handle('delegate_join', {'job_id': accepted['id']})
            release.set()
            seen['result'] = _join_until_terminal(ctx, accepted['id'])
            seen['again'] = ctx.handle('delegate_join', {'job_id': accepted['id']})

        release.set()  # overwritten by the parent only after the pending join
        started.clear()
        release.clear()
        result = self.run_parent(work)
        self.assertEqual(result['status'], 'completed')
        accepted = seen['accepted']
        self.assertEqual(set(accepted), {'id', 'status', 'reserved', 'run'})
        self.assertEqual(len(accepted['id']), 64)
        self.assertEqual(accepted['run'], seen['child_run'])
        self.assertLess(seen['elapsed'], 1.0)
        self.assertEqual(set(seen['pending']),
                         {'id', 'status', 'reserved', 'result_available', 'run'})
        self.assertEqual(seen['pending']['status'], 'running')
        self.assertEqual(seen['pending']['run'], seen['child_run'])
        self.assertNotIn('answer', seen['pending'])
        self.assertEqual(seen['result'], seen['again'])
        self.assertEqual(seen['result']['answer'], 'Async child done')
        self.assertEqual(seen['result']['task'], self.task)
        self.assertEqual(seen['result']['role'], 'helper')
        self.assertEqual(seen['result']['snapshot'], seen['snapshot'])
        store = DelegationJobs(self.project.root)
        self.assertIsNone(store.reservation())
        self.assertEqual(store.status('worker', accepted['id'])['status'], 'terminal')

    def test_spend_is_shared_and_child_has_no_collaboration_tools(self):
        seen = {}

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                seen['spend_child'] = ctx._spend
                seen['tools'] = [tool['name'] for tool in tool_definitions(ctx.role.capabilities)]
                seen['delegate_injection'] = ctx.delegate
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Child', 'state': 'x'})
                return
            accepted = ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                               'async_mode': True})
            seen['spend_parent'] = ctx._spend
            seen['result'] = _join_until_terminal(ctx, accepted['id'])

        self.run_parent(work)
        self.assertIs(seen['spend_child'], seen['spend_parent'])
        self.assertNotIn('mizu_delegate', seen['tools'])
        self.assertNotIn('mizu_delegate_join', seen['tools'])
        self.assertIsNone(seen['delegate_injection'])

    def test_restart_retry_never_relaunches_a_claimed_child(self):
        count = []
        release = threading.Event()
        child_started = threading.Event()

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                count.append(ctx.run_dir.name)
                child_started.set()
                release.wait(5)
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Child', 'state': 'x'})
                return
            snap = ctx.snapshot
            accepted = ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                               'async_mode': True})
            self.assertTrue(child_started.wait(5))
            key = digest(canonical([ctx.run_dir.name, 'helper', self.task, snap['id']]))[:32]
            with patch.object(delegation_async, '_CHILDREN', {}):
                retry = delegation_async.start(Engine(self.config), ctx, key, 'helper',
                                               self.task, snap)
            count_before = list(count)
            time.sleep(.05)
            self.assertEqual(count, count_before)
            self.assertEqual(retry['status'], 'running')
            self.assertEqual(retry['run'], count[0])
            release.set()
            seen = _join_until_terminal(ctx, accepted['id'])
            self.assertEqual(seen['answer'], 'Child')

        self.run_parent(work)
        self.assertEqual(len(count), 1)

    def test_nonterminal_retry_from_fresh_owner_context_keeps_one_child_and_fence(self):
        """A new owner view cannot relaunch or unfence a still-running child."""
        count = []
        release = threading.Event()
        child_started = threading.Event()

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                count.append(ctx.run_dir.name)
                child_started.set()
                release.wait(5)
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Child', 'state': 'x'})
                return
            snap = ctx.snapshot
            accepted = ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                               'async_mode': True})
            self.assertTrue(child_started.wait(5))
            key = digest(canonical([ctx.run_dir.name, 'helper', self.task, snap['id']]))[:32]
            child = delegation_async._CHILDREN[accepted['id']]
            self.assertFalse(child.done.is_set())
            self.assertTrue(child.thread.is_alive())
            # Model a restarted owner: a fresh context with only the durable
            # reservation projection, while the original child is nonterminal.
            fresh = self.context(reservation_view=DelegationJobs.reservation_projection(
                self.project.root))
            retry = delegation_async.start(Engine(self.config), fresh, key, 'helper',
                                           self.task, snap)
            self.assertEqual(retry['id'], accepted['id'])
            self.assertEqual(retry['run'], count[0])
            self.assertFalse(retry.get('result_available', False))
            self.assertIs(delegation_async._CHILDREN[accepted['id']], child)
            # The child's helper-role exclusion fence is still held.
            with self.assertRaises(Busy):
                with lock(role_exclusion(self.project.root, 'helper'), blocking=False):
                    pass
            time.sleep(.05)
            self.assertEqual(count, [child.run_id])
            self.assertFalse(child.done.is_set())
            self.assertIsNotNone(DelegationJobs(self.project.root).reservation())
            release.set()
            result = _join_until_terminal(ctx, accepted['id'])
            self.assertEqual(result['answer'], 'Child')
            self.assertEqual(result['run'], count[0])
            self.assertNotIn(accepted['id'], delegation_async._CHILDREN)

        self.run_parent(work)
        self.assertEqual(len(count), 1)
        self.assertIsNone(DelegationJobs(self.project.root).reservation())
        # Handback releases the helper-role fence.
        with lock(role_exclusion(self.project.root, 'helper'), blocking=False):
            pass

    def test_process_loss_keeps_reservation_and_never_relaunches(self):
        """A killed owner leaves the durable claim held and join stays pending."""
        source = self.file.read_text().splitlines()
        in_worker = False
        for index, line in enumerate(source):
            if line.startswith('['):
                in_worker = line.strip() == '[roles.worker]'
            if in_worker and line.startswith('capabilities = '):
                grants = json.loads(line.split('=', 1)[1].strip())
                source[index] = 'capabilities = ' + json.dumps(
                    [*grants, 'delegate', 'delegate_join'])
            if in_worker and line.startswith('# tools_per_run'):
                source[index] = 'tools_per_run = 2000'
        config_file = self.root / 'config' / 'process-loss.toml'
        config_file.write_text('\n'.join(source) + '''
[roles.helper]
profile = "alternate"
policy = ["policies/shared.md", "policies/searcher.md"]
workspace = "read"
capabilities = ["files", "read", "finish"]
''')
        config = load(config_file)
        project = Project(config, 'sample')
        marker = self.root / 'process-loss-markers'
        marker.mkdir()
        child = '''
import json, sys, time
from pathlib import Path
from mizu.config import load
from mizu.delegation_jobs import canonical, digest
from mizu.project import Project
from mizu.runtime import Engine

config_file, marker_dir = sys.argv[1], Path(sys.argv[2])
config = load(Path(config_file))
project = Project(config, 'sample')
task = 'Implement the focused slice'

class Driver:
    requires_sandbox = False

    def execute(self, ctx, prompt, *, profile=None):
        if ctx.role.name == 'helper':
            (marker_dir / 'child').write_text(ctx.run_dir.name)
            time.sleep(30)
            return
        accepted = ctx.handle('delegate', {'role': 'helper', 'task': task,
                                           'async_mode': True})
        snap = ctx.snapshot
        key = digest(canonical([ctx.run_dir.name, 'helper', task, snap['id']]))[:32]
        (marker_dir / 'accepted').write_text(json.dumps({'accepted': accepted, 'key': key}))
        time.sleep(30)

Engine(config, driver=Driver()).run(project, 'worker')
'''
        environment = dict(os.environ)
        source_root = Path(__file__).resolve().parents[1]
        environment['PYTHONPATH'] = os.pathsep.join(
            [str(source_root / 'tests'), str(source_root / 'src')])
        process = subprocess.Popen([sys.executable, '-c', child, str(config_file), str(marker)],
                                   env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            for _ in range(250):
                if (marker / 'accepted').exists() and (marker / 'child').exists():
                    break
                time.sleep(.02)
            else:
                self.fail('the owner process did not start an async child')
            accepted_info = json.loads((marker / 'accepted').read_text())
            accepted = accepted_info['accepted']
            before = {path.name for path in (project.root / 'runs').iterdir()
                      if (path / 'started.json').exists()}
            process.kill()
            process.wait(timeout=10)
            store = DelegationJobs(project.root)
            reservation = store.reservation()
            self.assertIsNotNone(reservation)
            self.assertEqual(reservation['id'], accepted['id'])
            self.assertEqual(reservation['run_id'], accepted['run'])
            self.assertEqual(reservation['status'], 'running')
            after = {path.name for path in (project.root / 'runs').iterdir()
                     if (path / 'started.json').exists()}
            self.assertEqual(after, before)
            fresh = self.context('worker', reservation_view=DelegationJobs.reservation_projection(
                project.root))
            retry = delegation_async.start(Engine(config), fresh, accepted_info['key'],
                                           'helper', 'Implement the focused slice',
                                           project.snapshots.get())
            self.assertEqual(retry['id'], accepted['id'])
            self.assertEqual(retry['run'], accepted['run'])
            pending = delegation_async.join(fresh, accepted['id'])
            self.assertFalse(pending.get('result_available', False))
            self.assertEqual(pending['status'], 'running')
            self.assertEqual(pending['run'], accepted['run'])
            self.assertEqual(after, {path.name for path in (project.root / 'runs').iterdir()
                                     if (path / 'started.json').exists()})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stderr is not None:
                process.stderr.close()

    def test_missing_lease_and_wrong_owner_are_typed_refusals(self):
        engine = Engine(self.config)
        ctx = self.context()
        request = {'role': 'helper', 'task': self.task, 'profile': None,
                   'source_snapshot': self.project.snapshots.get()['id']}
        with self.assertRaisesRegex(Denied, 'Missing admitted workspace lease'):
            engine._reserve_delegation(ctx, 'first', request)
        store = DelegationJobs(self.project.root)
        job = store.start('worker', 'first', request)
        with self.assertRaisesRegex(Denied, 'unavailable to owner'):
            store.status('helper', job['id'])
        with self.assertRaisesRegex(Denied, 'unavailable to owner'):
            delegation_async.join(self.context('searcher'), job['id'])

    def test_failed_launch_seals_reservation_and_leaves_no_run_directory(self):
        seen = {}
        runs = self.project.root / 'runs'
        runs.mkdir(exist_ok=True)

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'not used', 'state': 'x'})
                return
            before = {path.name for path in runs.iterdir()}
            with patch.object(delegation_async, '_gate', side_effect=Denied('unsafe child role')):
                with self.assertRaisesRegex(Denied, 'unsafe child role'):
                    ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                            'async_mode': True})
            seen['before'], seen['after'] = before, {path.name for path in runs.iterdir()}
            seen['gate'] = ctx._reservation_view

        result = self.run_parent(work)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(seen['after'], seen['before'])
        self.assertIsNone(seen['gate'])
        store = DelegationJobs(self.project.root)
        self.assertIsNone(store.reservation())

    def test_missing_quiescence_proof_keeps_the_reservation_held(self):
        release = threading.Event()
        seen = {}

        def work(ctx, *_):
            if ctx.role.name == 'helper':
                release.wait(5)
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'Child', 'state': 'x'})
                return
            accepted = ctx.handle('delegate', {'role': 'helper', 'task': self.task,
                                               'async_mode': True})
            with patch.object(delegation_async._Child, 'proof',
                              return_value={'thread_done': True, 'drivers_quiescent': False,
                                            'process_groups_absent': True,
                                            'no_external_resources': True}):
                release.set()
                for _ in range(150):
                    status = ctx.handle('delegate_join', {'job_id': accepted['id']})
                    if 'result_available' not in status:
                        break
                    time.sleep(.01)
            seen['status'] = status
            # The parent cannot finish while the failed handback holds the tree.
            with self.assertRaisesRegex(Denied, 'unavailable while the workspace is reserved'):
                ctx.handle('finish', {'outcome': 'wait', 'summary': 'cannot seal', 'state': 'x'})

        with self.assertRaisesRegex(Denied, 'unavailable while the workspace is reserved'):
            self.run_parent(work)
        self.assertEqual(seen['status']['status'], 'running')
        self.assertIn('run', seen['status'])
        self.assertIsNotNone(DelegationJobs(self.project.root).reservation())


if __name__ == '__main__':
    raise SystemExit(unittest.main())
