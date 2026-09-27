import concurrent.futures
import dataclasses
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from support import Fixture, ROOT
from mizu.budget import Budget
from mizu.config import load
from mizu.errors import Busy, ConfigError, Denied, LimitExceeded
from mizu.fs import atomic_write, canonical, digest, lock, relative_parts, safe_read
from mizu.pi import credentials
from mizu.process import environment, run


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_atomic_replace_and_private_mode(self):
        path = self.root / "a/b.txt"
        atomic_write(path, b"one")
        atomic_write(path, b"two")
        self.assertEqual(path.read_bytes(), b"two")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(list(path.parent.glob(".new-*")))

    def test_immutable_idempotence_and_conflict(self):
        path = self.root / "record"
        atomic_write(path, b"first", exclusive=True)
        atomic_write(path, b"first", exclusive=True)
        with self.assertRaises(Denied):
            atomic_write(path, b"other", exclusive=True)

    def test_bad_relative_paths(self):
        for path in ("/etc/passwd", "../x", "a/../x", "a//x", "", ".", "x\\y", "x\x00y"):
            with self.subTest(path=path), self.assertRaises(Denied):
                relative_parts(path)

    def test_safe_read_regular(self):
        (self.root / "x").write_bytes("unicode\u2028value".encode())
        self.assertEqual(safe_read(self.root, "x", 100), "unicode\u2028value".encode())

    def test_read_size_bound(self):
        (self.root / "x").write_bytes(b"12345")
        with self.assertRaises(LimitExceeded):
            safe_read(self.root, "x", 4)

    def test_symlink_file_denied(self):
        real = self.root / "real.txt"
        real.write_bytes(b"secret")
        (self.root / "x").symlink_to(real)
        with self.assertRaises(Denied):
            safe_read(self.root, "x", 100000)

    def test_symlink_parent_denied(self):
        target = self.root / "target-dir"
        target.mkdir()
        (target / "passwd").write_bytes(b"secret")
        (self.root / "link").symlink_to(target, target_is_directory=True)
        with self.assertRaises(Denied):
            safe_read(self.root, "link/passwd", 100000)

    def test_hardlink_denied(self):
        (self.root / "one").write_text("data")
        os.link(self.root / "one", self.root / "two")
        with self.assertRaises(Denied):
            safe_read(self.root, "two", 100)

    def test_fifo_nonblocking_denied(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs require POSIX")
        os.mkfifo(self.root / "pipe")
        before = time.monotonic()
        with self.assertRaises(Denied):
            safe_read(self.root, "pipe", 100)
        self.assertLess(time.monotonic() - before, 1)

    def test_lock_contention_and_recovery(self):
        path = self.root / "lock"
        with lock(path):
            with self.assertRaises(Busy):
                with lock(path, blocking=False):
                    pass
        inode = path.stat().st_ino
        with lock(path, blocking=False):
            self.assertEqual(path.stat().st_ino, inode)

    def test_canonical_stable(self):
        self.assertEqual(canonical({"z": 1, "a": 2}), canonical({"a": 2, "z": 1}))
        with self.assertRaises(ValueError):
            canonical({"x": float("nan")})


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unconfigured_budget_denies(self):
        with self.assertRaises(LimitExceeded):
            Budget(self.root, 0).take("one")

    def test_duplicate_admission_not_double_charged(self):
        budget = Budget(self.root, 1)
        self.assertEqual(budget.take("same"), 1)
        self.assertEqual(budget.take("same"), 1)
        with self.assertRaises(LimitExceeded):
            budget.take("different")

    def test_day_rollover(self):
        budget = Budget(self.root, 1)
        budget.take("one", day="2025-01-01")
        self.assertEqual(budget.take("two", day="2025-01-02"), 1)

    def test_concurrent_admission_never_exceeds_limit(self):
        def attempt(i):
            try:
                Budget(self.root, 12).take(str(i))
                return 1
            except LimitExceeded:
                return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(40))), 12)
        self.assertEqual(Budget(self.root, 12).usage()["used"], 12)

    def test_old_day_files_are_collected(self):
        budget = Budget(self.root, 100)
        (self.root / "2000-01-01.json").write_text('{"day": "2000-01-01", "requests": ["old"]}')
        (self.root / "not-a-day.json").write_text("{}")
        self.assertEqual(budget.gc(), 1)
        self.assertFalse((self.root / "2000-01-01.json").exists())
        self.assertTrue((self.root / "not-a-day.json").exists())

    def test_day_file_byte_bound(self):
        import json
        budget = Budget(self.root, 100000)
        (self.root / "2000-01-01.json").write_text(json.dumps({"day": "2000-01-01",
            "requests": ["x" * 30] * 150000}))
        with self.assertRaises(LimitExceeded):
            budget.take("new", day="2000-01-01")

    def test_limit_is_exact_not_plus_one(self):
        budget = Budget(self.root, 2)
        self.assertEqual(budget.take("a"), 1)
        self.assertEqual(budget.take("b"), 2)
        with self.assertRaises(LimitExceeded):
            budget.take("c")
        self.assertEqual(budget.usage()["used"], 2)

    def test_doctor_flag_match_is_token_anchored(self):
        from mizu.doctor import help_has_flag
        self.assertTrue(help_has_flag("--print-all\n  -p, --print", "-p"))
        self.assertFalse(help_has_flag("Options:\n  --print-all", "-p"))
        self.assertTrue(help_has_flag("--mcp-config PATH", "--mcp-config"))


class ConfigTests(Fixture):
    def test_strict_unknown_setting(self):
        self.file.write_text("unknown = true\n" + self.file.read_text())
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_bad_scalar_type(self):
        self.file.write_text(self.file.read_text().replace("requests_per_run = 24", "requests_per_run = true"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_unknown_role_capability(self):
        self.file.write_text(self.file.read_text().replace('"decide"', '"host_root_shell"'))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_mutable_image_tag_refused(self):
        self.file.write_text(self.file.read_text().replace("sha256:" + "a" * 64, "image:latest"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_model_resolution_is_explicit(self):
        self.assertEqual(self.config.model("primary")["model"], "test-model")
        with self.assertRaises(ConfigError):
            self.config.model("unknown")

    def test_configuration_is_not_overwritten(self):
        from mizu.cli import configure
        before = self.file.read_bytes()
        self.assertEqual(configure(self.file, None)["status"], "unchanged")
        self.assertEqual(self.file.read_bytes(), before)

    def test_init_defaults_to_one_unarmed_worker(self):
        from mizu.cli import parser
        args = parser().parse_args(["init", "demo", "--source", "s", "--goal", "g"])
        self.assertEqual(args.roles, "worker")
        self.assertFalse(args.armed)

    def test_smoke_role_defaults_to_consult(self):
        from mizu.cli import parser
        args = parser().parse_args(["smoke", "--live"])
        self.assertEqual(args.role, "consult")

    def test_history_and_prompt_bounds_are_validated(self):
        self.file.write_text(self.file.read_text().replace("history_index = 128", "history_index = 2"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_daily_requests_cap_bounds_day_file(self):
        self.file.write_text(self.file.read_text().replace("daily_requests = 100", "daily_requests = 1000001"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_credentials_are_literal(self):
        path = self.file.parent / "credentials.env"
        path.write_text("API_KEY='$(touch should-not-exist)'\n")
        path.chmod(0o600)
        self.assertEqual(credentials(path)["API_KEY"], "$(touch should-not-exist)")
        self.assertFalse((self.file.parent / "should-not-exist").exists())

    def test_unknown_timezone_is_refused(self):
        self.file.write_text(self.file.read_text().replace('timezone = "UTC"', 'timezone = "Mars/Olympus"'))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_utc_needs_no_database(self):
        import datetime
        import mizu.config as config_mod
        from zoneinfo import ZoneInfoNotFoundError
        from mizu.config import resolve_timezone
        self.assertEqual(str(resolve_timezone("UTC")), "UTC")
        def no_database(name):
            raise ZoneInfoNotFoundError(name)
        with patch.object(config_mod, "ZoneInfo", side_effect=no_database):
            self.assertEqual(resolve_timezone("UTC"), datetime.timezone.utc)
            with self.assertRaises(ConfigError) as ctx:
                resolve_timezone("Mars/Olympus")
            self.assertIn("database", str(ctx.exception))
            with self.assertRaises(ConfigError) as ctx:
                resolve_timezone("America/New_York")
            self.assertIn("database", str(ctx.exception))

    def test_credentials_permissions_and_injection(self):
        from mizu import platform as _platform
        path = self.file.parent / "credentials.env"
        path.write_text("API_KEY=example\n")
        if not _platform.IS_WINDOWS:
            path.chmod(0o644)
            with self.assertRaises(ConfigError):
                credentials(path)
            path.chmod(0o600)
        path.write_text("NODE_OPTIONS=--require=malicious.js\n")
        with self.assertRaises(ConfigError):
            credentials(path)

    def test_wait_bounds_match_protocol(self):
        from mizu.config import MAX_WAIT_SECONDS
        from mizu.protocol import DEFINITIONS
        self.assertEqual(DEFINITIONS["finish"][1]["properties"]["wait_seconds"]["maximum"], MAX_WAIT_SECONDS)
        self.file.write_text(self.file.read_text().replace("maximum_wait_seconds = 86400", "maximum_wait_seconds = 86401"))
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_preview_bound_is_shared(self):
        from mizu.fs import PREVIEW_BYTES
        from mizu import __version__
        from mizu.editor import TOOLS  # noqa: F401 (capsule bound stays domain-local)
        self.assertEqual(PREVIEW_BYTES, 131072)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_pinned_versions_agree(self):
        import json
        from mizu import NODE_MINIMUM, PI_MINIMUM, __version__
        minimum = tuple(map(int, PI_MINIMUM.split(".")))
        compat = json.loads((ROOT / "adapters/pi/compatibility.json").read_text())
        self.assertEqual((compat["pi_minimum"], compat["node_minimum"]), (PI_MINIMUM, NODE_MINIMUM))
        package = json.loads((ROOT / "adapters/pi/package.json").read_text())
        self.assertEqual(package["version"], __version__)
        for name in ("@earendil-works/pi-coding-agent", "@earendil-works/pi-ai"):
            declared = package["dependencies"][name]
            self.assertTrue(declared.startswith(">=") and PI_MINIMUM in declared)
        self.assertIn(NODE_MINIMUM, package["engines"]["node"])
        lock = json.loads((ROOT / "adapters/pi/package-lock.json").read_text())
        self.assertEqual(lock["version"], __version__)
        self.assertEqual(lock["packages"][""]["version"], __version__)
        for name in ("@earendil-works/pi-coding-agent", "@earendil-works/pi-ai"):
            pinned = tuple(map(int, lock["packages"]["node_modules/" + name]["version"].split(".")))
            self.assertGreaterEqual(pinned, minimum)

    def test_shared_bounds_are_single_sourced(self):
        import mizu.bridge
        import mizu.editor
        import mizu.mcp_proxy
        import mizu.protocol
        import mizu.sandbox
        from mizu import fs
        self.assertIs(mizu.bridge.MAX_FRAME, fs.MAX_FRAME)
        self.assertIs(mizu.mcp_proxy.MAX_FRAME, fs.MAX_FRAME)
        self.assertIs(mizu.protocol.SCRIPT_MAX, fs.SCRIPT_MAX)
        self.assertIs(mizu.sandbox.SCRIPT_MAX, fs.SCRIPT_MAX)
        self.assertIs(mizu.editor.MCP_VERSIONS, mizu.protocol.MCP_VERSIONS)
        self.assertIs(mizu.mcp_proxy.MCP_VERSIONS, mizu.protocol.MCP_VERSIONS)
        from mizu.drivers import DIAGNOSTICS_TAIL_BYTES, EVENT_STREAM_BYTES
        import mizu.claude
        import mizu.codex
        self.assertIs(mizu.codex.DIAGNOSTICS_TAIL_BYTES, DIAGNOSTICS_TAIL_BYTES)
        self.assertIs(mizu.claude.EVENT_STREAM_BYTES, EVENT_STREAM_BYTES)

    def test_verify_commands_are_counted_and_toml_safe(self):
        from mizu.project import check_verify
        with self.assertRaises(ConfigError):
            check_verify([f"true # {n}" for n in range(33)])
        with self.assertRaises(ConfigError):
            check_verify(["echo hi\necho lo"])
        self.assertEqual(check_verify(["make check"]), ("make check",))

    def test_profile_values_reject_newlines(self):
        text = self.file.read_text().replace('model = "test-model"', 'model = "a\\nb"')
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)

    def test_exit_codes_distinguish_busy_and_budget(self):
        from mizu.errors import Busy, LimitExceeded, ProtocolError
        self.assertNotEqual(Busy.code, LimitExceeded.code)
        from mizu.services import render
        units = render(self.config, self.project, ROOT / "bin/mizu", system="linux")
        service = next(v for k, v in units.items() if v.startswith("[Unit]") or "Service" in v)
        for code in (Busy.code, LimitExceeded.code, ProtocolError.code):
            self.assertIn(str(code), service)


class CheckBashTests(unittest.TestCase):
    def load_check(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import check
            return check
        finally:
            sys.path.remove(str(ROOT / "scripts"))

    def test_missing_bash_is_unavailable(self):
        check = self.load_check()
        with patch("shutil.which", return_value=None):
            self.assertFalse(check.bash_available())

    def test_broken_launcher_is_unavailable(self):
        import subprocess
        check = self.load_check()
        broken = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"no distributions")
        with patch("shutil.which", return_value="bash"), \
             patch("subprocess.run", return_value=broken):
            self.assertFalse(check.bash_available())

    def test_working_bash_is_available(self):
        import subprocess
        check = self.load_check()
        working = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"GNU bash", stderr=b"")
        with patch("shutil.which", return_value="/bin/bash"), \
             patch("subprocess.run", return_value=working):
            self.assertTrue(check.bash_available())


class ProcessTests(unittest.TestCase):
    def test_output_and_status(self):
        result = run([sys.executable, "-c", "print('ok')"], timeout=5, maximum=100)
        self.assertEqual((result.exit_code, result.stdout, result.reason), (0, "ok\n", "exited"))

    def test_stdin_pump(self):
        text = b"x" * 200000
        result = run([sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
                     timeout=5, maximum=100, input_data=text)
        self.assertEqual(result.stdout.strip(), "200000")

    def test_output_bound(self):
        result = run([sys.executable, "-c", "print('x'*1000000)"], timeout=5, maximum=1000)
        self.assertEqual(result.reason, "output_limit")
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 1000)

    def test_timeout(self):
        start = time.monotonic()
        result = run([sys.executable, "-c", "import time; time.sleep(20)"], timeout=.15, maximum=1000)
        self.assertEqual(result.reason, "timeout")
        self.assertLess(time.monotonic() - start, 3)

    def test_cancellation(self):
        result = run([sys.executable, "-c", "import time; time.sleep(20)"], timeout=5, maximum=100, cancel=lambda: True)
        self.assertEqual(result.reason, "cancelled")

    def test_environment_does_not_leak_keys(self):
        with patch.dict(os.environ, {"SECRET_API_KEY": "never-forward", "NODE_OPTIONS": "evil"}):
            self.assertNotIn("SECRET_API_KEY", environment())
            self.assertNotIn("NODE_OPTIONS", environment())

    def test_orphan_pipe_is_bounded(self):
        if not hasattr(os, "fork"):
            self.skipTest("Orphan reparenting requires POSIX fork")
        code = "import os,time; pid=os.fork(); time.sleep(30) if pid==0 else None"
        result = run([sys.executable, "-c", code], timeout=.2, maximum=100)
        self.assertEqual(result.reason, "timeout")
