"""In-process lock exclusion survives OS locks that are per-process (Windows msvcrt)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT  # noqa: F401 (ensures src on sys.path)
from mizu import fs, sandbox
from mizu.errors import Busy
from mizu.fs import lock


class LocalLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-local-lock-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "guard.lock"

    def test_pending_kernel_acquire_is_not_cleanup_ownership(self):
        entered = threading.Event()
        proceed = threading.Event()
        outcome = []

        def slow_lock_fd(*args, **kwargs):
            entered.set()
            if not proceed.wait(timeout=10):
                raise TimeoutError("kernel acquisition was not released")

        def acquire():
            try:
                with lock(self.path):
                    outcome.append(fs.held(self.path))
            except BaseException as exc:
                outcome.append(exc)

        with patch("mizu.fs._platform.lock_fd", side_effect=slow_lock_fd), \
             patch("mizu.fs._platform.unlock_fd"), \
             patch("mizu.sandbox._remove_containers") as remove:
            other = threading.Thread(target=acquire)
            other.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertFalse(fs.held(self.path))
                self.assertEqual(
                    sandbox.cleanup(None, Path(self.temp.name), owner=True,
                                    exclusion=self.path),
                    {"removed": 0, "skipped": "role-active"})
                remove.assert_not_called()
            finally:
                proceed.set()
                other.join(timeout=10)
            self.assertFalse(other.is_alive())
        self.assertEqual(outcome, [True])
        self.assertFalse(fs.held(self.path))

    def test_held_tracks_successful_body_and_release(self):
        with patch("mizu.fs._platform.lock_fd"), \
             patch("mizu.fs._platform.unlock_fd"):
            for error in (None, Busy("body busy"), RuntimeError("body failed")):
                with self.subTest(error=error):
                    self.assertFalse(fs.held(self.path))
                    def body():
                        with lock(self.path):
                            self.assertTrue(fs.held(self.path))
                            if error is not None:
                                raise error
                    if error is None:
                        body()
                    else:
                        with self.assertRaises(type(error)):
                            body()
                    self.assertFalse(fs.held(self.path))

    def test_failed_kernel_acquire_is_not_held(self):
        with patch("mizu.fs._platform.unlock_fd"):
            for error, expected in ((BlockingIOError(), Busy), (OSError(), OSError)):
                with self.subTest(error=error), \
                     patch("mizu.fs._platform.lock_fd", side_effect=error):
                    with self.assertRaises(expected):
                        with lock(self.path):
                            self.fail("failed kernel acquisition entered body")
                    self.assertFalse(fs.held(self.path))
            # A failed acquisition must also release the local guard.
            with patch("mizu.fs._platform.lock_fd"):
                with lock(self.path, blocking=False):
                    self.assertTrue(fs.held(self.path))
            self.assertFalse(fs.held(self.path))

    def test_successful_owner_cleanup_removes_containers(self):
        root = Path(self.temp.name)
        with patch("mizu.fs._platform.lock_fd") as acquire, \
             patch("mizu.fs._platform.unlock_fd"), \
             patch("mizu.sandbox._remove_containers",
                   return_value={"removed": 1}) as remove:
            with lock(self.path):
                self.assertEqual(
                    sandbox.cleanup(None, root, owner=True, exclusion=self.path),
                    {"removed": 1})
                remove.assert_called_once_with(None, root, None)
                acquire.assert_called_once()

    def test_second_nonblocking_acquire_refused_when_os_lock_is_per_process(self):
        # Simulate an OS primitive that never contends within one process
        # (Windows msvcrt): the in-process guard must still refuse.
        with patch("mizu.fs._platform.lock_fd", return_value=True), \
             patch("mizu.fs._platform.unlock_fd", return_value=None):
            with lock(self.path):
                with self.assertRaises(Busy):
                    with lock(self.path, blocking=False):
                        pass
                with fs.reuse_owned_locks([self.path]):
                    with lock(self.path, blocking=False):
                        pass
                self.assertTrue(fs.held(self.path))
                with self.assertRaises(Busy), lock(self.path, blocking=False):
                    self.fail("Lock reuse escaped its scope")
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
