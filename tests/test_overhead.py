"""Bounded idle I/O, exact exclusion semantics and buffer-only evidence publication."""
import fnmatch
import itertools
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from mizu import platform
from mizu.process import run
from mizu.snapshot import Snapshots


class OverheadTests(unittest.TestCase):
    def test_compiled_excludes_match_fnmatchcase_and_policy_changes(self):
        store = Snapshots(Path('.'), excludes=(), max_file=100, max_bytes=1000,
                          max_files=10, create_dirs=False)
        patterns = ('target', '*.pyc', '[ab]?', 'a[!x]', 'λ*', '*\n?', 'a/b', '')
        names = ['/'.join(parts) for parts in itertools.product(
            ('x', 'target', 'λπ', 'a\nq', 'a.pyc', 'ab', 'ax', ''), repeat=2)]
        for excludes in ((), patterns, ('private',), ('*',), patterns):
            store.excludes = excludes
            for name in names:
                expected = any(fnmatch.fnmatchcase(part, p) for part in name.split('/') for p in excludes)
                self.assertEqual(store.excluded(name), expected, (name, excludes))
        self.assertTrue(store.excluded('refs/remotes/upstream/main'))

    @unittest.skipUnless(platform.IS_POSIX, 'POSIX pipe readiness')
    def test_idle_pipe_does_not_attempt_read_until_ready_and_eof_is_ready(self):
        for interval in (100, 500):
            with self.subTest(interval=interval), platform.pipe_poll_policy(interval):
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, 'rb', buffering=0) as stream:
                    try:
                        stop = threading.Event()
                        self.assertFalse(platform.wait_pipe(stream, stop, .001))
                        os.write(write_fd, b'x')
                        self.assertTrue(platform.wait_pipe(stream, stop))
                        self.assertEqual(os.read(read_fd, 1), b'x')
                        stop.set()
                        self.assertFalse(platform.wait_pipe(stream, stop))
                    finally:
                        os.close(write_fd)
                    stop.clear()
                    self.assertTrue(platform.wait_pipe(stream, stop))
                    self.assertEqual(os.read(read_fd, 1), b'')

    @unittest.skipUnless(platform.IS_POSIX, 'POSIX pipe readiness')
    def test_pipe_poll_policy_default_override_and_explicit_timeout(self):
        stop = threading.Event()
        with patch('select.poll') as factory:
            poller = factory.return_value
            poller.poll.return_value = []
            platform.wait_pipe(object(), stop)
            poller.poll.assert_called_with(100)
            with platform.pipe_poll_policy(7):
                platform.wait_pipe(object(), stop)
                poller.poll.assert_called_with(7)
                platform.wait_pipe(object(), stop, .02)
                poller.poll.assert_called_with(20)
            stop.set()
            poller.reset_mock()
            self.assertFalse(platform.wait_pipe(object(), stop))
            poller.poll.assert_not_called()
        with patch.object(platform, 'IS_POSIX', False):
            self.assertFalse(platform.wait_pipe(object(), stop))
            stop.clear()
            self.assertTrue(platform.wait_pipe(object(), stop))

    @unittest.skipUnless(platform.IS_POSIX, 'POSIX pipe readiness')
    def test_idle_pipe_cancel_with_default_and_ceiling_policy(self):
        for interval in (100, 500):
            with self.subTest(interval=interval), platform.pipe_poll_policy(interval):
                read_fd, write_fd = os.pipe()
                stop, entered = threading.Event(), threading.Event()
                timeout = platform.current_pipe_poll_interval()
                import select
                poller = select.poll()
                def poll(milliseconds):
                    entered.set()
                    return poller.poll(milliseconds)
                with os.fdopen(read_fd, 'rb', buffering=0) as stream, \
                        patch('select.poll') as factory:
                    factory.return_value.register.side_effect = poller.register
                    factory.return_value.poll.side_effect = poll
                    def reader():
                        while not stop.is_set():
                            platform.wait_pipe(stream, stop, timeout)
                    thread = threading.Thread(target=reader)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        started = time.monotonic()
                        stop.set()
                        thread.join(.9)
                        self.assertFalse(thread.is_alive())
                        self.assertLess(time.monotonic() - started, 1)
                    finally:
                        stop.set()
                        thread.join(1)
                        os.close(write_fd)

    def test_process_readers_capture_pipe_policy(self):
        wait = platform.wait_pipe
        seen = []
        def observe(stream, stop, timeout=None):
            seen.append((timeout, platform.current_pipe_poll_interval()))
            return wait(stream, stop, timeout)
        for interval in (100, 7, 500):
            with self.subTest(interval=interval), platform.pipe_poll_policy(interval), \
                    patch('mizu.process._platform.wait_pipe', side_effect=observe):
                seen.clear()
                result = run([sys.executable, '-c',
                              'import sys; print("out"); print("err", file=sys.stderr)'],
                             timeout=3, maximum=1024)
                self.assertEqual((result.reason, result.exit_code), ('exited', 0))
                self.assertEqual((result.stdout.strip(), result.stderr.strip()), ('out', 'err'))
                self.assertTrue(seen)
                self.assertEqual(set(seen), {(interval / 1000, .1)})

    def test_idle_process_checks_authority_without_busy_polling(self):
        calls = []
        def cancel():
            calls.append(time.monotonic())
            return False
        result = run([sys.executable, '-c', 'import time; time.sleep(.3)'],
                     timeout=3, maximum=1024, cancel=cancel)
        self.assertEqual(result.reason, 'exited')
        self.assertLess(len(calls), 25)

    def test_midflight_revocation_stops_idle_process(self):
        for interval in (100, 500):
            with self.subTest(interval=interval), platform.pipe_poll_policy(interval):
                started = time.monotonic()
                result = run([sys.executable, '-c', 'import time; time.sleep(30)'],
                             timeout=5, maximum=1024, cancel=lambda: time.monotonic()-started >= .15)
                self.assertEqual(result.reason, 'cancelled')
                self.assertLess(time.monotonic()-started, 1)
