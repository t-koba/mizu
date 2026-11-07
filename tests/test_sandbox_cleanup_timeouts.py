"""OCI cleanup wall deadlines are host policy with unchanged defaults."""
import dataclasses
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.config import TOML_INTEGER_MAX, load
from mizu.errors import ConfigError
from mizu.process import Result
from mizu import sandbox


class SandboxCleanupTimeoutConfigTests(Fixture):
    names = {
        "cleanup_timeout_seconds": 15,
        "bulk_cleanup_timeout_seconds": 30,
    }

    def source_with(self, key, value, text=None):
        text = self.file.read_text() if text is None else text
        marker = 'mode = "rootless"'
        assert marker in text
        return text.replace(marker, marker + f"\n{key} = {value}", 1)

    def test_defaults_and_positive_toml_integer_overrides(self):
        for key, default in self.names.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(self.config.sandbox, key), default)
                original = self.file.read_text()
                for value in (1, default, TOML_INTEGER_MAX):
                    self.file.write_text(self.source_with(key, value, original))
                    self.assertEqual(getattr(load(self.file).sandbox, key), value)
                for raw in ("0", "-1", "true", "1.5", '"1"', "[]", "{}"):
                    self.file.write_text(self.source_with(key, raw, original))
                    with self.assertRaisesRegex(ConfigError, f"sandbox.{key}"):
                        load(self.file)
                self.file.write_text(original)

    def test_example_and_configuration_docs_name_both_defaults(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "config" / "config.example.toml").read_text(encoding="utf-8")
        docs = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
        for key, default in self.names.items():
            with self.subTest(key=key):
                self.assertIn(f"# {key} = {default}", example)
                self.assertIn(f"`{key}`", docs)
                self.assertIn(f"| `{key}` | `{default}` |", docs)

    def test_cleanup_commands_use_configured_per_call_deadlines(self):
        configured_sandbox = dataclasses.replace(
            self.config.sandbox, executable="podman", cleanup_timeout_seconds=7,
            bulk_cleanup_timeout_seconds=19)
        config = dataclasses.replace(self.config, sandbox=configured_sandbox)

        with patch("mizu.sandbox.run", side_effect=[
                Result(0, "a1b2c3", "", "exited", 0.01),
                Result(0, "", "", "exited", 0.01)]) as invoke:
            self.assertIsNone(sandbox.remove_leftover(config, "mizu-test", {}))
        self.assertEqual([call.kwargs["timeout"] for call in invoke.call_args_list], [7, 7])

        with patch("mizu.sandbox.run", side_effect=[
                Result(0, "a1b2c3\ndef456", "", "exited", 0.01),
                Result(0, "", "", "exited", 0.01)]) as invoke:
            self.assertEqual(sandbox._remove_containers(config, self.project.root, None),
                             {"removed": 2})
        self.assertEqual([call.kwargs["timeout"] for call in invoke.call_args_list], [7, 19])

        run_dir = self.project.root / "runs" / "cleanup-timeout-test"
        (run_dir / "commands").mkdir(parents=True)
        executor = sandbox.Sandbox(config, self.project.root, "worker", run_dir)
        with patch("mizu.sandbox.run", side_effect=[
                Result(0, "{}", "", "exited", 0.01),
                Result(0, "ok", "", "exited", 0.01),
                Result(0, "", "", "exited", 0.01)]) as invoke:
            executor.execute(self.project.workspace, "true", writable=True)
        self.assertEqual([call.kwargs["timeout"] for call in invoke.call_args_list],
                         [15, config.limits.command_seconds, 7])


if __name__ == "__main__":
    import unittest
    raise SystemExit(unittest.main())
