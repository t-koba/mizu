"""Managed import fencing with real workspace locks; no child runner claims."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import re
import sys
from collections import deque
import shutil
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from support import Fixture
from mizu import fs as fs_module
from mizu import project as lifecycle
from mizu import snapshot as snapshot_module
from mizu.delegation_jobs import DelegationJobs
from mizu.errors import Denied
from mizu.fs import held, lock
from mizu.process import run


_IMPORT_DIAGNOSTIC_FIXED_TARGETS = frozenset({
    '<redacted>', 'PROJECT.md', 'control.json', 'current.json',
    'locks/control.lock', 'locks/publication.lock', 'locks/workspace.lock',
    'project.toml', 'spool/editor/.mizu-outbox', 'workspace/app.py',
})
_IMPORT_DIAGNOSTIC_TARGET_PATTERNS = tuple(re.compile(pattern, re.ASCII) for pattern in (
    r'histories/[0-9a-f]{64}\.json',
    r'objects/[0-9a-f]{64}',
    r'snapshots/[0-9a-f]{64}\.json',
))


def _sanitize_import_target(target):
    """Keep only the known synthetic fixture paths; never serialize arbitrary names."""
    if isinstance(target, str) and (target in _IMPORT_DIAGNOSTIC_FIXED_TARGETS or
                                    any(pattern.fullmatch(target)
                                        for pattern in _IMPORT_DIAGNOSTIC_TARGET_PATTERNS)):
        return target
    return '<redacted>'


def _fixture_import_target(root, name, project_root, workspace_root):
    """Map a captured read to an allowlisted path relative to the test project."""
    if not isinstance(name, str):
        return '<redacted>'
    try:
        root = Path(root)
        project_root = Path(project_root)
        workspace_root = Path(workspace_root)
        if root == workspace_root:
            target = f'workspace/{name}'
        elif root == project_root:
            target = name
        else:
            return '<redacted>'
    except Exception:
        return '<redacted>'
    return _sanitize_import_target(target)


def _has_oserror(exc):
    """Recognize the safe-read wrapper's OSError cause without formatting it."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError):
            return True
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return False


def _import_failure_json(exc, *, target='<redacted>'):
    """Test-only allowlist: <=4 exceptions, <=6 frames each, no exception text.

    Names are ASCII tokens (48 characters max); targets are known fixture-
    relative paths only; filenames are basenames. Never format an exception
    or traceback: those include messages, source lines and absolute paths.
    """
    def token(value, pattern):
        return value if re.fullmatch(pattern, value, flags=re.ASCII) else '<redacted>'

    chain = []
    while exc is not None and len(chain) < 4:
        frames = deque(maxlen=6)
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            basename = code.co_filename.replace('\\', '/').rsplit('/', 1)[-1]
            frames.append({
                'file': token(basename, r'[A-Za-z_][A-Za-z0-9_]{0,44}\.py'),
                'function': token(code.co_name, r'(?:[A-Za-z_][A-Za-z0-9_]{0,47}|<module>)'),
                'line': min(max(tb.tb_lineno, 0), 2**31 - 1),
            })
            tb = tb.tb_next
        entry = {'type': token(type(exc).__name__, r'[A-Za-z_][A-Za-z0-9_]{0,47}'),
                 'frames': list(frames)}
        if isinstance(exc, OSError):
            for key in ('errno', 'winerror'):
                value = getattr(exc, key, None)
                if type(value) is int and -(2**31) <= value < 2**31:
                    entry[key] = value
        chain.append(entry)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return json.dumps({'operation': 'managed-import',
                       'target': _sanitize_import_target(target),
                       'chain': chain}, ensure_ascii=True, separators=(',', ':'))


def _diagnosed_managed_import(import_source, *, project_root=None, workspace_root=None):
    """Used only at the known managed-import call; never diagnose external input."""
    target = '<redacted>'
    original_read = snapshot_module.safe_read_info

    def tracked_read(root, name, maximum):
        nonlocal target
        try:
            return original_read(root, name, maximum)
        except Exception as exc:
            if _has_oserror(exc) and project_root is not None and workspace_root is not None:
                target = _fixture_import_target(root, name, project_root, workspace_root)
            raise

    def invoke():
        if project_root is None or workspace_root is None:
            return import_source()
        # safe_read resolves fs.safe_read_info dynamically; SnapshotStore holds
        # its own imported alias, so both test-only boundaries must be wrapped.
        with patch.object(fs_module, 'safe_read_info', side_effect=tracked_read), \
             patch.object(snapshot_module, 'safe_read_info', side_effect=tracked_read):
            return import_source()

    try:
        return invoke()
    except Exception as exc:
        if sys.platform == 'win32':
            try:
                payload = _import_failure_json(exc, target=target).encode('ascii')
                if len(payload) <= 8192:
                    # Parent-owned scratch path, not an artifact. No stderr output.
                    path = os.environ['MIZU_PROJECT_IMPORT_DIAGNOSTIC']
                    with open(path, 'xb') as stream:
                        stream.write(payload)
            except Exception:
                pass  # Diagnostics must never replace the original failure.
        raise


class ProjectImportDiagnosticTests(unittest.TestCase):
    def test_sanitized_failure_canary(self):
        # Exercise the parent's actual scratch/export boundary with synthetic
        # secrets only. This fails outside safe_read, unlike the old canary.
        from test_check_offline import CHECK
        failure = Denied('DENIED_CANARY')
        namespace = {'failure': failure}
        exec(compile("def fail():\n"
                     "    try:\n"
                     "        error = OSError(13, 'MESSAGE_CANARY', 'FILE_CANARY')\n"
                     "        error.winerror = 5\n"
                     "        raise error\n"
                     "    except OSError as cause:\n"
                     "        raise failure from cause\n",
                     r'C:\PATH_CANARY\canary.py', 'exec'), namespace)
        captured = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(CHECK, 'ROOT', Path(temporary)), \
             patch.object(sys, 'platform', 'win32'), \
             patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40}), \
             contextlib.redirect_stderr(captured):
            with self.assertRaises(Denied) as raised:
                with CHECK.project_import_diagnostic(os.environ, Path(temporary), 'b' * 64):
                    _diagnosed_managed_import(namespace['fail'])
            output = (Path(temporary) / 'private-validation/project-import-diagnostic.json').read_text()
        self.assertIs(raised.exception, failure)
        self.assertEqual(captured.getvalue(), '')
        self.assertLessEqual(len(output.encode('ascii')), 8192)
        for secret in ('MESSAGE_CANARY', 'FILE_CANARY', 'PATH_CANARY', 'DENIED_CANARY'):
            self.assertNotIn(secret, output)
        data = json.loads(output)
        self.assertEqual(set(data), {'operation', 'target', 'chain', 'source_sha',
                                     'source_manifest_sha256', 'python'})
        self.assertEqual((data['operation'], data['target']), ('managed-import', '<redacted>'))
        self.assertEqual((data['source_sha'], data['source_manifest_sha256']), ('a' * 40, 'b' * 64))
        self.assertEqual(data['python'], '.'.join(map(str, sys.version_info[:3])))
        denied, error = data['chain']
        self.assertEqual(set(denied), {'type', 'frames'})
        self.assertEqual(set(error), {'type', 'frames', 'errno', 'winerror'})
        self.assertEqual(denied['type'], 'Denied')
        self.assertEqual((error['type'], error['errno'], error['winerror']), ('PermissionError', 13, 5))
        self.assertEqual(error['frames'], [{'file': 'canary.py', 'function': 'fail', 'line': 5}])
        self.assertIs(raised.exception.__cause__.__traceback__.tb_frame.f_code,
                      namespace['fail'].__code__)

    def test_failed_safe_read_exports_only_fixture_relative_target(self):
        from test_check_offline import CHECK
        failure = Denied('DENIED_CANARY')
        failure.__cause__ = PermissionError(13, 'MESSAGE_CANARY', 'FILE_CANARY')
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(CHECK, 'ROOT', Path(temporary)), \
             patch.object(sys, 'platform', 'win32'), \
             patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40}):
            root = Path(temporary)
            project_root = root / 'fixture-project'
            workspace_root = project_root / 'workspace'
            with patch.object(fs_module, '_safe_read_openat', side_effect=failure), \
                 patch.object(fs_module, '_safe_read_portable', side_effect=failure):
                with self.assertRaises(Denied) as raised:
                    with CHECK.project_import_diagnostic(os.environ, root, 'b' * 64):
                        _diagnosed_managed_import(
                            lambda: snapshot_module.safe_read_info(workspace_root, 'app.py', 1024),
                            project_root=project_root, workspace_root=workspace_root)
            output = (root / 'private-validation/project-import-diagnostic.json').read_text()
        self.assertIs(raised.exception, failure)
        self.assertLessEqual(len(output.encode('ascii')), 8192)
        self.assertEqual(json.loads(output)['target'], 'workspace/app.py')
        for secret in ('MESSAGE_CANARY', 'FILE_CANARY', 'DENIED_CANARY', str(project_root)):
            self.assertNotIn(secret, output)

    def test_chain_and_frame_bounds(self):
        def fail(depth):
            if depth:
                fail(depth - 1)
            raise Denied('Not serialized')

        try:
            fail(12)
        except Denied as exc:
            exc.__cause__ = exc  # Even cyclic chains are bounded.
            output = _import_failure_json(exc)
        chain = json.loads(output)['chain']
        self.assertEqual(len(chain), 4)
        self.assertTrue(all(len(entry['frames']) == 6 for entry in chain))
        self.assertLessEqual(len(output.encode('ascii')), 8192)

    def test_success_non_windows_and_capture_failure_preserve_outcome(self):
        failure = RuntimeError('Synthetic failure')
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / 'capture.json'
            with patch.dict(os.environ, {'MIZU_PROJECT_IMPORT_DIAGNOSTIC': str(sidecar)}):
                with patch.object(sys, 'platform', 'win32'):
                    self.assertEqual(_diagnosed_managed_import(lambda: 'ok'), 'ok')
                with patch.object(sys, 'platform', 'linux'):
                    with self.assertRaises(RuntimeError) as raised:
                        _diagnosed_managed_import(Mock(side_effect=failure))
                self.assertIs(raised.exception, failure)
                self.assertFalse(sidecar.exists())
                with patch.object(sys, 'platform', 'win32'), \
                     patch('builtins.open', side_effect=OSError('Synthetic write failure')):
                    with self.assertRaises(RuntimeError) as raised:
                        _diagnosed_managed_import(Mock(side_effect=failure))
                self.assertIs(raised.exception, failure)


class ProjectImportReservationTests(Fixture):
    def import_source(self, source=None, name='imported'):
        return lifecycle.initialize(self.config, name,
                                    source if source is not None else self.project.workspace,
                                    self.goal, ['worker'], [])

    def start(self, store):
        # Trusted start lock order, not a child runner or runtime integration.
        with lock(self.project.root / 'locks/workspace.lock'):
            return store.start('worker', 'first', {
                'role': 'helper', 'task': 'Import fence test', 'profile': None,
                'source_snapshot': 'a' * 64})

    def assert_unpublished(self):
        self.assertFalse((self.project.root.parent / 'imported').exists())
        self.assertEqual(list(self.project.root.parent.glob('.init-*')), [])

    def denied_import(self, source=None):
        with patch.object(lifecycle, '_import_plain_directory') as plain, \
             patch.object(lifecycle, '_import_git_tree') as git:
            with self.assertRaises(Denied):
                self.import_source(source)
            plain.assert_not_called()
            git.assert_not_called()
        self.assert_unpublished()

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')

    def test_active_reservation_denies_before_capture_in_all_held_phases(self):
        store = DelegationJobs(self.project.root)
        self.start(store)
        reservation = store.reservation()
        binding = reservation['id'], reservation['token']
        for phase in ('queued', 'running', 'cancel-requested', 'finished'):
            with self.subTest(phase=phase):
                if phase == 'running':
                    store.runner_started(*binding)
                elif phase == 'cancel-requested':
                    store.cancel('worker', binding[0])
                elif phase == 'finished':
                    store.runner_finished(*binding, 'cancelled')
                for source in (self.project.workspace, self.project.root):
                    self.denied_import(source)
        # Terminal ledger entries permit imports without rewriting the store.
        store.runner_quiescent(*binding, 'Fixture state only; no child launched')
        before = (store.root / 'state.json').read_bytes()
        self.import_source()
        self.assertEqual((store.root / 'state.json').read_bytes(), before)

    def test_incomplete_corrupt_and_unreadable_store_refuse_without_repair(self):
        root = self.project.root / 'delegation-jobs'
        root.mkdir()
        for source in (self.project.workspace, self.project.root):
            self.denied_import(source)
        self.assertEqual(list(root.iterdir()), [])
        (root / 'store.lock').touch()
        for content in (None, b'{', b'{}', b'{"version":1,"active":null,"jobs":{}}'):
            with self.subTest(content=content):
                if content is not None:
                    (root / 'state.json').write_bytes(content)
                for source in (self.project.workspace, self.project.root):
                    self.denied_import(source)
                if content is not None:
                    self.assertEqual((root / 'state.json').read_bytes(), content)
        with patch('mizu.delegation_jobs.safe_read', side_effect=PermissionError):
            for source in (self.project.workspace, self.project.root):
                self.denied_import(source)

    def test_unsafe_store_symlink_refuses(self):
        store = DelegationJobs(self.project.root)
        state = store.root / 'state.json'
        saved = self.root / 'saved-state.json'
        state.rename(saved)
        self.symlink(state, saved)
        self.denied_import()
        self.denied_import(self.project.root)

    def test_absent_store_imports_without_initializing_and_external_is_unchanged(self):
        real_guard = DelegationJobs.guard
        def guarded(root):
            self.assertTrue(held(root / 'locks/workspace.lock'))
            return real_guard(root)

        with patch.object(DelegationJobs, '__init__', side_effect=AssertionError('Store initialization')), \
             patch.object(DelegationJobs, 'guard', side_effect=guarded) as guard:
            imported = _diagnosed_managed_import(
                self.import_source, project_root=self.project.root,
                workspace_root=self.project.workspace)
            root_import = _diagnosed_managed_import(
                lambda: self.import_source(self.project.root, name='root-import'),
                project_root=self.project.root, workspace_root=self.project.workspace)
            self.assertEqual(guard.call_count, 2)
            guard.assert_called_with(self.project.root)
        self.assertFalse((self.project.root / 'delegation-jobs').exists())
        self.assertEqual((imported.workspace / 'app.py').read_bytes(), b'VALUE = 2\n')
        self.assertEqual((root_import.workspace / 'workspace/app.py').read_bytes(), b'VALUE = 2\n')
        self.start(DelegationJobs(self.project.root))
        with patch.object(DelegationJobs, 'guard', side_effect=AssertionError('External guard')):
            external = self.import_source(self.root / 'source', name='external')
        self.assertEqual((external.workspace / 'app.py').read_bytes(), b'VALUE = 2\n')

    def test_diagnostic_captures_managed_project_marker_read_target(self):
        from test_check_offline import CHECK
        failure = Denied('DENIED_CANARY')
        failure.__cause__ = PermissionError(13, 'MESSAGE_CANARY', 'FILE_CANARY')
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(CHECK, 'ROOT', Path(temporary)), \
             patch.object(sys, 'platform', 'win32'), \
             patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40}):
            root = Path(temporary)
            with patch.object(DelegationJobs, 'guard', return_value=None), \
                 patch.object(fs_module, '_safe_read_openat', side_effect=failure), \
                 patch.object(fs_module, '_safe_read_portable', side_effect=failure):
                with self.assertRaises(Denied) as raised:
                    with CHECK.project_import_diagnostic(os.environ, root, 'b' * 64):
                        _diagnosed_managed_import(
                            self.import_source, project_root=self.project.root,
                            workspace_root=self.project.workspace)
            output = (root / 'private-validation/project-import-diagnostic.json').read_text()
        self.assertIs(raised.exception, failure)
        data = json.loads(output)
        self.assertEqual(data['target'], 'project.toml')
        self.assertEqual([entry['type'] for entry in data['chain']],
                         ['Denied', 'PermissionError'])
        self.assertEqual(data['chain'][1]['errno'], 13)
        for secret in ('MESSAGE_CANARY', 'FILE_CANARY', 'DENIED_CANARY', str(self.root)):
            self.assertNotIn(secret, output)

    def test_non_workspace_project_subdirectory_does_not_become_root_import(self):
        store = DelegationJobs(self.project.root)
        self.start(store)
        notes = self.project.root / 'notes'
        notes.mkdir()
        (notes / 'README.txt').write_text('Not the live workspace.\n')
        imported = self.import_source(notes, name='notes-import')
        self.assertEqual((imported.workspace / 'README.txt').read_text(),
                         'Not the live workspace.\n')

    def test_root_aliases_and_resolved_equivalents_are_guarded(self):
        alias = self.root / 'root-alias'
        self.symlink(alias, self.project.root)
        imported = self.import_source(alias, name='root-alias-import')
        self.assertEqual((imported.workspace / 'workspace/app.py').read_bytes(), b'VALUE = 2\n')
        self.start(DelegationJobs(self.project.root))
        for source in (alias, self.project.root / 'locks/..'):
            self.denied_import(source)

    def test_collection_and_ancestor_sources_are_ambiguous(self):
        alias = self.root / 'collection-alias'
        self.symlink(alias, self.project.root.parent)
        self.import_source(self.root / 'source', name='second')
        # Refuse without scanning the collection or choosing one workspace lock.
        for source in (self.project.root.parent, self.config.data, self.root, alias,
                       self.project.root / '..'):
            with self.subTest(source=source.name), \
                 patch.object(DelegationJobs, 'guard', side_effect=AssertionError('No single guard')):
                self.denied_import(source)
        self.assertFalse((self.project.root / 'delegation-jobs').exists())

    def test_aliases_and_managed_subdirectories_bind_to_the_source_project(self):
        nested = self.project.workspace / 'nested'
        nested.mkdir()
        (nested / 'app.py').write_bytes(b'VALUE = 3\n')
        alias = self.root / 'alias'
        self.symlink(alias, self.project.workspace)
        imported = self.import_source(alias / 'nested', name='nested')
        self.assertEqual((imported.workspace / 'app.py').read_bytes(), b'VALUE = 3\n')
        self.start(DelegationJobs(self.project.root))
        for source in (nested, alias, alias / 'nested', alias / 'missing'):
            with self.subTest(source=source.name):
                self.denied_import(source)

    def test_managed_workspace_and_source_symlink_escapes_refuse(self):
        escape = self.project.workspace / 'escape'
        self.symlink(escape, self.root / 'source')
        self.denied_import(escape)
        # Even an external alias leading through that lexical escape is held.
        alias = self.root / 'alias'
        self.symlink(alias, self.project.workspace)
        self.denied_import(alias / 'escape')
        escape.unlink()
        saved = self.project.workspace.with_name('saved-workspace')
        self.project.workspace.rename(saved)
        self.symlink(self.project.workspace, self.root / 'source')
        self.denied_import()
        self.denied_import(self.project.root)

    def test_unknown_managed_workspace_never_falls_back_to_external(self):
        (self.project.root / 'project.toml').unlink()
        self.denied_import()
        self.denied_import(self.project.root)
        self.assertFalse((self.project.root / 'delegation-jobs').exists())

    def test_start_first_blocks_import_then_guard_denies_without_capture(self):
        self.start_first(self.project.workspace)

    def test_root_start_first_blocks_import_before_capture(self):
        self.start_first(self.project.root)

    def start_first(self, source):
        store = DelegationJobs(self.project.root)
        attempting = threading.Event()
        workspace_lock = self.project.root / 'locks/workspace.lock'

        @contextlib.contextmanager
        def observed_lock(path, **kwargs):
            if path == workspace_lock:
                attempting.set()
            with lock(path, **kwargs):
                yield

        with patch.object(lifecycle, 'lock', observed_lock), \
             patch.object(lifecycle, '_import_plain_directory') as capture, \
             ThreadPoolExecutor(max_workers=1) as pool:
            with lock(workspace_lock):
                future = pool.submit(self.import_source, source)
                self.assertTrue(attempting.wait(5), 'Import did not attempt workspace.lock')
                # Already holding the trusted start lock; do not reacquire it.
                store.start('worker', 'first', {
                    'role': 'helper', 'task': 'Start-first race', 'profile': None,
                    'source_snapshot': 'a' * 64})
                self.assertFalse(future.done())
                capture.assert_not_called()
            with self.assertRaises(Denied):
                future.result(timeout=10)
            capture.assert_not_called()
        self.assert_unpublished()

    def test_import_first_blocks_start_through_real_source_capture(self):
        self.import_first('_import_plain_directory')

    def test_root_import_first_blocks_start_through_recursive_capture(self):
        self.import_first('_import_plain_directory', self.project.root)

    @unittest.skipUnless(shutil.which('git'), 'Git unavailable')
    def test_import_first_blocks_start_through_real_git_clone(self):
        workspace = self.project.workspace
        env = lifecycle.git_env()
        for args in (['init'], ['add', '.'],
                     ['-c', 'user.name=Mizu', '-c', 'user.email=automation@localhost',
                      'commit', '-m', 'Create import fixture']):
            result = run(['git', '-C', str(workspace), *args],
                         env=env, timeout=30, maximum=8192)
            self.assertEqual(result.exit_code, 0, result.stderr)
        self.import_first('_import_git_tree')

    def import_first(self, operation, source=None):
        store = DelegationJobs(self.project.root)
        attempting, started = threading.Event(), threading.Event()
        original = getattr(lifecycle, operation)

        def start():
            attempting.set()
            result = self.start(store)
            started.set()
            return result

        with ThreadPoolExecutor(max_workers=1) as pool:
            futures = []

            def capture(*args):
                self.assertTrue(held(self.project.root / 'locks/workspace.lock'))
                futures.append(pool.submit(start))
                self.assertTrue(attempting.wait(5), 'Start did not attempt workspace.lock')
                self.assertFalse(started.is_set())
                self.assertIsNone(store.reservation())
                original(*args)
                self.assertFalse(started.is_set(), 'Start slipped in during capture')

            with patch.object(lifecycle, operation, side_effect=capture):
                imported = self.import_source(source)
            futures[0].result(timeout=10)
        self.assertTrue(started.is_set())
        relative = 'workspace/app.py' if source == self.project.root else 'app.py'
        self.assertEqual((imported.workspace / relative).read_bytes(), b'VALUE = 2\n')
