"""Cross-platform operation: one container mechanism, one schedule model.

These tests must pass on Linux, macOS and Windows without a container
runtime, systemd, bash or root. Container execution itself needs a runtime
(Podman or Docker Desktop) and is covered by argv/record unit tests plus the
explicit `doctor --sandbox` gate; Linux-only constrained-host (single-ID user
namespaces, the userns wrapper) is asserted only via explicit skips.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from support import ROOT

from mizu import platform as _platform


class PlatformContractTests(unittest.TestCase):
    def test_single_source_of_truth(self):
        self.assertIn(_platform.SYSTEM, ("linux", "macos", "windows"))
        self.assertEqual(_platform.IS_LINUX, _platform.SYSTEM == "linux")
        self.assertEqual(_platform.IS_WINDOWS, os.name == "nt" or _platform.SYSTEM == "windows")
        self.assertTrue(_platform.HAS_FLOCK or _platform.HAS_MSVCRT_LOCK or os.name == "nt")

    def test_container_user_is_never_root(self):
        if _platform.is_root():
            self.skipTest("container-user probe needs an unprivileged host uid; root hosts are refused by the run/doctor/smoke guards")
        user = _platform.container_user()
        self.assertNotEqual(user.split(":")[0], "0")
        ids = _platform.uid_gid()
        if ids is not None:
            self.assertEqual(user, f"{ids[0]}:{ids[1]}")

    def test_service_directories_cover_all_platforms(self):
        # Compare path parts, not string suffixes: separators differ per host.
        self.assertEqual(_platform.service_dir("linux").parts[-2:], ("systemd", "user"))
        self.assertEqual(_platform.service_dir("macos").parts[-1:], ("LaunchAgents",))
        self.assertEqual(_platform.service_dir("windows").parts[-1:], ("tasks",))
        with self.assertRaises(ValueError):
            _platform.service_dir("plan9")

    def test_privilege_check_never_crashes(self):
        self.assertIsInstance(_platform.is_root(), bool)

    def test_config_home_respects_override(self):
        with unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp_home())}):
            self.assertEqual(_platform.config_home(), Path(os.environ["XDG_CONFIG_HOME"]))

    def tmp_home(self):
        tmp = tempfile.mkdtemp(prefix="mizu-platform-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return tmp

    def test_popen_kwargs_are_portable(self):
        import subprocess
        kwargs = _platform.popen_kwargs()
        subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs).wait(timeout=10)

    def test_terminate_is_portable(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **_platform.popen_kwargs())
        _platform.terminate_process(proc)
        self.assertIsNotNone(proc.poll())

    def test_half_started_reap_is_portable(self):
        # Secured-startup cleanup (Windows job assignment failure path) must
        # kill, reap under a fixed bound, and never raise: a raising cleanup
        # would mask the launch error it was handling.
        import subprocess
        import time
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **_platform.popen_kwargs())
        started = time.monotonic()
        _platform._reap_half_started(proc)
        self.assertLess(time.monotonic() - started, _platform._SPAWN_REAP_TIMEOUT)
        self.assertIsNotNone(proc.poll())
        # Reaping an already-dead process stays a silent no-op.
        _platform._reap_half_started(proc)


class PortablePrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-portable-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_lock_contention_portable(self):
        from mizu.errors import Busy
        from mizu.fs import lock
        path = self.root / "dev.lock"
        with lock(path):
            with self.assertRaises(Busy):
                with lock(path, blocking=False):
                    pass
        with lock(path, blocking=False):
            pass

    def test_safe_read_refuses_symlink_portable(self):
        from mizu.errors import Denied
        from mizu.fs import safe_read
        real = self.root / "real.txt"
        real.write_bytes(b"portable")
        (self.root / "link.txt").symlink_to(real)
        with self.assertRaises(Denied):
            safe_read(self.root, "link.txt", 1000)
        self.assertEqual(safe_read(self.root, "real.txt", 1000), b"portable")

    def test_process_run_portable(self):
        from mizu.process import environment, run
        result = run([sys.executable, "-c", "print('portable')"], timeout=10, maximum=1024)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.reason), (0, "portable", "exited"))
        env = environment()
        self.assertIn("PATH", env)
        self.assertNotIn("SECRET_API_KEY_XYZ", env)

    def test_git_devnull_is_portable(self):
        from mizu.project import git_env
        self.assertEqual(git_env()["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_doctor_platform_is_portable(self):
        from mizu.doctor import platform as check_platform
        info = check_platform()
        self.assertEqual(info["system"], _platform.SYSTEM)


class ContainerArgvTests(unittest.TestCase):
    """One argv for Podman and Docker: common flags, portable mounts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-argv-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, executable="podman"):
        import json
        from mizu.cli import configure
        from mizu.config import load
        config_file = self.root / "config.toml"
        configure(config_file, None)
        text = config_file.read_text().replace('data_dir = "~/.local/state/mizu"',
                                                'data_dir = ' + json.dumps(str(self.root / "data")))
        text = text.replace('provider = ""', 'provider = "p"').replace('model = ""', 'model = "m"')
        text = text.replace('\ndaily_requests = 0', '\ndaily_requests = 1').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
        text = text.replace('image = ""', 'image = "sha256:' + 'a' * 64 + '"')
        text = text.replace('executable = "podman"', f'executable = "{executable}"')
        config_file.write_text(text)
        return load(config_file)

    def test_common_flags_have_no_runtime_specific_options(self):
        from mizu.sandbox import Sandbox
        work = self.root / "work"
        work.mkdir()
        engine = Sandbox(self.config(), self.root, "worker", self.root / "run")
        args = engine.argv("n", work, "echo hi", writable=True)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                     "--pull=never", "--pids-limit", "--memory", "--cpus", "--user"):
            self.assertIn(flag, args)
        # M7: rootless Podman keeps the host uid so the workspace stays readable.
        self.assertIn("--userns=keep-id", args)
        self.assertFalse(any("docker.sock" in s or "credentials.env" in s for s in args))
        self.assertEqual(args[-3:], [self.config().sandbox.image, "-c", "echo hi"])

    def test_rootless_docker_omits_keep_id(self):
        from mizu.sandbox import Sandbox
        work = self.root / "work"
        work.mkdir()
        engine = Sandbox(self.config("docker"), self.root, "worker", self.root / "run")
        args = engine.argv("n", work, "echo hi", writable=True)
        self.assertIn("--user", args)
        self.assertNotIn("--userns=keep-id", args)
        self.assertNotIn("--userns", " ".join(args))

    def test_network_is_policy_selected(self):
        from mizu.sandbox import Sandbox
        work = self.root / "work"
        work.mkdir()
        import dataclasses
        custom = dataclasses.replace(self.config(), sandbox=dataclasses.replace(self.config().sandbox, network="slirp4netns"))
        args = Sandbox(custom, self.root, "worker", self.root / "run").argv("n", work, "echo hi", writable=True)
        self.assertIn("--network=slirp4netns", args)
        self.assertNotIn("--network=none", args)
        for bad in ("a b", "a;b", "", "a/b", "a\nb"):
            with self.subTest(bad=bad):
                from mizu.cli import configure
                from mizu.config import load
                from mizu.errors import ConfigError
                config_file = self.root / "owned.toml"
                if config_file.exists():
                    config_file.unlink()
                configure(config_file, None)
                body = config_file.read_text().replace('network = "none"', f'network = "{bad}"')
                config_file.write_text(body)
                with self.assertRaises(ConfigError):
                    load(config_file)

    def test_mounts_and_env_are_policy_selected(self):
        import dataclasses
        from mizu.sandbox import Sandbox
        data = self.root / "corpus"
        data.mkdir()
        work = self.root / "work"
        work.mkdir()
        base = self.config()
        sandbox = dataclasses.replace(base.sandbox, entrypoint="/bin/bash",
                                      mounts=({"source": str(data), "target": "/data"},),
                                      env={"HF_HOME": "/data/hf"})
        custom = dataclasses.replace(base, sandbox=sandbox)
        box = Sandbox(custom, self.root, "worker", self.root / "run")
        args = box.argv("n", work, "echo hi", writable=True)
        self.assertIn("--entrypoint", args)
        self.assertIn("/bin/bash", args)
        # `-v` on Linux, `--mount` elsewhere (drive letters contain `:`);
        # both spellings carry the same read-only operator mount.
        selected = [a for a in args if "/data" in a and "/workspace" not in a]
        self.assertTrue(any(a.endswith(":ro") or a.endswith(":ro,z") or "readonly" in a for a in selected))
        self.assertTrue(any(str(data) in a for a in selected))
        self.assertIn("HOME=/tmp/home", args)
        home_idx = args.index("HF_HOME=/data/hf")
        self.assertEqual(args[home_idx - 1], "--env")

    def test_mounts_are_always_readonly(self):
        import dataclasses
        from mizu.sandbox import Sandbox
        data = self.root / "corpus"
        data.mkdir()
        work = self.root / "work"
        work.mkdir()
        base = self.config()
        sandbox = dataclasses.replace(base.sandbox, mounts=({"source": str(data), "target": "/data"},))
        args = Sandbox(dataclasses.replace(base, sandbox=sandbox), self.root, "worker",
                       self.root / "run").argv("n", work, "echo hi", writable=True)
        self.assertTrue(any(a.endswith(":ro") or a.endswith(":ro,z") or "readonly" in a
                            for a in args if "/data" in a and "/workspace" not in a))

    def test_missing_mount_source_refuses_per_run(self):
        import dataclasses
        from mizu.errors import ConfigError as ConfigError2
        from mizu.sandbox import Sandbox
        work = self.root / "work"
        work.mkdir()
        base = self.config()
        sandbox = dataclasses.replace(base.sandbox,
                                      mounts=({"source": str(self.root / "absent"), "target": "/data"},))
        with self.assertRaises(ConfigError2):
            Sandbox(dataclasses.replace(base, sandbox=sandbox), self.root, "worker",
                    self.root / "run").argv("n", work, "echo hi", writable=True)

    def test_mounts_env_entrypoint_validated_at_load(self):
        from mizu.cli import configure
        from mizu.config import load
        from mizu.errors import ConfigError
        cases = [
            ('[[sandbox.mounts]]\nsource = "relative/path"\ntarget = "/data"\n', "relative source", True),
            ('[[sandbox.mounts]]\nsource = "/data"\ntarget = "/etc/shadow"\n', "system target", True),
            ('[[sandbox.mounts]]\nsource = "/data"\ntarget = "/workspace"\n', "workspace target", True),
            ('[[sandbox.mounts]]\nsource = "/data"\n', "missing target", True),
            ('[sandbox.env]\nHOME = "/x"\n', "shadowed runtime var", True),
            ('[sandbox.env]\nHF_TOKEN = "s"\n', "secret name", True),
            ('[sandbox.env]\nBAD-KEY = "s"\n', "bad key", True),
            ('entrypoint = "relative/sh"\n', "relative entrypoint", False),
        ]
        for extra, label, append in cases:
            with self.subTest(label=label):
                config_file = self.root / "owned.toml"
                if config_file.exists():
                    config_file.unlink()
                configure(config_file, None)
                body = config_file.read_text()
                if append:
                    body = body + "\n" + extra
                else:
                    body = body.replace('mode = "rootless"', 'mode = "rootless"\n' + extra.rstrip("\n"), 1)
                config_file.write_text(body)
                with self.assertRaises(ConfigError):
                    load(config_file)

    def test_command_record_lists_mounts_and_env_keys(self):
        import dataclasses
        from unittest.mock import patch
        from mizu.process import Result
        from mizu.sandbox import Sandbox
        data = self.root / "corpus"
        data.mkdir()
        work = self.root / "work"
        work.mkdir()
        run_dir = self.root / "run"
        run_dir.mkdir()
        base = self.config()
        sandbox = dataclasses.replace(base.sandbox, entrypoint="/bin/bash",
                                      mounts=({"source": str(data), "target": "/data"},),
                                      env={"HF_HOME": "/data/hf"})
        box = Sandbox(dataclasses.replace(base, sandbox=sandbox), self.root, "worker", run_dir)
        calls = [Result(0, "[]", "", "exited", 0.0), Result(1, "", "", "exited", 0.1), Result(0, "", "", "exited", 0.0)]
        with patch("mizu.sandbox.run", side_effect=calls):
            record = box.execute(work, "echo hi", writable=True)
        self.assertEqual(record["exit_code"], 1)
        self.assertEqual(record["entrypoint"], "/bin/bash")
        self.assertEqual(record["mounts"], [{"source": str(data), "target": "/data"}])
        self.assertEqual(record["env_keys"], ["HF_HOME"])

    def test_bind_spellings(self):
        from mizu.sandbox import bind_args
        linux = bind_args("/src", "/workspace", readonly=True, selinux=True, linux=True)
        self.assertEqual(linux, ["--volume", "/src:/workspace:ro,z"])
        plain = bind_args("/src", "/workspace", readonly=False, selinux=False, linux=True)
        self.assertEqual(plain, ["--volume", "/src:/workspace:rw"])
        mount = bind_args("C:/work/src", "/workspace", readonly=True, selinux=True, linux=False)
        self.assertEqual(mount, ["--mount", "type=bind,src=C:/work/src,dst=/workspace,readonly"])
        # Drive-letter colons survive the mount spelling; newlines and commas never do.
        self.assertIn("C:/work/src", " ".join(mount))

    def test_remove_argv_fast_path_is_podman_only(self):
        from mizu.sandbox import remove_argv
        podman = remove_argv(self.config("podman"), "mizu-x")
        self.assertEqual(podman[-3:], ["--force", "--ignore", "mizu-x"])
        self.assertIsNone(remove_argv(self.config("docker"), "mizu-x"))

    def test_execute_refuses_launch_without_runtime(self):
        # Fail closed: an unverifiable inspect (missing runtime) denies
        # launch instead of recording a startup_error run.
        from unittest.mock import patch
        from mizu.errors import Denied
        from mizu.process import Result
        from mizu.sandbox import Sandbox
        work = self.root / "work"
        work.mkdir()
        engine = Sandbox(self.config("mizu-missing-runtime-xyz"), self.root, "worker", self.root / "run")
        with patch("mizu.sandbox.run",
                   side_effect=[Result(None, "", "missing", "startup_error", 0),
                                Result(None, "", "missing", "startup_error", 0),
                                Result(0, "", "", "exited", 0)]):
            with self.assertRaises(Denied) as caught:
                engine.execute(work, "echo hi", writable=False)
        self.assertIn("cannot verify", str(caught.exception).lower())


class ServiceRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-3-svc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        import json
        from mizu.cli import configure
        from mizu.config import load
        from mizu.project import initialize
        config_file = self.root / "config.toml"
        configure(config_file, None)
        text = config_file.read_text().replace('data_dir = "~/.local/state/mizu"',
                                                'data_dir = ' + json.dumps(str(self.root / "data")))
        text = text.replace('provider = ""', 'provider = "p"').replace('model = ""', 'model = "m"')
        text = text.replace('\ndaily_requests = 0', '\ndaily_requests = 1').replace('free_disk_mb = 1024', 'free_disk_mb = 0')
        text = text.replace('image = ""', 'image = "sha256:' + 'a' * 64 + '"')
        config_file.write_text(text)
        self.config = __import__("dataclasses").replace(load(config_file), timezone="local")
        source = self.root / "src"
        source.mkdir()
        (source / "app.py").write_text("x=1\n")
        goal = self.root / "GOAL.md"
        goal.write_text("goal\n")
        self.project = initialize(self.config, "svc", source, goal, ["worker", "reporter"], [])

    def test_systemd_render_unchanged(self):
        from mizu.services import render
        units = render(self.config, self.project, ROOT / "bin/mizu", system="linux")
        self.assertIn("mizu-3-svc-worker.service", units)
        self.assertIn("mizu-3-svc-reporter.timer", units)
        self.assertIn("Delegate=yes", units["mizu-3-svc-worker.service"])
        self.assertNotIn("credentials.env", "".join(units.values()))

    def test_launchd_render_is_valid_plist(self):
        import plistlib
        from mizu.services import render
        units = render(self.config, self.project, ROOT / "bin/mizu", system="macos")
        self.assertEqual(sorted(units), ["mizu-3-svc-reporter.plist", "mizu-3-svc-worker.plist"])
        worker = plistlib.loads(units["mizu-3-svc-worker.plist"].encode())
        self.assertEqual(worker["Label"], "mizu-3-svc-worker")
        self.assertTrue(worker["KeepAlive"])
        self.assertIn("daemon", worker["ProgramArguments"])
        reporter = plistlib.loads(units["mizu-3-svc-reporter.plist"].encode())
        self.assertTrue(any("Hour" in entry for entry in reporter["StartCalendarInterval"]))

    def test_windows_render_is_valid_task_xml(self):
        import xml.etree.ElementTree as ET
        from mizu.services import render
        units = render(self.config, self.project, ROOT / "bin/mizu", system="windows")
        self.assertEqual(sorted(units), ["mizu-3-svc-reporter.xml", "mizu-3-svc-worker.xml"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        worker = ET.fromstring(units["mizu-3-svc-worker.xml"])
        self.assertIsNotNone(worker.find("t:Triggers/t:LogonTrigger", ns))
        self.assertIsNotNone(worker.find("t:Actions/t:Exec/t:Command", ns))
        reporter = ET.fromstring(units["mizu-3-svc-reporter.xml"])
        self.assertIsNotNone(reporter.find("t:Triggers/t:CalendarTrigger", ns))

    def test_event_trigger_watches_decisions_and_waits(self):
        import dataclasses
        import plistlib
        from mizu.doctor import service_units
        from mizu.services import event_watches, install, render
        roles = dict(self.config.roles)
        roles["reporter"] = dataclasses.replace(roles["reporter"], decision_events=("reject", "defer"))
        config = dataclasses.replace(self.config, roles=roles)
        watches = event_watches(config, self.project)
        # The daemon writer needs no file trigger; the unwatched reporter
        # schedule is unchanged.
        self.assertEqual(sorted(watches), ["reporter"])
        base = "mizu-3-svc-reporter"
        units = render(config, self.project, ROOT / "bin/mizu", system="linux")
        trigger = units[base + ".path"]
        self.assertIn(f"Unit={base}.service", trigger)
        self.assertIn("decision-history", trigger)
        self.assertIn("waits", trigger)
        self.assertIn("OnCalendar=", units[base + ".timer"])
        job = plistlib.loads(render(config, self.project, ROOT / "bin/mizu", system="macos")[base + ".plist"].encode())
        self.assertTrue(any("decision-history" in path for path in job["WatchPaths"]))
        worker = plistlib.loads(render(config, self.project, ROOT / "bin/mizu", system="macos")["mizu-3-svc-worker.plist"].encode())
        self.assertNotIn("WatchPaths", worker)
        directory = self.root / "units-event-linux"
        result = install(config, self.project, ROOT / "bin/mizu", directory, system="linux")
        self.assertIn(base + ".path", result["written"])
        self.assertIn(base + ".path", result["enable_units"])
        self.assertNotIn(base + ".service", result["enable_units"])
        checked = service_units(directory, system="linux")
        self.assertEqual(sorted(checked["units"]), sorted(result["written"]))

    def test_unknown_system_is_refused(self):
        from mizu.errors import Denied
        from mizu.services import render
        with self.assertRaises(Denied):
            render(self.config, self.project, ROOT / "bin/mizu", system="plan9")

    def test_install_roundtrip_per_system(self):
        import plistlib
        import xml.etree.ElementTree as ET
        from mizu.doctor import service_units
        from mizu.services import install
        for system, suffix in (("linux", ".service"), ("macos", ".plist"), ("windows", ".xml")):
            directory = self.root / f"units-{system}"
            result = install(self.config, self.project, ROOT / "bin/mizu", directory, system=system)
            self.assertEqual(result["system"], system)
            self.assertTrue(any(u.endswith(suffix) for u in result["written"]))
            checked = service_units(directory, system=system)
            self.assertEqual(sorted(checked["units"]), sorted(result["written"]))
            if system == "macos":
                plistlib.loads((directory / result["written"][0]).read_bytes())
            elif system == "windows":
                ET.fromstring((directory / result["written"][0]).read_bytes())

    def test_stale_definitions_are_cleaned_per_system(self):
        from mizu.services import install
        for system, stale in (("linux", "mizu-svc-ghost.service"), ("macos", "mizu-svc-ghost.plist"),
                              ("windows", "mizu-svc-ghost.xml")):
            directory = self.root / f"stale-{system}"
            directory.mkdir()
            (directory / stale).write_text("stale")
            result = install(self.config, self.project, ROOT / "bin/mizu", directory, system=system)
            self.assertNotIn("ghost", "".join(result["written"]))
            self.assertTrue((directory / stale).exists())
            self.assertEqual((directory / stale).read_text(), "stale")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
