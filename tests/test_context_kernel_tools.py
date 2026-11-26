"""Context queue/message tool dispatch is owned by an extracted mixin.

The four live queue/message wrappers keep their dispatch keys and reach the
kernel queue tool module with the trusted Context as first argument. The
object/event kernel and its dispatch keys were deleted; no test subject remains
for them here.
"""
import unittest
from unittest.mock import patch

from mizu.context_kernel_tools import ContextKernelToolsMixin
from mizu.runtime import Context


# (dispatch key, kernel tool function)
_OPS = (
    ("queue_write", "queue_write"),
    ("queue_read", "queue_read"),
    ("message_read", "message_read"),
    ("message_refs", "message_refs"),
)

_RETIRED = ("objects_create", "objects_transition", "objects_read", "objects_list",
            "events_read", "events_ack", "queue_ack")


class ContextKernelToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for key, _ in _OPS:
            method = getattr(Context, "_op_" + key)
            self.assertTrue(callable(method), key)
        self.assertTrue(issubclass(Context, ContextKernelToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, _ in _OPS:
            self.assertIs(Context._DISPATCH[key],
                          getattr(ContextKernelToolsMixin, "_op_" + key), key)

    def test_dispatch_reaches_each_kernel_tool_with_context(self):
        for key, function in _OPS:
            with self.subTest(op=key):
                context = object()
                args = {"op": key}
                with patch("mizu.context_kernel_tools.kernel_queue_tools") as tool:
                    target = getattr(tool, function)
                    target.return_value = {"ok": key}
                    result = Context._DISPATCH[key](context, args)
                target.assert_called_once_with(context, args)
                self.assertEqual(result, {"ok": key})

    def test_only_live_queue_and_message_dispatch_keys_are_owned(self):
        expected = {"_op_" + key for key, _ in _OPS}
        owned = {name for name in dir(ContextKernelToolsMixin)
                 if name.startswith("_op_")}
        self.assertEqual(owned, expected)
        for key in _RETIRED:
            with self.subTest(op=key):
                self.assertNotIn(key, Context._DISPATCH)
                self.assertNotIn("_op_" + key, owned)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
