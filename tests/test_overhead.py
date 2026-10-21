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
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            try:
                stop = threading.Event()
                self.assertFalse(platform.wait_pipe(stream, stop, .01))
                os.write(write_fd, b'x')
                self.assertTrue(platform.wait_pipe(stream, stop, .01))
                self.assertEqual(os.read(read_fd, 1), b'x')
                stop.set()
                self.assertFalse(platform.wait_pipe(stream, stop, .01))
                stop.clear()
            finally:
                os.close(write_fd)
            self.assertTrue(platform.wait_pipe(stream, stop, .01))
            self.assertEqual(os.read(read_fd, 1), b'')

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
        started = time.monotonic()
        result = run([sys.executable, '-c', 'import time; time.sleep(30)'],
                     timeout=5, maximum=1024, cancel=lambda: time.monotonic()-started >= .15)
        self.assertEqual(result.reason, 'cancelled')
        self.assertLess(time.monotonic()-started, 1)
