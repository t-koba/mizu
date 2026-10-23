"""Per-role event-only recovery poll override: config, helper and rendering bounds."""
import dataclasses
import unittest

from support import Fixture
from mizu.config import (
    EVENT_POLL_SECONDS_DEFAULT,
    EVENT_POLL_SECONDS_MAX,
    EVENT_POLL_SECONDS_MIN,
    load,
    role_event_poll_seconds,
)
from mizu.errors import ConfigError


class EventPollConfigTests(Fixture):
    def test_bounds_match_task_scheduler_minimum(self):
        self.assertEqual(EVENT_POLL_SECONDS_DEFAULT, 60)
        self.assertEqual(EVENT_POLL_SECONDS_MIN, 60)
        self.assertEqual(EVENT_POLL_SECONDS_MAX, 31 * 86400)

    def test_default_is_none_and_effective_is_sixty(self):
        role = self.config.roles["worker"]
        self.assertIsNone(role.event_poll_seconds)
        self.assertEqual(role_event_poll_seconds(role), 60)

    def test_override_applies(self):
        config = dataclasses.replace(
            self.config,
            roles={**self.config.roles,
                   "worker": dataclasses.replace(self.config.roles["worker"], event_poll_seconds=300)})
        self.assertEqual(role_event_poll_seconds(config.roles["worker"]), 300)
        # Other roles still use the fixed default.
        self.assertEqual(role_event_poll_seconds(config.roles["searcher"]), 60)

    def test_malformed_override_reads_as_default(self):
        role = dataclasses.replace(self.config.roles["worker"], event_poll_seconds=0)
        self.assertEqual(role_event_poll_seconds(role), 60)
        role = dataclasses.replace(self.config.roles["worker"], event_poll_seconds="300")
        self.assertEqual(role_event_poll_seconds(role), 60)

    def test_role_validation(self):
        original = self.file.read_text()
        for value, valid in (("60", True), ("300", True), ("2678400", True),
                             ("59", False), ("2678401", False), ("0", False),
                             ("true", False), ('"300"', False), ("2.5", False)):
            with self.subTest(value=value):
                self.file.write_text(
                    original.replace("[roles.worker]",
                                     f"[roles.worker]\nevent_poll_seconds = {value}"))
                if valid:
                    self.assertEqual(load(self.file).roles["worker"].event_poll_seconds, int(value))
                else:
                    with self.assertRaises(ConfigError):
                        load(self.file)
        with self.subTest(unknown="unknown-key"):
            self.file.write_text(
                original.replace("[roles.worker]",
                                 "[roles.worker]\nevent_poll_seconds_typo = 60"))
            with self.assertRaises(ConfigError):
                load(self.file)

    def test_example_config_documents_override(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "config" / "config.example.toml").read_text()
        self.assertIn("event_poll_seconds", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
