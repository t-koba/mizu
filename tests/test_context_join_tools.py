"""Context nested-join dispatch is owned by an extracted mixin.

The consultation and delegation handlers keep their join lifecycle: they gate
on the injected child, mark the join active and restore the idle signal.
"""
import unittest
from unittest.mock import Mock

from mizu.context_join_tools import ContextJoinToolsMixin
from mizu.errors import Denied
from mizu.runtime import Context


class _Idle:
    """Joins-idle stand-in that records clear/set calls."""

    def __init__(self):
        self.value = True
        self.clears = 0
        self.sets = 0

    def clear(self):
        self.value = False
        self.clears += 1

    def set(self):
        self.value = True
        self.sets += 1

    def is_set(self):
        return self.value


class _JoinContext:
    """Minimal Context stand-in with a real _activity_change."""

    def __init__(self, injection):
        self.consult = injection
        self.delegate = injection
        self.joins_idle = _Idle()
        self._activity_join = "before"
        self.changes = []

    def _activity_change(self, change, count=None):
        self.changes.append(count)
        change()


class ContextJoinToolsTests(unittest.TestCase):
    def test_context_inherits_both_wrappers(self):
        for name in ("_op_consult", "_op_delegate"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextJoinToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        self.assertIs(Context._DISPATCH["consult"], ContextJoinToolsMixin._op_consult)
        self.assertIs(Context._DISPATCH["delegate"], ContextJoinToolsMixin._op_delegate)

    def test_consult_success_runs_child_and_restores_idle(self):
        args = {"request": "x"}
        child = Mock(return_value={"ok": "consult"})
        context = _JoinContext(child)
        result = ContextJoinToolsMixin._op_consult(context, args)
        self.assertEqual(result, {"ok": "consult"})
        child.assert_called_once_with(context, args)
        self.assertIsNone(context._activity_join)
        self.assertTrue(context.joins_idle.is_set())
        self.assertEqual((context.joins_idle.clears, context.joins_idle.sets), (1, 1))
        self.assertEqual(context.changes, ["join:consult", None])

    def test_delegate_success_runs_child_and_restores_idle(self):
        args = {"task": "y"}
        child = Mock(return_value={"ok": "delegate"})
        context = _JoinContext(child)
        result = ContextJoinToolsMixin._op_delegate(context, args)
        self.assertEqual(result, {"ok": "delegate"})
        child.assert_called_once_with(context, args)
        self.assertIsNone(context._activity_join)
        self.assertTrue(context.joins_idle.is_set())
        self.assertEqual((context.joins_idle.clears, context.joins_idle.sets), (1, 1))
        self.assertEqual(context.changes, ["join:delegate", None])

    def test_consult_refusal_does_not_touch_activity(self):
        context = _JoinContext(None)
        with self.assertRaisesRegex(Denied, "Nested consultation is disabled"):
            ContextJoinToolsMixin._op_consult(context, {})
        self.assertEqual((context.joins_idle.clears, context.joins_idle.sets), (0, 0))
        self.assertEqual(context.changes, [])
        self.assertEqual(context._activity_join, "before")

    def test_delegate_refusal_does_not_touch_activity(self):
        context = _JoinContext(None)
        with self.assertRaisesRegex(Denied, "Nested delegation is disabled"):
            ContextJoinToolsMixin._op_delegate(context, {})
        self.assertEqual((context.joins_idle.clears, context.joins_idle.sets), (0, 0))
        self.assertEqual(context.changes, [])
        self.assertEqual(context._activity_join, "before")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
