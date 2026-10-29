"""Managed import fencing with real workspace locks; no child runner claims."""
import contextlib
import shutil
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from support import Fixture
from mizu import project as lifecycle
from mizu.delegation_jobs import DelegationJobs
from mizu.errors import Denied
from mizu.fs import held, lock
from mizu.process import run


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
            imported = self.import_source()
            root_import = self.import_source(self.project.root, name='root-import')
            self.assertEqual(guard.call_count, 2)
            guard.assert_called_with(self.project.root)
        self.assertFalse((self.project.root / 'delegation-jobs').exists())
        self.assertEqual((imported.workspace / 'app.py').read_bytes(), b'VALUE = 2\n')
        self.assertEqual((root_import.workspace / 'workspace/app.py').read_bytes(), b'VALUE = 2\n')
        self.start(DelegationJobs(self.project.root))
        with patch.object(DelegationJobs, 'guard', side_effect=AssertionError('External guard')):
            external = self.import_source(self.root / 'source', name='external')
        self.assertEqual((external.workspace / 'app.py').read_bytes(), b'VALUE = 2\n')

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
