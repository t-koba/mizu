"""The extracted VCS retirement handler keeps its dispatch and refusal behaviour."""
import dataclasses
import unittest
from unittest.mock import Mock, patch

from support import Fixture
from mizu.context_vcs_retire_tools import ContextVcsRetireMixin
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_vcs_retire_tools"
ARGS = {"branch": "mizu/x-1", "expected_sha": "a" * 40}
DATA = {"branch": "mizu/x-1", "sha": "b" * 40, "classification": "owned",
        "live_refs": [], "deleted": True}
RECORD = {"retired": True, "branch": DATA["branch"], "sha": DATA["sha"],
          "classification": DATA["classification"], "live_refs": DATA["live_refs"],
          "trust": "external-untrusted", "result": DATA}


def _context(workspace="write"):
    context = Mock()
    context.role.workspace = workspace
    context.config.vcs = {"retire_grant": True}
    context.tools_cancelled = "cancel-token"
    before_local, observe = Mock(), Mock(return_value={"observed": True})
    context._mutation_evidence.return_value = (before_local, observe)
    return context


class ContextVcsRetireDispatchTests(Fixture):
    def test_context_inherits_the_mixin_and_dispatches_its_identity(self):
        handler = ContextVcsRetireMixin._op_vcs_retire
        self.assertTrue(issubclass(Context, ContextVcsRetireMixin))
        self.assertIs(Context._op_vcs_retire, handler)
        self.assertIs(Context._DISPATCH["vcs_retire"], handler)
        self.assertEqual(handler.__module__, OWNER)

    def test_no_workspace_is_denied_before_any_adapter_dispatch(self):
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, workspace="none")
        with patch(f"{OWNER}._vcs.live_proposal_refs") as refs, \
                patch(f"{OWNER}._vcs.retire_via") as retire_via:
            with self.assertRaises(Denied) as refused:
                ctx.dispatch("vcs_retire", ARGS)
        self.assertEqual(str(refused.exception),
                         "Branch retirement requires a visible workspace")
        refs.assert_not_called()
        retire_via.assert_not_called()


class ContextVcsRetireMixinTests(unittest.TestCase):
    def test_live_proposal_references_block_before_mutation_or_adapter(self):
        context = _context()
        with patch(f"{OWNER}._vcs.live_proposal_refs",
                   return_value=["forge:owner/repo#7", "forge:owner/repo#9"]) as refs, \
                patch(f"{OWNER}._vcs.retire_via") as retire_via:
            with self.assertRaises(Denied) as refused:
                Context._op_vcs_retire(context, ARGS)
        self.assertEqual(str(refused.exception),
                         "Branch retirement is blocked by open proposal reference(s) "
                         "forge:owner/repo#7, forge:owner/repo#9; dispose the proposal "
                         "to a terminal state and re-observe before retiring")
        refs.assert_called_once_with(context.project, ARGS["branch"])
        context._mutation_evidence.assert_not_called()
        retire_via.assert_not_called()

    def test_retire_records_the_exact_record_and_observes_it(self):
        context = _context()
        before_local, observe = context._mutation_evidence.return_value
        with patch(f"{OWNER}._vcs.live_proposal_refs", return_value=[]) as refs, \
                patch(f"{OWNER}._vcs.retire_via", return_value=DATA) as retire_via:
            result = Context._op_vcs_retire(context, ARGS)
        refs.assert_called_once_with(context.project, ARGS["branch"])
        context._mutation_evidence.assert_called_once_with(
            "vcs-retire", "retire",
            {"branch": ARGS["branch"], "expected_sha": ARGS["expected_sha"]})
        retire_via.assert_called_once_with(
            context.config.vcs, ARGS["branch"], ARGS["expected_sha"],
            cancel="cancel-token", before_invoke=retire_via.call_args.kwargs["before_invoke"])
        retire_via.call_args.kwargs["before_invoke"]()
        before_local.assert_called_once_with()
        observe.assert_called_once_with(RECORD)
        self.assertEqual(result, {"observed": True})

    def test_adapter_oserror_maps_to_the_exact_denial(self):
        context = _context()
        _, observe = context._mutation_evidence.return_value
        failure = OSError("adapter down")
        with patch(f"{OWNER}._vcs.live_proposal_refs", return_value=[]), \
                patch(f"{OWNER}._vcs.retire_via", side_effect=failure):
            with self.assertRaises(Denied) as refused:
                Context._op_vcs_retire(context, ARGS)
        self.assertEqual(str(refused.exception),
                         "Branch retirement is unavailable: adapter down")
        self.assertIs(refused.exception.__cause__, failure)
        observe.assert_not_called()
