"""In-process accounting contention only; no async runner/provider evidence."""
import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from support import Fixture
from mizu.drivers import admit_invocation
from mizu.errors import InfraExceeded, LimitExceeded
from mizu.runtime import Context


class ContendedLock:
    """Observe a real competing claim without timing sleeps."""
    def __init__(self):
        self.lock = threading.RLock()
        self.contended = threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            if not self.lock.acquire(timeout=5):
                raise AssertionError("Spend claim deadlocked")
        return self

    def __exit__(self, *args):
        self.lock.release()


class SharedSpendTests(Fixture):
    def pair(self, *, requests=1, tools=1):
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, requests_per_run=requests, tools_per_run=tools,
            wrap_up_requests=3, wrap_up_tools=3,
            delegate_wrap_up_requests=2, delegate_wrap_up_tools=2))
        parent, child = self.context(), self.context()
        child._spend = parent._spend
        child.delegated_child = True
        child.spend_role = parent.role
        for ctx in (parent, child):
            ctx.role = dataclasses.replace(ctx.role, engine_tools=("native",))
            ctx.dispatch = Mock(return_value={"ok": True})
        return parent, child

    @staticmethod
    def request(ctx, mode, sequence=1):
        if mode == "bridge":
            return ctx.handle("_budget", {"sequence": sequence})
        return admit_invocation(ctx, unit="query")

    def race(self, parent, first, second, *, tool=False, daily_failure=False):
        """Pause the first debit until the second is waiting on its ledger."""
        entered, release = threading.Event(), threading.Event()
        ledger_lock = parent._spend.lock = ContendedLock()

        def pause(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Second claim did not reach spend admission")
                if daily_failure:
                    raise InfraExceeded("daily refusal")

        counter = Context.tool_count
        def read(ctx):
            value = counter.fget(ctx)
            pause()
            return value

        target = (patch.object(Context, "tool_count", property(read, counter.fset)) if tool
                  else patch("mizu.runtime.Budget.take", side_effect=pause))
        def attempt(call):
            try:
                call()
                return None
            except LimitExceeded as exc:
                return exc

        with target as admission, ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(attempt, first)
            try:
                self.assertTrue(entered.wait(5), "First claim never reached debit")
                two = pool.submit(attempt, second)
                self.assertTrue(ledger_lock.contended.wait(5), "No shared admission lock")
            finally:
                release.set()
            result = one.result(timeout=5), two.result(timeout=5)
            if not tool:
                self.assertEqual(admission.call_count, 2 if daily_failure else 1)
            return result

    def test_requests_share_last_ordinary_and_wrapup_slots_across_adapters(self):
        for first in ("bridge", "driver"):
            for second in ("bridge", "driver"):
                for wrapup in (False, True):
                    with self.subTest(first=first, second=second, wrapup=wrapup):
                        parent, child = self.pair()
                        if wrapup:
                            parent.request_count = 1
                            parent.wrap_up_requests = 2
                            parent.wrap_up_attempted = child.wrap_up_attempted = True
                        outcomes = self.race(parent,
                            lambda: self.request(child, first),
                            lambda: self.request(parent, second))
                        self.assertIsNone(outcomes[0])
                        self.assertIsInstance(outcomes[1], LimitExceeded)
                        self.assertEqual(parent.request_count, 2 if wrapup else 1)
                        self.assertEqual(parent.delegated_wrap_up_requests, int(wrapup))
                        self.assertEqual(parent.wrap_up_requests, 2 if wrapup else 0)
                        if wrapup:
                            self.assertEqual(child._wrap_up_remaining()[0], 0)
                            with self.assertRaises(LimitExceeded):
                                self.request(child, "bridge", 2)

    def test_daily_refusal_leaves_ordinary_and_wrapup_capacity_for_parent(self):
        for mode in ("bridge", "driver"):
            for wrapup in (False, True):
                with self.subTest(mode=mode, wrapup=wrapup):
                    parent, child = self.pair()
                    if wrapup:
                        parent.request_count = 1
                        parent.wrap_up_attempted = child.wrap_up_attempted = True
                    outcomes = self.race(parent,
                        lambda: self.request(child, mode),
                        lambda: self.request(parent, mode), daily_failure=True)
                    self.assertIsInstance(outcomes[0], InfraExceeded)
                    self.assertIsNone(outcomes[1])
                    self.assertFalse(child.request_sequences)
                    self.assertEqual(parent.request_count, 2 if wrapup else 1)
                    self.assertEqual(parent.wrap_up_requests, int(wrapup))
                    self.assertEqual(parent.delegated_wrap_up_requests, 0)
                    self.assertIsNone(child.bound_hit)

    def test_native_and_bridge_tools_share_last_slot_and_wrapup_total(self):
        for child_native, parent_native, wrapup in (
                (False, False, False), (True, False, False),
                (False, True, False), (True, True, False), (False, False, True)):
            with self.subTest(child_native=child_native, parent_native=parent_native, wrapup=wrapup):
                parent, child = self.pair()
                if wrapup:
                    parent.tool_count = 1
                    parent.wrap_up_tools = 2
                    parent.wrap_up_attempted = child.wrap_up_attempted = True
                def claim(ctx, native):
                    if native:
                        ctx.handle("_engine_tool", {"name": "native", "invocation_id": "one"})
                    else:
                        ctx.handle("finish" if wrapup else "files",
                            {"outcome": "wait", "summary": "result", "state": "saved"} if wrapup else {})
                outcomes = self.race(parent, lambda: claim(child, child_native),
                                     lambda: claim(parent, parent_native), tool=True)
                self.assertIsNone(outcomes[0])
                self.assertIsInstance(outcomes[1], LimitExceeded)
                # Tool count records attempts, including the refusal, as before.
                self.assertEqual(parent.tool_count, 3 if wrapup else 2)
                self.assertEqual(parent.delegated_wrap_up_tools, int(wrapup))
                self.assertEqual(parent.wrap_up_tools, 2 if wrapup else 0)
                self.assertEqual(len(child.engine_tool_invocations), int(child_native))
                self.assertEqual(child.dispatch.call_count, int(not child_native))
                self.assertFalse(parent.engine_tool_invocations)
                parent.dispatch.assert_not_called()
                if wrapup:
                    self.assertEqual(child._wrap_up_remaining()[1], 0)
                    with self.assertRaises(LimitExceeded):
                        claim(child, False)

    def test_child_share_exhaustion_preserves_root_seal_and_local_deadline(self):
        parent, child = self.pair(requests=0, tools=0)
        child.bound_hit = "deadline"
        child.wrap_up_attempted = parent.wrap_up_attempted = True
        finish = {"outcome": "wait", "summary": "result", "state": "saved"}
        with patch("mizu.runtime.Budget.take"):
            for sequence in (1, 2):
                self.request(child, "bridge", sequence)
                child.handle("finish", finish)
            with self.assertRaises(LimitExceeded):
                self.request(child, "bridge", 3)
            with self.assertRaises(LimitExceeded):
                child.handle("finish", finish)
            self.assertIsNone(parent.bound_hit)
            self.assertEqual(parent._wrap_up_remaining(), (1, 1))
            self.request(parent, "driver")
            parent.handle("finish", finish)
        self.assertEqual(parent._wrap_up_remaining(), (0, 0))
        self.assertEqual(child._wrap_up_remaining(), (0, 0))
        self.assertEqual(child.bound_hit, "deadline")

    def test_request_and_native_tool_identities_remain_context_local(self):
        for mode in ("bridge", "driver"):
            with self.subTest(mode=mode):
                parent, child = self.pair(requests=2, tools=2)
                with patch("mizu.runtime.Budget.take") as daily:
                    for ctx in (parent, child, parent, child):
                        self.request(ctx, mode)
                        ctx.handle("_engine_tool", {"name": "native", "invocation_id": "one"})
                self.assertEqual(daily.call_count, 2)
                self.assertEqual(parent.request_count, 2)
                self.assertEqual(parent.tool_count, 2)
