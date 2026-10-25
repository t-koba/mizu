"""Regression: every blocking lock acquire is bounded (ADR-026).

The intra-process guard was bounded, but the cross-process (kernel)
acquire was not: on POSIX a blocking ``flock`` waited forever, so a
contended lock could hang the caller instead of failing as ``Busy``.
Both halves now share one bound, and these tests pin that.

The contended acquire runs in its own process on purpose: against the
unbounded implementation the acquirer would hang, and a hanging test
process is a worse failure than a failing one.
"""
from __future__ import annotations

import errno
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mizu import fs, platform
from mizu.errors import Busy

HOLDER = """
import sys, time
from pathlib import Path
sys.path.insert(0, {src!r})
from mizu.fs import lock
with lock(Path({path!r})):
    open({marker!r}, "w").write("locked")
    time.sleep({hold!r})
"""

ACQUIRER = """
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
from mizu import fs, platform
platform.LOCK_TIMEOUT = {bound!r}
try:
    with fs.lock(Path({path!r}), blocking={blocking!r}):
        print("acquired")
except Exception as exc:
    print(type(exc).__name__ + ": " + str(exc))
    sys.exit(3)
"""


class LockBoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mizu-lock-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "device.lock"
        self.marker = Path(self.temp.name) / "held"

    def _child(self, script: str) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", script],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._stop, child)
        return child

    @staticmethod
    def _stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        if child.stderr is not None:
            child.stderr.close()

    def _holder(self, hold: float = 60.0) -> subprocess.Popen:
        script = HOLDER.format(src=str(ROOT / "src"), path=str(self.path),
                               marker=str(self.marker), hold=hold)
        child = self._child(script)
        deadline = time.monotonic() + 60
        while not self.marker.exists():
            if child.poll() is not None:
                self.fail("holder exited before acquiring the lock")
            if time.monotonic() > deadline:
                self.fail("holder never acquired the lock")
            time.sleep(0.02)
        return child

    def _acquire(self, *, bound: float, blocking: bool, timeout: float = 30.0):
        script = ACQUIRER.format(src=str(ROOT / "src"), path=str(self.path),
                                 bound=bound, blocking=blocking)
        return subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=timeout)

    def test_cross_process_blocking_acquire_is_bounded(self):
        self._holder()
        start = time.monotonic()
        outcome = self._acquire(bound=0.5, blocking=True, timeout=30)
        elapsed = time.monotonic() - start
        self.assertEqual(outcome.returncode, 3, outcome.stdout + outcome.stderr)
        self.assertIn("Busy", outcome.stdout)
        self.assertLess(elapsed, 20)

    def test_non_blocking_acquire_fails_fast(self):
        self._holder()
        outcome = self._acquire(bound=0.5, blocking=False, timeout=30)
        self.assertEqual(outcome.returncode, 3, outcome.stdout + outcome.stderr)
        self.assertIn("Busy", outcome.stdout)

    def test_acquire_succeeds_once_the_holder_releases(self):
        child = self._holder(hold=0.5)
        child.wait(timeout=60)
        with fs.lock(self.path):
            pass

    def test_retry_does_not_acquire_after_deadline_even_if_lock_frees(self):
        for primitive in ("posix", "windows"):
            with self.subTest(primitive=primitive):
                attempts = []

                def acquire(*args):
                    attempts.append(args)
                    if len(attempts) == 1:
                        raise OSError(errno.EAGAIN, "contended")

                if primitive == "posix":
                    fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=acquire)
                    msvcrt = None
                else:
                    fcntl = None
                    msvcrt = SimpleNamespace(LK_NBLCK=1, locking=acquire)
                # Initial deadline, first contention before expiry, then a
                # late poll after expiry where the fake lock would be free.
                with patch.object(platform, "_fcntl", fcntl), \
                     patch.object(platform, "_msvcrt", msvcrt), \
                     patch.object(platform, "LOCK_TIMEOUT", 1.0), \
                     patch.object(platform.time, "monotonic", side_effect=[0.0, 0.1, 2.0]), \
                     patch.object(platform.time, "sleep", return_value=None):
                    with self.assertRaises(BlockingIOError):
                        platform.lock_fd(7, blocking=True)
                self.assertEqual(len(attempts), 1)

    def test_both_halves_share_one_bound(self):
        self.assertEqual(platform.LOCK_TIMEOUT, 30.0)
        self.assertEqual(platform.LOCK_TIMEOUT, fs._LOCAL_BLOCKING_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
