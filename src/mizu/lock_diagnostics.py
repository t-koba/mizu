"""Opt-in, run-local record-lock telemetry; never an admission/retry input."""
from __future__ import annotations

import contextlib
import contextvars
import json
import sys
import threading
import time
from collections import deque

# Mechanism-format limits: at most 32 fixed-shape rows per journal line;
# integers saturate at signed 32-bit maximum to bound serialized field width.
MAX_LOCK_DIAGNOSTIC_ROWS = 32
MAX_LOCK_DIAGNOSTIC_VALUE = 2147483647
_LOCKS = {"insights.lock": "project-record", "smoke-retention.lock": "shared-smoke",
          "smoke-retention-apply.lock": "retention-apply"}
_PHASES = {"pre-admission-startup", "editor-ingestion", "pre-admission-dispatch",
           "admission", "post-admission"}
_current = contextvars.ContextVar("lock_diagnostics", default=None)
_OPERATIONS = {"record", "read", "list", "page", "write"}
_operation = contextvars.ContextVar("lock_diagnostic_operation", default="record")


class _Summary:
    def __init__(self):
        self.rows = deque(maxlen=MAX_LOCK_DIAGNOSTIC_ROWS)
        self.omitted = 0
        self.phase = "pre-admission-startup"
        self.mutex = threading.Lock()


@contextlib.contextmanager
def collect(enabled):
    summary = _Summary() if enabled else None
    token = _current.set(summary)
    operation_token = _operation.set("record")
    try:
        yield
    finally:
        _operation.reset(operation_token)
        _current.reset(token)
        # Only fixed labels and bounded numeric fields enter this line. No
        # exceptions, notes, paths or project/record identities are serialized.
        if summary is not None:
            with summary.mutex:
                payload = {"version": 1, "observations": list(summary.rows), "omitted": summary.omitted}
            with contextlib.suppress(OSError, ValueError):
                print(json.dumps({"lock_diagnostics": payload}, separators=(",", ":")), file=sys.stderr)


def phase(value):
    if value not in _PHASES:
        raise ValueError("Unknown lock diagnostic phase")
    summary = _current.get()
    if summary is not None:
        summary.phase = value


def capture():
    """Carry only this run's collector into its Context tool/bridge threads."""
    return _current.get()


@contextlib.contextmanager
def bind(summary):
    token = _current.set(summary)
    try:
        yield
    finally:
        _current.reset(token)


@contextlib.contextmanager
def operation(value):
    """Bind one fixed operation class to locks acquired in this scope."""
    if _current.get() is None:
        yield
        return
    if value not in _OPERATIONS:
        value = "record"
    token = _operation.set(value)
    try:
        yield
    finally:
        _operation.reset(token)


def begin(name):
    summary = _current.get()
    if summary is None or name not in _LOCKS:
        return None
    return _Observation(summary, _LOCKS[name])


class _Observation:
    def __init__(self, summary, name):
        self.summary = summary
        self.row = {"lock": name, "phase": summary.phase, "operation": _operation.get(),
                    "stage": "open", "outcome": "success",
                    "local_ms": None, "kernel_ms": None, "hold_ms": None}
        self.started = time.perf_counter_ns()

    def advance(self, stage):
        self._elapsed()
        self.row["stage"] = stage
        self.started = time.perf_counter_ns()

    def _elapsed(self):
        if self.started is None:
            return
        field = {"local-acquire": "local_ms", "kernel-acquire": "kernel_ms",
                 "body": "hold_ms"}.get(self.row["stage"])
        if field:
            self.row[field] = min(MAX_LOCK_DIAGNOSTIC_VALUE,
                                 max(0, (time.perf_counter_ns() - self.started) // 1000000))

    def stop(self):
        self._elapsed()
        self.started = None

    def finish(self):
        self.stop()
        with self.summary.mutex:
            if len(self.summary.rows) == MAX_LOCK_DIAGNOSTIC_ROWS:
                self.summary.omitted = min(MAX_LOCK_DIAGNOSTIC_VALUE, self.summary.omitted + 1)
            self.summary.rows.append(self.row)
