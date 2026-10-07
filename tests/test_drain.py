from support import Fixture, ScriptDriver
from mizu.cli import _with_sandbox_image, parser, execute
from mizu.config import load
from mizu.errors import Busy, ConfigError
from mizu.project import Project
from mizu.runtime import Engine, should_run


class DrainTests(Fixture):
    def test_drain_blocks_new_runs_as_busy_without_cancelling(self):
        ctx = self.context()
        self.project.set_control(draining=True)
        with self.assertRaises(Busy):
            Engine(self.config, driver=ScriptDriver()).run(self.project, "worker")
        # Active work is undisturbed: draining never cancels tools.
        ctx.handle("read", {"path": "app.py"})
        self.assertFalse(self.project.snapshots.get()["id"] == "missing")

    def test_should_run_quiet_while_draining_and_resume_clears(self):
        snap = self.project.snapshots.get()
        self.assertTrue(should_run(self.project, snap))
        self.project.set_control(draining=True)
        self.assertFalse(should_run(self.project, snap))
        # Resume clears the drain and admits work again.
        args = parser().parse_args(["--config", str(self.file), "resume", "sample"])
        execute(args)
        self.assertFalse(self.project.control().get("draining"))
        self.assertFalse(self.project.control().get("paused"))

    def test_drain_command_sets_flag_and_status_exposes_it(self):
        args = parser().parse_args(["--config", str(self.file), "drain", "sample"])
        result = execute(args)
        self.assertTrue(result.get("draining"))
        status = self.project.status()
        self.assertTrue(status["control"].get("draining"))
        self.assertEqual(status["project"], "sample")

    def test_fresh_initialize_includes_draining_false(self):
        import json
        raw = json.loads((self.project.root / "control.json").read_text())
        self.assertIn("draining", raw)
        self.assertFalse(raw["draining"])
        self.assertIn("draining", self.project.status()["control"])

    def test_arm_clears_drain(self):
        self.project.set_control(draining=True)
        args = parser().parse_args(["--config", str(self.file), "arm", "sample"])
        execute(args)
        self.assertFalse(self.project.control().get("draining"))


class SandboxImageTests(Fixture):
    def test_override_replaces_in_memory_only(self):
        before = self.file.read_bytes()
        pinned = "sha256:" + "b" * 64
        config = _with_sandbox_image(self.config, pinned)
        self.assertEqual(config.sandbox.image, pinned)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertNotEqual(self.config.sandbox.image, pinned)

    def test_override_rejects_unpinned(self):
        with self.assertRaises(ConfigError):
            _with_sandbox_image(self.config, "ubuntu:latest")
        with self.assertRaises(ConfigError):
            _with_sandbox_image(self.config, "")

    def test_doctor_parser_accepts_candidate_image(self):
        args = parser().parse_args(["--config", str(self.file), "doctor", "--sandbox-image", "sha256:" + "c" * 64])
        self.assertTrue(args.sandbox_image.endswith("c" * 64))

    def test_smoke_has_no_sandbox_image_flag(self):
        # Read-only probe never consults the sandbox image; the flag was removed
        # so the promote flow cannot give false confidence.
        args = parser().parse_args(["--config", str(self.file), "smoke", "--live"])
        self.assertFalse(hasattr(args, "sandbox_image"))
        with self.assertRaises(SystemExit):
            parser().parse_args(["--config", str(self.file), "smoke", "--live", "--sandbox-image", "sha256:" + "d" * 64])
