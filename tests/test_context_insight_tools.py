"""Context insight/decision dispatch is owned by an extracted mixin.

The seven proposal/decision wrappers keep their dispatch keys and reach their
owning modules with the trusted Context as the first argument.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_insight_tools import ContextInsightToolsMixin
from mizu.runtime import Context


# (dispatch key, owning module, function)
_OPS = (
    ("decision_read", "decision_delivery", "read"),
    ("insights", "insight_tools", "insights"),
    ("decide", "insight_tools", "decide"),
    ("submit_insight", "insight_tools", "submit_insight"),
    ("revise_insight", "insight_tools", "revise_insight"),
    ("withdraw_insight", "insight_tools", "withdraw_insight"),
    ("attach_evidence", "insight_tools", "attach_evidence"),
)


class ContextInsightToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for key, _, _ in _OPS:
            self.assertTrue(callable(getattr(Context, "_op_" + key)), key)
        self.assertTrue(issubclass(Context, ContextInsightToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, _, _ in _OPS:
            self.assertIs(Context._DISPATCH[key],
                          getattr(ContextInsightToolsMixin, "_op_" + key), key)

    def test_dispatch_reaches_each_owner_with_context(self):
        for key, module, function in _OPS:
            with self.subTest(op=key):
                context = Mock()
                args = {"op": key}
                with patch("mizu.context_insight_tools." + module) as owner:
                    target = getattr(owner, function)
                    target.return_value = {"ok": key}
                    result = Context._DISPATCH[key](context, args)
                target.assert_called_once_with(context, args)
                self.assertEqual(result, {"ok": key})

    def test_runtime_retains_decision_delivery_for_engine(self):
        from mizu import runtime
        self.assertTrue(hasattr(runtime, "decision_delivery"))
        self.assertTrue(hasattr(runtime, "INSIGHT_RETRIEVAL_LIMIT"))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
