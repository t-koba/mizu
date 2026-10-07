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
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu import __version__


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


def durable_gate_entry(state, details, allowed) -> dict | None:
    """Map a durable preflight state to its gate check entry (pure, no I/O).

    `ready` returns None: the caller runs the suite. `absent` fails closed
    unless the suite was explicitly waived, in which case it is recorded
    not_run with the waiver noted. `mismatch` always fails: wrong versions
    are a contract violation, not missing provisioning.
    """
    name = 'node-durable-contract-tests'
    if state == 'ready':
        return None
    if state == 'absent' and name in allowed:
        return {'name': name, 'status': 'not_run',
                'details': details + ' (explicitly waived via --allow-not-run)'}
    return {'name': name, 'status': 'fail', 'details': details}


def _allow_list(value: str) -> frozenset:
    """Parse an --allow-not-run value into waived check names."""
    return frozenset(part.strip() for part in value.split(',') if part.strip())


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
    # The offline suite is process-spawn heavy (~75 s on Linux); Windows
    # runners need substantially more headroom than 300 s, well within the
    # 15-minute CI job budget. Step output streams live (merged in order) so
    # a hung step leaves the last-running test visible, and a step timeout
    # is recorded as a structured failure with the partial output counted
    # instead of crashing the gate before the receipt is written.
    def execute(name, command, *, count=None, timeout=600):
        started = time.monotonic()
        print(f'== {name} ==', flush=True)
        chunks: list[str] = []
        def pump(stream):
            for line in stream:
                sys.stdout.write(line)
                chunks.append(line)
            sys.stdout.flush()
        status, exit_code, details = 'pass', 0, ''
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
            reader = threading.Thread(target=pump, args=(process.stdout,), daemon=True)
            reader.start()
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=30)
                status = 'fail'
                # A killed step prints no summary, so the report carries the
                # last output: with unbuffered verbose progress this names the
                # test that never finished instead of a bare run:0.
                tail = ''.join(chunks)[-1500:]
                details = f'timed out after {timeout} s; output tail: {tail.strip()}'
            reader.join(timeout=30)
        text = ''.join(chunks)
        if exit_code != 0:
            status = 'fail'
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
    # Unbuffered verbose progress: on a timeout the killed step leaves the
    # hanging test as the last unfinished line (see output-tail details).
    execute('python-unit-and-contract-tests', [sys.executable, '-u', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
            count=unittest_counts)
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
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
