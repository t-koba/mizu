"""Ordered custom-command batches through check_job start.

Covers the bounded slice: `commands` as an alternative to one custom
`script` on action=start only, reusing the frozen-source sequential lifecycle.
"""
import dataclasses
import threading
import uuid

from support import Fixture
from mizu import check_jobs
from mizu.errors import Denied
from mizu.fs import read_json, write_json
from mizu.protocol import DEFINITIONS, validate


class BatchSandbox:
    calls = []
    failures = set()
    cancel_after = None

    def __init__(self, config, project, role, directory, **kwargs):
        self.directory = directory

    def execute(self, workspace, script, **kwargs):
        BatchSandbox.calls.append((str(workspace), script, dict(kwargs)))
        ident = uuid.uuid4().hex
        commands_dir = self.directory / "commands"
        commands_dir.mkdir(exist_ok=True)
        write_json(commands_dir / f"{ident}.json",
                   {"script": script, "stdout": "out:" + script[:32], "stderr": ""})
        if script == BatchSandbox.cancel_after:
            write_json(self.directory / "cancel.json", {"requested": True})
        if script in BatchSandbox.failures:
            return {"id": ident, "exit_code": 1, "reason": "exited",
                    "script": script, "stdout": "", "stderr": "boom"}
        return {"id": ident, "exit_code": 0, "reason": "exited",
                "script": script, "stdout": "", "stderr": ""}


class BatchFixture(Fixture):
    def setUp(self):
        super().setUp()
        BatchSandbox.calls = []
        BatchSandbox.failures = set()
        BatchSandbox.cancel_after = None
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job"))
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

    def run_job(self, factory=BatchSandbox, stop=None):
        return check_jobs.run_one(self.project, stop or threading.Event(),
                                  sandbox_factory=factory)


class BatchProtocolTests(BatchFixture):
    def test_schema_accepts_ordered_commands(self):
        validate({"action": "start", "commands": ["echo first", "echo second"]},
                 DEFINITIONS["check_job"][1])

    def test_schema_refuses_empty_and_oversized_batches(self):
        from mizu.fs import SCRIPT_MAX
        with self.assertRaises(Denied):
            validate({"action": "start", "commands": []}, DEFINITIONS["check_job"][1])
        with self.assertRaises(Denied):
            validate({"action": "start", "commands": ["echo"] * 33},
                     DEFINITIONS["check_job"][1])
        with self.assertRaises(Denied):
            validate({"action": "start", "commands": ["ok", 7]},
                     DEFINITIONS["check_job"][1])
        with self.assertRaises(Denied):
            validate({"action": "start", "commands": ["x" * (SCRIPT_MAX + 1)]},
                     DEFINITIONS["check_job"][1])

    def test_schema_allows_blank_only_for_runtime_to_refuse(self):
        # text() carries no blank pattern; the runtime normalizes it away.
        validate({"action": "start", "commands": ["   "]}, DEFINITIONS["check_job"][1])
        with self.assertRaises(Denied):
            self.context()._op_check_job({"action": "start", "commands": ["   "]})
        with self.assertRaises(Denied):
            check_jobs.submit(self.context(), commands=["   "])

    def test_runtime_enforces_utf8_byte_bound_for_multibyte_text(self):
        from mizu.fs import SCRIPT_MAX
        command = "é" * (SCRIPT_MAX // 2 + 1)
        self.assertLessEqual(len(command), SCRIPT_MAX)
        self.assertGreater(len(command.encode("utf-8")), SCRIPT_MAX)
        args = {"action": "start", "commands": [command]}
        validate(args, DEFINITIONS["check_job"][1])
        with self.assertRaises(Denied):
            self.context()._op_check_job(args)


class BatchExecutionTests(BatchFixture):
    def test_ordered_execution_with_separate_evidence(self):
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo first", "echo second"])
        self.assertEqual(job["kind"], "experiment")
        self.assertEqual(job["command_count"], 2)
        self.assertEqual(job["command_preview"], ["echo first", "echo second"])
        self.assertEqual(self.run_job()["status"], "completed")
        self.assertEqual([c[1] for c in BatchSandbox.calls], ["echo first", "echo second"])
        # Each command is its own sandbox invocation, not one combined script.
        self.assertEqual(len(BatchSandbox.calls), 2)
        for _, _, kwargs in BatchSandbox.calls:
            self.assertTrue(kwargs.get("experiment"))
            self.assertFalse(kwargs.get("writable"))
        record = read_json(check_jobs.root(self.project) / job["id"] / "result.json")
        self.assertTrue(record["passed"])
        self.assertEqual([c["script"] for c in record["commands"]],
                         ["echo first", "echo second"])
        self.assertEqual(len({c["id"] for c in record["commands"]}), 2)
        listing = check_jobs.logs(ctx, job["id"])
        self.assertEqual(len(listing["commands"]), 2)
        seen = set()
        for entry in listing["commands"]:
            text = ctx._op_check_job(
                {"action": "logs", "id": job["id"],
                 "evidence_id": entry["evidence_id"]})["text"]
            self.assertIn("out:", text)
            seen.add(entry["evidence_id"])
        self.assertEqual(seen, {c["id"] for c in record["commands"]})

    def test_single_script_and_singleton_batch_share_identity(self):
        ctx = self.context()
        via_script = check_jobs.submit(ctx, "echo hi")
        via_batch = check_jobs.submit(ctx, commands=["echo hi"])
        self.assertEqual(via_script["id"], via_batch["id"])

    def test_identity_changes_on_edit_reorder_and_length(self):
        ctx = self.context()
        base = check_jobs.submit(ctx, commands=["echo a", "echo b"])["id"]
        reordered = check_jobs.submit(self.context(), commands=["echo b", "echo a"])["id"]
        edited = check_jobs.submit(self.context(), commands=["echo a", "echo c"])["id"]
        longer = check_jobs.submit(self.context(),
                                   commands=["echo a", "echo b", "echo c"])["id"]
        shorter = check_jobs.submit(self.context(), commands=["echo a"])["id"]
        self.assertNotEqual(base, reordered)
        self.assertNotEqual(base, edited)
        self.assertNotEqual(base, longer)
        self.assertNotEqual(base, shorter)

    def test_batch_freezes_source(self):
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo first", "echo second"])
        (ctx.workspace / "app.py").write_text("VALUE = 3\n")
        frozen = (check_jobs.root(self.project) / job["id"] / "input/app.py").read_text()
        self.assertEqual(frozen, "VALUE = 2\n")
        self.assertEqual(self.run_job()["status"], "completed")

    def test_aggregate_failure_is_completed_without_acceptance(self):
        BatchSandbox.failures = {"fail now"}
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo ok", "fail now"])
        self.assertEqual(self.run_job()["status"], "completed")
        result = check_jobs.result(ctx, job["id"])
        self.assertFalse(result["result"]["passed"])
        self.assertNotIn("accepted_for_current_source", result)
        self.assertIsNone(ctx.verification)

    def test_batch_equal_to_acceptance_never_imports(self):
        acceptance_commands = list(self.project.verify)
        self.assertTrue(acceptance_commands)
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=list(acceptance_commands))
        self.assertEqual(job["kind"], "experiment")
        self.assertEqual(self.run_job()["status"], "completed")
        result = check_jobs.result(ctx, job["id"])
        self.assertTrue(result["result"]["passed"])
        self.assertNotIn("accepted_for_current_source", result)
        self.assertIsNone(ctx.verification)
        self.assertFalse((ctx.run_dir / "verification.json").exists())


class BatchRequestShapeTests(BatchFixture):
    def test_simultaneous_and_malformed_batches_refused(self):
        from mizu.fs import SCRIPT_MAX
        ctx = self.context()
        with self.assertRaises(Denied):
            check_jobs.submit(ctx, "echo a", commands=["echo a"])
        for bad in ([], ["ok"] * 33, ["ok", 7], ["   "], [""],
                    ["x" * (SCRIPT_MAX + 1)], ["ok", "   "], "echo a",
                    [None], ["\x00"]):
            with self.subTest(bad=str(bad)[:24]), self.assertRaises(Denied):
                check_jobs.submit(self.context(), commands=bad)
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "script": "echo a", "commands": ["echo a"]})
        with self.assertRaises(Denied):
            self.context()._op_check_job({"action": "start", "commands": []})
        with self.assertRaises(Denied):
            self.context()._op_check_job({"action": "start", "commands": ["   "]})

    def test_custom_fields_require_start(self):
        for action in ("list", "result", "cancel", "logs", "join"):
            args = {"action": action, "id": "a" * 64}
            with self.subTest(action=action):
                with self.assertRaises(Denied):
                    self.context()._op_check_job({**args, "script": "echo hi"})
                with self.assertRaises(Denied):
                    self.context()._op_check_job({**args, "commands": ["echo hi"]})
                with self.assertRaises(Denied):
                    self.context()._op_check_job({**args, "scratch_paths": []})

    def test_tool_start_accepts_commands(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "commands": ["echo a", "echo b"]})
        self.assertEqual(job["command_count"], 2)
        self.assertEqual(job["kind"], "experiment")


class BatchOwnershipTests(BatchFixture):
    def test_cancellation_prevents_execution_and_wakes_only_owner(self):
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo a", "echo b"])
        self.assertTrue(check_jobs.cancel(ctx, job["id"])["cancel_requested"])
        self.assertEqual(self.run_job()["status"], "cancelled")
        self.assertFalse(BatchSandbox.calls)
        self.assertTrue(check_jobs.pending(self.project, "worker"))
        self.assertFalse(check_jobs.pending(self.project, "reviewer"))

    def test_cancellation_between_commands_stops_remaining_batch(self):
        BatchSandbox.cancel_after = "echo first"
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo first", "echo second"])
        self.assertEqual(self.run_job()["status"], "cancelled")
        self.assertEqual([call[1] for call in BatchSandbox.calls], ["echo first"])
        record = read_json(check_jobs.root(self.project) / job["id"] / "result.json")
        self.assertFalse(record["passed"])
        self.assertEqual([item["script"] for item in record["commands"]], ["echo first"])

    def test_owner_bound_result_and_logs(self):
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo a", "echo b"])
        self.run_job()
        other = self.context("reviewer")
        with self.assertRaises(Denied):
            check_jobs.result(other, job["id"])
        with self.assertRaises(Denied):
            check_jobs.logs(other, job["id"])

    def test_delegated_batch_is_delivered_to_parent(self):
        parent = self.context()
        child = self.context("reviewer")
        child.role = dataclasses.replace(
            child.role, capabilities=(*child.role.capabilities, "experiment"))
        self.project.config.roles["reviewer"] = child.role
        child.delegated_child = True
        child.spend_role = parent.role
        job = check_jobs.submit(child, commands=["echo a", "echo b"])
        self.assertEqual(job["role"], "worker")
        self.run_job()
        self.assertEqual([c[1] for c in BatchSandbox.calls], ["echo a", "echo b"])
        self.assertTrue(check_jobs.pending(self.project, "worker"))
        self.assertFalse(check_jobs.pending(self.project, "reviewer"))
        self.assertEqual(check_jobs.result(parent, job["id"])["status"], "completed")

    def test_purpose_replaces_earlier_batch(self):
        ctx = self.context()
        first = check_jobs.submit(ctx, commands=["echo a"], purpose="batch")
        second = check_jobs.submit(ctx, commands=["echo b"], purpose="batch")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(check_jobs.read(self.project, first["id"])["status"], "cancelled")


class BatchScratchTests(BatchFixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config,
            exclude=(*self.config.exclude, "target"),
            sandbox=dataclasses.replace(self.config.sandbox,
                                        check_cache_paths=("target",)))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude

    def test_scratch_batches_never_share(self):
        ctx = self.context()
        job = check_jobs.submit(ctx, commands=["echo a", "echo b"],
                                scratch_paths=["target"])
        self.assertEqual(job["scratch_paths"], ["target"])
        self.assertIsNone(check_jobs.read(self.project, job["id"])["shared_key"])
        self.run_job()
        self.assertFalse(check_jobs.available(self.context()))
        with self.assertRaises(Denied):
            check_jobs.join(self.context(), job["id"])
        other = check_jobs.submit(self.context(), commands=["echo a", "echo b"],
                                  scratch_paths=["target"])
        self.assertNotEqual(other["id"], job["id"])
        # Same request in the same run remains one demand.
        self.assertEqual(check_jobs.submit(ctx, commands=["echo a", "echo b"],
                                           scratch_paths=["target"])["id"], job["id"])

    def test_scratch_requires_custom_batch(self):
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "scratch_paths": ["target"]})
        with self.assertRaises(Denied):
            check_jobs.submit(self.context(), commands=["echo a"],
                              scratch_paths=["unknown"])
