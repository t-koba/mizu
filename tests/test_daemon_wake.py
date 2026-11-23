"""Portable daemon wake detection: metadata fingerprint and interruptible wait."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

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

    def test_fingerprint_detects_nested_revision(self):
        item = self.root / "work-items" / "item-1"
        item.mkdir(parents=True)
        (item / "1.json").write_text("{}")
        base = daemon_wake.fingerprint(self.root)
        # A new revision inside an existing entity dir does not change the
        # parent directory mtime, but must still wake the daemon.
        (item / "2.json").write_text("{}")
        self.assertNotEqual(daemon_wake.fingerprint(self.root), base)

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
