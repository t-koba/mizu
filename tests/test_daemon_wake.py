"""Portable daemon wake detection: metadata fingerprint and interruptible wait."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mizu import daemon_wake


class DaemonWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="daemon-wake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fingerprint_stable_and_identity_aware(self):
        self.assertEqual(daemon_wake.fingerprint(None), ())
        base = daemon_wake.fingerprint(self.root)
        self.assertEqual(daemon_wake.fingerprint(self.root), base)
        # A watched file appearing changes the fingerprint.
        (self.root / "control.json").write_text("{}")
        self.assertNotEqual(daemon_wake.fingerprint(self.root), base)
        after_file = daemon_wake.fingerprint(self.root)
        # Rewriting the same-size content still changes mtime/inode.
        (self.root / "control.json").write_text("[]")
        self.assertNotEqual(daemon_wake.fingerprint(self.root), after_file)
        after_rewrite = daemon_wake.fingerprint(self.root)
        # A watched directory gaining a child changes the fingerprint.
        (self.root / "wake").mkdir()
        self.assertNotEqual(daemon_wake.fingerprint(self.root), after_rewrite)
        after_dir = daemon_wake.fingerprint(self.root)
        (self.root / "wake" / "worker").write_text("")
        self.assertNotEqual(daemon_wake.fingerprint(self.root), after_dir)

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
                         len(daemon_wake.WATCHED_FILES) + len(daemon_wake.WATCHED_DIRS) + 1)
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

    def test_fingerprint_detects_root_and_work_items_hints(self):
        watched = self.root / "work-items"
        watched.mkdir()
        after_dir = daemon_wake.fingerprint(self.root)
        st = self.root.stat()
        os.utime(self.root, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000),
                 follow_symlinks=False)
        self.assertNotEqual(daemon_wake.fingerprint(self.root), after_dir)
        after_root = daemon_wake.fingerprint(self.root)
        st = watched.stat()
        os.utime(watched, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000),
                 follow_symlinks=False)
        self.assertNotEqual(daemon_wake.fingerprint(self.root), after_root)

    def test_wait_returns_change_already_present(self):
        (self.root / "control.json").write_text("{}")
        baseline = daemon_wake.fingerprint(self.root)
        (self.root / "control.json").write_text("[]")
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
