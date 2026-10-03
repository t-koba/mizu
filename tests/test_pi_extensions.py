"""Contract tests for policy-selected Pi extensions.

Extensions are operator-trusted code chosen by policy: the mechanism only
verifies selection (paths, pins, per-run hash) and loads them explicitly.
Nothing here executes an extension; live behavior stays a separate gate.
"""
import hashlib
import unittest
from support import Fixture
from mizu.config import load
from mizu.errors import ConfigError
from mizu.fs import mkdir
from mizu.engine_config import effective

BODY = b"// reviewed extension\n"


def write_ext(fixture, name="s.mjs", body=BODY):
    path = fixture.root / "config" / "extensions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def load_with(fixture, block, name="ext.toml"):
    path = fixture.root / "config" / name
    path.write_text(fixture.file.read_text() + "\n" + block)
    return load(path)


def block_for(path, sha):
    return ('[[profiles.primary.resources]]\nkind = "extension"\npath = "%s"\nsha256 = "%s"\n'
            % (path, sha))


class PiExtensionConfigTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ext = write_ext(self)
        self.sha = hashlib.sha256(BODY).hexdigest()
        self.block = block_for("extensions/s.mjs", self.sha)

    def test_default_is_empty(self):
        self.assertEqual(self.config.profiles["primary"]["resources"], [])

    def test_valid_entry_loads(self):
        config = load_with(self, self.block)
        self.assertEqual(len(config.profiles["primary"]["resources"]), 1)
        self.assertEqual(config.profiles["primary"]["resources"][0]["sha256"], self.sha)
        from pathlib import Path as _Path
        self.assertTrue(_Path(config.profiles["primary"]["resources"][0]["path"]).as_posix().endswith("extensions/s.mjs"))

    def test_bad_sha_format_rejected(self):
        with self.assertRaises(ConfigError):
            load_with(self, block_for("extensions/s.mjs", "zz"), name="bad-sha.toml")

    def test_missing_file_rejected(self):
        with self.assertRaises(ConfigError):
            load_with(self, block_for("extensions/gone.mjs", self.sha), name="missing.toml")

    def test_symlink_rejected(self):
        link = self.ext.parent / "link.mjs"
        link.symlink_to(self.ext)
        with self.assertRaises(ConfigError):
            load_with(self, block_for("extensions/link.mjs", self.sha), name="link.toml")

    def test_duplicate_path_rejected(self):
        with self.assertRaises(ConfigError):
            load_with(self, self.block + self.block, name="dup.toml")

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            load_with(self, self.block + 'x = 1\n', name="key.toml")

    def test_non_array_rejected(self):
        path = self.root / "config" / "not-array.toml"
        path.write_text(self.file.read_text().replace("[profiles.primary.options]", 'resources = "yes"\n[sandbox]', 1))
        with self.assertRaises(ConfigError):
            load(path)

    def test_limit_enforced(self):
        entries = []
        for i in range(65):
            name = f"e{i}.mjs"
            write_ext(self, name)
            entries.append(block_for("extensions/" + name, self.sha))
        with self.assertRaises(ConfigError):
            load_with(self, "".join(entries), name="many.toml")


class PiExtensionArgvTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ext = write_ext(self)
        self.sha = hashlib.sha256(BODY).hexdigest()
        self.config = load_with(self, block_for("extensions/s.mjs", self.sha))
        self.run_dir = self.project.root / "runs" / ("a" * 32)
        mkdir(self.run_dir)

    def argv(self):
        role = self.config.roles["consult"]
        return effective(self.config, role, "primary")

    def test_explicit_resource_selection(self):
        settings = self.argv()
        self.assertEqual(settings['resources'][0]['kind'], 'extension')
        self.assertEqual(settings['resources'][0]['sha256'], self.sha)

    def test_tampered_file_refuses_before_spawn(self):
        self.ext.write_bytes(b"// tampered\n")
        with self.assertRaises(ConfigError):
            self.argv()

    def test_swapped_symlink_refuses(self):
        self.ext.unlink()
        self.ext.symlink_to("/etc/hostname")
        with self.assertRaises(ConfigError):
            self.argv()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
