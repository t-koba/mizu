"""Context workspace/execution dispatch is owned by an extracted mixin.

The six wrappers keep their behavior and reach their owning modules (or the
shared exec/experiment helper) with the trusted Context.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_read_tools import ContextReadToolsMixin
from mizu.runtime import Context


class ContextReadToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_op_result_read", "_op_diff", "_op_files",
                     "_op_exec_experiment", "_op_exec", "_op_experiment"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextReadToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, name in (("result_read", "_op_result_read"),
                          ("diff", "_op_diff"),
                          ("files", "_op_files"),
                          ("exec", "_op_exec"),
                          ("experiment", "_op_experiment")):
            with self.subTest(op=key):
                self.assertIs(Context._DISPATCH[key],
                              getattr(ContextReadToolsMixin, name))

    def test_read_wrappers_reach_their_owners(self):
        context = Mock()
        args = {"a": 1}
        with patch("mizu.context_read_tools.tool_results") as results, \
                patch("mizu.context_read_tools.workspace_reads") as reads:
            results.read.return_value = {"read": 1}
            reads.op_diff.return_value = {"diff": 1}
            reads.op_files.return_value = {"files": 1}
            self.assertEqual(Context._DISPATCH["result_read"](context, args), {"read": 1})
            results.read.assert_called_once_with(context, args)
            self.assertEqual(Context._DISPATCH["diff"](context, args), {"diff": 1})
            reads.op_diff.assert_called_once_with(context, args)
            self.assertEqual(Context._DISPATCH["files"](context, args), {"files": 1})
            reads.op_files.assert_called_once_with(context, args)

    def test_exec_experiment_reaches_sandbox_operations(self):
        context = Mock()
        args = {"script": "echo hi"}
        with patch("mizu.context_read_tools.sandbox_operations") as sandbox:
            sandbox.exec_experiment.return_value = {"ok": 1}
            self.assertEqual(
                ContextReadToolsMixin._op_exec_experiment(context, "exec", args), {"ok": 1})
            sandbox.exec_experiment.assert_called_once_with(context, "exec", args)

    def test_exec_and_experiment_route_to_shared_helper(self):
        for name, op in (("_op_exec", "exec"), ("_op_experiment", "experiment")):
            with self.subTest(op=op):
                context = Mock()
                context._op_exec_experiment.return_value = {"op": op}
                result = getattr(ContextReadToolsMixin, name)(context, {"a": 1})
                context._op_exec_experiment.assert_called_once_with(op, {"a": 1})
                self.assertEqual(result, {"op": op})

    def test_exec_dispatch_reaches_sandbox_through_shared_helper(self):
        class Stub(ContextReadToolsMixin):
            pass

        stub = Stub()
        args = {"script": "echo hi"}
        with patch("mizu.context_read_tools.sandbox_operations") as sandbox:
            sandbox.exec_experiment.return_value = {"ok": 1}
            for op in ("exec", "experiment"):
                with self.subTest(op=op):
                    self.assertEqual(getattr(stub, "_op_" + op)(args), {"ok": 1})
                    sandbox.exec_experiment.assert_called_with(stub, op, args)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
