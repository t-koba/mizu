"""Focused contracts for the Context host-call/protocol owner boundary."""
import dataclasses
import unittest
from unittest.mock import patch

from support import Fixture
from mizu import context_protocol, runtime
from mizu.errors import Cancelled, Denied, RunDeadlineExceeded


class ProtocolOwnerTests(Fixture):
    def test_host_operations_have_one_owner_outside_the_composition(self):
        expected = {"_request_done", "_hello", "_model_usage", "_check_steer",
                    "_check_steer_delivered", "_engine_tool", "_budget"}
        self.assertEqual(context_protocol.HOST_OPS, expected)
        for name in expected:
            self.assertTrue(context_protocol.is_host_operation(name), name)
            self.assertFalse(context_protocol.is_host_operation("files"), name)
        self.assertEqual(context_protocol.handle.__module__, "mizu.context_protocol")
        self.assertEqual(runtime.Context._handle.__module__, "mizu.runtime")
        self.assertEqual(runtime.Context.dispatch.__module__, "mizu.runtime")
        self.assertIs(runtime.Context._DISPATCH["files"], runtime.ContextReadToolsMixin._op_files)
        self.assertEqual(context_protocol.is_deferred.__module__, "mizu.context_protocol")
        self.assertIs(runtime.is_deferred, context_protocol.is_deferred)
        self.assertIs(runtime.is_infra_wait, context_protocol.is_infra_wait)

    def test_inputs_are_narrow_dataclass_holders_of_live_state(self):
        ctx = self.context()
        inputs = ctx._protocol_inputs("_engine_tool", {"name": "native"})
        self.assertTrue(dataclasses.is_dataclass(inputs))
        self.assertFalse(hasattr(inputs, "context"))
        self.assertFalse(any(field.name == "context" for field in dataclasses.fields(inputs)))
        self.assertEqual(inputs.operation, "_engine_tool")
        self.assertEqual(inputs.arguments, {"name": "native"})
        self.assertIs(inputs.state.runtime_usage, ctx.runtime_usage)
        self.assertIs(inputs.state.engine_tool_invocations, ctx.engine_tool_invocations)
        self.assertIs(inputs.state.hello, ctx.hello)
        self.assertEqual(inputs.state.fields.bound_hit.get(), ctx.bound_hit)
        self.assertEqual(inputs.limits.engine_tool_id, runtime.MAX_ENGINE_TOOL_ID)
        self.assertEqual(inputs.limits.engine_tool_invocations,
                         runtime.MAX_ENGINE_TOOL_INVOCATIONS)
        ctx.bound_hit = "tools"
        self.assertEqual(inputs.state.fields.bound_hit.get(), "tools")
        inputs.state.fields.bound_hit.set(None)
        self.assertIsNone(ctx.bound_hit)

    def test_every_gate_precedes_the_protocol_owner(self):
        ctx = self.context()
        with patch("mizu.context_protocol.handle", wraps=context_protocol.handle) as owner:
            with patch.object(runtime.delegation_gates, "check_access",
                              side_effect=Denied("delegation gate")):
                with self.assertRaisesRegex(Denied, "delegation gate"):
                    ctx.handle("_hello", {})
            owner.assert_not_called()
            ctx.stop.set()
            with self.assertRaises(Cancelled):
                ctx.handle("_hello", {})
            ctx.stop.clear()
            owner.assert_not_called()
            ctx.deadline = 0
            with self.assertRaises(RunDeadlineExceeded):
                ctx.handle("_hello", {})
            ctx.deadline = None
            owner.assert_not_called()
            ctx.finished = {"outcome": "wait"}
            with self.assertRaisesRegex(Denied, "sealed"):
                ctx.handle("_hello", {})
            ctx.finished = None
            owner.assert_not_called()
            ctx.checkpoint_seal_only = True
            with self.assertRaisesRegex(Denied, "checkpoint seal phase"):
                ctx.handle("_hello", {})
            owner.assert_not_called()
            ctx.checkpoint_seal_only = False
            self.assertEqual(ctx.handle("_hello", {}), {})
            self.assertTrue(ctx.hello.is_set())
            owner.assert_called_once()
            self.assertIsInstance(owner.call_args.args[0], context_protocol.ProtocolInputs)

    def test_owner_still_accounts_usage_grants_delivery_and_request_budget(self):
        ctx = self.context()
        self.assertEqual(ctx.handle("_hello", {}), {})
        self.assertEqual(ctx.handle("_model_usage", {
            "sequence": 1, "usage": {"input_tokens": 3}, "model": {"id": "m"}}),
            {"recorded": True})
        self.assertEqual(ctx.runtime_usage[(ctx.execution_attempt, 1)], {"input_tokens": 3})
        self.assertEqual(ctx.runtime_models[(ctx.execution_attempt, 1)], {"id": "m"})
        self.assertTrue((ctx.run_dir / "runtime-usage.json").exists())

        jobs = [{"id": "job-1", "state": "blocked", "next_step": "wait"}]
        with patch("mizu.context_protocol.check_jobs.steer_pending", return_value=jobs), \
                patch("mizu.context_protocol.check_jobs.steer_message", return_value="note"), \
                patch("mizu.context_protocol.check_jobs.mark_wake_delivered") as delivered:
            self.assertEqual(ctx.handle("_check_steer", {}),
                             {"messages": [{"id": "job-1", "message": "note"}]})
            self.assertEqual(ctx.check_steer_seen, frozenset({"job-1"}))
            self.assertEqual(ctx._check_steer_pending, {"job-1"})
            self.assertEqual(ctx.handle("_check_steer_delivered", {"ids": ["job-1"]}),
                             {"recorded": True})
            delivered.assert_called_once_with(ctx.project, ctx.role.name,
                                              ["job-1"], ctx.run_dir.name)
            self.assertEqual(ctx._check_steer_pending, set())

        ctx.role = dataclasses.replace(ctx.role, engine_tools=("native", "other"))
        self.assertEqual(ctx.handle("_engine_tool", {
            "name": "native", "invocation_id": "id-1"}), {"allowed": True})
        with self.assertRaisesRegex(Denied, "different name"):
            ctx.handle("_engine_tool", {"name": "other", "invocation_id": "id-1"})
        self.assertEqual(ctx.tool_count, 1)

        with patch("mizu.runtime.Budget.take") as take:
            self.assertEqual(ctx.handle("_budget", {"sequence": 1}), {"admitted": True})
            take.assert_called_once()
        self.assertEqual(ctx.request_count, 1)
        self.assertEqual(ctx.handle("_request_done", {"sequence": 1}), {"recorded": True})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
