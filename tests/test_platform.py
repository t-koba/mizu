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
        proc = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kwargs)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(proc.poll(), 0)

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

    def test_git_is_operation_specific_not_platform(self):
        import shutil
        from unittest.mock import patch
        from mizu.doctor import git_presence, platform as check_platform
        with patch.object(shutil, "which", return_value=None):
            info = check_platform()
            self.assertEqual(info["system"], _platform.SYSTEM)
            status, details = git_presence()
            self.assertEqual(status, "not_run")
            self.assertIn("git-tree", details)
        with patch.object(shutil, "which", return_value="/usr/bin/git"):
            status, details = git_presence()
            self.assertEqual(status, "pass")

    def test_git_tree_import_fails_before_running_git(self):
        import shutil
        from unittest.mock import patch
        from support import Fixture
        from mizu.errors import Denied
        from mizu.project import initialize

        class T(Fixture):
            def runTest(self):
                pass

        case = T()
        case.setUp()
        try:
            source = case.root / "gitsource"
            source.mkdir()
            (source / ".git").mkdir()
            (source / "app.py").write_bytes(b"VALUE = 2\n")
            with patch.object(shutil, "which", return_value=None):
                with self.assertRaisesRegex(Denied, "Git is required for git-tree import"):
                    initialize(case.config, "gitless", source, case.goal,
                               ["worker"], ["python3 -m unittest"])
        finally:
            case.tearDown()

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
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", args)
        self.assertIn("PYTHONPYCACHEPREFIX=/tmp/pycache", args)
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
            ('[sandbox.env]\nNODE_OPTIONS = "x"\n', "hijack var", True),
            ('[sandbox.env]\nLD_PRELOAD = "x"\n', "hijack var", True),
            ('[sandbox.env]\nLD_LIBRARY_PATH = "x"\n', "hijack var", True),
            ('[sandbox.env]\nPYTHONPATH = "x"\n', "hijack var", True),
            ('[sandbox.env]\nPYTHONDONTWRITEBYTECODE = "0"\n', "shadowed runtime var", True),
            ('[sandbox.env]\nPYTHONPYCACHEPREFIX = "/x"\n', "shadowed runtime var", True),
            ('[sandbox.env]\nBASH_ENV = "x"\n', "hijack var", True),
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
        # Event-triggered dispatch is a separate service invoked with --event so
        # a trigger with nothing due exits before any model use; the periodic
        # timer keeps invoking the plain service on its own schedule.
        self.assertIn("--event", units[base + "-event.service"])
        self.assertNotIn("--event", units[base + ".service"])
        trigger = units[base + "-event.path"]
        self.assertIn(f"Unit={base}-event.service", trigger)
        self.assertIn("decision-history", trigger)
        self.assertIn("waits", trigger)
        # No trigger limit: TriggerLimitBurst=1 fails the path unit on a burst
        # and stops dispatching; extra dispatches exit cheaply via --event.
        self.assertNotIn("TriggerLimit", trigger)
        event_service = units[base + "-event.service"]
        self.assertIn("Type=oneshot", event_service)
        self.assertIn("StartLimitIntervalSec=0", event_service)
        self.assertNotIn("TriggerLimit", units[base + ".service"])
        self.assertNotIn("StartLimitIntervalSec=0", units[base + ".service"])
        self.assertIn("OnCalendar=", units[base + ".timer"])
        macos = render(config, self.project, ROOT / "bin/mizu", system="macos")
        schedule = plistlib.loads(macos[base + ".plist"].encode())
        self.assertNotIn("WatchPaths", schedule)
        job = plistlib.loads(macos[base + "-event.plist"].encode())
        self.assertTrue(any("decision-history" in path for path in job["WatchPaths"]))
        self.assertIn("--event", job["ProgramArguments"])
        worker = plistlib.loads(macos["mizu-3-svc-worker.plist"].encode())
        self.assertNotIn("WatchPaths", worker)
        # work_item_events adds the work-items parent watch on Linux/macOS.
        terminal_roles = dict(self.config.roles)
        terminal_roles["reporter"] = dataclasses.replace(
            terminal_roles["reporter"], decision_events=(),
            work_item_events=("done",))
        terminal_config = dataclasses.replace(self.config, roles=terminal_roles)
        terminal_watches = event_watches(terminal_config, self.project)
        self.assertEqual(sorted(terminal_watches), ["reporter"])
        self.assertTrue(any(str(watch).endswith("work-items")
                            for watch in terminal_watches["reporter"]))
        # Windows keeps only the scheduled trigger for this subscribed role:
        # the reporter XML carries its calendar trigger, with no --event
        # arguments or separate file-trigger job.
        windows = render(config, self.project, ROOT / "bin/mizu", system="windows")
        self.assertIn(base + ".xml", windows)
        self.assertNotIn(base + "-event.xml", windows)
        reporter_xml = windows[base + ".xml"]
        self.assertIn("CalendarTrigger", reporter_xml)
        self.assertNotIn("--event", reporter_xml)
        self.assertNotIn("WatchPaths", reporter_xml)
        self.assertNotIn("PathChanged", reporter_xml)
        directory = self.root / "units-event-linux"
        result = install(config, self.project, ROOT / "bin/mizu", directory, system="linux")
        self.assertIn(base + "-event.path", result["written"])
        self.assertIn(base + "-event.service", result["written"])
        self.assertIn(base + "-event.path", result["enable_units"])
        self.assertIn(base + ".timer", result["enable_units"])
        self.assertNotIn(base + ".service", result["enable_units"])
        self.assertNotIn(base + "-event.service", result["enable_units"])
        checked = service_units(directory, system="linux")
        self.assertEqual(sorted(checked["units"]), sorted(result["written"]))

    def test_event_only_role_gets_trigger_without_schedule(self):
        import dataclasses
        import plistlib
        from mizu.services import event_watches, render
        roles = dict(self.config.roles)
        # Event-only resolver: subscriptions without any schedule. The project
        # here enables only worker/reporter, so turn reporter event-only.
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=0, calendar=(), daemon=False,
            decision_events=("defer",), decision_sources="any")
        config = dataclasses.replace(self.config, roles=roles)
        watches = event_watches(config, self.project)
        self.assertEqual(sorted(watches), ["reporter"])
        base = "mizu-3-svc-reporter"
        units = render(config, self.project, ROOT / "bin/mizu", system="linux")
        # Every unscheduled watched role also gets recovery reevaluation at the
        # effective event_poll_seconds cadence (default 60 seconds).
        self.assertIn(base + "-event.service", units)
        self.assertIn(base + "-event.path", units)
        self.assertNotIn(base + ".timer", units)
        self.assertNotIn(base + ".service", units)
        self.assertIn("--event", units[base + "-event.service"])
        self.assertIn("decision-history", units[base + "-event.path"])
        recovery = units[base + "-event.timer"]
        self.assertIn("OnBootSec=60s", recovery)
        self.assertIn("OnUnitInactiveSec=60s", recovery)
        self.assertIn(f"Unit={base}-event.service", recovery)
        # macOS renders the WatchPaths event plist with no base schedule plist.
        mac = render(config, self.project, ROOT / "bin/mizu", system="macos")
        self.assertNotIn(base + ".plist", mac)
        event = plistlib.loads(mac[base + "-event.plist"].encode())
        self.assertIn("WatchPaths", str(event))
        self.assertEqual(event["StartInterval"], 60)
        # A role with neither schedule nor subscriptions stays explicit-run only.
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=0, calendar=(), daemon=False,
            decision_events=())
        bare = dataclasses.replace(config, roles=roles)
        self.assertEqual(event_watches(bare, self.project), {})

    def test_unscheduled_on_change_watches_code_pointer_on_all_platforms(self):
        import dataclasses
        import plistlib
        import xml.etree.ElementTree as ET
        from mizu.services import event_watches, render
        roles = dict(self.config.roles)
        # Reporter is scheduled; clearing the schedule and enabling on_change
        # makes it event-only for this project (worker/reporter only).
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=0, calendar=(), daemon=False,
            decision_events=(), work_item_events=(), on_change=True)
        config = dataclasses.replace(self.config, roles=roles)
        watches = event_watches(config, self.project)
        self.assertIn("reporter", watches)
        pointer = str(self.project.root / "current.json")
        self.assertIn(pointer, watches["reporter"])
        base = "mizu-3-svc-reporter"
        linux = render(config, self.project, ROOT / "bin/mizu", system="linux")
        self.assertIn(base + "-event.service", linux)
        self.assertIn(base + "-event.path", linux)
        self.assertIn("current.json", linux[base + "-event.path"])
        recovery = linux[base + "-event.timer"]
        self.assertIn("OnBootSec=60s", recovery)
        self.assertIn("OnUnitInactiveSec=60s", recovery)
        self.assertIn("AccuracySec=1s", recovery)
        self.assertIn(f"Unit={base}-event.service", recovery)
        self.assertNotIn(base + ".service", linux)
        self.assertNotIn(base + ".timer", linux)
        mac = render(config, self.project, ROOT / "bin/mizu", system="macos")
        self.assertNotIn(base + ".plist", mac)
        event = plistlib.loads(mac[base + "-event.plist"].encode())
        self.assertTrue(any("current.json" in path for path in event["WatchPaths"]))
        self.assertEqual(event["StartInterval"], 60)
        self.assertIn("--event", event["ProgramArguments"])
        windows = render(config, self.project, ROOT / "bin/mizu", system="windows")
        self.assertIn(base + "-event.xml", windows)
        task = ET.fromstring(windows[base + "-event.xml"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        repetition = task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns)
        self.assertIsNotNone(repetition)
        self.assertEqual(repetition.text, "PT60S")
        arguments = task.find("t:Actions/t:Exec/t:Arguments", ns).text
        self.assertIn("--event", arguments)
        self.assertIn("reporter", arguments)

    def test_unscheduled_on_change_install_enables_recovery_not_oneshot(self):
        import dataclasses
        import plistlib
        from mizu.services import install, render
        roles = dict(self.config.roles)
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=0, calendar=(), daemon=False,
            on_change=True, decision_events=("accept",))
        config = dataclasses.replace(self.config, roles=roles)
        base = "mizu-3-svc-reporter"
        for system in ("linux", "macos", "windows"):
            with self.subTest(system=system):
                directory = self.root / f"recovery-{system}"
                result = install(config, self.project, ROOT / "bin/mizu", directory, system=system)
                expected = render(config, self.project, ROOT / "bin/mizu", system=system)
                self.assertEqual(result["written"], sorted(expected))
                for unit, content in expected.items():
                    self.assertEqual((directory / unit).read_text(), content)
                if system == "linux":
                    self.assertIn(base + "-event.timer", result["enable_units"])
                    self.assertIn(base + "-event.path", result["enable_units"])
                    self.assertNotIn(base + "-event.service", result["enable_units"])
                elif system == "macos":
                    self.assertIn(base + "-event.plist", result["enable_units"])
                    self.assertEqual([u for u in result["written"] if base in u],
                                     [base + "-event.plist"])
                    event = plistlib.loads((directory / (base + "-event.plist")).read_bytes())
                    self.assertEqual(event["StartInterval"], 60)
                    self.assertIn(str(self.project.root / "current.json"), event["WatchPaths"])
                else:
                    self.assertIn(base + "-event.xml", result["enable_units"])

    def test_event_poll_override_applies_on_all_platforms(self):
        import dataclasses
        import plistlib
        import xml.etree.ElementTree as ET
        from mizu.services import install, render
        roles = dict(self.config.roles)
        # Event-only subscribed role with an explicit poll override (no
        # on_change, so the generalization itself is under test, not only the
        # code-pointer path).
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=0, calendar=(), daemon=False,
            decision_events=("defer",), decision_sources="any",
            event_poll_seconds=300)
        config = dataclasses.replace(self.config, roles=roles)
        base = "mizu-3-svc-reporter"
        linux = render(config, self.project, ROOT / "bin/mizu", system="linux")
        recovery = linux[base + "-event.timer"]
        self.assertIn("OnBootSec=300s", recovery)
        self.assertIn("OnUnitInactiveSec=300s", recovery)
        self.assertNotIn("OnBootSec=60s", recovery)
        self.assertIn(f"Unit={base}-event.service", recovery)
        mac = render(config, self.project, ROOT / "bin/mizu", system="macos")
        event = plistlib.loads(mac[base + "-event.plist"].encode())
        self.assertEqual(event["StartInterval"], 300)
        windows = render(config, self.project, ROOT / "bin/mizu", system="windows")
        task = ET.fromstring(windows[base + "-event.xml"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        repetition = task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns)
        self.assertIsNotNone(repetition)
        self.assertEqual(repetition.text, "PT300S")
        for system in ("linux", "macos", "windows"):
            with self.subTest(system=system):
                directory = self.root / f"poll-override-{system}"
                result = install(config, self.project, ROOT / "bin/mizu", directory, system=system)
                expected = render(config, self.project, ROOT / "bin/mizu", system=system)
                self.assertEqual(result["written"], sorted(expected))
                if system == "linux":
                    self.assertIn(base + "-event.timer", result["enable_units"])
                    self.assertIn(base + "-event.path", result["enable_units"])
                    self.assertNotIn(base + "-event.service", result["enable_units"])
                else:
                    suffix = ".plist" if system == "macos" else ".xml"
                    self.assertIn(base + f"-event{suffix}", result["enable_units"])

    def test_scheduled_trigger_ignores_event_poll_override(self):
        import dataclasses
        import plistlib
        import xml.etree.ElementTree as ET
        from mizu.services import render
        roles = dict(self.config.roles)
        # Scheduled subscribed role: the override must not add recovery or
        # change the schedule trigger on any platform.
        roles["reporter"] = dataclasses.replace(
            roles["reporter"], interval_seconds=900, calendar=(),
            on_change=True, decision_events=("reject", "defer"),
            event_poll_seconds=300)
        config = dataclasses.replace(self.config, roles=roles)
        base = "mizu-3-svc-reporter"
        linux = render(config, self.project, ROOT / "bin/mizu", system="linux")
        self.assertIn("OnUnitInactiveSec=900s", linux[base + ".timer"])
        self.assertNotIn(base + "-event.timer", linux)
        self.assertNotIn("--event", linux[base + ".service"])
        mac = render(config, self.project, ROOT / "bin/mizu", system="macos")
        schedule = plistlib.loads(mac[base + ".plist"].encode())
        self.assertEqual(schedule["StartInterval"], 900)
        event = plistlib.loads(mac[base + "-event.plist"].encode())
        self.assertNotIn("StartInterval", event)
        windows = render(config, self.project, ROOT / "bin/mizu", system="windows")
        self.assertIn(base + ".xml", windows)
        self.assertNotIn(base + "-event.xml", windows)
        self.assertNotIn("--event", windows[base + ".xml"])

    def test_scheduled_triggers_stay_unchanged_for_subscribed_roles(self):
        import dataclasses
        import plistlib
        from mizu.services import event_watches, render
        base = "mizu-3-svc-reporter"
        for interval, calendar in ((0, ("07:00", "19:00")), (900, ())):
            with self.subTest(interval=interval, calendar=calendar):
                roles = dict(self.config.roles)
                roles["reporter"] = dataclasses.replace(
                    roles["reporter"], interval_seconds=interval, calendar=calendar,
                    on_change=True, decision_events=("reject", "defer"))
                config = dataclasses.replace(self.config, roles=roles)
                watches = event_watches(config, self.project)
                self.assertNotIn(str(self.project.root / "current.json"), watches["reporter"])
                linux = render(config, self.project, ROOT / "bin/mizu", system="linux")
                self.assertIn("OnUnitInactiveSec=900s" if interval else "OnCalendar=",
                              linux[base + ".timer"])
                self.assertNotIn(base + "-event.timer", linux)
                self.assertNotIn("--event", linux[base + ".service"])
                mac = render(config, self.project, ROOT / "bin/mizu", system="macos")
                event = plistlib.loads(mac[base + "-event.plist"].encode())
                self.assertNotIn("StartInterval", event)
                self.assertNotIn("StartCalendarInterval", event)
                schedule = plistlib.loads(mac[base + ".plist"].encode())
                if interval:
                    self.assertEqual(schedule["StartInterval"], interval)
                else:
                    self.assertEqual(schedule["StartCalendarInterval"],
                                     [{"Hour": 7, "Minute": 0}, {"Hour": 19, "Minute": 0}])
                windows = render(config, self.project, ROOT / "bin/mizu", system="windows")
                self.assertIn(base + ".xml", windows)
                self.assertNotIn(base + "-event.xml", windows)
                self.assertNotIn("--event", windows[base + ".xml"])

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
