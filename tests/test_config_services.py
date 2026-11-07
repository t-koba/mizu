"""Systemd deployment and validation settings preserve defaults and reject invalid values."""
import dataclasses
import subprocess
import sys
from pathlib import Path

from support import Fixture
from mizu.config import (MAX_SYSTEMD_VERIFY_TIMEOUT_SECONDS, ServiceSettings,
                         TOML_INTEGER_MAX, load)
from mizu.errors import ConfigError


class ServiceSettingsTests(Fixture):
    def test_defaults_and_positive_integer_overrides(self):
        expected = {
            "systemd_restart_delay_seconds": 15,
            "systemd_stop_timeout_seconds": 45,
            "systemd_verify_timeout_seconds": 60,
        }
        self.assertEqual(dataclasses.asdict(self.config.services), expected)
        self.assertEqual(dataclasses.asdict(ServiceSettings()), expected)

        original = self.file.read_text()
        for name in expected:
            maximum = (MAX_SYSTEMD_VERIFY_TIMEOUT_SECONDS
                       if name == "systemd_verify_timeout_seconds" else TOML_INTEGER_MAX)
            for value in (1, expected[name], maximum):
                with self.subTest(name=name, value=value):
                    self.file.write_text(original + f"\n[services]\n{name} = {value}\n")
                    configured = getattr(load(self.file).services, name)
                    self.assertEqual(configured, value)
            invalid_values = [0, -1, "true", "false", "1.5", '"1"', "[]", "{}"]
            if name == "systemd_verify_timeout_seconds":
                invalid_values.extend((maximum + 1, TOML_INTEGER_MAX))
            for value in invalid_values:
                with self.subTest(name=name, invalid=value):
                    self.file.write_text(original + f"\n[services]\n{name} = {value}\n")
                    with self.assertRaisesRegex(ConfigError, f"services.{name}"):
                        load(self.file)
        self.file.write_text(original)
        self.assertEqual(load(self.file).services, ServiceSettings())

    def test_maximum_timeout_is_representable_by_subprocess(self):
        result = subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            capture_output=True, timeout=MAX_SYSTEMD_VERIFY_TIMEOUT_SECONDS)
        self.assertEqual(result.returncode, 0)

    def test_unknown_keys_and_non_table_are_refused(self):
        original = self.file.read_text()
        for text in (original + "\n[services]\nrestart_delay_seconds = 15\n",
                     "services = 1\n" + original,
                     "services = []\n" + original):
            with self.subTest(text=text[-48:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, "services"):
                    load(self.file)

    def test_example_documents_the_optional_systemd_defaults(self):
        example = Path(__file__).resolve().parents[1] / "config" / "config.example.toml"
        text = example.read_text(encoding="utf-8")
        self.assertIn("# [services]", text)
        self.assertIn("# systemd_restart_delay_seconds = 15", text)
        self.assertIn("# systemd_stop_timeout_seconds = 45", text)
        self.assertIn("# systemd_verify_timeout_seconds = 60", text)


if __name__ == "__main__":
    import unittest
    raise SystemExit(unittest.main())
