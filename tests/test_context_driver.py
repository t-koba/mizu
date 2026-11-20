"""Context execution lifecycle owns one mixin module after the runtime split.

The exec-session, driver-checkpoint and cancellation methods now live in
context_driver.py and are inherited by Context. This pins the boundary and the
patch points without changing behaviour: the existing driver/deadline suites
still exercise the methods themselves.
"""
from unittest.mock import patch

from support import Fixture, ROOT  # noqa: F401  (keeps sys.path contract identical)

from mizu.context_driver import ContextDriverMixin
from mizu.runtime import Context

METHODS = (
    "close_exec", "persistent_execute", "driver_capabilities", "begin_driver",
    "complete_driver_checkpoint", "claim_driver_checkpoint", "wait_for_joins",
    "cancel_operations", "tools_cancelled", "host_cancelled", "cancelled",
)


class ContextDriverBoundaryTests(Fixture):
    def test_mixin_owns_every_lifecycle_method(self):
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, ContextDriverMixin.__dict__)
                # Not defined in the Context body: supplied by inheritance.
                self.assertNotIn(name, Context.__dict__)

    def test_inheritance_preserves_method_identity_and_patch_points(self):
        ctx = self.context()
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIs(getattr(Context, name), getattr(ContextDriverMixin, name))
                self.assertIs(getattr(type(ctx), name), getattr(ContextDriverMixin, name))
        # patch.object(Context, ...) still intercepts the inherited method.
        with patch.object(Context, "close_exec") as mocked:
            ctx.close_exec()
        mocked.assert_called_once_with()

    def test_driver_capabilities_still_gated_by_seal_only(self):
        ctx = self.context()
        ctx.role = self.config.roles["worker"]
        self.assertIn("finish", ctx.driver_capabilities)
        ctx.checkpoint_seal_only = True
        self.assertEqual(ctx.driver_capabilities, ("finish",))

    def test_cancellation_state_is_unchanged(self):
        ctx = self.context()
        self.assertFalse(ctx.tools_cancelled())
        self.assertFalse(ctx.cancelled())
        ctx.operations_stopped.set()
        self.assertTrue(ctx.tools_cancelled())
