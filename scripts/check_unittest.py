#!/usr/bin/env python3
"""Verbose offline unittest discovery with value-free failure metadata.

The parent supplies new sidecar paths inside its private temporary directory.
We exclusively create them (mode 0600) and flush one JSON line per event.
The failure sidecar holds one line per failure/error or unexpected success.
Records contain only kind, stable test ID, exception class name, and the
innermost real repository-relative frame (path/line, or null/null).
Exception values, chains, source lines and locals are never formatted.
The timing sidecar (optional second argv) holds one start line and one stop
line per test, keyed only by stable test ID with monotonic offsets; the parent
derives durations.
The optional third argv supplies the parent monotonic origin (the clock is
system-wide on supported Python/platforms). Only relative offsets leave here.
No test output, arguments, or exception values are recorded there either.
Both sidecars are local to this run, not artifacts or retry protocols; the
parent bounds/validates them and fails generically when failures cannot be
read. Timings are evidence only and never change the gate outcome. A killed
child leaves its last start without a stop, which the parent reports as an
unfinished entry with elapsed duration at child exit/timeout. Unittest's own
progress/error display is sanitized too; arbitrary output from tests still streams normally. Discovery and verbosity
match `unittest discover -s tests -v`. The optional `--test-shard 0|1`
partitions discovery by SHA-256 stable-ID parity; omission remains unsharded.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Kept in sync with the parent parser by a contract test. Stop admitting tests
# before these limits, preserving a parseable prefix even as the suite grows.
TIMING_SIDECAR_MAX_BYTES = 1_000_000
TIMING_SIDECAR_MAX_LINES = 20000
TIMING_SIDECAR_MAX_TESTS = 20000
TIMING_MAX_SECONDS = 100000.0


def stable_test_id(test) -> str:
    # _SubTest.id()/str() include message and parameter reprs. Use only the
    # owning TestCase, including for nested subtests and skip/error display.
    while isinstance(test, unittest.case._SubTest):
        test = test.test_case
    identifier = test.id()
    if (isinstance(identifier, str) and len(identifier) <= 500
            and re.fullmatch(r'[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|<locals>))*', identifier, re.ASCII)):
        return identifier
    return 'unknown_test'


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def select_shard(suite, shard):
    """Keep discovery order and fixtures; assign each unique stable ID once."""
    if shard not in (0, 1):
        raise ValueError('invalid test shard')
    selected, seen = [], set()
    for test in test_cases(suite):
        identifier = stable_test_id(test)
        if identifier == 'unknown_test' or identifier in seen:
            raise ValueError('sharding requires unique stable test IDs')
        seen.add(identifier)
        if hashlib.sha256(identifier.encode('utf-8')).digest()[-1] % 2 == shard:
            selected.append(test)
    return unittest.TestSuite(selected)


class ShardLoader(unittest.TestLoader):
    def __init__(self, shard):
        super().__init__()
        self.shard = shard

    def discover(self, *args, **kwargs):
        return select_shard(super().discover(*args, **kwargs), self.shard)


def failure_record(kind, test, err=None) -> dict:
    record = {'kind': kind, 'test': stable_test_id(test), 'type': None, 'path': None, 'line': None}
    if err is None:
        return record
    exception_type, _, tb = err
    name = exception_type.__name__
    record['type'] = name if re.fullmatch(r'[A-Za-z_]\w{0,199}', name, re.ASCII) else 'Exception'
    # Walk ONLY the supplied outer traceback, never __cause__/__context__.
    # Substring matching would mistake an unrelated host's tests/ for ours.
    while tb is not None:
        try:
            path = Path(tb.tb_frame.f_code.co_filename).resolve(strict=True)
            relative = path.relative_to(ROOT).as_posix()
            if path.is_file() and tb.tb_lineno > 0:
                record['path'], record['line'] = relative, tb.tb_lineno
        except (OSError, ValueError, RuntimeError):
            pass
        tb = tb.tb_next
    return record


class SafeTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, sidecar, timing=None, timing_origin=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sidecar = sidecar
        self.timing = timing
        self._timing_base = (timing_origin if timing_origin is not None else
                             time.monotonic() if timing is not None else 0.0)
        self._timing_bytes = self._timing_lines = self._timing_tests = 0

    def getDescription(self, test):
        # Neither str(test) nor shortDescription(): both may contain values.
        return stable_test_id(test)

    def _exc_info_to_string(self, err, test):
        record = failure_record('ERROR', test, err)
        location = f"{record['path']}:{record['line']} " if record['path'] else ''
        return f"{location}[{record['type']}]"

    def _record(self, kind, test, err=None):
        self.sidecar.write(json.dumps(failure_record(kind, test, err)) + '\n')
        self.sidecar.flush()

    def _timing_write(self, record: dict):
        if self.timing is None:
            return
        try:
            line = json.dumps(record) + '\n'
            size = len(line.encode('utf-8'))
            if (self._timing_bytes + size > TIMING_SIDECAR_MAX_BYTES
                    or self._timing_lines >= TIMING_SIDECAR_MAX_LINES):
                self.timing = None
                return
            self.timing.write(line)
            self.timing.flush()
            self._timing_bytes += size
            self._timing_lines += 1
        except (OSError, ValueError):
            self.timing = None

    def startTest(self, test):
        super().startTest(test)
        if self.timing is not None:
            try:
                now = time.monotonic()
                moment = round(now - self._timing_base, 3)
                record = {'test': stable_test_id(test), 'event': 'start', 't': moment}
                # Reserve the entire start/stop pair before writing either.
                # This also bounds bytes on disk, not just parent memory.
                stop = dict(record, event='stop', t=TIMING_MAX_SECONDS)
                pair_bytes = sum(len((json.dumps(r) + '\n').encode('utf-8'))
                                 for r in (record, stop))
                if (not 0 <= moment <= TIMING_MAX_SECONDS
                        or self._timing_tests >= TIMING_SIDECAR_MAX_TESTS
                        or self._timing_lines + 2 > TIMING_SIDECAR_MAX_LINES
                        or self._timing_bytes + pair_bytes > TIMING_SIDECAR_MAX_BYTES):
                    self.timing = None
                    return
                self._timing_tests += 1
                self._timing_write(record)
            except (OSError, ValueError):
                pass

    def stopTest(self, test):
        try:
            if self.timing is not None:
                now = time.monotonic()
                self._timing_write({'test': stable_test_id(test),
                                    'event': 'stop',
                                    't': round(now - self._timing_base, 3)})
        except (OSError, ValueError):
            self.timing = None
        super().stopTest(test)

    def addError(self, test, err):
        self._record('ERROR', test, err)
        super().addError(test, err)

    def addFailure(self, test, err):
        self._record('FAIL', test, err)
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            kind = 'FAIL' if issubclass(err[0], test.failureException) else 'ERROR'
            self._record(kind, subtest, err)
        super().addSubTest(test, subtest, err)

    def addUnexpectedSuccess(self, test):
        self._record('UNEXPECTED SUCCESS', test)
        super().addUnexpectedSuccess(test)

    def addSkip(self, test, reason):
        # Skip reasons can also be derived from exception values/host paths.
        super().addSkip(test, 'reason omitted')


def main():
    # A script normally starts with scripts/ on sys.path; -m unittest starts
    # with the working directory. Preserve root-level test-helper imports.
    sys.path[0] = os.getcwd()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sidecar')
    parser.add_argument('timing_path', nargs='?')
    parser.add_argument('timing_origin', nargs='?', type=float)
    parser.add_argument('--fail-fast', action='store_true')
    parser.add_argument('--test-shard', type=int, choices=(0, 1))
    args = parser.parse_args()
    fd = os.open(args.sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    timing_path, timing_origin = args.timing_path, args.timing_origin
    with os.fdopen(fd, 'w', encoding='utf-8') as sidecar, ExitStack() as stack:
        timing = None
        if timing_path is not None:
            try:
                timing_fd = os.open(timing_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                # Literal LF keeps the byte accounting identical on Windows.
                timing = os.fdopen(timing_fd, 'w', encoding='utf-8', newline='\n')
                def close_timing():
                    try:
                        timing.close()
                    except (OSError, ValueError):
                        pass
                stack.callback(close_timing)
            except OSError:
                pass

        class Runner(unittest.TextTestRunner):
            def __init__(self, **kwargs):
                super().__init__(resultclass=partial(
                    SafeTextTestResult, sidecar=sidecar, timing=timing,
                    timing_origin=timing_origin), **kwargs)

        unittest.main(module=None,
                      argv=[sys.argv[0], 'discover', '-s', 'tests', '-v'] + (['-f'] if args.fail_fast else []),
                      testRunner=Runner,
                      testLoader=(unittest.defaultTestLoader if args.test_shard is None
                                  else ShardLoader(args.test_shard)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Startup/runner errors have no structured metadata. Do not print an
        # unsanitized traceback; the parent reports missing details generically.
        print('unittest runner failed', file=sys.stderr, flush=True)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('unittest runner interrupted', file=sys.stderr, flush=True)
        raise SystemExit(130)
