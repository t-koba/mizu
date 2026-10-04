"""M1 step 1: [vcs] trusted-adapter configuration. Offline only."""
import unittest
from pathlib import Path
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError


class VcsConfigTests(Fixture):
    def test_defaults_keep_existing_configs_loading(self):
        self.assertEqual(self.config.vcs["command"], [])
        self.assertEqual(self.config.vcs["timeout_seconds"], 20)
        self.assertEqual(self.config.vcs["max_bytes"], 524288)

    def test_example_config_documents_vcs(self):
        from support import ROOT
        example = (ROOT / "config/config.example.toml").read_text()
        self.assertIn("[vcs]", example)
        self.assertIn("timeout_seconds", example)
        self.assertIn("max_bytes", example)

    def test_unknown_vcs_keys_fail(self):
        path = self.root / "config/bad-vcs.toml"
        base = self.file.read_text()
        assert "[vcs]" in base
        path.write_text(base.replace("[vcs]", "[vcs]\nunknown_key = 1"))
        with self.assertRaises(ConfigError):
            load(path)

    def test_bad_argv_refused(self):
        for bad in ('command = [""]', 'command = ["ok\nbad"]'):
            path = self.root / "config/bad-argv.toml"
            path.write_text(self.file.read_text() + f'\n[vcs]\n{bad}\n')
            with self.assertRaises(ConfigError, msg=bad):
                load(path)

    def test_bounds_refused(self):
        for key, value in (("timeout_seconds", 0), ("max_bytes", 0),
                           ("timeout_seconds", 16777217), ("max_bytes", 99999999)):
            path = self.root / "config/bad-bound.toml"
            base = self.file.read_text()
            lines = base.splitlines()
            out = []
            in_vcs = False
            for line in lines:
                if line.strip() == "[vcs]":
                    in_vcs = True
                    out.append(line)
                    continue
                if in_vcs and line.startswith(key + " ="):
                    continue
                if line.startswith("[") and in_vcs:
                    in_vcs = False
                    out.append(f"{key} = {value}")
                out.append(line)
            # ensure override present
            text = "\n".join(out)
            if f"{key} = {value}" not in text:
                text = text.replace("[vcs]", f"[vcs]\n{key} = {value}")
            path.write_text(text + "\n")
            with self.assertRaises(ConfigError, msg=f"{key}={value}"):
                load(path)

    def test_valid_vcs_loads(self):
        path = self.root / "config/good-vcs.toml"
        base = self.file.read_text()
        text = base.replace("command = []", 'command = ["vcs-adapter"]', 1)
        text = text.replace("timeout_seconds = 20", "timeout_seconds = 5", 1)
        text = text.replace("max_bytes = 524288", "max_bytes = 1024", 1)
        assert text != base
        path.write_text(text)
        config = load(path)
        self.assertEqual(list(config.vcs["command"]), ["vcs-adapter"])
        self.assertEqual(config.vcs["timeout_seconds"], 5)
        self.assertEqual(config.vcs["max_bytes"], 1024)

    def test_unknown_root_keys_still_fail(self):
        path = self.root / "config/bad-root.toml"
        path.write_text(self.file.read_text() + '\n[unknown_table]\nx = 1\n')
        with self.assertRaises(ConfigError):
            load(path)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
