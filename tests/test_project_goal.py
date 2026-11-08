"""Project-goal byte policy at normal-read, import and restore boundaries."""
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from support import Fixture
from mizu.errors import ConfigError
from mizu.project import Project, initialize
from mizu.storage import backup, restore


class ProjectGoalLimitTests(Fixture):
    def configured(self, maximum):
        limits = dataclasses.replace(self.config.limits, project_goal_bytes=maximum)
        return dataclasses.replace(self.config, limits=limits)

    def test_default_limit_is_inclusive_and_preserves_existing_ceiling(self):
        path = self.project.root / "PROJECT.md"
        path.write_bytes(b"g" * 65536)
        self.assertEqual(len(self.project.goal.encode("utf-8")), 65536)
        path.write_bytes(b"g" * 65537)
        with self.assertRaisesRegex(ConfigError, "65536 bytes"):
            self.project.goal

    def test_configured_limit_applies_to_project_reads_and_imports(self):
        maximum = 65537
        config = self.configured(maximum)
        content = b"g" * maximum
        goal = self.root / "larger-goal.md"
        goal.write_bytes(content)
        self.project.config = config
        (self.project.root / "PROJECT.md").write_bytes(content)
        self.assertEqual(self.project.goal.encode("utf-8"), content)

        with self.assertRaisesRegex(ConfigError, "65536 bytes"):
            initialize(self.config, "goal-default-rejected", self.root / "source",
                       goal, ["worker"], [])
        imported = initialize(config, "goal-override", self.root / "source",
                              goal, ["worker"], [])
        self.assertEqual(imported.goal.encode("utf-8"), content)

    def test_restore_uses_the_configured_goal_limit(self):
        maximum = 65537
        config = self.configured(maximum)
        content = b"g" * maximum
        self.project.config = config
        self.project.set_control(paused=True)
        (self.project.root / "PROJECT.md").write_bytes(content)
        archive = self.root / "large-goal.tar.gz"
        backup(self.project, archive)

        with self.assertRaisesRegex(ConfigError, "65536 bytes"):
            restore(self.config, "goal-restore-default", archive)
        restore(config, "goal-restore-override", archive)
        restored = Project(config, "goal-restore-override")
        self.assertEqual(restored.goal.encode("utf-8"), content)

    def test_decoded_content_is_rechecked_after_pre_read_stat(self):
        maximum = 65536
        path = self.project.root / "PROJECT.md"
        path.write_bytes(b"g" * (maximum + 1))
        self.project.config = self.configured(maximum)
        original_stat = Path.stat

        def report_in_range(candidate, *args, **kwargs):
            result = original_stat(candidate, *args, **kwargs)
            if candidate == path:
                return SimpleNamespace(st_size=maximum)
            return result

        with patch.object(Path, "stat", autospec=True, side_effect=report_in_range):
            with self.assertRaisesRegex(ConfigError, "65536 bytes"):
                self.project.goal


if __name__ == "__main__":
    unittest.main()
