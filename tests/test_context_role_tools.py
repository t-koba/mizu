"""Context role/work dispatch is owned by an extracted mixin.

The four wrappers keep their behavior and reach their owning modules with the
trusted Context.
"""
import unittest
from unittest.mock import Mock, patch

from mizu.context_role_tools import ContextRoleToolsMixin
from mizu.runtime import Context


class ContextRoleToolsTests(unittest.TestCase):
    def test_context_inherits_every_wrapper(self):
        for name in ("_op_research_read", "_op_research", "_op_work_order",
                     "_op_materialize"):
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextRoleToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, name in (("research_read", "_op_research_read"),
                          ("research", "_op_research"),
                          ("work_order", "_op_work_order"),
                          ("materialize", "_op_materialize")):
            with self.subTest(op=key):
                self.assertIs(Context._DISPATCH[key],
                              getattr(ContextRoleToolsMixin, name))

    def test_wrappers_reach_their_owners(self):
        context = Mock()
        args = {"a": 1}
        with patch("mizu.context_role_tools.role_state_ops") as role_state, \
                patch("mizu.context_role_tools.work_order_ops") as work_order, \
                patch("mizu.context_role_tools._materialize") as materialize_mod:
            role_state.research_read.return_value = {"read": 1}
            role_state.research.return_value = {"research": 1}
            work_order.record.return_value = {"record": 1}
            materialize_mod.op_materialize.return_value = {"materialize": 1}

            self.assertEqual(Context._DISPATCH["research_read"](context, args), {"read": 1})
            role_state.research_read.assert_called_once_with(context, args)
            self.assertEqual(Context._DISPATCH["research"](context, args), {"research": 1})
            role_state.research.assert_called_once_with(context, args)
            self.assertEqual(Context._DISPATCH["work_order"](context, args), {"record": 1})
            work_order.record.assert_called_once_with(context, args)
            self.assertEqual(Context._DISPATCH["materialize"](context, args), {"materialize": 1})
            materialize_mod.op_materialize.assert_called_once_with(context, args)

    def test_materialize_doc_names_the_current_owner(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        text = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
        module = Context._op_materialize.__module__
        owner_path = "src/" + module.replace(".", "/") + ".py"
        self.assertIn("`" + owner_path + "`", text)
        self.assertIn("ContextRoleToolsMixin._op_materialize", text)
        self.assertNotIn("`src/mizu/runtime.py` (`Context._op_materialize`)", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
