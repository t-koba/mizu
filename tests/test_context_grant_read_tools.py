"""Grant-checked Context read dispatch is owned by an extracted mixin.

The three handlers keep their grants, defaults, dispatch keys and patch seams
while reaching their owning modules with the trusted Context.
"""
import inspect
import unittest
from unittest.mock import Mock, patch

from mizu.context_grant_read_tools import ContextGrantReadToolsMixin
from mizu.errors import Denied
from mizu.runtime import Context

OWNER = "mizu.context_grant_read_tools"
HANDLERS = (
    ("ready_work_read", "_op_ready_work_read", "Ready-work read requires a work capability"),
    ("work_order_read", "_op_work_order_read", "Work-order read requires the work_order grant"),
    ("report_read", "_op_report_read", "Report read requires the report grant"),
)


def _context():
    context = Mock()
    context.role.name = "worker"
    context.role.capabilities = ("work_items_read", "work_order", "report")
    return context


class ContextGrantReadToolsTests(unittest.TestCase):
    def test_context_inherits_every_handler(self):
        for _, name, _ in HANDLERS:
            self.assertTrue(callable(getattr(Context, name)), name)
        self.assertTrue(issubclass(Context, ContextGrantReadToolsMixin))

    def test_dispatch_handlers_are_the_mixin_methods(self):
        for key, name, _ in HANDLERS:
            with self.subTest(op=key):
                self.assertIs(Context._DISPATCH[key],
                              getattr(ContextGrantReadToolsMixin, name))

    def test_handlers_are_owned_by_the_mixin(self):
        for _, name, _ in HANDLERS:
            with self.subTest(name=name):
                method = getattr(Context, name)
                self.assertIs(method, getattr(ContextGrantReadToolsMixin, name))
                self.assertEqual(method.__module__, OWNER)

    def test_each_handler_refuses_without_its_grant(self):
        for key, name, message in HANDLERS:
            with self.subTest(op=key):
                context = _context()
                with patch(f"{OWNER}.operation_allowed", return_value=False) as allowed, \
                        patch(f"{OWNER}._ready_work_projection") as projection, \
                        patch(f"{OWNER}._previous_report") as previous, \
                        patch("mizu.work_order.projection_for") as order:
                    with self.assertRaisesRegex(Denied, message):
                        Context._DISPATCH[key](context, {})
                allowed.assert_called_once_with(key, context.role.capabilities)
                context.project.work_items.page_ready.assert_not_called()
                projection.assert_not_called()
                order.assert_not_called()
                previous.assert_not_called()

    def test_ready_work_read_uses_its_page_default_and_trusted_context(self):
        for args, limit in (({}, 25), ({"limit": 7}, 7)):
            with self.subTest(args=args):
                context = _context()
                page = {"items": []}
                context.project.work_items.page_ready.return_value = page
                with patch(f"{OWNER}.operation_allowed", return_value=True) as allowed, \
                        patch(f"{OWNER}._ready_work_projection",
                              return_value={"projected": True}) as projection:
                    result = Context._DISPATCH["ready_work_read"](context, args)
                allowed.assert_called_once_with("ready_work_read", context.role.capabilities)
                context.project.work_items.page_ready.assert_called_once_with(
                    limit=limit, role_name="worker")
                projection.assert_called_once_with(page)
                self.assertEqual(result, {"projected": True})

    def test_work_order_read_reaches_its_owner_with_trusted_context(self):
        context = _context()
        with patch(f"{OWNER}.operation_allowed", return_value=True) as allowed, \
                patch("mizu.work_order.projection_for",
                      return_value={"status": "absent"}) as order:
            result = Context._DISPATCH["work_order_read"](context, {})
        allowed.assert_called_once_with("work_order_read", context.role.capabilities)
        order.assert_called_once_with(context)
        self.assertEqual(result, {"status": "absent"})

    def test_report_read_reaches_its_owner_with_trusted_project(self):
        context = _context()
        with patch(f"{OWNER}.operation_allowed", return_value=True) as allowed, \
                patch(f"{OWNER}._previous_report",
                      return_value={"body": "prior"}) as previous:
            result = Context._DISPATCH["report_read"](context, {})
        allowed.assert_called_once_with("report_read", context.role.capabilities)
        previous.assert_called_once_with(context.project)
        self.assertEqual(result, {"previous_report": {"body": "prior"}})

    def test_ready_work_read_keeps_the_no_delivery_comment(self):
        source = inspect.getsource(ContextGrantReadToolsMixin._op_ready_work_read)
        self.assertIn(
            "# A tool read is not opening-page delivery: do not retain signatures,",
            source)
        self.assertIn(
            "# consume resolution tokens or advance the prompt high-water receipt.",
            source)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
