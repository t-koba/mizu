#!/usr/bin/env python3
"""Run network-free quality gates. Missing required tools fail; absent
operator-provisioned adapter dependencies fail unless explicitly waived with
--allow-not-run, so the receipt never passes silently without coverage."""
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
from mizu.platform import popen_kwargs, spawn, terminate_process


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


def durable_gate_entry(state, details, allowed, *, name='node-durable-contract-tests') -> dict | None:
    """Map an SDK preflight state to its named gate entry (pure, no I/O).

    `ready` returns None: the caller runs the suite. `absent` fails closed
    unless the suite was explicitly waived, in which case it is recorded
    not_run with the waiver noted. `mismatch` always fails: wrong versions
    are a contract violation, not missing provisioning.
    """
    if state == 'ready':
        return None
    if state == 'absent' and name in allowed:
        return {'name': name, 'status': 'not_run',
                'details': details + ' (explicitly waived via --allow-not-run)'}
    return {'name': name, 'status': 'fail', 'details': details}


def _allow_list(value: str) -> frozenset:
    """Parse an --allow-not-run value into waived check names."""
    return frozenset(part.strip() for part in value.split(',') if part.strip())


DEFAULT_CHECK_TIMEOUT_SECONDS = 600
PROCESS_CLEANUP_TIMEOUT_SECONDS = 30


def _unittest_timeout_seconds(platform: str | None = None) -> int:
    """Give the Windows suite a 1450-second child cap under the 40-minute job cap."""
    return 1450 if (os.name if platform is None else platform) == 'nt' else 1000


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


def durable_deps_status(adapter: Path) -> tuple:
    """Check pinned adapter dependencies are installed at pinned versions.

    The durable contract suite imports the operator-verified Pi SDK, which is
    provisioned through the offline npm cache and never vendored into git.
    Returns ("ready", "") when every pinned dependency resolves at its pinned
    version, ("absent", reason) when nothing usable is installed, and
    ("mismatch", reason) when the manifest is unreadable or an installed
    version differs from the pin. Absent inputs are environment provisioning
    and fail closed unless explicitly waived via --allow-not-run; a version
    mismatch always fails like any contract violation.
    """
    try:
        manifest = json.loads((adapter / "package.json").read_text(encoding="utf-8"))
        pinned = manifest["dependencies"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ("mismatch", f"pinned dependency manifest unreadable: {exc}")
    if not isinstance(pinned, dict) or not pinned:
        return ("mismatch", "pinned dependency manifest holds no dependencies")
    modules = adapter / "node_modules"
    for name, version in pinned.items():
        try:
            found = json.loads((modules / name / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            found = None
        if found is None:
            return ("absent", f"{name}@{version} is not installed; provision the pinned Pi "
                              "dependencies via the offline npm cache before running the durable contract suite")
        if not isinstance(version, str) or found != version:
            return ("mismatch", f"{name} installed at {found}, pinned at {version}")
    return ("ready", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write a portable JSON validation receipt')
    parser.add_argument('--allow-not-run', default='',
                        help='Comma-separated check names explicitly permitted to stay not_run '
                             '(e.g. node-durable-contract-tests where the operator has not '
                             'provisioned the pinned SDK); absent durable dependencies fail '
                             'without an explicit waiver')
    args = parser.parse_args()
    allowed_not_run = _allow_list(args.allow_not_run)
    checks = []
    counts = {}
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONFAULTHANDLER': '1'}
    # The offline suite is process-spawn heavy. The 40-minute Windows job cap
    # starts before setup; budget for a 60 s setup, the 1450 s child, one 600 s
    # later check, bounded process cleanup, and a 120 s suffix/receipt reserve.
    # A contract test keeps the full budget relationship explicit. Step output
    # streams live (merged in order) so a hung step leaves
    # the last-running test visible, and a step timeout
    # is recorded as a structured failure with the partial output counted
    # instead of crashing the gate before the receipt is written.
    def execute(name, command, *, count=None, failure_summary=None,
                timeout=DEFAULT_CHECK_TIMEOUT_SECONDS):
        started = time.monotonic()
        print(f'== {name} ==', flush=True)
        chunks: list[str] = []
        def pump(stream):
            # Relay each line immediately with its arrival time: the
            # parent flushes per line (never only at EOF) and every line
            # carries monotonic elapsed, so a killed step's log shows
            # whether progress was steady until the budget ran out or one
            # line went quiet (a genuine stall). Chunks stay unprefixed so
            # counts and receipts parse the raw child output.
            # Only the reader closes its stream: closing a buffered stream
            # from another thread can block on that thread's pending read.
            with stream:
                for line in stream:
                    elapsed = time.monotonic() - started
                    sys.stdout.write(f'[+{elapsed:8.1f}s] {line}')
                    sys.stdout.flush()
                    chunks.append(line)
                sys.stdout.flush()
        status, exit_code, details = 'pass', 0, ''
        process = spawn(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1, **popen_kwargs())
        reader = threading.Thread(target=pump, args=(process.stdout,), daemon=True)
        try:
            reader.start()
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                status = 'fail'
                # Unittest diagnostics come only from structured metadata,
                # even on timeout. Other steps retain their raw output tail;
                # live timestamped progress still locates an unfinished test.
                details = f'timed out after {timeout} s'
                if failure_summary is None:
                    tail = ''.join(chunks)[-1500:]
                    details += f'; output tail: {tail.strip()}'
        finally:
            # A successful direct child may still leave descendants holding
            # stdout. End the whole group/job before waiting for EOF, without
            # Popen.__exit__ closing a buffered stream under a blocked reader.
            terminate_process(process)
            exit_code = process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
            reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
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
        entry = {'name': name, 'status': status,
                 'seconds': round(time.monotonic() - started, 3), 'exit_code': exit_code}
        if details:
            entry['details'] = details
        if count is not None:
            found = count(text)
            entry['tests'] = found
            counts[name] = found
        checks.append(entry)
    # AST checks avoid leaving bytecode inside an immutable source release.
    for directory in ('src', 'scripts', 'tests', 'examples', 'adapters/claude'):
        for path in (ROOT / directory).rglob('*.py'):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(ROOT)))
    checks.append({'name': 'python-syntax', 'status': 'pass'})
    def unittest_counts(output):
        import re
        ran = re.search(r'^Ran (\d+) tests?', output, re.M)
        failed = re.search(r'^FAILED', output, re.M)
        numbers = re.findall(r'(?:failures|errors)=(\d+)', output)
        return {'run': int(ran.group(1)) if ran else 0,
                'failed': sum(map(int, numbers)) if failed else 0,
                'skipped': int(re.search(r'skipped=(\d+)', output).group(1)) if re.search(r'skipped=(\d+)', output) else 0}
    def node_counts(output):
        import re
        passed = re.search(r'^# tests (\d+)', output, re.M)
        failed = re.search(r'^# fail (\d+)', output, re.M)
        return {'run': int(passed.group(1)) if passed else 0,
                'failed': int(failed.group(1)) if failed else 0,
                'skipped': int(re.search(r'^# skipped (\d+)', output, re.M).group(1)) if re.search(r'^# skipped (\d+)', output, re.M) else 0}
    # Timestamped verbose progress: on a timeout the killed step leaves the
    # unfinished test as the last timestamped line in the live log.
    # Windows/Python 3.14 was still progressing through test_runtime.EngineTests
    # when the 1150 s child cap killed a CI run; the same SHA passed on another
    # Windows runner. Keep the child cap at 1450 s. The workflow's 40-minute
    # limit begins before checkout/setup and covers one full later check timeout
    # plus bounded shutdown and a receipt/suffix reserve; POSIX retains 1000 s.
    # The child owns structured exception metadata. Its new sidecar is private
    # and deleted even on timeout; no failure/timeout details parse raw output.
    with tempfile.TemporaryDirectory(prefix='mizu-unittest-') as temporary:
        sidecar = Path(temporary) / 'failures.jsonl'
        execute('python-unit-and-contract-tests',
                [sys.executable, '-u', str(ROOT / 'scripts/check_unittest.py'), str(sidecar)],
                count=unittest_counts, failure_summary=lambda: unittest_failure_summary(sidecar),
                timeout=_unittest_timeout_seconds())
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
                count=node_counts)
        durable_state, durable_details = durable_deps_status(ROOT / 'adapters/pi-durable')
        if durable_state == 'ready':
            execute('node-durable-contract-tests', ['node', '--test', 'tests/durable.test.mjs'],
                    count=node_counts)
        else:
            checks.append(durable_gate_entry(durable_state, durable_details, allowed_not_run))
        # This gate uses both real installed SDKs, not the fake-RPC bridge.
        replay_name = 'node-pi-reasoning-replay-tests'
        replay_deps = [(adapter, *durable_deps_status(ROOT / 'adapters' / adapter))
                       for adapter in ('pi', 'pi-durable')]
        if all(state == 'ready' for _, state, _ in replay_deps):
            execute(replay_name, ['node', '--test', 'tests/pi-reasoning.test.mjs'], count=node_counts)
        else:
            state = 'mismatch' if any(s == 'mismatch' for _, s, _ in replay_deps) else 'absent'
            details = '; '.join(f'{adapter}: {detail}' for adapter, s, detail in replay_deps if s != 'ready')
            checks.append(durable_gate_entry(state, details, allowed_not_run, name=replay_name))
        for path in sorted([*(ROOT / 'adapters/pi').glob('*.mjs'), *(ROOT / 'adapters/pi-durable').glob('*.mjs'), *(ROOT / 'scripts').glob('*.mjs')]):
            execute('javascript-syntax:' + path.name, ['node', '--check', str(path)])
    if has_bash:
        for path in sorted((ROOT / 'scripts').glob('*.sh')):
            execute('shell-syntax:' + path.name, ['bash', '-n', str(path)])
    receipt = {'suite': 'offline', 'mizu_version': __version__,
               'ok': all(c['status'] != 'fail' for c in checks),
               'checks': checks, 'counts': counts, 'python': sys.version.split()[0], 'os': sys.platform,
               'not_run': ['installed-Pi', 'rootless-Podman', 'live-provider', '24-hour-soak', 'cross-distribution-deployment']}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))
    if not receipt['ok']:
        # Tail-visible diagnosis: the VCS log view truncates to the tail,
        # which hides the failing entry at the head of the receipt checks
        # array (verbose progress plus later suites bury it). Re-print each
        # failure last so a timeout names the hanging test in the tail.
        print('FAILED CHECKS SUMMARY (tail-visible):', flush=True)
        for failed in [c for c in checks if c.get('status') == 'fail']:
            detail = str(failed.get('details', '')).strip().replace('\n', ' | ')[-1500:]
            print(f"- {failed.get('name')}: exit={failed.get('exit_code', '?')} {detail}", flush=True)
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
