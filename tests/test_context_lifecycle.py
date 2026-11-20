"""Context run-lifecycle owns one mixin module after the runtime split.

The wall/deadline, wrap-up, activity and CI-request methods now live in
context_lifecycle.py and are inherited by Context. This pins the boundary,
the re-exported wrap-up constant and the patch points without changing
behaviour: the deadline and drain suites still exercise the methods themselves.
"""
import time

from support import Fixture

from mizu.context_driver import ContextDriverMixin
from mizu.context_lifecycle import ContextLifecycleMixin
from mizu.context_lifecycle import WRAP_UP_OPS as MIXIN_WRAP_UP_OPS
from mizu.errors import DeadlineWrapUp
from mizu.runtime import Context, WRAP_UP_OPS
from mizu.unit_finish import ContextFinishMixin

METHODS = (
    "wall_expired", "enter_deadline_wrap_up", "enter_drain_wrap_up", "drain_refusal",
    "_activity_bucket", "_activity_change", "_publish_activity", "activity_summary",
    "record_ci_request", "ci_request_summary", "clear_request_deadlines",
    "start_request", "end_request", "check_deadlines", "transport_deadline",
    "transport_cancelled", "_delegated_wrap_up_limits", "_wrap_up_used_limit",
    "_wrap_up_request_used_limit", "_wrap_up_remaining", "_bound_message",
)


class ContextLifecycleBoundaryTests(Fixture):
    def test_mixin_owns_every_lifecycle_method(self):
        self.assertEqual(len(METHODS), 21)
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, ContextLifecycleMixin.__dict__)
                # Not defined in the Context body: supplied by inheritance.
                self.assertNotIn(name, Context.__dict__)
                self.assertIs(getattr(Context, name), getattr(ContextLifecycleMixin, name))

    def test_wrap_up_ops_is_one_shared_object(self):
        self.assertIs(WRAP_UP_OPS, MIXIN_WRAP_UP_OPS)
        self.assertEqual(WRAP_UP_OPS,
                         frozenset({"finish", "research", "submit_insight", "attach_evidence", "decide"}))

    def test_drain_refusal_constant_resolves_through_the_mixin(self):
        self.assertIn("DRAIN_REQUEST_REFUSED", ContextLifecycleMixin.__dict__)
        self.assertNotIn("DRAIN_REQUEST_REFUSED", Context.__dict__)
        self.assertEqual(Context.DRAIN_REQUEST_REFUSED, "Drain bound refused the provider request")
        self.assertIs(Context.DRAIN_REQUEST_REFUSED, ContextLifecycleMixin.DRAIN_REQUEST_REFUSED)

    def test_mixin_order_keeps_finish_and_driver_first(self):
        self.assertTrue(issubclass(Context, ContextLifecycleMixin))
        mro = Context.__mro__
        self.assertLess(mro.index(ContextFinishMixin), mro.index(ContextLifecycleMixin))
        self.assertLess(mro.index(ContextDriverMixin), mro.index(ContextLifecycleMixin))

    def test_moved_wall_deadline_and_drain_refusal_still_work(self):
        ctx = self.context()
        ctx.deadline = time.monotonic() - 1
        self.assertTrue(ctx.wall_expired())
        ctx.deadline = time.monotonic() + 60
        self.assertFalse(ctx.wall_expired())
        with self.assertRaises(DeadlineWrapUp):
            ctx.drain_refusal("drain bound reached")
