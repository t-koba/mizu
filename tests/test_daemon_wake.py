"""Portable daemon wake detection: metadata fingerprint and interruptible wait."""
import contextlib
import os
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


if __name__ == "__main__":
    unittest.main()
