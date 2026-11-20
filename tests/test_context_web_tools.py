"""Context web-tool dispatch is owned by an extracted mixin.

The five fetch/probe/search/feed wrappers keep their dispatch keys and reach
their owning modules (and the bounded worker) with the trusted Context.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_web_tools import ContextWebToolsMixin
from mizu.runtime import Context


# (dispatch key, owning module, function)
_OPS = (
    ("fetch", "web_operations", "fetch"),
    ("probe", "web_operations", "probe"),
    ("search", "web_operations", "search"),
    ("feed_updates", "feed_updates", "read"),
)


class ContextWebToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_bounded_web_operation", "_op_fetch", "_op_probe",
                     "_op_feed_updates", "_op_search"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextWebToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, _, function in _OPS:
            self.assertIs(Context._DISPATCH[key],
                          getattr(ContextWebToolsMixin, "_op_" + key), key)

    def test_dispatch_reaches_each_owner_with_context(self):
        for key, module, function in _OPS:
            with self.subTest(op=key):
                context = Mock()
                args = {"op": key}
                with patch("mizu.context_web_tools." + module) as owner:
                    target = getattr(owner, function)
                    target.return_value = {"ok": key}
                    result = Context._DISPATCH[key](context, args)
                expected = (context,) if key == "feed_updates" else (context, args)
                target.assert_called_once_with(*expected)
                self.assertEqual(result, {"ok": key})

    def test_bounded_operation_reaches_web_worker(self):
        with patch("mizu.context_web_tools.bounded") as bounded:
            bounded.return_value = {"record": 1}
            result = ContextWebToolsMixin._bounded_web_operation(Mock(), "arg", "value")
        bounded.assert_called_once_with("arg", "value")
        self.assertEqual(result, {"record": 1})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
