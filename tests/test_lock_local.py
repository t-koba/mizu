"""In-process lock exclusion survives OS locks that are per-process (Windows msvcrt)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT  # noqa: F401 (ensures src on sys.path)
from mizu.errors import Busy
from mizu.fs import lock


class LocalLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-local-lock-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "guard.lock"

    def test_second_nonblocking_acquire_refused_when_os_lock_is_per_process(self):
        # Simulate an OS primitive that never contends within one process
        # (Windows msvcrt): the in-process guard must still refuse.
        with patch("mizu.fs._platform.lock_fd", return_value=True), \
             patch("mizu.fs._platform.unlock_fd", return_value=None):
            with lock(self.path):
                with self.assertRaises(Busy):
                    with lock(self.path, blocking=False):
                        pass
            with lock(self.path, blocking=False):
                pass

    def test_peer_sees_busy_while_held_then_acquires_after_release(self):
        entered = threading.Event()
        proceed = threading.Event()
        outcome: list[str] = []

        def peer():
            entered.set()
            proceed.wait(timeout=10)
            try:
                with lock(self.path, blocking=False):
                    outcome.append("unexpected")
            except Busy:
                outcome.append("busy")
            proceed.clear()

        with lock(self.path):
            other = threading.Thread(target=peer)
            other.start()
            self.assertTrue(entered.wait(timeout=10))
            proceed.set()
            other.join(timeout=10)
            self.assertEqual(outcome, ["busy"])
        with lock(self.path, blocking=False):
            pass
        other.join(timeout=10)
        self.assertFalse(other.is_alive())


if __name__ == "__main__":
    unittest.main()
