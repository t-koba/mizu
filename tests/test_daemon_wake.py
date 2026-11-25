"""Portable daemon wake detection: metadata fingerprint and interruptible wait."""
import contextlib
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        gc.collect()
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after - before, 1)

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

        def hint():
            time.sleep(0.05)
            fs.touch_directory(self.root / "wake")

        thread = threading.Thread(target=hint)
        thread.start()
        started = time.monotonic()
        reason = daemon_wake.wait(threading.Event(), self.root, baseline, None)
        thread.join()
        self.assertEqual(reason, "change")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_native_wait_honours_exact_deadline(self):
        baseline = daemon_wake.fingerprint(self.root)
        deadline = time.monotonic() + 0.2
        reason = daemon_wake.wait(threading.Event(), self.root, baseline, deadline)
        self.assertEqual(reason, "deadline")
        self.assertGreaterEqual(time.monotonic(), deadline - 0.02)

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
        self.assertLessEqual(len(marks), 1, "interior fingerprint reads after arming failure")

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


if __name__ == "__main__":
    unittest.main()
