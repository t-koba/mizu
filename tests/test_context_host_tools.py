"""The extracted host sync/report handlers keep their gates, calls and records.

``Context`` inherits both methods from the owner module and its ``sync`` and
``report`` dispatch entries are the same method objects. These tests cover the
ownership plus the exact denial, adapter, receipt and staging contracts without
repeating the sync end-to-end suites.
"""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu.context_host_tools import ContextHostToolsMixin
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_host_tools"
SYNC = ContextHostToolsMixin._op_sync
REPORT = ContextHostToolsMixin._op_report
SHA = "a" * 40


def _context(*, workspace="write"):
    context = Mock()
    context.role.workspace = workspace
    context.config.vcs = {"command": ["vcs-adapter"]}
    context.project.root = Path("/project")
    context.tools_cancelled = "cancel-token"
    context.run_dir = Path("/run")
    return context


class ContextHostToolsDispatchTests(unittest.TestCase):
    def test_context_inherits_both_methods_and_dispatches_their_identity(self):
        self.assertTrue(issubclass(Context, ContextHostToolsMixin))
        self.assertIs(Context._op_sync, SYNC)
        self.assertIs(Context._op_report, REPORT)
        self.assertIs(Context._DISPATCH["sync"], SYNC)
        self.assertIs(Context._DISPATCH["report"], REPORT)
        self.assertEqual(SYNC.__module__, OWNER)
        self.assertEqual(REPORT.__module__, OWNER)
        self.assertNotIn("_op_sync", Context.__dict__)
        self.assertNotIn("_op_report", Context.__dict__)


class ContextSyncTests(Fixture):
    def test_read_only_role_is_denied_without_adapter_calls(self):
        context = self.context("reviewer")
        with patch(f"{OWNER}._vcs.fetch_refs") as fetch, \
                patch(f"{OWNER}._vcs.inject_refs") as inject, \
                self.assertRaises(Denied) as refused:
            Context._DISPATCH["sync"](context, {})
        self.assertEqual(str(refused.exception), "Sync requires a writable workspace")
        fetch.assert_not_called()
        inject.assert_not_called()

    def test_fetch_oserror_maps_to_the_exact_denial(self):
        context = _context()
        failure = OSError("fetch failed")
        with patch(f"{OWNER}._vcs.fetch_refs", side_effect=failure) as fetch, \
                patch(f"{OWNER}._vcs.inject_refs") as inject, \
                self.assertRaises(Denied) as refused:
            SYNC(context, {})
        self.assertEqual(str(refused.exception), "Upstream sync is unavailable: fetch failed")
        self.assertIs(refused.exception.__cause__, failure)
        fetch.assert_called_once_with(context.config.vcs, cancel="cancel-token")
        inject.assert_not_called()

    def test_inject_oserror_maps_to_the_exact_denial(self):
        context = _context()
        refs = {"main": SHA, "topic": "b" * 40}
        failure = OSError("inject failed")
        with patch(f"{OWNER}._vcs.fetch_refs", return_value={"refs": refs}), \
                patch(f"{OWNER}._vcs.inject_refs", side_effect=failure) as inject, \
                self.assertRaises(Denied) as refused:
            SYNC(context, {})
        self.assertEqual(str(refused.exception), "Upstream refs are unavailable: inject failed")
        self.assertIs(refused.exception.__cause__, failure)
        inject.assert_called_once_with(Path("/project"), refs)

    def test_success_writes_the_exact_payload_and_returns_the_exact_record(self):
        context = _context()
        refs = {"topic": "b" * 40, "main": SHA, "z": "c" * 40}
        receipt = {"injected": 3, "prefix": "refs/remotes/upstream"}
        with patch(f"{OWNER}._vcs.fetch_refs", return_value={"refs": refs}), \
                patch(f"{OWNER}._vcs.inject_refs", return_value=receipt), \
                patch(f"{OWNER}.write_json") as write:
            result = SYNC(context, {})
        self.assertEqual(result, {
            "synced": True, "injected": 3, "prefix": "refs/remotes/upstream",
            "trust": "external-untrusted", "upstream": SHA,
            "evidence": "sync.json", "refs": ["main", "topic", "z"]})
        write.assert_called_once_with(Path("/run/sync.json"), {
            "injected": 3, "prefix": "refs/remotes/upstream",
            "trust": "external-untrusted", "refs": refs})


class ContextReportTests(unittest.TestCase):
    def test_report_stages_args_writes_commentary_and_returns_staged(self):
        context = Mock()
        context.run_dir = Path("/run")
        args = {"title": "Shift notes", "body": "Nothing invented."}
        with patch(f"{OWNER}.write_json") as write:
            result = REPORT(context, args)
        self.assertIs(context.commentary, args)
        write.assert_called_once_with(Path("/run/commentary.json"), args)
        self.assertEqual(result, {"staged": True})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
