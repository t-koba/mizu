"""Context work-item dispatch is owned by an extracted mixin.

The four wrappers keep their behavior and reach work_item_operations with the
trusted Context.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_work_tools import ContextWorkToolsMixin
from mizu.runtime import Context


class ContextWorkToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_op_work_items_read", "_op_work_items_create",
                     "_op_work_items_transition", "_op_work_items_reopen"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextWorkToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, name in (("work_items_read", "_op_work_items_read"),
                          ("work_items_create", "_op_work_items_create"),
                          ("work_items_transition", "_op_work_items_transition"),
                          ("work_items_reopen", "_op_work_items_reopen")):
            with self.subTest(op=key):
                self.assertIs(Context._DISPATCH[key],
                              getattr(ContextWorkToolsMixin, name))

    def test_wrappers_reach_work_item_operations(self):
        context = Mock()
        args = {"a": 1}
        with patch("mizu.context_work_tools.work_item_operations") as operations:
            operations.read.return_value = {"read": 1}
            operations.create.return_value = {"create": 1}
            operations.transition.return_value = {"transition": 1}
            operations.reopen.return_value = {"reopen": 1}

            for key, name in (("work_items_read", "read"),
                              ("work_items_create", "create"),
                              ("work_items_transition", "transition"),
                              ("work_items_reopen", "reopen")):
                with self.subTest(op=key):
                    self.assertEqual(Context._DISPATCH[key](context, args),
                                     {name: 1})
                    getattr(operations, name).assert_called_once_with(context, args)

    def test_wrappers_are_copied_from_runtime_contract(self):
        # Each wrapper is a thin, verbatim forward; its module is the new owner.
        for name in ("_op_work_items_read", "_op_work_items_create",
                     "_op_work_items_transition", "_op_work_items_reopen"):
            with self.subTest(name=name):
                method = getattr(Context, name)
                self.assertIs(method, getattr(ContextWorkToolsMixin, name))
                self.assertEqual(method.__module__, "mizu.context_work_tools")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
