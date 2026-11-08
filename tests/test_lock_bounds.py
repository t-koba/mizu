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

import dataclasses
import errno
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mizu import fs, platform
from mizu.errors import Busy, ConfigError
from mizu.config import load
from support import Fixture

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
try:
    with fs.lock_policy({bound!r}), fs.lock(Path({path!r}), blocking={blocking!r}):
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

    def test_kernel_retry_cadence_and_deadline_clipping(self):
        for primitive in ("posix", "windows"):
            # The lock frees 1 ms after the first failed probe. A 10-ms
            # cadence can acquire it within 20 ms; the standalone default
            # and a cadence longer than the deadline must not probe late.
            for interval in (10, None, 86400000):
                with self.subTest(primitive=primitive, interval=interval):
                    now = 0.0
                    attempts, sleeps = [], []

                    def acquire(*args):
                        attempts.append(now)
                        if now < 0.001:
                            raise OSError(errno.EAGAIN, "contended")

                    def sleep(seconds):
                        nonlocal now
                        sleeps.append(seconds)
                        now += seconds

                    fcntl = (SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=acquire)
                             if primitive == "posix" else None)
                    msvcrt = (SimpleNamespace(LK_NBLCK=1, locking=acquire)
                              if primitive == "windows" else None)
                    options = {} if interval is None else {"retry_interval_ms": interval}
                    with patch.object(platform, "_fcntl", fcntl), \
                            patch.object(platform, "_msvcrt", msvcrt), \
                            patch.object(platform.time, "monotonic", side_effect=lambda: now), \
                            patch.object(platform.time, "sleep", side_effect=sleep):
                        if interval == 10:
                            self.assertTrue(platform.lock_fd(7, timeout=0.02, **options))
                            self.assertEqual(attempts, [0, 0.01])
                            self.assertEqual(sleeps, [0.01])
                        else:
                            with self.assertRaises(BlockingIOError):
                                platform.lock_fd(7, timeout=0.02, **options)
                            self.assertEqual(attempts, [0])
                            self.assertEqual(sleeps, [0.02])

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
                     patch.object(platform.time, "monotonic", side_effect=[0.0, 0.1, 2.0]), \
                     patch.object(platform.time, "sleep", return_value=None):
                    with self.assertRaises(BlockingIOError):
                        platform.lock_fd(7, blocking=True, timeout=1, retry_interval_ms=10)
                self.assertEqual(len(attempts), 1)

    def test_kernel_zero_probes_once_and_nonblocking_ignores_timeout(self):
        for primitive in ("posix", "windows"):
            for blocking in (True, False):
                with self.subTest(primitive=primitive, blocking=blocking):
                    acquire = Mock(side_effect=OSError(errno.EAGAIN, "contended"))
                    fcntl = (SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=acquire)
                             if primitive == "posix" else None)
                    msvcrt = (SimpleNamespace(LK_NBLCK=1, locking=acquire)
                              if primitive == "windows" else None)
                    with patch.object(platform, "_fcntl", fcntl), \
                            patch.object(platform, "_msvcrt", msvcrt), \
                            patch.object(platform.time, "monotonic", return_value=0) as clock, \
                            patch.object(platform.time, "sleep") as sleep:
                        with self.assertRaises(BlockingIOError):
                            platform.lock_fd(7, blocking=blocking, timeout=0 if blocking else 86400,
                                             retry_interval_ms=10)
                        self.assertEqual(acquire.call_count, 1)
                        sleep.assert_not_called()
                        if not blocking:
                            clock.assert_not_called()
                    acquire.reset_mock(side_effect=True)
                    with patch.object(platform, "_fcntl", fcntl), \
                            patch.object(platform, "_msvcrt", msvcrt):
                        self.assertTrue(platform.lock_fd(7, blocking=blocking, timeout=0))

    def test_both_halves_share_one_bound(self):
        self.assertEqual(platform.LOCK_TIMEOUT, 30.0)
        self.assertEqual(platform.LOCK_TIMEOUT, fs._LOCAL_BLOCKING_TIMEOUT)


class LockPolicyTests(Fixture):
    def configured(self, seconds, interval=50):
        return dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, lock_timeout_seconds=seconds,
            lock_retry_interval_ms=interval))

    def test_default_and_configured_policy_reach_both_halves(self):
        path = self.root / "policy.lock"
        for seconds, interval in ((30, 50), (0, 1), (7, 10), (86400, 86400000)):
            with self.subTest(seconds=seconds, interval=interval):
                guard = Mock()
                guard.acquire.return_value = True
                with self.configured(seconds, interval).lock_policy(), \
                        patch.object(fs, "_LOCAL_LOCKS", {}), \
                        patch.object(fs.threading, "Lock", return_value=guard), \
                        patch.object(platform, "lock_fd") as kernel, \
                        patch.object(platform, "unlock_fd"):
                    with fs.lock(path):
                        pass
                    guard.acquire.assert_called_once_with(True, seconds)
                    self.assertEqual(kernel.call_args.kwargs, {"blocking": True, "timeout": seconds,
                                                              "retry_interval_ms": interval})
                    guard.release.assert_called_once_with()
        self.assertIsNone(fs._lock_timeout.get())

    def test_unvalidated_programmatic_policy_fails_before_binding(self):
        invalid = self.configured(True)
        with self.assertRaisesRegex(ConfigError, "limits.lock_timeout_seconds"):
            with invalid.lock_policy():
                self.fail("invalid policy must not bind")
        self.assertIsNone(fs._lock_timeout.get())

    def test_retry_interval_validation_in_file_and_programmatic_config(self):
        text = self.file.read_text()
        for value in (0, -1, 86400001, True, 1.5, "10"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "limits.lock_retry_interval_ms"):
                    self.configured(30, value).lock_policy()
                # TOML values have the same strict integer/range contract.
                self.file.write_text(text.replace(
                    "lock_retry_interval_ms = 50", "lock_retry_interval_ms = " + json.dumps(value)))
                with self.assertRaisesRegex(ConfigError, "limits.lock_retry_interval_ms"):
                    load(self.file)
                self.assertEqual(fs._lock_retry_interval_ms.get(), 50)
        for value in (1, 10, 86400000):
            self.file.write_text(text.replace(
                "lock_retry_interval_ms = 50", f"lock_retry_interval_ms = {value}"))
            self.assertEqual(load(self.file).limits.lock_retry_interval_ms, value)

    def test_zero_preserves_exclusion_and_nonblocking_does_not_wait(self):
        with self.configured(0).lock_policy(), fs.lock(self.root / "zero.lock"):
            with self.assertRaises(Busy):
                with fs.lock(self.root / "zero.lock"):
                    self.fail("zero must not bypass locking")
        guard = Mock()
        guard.acquire.return_value = False
        with self.configured(86400).lock_policy(), \
                patch.object(fs, "_LOCAL_LOCKS", {}), \
                patch.object(fs.threading, "Lock", return_value=guard), \
                patch.object(platform, "lock_fd") as kernel:
            with self.assertRaises(Busy):
                with fs.lock(self.root / "immediate.lock", blocking=False):
                    pass
            guard.acquire.assert_called_once_with(False)
            kernel.assert_not_called()

    def test_scopes_are_nested_thread_local_and_loading_has_no_side_effects(self):
        first, second = self.configured(7, 10), self.configured(17, 100)
        ready = threading.Barrier(2)
        observed = []

        def peer():
            with second.lock_policy():
                ready.wait(timeout=10)
                observed.append((fs._lock_timeout.get(), fs._lock_retry_interval_ms.get()))
                ready.wait(timeout=10)

        with first.lock_policy():
            self.assertEqual(load(self.file).limits.lock_timeout_seconds, 30)
            self.assertEqual(fs._lock_timeout.get(), 7)
            self.assertEqual(fs._lock_retry_interval_ms.get(), 10)
            with self.assertRaisesRegex(ValueError, "scope"):
                with second.lock_policy():
                    self.assertEqual(fs._lock_timeout.get(), 17)
                    self.assertEqual(fs._lock_retry_interval_ms.get(), 100)
                    raise ValueError("scope")
            other = threading.Thread(target=peer)
            other.start()
            try:
                ready.wait(timeout=10)
                self.assertEqual(fs._lock_timeout.get(), 7)
                ready.wait(timeout=10)
            finally:
                other.join(timeout=10)
            self.assertFalse(other.is_alive())
            self.assertEqual(observed, [(17, 100)])
            self.assertEqual(fs._lock_timeout.get(), 7)
            self.assertEqual(fs._lock_retry_interval_ms.get(), 10)
        self.assertIsNone(fs._lock_timeout.get())
        self.assertEqual(fs._lock_retry_interval_ms.get(), 50)
        self.assertEqual(platform.LOCK_TIMEOUT, 30)
        self.assertEqual(fs._LOCAL_BLOCKING_TIMEOUT, 30)

    def test_cli_engine_and_tool_thread_bind_the_owning_config(self):
        from mizu import cli, runtime
        config = self.configured(7)

        def observed(*args, **kwargs):
            return fs._lock_timeout.get()

        with patch.object(cli, "load", return_value=config), \
                patch.object(cli, "_execute", side_effect=observed):
            self.assertEqual(cli.execute(SimpleNamespace(command="doctor", config=self.file)), 7)
        engine = runtime.Engine(config)
        with patch.object(engine, "_run", side_effect=observed):
            self.assertEqual(engine.run(self.project, "worker"), 7)
        self.config = config
        ctx = self.context()
        seen = []

        def tool(*args):
            seen.append(fs._lock_timeout.get())
            raise Busy("stop after policy binding")

        def peer():
            with self.assertRaises(Busy):
                ctx.handle("read", {})

        with patch.object(runtime.delegation_gates, "check_access", side_effect=tool):
            other = threading.Thread(target=peer)
            other.start()
            other.join(timeout=10)
        self.assertFalse(other.is_alive())
        self.assertEqual(seen, [7])
        self.assertIsNone(fs._lock_timeout.get())

    def test_daemon_rebinds_after_reload_and_restores_outer_scope(self):
        from mizu import probes, runtime
        stop = threading.Event()
        seen = []

        def service(*args):
            seen.append(fs._lock_timeout.get())
            stop.set()
            raise ConfigError("stop after reload")

        with self.configured(7).lock_policy(), \
                patch.object(runtime, "load", return_value=self.configured(17)), \
                patch.object(runtime.threading, "Event", return_value=stop), \
                patch.object(runtime.signal, "signal"), \
                patch.object(probes, "service_due", side_effect=service), \
                patch.object(probes, "next_deadline", return_value=None), \
                patch("sys.stdout", new_callable=io.StringIO):
            runtime.daemon(self.config, "sample", "worker")
            self.assertEqual(seen, [17])
            self.assertEqual(fs._lock_timeout.get(), 7)
        self.assertIsNone(fs._lock_timeout.get())


if __name__ == "__main__":
    unittest.main()
