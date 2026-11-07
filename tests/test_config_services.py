"""Systemd service-manager deadline configuration preserves defaults and rejects invalid values."""
import dataclasses
from pathlib import Path

from support import Fixture
from mizu.config import ServiceSettings, TOML_INTEGER_MAX, load
from mizu.errors import ConfigError


class ServiceSettingsTests(Fixture):
    def test_defaults_and_positive_integer_overrides(self):
        expected = {
            "systemd_restart_delay_seconds": 15,
            "systemd_stop_timeout_seconds": 45,
        }
        self.assertEqual(dataclasses.asdict(self.config.services), expected)
        self.assertEqual(dataclasses.asdict(ServiceSettings()), expected)

        original = self.file.read_text()
        for name in expected:
            for value in (1, expected[name], TOML_INTEGER_MAX):
                with self.subTest(name=name, value=value):
                    self.file.write_text(original + f"\n[services]\n{name} = {value}\n")
                    configured = getattr(load(self.file).services, name)
                    self.assertEqual(configured, value)
            for value in (0, -1, "true", "false", "1.5", '"1"', "[]", "{}"):
                with self.subTest(name=name, invalid=value):
                    self.file.write_text(original + f"\n[services]\n{name} = {value}\n")
                    with self.assertRaisesRegex(ConfigError, f"services.{name}"):
                        load(self.file)
        self.file.write_text(original)
        self.assertEqual(load(self.file).services, ServiceSettings())

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


if __name__ == "__main__":
    import unittest
    raise SystemExit(unittest.main())
