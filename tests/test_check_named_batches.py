"""Named recorded batches selectable through check_job batch_name.

Additive slice on the frozen check-job path: operator-owned
project.toml [recorded_batches] definitions resolve at submission to the
existing custom-experiment ordered-command path.
"""
import dataclasses
import threading
import uuid

from support import Fixture
from mizu import check_jobs
from mizu.errors import ConfigError, Denied
from mizu.fs import SCRIPT_MAX, read_json, write_json
from mizu.project import Project, check_recorded_batches
from mizu.protocol import DEFINITIONS, validate


class NamedSandbox:
    calls = []
    failures = set()

    def __init__(self, config, project, role, directory, **kwargs):
        self.directory = directory

    def execute(self, workspace, script, **kwargs):
        NamedSandbox.calls.append((str(workspace), script, dict(kwargs)))
        ident = uuid.uuid4().hex
        commands_dir = self.directory / "commands"
        commands_dir.mkdir(exist_ok=True)
        write_json(commands_dir / f"{ident}.json",
                   {"script": script, "stdout": "out:" + script[:32], "stderr": ""})
        if script in NamedSandbox.failures:
            return {"id": ident, "exit_code": 1, "reason": "exited",
                    "script": script, "stdout": "", "stderr": "boom"}
        return {"id": ident, "exit_code": 0, "reason": "exited",
                "script": script, "stdout": "", "stderr": ""}


class NamedFixture(Fixture):
    def setUp(self):
        super().setUp()
        NamedSandbox.calls = []
        NamedSandbox.failures = set()
        roles = {k: dataclasses.replace(v, capabilities=(*v.capabilities, "check_job"))
                 for k, v in self.config.roles.items()}
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
        self.project.recorded_batches = {
            "lint": ("echo lint", "echo test"),
            "single": ("echo hi",),
        }

    def run_job(self, factory=NamedSandbox, stop=None):
        return check_jobs.run_one(self.project, stop or threading.Event(),
                                  sandbox_factory=factory)


class NamedConfigTests(NamedFixture):
    def test_valid_table_parses(self):
        value = check_recorded_batches({"lint": ["echo a", "echo b"], "x-1": ["true"]})
        self.assertEqual(value, {"lint": ("echo a", "echo b"), "x-1": ("true",)})

    def test_missing_is_empty(self):
        self.assertEqual(check_recorded_batches(None), {})
        project = Project(self.config, self.project.name)
        # Fixture project.toml has no recorded_batches table.
        self.assertEqual(project.recorded_batches, {})

    def test_invalid_shapes_fail_load(self):
        with self.assertRaises(ConfigError):
            check_recorded_batches(["not-a-table"])
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": "not-a-list"})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": []})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": ["echo a"] * 33})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": ["echo a", 7]})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": ["   "]})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": ["echo\0bad"]})
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": ["x" * (SCRIPT_MAX + 1)]})
        with self.assertRaises(ConfigError):
            check_recorded_batches({f"n{n}": ["true"] for n in range(33)})
        # Multibyte UTF-8 is bounded by bytes, not characters.
        command = "\u00e9" * (SCRIPT_MAX // 2 + 1)
        self.assertLessEqual(len(command), SCRIPT_MAX)
        self.assertGreater(len(command.encode("utf-8")), SCRIPT_MAX)
        with self.assertRaises(ConfigError):
            check_recorded_batches({"ok": [command]})

    def test_invalid_names_fail_load(self):
        for bad in ("", "Upper", "has space", "has.dot", "a" * 64, "-lead", "_lead"):
            with self.subTest(name=bad):
                with self.assertRaises(ConfigError):
                    check_recorded_batches({bad: ["true"]})

    def test_project_toml_round_trip(self):
        settings = self.project.root / "project.toml"
        original = settings.read_text()
        try:
            settings.write_text(original + '\n[recorded_batches]\nlint = ["echo lint", "echo test"]\n')
            project = Project(self.config, self.project.name)
            self.assertEqual(project.recorded_batches, {"lint": ("echo lint", "echo test")})
        finally:
            settings.write_text(original)

    def test_project_toml_rejects_bad_batch(self):
        settings = self.project.root / "project.toml"
        original = settings.read_text()
        try:
            settings.write_text(original + '\n[recorded_batches]\nBad = ["true"]\n')
            with self.assertRaises(ConfigError):
                Project(self.config, self.project.name)
        finally:
            settings.write_text(original)


class NamedProtocolTests(NamedFixture):
    def test_schema_accepts_batch_name(self):
        validate({"action": "start", "batch_name": "lint"}, DEFINITIONS["check_job"][1])

    def test_schema_refuses_oversized_batch_name(self):
        with self.assertRaises(Denied):
            validate({"action": "start", "batch_name": "a" * 64}, DEFINITIONS["check_job"][1])

    def test_start_resolves_ordered_commands(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "batch_name": "lint"})
        self.assertEqual(job["kind"], "experiment")
        self.assertEqual(job["command_count"], 2)
        self.assertEqual(job["command_preview"], ["echo lint", "echo test"])
        self.assertEqual(self.run_job()["status"], "completed")
        self.assertEqual([c[1] for c in NamedSandbox.calls], ["echo lint", "echo test"])

    def test_unknown_and_malformed_names_refused_before_submission(self):
        before = len(list(check_jobs.root(self.project).glob("*/job.json")))
        for args in ({"action": "start", "batch_name": "missing"},
                     {"action": "start", "batch_name": "Bad"},
                     {"action": "start", "batch_name": ""},
                     {"action": "start", "batch_name": "a" * 64}):
            with self.subTest(args=args):
                with self.assertRaises(Denied):
                    self.context()._op_check_job(args)
        after = len(list(check_jobs.root(self.project).glob("*/job.json")))
        self.assertEqual(before, after)

    def test_conflicting_forms_refused(self):
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "batch_name": "lint", "script": "echo hi"})
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "batch_name": "lint", "commands": ["echo hi"]})
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "script": "echo hi", "commands": ["echo hi"]})

    def test_batch_name_on_non_start_refused(self):
        for action in ("list", "result", "cancel", "logs", "join"):
            args = {"action": action, "id": "x" * 64, "batch_name": "lint"}
            if action in ("list",):
                args = {"action": action, "batch_name": "lint"}
            if action in ("logs",):
                args = {"action": action, "id": "x" * 64, "batch_name": "lint"}
            with self.subTest(action=action):
                with self.assertRaises(Denied):
                    self.context()._op_check_job(args)

    def test_list_shows_available_names_without_command_text(self):
        result = self.context()._op_check_job({"action": "list"})
        self.assertEqual(result["batch_names"], ["lint", "single"])
        self.assertNotIn("commands", result)

    def test_omitted_mode_still_acceptance(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start"})
        self.assertEqual(job["kind"], "verify")
        self.assertEqual(list(check_jobs.read(self.project, job["id"])["commands"]),
                         list(self.project.verify))


class NamedIdentityTests(NamedFixture):
    def test_named_and_adhoc_same_commands_share_identity(self):
        ctx = self.context()
        via_name = ctx._op_check_job({"action": "start", "batch_name": "single"})
        via_adhoc = check_jobs.submit(ctx, commands=["echo hi"])
        self.assertEqual(via_name["id"], via_adhoc["id"])

    def test_rename_without_change_keeps_identity(self):
        self.project.recorded_batches = {"renamed": ("echo hi",)}
        ctx = self.context()
        via_renamed = ctx._op_check_job({"action": "start", "batch_name": "renamed"})
        via_adhoc = check_jobs.submit(ctx, commands=["echo hi"])
        self.assertEqual(via_renamed["id"], via_adhoc["id"])

    def test_edited_batch_selects_different_job(self):
        first = self.context()._op_check_job({"action": "start", "batch_name": "lint"})
        self.project.recorded_batches = {"lint": ("echo lint", "echo changed")}
        second = self.context()._op_check_job({"action": "start", "batch_name": "lint"})
        self.assertNotEqual(first["id"], second["id"])

    def test_submitted_job_keeps_resolved_commands(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "batch_name": "lint"})
        job_id = job["id"]
        # Later config edits affect new submissions only.
        self.project.recorded_batches = {"lint": ("echo edited",)}
        stored = check_jobs.read(self.project, job_id)
        self.assertEqual(list(stored["commands"]), ["echo lint", "echo test"])
        self.assertEqual(self.run_job()["status"], "completed")
        self.assertEqual([c[1] for c in NamedSandbox.calls], ["echo lint", "echo test"])

    def test_named_equal_to_acceptance_stays_custom_only(self):
        self.project.recorded_batches = {"acc": tuple(self.project.verify)}
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "batch_name": "acc"})
        self.assertEqual(job["kind"], "experiment")
        self.assertEqual(self.run_job()["status"], "completed")
        result = check_jobs.result(ctx, job["id"])
        self.assertTrue(result["result"]["passed"])
        self.assertNotIn("accepted_for_current_source", result)
        self.assertIsNone(ctx.verification)
        self.assertFalse((ctx.run_dir / "verification.json").exists())

    def test_named_batch_runs_on_frozen_snapshot(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "batch_name": "single"})
        (ctx.workspace / "app.py").write_text("VALUE = 3\n")
        frozen = (check_jobs.root(self.project) / job["id"] / "input/app.py").read_text()
        self.assertEqual(frozen, "VALUE = 2\n")
        self.assertEqual(self.run_job()["status"], "completed")


class NamedScratchTests(NamedFixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config,
            exclude=(*self.config.exclude, "target"),
            sandbox=dataclasses.replace(self.config.sandbox,
                                        check_cache_paths=("target",)))
        self.project.config = self.config
        self.project.snapshots.excludes = self.config.exclude

    def test_scratch_works_with_named_batch(self):
        ctx = self.context()
        job = ctx._op_check_job({"action": "start", "batch_name": "single",
                                 "scratch_paths": ["target"]})
        self.assertEqual(job["scratch_paths"], ["target"])
        self.assertIsNone(check_jobs.read(self.project, job["id"])["shared_key"])
        self.run_job()
        self.assertFalse(check_jobs.available(self.context()))
        other = check_jobs.submit(self.context(), commands=["echo hi"],
                                  scratch_paths=["target"])
        self.assertNotEqual(other["id"], job["id"])

    def test_scratch_still_requires_custom(self):
        with self.assertRaises(Denied):
            self.context()._op_check_job(
                {"action": "start", "scratch_paths": ["target"]})
