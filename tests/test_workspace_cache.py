"""Workspace caches: eligibility before deletion, nonblocking quiescence."""
import contextlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from unittest import mock

from support import Fixture, ScriptDriver
from mizu import check_jobs, workspace_cache as cache
from mizu.admission import check_slot_exclusions, role_exclusion, role_exclusions
from mizu.config import WorkspaceCache, load, workspace_cache_policy
from mizu.errors import Busy, ConfigError
from mizu.fs import held, lock, write_json
from mizu.process import Result, run
from mizu.runtime import Engine


class WorkspaceCacheTests(Fixture):
    def setUp(self):
        super().setUp()
        self.workspace = self.project.workspace
        # Ordinary disposable index: plain imports initially have no Git index.
        self.git('init')
        self.git('add', 'app.py')
        self.policy()
        self.state = self.project.root / 'maintenance/workspace-cache.json'

    def git(self, *args):
        result = run(['git', '-C', str(self.workspace), *args], timeout=60, maximum=65536)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result

    def policy(self, roots=('__pycache__',), maximum=10, interval=100):
        object.__setattr__(self.config, 'workspace_cache', WorkspaceCache(roots, maximum, interval))

    def make(self, name='__pycache__', size=10):
        directory = self.workspace / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'cache.bin').write_bytes(b'x' * size)
        return directory

    def test_disabled_does_not_inspect_or_lock(self):
        self.policy(roots=())
        with mock.patch.object(cache, 'lock', side_effect=AssertionError), \
                mock.patch.object(cache, '_preflight', side_effect=AssertionError):
            self.assertEqual(cache.maintain(self.project), {'status': 'disabled'})
        self.assertFalse(self.state.exists())

    def test_cadence_threshold_and_snapshot_identity(self):
        directory = self.make(size=9)
        before = self.project.snapshots.capture_files(self.workspace)['code_digest']
        with mock.patch.object(cache.time, 'time', return_value=1000):
            self.assertEqual(cache.maintain(self.project)['status'], 'below-threshold')
        (directory / 'more').write_bytes(b'xx')
        with mock.patch.object(cache.time, 'time', return_value=1099), \
                mock.patch.object(cache, '_preflight', side_effect=AssertionError):
            self.assertEqual(cache.maintain(self.project)['status'], 'not-due')
        with mock.patch.object(cache.time, 'time', return_value=1100):
            result = cache.maintain(self.project)
        self.assertEqual((result['status'], result['bytes'], result['removed_roots']), ('removed', 11, 1))
        self.assertFalse(directory.exists())
        self.assertTrue((self.workspace / 'app.py').exists())
        self.assertEqual(self.project.snapshots.capture_files(self.workspace)['code_digest'], before)
        self.assertEqual(json.loads(self.state.read_text()), result)

    def test_aggregate_threshold_and_missing_roots(self):
        self.policy(('__pycache__', '.pytest_cache', 'missing/__pycache__'))
        first, second = self.make(size=4), self.make('.pytest_cache', size=6)
        self.assertEqual(cache.maintain(self.project)['removed_roots'], 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_tracked_and_nonexcluded_refused_before_any_deletion(self):
        for unsafe in ('source-cache', '.pytest_cache', 'refs/remotes/upstream/cache'):
            with self.subTest(root=unsafe):
                safe, bad = self.make(), self.make(unsafe)
                if unsafe == '.pytest_cache':
                    self.git('add', '-f', unsafe)
                self.policy(('__pycache__', unsafe))
                self.state.unlink(missing_ok=True)
                self.assertEqual(cache.maintain(self.project)['status'], 'refused')
                self.assertTrue(safe.exists())
                self.assertTrue(bad.exists())

    def test_snapshot_private_state_inside_cache_is_refused(self):
        from mizu.startup import HOST_STATE_FILES
        protected = ('private-validation', '.env.local', '.ENV.dev', 'credentials.env',
                     sorted(HOST_STATE_FILES)[0])
        for name in protected:
            with self.subTest(name=name):
                root = self.make(size=10)
                child = root / name
                if name == 'private-validation':
                    child.mkdir()
                    (child / 'receipt.json').write_text('private')
                else:
                    child.write_text('private')
                self.policy(('__pycache__',))
                self.state.unlink(missing_ok=True)
                self.assertEqual(cache.maintain(self.project)['status'], 'refused')
                self.assertTrue(root.exists())
                self.assertTrue(child.exists())
                shutil.rmtree(root)

    def test_nested_repositories_and_tracked_ancestors_are_refused(self):
        directory = self.make()
        (directory / '.git').mkdir()
        self.assertEqual(cache.maintain(self.project)['status'], 'refused')
        self.assertTrue(directory.exists())
        self.state.unlink()
        # A gitlink's indexed path is an ancestor, not a cached file name.
        self.git('update-index', '--add', '--cacheinfo',
                 '160000,' + 'a' * 40 + ',vendor')
        self.make('vendor/__pycache__')
        self.policy(('vendor/__pycache__',))
        self.assertEqual(cache.maintain(self.project)['status'], 'refused')

    def test_case_alias_of_tracked_path_is_refused(self):
        self.make('CACHE')
        self.git('add', '-f', 'CACHE')
        self.make('cache')
        object.__setattr__(self.project.snapshots, 'excludes', (*self.config.exclude, 'cache'))
        self.policy(('cache',))
        self.assertEqual(cache.maintain(self.project)['status'], 'refused')

    def test_symlinks_in_root_ancestor_and_descendants_are_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep').write_bytes(b'keep')
        for where in ('root', 'ancestor', 'descendant'):
            with self.subTest(where=where):
                name = where + '/__pycache__'
                root = self.workspace / where
                root.mkdir()
                try:
                    if where == 'root':
                        (root / '__pycache__').symlink_to(outside, target_is_directory=True)
                    elif where == 'ancestor':
                        root.rmdir()
                        root.symlink_to(outside, target_is_directory=True)
                    else:
                        directory = self.make(name)
                        (directory / 'link').symlink_to(outside, target_is_directory=True)
                except OSError:
                    self.skipTest('Symlinks unavailable')
                self.policy((name,))
                self.state.unlink(missing_ok=True)
                self.assertEqual(cache.maintain(self.project)['status'], 'refused')
                self.assertEqual((outside / 'keep').read_bytes(), b'keep')

    def test_hardlinks_and_special_files_are_refused(self):
        directory = self.make()
        try:
            os.link(directory / 'cache.bin', directory / 'linked')
        except OSError:
            self.skipTest('Hardlinks unavailable')
        self.assertEqual(cache.maintain(self.project)['status'], 'refused')
        (directory / 'linked').unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(directory / 'pipe')
            self.state.unlink()
            self.assertEqual(cache.maintain(self.project)['status'], 'refused')

    def test_linked_worktree_index_is_supported(self):
        self.git('config', 'user.name', 'Cache Fixture')
        self.git('config', 'user.email', 'cache@example.invalid')
        self.git('add', 'app.py')
        self.git('commit', '-m', 'fixture')
        linked = self.root / 'linked-workspace'
        self.git('worktree', 'add', '--detach', str(linked))
        self.project.workspace = linked
        self.workspace = linked
        directory = self.make(size=10)
        self.assertTrue((linked / '.git').is_file())
        self.assertEqual(cache.maintain(self.project)['status'], 'removed')
        self.assertFalse(directory.exists())
        self.assertTrue((linked / 'app.py').exists())

    def test_plain_workspace_unreadable_git_and_scan_bounds_refuse(self):
        directory = self.make()
        for patch in (
            mock.patch.object(cache, 'run', return_value=Result(1, '', '', 'exited', 0)),
            mock.patch.object(cache, 'run', return_value=Result(0, '', '', 'output_limit', 0)),
            mock.patch.object(cache, 'MAX_ENTRIES', 0),
            mock.patch.object(cache, 'MAX_DEPTH', -1),
            mock.patch.object(cache, 'SCAN_SECONDS', -1),
        ):
            self.state.unlink(missing_ok=True)
            with patch:
                self.assertEqual(cache.maintain(self.project)['status'], 'refused')
            self.assertTrue(directory.exists())
        # Rename instead of deleting Git's read-only object files on Windows.
        (self.workspace / '.git').rename(self.workspace / 'unused-git-metadata')
        self.state.unlink()
        self.assertEqual(cache.maintain(self.project)['status'], 'refused')

    def exclusions(self):
        return sorted(set(role_exclusions(self.project.root, tuple(self.config.roles))
                          + check_slot_exclusions(self.project.root, self.config.limits.parallel_checks)))

    def test_busy_exclusions_roll_back_without_consuming_cadence(self):
        directory = self.make()
        extra = role_exclusion(self.project.root, 'retired-child')
        with lock(extra):
            pass  # Existing but no longer configured must still be covered.
        paths = [self.project.root / 'locks/workspace.lock', *self.exclusions(),
                 self.project.root / 'locks/check-cache.lock']
        for busy in paths:
            with self.subTest(busy=busy.name), lock(busy):
                with mock.patch.object(cache, '_preflight', side_effect=AssertionError):
                    self.assertEqual(cache.maintain(self.project)['status'], 'busy')
                for other in paths:
                    if other != busy:
                        with lock(other, blocking=False):
                            pass
                self.assertTrue(directory.exists())
                self.assertFalse(self.state.exists())
        self.assertEqual(cache.maintain(self.project)['status'], 'removed')

    def test_all_locks_held_in_fixed_order_prevent_new_entrants(self):
        self.make()
        order = []
        original = cache.lock
        @contextlib.contextmanager
        def observe(path, *, blocking=True):
            self.assertFalse(blocking)
            with original(path, blocking=blocking):
                order.append(path)
                yield
        expected = [self.project.root / 'locks/workspace.lock', *self.exclusions(),
                    self.project.root / 'locks/check-cache.lock']
        preflight = cache._preflight
        def inspect(*args):
            self.assertEqual(order, expected)
            for path in expected:
                self.assertTrue(held(path))
                with self.assertRaises(Busy), original(path, blocking=False):
                    pass
            child = run([sys.executable, '-c', """
import sys
from pathlib import Path
from mizu.errors import Busy
from mizu.fs import lock
for path in sys.argv[1:]:
    try:
        with lock(Path(path), blocking=False):
            raise AssertionError('Maintenance did not exclude a new entrant')
    except Busy:
        pass
print('excluded')
""", *map(str, expected)], timeout=60, maximum=4096,
                        env={**os.environ, 'PYTHONPATH': str(Path(cache.__file__).parents[1])})
            self.assertEqual(child.exit_code, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), 'excluded')
            return preflight(*args)
        remove = cache.shutil.rmtree
        def guarded_remove(path):
            self.assertTrue(all(held(exclusion) for exclusion in expected))
            return remove(path)
        with mock.patch.object(cache, 'lock', observe), \
                mock.patch.object(cache, '_preflight', inspect), \
                mock.patch.object(cache.shutil, 'rmtree', guarded_remove):
            self.assertEqual(cache.maintain(self.project)['status'], 'removed')
        for path in expected:
            self.assertFalse(held(path))

    def test_pause_drain_control_never_read_or_mutated(self):
        control = self.project.root / 'control.json'
        for paused, draining in ((False, False), (True, False), (False, True), (True, True)):
            self.make()
            self.state.unlink(missing_ok=True)
            self.project.set_control(paused=paused, draining=draining)
            before = control.read_bytes()
            with mock.patch.object(self.project, 'control', side_effect=AssertionError), \
                    mock.patch.object(self.project, 'set_control', side_effect=AssertionError):
                self.assertEqual(cache.maintain(self.project)['status'], 'removed')
            self.assertEqual(control.read_bytes(), before)

    def test_bad_cadence_record_fails_closed(self):
        self.make()
        for value in (None, {}, {'checked_at': True}, {'checked_at': 2**63}, 'bad', 'x' * 4096):
            write_json(self.state, value)
            with mock.patch.object(cache, '_preflight', side_effect=AssertionError):
                self.assertEqual(cache.maintain(self.project)['status'], 'unavailable')

    def test_runtime_and_check_runner_call_maintenance_before_locks(self):
        def inspect(project):
            self.assertIs(project, self.project)
            self.assertFalse(any(held(path) for path in self.exclusions()))
            self.assertFalse(held(project.root / 'locks/workspace.lock'))
        with mock.patch.object(cache, 'maintain', side_effect=inspect) as maintenance:
            Engine(self.config, driver=ScriptDriver()).run(self.project, 'reporter')
            self.assertEqual(maintenance.call_count, 1)
            maintenance.reset_mock()
            check_jobs.run_one(self.project, threading.Event())
            self.assertEqual(maintenance.call_count, 1)

    def test_policy_load_and_validation(self):
        self.assertEqual(workspace_cache_policy({}), WorkspaceCache())
        text = self.file.read_text()
        self.file.write_text(text.replace('roots = []', 'roots = ["__pycache__"]'))
        self.assertEqual(load(self.file).workspace_cache.roots, ('__pycache__',))
        for raw in (
            {'roots': ['../cache']}, {'roots': ['/cache']}, {'roots': ['a/./b']},
            {'roots': ['a//b']}, {'roots': ['a\\b']}, {'roots': ['.git']},
            {'roots': ['x/.GIT']}, {'roots': ['']}, {'roots': ['a', 'a/b']},
            {'roots': ['a', 'a']}, {'roots': ['cache', 'CACHE/sub']}, {'roots': ['a'] * 33}, {'roots': 'cache'},
            {'max_bytes': 0}, {'max_bytes': True}, {'max_bytes': 2**63},
            {'check_interval_seconds': 0}, {'check_interval_seconds': 31 * 86400 + 1},
            {'check_interval_seconds': 1.5}, {'unknown': True},
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                workspace_cache_policy(raw)
