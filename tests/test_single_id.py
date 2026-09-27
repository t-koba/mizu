"""Single-ID sandbox mode: explicit configuration, strict argv, loud refusals."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture

from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.sandbox import Sandbox, ensure_single, runtime_base

ROOT = Path(__file__).resolve().parents[1]

#: Portable executable helper for single-mode config tests. /bin/true does not
#: exist on Windows; the interpreter itself always does.
HELPER = sys.executable.replace("\\", "/")


def single_text(text):
    assert 'mode = "rootless"' in text
    text = text.replace('mode = "rootless"', 'mode = "single"', 1)
    anchor = "[sandbox]\n"
    addition = f"""[sandbox]
namespace_helper = ["{HELPER}"]
podman_root = "/tmp/mizu-test-storage"
podman_runroot = "/tmp/mizu-test-run"
podman_tmpdir = "/tmp/mizu-test-tmp"
storage_options = ["overlay.ignore_chown_errors=true"]
cgroup_manager = "cgroupfs"
cgroup_parent = "user.slice/user-1000.slice/user@1000.service/mizucg"
"""
    assert anchor in text
    return text.replace(anchor, addition, 1)


class SingleModeConfigTests(Fixture):
    def load_single(self):
        self.file.write_text(single_text(self.file.read_text()))
        return load(self.file)

    def test_single_mode_loads(self):
        config = self.load_single()
        self.assertEqual(config.sandbox.mode, "single")
        self.assertEqual(config.sandbox.namespace_helper, (HELPER,))

    def test_mode_allowlist(self):
        self.file.write_text(self.file.read_text().replace('mode = "rootless"', 'mode = "orbital"'))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_single_requires_helper(self):
        text = single_text(self.file.read_text()).replace(f'namespace_helper = ["{HELPER}"]\n', "")
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_single_requires_paths(self):
        text = single_text(self.file.read_text()).replace('podman_root = "/tmp/mizu-test-storage"\n', "")
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_single_requires_cgroupfs(self):
        text = single_text(self.file.read_text()).replace('cgroup_manager = "cgroupfs"', 'cgroup_manager = "systemd"')
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_rootless_refuses_single_keys(self):
        text = self.file.read_text().replace("[sandbox]\n", f'[sandbox]\nnamespace_helper = ["{HELPER}"]\n')
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_helper_must_be_absolute(self):
        text = single_text(self.file.read_text()).replace(f'["{HELPER}"]', '["relative/helper"]')
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_storage_option_format(self):
        text = single_text(self.file.read_text()).replace("overlay.ignore_chown_errors=true", "no-equals-here")
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_cgroup_parent_slice_suffix_refused(self):
        text = single_text(self.file.read_text()).replace("service/mizucg", "service/mizu.slice")
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)


class SingleModeArgvTests(Fixture):
    def single_config(self):
        self.file.write_text(single_text(self.file.read_text()))
        return load(self.file)

    def test_single_argv_shape(self):
        config = self.single_config()
        engine = Sandbox(config, self.root, "worker", self.root / "run")
        with patch("mizu.sandbox.ensure_single", return_value={}):
            args = engine.argv("n", self.root, "echo hi", writable=True)
        self.assertEqual(args[0], HELPER)
        self.assertIn("--root", args)
        self.assertIn("--uidmap", args)
        self.assertEqual(args[args.index("--uidmap") + 1], "0:0:1")
        self.assertEqual(args[args.index("--gidmap") + 1], "0:0:1")
        self.assertEqual(args[args.index("--user") + 1], "0:0")
        self.assertEqual(args[args.index("--cgroup-parent") + 1], config.sandbox.cgroup_parent)
        self.assertIn("--storage-opt", args)
        self.assertNotIn("--userns", " ".join(args))
        self.assertEqual(args[-3:], [config.sandbox.image, "-c", "echo hi"])

    def test_rootless_argv_uses_portable_user(self):
        from mizu import platform as _platform
        engine = Sandbox(self.config, self.root, "worker", self.root / "run")
        args = engine.argv("n", self.root, "echo hi", writable=True)
        self.assertNotIn("--root", args)
        self.assertNotIn("--uidmap", args)
        self.assertNotIn("--userns", " ".join(args))
        self.assertIn(_platform.container_user(), args)

    def test_missing_helper_refused(self):
        config = self.single_config()
        missing = list(config.sandbox.namespace_helper)
        missing[0] = "/nonexistent-namespace-helper"
        config = config.__class__(**{**config.__dict__, "sandbox": config.sandbox.__class__(
            **{**config.sandbox.__dict__, "namespace_helper": tuple(missing)})})
        with self.assertRaises(Denied):
            runtime_base(config)

    def test_ensure_refuses_missing_parent(self):
        config = self.single_config()
        missing = config.sandbox.__class__(
            **{**config.sandbox.__dict__, "cgroup_parent": "definitely-not-here/mizucg"})
        config = config.__class__(**{**config.__dict__, "sandbox": missing})
        with self.assertRaises(Denied):
            ensure_single(config)

    def test_ensure_memoizes_by_composite_key(self):
        import unittest.mock
        import mizu.sandbox as sandbox_mod
        sandbox_mod._PREPARED_SINGLE.clear()
        self.addCleanup(sandbox_mod._PREPARED_SINGLE.clear)
        config = self.single_config()
        calls = {"mkdir": 0, "read": 0}
        def counting_mkdir(path):
            calls["mkdir"] += 1
        def counting_read(self):
            calls["read"] += 1
            return "cpu memory pids"
        with patch.object(sandbox_mod, "mkdir", counting_mkdir), \
             patch("pathlib.Path.is_dir", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True), \
             patch("pathlib.Path.read_text", counting_read):
            ensure_single(config)
            self.assertEqual(len(sandbox_mod._PREPARED_SINGLE), 1)
            key = next(iter(sandbox_mod._PREPARED_SINGLE))
            self.assertIn("|", key)  # Composite key; a bare slot name means shadowing.
            calls.update(mkdir=0, read=0)
            second = ensure_single(config)
            self.assertEqual(second["prepared"], [])
            self.assertEqual(calls, {"mkdir": 0, "read": 0})


class SecretProbeTests(unittest.TestCase):
    SNIPPET = """if env | grep -qE '(^|_)(API_KEY|TOKEN|SECRET)='; then
  echo 'container inherited API secrets' >&2
  exit 1
fi
"""

    def run_probe(self, extra_env):
        import shutil
        if not shutil.which("sh"):
            self.skipTest("Secret probe requires POSIX sh")
        env = {"PATH": "/usr/bin:/bin"}
        env.update(extra_env)
        return subprocess.run(["sh", "-c", "set -eu\n" + self.SNIPPET + "echo clean"],
                              capture_output=True, text=True, env=env)

    def test_clean_environment_passes(self):
        result = self.run_probe({})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "clean"))

    def test_inherited_secret_fails_loudly(self):
        result = self.run_probe({"API_KEY": "leaked"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inherited API secrets", result.stderr)


class WrapperTests(unittest.TestCase):
    def test_wrapper_usage_without_command(self):
        from mizu import platform as _platform
        if not _platform.IS_LINUX:
            self.skipTest("mizu-userns requires Linux user namespaces")
        helper = ROOT / "scripts" / "idmap" / "mizu-userns"
        result = subprocess.run([str(helper)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 64)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
