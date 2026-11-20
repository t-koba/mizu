"""Context evidence-record dispatch is owned by an extracted mixin.

The three evidence helpers and two dispatch wrappers keep their behavior and
reach evidence_records (or the shared read helper) with the trusted Context.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_evidence import ContextEvidenceMixin
from mizu.runtime import Context


class ContextEvidenceTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_read_retained_evidence", "_new_invocation_evidence",
                     "_mutation_evidence", "_op_command_evidence_read",
                     "_op_experiment_evidence_read"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextEvidenceMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        self.assertIs(Context._DISPATCH["command_evidence_read"],
                      ContextEvidenceMixin._op_command_evidence_read)
        self.assertIs(Context._DISPATCH["experiment_evidence_read"],
                      ContextEvidenceMixin._op_experiment_evidence_read)

    def test_helpers_reach_evidence_records(self):
        context = Mock()
        with patch("mizu.context_evidence.evidence_records") as records:
            records.read_retained.return_value = {"read": 1}
            records.new_invocation.return_value = ("id", {"new": 1})
            records.mutation.return_value = ("before", "observe")
            self.assertEqual(ContextEvidenceMixin._read_retained_evidence(context, "commands", {"a": 1}), {"read": 1})
            records.read_retained.assert_called_once_with(context, "commands", {"a": 1})
            self.assertEqual(ContextEvidenceMixin._new_invocation_evidence(context, "vcs-read"), ("id", {"new": 1}))
            records.new_invocation.assert_called_once_with("vcs-read")
            self.assertEqual(ContextEvidenceMixin._mutation_evidence(context, "kind", "op", {"p": 1}), ("before", "observe"))
            records.mutation.assert_called_once_with(context, "kind", "op", {"p": 1})

    def test_dispatch_wrappers_route_through_shared_helper(self):
        for key, kind in (("command_evidence_read", "commands"),
                          ("experiment_evidence_read", "experiments")):
            with self.subTest(op=key):
                context = Mock()
                context._read_retained_evidence.return_value = {"ok": key}
                result = Context._DISPATCH[key](context, {"a": 1})
                context._read_retained_evidence.assert_called_once_with(kind, {"a": 1})
                self.assertEqual(result, {"ok": key})

    def test_runtime_retains_evidence_reexport_seams(self):
        from mizu import runtime
        self.assertTrue(hasattr(runtime, "evidence_records"))
        self.assertTrue(hasattr(runtime, "EVIDENCE_UUID"))
        self.assertTrue(hasattr(runtime, "MAX_STARTED_EVIDENCE_BYTES"))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
