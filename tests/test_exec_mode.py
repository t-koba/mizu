"""Caller-selected sync/async mode for ordinary exec/experiment calls."""
import dataclasses
import unittest
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied
from mizu.protocol import DEFINITIONS, validate


def _with_check_job(config):
    roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job"))
             for k, v in config.roles.items()}
    return dataclasses.replace(config, roles=roles)


def _sync_result(script="echo hi"):
    return {"id": "b" * 32, "kind": "command", "exit_code": 0, "reason": "exited",
            "seconds": 0.1, "writable": False, "script": script,
            "stdout": "hi", "stderr": ""}


class ExecModeSchemaTests(unittest.TestCase):
    def test_exec_and_experiment_expose_optional_mode(self):
        for op in ("exec", "experiment"):
            schema = DEFINITIONS[op][1]
            self.assertIn("mode", schema["properties"])
            self.assertEqual(schema["properties"]["mode"],
                             {"type": "string", "enum": ["sync", "async"]})
            self.assertEqual(list(schema["required"]), ["script"])
            validate({"script": "echo hi"}, schema)
            validate({"script": "echo hi", "mode": "sync"}, schema)
            validate({"script": "echo hi", "mode": "async"}, schema)
            with self.assertRaises(Denied):
                validate({"script": "echo hi", "mode": "queued"}, schema)
            with self.assertRaises(Denied):
                validate({"script": "echo hi", "mode": ""}, schema)

    def test_verify_and_check_job_do_not_accept_mode(self):
        for name in ("verify", "check_job"):
            with self.subTest(name=name), self.assertRaises(Denied):
                validate({"mode": "sync"}, DEFINITIONS[name][1])


class ExecModeRoutingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = _with_check_job(self.config)
        self.project.config = self.config

    def reviewer(self):
        ctx = self.context("reviewer")
        ctx.role = dataclasses.replace(ctx.role, capabilities=(*ctx.role.capabilities, "exec"))
        return ctx

    def test_omitted_mode_runs_direct_even_with_check_job(self):
        reader = self.reviewer()
        with patch.object(reader.sandbox, "execute", return_value=_sync_result()) as spy:
            result = reader._op_exec_experiment("exec", {"script": "echo hi"})
            self.assertNotIn("status", result)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(spy.called)
            _, kwargs = spy.call_args
            self.assertFalse(kwargs.get("writable"))
        writer = self.context()
        with patch.object(writer.sandbox, "execute", return_value=_sync_result()) as spy:
            result = writer._op_exec_experiment(
                "experiment", {"script": "echo hi"})
            self.assertNotIn("status", result)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(spy.called)
        # Writable exec stays synchronous by default.
        with patch.object(writer.sandbox, "execute", return_value=_sync_result()) as spy:
            result = writer._op_exec_experiment("exec", {"script": "echo hi"})
            self.assertNotIn("status", result)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(spy.called)
            _, kwargs = spy.call_args
            self.assertTrue(kwargs.get("writable"))
        # Source-receipt experiments stay synchronous by default.
        with patch.object(writer.sandbox, "execute", return_value=_sync_result()) as spy:
            result = writer._op_exec_experiment(
                "experiment", {"script": "echo hi", "source_receipt": "r" * 64})
            self.assertNotIn("status", result)
            self.assertTrue(spy.called)

    def test_omitted_mode_does_not_record_a_job(self):
        from mizu import check_jobs
        reader = self.reviewer()
        with patch.object(reader.sandbox, "execute", return_value=_sync_result()):
            reader._op_exec_experiment("exec", {"script": "echo hi"})
        writer = self.context()
        with patch.object(writer.sandbox, "execute", return_value=_sync_result()):
            writer._op_exec_experiment("experiment", {"script": "echo hi"})
        self.assertEqual(check_jobs.pending(self.project, "reviewer"), [])
        self.assertEqual(check_jobs.pending(self.project, "worker"), [])

    def test_without_check_job_ordinary_execution_stays_synchronous(self):
        ctx = self.context("reviewer")
        ctx.role = dataclasses.replace(
            ctx.role, capabilities=tuple(c for c in ctx.role.capabilities if c != "check_job"))
        with patch.object(ctx.sandbox, "execute", return_value=_sync_result()) as spy:
            result = ctx._op_exec_experiment("exec", {"script": "echo hi"})
            self.assertNotIn("status", result)
            self.assertTrue(spy.called)
        with patch.object(ctx.sandbox, "execute", return_value=_sync_result()) as spy:
            result = ctx._op_exec_experiment("experiment", {"script": "echo hi"})
            self.assertNotIn("status", result)
            self.assertTrue(spy.called)

    def test_sync_selects_the_direct_path(self):
        reader = self.reviewer()
        with patch.object(reader.sandbox, "execute", return_value=_sync_result()) as spy:
            result = reader._op_exec_experiment("exec", {"script": "echo hi", "mode": "sync"})
            self.assertNotIn("status", result)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(spy.called)
            _, kwargs = spy.call_args
            self.assertFalse(kwargs.get("writable"))
        writer = self.context()
        with patch.object(writer.sandbox, "execute", return_value=_sync_result()) as spy:
            result = writer._op_exec_experiment(
                "experiment", {"script": "echo hi", "mode": "sync"})
            self.assertNotIn("status", result)
            self.assertTrue(spy.called)

    def test_sync_does_not_bypass_workspace_denial(self):
        ctx = self.context()
        ctx.role = dataclasses.replace(ctx.role, workspace="none")
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("exec", {"script": "echo hi", "mode": "sync"})
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("experiment", {"script": "echo hi", "mode": "sync"})

    def test_explicit_async_queues_on_safe_paths(self):
        reader = self.reviewer()
        with patch.object(reader.sandbox, "execute",
                          side_effect=AssertionError("async must queue")):
            job = reader._op_exec_experiment("exec", {"script": "echo hi", "mode": "async"})
            self.assertEqual(job["status"], "queued")
        writer = self.context()
        with patch.object(writer.sandbox, "execute",
                          side_effect=AssertionError("async must queue")):
            job = writer._op_exec_experiment(
                "experiment", {"script": "echo hi", "mode": "async"})
            self.assertEqual(job["status"], "queued")

    def test_async_requires_check_job(self):
        ctx = self.context("reviewer")
        ctx.role = dataclasses.replace(
            ctx.role, capabilities=tuple(c for c in ctx.role.capabilities if c != "check_job"))
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("exec", {"script": "echo hi", "mode": "async"})
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("experiment", {"script": "echo hi", "mode": "async"})

    def test_async_refuses_live_writes_and_receipts(self):
        writer = self.context()
        with self.assertRaises(Denied):
            writer._op_exec_experiment("exec", {"script": "echo hi", "mode": "async"})
        with self.assertRaises(Denied):
            writer._op_exec_experiment(
                "experiment", {"script": "echo hi", "source_receipt": "r" * 64, "mode": "async"})
        reader = self.reviewer()
        with self.assertRaises(Denied):
            reader._op_exec_experiment(
                "experiment", {"script": "echo hi", "source_receipt": "r" * 64, "mode": "async"})

    def test_unsupported_mode_rejected(self):
        ctx = self.context()
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("exec", {"script": "echo hi", "mode": "queued"})
        with self.assertRaises(Denied):
            ctx._op_exec_experiment("experiment", {"script": "echo hi", "mode": ""})

    def test_explicit_async_calls_share_queue_identity(self):
        writer = self.context()
        first = writer._op_exec_experiment("experiment", {"script": "echo shared", "mode": "async"})
        second = writer._op_exec_experiment("experiment", {"script": "echo shared", "mode": "async"})
        self.assertEqual(first["id"], second["id"])

    def test_omitted_mode_does_not_share_queue_identity_with_async(self):
        writer = self.context()
        with patch.object(writer.sandbox, "execute", return_value=_sync_result("echo shared")):
            direct = writer._op_exec_experiment("experiment", {"script": "echo shared"})
        queued = writer._op_exec_experiment("experiment", {"script": "echo shared", "mode": "async"})
        self.assertNotIn("status", direct)
        self.assertEqual(queued["status"], "queued")

    def test_verify_behavior_unchanged(self):
        ctx = self.context()
        with patch.object(ctx.sandbox, "execute", side_effect=AssertionError("verify must queue")):
            self.assertEqual(ctx._op_verify({})["status"], "queued")
