"""A stopped git-tree import is a host bound, not a dirty-tree verdict."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu import project as lifecycle
from mizu.config import ProjectImport
from mizu.errors import Denied
from mizu.process import Result

POLICY = ProjectImport(status_timeout_seconds=7, clone_timeout_seconds=11,
                       local_timeout_seconds=3)


class GitImportTimeoutTests(Fixture):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mizu-import-')
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source'
        self.workspace = Path(self.temporary.name) / 'workspace'
        self.source.mkdir()

    def import_result(self, *results):
        runner = Mock(side_effect=results)
        with patch.object(lifecycle.shutil, 'which', return_value='/usr/bin/git'), \
             patch.object(lifecycle, 'run', runner):
            with self.assertRaises(Denied) as raised:
                lifecycle._import_git_tree(self.source, self.workspace, {}, POLICY)
        return str(raised.exception), runner

    def test_status_timeout_names_reason_and_configured_bound(self):
        message, runner = self.import_result(
            Result(None, '', '', 'timeout', 7.0))
        self.assertIn('status check stopped at the 7s time bound after 7.0s (timeout)', message)
        self.assertNotIn('clean source tree', message)
        self.assertEqual(runner.call_args.kwargs['timeout'], 7)

    def test_status_output_limit_is_not_a_dirty_tree_verdict(self):
        message, _ = self.import_result(
            Result(None, '', '', 'output_limit', 0.2))
        self.assertIn('status check stopped after 0.2s (output_limit)', message)
        self.assertNotIn('clean source tree', message)

    def test_clone_timeout_names_reason_and_configured_bound(self):
        message, runner = self.import_result(
            Result(0, '', '', 'exited', 0.1),
            Result(None, '', '', 'timeout', 11.0))
        self.assertIn('clone stopped at the 11s time bound after 11.0s (timeout)', message)
        self.assertNotIn('Repository import failed: ', message)
        self.assertEqual(runner.call_args_list[1].kwargs['timeout'], 11)

    def test_status_nonzero_exit_keeps_stderr_not_dirty_tree_text(self):
        message, _ = self.import_result(
            Result(1, '', 'fatal: not a git repository', 'exited', 0.1))
        self.assertIn('Git import status check failed: fatal: not a git repository', message)
        self.assertNotIn('clean source tree', message)

    def test_clone_nonzero_exit_keeps_stderr(self):
        message, _ = self.import_result(
            Result(0, '', '', 'exited', 0.1),
            Result(128, '', 'fatal: could not clone source', 'exited', 0.1))
        self.assertIn('Repository import failed: fatal: could not clone source', message)

    def test_local_config_timeout_names_reason_and_configured_bound(self):
        message, runner = self.import_result(
            Result(0, '', '', 'exited', 0.1),
            Result(0, '', '', 'exited', 0.1),
            Result(0, '', '', 'exited', 0.1),
            Result(None, '', '', 'timeout', 3.0))
        self.assertIn('git config stopped at the 3s time bound after 3.0s (timeout)', message)
        self.assertEqual(runner.call_args_list[3].kwargs['timeout'], 3)

    def test_origin_removal_failure_aborts_import(self):
        message, _ = self.import_result(
            Result(0, '', '', 'exited', 0.1),
            Result(0, '', '', 'exited', 0.1),
            Result(1, '', "error: could not remove 'origin'", 'exited', 0.1))
        self.assertIn("Cannot remove the imported origin remote: error: could not remove 'origin'",
                      message)


if __name__ == '__main__':
    unittest.main()
