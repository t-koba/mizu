"""Claude adapter installer version-floor parsing tests. Offline only."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_claude_adapter", ROOT / "scripts" / "install-claude-adapter.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class ClaudeInstallerPipVersionTests(unittest.TestCase):
    def _parse(self, version, *, command="pip"):
        output = f"{command} {version} from /tmp/site-packages/pip (python 3.13)"
        with patch.object(INSTALLER.subprocess, "check_output", return_value=output):
            return INSTALLER._pip_version(Path("/fake/python"))

    def test_stable_floor_and_adjacent_releases(self):
        self.assertLess(self._parse("26.1.1"), INSTALLER.PIP_MINIMUM)
        self.assertEqual(self._parse("26.1.2"), INSTALLER.PIP_MINIMUM)
        self.assertEqual(self._parse("26.1.2.0"), INSTALLER.PIP_MINIMUM)
        self.assertGreater(self._parse("26.1.2.post1"), INSTALLER.PIP_MINIMUM)
        self.assertGreater(self._parse("26.2.0"), INSTALLER.PIP_MINIMUM)

    def test_prerelease_and_dev_releases_are_rejected(self):
        for version in ("26.1.2rc1", "26.1.2.dev0", "26.2.0b1", "26.2.0a1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self._parse(version)

    def test_local_and_unrecognized_versions_fail_closed(self):
        for version in ("26.1.2+vendor", "26.1.2.post1.dev0", "unknown"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self._parse(version)

    def test_output_must_be_pip_version_output(self):
        with self.assertRaises(ValueError):
            self._parse("26.1.2", command="not-pip")

    def test_malformed_command_output_fails_closed(self):
        for output in ("", "pip", "pip version from somewhere"):
            with self.subTest(output=output), patch.object(
                    INSTALLER.subprocess, "check_output", return_value=output):
                with self.assertRaises(ValueError):
                    INSTALLER._pip_version(Path("/fake/python"))


if __name__ == "__main__":
    unittest.main()
