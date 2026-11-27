"""Portable daemon wake detection: metadata fingerprint and interruptible wait."""
import contextlib
import ctypes
import gc
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mizu import daemon_wake, fs


class DaemonWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="daemon-wake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _eventually_changed(self, baseline, timeout=3.0):
        # Some filesystems expose directory and same-size file mtime only after
        # a short delay. A wake hint is a reason to look, so the test waits for
        # the metadata observation to catch up instead of assuming one tick.
        deadline = time.monotonic() + timeout
        while daemon_wake.fingerprint(self.root) == baseline and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertNotEqual(daemon_wake.fingerprint(self.root), baseline)

    def test_fingerprint_stable_and_identity_aware(self):
        self.assertEqual(daemon_wake.fingerprint(None), ())
        base = daemon_wake.fingerprint(self.root)
        self.assertEqual(daemon_wake.fingerprint(self.root), base)
        # A watched file appearing changes the fingerprint.
        (self.root / "control.json").write_text("{}")
        self._eventually_changed(base)
        after_file = daemon_wake.fingerprint(self.root)
        # A same-size rewrite through the product writer changes st_ino
        # deterministically; an in-place write relies on mtime alone and flakes
        # on filesystems with coarse timestamp granularity.
        fs.atomic_write(self.root / "control.json", b"[]")
        self._eventually_changed(after_file)
        after_rewrite = daemon_wake.fingerprint(self.root)
        # A watched directory appearing changes the fingerprint directly.
        (self.root / "wake").mkdir()
        self._eventually_changed(after_rewrite)
        after_dir = daemon_wake.fingerprint(self.root)
        # A nested revision is observed through the writer's published hint.
        # NTFS does not reliably move a directory mtime on child creation, so
        # writers call touch_directory explicitly and the fingerprint watches
        # that hint rather than raw filesystem churn.
        (self.root / "wake" / "worker").write_text("")
        fs.touch_directory(self.root / "wake")
        self._eventually_changed(after_dir)

    def test_fingerprint_includes_authoritative_config_file(self):
        # The deployment config normally lives outside the project root. A
        # change to it must perturb the fingerprint so a settled daemon wakes,
        # even when there is no project root at all.
        cfg = self.root / "config" / "config.toml"
        cfg.parent.mkdir()
        self.addCleanup(cfg.unlink, missing_ok=True)
        cfg.write_text("idle_seconds = 60\n")
        base = daemon_wake.fingerprint(None, cfg)
        self.assertNotEqual(base, daemon_wake.fingerprint(None))
        self.assertEqual(daemon_wake.fingerprint(None, cfg), base)
        # Change content length so st_size changes on every filesystem.
        cfg.write_text("idle_seconds = 6100\n")
        self.assertNotEqual(daemon_wake.fingerprint(None, cfg), base)
        # The config row augments, never replaces, the project-root rows.
        with_root = daemon_wake.fingerprint(self.root, cfg)
        self.assertEqual(with_root[0], daemon_wake.fingerprint(None, cfg)[0])
        self.assertEqual(with_root[1:], daemon_wake.fingerprint(self.root))

    def test_wait_wakes_on_config_file_change(self):
        cfg = self.root / "config" / "config.toml"
        cfg.parent.mkdir()
        self.addCleanup(cfg.unlink, missing_ok=True)
        cfg.write_text("idle_seconds = 60\n")
        baseline = daemon_wake.fingerprint(self.root, cfg)
        cfg.write_text("idle_seconds = 6100\n")
        stop = threading.Event()
        self.assertEqual(daemon_wake.wait(stop, self.root, baseline, None,
                                          poll=0.01, config_file=cfg), "change")

    def test_fingerprint_stat_count_is_independent_of_retained_inventory(self):
        original_stat = Path.stat
        calls = []

        def counting_stat(path, *args, **kwargs):
            calls.append(path)
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", counting_stat):
            empty = daemon_wake.fingerprint(self.root)
        fixed_count = len(calls)
        self.assertEqual(fixed_count,
                         len(daemon_wake.WATCHED_FILES) + len(daemon_wake.WATCHED_DIRS))
        for name in ("check-jobs", "histories"):
            children = self.root / name
            children.mkdir()
            for number in range(250):
                (children / f"item-{number}").mkdir()
        calls.clear()
        with patch.object(Path, "stat", counting_stat), \
                patch("os.scandir", side_effect=AssertionError("scandir in wait path")):
            populated = daemon_wake.fingerprint(self.root)
            self.assertEqual(len(calls), fixed_count)
            self.assertEqual(len(populated), len(empty))
            # The wait path must never enumerate retained directories either.
            stop = threading.Event()
            self.assertEqual(
                daemon_wake.wait(stop, self.root, populated,
                                 time.monotonic() + 0.01, poll=0.005),
                "deadline")

    def _bump_directory(self, directory):
        # Set the directory mtime directly, as the product's wake publisher
        # does. Adding a child does not reliably update a directory's
        # st_mtime_ns on every filesystem (notably NTFS), which made this test
        # flake on Windows. os.utime(follow_symlinks=False) is unsupported on
        # Windows directories, so use the default (follow_symlinks=True).
        st = directory.stat()
        os.utime(directory, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    def test_touch_directory_publishes_portable_hint_and_refuses_symlinks(self):
        watched = self.root / "work-items"
        watched.mkdir()
        before = watched.stat().st_mtime_ns
        real_utime = os.utime

        def portable_utime(path, *args, **kwargs):
            if kwargs.get("follow_symlinks") is False:
                raise NotImplementedError("unsupported for directories on Windows")
            return real_utime(path, *args, **kwargs)

        with patch("mizu.fs.os.utime", side_effect=portable_utime) as utime:
            fs.touch_directory(watched)
        self.assertGreater(watched.stat().st_mtime_ns, before)
        utime.assert_called_once()
        self.assertNotIn("follow_symlinks", utime.call_args.kwargs)

        link = self.root / "work-items-link"
        try:
            link.symlink_to(watched, target_is_directory=True)
        except OSError:  # Windows may require symlink privilege.
            link = watched
            guard = patch.object(Path, "is_symlink", return_value=True)
        else:
            guard = contextlib.nullcontext()
        target_before = watched.stat().st_mtime_ns
        with guard, patch("mizu.fs.os.utime", side_effect=portable_utime) as utime:
            fs.touch_directory(link)
        utime.assert_not_called()
        self.assertEqual(watched.stat().st_mtime_ns, target_before)

    def test_fingerprint_detects_writer_hints_but_not_project_root(self):
        watched = self.root / "work-items"
        watched.mkdir()
        stable = daemon_wake.fingerprint(self.root)
        # The project root churns with ledger journal files. Watching it makes
        # the daemon wake itself on every pass, so its mtime must be ignored.
        self._bump_directory(self.root)
        self.assertEqual(daemon_wake.fingerprint(self.root), stable)
        # A writer's top-level hint is observed.
        self._bump_directory(watched)
        self.assertNotEqual(daemon_wake.fingerprint(self.root), stable)

    def test_wait_returns_change_already_present(self):
        (self.root / "control.json").write_text("{}")
        baseline = daemon_wake.fingerprint(self.root)
        fs.atomic_write(self.root / "control.json", b"[]")
        self._eventually_changed(baseline)
        stop = threading.Event()
        self.assertEqual(daemon_wake.wait(stop, self.root, baseline, None, poll=0.01), "change")

    def test_wait_deadline_and_stop(self):
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 0.05
        self.assertEqual(daemon_wake.wait(stop, self.root, baseline, deadline, poll=0.01), "deadline")
        stop.set()
        self.assertEqual(daemon_wake.wait(stop, self.root, baseline, None, poll=0.01), "stop")

    def test_wait_change_precedes_deadline(self):
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 5.0

        def mutate():
            time.sleep(0.03)
            (self.root / "wake").mkdir()

        thread = threading.Thread(target=mutate)
        thread.start()
        started = time.monotonic()
        reason = daemon_wake.wait(stop, self.root, baseline, deadline, poll=0.01)
        thread.join()
        self.assertEqual(reason, "change")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_wait_no_change_waits_for_deadline(self):
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 0.06
        started = time.monotonic()
        self.assertEqual(daemon_wake.wait(stop, self.root, baseline, deadline, poll=0.01), "deadline")
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def _config(self, name, text="idle_seconds = 60\n"):
        cfg = self.root / "config" / name
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(text)
        self.addCleanup(cfg.unlink, missing_ok=True)
        return cfg

    def test_fingerprint_includes_secondary_config_file(self):
        primary = self._config("config.toml")
        fragment = self._config("shared.toml", "[limits]\nidle_seconds = 15\n")
        base = daemon_wake.fingerprint(None, primary, config_files=(fragment,))
        self.assertEqual(len(base), len(daemon_wake.fingerprint(None, primary)) + 1)
        self.assertIn(("config", str(fragment)), [row[:2] for row in base])
        self.assertEqual(base, daemon_wake.fingerprint(None, primary, config_files=(fragment,)))
        # An edit to the included file perturbs the fingerprint even though the
        # authoritative root path is byte-for-byte untouched.
        fragment.write_text("[limits]\nidle_seconds = 1500\n")
        self.assertEqual(primary.read_text(), "idle_seconds = 60\n")
        self.assertNotEqual(daemon_wake.fingerprint(None, primary, config_files=(fragment,)), base)

    def test_fingerprint_detects_removed_and_restored_config_file(self):
        primary = self._config("config.toml")
        fragment = self._config("shared.toml", "[limits]\nidle_seconds = 15\n")
        base = daemon_wake.fingerprint(None, primary, config_files=(fragment,))
        fragment.unlink()
        removed = daemon_wake.fingerprint(None, primary, config_files=(fragment,))
        self.assertNotEqual(removed, base)
        self.assertEqual(removed[1], ("config", str(fragment), None))
        fragment.write_text("[limits]\nidle_seconds = 15\n")
        restored = daemon_wake.fingerprint(None, primary, config_files=(fragment,))
        self.assertNotEqual(restored, removed)
        self.assertIsInstance(restored[1][2], tuple)

    def test_fingerprint_deduplicates_authoritative_config_paths(self):
        primary = self._config("config.toml")
        fragment = self._config("shared.toml")
        # The root appears in both arguments but is statted and emitted once.
        self.assertEqual(daemon_wake.fingerprint(None, primary, config_files=(primary,)),
                         daemon_wake.fingerprint(None, primary))
        original_stat = Path.stat
        calls = []

        def counting_stat(path, *args, **kwargs):
            calls.append(path)
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", counting_stat):
            daemon_wake.fingerprint(None, primary, config_files=(primary, fragment))
        self.assertEqual(calls, [primary, fragment])

    def test_wait_wakes_on_secondary_config_file_change(self):
        primary = self._config("config.toml")
        fragment = self._config("shared.toml", "[limits]\nidle_seconds = 15\n")
        baseline = daemon_wake.fingerprint(self.root, primary, config_files=(fragment,))
        fragment.write_text("[limits]\nidle_seconds = 1500\n")
        stop = threading.Event()
        self.assertEqual(
            daemon_wake.wait(stop, self.root, baseline, None, poll=0.01,
                             config_file=primary, config_files=(fragment,)),
            "change")



class NativeWakeTests(unittest.TestCase):
    """The no-poll path: kernel notification plus exact deadline and stop."""

    def test_all_supported_platforms_have_a_backend(self):
        from mizu import daemon_notify
        for platform in ("linux", "darwin", "win32"):
            with patch.object(sys, "platform", platform):
                self.assertTrue(daemon_notify.available(), platform)

    def test_kqueue_vnode_flags_use_note_constants(self):
        from mizu import daemon_notify
        notes = {f"KQ_NOTE_{name}": 1 << number for number, name in enumerate(
            ("DELETE", "WRITE", "EXTEND", "ATTRIB", "LINK", "RENAME", "REVOKE"))}
        registrations = []

        class FakeKqueue:
            def control(self, events, max_events, timeout):
                registrations.extend(events)
                return []

            def close(self):
                pass

        fake_select = types.SimpleNamespace(
            KQ_EV_ADD=0x0001, KQ_EV_ENABLE=0x0002, KQ_EV_CLEAR=0x0004,
            KQ_FILTER_VNODE=0x0100, KQ_FILTER_READ=0x0200,
            kqueue=FakeKqueue,
            kevent=lambda fd, filt, flags, fflags, data, udata: (
                fd, filt, flags, fflags, data, udata),
            **notes)

        flags, fflags = daemon_notify.kqueue_vnode_flags(fake_select)
        self.assertEqual(flags, 0x0001 | 0x0002 | 0x0004)
        for name, bit in notes.items():
            self.assertTrue(fflags & bit, f"{name} missing from vnode flags")
        self.assertEqual(fflags, sum(notes.values()))

        class Stop:
            def fileno(self):
                return -1

        real_helper = daemon_notify.kqueue_vnode_flags
        with patch.object(daemon_notify, "select", fake_select), \
                patch.object(daemon_notify, "kqueue_vnode_flags",
                             wraps=real_helper) as helper:
            watcher = daemon_notify._Kqueue((), (), Stop())
        try:
            helper.assert_called_once_with(fake_select)
            self.assertEqual(registrations[0][2], flags)
        finally:
            watcher.close()

    def test_kqueue_registers_regular_files_without_directory_flags(self):
        from mizu import daemon_notify

        class FakeKqueue:
            def control(self, events, max_events, timeout):
                return []

            def close(self):
                pass

        class Stop:
            def fileno(self):
                return -1

        notes = {f"KQ_NOTE_{name}": 1 << number for number, name in enumerate(
            ("DELETE", "WRITE", "EXTEND", "ATTRIB", "LINK", "RENAME", "REVOKE"))}
        fake_select = types.SimpleNamespace(
            KQ_EV_ADD=0x0001, KQ_EV_ENABLE=0x0002, KQ_EV_CLEAR=0x0004,
            KQ_FILTER_VNODE=0x0100, KQ_FILTER_READ=0x0200,
            kqueue=FakeKqueue,
            kevent=lambda fd, filt, flags, fflags, data, udata: (
                fd, filt, flags, fflags, data, udata),
            **notes)

        with tempfile.TemporaryDirectory(prefix="kqueue-files-") as tmp:
            root = Path(tmp)
            regular = root / "config.json"
            regular.write_text("{}")
            missing = root / "missing.json"
            opened = []
            next_fd = [1000]
            # Windows has no O_DIRECTORY; the fake namespace supplies a
            # non-zero sentinel so the derived flag is testable everywhere.
            directory_bit = 0x10000

            def synthetic_open(path, flags, *args, **kwargs):
                opened.append((Path(path), flags))
                if not Path(path).exists():
                    raise FileNotFoundError(path)
                fd = next_fd[0]
                next_fd[0] += 1
                return fd

            def fake_os_namespace(with_nonblock):
                values = dict(O_RDONLY=os.O_RDONLY,
                              O_CLOEXEC=getattr(os, "O_CLOEXEC", 0),
                              O_DIRECTORY=directory_bit,
                              open=synthetic_open, close=lambda fd: None,
                              fspath=os.fspath)
                if with_nonblock:
                    values["O_NONBLOCK"] = getattr(os, "O_NONBLOCK", 0)
                return types.SimpleNamespace(**values)

            with patch.object(daemon_notify, "os", fake_os_namespace(True)), \
                    patch.object(daemon_notify, "select", fake_select):
                watcher = daemon_notify._Kqueue(
                    (root,), (regular, missing), Stop())
                try:
                    self.assertEqual(watcher.unwatchable, (missing,))
                    directory_flags = next(flags for path, flags in opened
                                           if path == root)
                    file_flags = next(flags for path, flags in opened
                                      if path == regular)
                    self.assertTrue(directory_flags & directory_bit)
                    self.assertFalse(file_flags & directory_bit)
                finally:
                    watcher.close()

            # Windows has no O_NONBLOCK; the portable flag derivation must
            # still register the same watched paths with a fake kqueue.
            with patch.object(daemon_notify, "os", fake_os_namespace(False)), \
                    patch.object(daemon_notify, "select", fake_select):
                watcher = daemon_notify._Kqueue((root,), (regular,), Stop())
                try:
                    self.assertEqual(watcher.unwatchable, ())
                    self.assertEqual(len(watcher._fds), 2)
                finally:
                    watcher.close()

    def setUp(self):
        from mizu import daemon_notify
        if not daemon_notify.available():
            self.skipTest("no native wake backend on this platform")
        self.tmp = tempfile.TemporaryDirectory(prefix="native-wake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "wake").mkdir()

    @contextlib.contextmanager
    def _watcher_spy(self):
        """Record whether the production native wait actually consumed a watcher."""
        from mizu import daemon_notify
        calls = []
        real = daemon_notify.Watcher

        class Spy(real):
            def __init__(self, *args, **kwargs):
                calls.append("armed")
                super().__init__(*args, **kwargs)

            def wait(self, deadline):
                calls.append("wait")
                return super().wait(deadline)

        with patch.object(daemon_notify, "Watcher", Spy):
            yield calls

    def _assert_native_idle_wait(self):
        # A settled daemon blocked on a notification must not stat the watched
        # set on a timer; only the boundary fingerprint reads may touch it.
        baseline = daemon_wake.fingerprint(self.root)
        original = Path.stat
        marks = []

        def counting(self, *a, **k):
            marks.append(time.monotonic())
            return original(self, *a, **k)

        stop = threading.Event()
        deadline = time.monotonic() + 0.5
        with self._watcher_spy() as calls, patch.object(Path, "stat", counting):
            reason = daemon_wake.wait(stop, self.root, baseline, deadline)
        self.assertEqual(reason, "deadline")
        # Output equality cannot tell the native wait from the portable loop:
        # both produce two boundary reads with this deadline. Assert the native
        # watcher actually blocked, so the fallback cannot satisfy this test.
        self.assertIn("wait", calls, "native watcher was not consumed (fallback ran)")
        if len(marks) >= 2:
            span = marks[-1] - marks[0]
            self.assertGreater(span, 0.4)
            interior = [m for m in marks if marks[0] + 0.05 < m < marks[-1] - 0.05]
            self.assertEqual(interior, [])

    def test_idle_native_wait_makes_no_periodic_stat_calls(self):
        self._assert_native_idle_wait()

    def test_idle_wait_assertion_rejects_forced_fallback(self):
        # Known-bad control: with the backend declared unavailable the same
        # assertion must fail, proving it is not satisfied by the portable loop.
        from mizu import daemon_notify
        with patch.object(daemon_notify, "available", return_value=False):
            with self.assertRaises(AssertionError):
                self._assert_native_idle_wait()

    def test_native_wait_ignores_unwatched_root_churn(self):
        # The project root churns with ledger journals. Writes to an unwatched
        # root child must not wake the daemon or recompute the fingerprint.
        junk = self.root / "ledger-journal.jsonl"
        junk.write_bytes(b"x" * 10)
        baseline = daemon_wake.fingerprint(self.root)
        original = Path.stat
        marks = []

        def counting(self, *a, **k):
            marks.append(time.monotonic())
            return original(self, *a, **k)

        deadline = time.monotonic() + 0.6

        def churn():
            while time.monotonic() < deadline - 0.05:
                with open(junk, "ab") as stream:
                    stream.write(b"x" * 10)
                time.sleep(0.02)

        thread = threading.Thread(target=churn)
        thread.start()
        with patch.object(Path, "stat", counting):
            reason = daemon_wake.wait(threading.Event(), self.root, baseline, deadline)
        thread.join()
        self.assertEqual(reason, "deadline")
        self.assertGreaterEqual(time.monotonic(), deadline - 0.02)
        if len(marks) >= 2:
            interior = [m for m in marks if marks[0] + 0.05 < m < marks[-1] - 0.05]
            self.assertEqual(interior, [])

    def _fd_count(self):
        for candidate in ("/proc/self/fd", "/dev/fd"):
            directory = Path(candidate)
            if directory.is_dir():
                return len(os.listdir(directory))
        self.skipTest("no descriptor inventory on this platform")

    def test_stop_bridge_acquired_before_any_backend_descriptor(self):
        # A constructor that fails before acquiring the bridge must not have
        # opened a backend descriptor at all, on any supported platform.
        from mizu import daemon_notify
        order = []

        def fail(stop):
            order.append("bridge")
            raise OSError("boom")

        class Backend:
            def __init__(self, *args, **kwargs):
                order.append("backend")

        with patch.object(daemon_notify, "stop_bridge", fail):
            for platform_name, backend in (
                    ("linux", "_Inotify"), ("darwin", "_Kqueue"),
                    ("win32", "_WinChange")):
                order.clear()
                with patch.object(sys, "platform", platform_name), \
                        patch.object(daemon_notify, backend, Backend):
                    with self.assertRaises(OSError):
                        daemon_notify.Watcher(self.root, None, (), threading.Event())
                self.assertEqual(order, ["bridge"], platform_name)

    def test_thread_start_failure_releases_stop_bridge_descriptors(self):
        if sys.platform.startswith("win32"):
            self.skipTest("POSIX descriptors only")
        from mizu import daemon_notify
        self._fd_count()
        gc.collect()
        before = self._fd_count()
        with patch.object(daemon_notify._thread, "start_new_thread",
                          side_effect=OSError("no thread")):
            for _ in range(5):
                with self.assertRaises(OSError):
                    daemon_notify.stop_bridge(threading.Event())
        gc.collect()
        self.assertLessEqual(self._fd_count() - before, 1)

    def test_failed_inotify_registration_releases_descriptor(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux inotify only")
        import ctypes
        from mizu import daemon_notify
        self._fd_count()
        before = self._fd_count()
        real_cdll = ctypes.CDLL

        class FailingWatch:
            argtypes = None
            restype = None

            def __call__(self, *args):
                raise OSError("watch registration failed")

        class FailingLibc:
            def __init__(self, *args, **kwargs):
                self._real = real_cdll(*args, **kwargs)

            def __getattr__(self, name):
                if name == "inotify_add_watch":
                    return FailingWatch()
                return getattr(self._real, name)

        class Bridge:
            def fileno(self):
                return -1

        bridge = Bridge()
        with patch.object(daemon_notify.ctypes, "CDLL", FailingLibc):
            for _ in range(5):
                with self.assertRaises(OSError):
                    daemon_notify._Inotify((self.root,), (), bridge, {})
        self.assertLessEqual(self._fd_count() - before, 1)

    def test_windows_wait_does_not_rearm_a_discarded_handle(self):
        from mizu import daemon_notify

        watcher = object.__new__(daemon_notify._WinChange)
        watcher._handles = [11]
        watcher._wait_handles = (11, 12)
        watcher._k32 = Mock(spec=("WaitForMultipleObjects",))
        watcher._k32.WaitForMultipleObjects.return_value = \
            watcher._WAIT_OBJECT_0
        watcher.wait(time.monotonic() + 0.01)
        self.assertEqual([name for name, _, _ in watcher._k32.mock_calls],
                         ["WaitForMultipleObjects"])

        watcher._k32 = Mock(spec=("WaitForMultipleObjects",))
        watcher._k32.WaitForMultipleObjects.return_value = \
            watcher._WAIT_FAILED
        with patch.object(daemon_notify.ctypes, "get_last_error",
                          lambda: 5, create=True):
            with self.assertRaises(OSError):
                watcher.wait(None)

    def test_watcher_rotations_share_one_stop_bridge(self):
        from mizu import daemon_notify
        stop = threading.Event()
        first = daemon_notify.stop_bridge(stop)
        self.assertIs(daemon_notify.stop_bridge(stop), first)
        # Closing a watcher releases its native handles, not the daemon bridge.
        baseline = daemon_wake.fingerprint(self.root)
        with daemon_notify.Watcher(self.root, None, (), stop) as watcher:
            watcher.wait(time.monotonic() + 0.01)
        self.assertIs(daemon_notify.stop_bridge(stop), first)
        stop.set()

    def test_stop_bridge_descriptors_released_on_retirement(self):
        if sys.platform.startswith("win32"):
            self.skipTest("POSIX descriptors only")
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.is_dir():
            fd_dir = Path("/dev/fd")
        if not fd_dir.is_dir():
            self.skipTest("no descriptor inventory on this platform")
        from mizu import daemon_notify
        gc.collect()
        before = len(os.listdir(fd_dir))
        for _ in range(5):
            stop = threading.Event()
            with tempfile.TemporaryDirectory(prefix="native-rotate-") as d:
                root = Path(d)
                (root / "wake").mkdir()
                daemon_wake.wait(stop, root, daemon_wake.fingerprint(root),
                                 time.monotonic() + 0.01)
            stop.set()
            del stop
        # The monitor thread holds the stop event until it observes the stop,
        # so the bridge finalizer can run after the last rotation returns. Wait
        # for retirement with a deadline rather than sampling in the middle of
        # an asynchronous close and blaming the product for the observation.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            gc.collect()
            if len(os.listdir(fd_dir)) - before <= 1:
                break
            time.sleep(0.001)
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after - before, 1,
                             "stop bridge descriptors were not retired")

    def test_restored_include_parent_and_file_wake_without_deadline(self):
        # The parent is absent when the daemon settles. Creating it must wake
        # and re-arm coverage of the new directory; restoring the fragment
        # inside it must then wake on its own, with no timer or unrelated hint.
        from mizu import daemon_notify
        cfg = self.root / "config.toml"
        cfg.write_text("idle_seconds = 60\n")
        fragment = self.root / "inc" / "fragment.toml"
        baseline = daemon_wake.fingerprint(self.root, cfg, config_files=(cfg, fragment))
        self.assertIn(("config", str(fragment), None), baseline)

        stop = threading.Event()
        result = []
        bridges = []
        real = daemon_notify.Watcher
        armed = threading.Event()
        arms = []

        class Spy(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                bridges.append(daemon_notify.stop_bridge(stop))
                arms.append(1)
                armed.set()

        self.addCleanup(stop.set)
        # This test owns its bridge explicitly so a later descriptor-retirement
        # test is not racing the finalizer of a stop event kept alive by a thread.
        self.addCleanup(lambda: bridges[0].close() if bridges else None)

        def waiter():
            result.append(daemon_wake.wait(stop, self.root, baseline, None,
                                           config_file=cfg, config_files=(cfg, fragment)))

        thread = threading.Thread(target=waiter)
        with patch.object(daemon_notify, "Watcher", Spy):
            thread.start()
            self.assertTrue(armed.wait(3.0), "initial watcher was not armed")
            first_arms = len(arms)
            (self.root / "inc").mkdir()
            deadline = time.monotonic() + 3.0
            while len(arms) <= first_arms and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(len(arms), first_arms, "no re-arm after parent appeared")
            fragment.write_text("[limits]\nidle_seconds = 15\n")
            thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive(), "wait did not wake on restored fragment")
        self.assertEqual(result, ["change"])

    def test_native_wait_wakes_on_watched_file_change(self):
        baseline = daemon_wake.fingerprint(self.root)

        def mutate():
            time.sleep(0.05)
            (self.root / "control.json").write_text("{}")

        thread = threading.Thread(target=mutate)
        thread.start()
        started = time.monotonic()
        reason = daemon_wake.wait(threading.Event(), self.root, baseline, None)
        thread.join()
        self.assertEqual(reason, "change")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_native_wait_wakes_on_directory_hint(self):
        baseline = daemon_wake.fingerprint(self.root)
        wake = self.root / "wake"

        def hint():
            time.sleep(0.05)
            # Windows change notifications report child entries, not a change
            # to the watched directory itself. Publish the writer's child hint
            # so every backend observes an entry event, then bump the mtime.
            (wake / "hint.json").write_text("{}")
            fs.touch_directory(wake)

        thread = threading.Thread(target=hint)
        thread.start()
        started = time.monotonic()
        reason = daemon_wake.wait(threading.Event(), self.root, baseline,
                                  started + 3.0)
        elapsed = time.monotonic() - started
        thread.join(timeout=3.0)
        self.assertEqual(reason, "change")
        self.assertLess(elapsed, 3.0)

    def test_native_wait_honours_exact_deadline(self):
        baseline = daemon_wake.fingerprint(self.root)
        deadline = time.monotonic() + 0.2
        reason = daemon_wake.wait(threading.Event(), self.root, baseline, deadline)
        self.assertEqual(reason, "deadline")
        self.assertGreaterEqual(time.monotonic(), deadline - 0.02)

    def test_registered_path_with_no_remote_events_reports_unverified_coverage(self):
        """A registered path that never delivers a remote event still reports
        coverage as unverified: registration alone proves no remote delivery.
        This exercises the network-mount shape (successful registration, no
        kernel event) instead of injecting a pre-populated unwatchable tuple.
        """
        from mizu import daemon_notify
        calls = []
        some_path = self.root / "remote-mount"

        class FakeWatcher:
            # Registration SUCCEEDED: nothing is unwatchable, yet wait() never
            # observes a remote-origin change (the network-mount condition).
            unwatchable = ()
            paths = (some_path,)

            def __init__(self, *args, **kwargs):
                calls.append("armed")

            def wait(self, deadline):
                calls.append("wait")
                stop.wait(max(0, deadline - time.monotonic()))

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 0.2
        captured = io.StringIO()
        with patch.object(daemon_notify, "available", return_value=True), \
                patch.object(daemon_notify, "Watcher", FakeWatcher), \
                patch.object(daemon_wake, "_wait_portable",
                             side_effect=AssertionError("polling fallback ran")), \
                contextlib.redirect_stdout(captured):
            reason = daemon_wake.wait(stop, self.root, baseline, deadline)

        self.assertEqual(reason, "deadline")
        self.assertGreaterEqual(time.monotonic(), deadline - 0.02)
        # One arming consumed once; the re-arm on the deadline path does not
        # add a second wait, so no polling fallback was entered.
        self.assertIn("armed", calls)
        self.assertEqual(calls.count("wait"), 1, calls)
        lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
        coverage = [e for e in events if e.get("event") == "native_wake_coverage"]
        self.assertTrue(coverage,
                        "registered path did not report a coverage fact")
        for event in coverage:
            self.assertEqual(event,
                             {"event": "native_wake_coverage",
                              "paths": ["remote-mount"],
                              "local_origin": "covered",
                              "remote_origin": "unverified"})
        # No registration failure here, and no line claims remote coverage.
        self.assertFalse([e for e in events
                          if e.get("event") == "native_wake_unwatchable"])
        for event in events:
            self.assertNotEqual(event.get("remote_origin"), "covered")
            self.assertNotEqual(event.get("remote_origin"), "established")
        self.assertFalse(any("watched" in str(v) for e in events
                             for v in e.values()),
                         "a fact claimed the path was watched")

    def test_dropped_notification_still_readmits_at_exact_deadline(self):
        """A dropped hint notification must not strand a settled daemon: an
        armed exact deadline re-arms the watcher, the fingerprint re-check sees
        the change the writer published, and no polling loop starts. This is
        the 'hint OR exact deadline' invariant on the remaining leg — a change
        whose kernel event was lost is still observed within the deadline.
        """
        from mizu import daemon_notify
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        wait_calls = []
        armed = []

        class DroppingWatcher:
            # Registration succeeds and coverage is reported, but wait()
            # never surfaces the writer's hint: the notification is lost
            # (queue overflow, coalesced remote-origin event). wait() blocks
            # only until the exact deadline or stop.
            unwatchable = ()
            paths = (self.root / "wake",)

            def __init__(self, *args, **kwargs):
                armed.append(time.monotonic())

            def wait(self, deadline):
                wait_calls.append(time.monotonic())
                stop.wait(max(0, deadline - time.monotonic()))

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        deadline = time.monotonic() + 0.25

        def bump():
            # The writer publishes a watched hint while the daemon is parked;
            # the fake watcher drops the event that would have surfaced it.
            time.sleep(0.05)
            (self.root / "wake" / "hint.json").write_text("{}")

        thread = threading.Thread(target=bump)
        thread.start()
        captured = io.StringIO()
        with patch.object(daemon_notify, "available", return_value=True), \
                patch.object(daemon_notify, "Watcher", DroppingWatcher), \
                patch.object(daemon_wake, "_wait_portable",
                             side_effect=AssertionError("polling fallback ran")), \
                contextlib.redirect_stdout(captured):
            started = time.monotonic()
            reason = daemon_wake.wait(stop, self.root, baseline, deadline)
            elapsed = time.monotonic() - started
        thread.join(timeout=1.0)

        # The dropped event did not wake it early; the deadline did, and the
        # re-arm's fingerprint check found the hint. So the result is a change
        # re-admitted at the deadline bound, not a bare deadline timeout.
        self.assertEqual(reason, "change")
        self.assertGreaterEqual(elapsed, deadline - started - 0.02)
        self.assertGreaterEqual(len(wait_calls), 1)
        # A first arm parked before the mutation; re-arming after the deadline
        # is what caught the dropped change.
        self.assertGreaterEqual(len(armed), 2, armed)
        events = [json.loads(line) for line in captured.getvalue().splitlines()
                  if line.strip()]
        coverage = [e for e in events if e.get("event") == "native_wake_coverage"]
        self.assertTrue(coverage)
        for event in coverage:
            self.assertEqual(event["remote_origin"], "unverified")

    def test_unwatchable_registration_failure_is_reported_as_a_fact(self):
        """A failed registration is still reported, alongside the coverage
        fact that every arming emits.
        """
        from mizu import daemon_notify
        some_path = self.root / "remote-mount"

        class FakeWatcher:
            unwatchable = (some_path,)
            paths = ()

            def __init__(self, *args, **kwargs):
                pass

            def wait(self, deadline):
                stop.wait(max(0, deadline - time.monotonic()))

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 0.2
        captured = io.StringIO()
        with patch.object(daemon_notify, "available", return_value=True), \
                patch.object(daemon_notify, "Watcher", FakeWatcher), \
                patch.object(daemon_wake, "_wait_portable",
                             side_effect=AssertionError("polling fallback ran")), \
                contextlib.redirect_stdout(captured):
            reason = daemon_wake.wait(stop, self.root, baseline, deadline)

        self.assertEqual(reason, "deadline")
        events = [json.loads(line) for line in captured.getvalue().splitlines()
                  if line.strip()]
        unwatchable = [e for e in events
                       if e.get("event") == "native_wake_unwatchable"]
        self.assertTrue(unwatchable, "registration failure not reported")
        for event in unwatchable:
            self.assertEqual(event, {"event": "native_wake_unwatchable",
                                     "paths": ["remote-mount"]})
        coverage = [e for e in events if e.get("event") == "native_wake_coverage"]
        self.assertTrue(coverage)
        for event in coverage:
            self.assertEqual(event["paths"], [])
            self.assertEqual(event["remote_origin"], "unverified")

    def test_arming_failure_does_not_resume_periodic_polling(self):
        # A reported native failure must not silently become the one-second
        # loop: an idle daemon still makes no interior stat calls or wakeups.
        from mizu import daemon_notify
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()
        deadline = time.monotonic() + 0.25
        original = Path.stat
        marks = []

        def counting(self, *a, **k):
            marks.append(time.monotonic())
            return original(self, *a, **k)

        with patch.object(daemon_notify, "stop_bridge",
                          side_effect=OSError("boom")), \
                patch.object(daemon_wake, "POLL_SECONDS", 0.02), \
                patch.object(Path, "stat", counting):
            reason = daemon_wake.wait(stop, self.root, baseline, deadline)
        self.assertEqual(reason, "deadline")
        self.assertGreaterEqual(time.monotonic(), deadline - 0.02)
        self.assertTrue(marks)
        self.assertLess(max(marks) - min(marks), 0.05,
                        "fingerprint recomputed after arming failure")

    def test_arming_failure_reports_change_already_present(self):
        from mizu import daemon_notify
        baseline = daemon_wake.fingerprint(self.root)
        (self.root / "control.json").write_text("{}")
        with patch.object(daemon_notify, "stop_bridge", side_effect=OSError("boom")):
            reason = daemon_wake.wait(threading.Event(), self.root, baseline, None)
        self.assertEqual(reason, "change")

    def test_arming_failure_still_honours_stop(self):
        from mizu import daemon_notify
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()

        def stopper():
            time.sleep(0.05)
            stop.set()

        thread = threading.Thread(target=stopper)
        thread.start()
        with patch.object(daemon_notify, "stop_bridge", side_effect=OSError("boom")):
            reason = daemon_wake.wait(stop, self.root, baseline, None)
        thread.join()
        self.assertEqual(reason, "stop")

    def test_native_wait_wakes_on_stop(self):
        baseline = daemon_wake.fingerprint(self.root)
        stop = threading.Event()

        def stopper():
            time.sleep(0.05)
            stop.set()

        thread = threading.Thread(target=stopper)
        thread.start()
        started = time.monotonic()
        reason = daemon_wake.wait(stop, self.root, baseline, None)
        thread.join()
        self.assertEqual(reason, "stop")
        self.assertLess(time.monotonic() - started, 1.0)


class WinChangePathTests(unittest.TestCase):
    """Win32 registration needs a full path; the ABI itself is mocked here."""

    def setUp(self):
        from mizu import daemon_notify
        self.daemon_notify = daemon_notify
        self.kernel = Mock()
        self.paths = []

        def register(path, subtree, notify_filter):
            self.paths.append(path)
            return 123  # a valid, non-INVALID notification handle

        self.kernel.FindFirstChangeNotificationW.side_effect = register
        patcher = patch.object(ctypes, "WinDLL", Mock(return_value=self.kernel),
                               create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _watch(self, directory):
        stop = Mock()
        stop.event.return_value = 0x5A5A  # sentinel stop handle
        watcher = self.daemon_notify._WinChange(
            (directory,), (), stop, frozenset())
        self.addCleanup(watcher.close)
        return watcher

    def test_registration_uses_absolute_path(self):
        relative = Path("relative-watch-dir")
        watcher = self._watch(relative)
        self.assertEqual(self.paths, [os.path.abspath(relative)])
        self.assertEqual(watcher.unwatchable, ())

        absolute = Path(os.path.abspath(relative))
        self.paths.clear()
        watcher = self._watch(absolute)
        self.assertEqual(self.paths, [os.fspath(absolute)])
        self.assertEqual(watcher.unwatchable, ())


if __name__ == "__main__":
    unittest.main()
