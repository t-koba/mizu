#!/usr/bin/env python3
"""Run network-free quality gates. Missing required tools fail; absent
operator-provisioned adapter dependencies are installed from the offline npm
cache when one is available and fail unless explicitly waived with
--allow-not-run, so the receipt never passes silently without coverage.
The gate's complete stdout is byte-bounded, including progress, the printed
receipt and its tail-visible failure summary, so it cannot grow with the
number of checks or tests it runs."""
from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu import __version__
from mizu.distribution import OFFLINE_REPORT_VERSION, source_manifest_sha
from mizu.platform import popen_kwargs, spawn, terminate_process


def node_version() -> str | None:
    """Return the exact Node runtime used by the offline gate, if available."""
    node = shutil.which('node')
    if not node:
        return None
    try:
        version = subprocess.check_output([node, '--version'], text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return version or None


def bash_available() -> bool:
    """A functional bash for shell-syntax checks.

    The WSL launcher answers to the name on Windows runners but cannot parse
    without an installed distribution; that counts as missing, matching the
    documented POSIX-only scope of the shell wrappers. The probe runs the
    exact operation the gate needs (`-n` over a trivial script) instead of
    trusting --version output or exit codes, which launchers can fake.
    """
    bash = shutil.which('bash')
    if not bash:
        return False
    try:
        probe = subprocess.run([bash, '-n'], input=b'exit 0\n', capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def sdk_gate_entry(state, details, allowed, *, name='node-durable-contract-tests') -> dict | None:
    """Map an SDK preflight state to its named gate entry (pure, no I/O).

    `ready` returns None: the caller runs the suite. `absent` and `partial`
    fail closed unless the suite was explicitly waived, in which case it is
    recorded not_run with the waiver noted: both are environment states, not
    contract violations. `mismatch` always fails: wrong versions are a
    contract violation, not missing provisioning.
    """
    if state == 'ready':
        return None
    if state in ('absent', 'partial') and name in allowed:
        return {'name': name, 'status': 'not_run',
                'details': details + ' (explicitly waived via --allow-not-run)'}
    return {'name': name, 'status': 'fail', 'details': details}


def _allow_list(value: str) -> frozenset:
    """Parse an --allow-not-run value into waived check names."""
    return frozenset(part.strip() for part in value.split(',') if part.strip())


DEFAULT_CHECK_TIMEOUT_SECONDS = 600
PROCESS_CLEANUP_TIMEOUT_SECONDS = 30
#: Seconds budgeted for the environment setup that precedes the suite child
#: (interpreter start, preflight probes, dependency resolution).
SETUP_RESERVE_SECONDS = 60
#: Seconds budgeted for the process-group cleanup that follows a bounded
#: child: two bounded waits plus the termination grace and the reap.
CLEANUP_RESERVE_SECONDS = 2 * PROCESS_CLEANUP_TIMEOUT_SECONDS + 6
#: Seconds budgeted for the receipt write and the trailing summary.
RECEIPT_RESERVE_SECONDS = 120
#: The SDK contract suites the later phase runs when their pinned
#: dependencies are ready, and the adapters each suite needs. Every suite is
#: one child of the later phase, and a waived suite runs none.
SDK_GATE_ADAPTERS = {
    'node-durable-contract-tests': ('pi-durable',),
    'node-pi-sdk-contract-tests': ('pi',),
    'node-pi-reasoning-replay-tests': ('pi', 'pi-durable'),
}
#: The SDK contract suite names, in the order the gate runs them.
SDK_GATE_NAMES = tuple(SDK_GATE_ADAPTERS)
#: The wall-clock budget the gate may spend on the checks that follow the
#: Python suite. The later phase runs one child per check: the transport
#: and SDK contract suites, one syntax check per JavaScript file, one per
#: shell wrapper -- sixteen children in a POSIX matrix cell, nine on
#: Windows, nineteen in the durable job. A budget per child would make the
#: gate's own path grow with the number of checked files, so the phase is
#: bounded as a whole and every enclosing scope holds one phase budget.
#: The budget covers the measured healthy phase with wide margin: about 4 s
#: for the sixteen children of a POSIX matrix cell, and about 103 s for the
#: nineteen children of the durable job (its three SDK contract suites
#: measured 5.6 s, 75.5 s and 18.1 s on the development host). The budget
#: also holds two children's full bounds, so one hung child still leaves
#: every remaining child its own run.
LATER_PHASE_BUDGET_SECONDS = 1200


class LaterPhase:
    """The wall-clock budget shared by the checks that follow the suite.

    The phase is bounded as a whole: every child takes at most the phase's
    remaining time and its own per-child bound, so the number of children
    cannot multiply the gate's path. A child the phase can no longer afford
    is recorded as a failure, never skipped silently.
    """

    def __init__(self, budget: int | None = None):
        self.budget = LATER_PHASE_BUDGET_SECONDS if budget is None else int(budget)
        self.consumed = 0.0
        self.children = 0

    def remaining(self) -> float:
        """The seconds the phase may still spend on its children."""
        return self.budget - self.consumed

    def consume(self, seconds: float) -> None:
        """Charge one child's own elapsed time, its cleanup included."""
        self.consumed += max(0.0, float(seconds))

    def timeout_for(self, cap: int) -> int | None:
        """The bound for the next child, or None when the phase is spent.

        Each child's own cleanup is reserved inside the phase, so the phase
        never exceeds its budget: a child is bounded by the remaining time
        minus that reserve.

        A child the phase can no longer afford is refused rather than
        spawned with a zero-second bound: such a child would report a
        plain timeout of its own, which reads as that check's failure
        instead of the phase's exhausted budget.
        """
        remaining = self.remaining()
        affordable = int(remaining - CLEANUP_RESERVE_SECONDS)
        if affordable < 1:
            return None
        return min(int(cap), affordable)


def phase_exhausted_entry(name: str, budget: int) -> dict:
    """The receipt entry for a later-phase child the phase could not run.

    A check that never ran is a failure: the phase is spent, so the child
    reported no outcome of its own.
    """
    return {'name': name, 'status': 'fail', 'exit_code': None,
            'reason': 'later-phase-budget-exhausted',
            'details': f'the later phase has no budget left for this check '
                       f'(its {budget} s budget is spent)'}


#: The bytes the gate may relay from its steps to its own stdout. The
#: harness captures a command's stdout and stderr under one documented cap
#: (`limits.output_bytes`, 262144 bytes by default), so a gate whose output
#: grows with the number of tests it runs is killed by its own harness
#: before it can report its own outcome. One budget is shared by every step,
#: exactly as the later phase shares one time budget, so the gate's output
#: cannot grow with the size of the suite.
RELAY_BUDGET_BYTES = 49152
#: The bytes of a step's own output kept for the tail print once the relay
#: budget no longer covers it. A child's summary and failure text sit at the
#: end of its output, so the tail keeps them visible after the live relay is
#: spent. This budget is shared as well, so the gate's stdout stays bounded
#: however many steps it runs.
RELAY_TAIL_BYTES = 16384
#: The complete printed receipt view (structure and details) is bounded.
#: The full check list remains in the optional --report file.
RECEIPT_OUTPUT_BUDGET_BYTES = 16384
#: The complete tail-visible summary, including its heading, every step name,
#: status, newline and omission notice, is bounded in UTF-8 bytes.
SUMMARY_OUTPUT_BUDGET_BYTES = 8192
#: All stdout emitted before the final receipt and failure summary, including
#: per-step headings and relay notices, is held to one shared byte budget.
PROGRESS_OUTPUT_BUDGET_BYTES = 98304
#: Fixed notes are reserved separately from the variable output budgets.
FINAL_NOTICE_BUDGET_BYTES = 1024
GATE_STDOUT_BUDGET_BYTES = (PROGRESS_OUTPUT_BUDGET_BYTES
                             + RECEIPT_OUTPUT_BUDGET_BYTES + 1
                             + SUMMARY_OUTPUT_BUDGET_BYTES
                             + FINAL_NOTICE_BUDGET_BYTES)
FAILURE_SUMMARY_HEADING = 'FAILED CHECKS SUMMARY (tail-visible):'


class BoundedTextOutput:
    """A UTF-8 byte-budgeted view of progress output.

    The gate may run one check per source file, so even its headings and
    per-child relay notices must share a fixed budget. The full structured
    receipt is written separately before the bounded final view is printed.
    """

    def __init__(self, stream, budget: int = PROGRESS_OUTPUT_BUDGET_BYTES):
        self.stream = stream
        self.remaining = max(0, int(budget))
        self.truncated = False
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        encoded = text.encode('utf-8', 'replace')
        with self.lock:
            if len(encoded) > self.remaining:
                visible = encoded[:self.remaining].decode('utf-8', 'ignore')
                if visible:
                    self.stream.write(visible)
                self.remaining = 0
                self.truncated = True
            elif encoded:
                # Re-decode the encoded bytes so unusual surrogate code
                # points cannot make the underlying stdout encoder fail.
                self.stream.write(encoded.decode('utf-8', 'replace'))
                self.remaining -= len(encoded)
            elif text and self.remaining == 0:
                self.truncated = True
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


class OutputRelay:
    """The gate's own stdout budget for the step output it relays.

    The relay is bounded as a whole: every step draws from one budget, so
    the gate's output cannot grow with the number of tests a step runs. A
    step whose share is spent keeps the tail of its own output visible: a
    child's summary and failure text sit at the end of its output. The bounded
    receipt summarizes results; --report retains the complete structured view.
    """

    def __init__(self, budget: int = RELAY_BUDGET_BYTES, tail: int = RELAY_TAIL_BYTES):
        self.budget = int(budget)
        self.remaining = int(budget)
        self.tail_remaining = int(tail)

    def live(self, text: str) -> bool:
        """Relay one line while the shared budget lasts."""
        size = len(text.encode('utf-8', 'replace'))
        if self.remaining < size:
            self.remaining = 0
            return False
        self.remaining -= size
        sys.stdout.write(text)
        return True

    def tail(self, text: str) -> str:
        """Return the end of the text the shared tail budget still allows."""
        data = text.encode('utf-8', 'replace')
        if len(data) > self.tail_remaining:
            data = data[len(data) - self.tail_remaining:]
        self.tail_remaining -= len(data)
        return data.decode('utf-8', 'replace')


def _compact_json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))


def _utf8_prefix(text: str, budget: int) -> str:
    """Return a prefix no longer than budget UTF-8 bytes."""
    if budget <= 0:
        return ''
    data = text.encode('utf-8', 'replace')
    if len(data) <= budget:
        return data.decode('utf-8', 'replace')
    return data[:budget].decode('utf-8', 'ignore')


def _utf8_tail(text: str, budget: int) -> str:
    """Return a suffix no longer than budget UTF-8 bytes."""
    if budget <= 0:
        return ''
    data = text.encode('utf-8', 'replace')
    if len(data) <= budget:
        return data.decode('utf-8', 'replace')
    return data[-budget:].decode('utf-8', 'ignore')


def receipt_text(receipt: dict, budget: int = RECEIPT_OUTPUT_BUDGET_BYTES) -> tuple[str, bool]:
    """Return a byte-bounded, parseable receipt view and whether it was cut.

    Small receipts keep the established pretty-printed form. A large receipt
    gets a compact view that favors failure rows and reports omitted counts;
    the full metadata, all rows and details remain in ``--report``. The view
    uses the serialized UTF-8 byte size as its bound.
    """
    if budget < 2:
        raise ValueError('receipt budget must fit a JSON object')
    full = json.dumps(receipt, indent=2)
    if len(full.encode('utf-8')) <= budget:
        return full, False
    checks = receipt.get('checks', [])
    if not isinstance(checks, list):
        checks = []
    failed_count = sum(isinstance(entry, dict) and entry.get('status') == 'fail'
                       for entry in checks)
    fixed = ('suite', 'report_version', 'mizu_version', 'source_sha256',
             'ok', 'python', 'os')
    reduced = {key: receipt[key] for key in fixed if key in receipt}
    reduced.update({'truncated': True, 'checks_total': len(checks),
                    'failed_checks_total': failed_count,
                    'checks_omitted': len(checks),
                    'failed_checks_omitted': failed_count,
                    'details_omitted': any(isinstance(entry, dict) and entry.get('details')
                                           for entry in checks),
                    'checks': []})
    if _compact_json_bytes(reduced) > budget:
        reduced = {'ok': bool(receipt.get('ok')), 'truncated': True,
                   'checks_total': len(checks), 'failed_checks_total': failed_count,
                   'checks_omitted': len(checks), 'failed_checks_omitted': failed_count,
                   'details_omitted': bool(reduced['details_omitted']), 'checks': []}
    if _compact_json_bytes(reduced) > budget:
        raise ValueError('receipt budget too small for its omission summary')

    def row(index):
        entry = checks[index]
        return {key: entry[key] for key in
                ('name', 'status', 'exit_code', 'seconds', 'reason', 'tests')
                if key in entry}

    priority = sorted((index for index, entry in enumerate(checks)
                       if isinstance(entry, dict)),
                      key=lambda index: (checks[index].get('status') != 'fail', index))
    selected = []
    for index in priority:
        indices = sorted(selected + [index])
        rows = [row(row_index) for row_index in indices]
        shown_failures = sum(checks[row_index].get('status') == 'fail'
                             for row_index in indices)
        candidate = dict(reduced, checks=rows,
                         checks_omitted=len(checks) - len(indices),
                         failed_checks_omitted=failed_count - shown_failures)
        if _compact_json_bytes(candidate) <= budget:
            selected.append(index)
    selected.sort()
    reduced['checks'] = [row(index) for index in selected]
    reduced['checks_omitted'] = len(checks) - len(selected)
    reduced['failed_checks_omitted'] = failed_count - sum(
        checks[index].get('status') == 'fail' for index in selected)
    text = json.dumps(reduced, ensure_ascii=True, separators=(',', ':'))
    if len(text.encode('utf-8')) > budget:
        raise ValueError('bounded receipt exceeded its byte budget')
    return text, True


def failure_summary_lines(checks: list, budget: int = SUMMARY_OUTPUT_BUDGET_BYTES) -> list[str]:
    """Return the tail-visible summary within a total UTF-8 byte budget.

    ``budget`` includes the printed heading, line terminators, all names and
    exit codes, detail text, and the omission marker. The newest failures are
    selected first so a truncated log retains the most recent diagnosis.
    """
    failing = [entry for entry in checks
               if isinstance(entry, dict) and entry.get('status') == 'fail']
    heading_bytes = len((FAILURE_SUMMARY_HEADING + '\n').encode('utf-8'))
    if budget < heading_bytes:
        raise ValueError('summary budget too small for its heading')
    marker_reserve = min(128, max(0, budget - heading_bytes))
    detail_reserve = min(4096, max(0, budget // 2))
    remaining = budget - heading_bytes - marker_reserve - detail_reserve
    selected = []
    for entry in reversed(failing):
        code = _utf8_prefix(str(entry.get('exit_code', '?')), 32) or '?'
        available = remaining
        # Keep a useful suffix of unusually long names rather than allowing
        # one path to consume the entire summary.
        original_name = str(entry.get('name', 'unknown check'))
        name_budget = min(256, max(0, available - 16))
        name = _utf8_prefix(original_name, name_budget)
        if len(original_name.encode('utf-8', 'replace')) > len(name.encode('utf-8')) and name_budget >= 3:
            name = _utf8_prefix(name, name_budget - 3) + '…'
        prefix = f'- {name}: exit={code}'
        prefix_bytes = len(prefix.encode('utf-8'))
        if prefix_bytes + 1 > available:
            break
        selected.append([entry, prefix, ''])
        remaining -= prefix_bytes + 1
    omitted = len(failing) - len(selected)
    # Allocate free text only after every selected check's name is secured.
    for selected_index, (entry, prefix, _detail_text) in enumerate(selected):
        detail = str(entry.get('details', '')).strip().replace('\n', ' | ')
        if not detail:
            continue
        # The marker reserve is available only when no check was omitted.
        marker_reserve_now = marker_reserve if omitted else 0
        already = sum(len((p + (' ' + d if d else '') + '\n').encode('utf-8'))
                      for _, p, d in selected)
        free = budget - heading_bytes - marker_reserve_now - already
        if free <= 1:
            continue
        allowance = min(1500, free - 1)
        detail_bytes = len(detail.encode('utf-8', 'replace'))
        if detail_bytes > allowance and allowance >= 3:
            tail = '…' + _utf8_tail(detail, allowance - 3)
        else:
            tail = _utf8_tail(detail, allowance)
        if tail:
            selected[selected_index][2] = tail
    lines = [prefix + ((' ' + detail) if detail else '')
             for _entry, prefix, detail in reversed(selected)]
    if omitted:
        marker = f'- {omitted} earlier failing checks omitted; use --report for the full receipt.'
        while lines and (sum(len((line + '\n').encode('utf-8')) for line in lines)
                         + len((marker + '\n').encode('utf-8'))
                         + heading_bytes > budget):
            lines.pop(0)
            omitted += 1
            marker = f'- {omitted} earlier failing checks omitted; use --report for the full receipt.'
        lines.insert(0, marker)
    return lines


def javascript_sources(root: Path | None = None) -> list:
    """The JavaScript files the gate syntax-checks, in the order it runs them."""
    root = ROOT if root is None else root
    return sorted([*(root / 'adapters/pi').glob('*.mjs'),
                   *(root / 'adapters/pi-durable').glob('*.mjs'),
                   *(root / 'scripts').glob('*.mjs')])


def shell_sources(root: Path | None = None) -> list:
    """The shell wrappers the gate syntax-checks, in the order it runs them."""
    root = ROOT if root is None else root
    return sorted((root / 'scripts').glob('*.sh'))


def later_phase_child_count(*, platform: str | None = None, sdk_suites: int = 0,
                             root: Path | None = None) -> int:
    """The number of children the later phase runs in one configuration.

    The later phase runs the transport suite, every SDK contract suite that
    is not waived (`sdk_suites` of them: the matrix cells waive all three
    and the durable job runs all of them), one syntax check per JavaScript
    file, and one per shell wrapper on a POSIX host with a functional bash.
    This count is what the phase budget has to cover: a budget per child
    would make the gate's own path grow with the number of checked files,
    and every enclosing scope with it.
    """
    platform = os.name if platform is None else platform
    root = ROOT if root is None else root
    count = 1 + sdk_suites + len(javascript_sources(root))
    if platform == 'posix':
        count += len(shell_sources(root))
    return count


def modelled_gate_seconds(child_timeout: int) -> int:
    """Return the seconds a bounded gate run needs from setup to receipt.

    Every outer scope that encloses the gate (a CI job, the installer
    staging call) must hold this path. A scope shorter than the path cuts
    off a healthy run before the gate can report its own bounded outcome,
    so the enclosing scope must never be the tighter bound.

    The later phase is bounded as a whole (LATER_PHASE_BUDGET_SECONDS), not
    per child: the phase runs one child per check, so a per-child term would
    make this path grow with the number of checked files.
    """
    return (SETUP_RESERVE_SECONDS + child_timeout + LATER_PHASE_BUDGET_SECONDS
            + CLEANUP_RESERVE_SECONDS + RECEIPT_RESERVE_SECONDS)


def core_cell_scope_seconds(child_timeout: int, staging_timeout: int) -> int:
    """Return the seconds the core CI cell needs from its first step to its last.

    The cell that installs runs the gate once: the installer reuses the
    source gate's receipt, and the idempotence reinstall finds the release
    the cell already staged, so neither install runs a second gate. The
    cell's scope still holds the source gate's path plus the installer's own
    bounded staging call, so the job never cuts off a healthy run before the
    gate reports its own bounded outcome.
    """
    return modelled_gate_seconds(child_timeout) + staging_timeout


#: The wall-clock bound the gate holds the Python suite child to. The suite
#: costs about 923 s on the development host, about 1050 s on a loaded
#: verification host (the 2026-10-21 run was still running when the previous
#: 1000 s bound cut it off) and about 1475 s on a slow Windows runner. One
#: bound covers the measured cost of the slowest supported host with margin,
#: so the modelled path and every enclosing scope are the same everywhere.
SUITE_CHILD_TIMEOUT_SECONDS = 1800


def _unittest_timeout_seconds() -> int:
    """Bound the suite child so the gate finishes inside its job scope.

    The platform does not change the bound: the suite's cost is a fact about
    the host, and the slowest supported host sets one bound for all of them.
    The job scope holds the full modelled path.
    """
    return SUITE_CHILD_TIMEOUT_SECONDS


def unittest_failure_summary(sidecar: Path) -> str:
    """Read only the child's sanitized JSONL, never flattened traceback text.

    The private, run-local sidecar holds kind/test/type/path/line records (see
    check_unittest.py). Reject missing, empty, malformed, oversized or unsafe
    metadata generically. Summary bounds remain 500 characters per failure and
    1500 total; test counts still come from unittest's standard final output.
    """
    import re
    unavailable = 'unittest failure details unavailable'
    try:
        with sidecar.open('rb') as stream:
            data = stream.read(1_000_001)
        if not data or len(data) > 1_000_000:
            return unavailable
        summaries = []
        for line in data.decode('utf-8').splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != {'kind', 'test', 'type', 'path', 'line'}:
                return unavailable
            kind, test, error_type = record['kind'], record['test'], record['type']
            if (kind not in ('FAIL', 'ERROR', 'UNEXPECTED SUCCESS')
                    or not isinstance(test, str) or len(test) > 500
                    or not re.fullmatch(r'[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|<locals>))*', test, re.ASCII)):
                return unavailable
            if error_type is not None and (not isinstance(error_type, str)
                    or not re.fullmatch(r'[A-Za-z_]\w{0,199}', error_type, re.ASCII)):
                return unavailable
            path, number = record['path'], record['line']
            if kind in ('FAIL', 'ERROR') and error_type is None:
                return unavailable
            if kind == 'UNEXPECTED SUCCESS' and (error_type is not None or path is not None or number is not None):
                return unavailable
            location = ''
            if path is not None:
                if (not isinstance(path, str) or not path or '\\' in path
                        or any(part in ('', '.', '..') for part in path.split('/'))
                        or Path(path).is_absolute() or type(number) is not int or number <= 0):
                    return unavailable
                resolved = (ROOT / path).resolve(strict=True)
                resolved.relative_to(ROOT)
                if not resolved.is_file() or resolved.relative_to(ROOT).as_posix() != path:
                    return unavailable
                location = f' @ {path}:{number}'
            elif number is not None:
                return unavailable
            exception = f' [{error_type}]' if error_type else ''
            summaries.append(f'{kind} {test}{location}{exception}'[:500])
        return '; '.join(summaries)[:1500] or unavailable
    except (OSError, ValueError, TypeError, RuntimeError):
        return unavailable


TIMING_SIDECAR_MAX_BYTES = 1_000_000
TIMING_SIDECAR_MAX_LINES = 20000
TIMING_SIDECAR_MAX_TESTS = 20000
TIMING_MAX_SECONDS = 100000.0


def _valid_timing_test(value) -> bool:
    import re
    return (isinstance(value, str) and 0 < len(value) <= 500
            and re.fullmatch(r'[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*|<locals>))*', value, re.ASCII) is not None)


def _valid_timing_seconds(value) -> bool:
    return (type(value) in (int, float)
            and 0.0 <= value <= TIMING_MAX_SECONDS)


def parse_unittest_timings(sidecar: Path, *, elapsed_seconds: float) -> list:
    """Read only the child's timing JSONL into sanitized per-test evidence.

    The private, run-local timing sidecar holds start/stop records keyed only
    by stable test ID with monotonic offsets (see
    check_unittest.py). No test output, arguments, or exception values are
    ever recorded. Parsing is bounded (1,000,000 bytes, 20000 lines/tests) and
    validated: malformed lines are skipped, missing/empty/oversized inputs
    yield an empty list. A start without a matching stop yields an unfinished
    entry with elapsed duration at child exit/timeout and completed False.
    Times are offsets from the parent-supplied system-wide monotonic origin;
    no absolute timestamps are exported. Invalid elapsed durations are omitted.
    This never governs the gate outcome; failures remain decided by the failure
    sidecar and exit status alone.
    """
    try:
        with sidecar.open('rb') as stream:
            data = stream.read(TIMING_SIDECAR_MAX_BYTES + 1)
        if not data or len(data) > TIMING_SIDECAR_MAX_BYTES:
            return []
        lines = data.decode('utf-8').splitlines()
        if len(lines) > TIMING_SIDECAR_MAX_LINES:
            return []
        ordered: list = []
        index_by_test: dict = {}
        start_times: dict = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event = record.get('event')
            if event == 'start':
                if set(record) != {'test', 'event', 't'}:
                    continue
                test, moment = record['test'], record['t']
                if not _valid_timing_test(test) or not _valid_timing_seconds(moment):
                    continue
                if test in index_by_test:
                    continue
                if len(ordered) >= TIMING_SIDECAR_MAX_TESTS:
                    continue
                index_by_test[test] = len(ordered)
                start_times[test] = float(moment)
                ordered.append({'test': test, 'duration_seconds': None, 'completed': False})
            elif event == 'stop':
                if set(record) != {'test', 'event', 't'}:
                    continue
                test, moment = record['test'], record['t']
                if not _valid_timing_test(test) or not _valid_timing_seconds(moment):
                    continue
                if test not in start_times:
                    continue
                delta = float(moment) - start_times[test]
                if not _valid_timing_seconds(delta):
                    continue
                entry = ordered[index_by_test[test]]
                entry['duration_seconds'] = round(delta, 3)
                entry['completed'] = True
                start_times.pop(test)
            else:
                continue
        for entry in ordered:
            if not entry['completed']:
                delta = elapsed_seconds - start_times[entry['test']]
                if _valid_timing_seconds(delta):
                    entry['duration_seconds'] = round(delta, 3)
        return [entry for entry in ordered if entry['duration_seconds'] is not None]
    except (OSError, ValueError, TypeError, RuntimeError):
        return []


def _timing_artifact_summary(tests: list) -> dict:
    """Return a bounded, sanitized view that keeps hotspots readable in CI."""
    valid = []
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        test = entry.get('test')
        duration = entry.get('duration_seconds')
        completed = entry.get('completed')
        if (_valid_timing_test(test) and _valid_timing_seconds(duration)
                and isinstance(completed, bool)):
            valid.append({'test': test, 'duration_seconds': round(duration, 3),
                          'completed': completed})
    completed_durations = sorted(item['duration_seconds'] for item in valid
                                 if item['completed'])

    def percentile(numerator: int, denominator: int):
        if not completed_durations:
            return None
        rank = (len(completed_durations) * numerator + denominator - 1) // denominator
        return completed_durations[max(0, rank - 1)]

    return {'tests_recorded': len(valid),
            'tests_completed': len(completed_durations),
            'completed_duration_seconds': round(sum(completed_durations), 3),
            'completed_duration_percentiles_seconds': {
                'p50': percentile(50, 100), 'p90': percentile(90, 100),
                'p95': percentile(95, 100), 'p99': percentile(99, 100)},
            'slowest_tests': sorted(valid,
                                    key=lambda item: (-item['duration_seconds'], item['test']))[:20]}


TIMING_PHASE_MAX_ENTRIES = 200
TIMING_PHASE_MAX_NAME_LEN = 500
TIMING_COUNTS_MAX = 1000000


def _valid_timing_phase_name(value) -> bool:
    import re
    return (isinstance(value, str) and 0 < len(value) <= TIMING_PHASE_MAX_NAME_LEN
            and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.\-/:]*', value, re.ASCII) is not None)


def _valid_timing_counts(value):
    if not isinstance(value, dict):
        return None
    allowed = ('run', 'failed', 'skipped')
    sanitized = {}
    for key in allowed:
        if key in value:
            item = value[key]
            if type(item) is not int or not 0 <= item <= TIMING_COUNTS_MAX:
                return None
            sanitized[key] = item
    for key in value:
        if key not in allowed:
            return None
    if not sanitized:
        return None
    return sanitized


def sanitize_timing_phases(checks) -> list:
    """Return bounded, sanitized per-check/phase timing entries.

    Each entry holds only a stable phase name, status, duration, and safe
    test counts. Details, exit codes, output tails, and failure diagnostics
    are never included. Malformed entries are skipped; over-limit entries
    are truncated. This never governs the gate outcome.
    """
    try:
        phases = []
        for entry in list(checks)[:TIMING_PHASE_MAX_ENTRIES * 2]:
            if len(phases) >= TIMING_PHASE_MAX_ENTRIES:
                break
            if not isinstance(entry, dict):
                continue
            name = entry.get('name')
            status = entry.get('status')
            if not _valid_timing_phase_name(name):
                continue
            if status not in ('pass', 'fail', 'not_run'):
                continue
            phase = {'name': name, 'status': status}
            seconds = entry.get('seconds', None)
            if seconds is not None:
                if _valid_timing_seconds(seconds):
                    phase['duration_seconds'] = round(float(seconds), 3)
                else:
                    continue
            counts = _valid_timing_counts(entry.get('tests')) if 'tests' in entry else None
            if 'tests' in entry and counts is None:
                continue
            if counts is not None:
                phase['tests'] = counts
            phases.append(phase)
        return phases
    except (TypeError, ValueError, RuntimeError):
        return []


def write_timings_artifact(path: Path, *, tests: list, status: str, exit_code: int,
                           elapsed_seconds: float, phases=()) -> None:
    """Write the dedicated timing artifact with only sanitized evidence.

    Best-effort: never raises; timing evidence must not change the gate
    outcome. Holds only stable test IDs, durations/completion flags, a bounded
    hotspot/percentile summary, bounded sanitized per-check/phase entries
    (stable name, status, duration, safe test counts only), and minimal
    runtime metadata (versions, platform, timeout, total elapsed, gate
    status). It never includes failure details, output tails, or diagnostics.
    """
    try:
        if not _valid_timing_seconds(elapsed_seconds):
            elapsed_seconds = 0.0
        payload = {'suite': 'test-timings',
                   'mizu_version': __version__,
                   'python': sys.version.split()[0], 'os': sys.platform,
                   'unittest_timeout_seconds': _unittest_timeout_seconds(),
                   'unittest_elapsed_seconds': round(float(elapsed_seconds), 3),
                   'unittest_status': status if status in ('pass', 'fail', 'not_run') else 'fail',
                   'unittest_exit_code': exit_code if type(exit_code) is int else 1,
                   'summary': _timing_artifact_summary(tests),
                   'tests': tests,
                   'phases': sanitize_timing_phases(phases)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, TypeError, RuntimeError):
        pass


#: The input the offline install consumes: npm's own cache, as selected by
#: `npm_config_cache` (npm's default is `~/.npm`). The install never reaches
#: the network, so a cache that does not hold the pinned tarballs fails it.
NPM_CACHE_INPUT = "npm_config_cache (npm's cache directory, ~/.npm by default)"


def pi_sdk_deps_status(adapter: Path) -> tuple:
    """Check pinned adapter dependencies are installed at pinned versions.

    SDK contract suites import operator-verified Pi dependencies, provisioned
    outside the offline test runner and never vendored into git.
    Returns ("ready", "") when every pinned dependency resolves at its pinned
    version, ("absent", reason) when the adapter holds no installed
    dependencies at all, ("partial", reason) when it holds some of the pinned
    dependencies but not all, and ("mismatch", reason) when the manifest is
    unreadable or an installed version differs from the pin.

    Only ("absent") is installable. The clean install replaces the whole
    dependency tree, so it never runs over a tree that holds anything the
    operator provisioned: a partial tree fails closed and names the packages
    that are missing, and a version mismatch fails like any contract
    violation. Absent inputs are environment provisioning and fail closed
    unless explicitly waived via --allow-not-run.
    """
    try:
        manifest = json.loads((adapter / "package.json").read_text(encoding="utf-8"))
        pinned = manifest["dependencies"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ("mismatch", f"pinned dependency manifest unreadable: {exc}")
    if not isinstance(pinned, dict) or not pinned:
        return ("mismatch", "pinned dependency manifest holds no dependencies")
    modules = adapter / "node_modules"
    missing = []
    mismatched = []
    present = 0
    for name, version in pinned.items():
        try:
            found = json.loads((modules / name / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            found = None
        if found is None:
            missing.append(f"{name}@{version}")
            continue
        present += 1
        if not isinstance(version, str) or found != version:
            mismatched.append(f"{name} installed at {found}, pinned at {version}")
    if mismatched:
        return ("mismatch", "; ".join(mismatched))
    if not missing:
        return ("ready", "")
    if present:
        return ("partial", "the pinned dependencies are partially installed: "
                           + ", ".join(missing) + " are missing. The installed tree is "
                           "left alone, so provision the adapter's own tree (npm ci in "
                           "the adapter directory) before running the durable contract suite")
    return ("absent", ", ".join(missing) + " are not installed; provision them from the "
                                       "operator's offline npm cache before running the "
                                       "durable contract suite")


#: Bound for one offline dependency install. The pinned tree is a few hundred
#: megabytes of packages resolved from a local cache; the bound is generous
#: enough for a loaded host and short enough to fail inside the gate's own
#: budget.
NPM_PROVISION_TIMEOUT_SECONDS = 300

#: The install command. `npm ci` installs exactly the committed lockfile,
#: `--offline` (with `npm_config_offline`) keeps npm away from the network,
#: and `--ignore-scripts` keeps the installer's default-deny lifecycle
#: posture.
NPM_PROVISION_COMMAND = ('npm', 'ci', '--offline', '--engine-strict',
                          '--no-audit', '--no-fund', '--ignore-scripts')


def provision_pinned_dependencies(adapter: Path, *, timeout: int = NPM_PROVISION_TIMEOUT_SECONDS) -> tuple:
    """Install one adapter's pinned dependencies from the offline npm cache.

    The pinned SDK is provisioned outside git, so a frozen source copy -- the
    input of a check job -- carries no `node_modules` and the SDK gates fail
    closed on absent dependencies. The operator's offline npm cache is the
    same source the installer and CI use, so the gate installs the pinned
    tree itself before the contract suites run: npm never reaches the
    network, the committed lockfile pins every version, and dependency
    lifecycle scripts stay denied.

    The clean install replaces the whole dependency tree, so it runs only
    over an adapter that holds no installed dependencies at all. A tree that
    holds some of the pinned packages is left exactly as it is and fails
    closed, so a failed install never destroys what the operator provisioned.

    Schema: the same shape as `pi_sdk_deps_status` -- ("ready", detail),
    ("absent", reason), ("partial", reason) or ("mismatch", reason). Bounds:
    one install per adapter, bounded by `timeout`. Trust: the operator's
    local npm cache and the repository's own lockfile. Retry: none; the gate
    records the outcome. Evidence: the gate's own stdout names the
    provisioned adapter. Failure: an absent npm, a missing lockfile, a
    failed install or an elapsed bound leaves the previous verdict and the
    previous installation untouched, and names npm's own output and the cache
    it resolves from; an installed version that differs from the pin is
    never reinstalled, because wrong versions are a contract violation
    rather than missing provisioning.
    """
    state, details = pi_sdk_deps_status(adapter)
    if state != 'absent':
        # Installed dependencies are left alone: a partial tree is the
        # operator's own, and a version that differs from the pin is a
        # contract violation the gate must not paper over.
        return state, details
    if (adapter / 'node_modules').exists():
        # The clean install replaces the whole dependency tree, so it never
        # runs over a tree the operator provisioned, even one that holds none
        # of the pinned packages.
        return (state, details + '; a dependency tree is present, so the clean install '
                                 'would replace it; provision the adapter\'s own tree '
                                 '(npm ci in the adapter directory)')
    if not shutil.which('npm'):
        return (state, details + '; npm is not available to provision the pinned dependencies'
                + ' (the install resolves from ' + NPM_CACHE_INPUT + ')')
    if not (adapter / 'package-lock.json').is_file():
        return state, details + '; no package-lock.json to install the pinned dependencies from'
    env = {**os.environ, 'npm_config_offline': 'true'}
    try:
        installed = subprocess.run(list(NPM_PROVISION_COMMAND), cwd=str(adapter), env=env,
                                   capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return state, details + f'; the offline install exceeded its {timeout} s bound'
    except (OSError, ValueError) as exc:
        return state, details + f'; the offline install could not start: {exc}'
    if installed.returncode != 0:
        lines = [line.strip() for line in (installed.stderr or installed.stdout or '').splitlines()
                 if line.strip()]
        # npm's pointer to its own log file is not a reason: it is dropped
        # so the gate names the cause (a cache miss, a tar or disk error)
        # rather than the log path.
        reasons = [line for line in lines if 'A complete log' not in line]
        errors = [line for line in reasons if line.startswith('npm error')]
        if errors:
            reason = '; '.join(errors[:2])
        elif reasons:
            reason = reasons[-1]
        else:
            reason = f'exit {installed.returncode}'
        return (state, details + '; the offline install failed: ' + reason[:400]
                + ' (the install resolves from ' + NPM_CACHE_INPUT
                + ', which must hold the pinned tarballs)')
    final, final_details = pi_sdk_deps_status(adapter)
    if final == 'ready':
        return 'ready', 'provisioned from the offline npm cache'
    return final, final_details or (details + '; the offline install left the pinned dependencies absent')


def provision_targets(allowed_not_run) -> set:
    """The adapters whose pinned dependencies this run needs.

    A waived gate runs no suite, so it needs no dependencies: the gate
    installs only for a suite that will actually run.
    """
    wanted = set()
    for name, adapters in SDK_GATE_ADAPTERS.items():
        if name not in allowed_not_run:
            wanted.update(adapters)
    return wanted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fail-fast', action='store_true',
                        help='Stop the Python suite at its first failure; successful gates still run every test')
    parser.add_argument('--report', type=Path, help='Write a portable JSON validation receipt')
    parser.add_argument('--test-timings', type=Path,
                        help='Write best-effort sanitized per-test timing evidence to this path')
    parser.add_argument('--no-provision', action='store_true',
                        help='Do not install the pinned adapter dependencies from the offline npm '
                             'cache; report absent dependencies instead')
    parser.add_argument('--allow-not-run', default='',
                        help='Comma-separated check names explicitly permitted to stay not_run '
                             '(e.g. node-pi-sdk-contract-tests or '
                             'node-pi-reasoning-replay-tests where the operator has not '
                             'provisioned the pinned SDK); absent SDK dependencies fail '
                             'without an explicit waiver')
    args = parser.parse_args()
    allowed_not_run = _allow_list(args.allow_not_run)
    checked_node_version = node_version()
    try:
        source_sha = source_manifest_sha(ROOT)
    except (OSError, ValueError) as exc:
        print(f'source manifest is unreadable: {exc}', file=sys.stderr)
        return 1
    progress_stream = BoundedTextOutput(sys.stdout)
    sys.stdout = progress_stream
    checks = []
    counts = {}
    phase = LaterPhase()
    relay = OutputRelay()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONFAULTHANDLER': '1'}
    # The offline suite is process-spawn heavy. Every outer scope that
    # encloses this gate holds the modelled path (setup, the child, the
    # bounded later phase, bounded process cleanup, and the suffix/receipt
    # reserve), so a bounded run reports its own outcome instead of being cut
    # off by the enclosing scope. `modelled_gate_seconds` states that path.
    # The later phase is bounded as a whole (one budget for every check that
    # follows the Python suite), so the number of checked files cannot grow
    # the gate's path.
    # A contract test keeps the full budget relationship explicit. Step output
    # streams live (merged in order) so a hung step leaves
    # the last-running test visible, and a step timeout
    # is recorded as a structured failure with the partial output counted
    # instead of crashing the gate before the receipt is written.
    def execute(name, command, *, count=None, failure_summary=None,
                timeout=DEFAULT_CHECK_TIMEOUT_SECONDS, later_phase=False):
        if later_phase:
            allowed = phase.timeout_for(timeout)
            phase.children += 1
            if allowed is None:
                entry = phase_exhausted_entry(name, phase.budget)
                checks.append(entry)
                print(f'== {name} == (not run: the later phase has no budget left)',
                      flush=True)
                return time.monotonic()
            timeout = allowed
        started = time.monotonic()
        print(f'== {name} ==', flush=True)
        chunks: list[str] = []
        suppressed: list[str] = []
        suppressed_bytes = 0
        capped = False
        def pump(stream):
            # Relay each line immediately with its arrival time: the
            # parent flushes per line (never only at EOF) and every line
            # carries monotonic elapsed, so a killed step's log shows
            # whether progress was steady until the budget ran out or one
            # line went quiet (a genuine stall). Chunks stay unprefixed so
            # counts and receipts parse the raw child output.
            # Only the reader closes its stream: closing a buffered stream
            # from another thread can block on that thread's pending read.
            # The relay draws on one shared budget, so a step that runs
            # many tests cannot make the gate's own output grow without
            # bound: the lines the budget no longer covers are kept for
            # the step's tail instead of the gate's stdout.
            nonlocal capped, suppressed_bytes
            with stream:
                for line in stream:
                    elapsed = time.monotonic() - started
                    if relay.live(f'[+{elapsed:8.1f}s] {line}'):
                        sys.stdout.flush()
                    else:
                        if not capped:
                            capped = True
                            sys.stdout.write(f'[+{elapsed:8.1f}s] (relay budget spent; '
                                             'the rest of this step is not relayed)\n')
                            sys.stdout.flush()
                        suppressed.append(line)
                        suppressed_bytes += len(line.encode('utf-8', 'replace'))
                        while suppressed_bytes > RELAY_TAIL_BYTES and len(suppressed) > 1:
                            dropped = suppressed.pop(0)
                            suppressed_bytes -= len(dropped.encode('utf-8', 'replace'))
                    chunks.append(line)
                sys.stdout.flush()
        status, exit_code, details = 'pass', 0, ''
        timed_out = False
        process = spawn(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1, **popen_kwargs())
        reader = threading.Thread(target=pump, args=(process.stdout,), daemon=True)
        try:
            reader.start()
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                status = 'fail'
                timed_out = True
                # Unittest diagnostics come only from structured metadata,
                # even on timeout. Other steps retain their raw output tail;
                # live timestamped progress still locates an unfinished test.
                details = f'timed out after {timeout} s'
                if failure_summary is None:
                    tail = ''.join(chunks)[-1500:]
                    details += f'; output tail: {tail.strip()}'
        finally:
            ended_at = time.monotonic()
            # A successful direct child may still leave descendants holding
            # stdout. End the whole group/job before waiting for EOF, without
            # Popen.__exit__ closing a buffered stream under a blocked reader.
            terminate_process(process)
            stopped = process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
            if timed_out:
                # The child was stopped for time and never reported an exit
                # code of its own: the code the platform reports for a killed
                # child says nothing about the suite, so the receipt names
                # the outcome instead.
                exit_code = None
            else:
                exit_code = stopped
            reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        finished_at = time.monotonic()
        if capped:
            # The step's own tail carries what the relay budget no longer
            # covers: a child's summary and failure text sit at the end of
            # its output, so they stay visible after the live relay is
            # spent. The receipt keeps the structured outcome either way.
            tail_text = relay.tail(''.join(suppressed))
            if tail_text:
                sys.stdout.write(f'[+{time.monotonic() - started:8.1f}s] '
                                 "(the live relay was capped; the step's last "
                                 'bytes follow)\n')
                sys.stdout.write(tail_text)
                if not tail_text.endswith('\n'):
                    sys.stdout.write('\n')
                sys.stdout.flush()
        if later_phase:
            # The phase pays for the child's own runtime and its cleanup.
            phase.consume(finished_at - started)
        if reader.is_alive():
            status = 'fail'
            details = f'{details}; output reader did not stop'.lstrip('; ')
        text = ''.join(chunks)
        if exit_code != 0:
            status = 'fail'
        # A timed-out child remains a failure even if Windows Job Object
        # cleanup reports exit code 0; include structured unittest evidence
        # based on the recorded status, not that platform-dependent code.
        if status == 'fail' and failure_summary is not None:
            summary = failure_summary()
            details = f'{details}; {summary}' if details else summary
        if status == 'fail' and capped and not timed_out and failure_summary is None:
            # The shared relay budget hid this step's own output, so the
            # receipt carries its tail instead: a failing child keeps the
            # evidence of why it failed even when the gate's live output is
            # capped. A step whose output was relayed needs no copy here.
            tail = ''.join(chunks).strip()[-1500:]
            if tail:
                details = (f'{details}; output tail: {tail}' if details
                           else f'output tail: {tail}')
        entry = {'name': name, 'status': status,
                 'seconds': round(time.monotonic() - started, 3), 'exit_code': exit_code}
        if timed_out:
            entry['reason'] = 'timeout'
        if details:
            entry['details'] = details
        if count is not None:
            # A child that was stopped for time may have reported no
            # summary, and a suite that reported none has no counts: the
            # receipt carries no test count rather than a count of zero.
            found = count(text)
            if found is not None:
                entry['tests'] = found
                counts[name] = found
        checks.append(entry)
        return ended_at
    # AST checks avoid leaving bytecode inside an immutable source release.
    for directory in ('src', 'scripts', 'tests', 'examples', 'adapters/claude'):
        for path in (ROOT / directory).rglob('*.py'):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(ROOT)))
    checks.append({'name': 'python-syntax', 'status': 'pass'})
    def unittest_counts(output):
        import re
        ran = re.search(r'^Ran (\d+) tests?', output, re.M)
        if not ran:
            # The child reported no summary, so it has no counts.
            return None
        failed = re.search(r'^FAILED', output, re.M)
        numbers = re.findall(r'(?:failures|errors)=(\d+)', output)
        skipped = re.search(r'skipped=(\d+)', output)
        return {'run': int(ran.group(1)),
                'failed': sum(map(int, numbers)) if failed else 0,
                'skipped': int(skipped.group(1)) if skipped else 0}
    def node_counts(output):
        import re
        passed = re.search(r'^# tests (\d+)', output, re.M)
        if not passed:
            # The child reported no summary, so it has no counts.
            return None
        failed = re.search(r'^# fail (\d+)', output, re.M)
        skipped = re.search(r'^# skipped (\d+)', output, re.M)
        return {'run': int(passed.group(1)),
                'failed': int(failed.group(1)) if failed else 0,
                'skipped': int(skipped.group(1)) if skipped else 0}
    # Timestamped verbose progress: on a timeout the killed step leaves the
    # unfinished test as the last timestamped line in the live log.
    # The suite costs about 1475 s on a slow Windows runner (the 2026-10-21
    # Windows/Python 3.13 run completed 2233 of about 2265 tests in 1447 s
    # and reached the previous 1450 s child cap with no reported test
    # failure) and about 1050 s on a loaded POSIX host (the 2026-10-21 run
    # reached the previous 1000 s child cap with the suite still running), so
    # the bound is 1800 s on every platform inside a 60-minute job. The
    # opt-in per-test timing artifact retains partial progress on timeout for
    # diagnosis. Samples remain diagnostic, not bounds.
    # The child owns structured exception metadata. Its failure sidecar is private
    # and deleted even on timeout; no failure/timeout details parse raw output.
    # The timing sidecar is likewise private and run-local, flushed per test so
    # a killed child still leaves a partial unfinished entry. Timings are
    # evidence only: parsing is bounded/validated and never governs the gate.
    # The dedicated test-timings artifact (sanitized per-test IDs,
    # durations/completion, a bounded hotspot summary, and bounded sanitized
    # per-check/phase entries) is opt-in via --test-timings. Per-test timings
    # are parsed immediately so they survive an ordinary child timeout; the
    # artifact is written immediately with partial phases so a later hang/job
    # timeout still leaves per-test evidence, then rewritten after all checks
    # finish so it also carries complete phase data. It records evidence, not
    # a policy.
    pending_timings: list = []
    pending_unittest_status = 'fail'
    pending_unittest_exit = 1
    pending_unittest_elapsed = 0.0
    with tempfile.TemporaryDirectory(prefix='mizu-unittest-') as temporary:
        sidecar = Path(temporary) / 'failures.jsonl'
        command = [sys.executable, '-u', str(ROOT / 'scripts/check_unittest.py'), str(sidecar)]
        timing_sidecar = None
        timing_origin = None
        if args.test_timings:
            timing_sidecar = Path(temporary) / 'timings.jsonl'
            timing_origin = time.monotonic()
            command.extend((str(timing_sidecar), str(timing_origin)))
        if args.fail_fast:
            command.append('--fail-fast')
        ended_at = execute('python-unit-and-contract-tests', command,
                count=unittest_counts, failure_summary=lambda: unittest_failure_summary(sidecar),
                timeout=_unittest_timeout_seconds())
        if args.test_timings and timing_sidecar is not None and timing_origin is not None:
            try:
                pending_timings = parse_unittest_timings(
                    timing_sidecar, elapsed_seconds=ended_at - timing_origin)
            except (OSError, ValueError, TypeError, RuntimeError):
                pending_timings = []
            unittest_entry = next((c for c in reversed(checks)
                                   if c.get('name') == 'python-unit-and-contract-tests'),
                                  {'status': 'fail', 'exit_code': 1})
            try:
                pending_unittest_status = str(unittest_entry.get('status', 'fail'))
            except (AttributeError, ValueError):
                pending_unittest_status = 'fail'
            try:
                pending_unittest_exit = int(unittest_entry.get('exit_code', 1))
            except (TypeError, ValueError):
                pending_unittest_exit = 1
            try:
                pending_unittest_elapsed = ended_at - timing_origin
            except TypeError:
                pending_unittest_elapsed = 0.0
            # Early partial write so a later hang/job timeout still leaves
            # per-test evidence; rewritten with complete phases below.
            # Best-effort: never changes the gate outcome.
            write_timings_artifact(args.test_timings, tests=pending_timings,
                                   status=pending_unittest_status,
                                   exit_code=pending_unittest_exit,
                                   elapsed_seconds=pending_unittest_elapsed,
                                   phases=checks)
    if not shutil.which('node'):
        checks.append({'name': 'node-available', 'status': 'fail'})
    # bash is a POSIX convenience for shell-syntax checks, not a runtime
    # requirement on Windows/macOS development: non-POSIX hosts report
    # not_run. The Windows runner answers to the name `bash` with a WSL
    # launcher that cannot parse, and probing cannot tell it apart from a
    # real shell reliably; shell wrappers are POSIX-only by scope.
    has_bash = os.name == 'posix' and bash_available()
    if not has_bash:
        checks.append({'name': 'shell-syntax', 'status': 'not_run',
                       'details': 'no functional bash; shell wrappers are POSIX-only'})
    if shutil.which('node'):
        execute('node-transport-and-extension-tests', ['node', '--test', 'tests/bridge.test.mjs'],
                count=node_counts, later_phase=True)
        adapters = {'pi': ROOT / 'adapters/pi', 'pi-durable': ROOT / 'adapters/pi-durable'}
        verdicts = {name: pi_sdk_deps_status(path) for name, path in adapters.items()}
        # The pinned SDK is provisioned outside git, so a frozen source copy
        # carries none of it. Install it from the operator's offline npm cache
        # before the suites that need it, and only for a suite that will
        # actually run: a waived gate needs no dependencies.
        if not args.no_provision:
            for name in sorted(provision_targets(allowed_not_run)):
                state, details = provision_pinned_dependencies(adapters[name])
                if state == 'ready' and verdicts[name][0] != 'ready':
                    print('== provisioned ' + str(adapters[name])
                          + ' pinned dependencies from the offline npm cache ==')
                verdicts[name] = (state, details)
        durable_name, pi_sdk_name, replay_name = SDK_GATE_NAMES
        durable_state, durable_details = verdicts['pi-durable']
        if durable_state == 'ready':
            execute(durable_name, ['node', '--test', 'tests/durable.test.mjs'],
                    count=node_counts, later_phase=True)
        else:
            checks.append(sdk_gate_entry(durable_state, durable_details, allowed_not_run,
                                         name=durable_name))
        pi_sdk_state, pi_sdk_details = verdicts['pi']
        if pi_sdk_state == 'ready':
            execute(pi_sdk_name, ['node', 'scripts/check-pi-sdk.mjs'], later_phase=True)
        else:
            checks.append(sdk_gate_entry(pi_sdk_state, pi_sdk_details, allowed_not_run,
                                         name=pi_sdk_name))
        # This gate uses both real installed SDKs, not the fake-RPC bridge.
        replay_deps = [(adapter, *verdicts[adapter]) for adapter in ('pi', 'pi-durable')]
        if all(state == 'ready' for _, state, _ in replay_deps):
            execute(replay_name, ['node', '--test', 'tests/pi-reasoning.test.mjs'],
                    count=node_counts, later_phase=True)
        else:
            state = ('mismatch' if any(s == 'mismatch' for _, s, _ in replay_deps)
                     else 'partial' if any(s == 'partial' for _, s, _ in replay_deps)
                     else 'absent')
            details = '; '.join(f'{adapter}: {detail}' for adapter, s, detail in replay_deps if s != 'ready')
            checks.append(sdk_gate_entry(state, details, allowed_not_run, name=replay_name))
        for path in javascript_sources():
            execute('javascript-syntax:' + path.relative_to(ROOT).as_posix(),
                    ['node', '--check', str(path)], later_phase=True)
    if has_bash:
        for path in shell_sources():
            execute('shell-syntax:' + path.name, ['bash', '-n', str(path)], later_phase=True)
    try:
        source_stable = source_sha == source_manifest_sha(ROOT)
        stable_details = 'source manifest unchanged' if source_stable else 'source changed during validation'
    except (OSError, ValueError):
        source_stable = False
        stable_details = 'source manifest unreadable after validation'
    checks.append({'name': 'source-manifest-stable', 'status': 'pass' if source_stable else 'fail',
                   'details': stable_details})
    if args.test_timings:
        # Rewrite with complete phases; the early partial write above already
        # preserved per-test evidence if a later check hangs or times out.
        # Best-effort: never changes the gate outcome.
        write_timings_artifact(args.test_timings, tests=pending_timings,
                               status=pending_unittest_status,
                               exit_code=pending_unittest_exit,
                               elapsed_seconds=pending_unittest_elapsed,
                               phases=checks)
    receipt = {'suite': 'offline', 'report_version': OFFLINE_REPORT_VERSION,
               'mizu_version': __version__, 'source_sha256': source_sha,
               'allowed_not_run': sorted(allowed_not_run), 'bash_available': has_bash,
               'node_version': checked_node_version,
               'ok': all(c['status'] != 'fail' for c in checks),
               'checks': checks, 'counts': counts, 'python': sys.version.split()[0], 'os': sys.platform,
               'later_phase': {'children': phase.children,
                               'budget_seconds': phase.budget,
                               'consumed_seconds': round(phase.consumed, 3)},
               'not_run': ['installed-Pi', 'rootless-Podman', 'live-provider', '24-hour-soak', 'cross-distribution-deployment']}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
    progress_stream.flush()
    sys.stdout = progress_stream.stream
    if progress_stream.truncated:
        print('note: progress output was shortened at the shared byte budget.')
    printed, shortened = receipt_text(receipt)
    print(printed)
    if shortened:
        if args.report:
            detail_note = ' The full receipt was written to the --report path.'
        else:
            detail_note = ' Pass --report to write the full receipt.'
        print("note: the printed receipt omits metadata, rows or details that do not fit its byte budget."
              + detail_note, flush=True)
    if not receipt['ok']:
        # The heading and every line, including structural labels and names,
        # share the summary's total byte budget. The complete check set remains
        # in the full receipt written above when --report was requested.
        print(FAILURE_SUMMARY_HEADING, flush=True)
        for line in failure_summary_lines(checks):
            print(line, flush=True)
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
