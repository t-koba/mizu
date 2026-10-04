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


    def test_concurrent_budget_admission_serialized_when_os_lock_is_per_process(self):
        # Budget admission holds budget.lock across read-modify-write; with a
        # per-process OS primitive (Windows msvcrt) threads would race without
        # the in-process guard and exceed the limit.
        import concurrent.futures
        from mizu.budget import Budget
        from mizu.errors import LimitExceeded

        root = Path(self.temp.name) / "budget"
        root.mkdir()

        def attempt(i):
            try:
                Budget(root, 12).take(str(i))
                return 1
            except LimitExceeded:
                return 0

        with patch("mizu.fs._platform.lock_fd", return_value=True), \
             patch("mizu.fs._platform.unlock_fd", return_value=None):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(attempt, range(40))), 12)
            self.assertEqual(Budget(root, 12).usage()["used"], 12)


    def test_aliased_paths_share_one_guard_entry(self):
        # A symlinked parent must not bypass the in-process guard: both
        # spellings address the same lock file.
        import os
        real = Path(self.temp.name) / "real"
        real.mkdir()
        link = Path(self.temp.name) / "link"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        first = real / "guard.lock"
        second = link / "guard.lock"
        with patch("mizu.fs._platform.lock_fd", return_value=True), \
             patch("mizu.fs._platform.unlock_fd", return_value=None):
            with lock(first):
                with self.assertRaises(Busy):
                    with lock(second, blocking=False):
                        pass

    def test_blocking_acquire_fails_busy_after_bound(self):
        # A re-entrant blocking acquire must fail as Busy after the
        # liveness bound instead of hanging the daemon thread.
        import time
        import mizu.fs as fs
        with patch("mizu.fs._platform.lock_fd", return_value=True), \
             patch("mizu.fs._platform.unlock_fd", return_value=None):
            with lock(self.path):
                with patch.object(fs, "_LOCAL_BLOCKING_TIMEOUT", 0.2):
                    start = time.monotonic()
                    with self.assertRaises(Busy):
                        with lock(self.path, blocking=True):
                            pass
                    self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()
