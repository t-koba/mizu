"""The extracted VCS disposition handler keeps its dispatch, gates and records."""
import dataclasses
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
import test_vcs_dispose as dispose
from mizu.context_vcs_dispose_tools import ContextVcsDisposeMixin
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_vcs_dispose_tools"
HANDLER = ContextVcsDisposeMixin._op_vcs_dispose
PINS = dispose.CLOSE_IF_PINS
CLOSE_IF_ARGS = dispose.close_if_args()
CLOSE_ARGS = dispose.close_args()
MERGE_ARGS = dispose.close_args(op="merge")
DIGEST = "e" * 64
APPROVAL = {"approval": "message-1"}


def _context():
    context = Mock()
    context.role.workspace = "write"
    context.workspace = Path("/workspace")
    context.config.vcs = {"approval_queue": "proposals"}
    context.tools_cancelled = "cancel-token"
    context.conditional_close_attempted = False
    before_local, observe = Mock(), Mock(return_value={"observed": True})
    context._mutation_evidence.return_value = (before_local, observe)
    return context


def _close_if_data(closed=True):
    receipt = dispose.close_if_receipt(closed=closed)
    return {"op": "close_if_adopted", **PINS, "predicate": receipt["predicate"],
            "closed": closed, "adapter_receipt": receipt,
            "trust": "external-untrusted"}


def _close_if_record(data):
    return {"disposed": data["closed"], "op": data["op"], **PINS,
            "predicate": data["predicate"], "closed": data["closed"],
            "trust": "external-untrusted",
            "adapter_receipt": data["adapter_receipt"]}


class ContextVcsDisposeDispatchTests(Fixture):
    def test_context_inherits_the_mixin_and_dispatches_its_identity(self):
        self.assertTrue(issubclass(Context, ContextVcsDisposeMixin))
        self.assertIs(Context._op_vcs_dispose, HANDLER)
        self.assertIs(Context._DISPATCH["vcs_dispose"], HANDLER)
        self.assertEqual(HANDLER.__module__, OWNER)

    def test_no_workspace_is_denied_before_evidence_or_adapter(self):
        context = self.context("worker")
        context.role = dataclasses.replace(context.role, workspace="none")
        with patch.object(context, "_mutation_evidence") as evidence, \
                patch(f"{OWNER}._vcs.close_if_adopted_via") as conditional, \
                patch(f"{OWNER}._vcs.dispose_via") as dispose_via:
            for args in (CLOSE_ARGS, CLOSE_IF_ARGS, MERGE_ARGS):
                with self.subTest(op=args["op"]), self.assertRaises(Denied) as refused:
                    context.dispatch("vcs_dispose", args)
                self.assertEqual(str(refused.exception),
                                 "Proposal disposition requires a visible workspace")
        evidence.assert_not_called()
        conditional.assert_not_called()
        dispose_via.assert_not_called()


class ContextVcsDisposeMixinTests(unittest.TestCase):
    def test_invalid_arguments_are_refused_before_evidence_or_adapter(self):
        context = _context()
        invalid = (
            {**CLOSE_ARGS, "op": "reopen"},
            {key: value for key, value in CLOSE_ARGS.items() if key != "sha"},
            {**CLOSE_ARGS, "unexpected": True},
            {**MERGE_ARGS, "unexpected": True},
        )
        with patch(f"{OWNER}._vcs.close_if_adopted_via") as conditional, \
                patch(f"{OWNER}._vcs.dispose_via") as dispose_via:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(Denied) as refused:
                    HANDLER(context, args)
                self.assertEqual(str(refused.exception),
                                 "Invalid proposal disposition arguments")
        context._mutation_evidence.assert_not_called()
        conditional.assert_not_called()
        dispose_via.assert_not_called()
        self.assertFalse(context.conditional_close_attempted)

    def test_invalid_conditional_pins_do_not_consume_the_one_shot(self):
        context = _context()
        invalid = (
            {key: value for key, value in CLOSE_IF_ARGS.items() if key != "head_sha"},
            {**CLOSE_IF_ARGS, "unexpected": True},
        )
        with patch(f"{OWNER}._vcs.close_if_adopted_via") as conditional:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(Denied) as refused:
                    HANDLER(context, args)
                self.assertEqual(str(refused.exception),
                                 "Conditional close requires op and exactly the six observed pins")
        context._mutation_evidence.assert_not_called()
        conditional.assert_not_called()
        self.assertFalse(context.conditional_close_attempted)

    def test_conditional_close_success_observes_record_and_reserves_one_attempt(self):
        context = _context()
        before_local, observe = context._mutation_evidence.return_value
        data = _close_if_data(closed=True)
        with patch(f"{OWNER}._vcs.close_if_adopted_via", return_value=data) as conditional:
            result = HANDLER(context, CLOSE_IF_ARGS)
        context._mutation_evidence.assert_called_once_with(
            "vcs-dispose", "close_if_adopted", PINS)
        conditional.assert_called_once_with(
            context.config.vcs, PINS, cancel="cancel-token",
            before_invoke=conditional.call_args.kwargs["before_invoke"])
        reserve = conditional.call_args.kwargs["before_invoke"]
        self.assertFalse(context.conditional_close_attempted)
        reserve()
        self.assertTrue(context.conditional_close_attempted)
        before_local.assert_called_once_with()
        with self.assertRaises(Denied) as refused:
            reserve()
        self.assertEqual(str(refused.exception),
                         "Conditional proposal close was already attempted in this work unit")
        before_local.assert_called_once_with()
        observe.assert_called_once_with(_close_if_record(data))
        self.assertEqual(result, {"observed": True})

    def test_conditional_close_refusal_is_observed_and_cannot_be_retried(self):
        context = _context()
        before_local, observe = context._mutation_evidence.return_value
        data = _close_if_data(closed=False)
        requests = []

        def adapter(settings, pins, *, cancel, before_invoke):
            requests.append(pins)
            before_invoke()
            return data

        with patch(f"{OWNER}._vcs.close_if_adopted_via", side_effect=adapter):
            result = HANDLER(context, CLOSE_IF_ARGS)
            with self.assertRaises(Denied) as refused:
                HANDLER(context, CLOSE_IF_ARGS)
        self.assertEqual(str(refused.exception),
                         "Conditional proposal close was already attempted in this work unit")
        self.assertEqual(requests, [PINS, PINS])
        before_local.assert_called_once_with()
        observe.assert_called_once_with(_close_if_record(data))
        self.assertEqual(result, {"observed": True})

    def test_close_uses_grant_parameters_and_observes_the_record(self):
        context = _context()
        before_local, observe = context._mutation_evidence.return_value
        data = {**dispose.echo(state="closed"), "trust": "external-untrusted"}
        with patch(f"{OWNER}._vcs.dispose_via", return_value=data) as dispose_via:
            result = HANDLER(context, CLOSE_ARGS)
        context._mutation_evidence.assert_called_once_with(
            "vcs-dispose", "close",
            {key: value for key, value in CLOSE_ARGS.items() if key != "op"})
        dispose_via.assert_called_once_with(
            context.config.vcs, "close", dispose.PROPOSAL_ID, dispose.SHA_A,
            dispose.BASE_A, dispose.TARGET, cancel="cancel-token",
            before_invoke=dispose_via.call_args.kwargs["before_invoke"])
        dispose_via.call_args.kwargs["before_invoke"]()
        before_local.assert_called_once_with()
        context.close_exec.assert_not_called()
        context.project.snapshots.capture_files.assert_not_called()
        expected = {"disposed": True, "op": "close", "id": data["id"],
                    "sha": data["sha"], "base": data["base"],
                    "target": data["target"], "state": "closed",
                    "branch": CLOSE_ARGS["branch"], "trust": "external-untrusted",
                    "result": data}
        observe.assert_called_once_with(expected)
        self.assertEqual(result, {"observed": True})

    def test_merge_binds_approval_to_the_assessed_endpoints(self):
        context = _context()
        before_local, observe = context._mutation_evidence.return_value
        context.project.snapshots.capture_files.return_value = {"code_digest": DIGEST}
        data = {**dispose.echo(state="merged"), "trust": "external-untrusted"}
        with patch(f"{OWNER}._vcs.require_go_approval", return_value=APPROVAL) as require, \
                patch(f"{OWNER}._vcs.dispose_via", return_value=data) as dispose_via:
            result = HANDLER(context, MERGE_ARGS)
        context.close_exec.assert_called_once_with()
        context.project.snapshots.capture_files.assert_called_once_with(context.workspace)
        require.assert_called_once_with(
            context.project, MERGE_ARGS["branch"], DIGEST,
            endpoints={"target": MERGE_ARGS["target"], "id": MERGE_ARGS["id"],
                       "sha": MERGE_ARGS["sha"], "base": MERGE_ARGS["base"]},
            queue="proposals")
        context._mutation_evidence.assert_called_once_with(
            "vcs-dispose", "merge",
            {**{key: value for key, value in MERGE_ARGS.items() if key != "op"},
             "code_digest": DIGEST, "approval": "message-1"})
        dispose_via.assert_called_once_with(
            context.config.vcs, "merge", dispose.PROPOSAL_ID, dispose.SHA_A,
            dispose.BASE_A, dispose.TARGET, cancel="cancel-token",
            before_invoke=dispose_via.call_args.kwargs["before_invoke"])
        dispose_via.call_args.kwargs["before_invoke"]()
        before_local.assert_called_once_with()
        expected = {"disposed": True, "op": "merge", "id": data["id"],
                    "sha": data["sha"], "base": data["base"],
                    "target": data["target"], "state": "merged",
                    "branch": MERGE_ARGS["branch"], "trust": "external-untrusted",
                    "result": data, "code_digest": DIGEST, "approval": "message-1"}
        observe.assert_called_once_with(expected)
        self.assertEqual(result, {"observed": True})

    def test_merge_requires_a_representable_snapshot(self):
        context = _context()
        context.project.snapshots.capture_files.return_value = {"skipped": ["unsafe"]}
        with patch(f"{OWNER}._vcs.require_go_approval") as require, \
                patch(f"{OWNER}._vcs.dispose_via") as dispose_via:
            with self.assertRaises(Denied) as refused:
                HANDLER(context, MERGE_ARGS)
        self.assertEqual(str(refused.exception),
                         "Proposal disposition requires a representable snapshot")
        context.close_exec.assert_called_once_with()
        require.assert_not_called()
        dispose_via.assert_not_called()
        context._mutation_evidence.assert_not_called()

    def test_adapter_oserrors_map_to_the_exact_denials(self):
        failure = OSError("adapter down")
        cases = (
            (CLOSE_IF_ARGS, "close_if_adopted_via",
             "Conditional proposal close is unavailable: adapter down"),
            (CLOSE_ARGS, "dispose_via",
             "Proposal disposition is unavailable: adapter down"),
        )
        for args, target, message in cases:
            with self.subTest(op=args["op"]):
                context = _context()
                _, observe = context._mutation_evidence.return_value
                with patch(f"{OWNER}._vcs.{target}", side_effect=failure):
                    with self.assertRaises(Denied) as refused:
                        HANDLER(context, args)
                self.assertEqual(str(refused.exception), message)
                self.assertIs(refused.exception.__cause__, failure)
                observe.assert_not_called()
