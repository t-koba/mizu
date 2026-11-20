"""Context kernel-tool dispatch is owned by an extracted mixin.

The eleven object/event/queue/message wrappers keep their dispatch keys and
reach the same kernel tool modules with the trusted Context as first argument.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_kernel_tools import ContextKernelToolsMixin
from mizu.runtime import Context


# (dispatch key, kernel module, tool function)
_OPS = (
    ("objects_create", "kernel_tools", "objects_create"),
    ("objects_transition", "kernel_tools", "objects_transition"),
    ("objects_read", "kernel_tools", "objects_read"),
    ("objects_list", "kernel_tools", "objects_list"),
    ("events_read", "kernel_tools", "events_read"),
    ("events_ack", "kernel_tools", "events_ack"),
    ("queue_write", "kernel_queue_tools", "queue_write"),
    ("queue_read", "kernel_queue_tools", "queue_read"),
    ("queue_ack", "kernel_queue_tools", "queue_ack"),
    ("message_read", "kernel_queue_tools", "message_read"),
    ("message_refs", "kernel_queue_tools", "message_refs"),
)


class ContextKernelToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for key, _, _ in _OPS:
            method = getattr(Context, "_op_" + key)
            self.assertTrue(callable(method), key)
        self.assertTrue(issubclass(Context, ContextKernelToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, _, _ in _OPS:
            self.assertIs(Context._DISPATCH[key],
                          getattr(ContextKernelToolsMixin, "_op_" + key), key)

    def test_dispatch_reaches_each_kernel_tool_with_context(self):
        for key, module, function in _OPS:
            with self.subTest(op=key):
                context = Mock()
                args = {"op": key}
                with patch("mizu.context_kernel_tools." + module) as tool:
                    target = getattr(tool, function)
                    target.return_value = {"ok": key}
                    result = Context._DISPATCH[key](context, args)
                target.assert_called_once_with(context, args)
                self.assertEqual(result, {"ok": key})

    def test_all_kernel_dispatch_keys_retained(self):
        expected = {"_op_" + key for key, _, _ in _OPS}
        self.assertTrue(expected <= set(dir(ContextKernelToolsMixin)))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
