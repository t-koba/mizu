#!/usr/bin/env python3
"""Run network-free quality gates. Missing required tools fail, never silently skip."""
from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write a portable JSON validation receipt')
    args = parser.parse_args()
    checks = []
    counts = {}
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    def execute(name, command, *, count=None):
        started = time.monotonic()
        print(f'== {name} ==', flush=True)
        process = subprocess.run(command, cwd=ROOT, env=env, check=False,
                                 capture_output=True, text=True, timeout=180)
        sys.stdout.write(process.stdout)
        sys.stderr.write(process.stderr)
        status = 'pass' if process.returncode == 0 else 'fail'
        entry = {'name': name, 'status': status,
                 'seconds': round(time.monotonic() - started, 3), 'exit_code': process.returncode}
        if count is not None:
            found = count(process.stdout + process.stderr)
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
    execute('python-unit-and-contract-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
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
        for path in sorted([*(ROOT / 'adapters/pi').glob('*.mjs'), *(ROOT / 'scripts').glob('*.mjs')]):
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
