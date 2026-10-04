"""Config `include` of shared TOML fragments (M3). Offline, stdlib-only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError


def _strip_line(text: str, needle: str) -> str:
    lines = [ln for ln in text.splitlines(keepends=True) if needle not in ln]
    return "".join(lines)


class IncludeTests(Fixture):
    def _split_idle(self, frag_name="shared.toml"):
        """Move idle_seconds out of the main file into a fragment."""
        text = self.file.read_text()
        assert "idle_seconds = 15" in text
        text = _strip_line(text, "idle_seconds = 15")
        text = f'include = ["{frag_name}"]\n' + text
        self.file.write_text(text)
        frag = self.file.parent / frag_name
        frag.write_text("[limits]\nidle_seconds = 15\n")
        return frag

    def test_include_merges_disjoint_fragment(self):
        self._split_idle()
        config = load(self.file)
        self.assertEqual(config.limits.idle_seconds, 15)

    def test_nested_include_resolves_relative_to_includer(self):
        subdir = self.file.parent / "nested"
        subdir.mkdir()
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["nested/mid.toml"]\n' + text
        self.file.write_text(text)
        (subdir / "mid.toml").write_text('include = ["leaf.toml"]\n')
        (subdir / "leaf.toml").write_text("[limits]\nidle_seconds = 17\n")
        config = load(self.file)
        self.assertEqual(config.limits.idle_seconds, 17)

    def test_duplicate_leaf_names_both_files(self):
        frag = self._split_idle()
        text = self.file.read_text().replace("[limits]", "[limits]\nidle_seconds = 15", 1)
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        message = str(ctx.exception)
        self.assertIn("limits.idle_seconds", message)
        self.assertIn(str(frag), message)
        self.assertIn(str(self.file.resolve()), message)

    def test_absolute_include_refused(self):
        text = 'include = ["/etc/hosts.toml"]\n' + self.file.read_text()
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("relative", str(ctx.exception))

    def test_symlink_escape_refused(self):
        outside = self.root / "outside.toml"
        outside.write_text("[limits]\nidle_seconds = 15\n")
        link = self.file.parent / "link.toml"
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks require privileges on this platform")
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["link.toml"]\n' + text
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("symlink", str(ctx.exception))

    def test_symlink_directory_refused(self):
        real = self.root / "realdir"
        real.mkdir()
        (real / "shared.toml").write_text("[limits]\nidle_seconds = 15\n")
        linkdir = self.file.parent / "linkdir"
        if linkdir.exists() or linkdir.is_symlink():
            import shutil
            if linkdir.is_symlink():
                linkdir.unlink()
            else:
                shutil.rmtree(linkdir)
        try:
            linkdir.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks require privileges on this platform")
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["linkdir/shared.toml"]\n' + text
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("symlink", str(ctx.exception))

    def test_cycle_refused(self):
        a = self.file.parent / "a.toml"
        b = self.file.parent / "b.toml"
        a.write_text('include = ["b.toml"]\n[limits]\nidle_seconds = 15\n')
        b.write_text('include = ["a.toml"]\n')
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["a.toml"]\n' + text
        self.file.write_text(text)
        # Break the idle duplicate so the cycle (not the duplicate) fires:
        # a.toml already carries idle_seconds; main no longer does.
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_depth_bound_refused(self):
        import mizu.config as config_mod
        depth = config_mod.MAX_INCLUDE_DEPTH
        # Build a chain one deeper than allowed.
        prev = None
        for i in range(depth + 2):
            name = f"chain{i}.toml"
            path = self.file.parent / name
            if i == depth + 1:
                path.write_text("[limits]\nidle_seconds = 15\n")
            else:
                path.write_text(f'include = ["chain{i + 1}.toml"]\n')
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["chain0.toml"]\n' + text
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("depth", str(ctx.exception).lower())

    def test_size_bound_refused(self):
        import mizu.config as config_mod
        big = self.file.parent / "big.toml"
        big.write_text("[limits]\nidle_seconds = 15\n# " + ("x" * 2048) + "\n")
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["big.toml"]\n' + text
        self.file.write_text(text)
        with patch.object(config_mod, "MAX_INCLUDE_BYTES", 10):
            with self.assertRaises(ConfigError) as ctx:
                load(self.file)
        self.assertIn("bytes", str(ctx.exception).lower())

    def test_env_substitution_unchanged(self):
        # ${VAR} in values stays fail-closed after includes land.
        # data_dir is expanded at load time (path_value -> expand).
        self._split_idle()
        import re
        text = self.file.read_text()
        text = re.sub(r'data_dir = ".*"', 'data_dir = "${MIZU_TEST_MISSING_VAR}/x"', text, count=1)
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("MIZU_TEST_MISSING_VAR", str(ctx.exception))
        # And include entries themselves are literal: no expansion.
        text2 = self.file.read_text().replace('include = ["shared.toml"]', 'include = ["${MIZU_TEST_MISSING_VAR}.toml"]')
        self.file.write_text(text2)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_unknown_keys_still_fail(self):
        frag = self.file.parent / "bad.toml"
        frag.write_text("[limits]\nidle_seconds = 15\nnope = 1\n")
        text = _strip_line(self.file.read_text(), "idle_seconds = 15")
        text = 'include = ["bad.toml"]\n' + text
        self.file.write_text(text)
        with self.assertRaises(ConfigError) as ctx:
            load(self.file)
        self.assertIn("Unknown keys", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
