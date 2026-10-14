#!/usr/bin/env python3
"""Verbose offline unittest discovery with value-free failure metadata.

The parent supplies a new sidecar path inside its private temporary directory.
We exclusively create it (mode 0600) and flush one JSON line per failure/error
or unexpected success. Records contain only kind, stable test ID, exception
class name, and the innermost real repository-relative frame (path/line, or
null/null). Exception values, chains, source lines and locals are never formatted.
The sidecar is local to this run, not an artifact or a retry protocol; the parent
bounds/validates it and fails generically when it cannot be read. Unittest's own
progress/error display is sanitized too; arbitrary output from tests still
streams normally. Discovery and verbosity match `unittest discover -s tests -v`.
"""
from __future__ import annotations

from functools import partial
import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


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
    def __init__(self, *args, sidecar, **kwargs):
        super().__init__(*args, **kwargs)
        self.sidecar = sidecar

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
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as sidecar:
        class Runner(unittest.TextTestRunner):
            def __init__(self, **kwargs):
                super().__init__(resultclass=partial(SafeTextTestResult, sidecar=sidecar), **kwargs)

        unittest.main(module=None,
                      argv=[sys.argv[0], 'discover', '-s', 'tests', '-v'],
                      testRunner=Runner)


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
