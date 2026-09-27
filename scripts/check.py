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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write a portable JSON validation receipt')
    args = parser.parse_args()
    checks = []
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    def execute(name, command):
        started = time.monotonic()
        print(f'== {name} ==', flush=True)
        process = subprocess.run(command, cwd=ROOT, env=env, check=False)
        checks.append({'name': name, 'status': 'pass' if process.returncode == 0 else 'fail',
                       'seconds': round(time.monotonic() - started, 3), 'exit_code': process.returncode})
    # AST checks avoid leaving bytecode inside an immutable source release.
    for directory in ('src', 'scripts', 'tests', 'examples'):
        for path in (ROOT / directory).rglob('*.py'):
            ast.parse(path.read_text(), filename=str(path.relative_to(ROOT)))
    checks.append({'name': 'python-syntax', 'status': 'pass'})
    execute('python-unit-and-contract-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'])
    if not shutil.which('node'):
        checks.append({'name': 'node-available', 'status': 'fail'})
    # bash is a POSIX convenience for shell-syntax checks, not a runtime
    # requirement on Windows/macOS development: missing bash is not_run.
    has_bash = bool(shutil.which('bash'))
    if not has_bash:
        checks.append({'name': 'shell-syntax', 'status': 'not_run',
                       'details': 'bash is not available; shell wrappers are POSIX-only'})
    if shutil.which('node'):
        execute('node-transport-and-extension-tests', ['node', '--test', 'tests/bridge.test.mjs'])
        for path in sorted((ROOT / 'adapters/pi').glob('*.mjs')):
            execute('javascript-syntax:' + path.name, ['node', '--check', str(path)])
    if has_bash:
        for path in sorted((ROOT / 'scripts').glob('*.sh')):
            execute('shell-syntax:' + path.name, ['bash', '-n', str(path)])
    receipt = {'schema': 1, 'suite': 'offline', 'mizu_version': __version__,
               'ok': all(c['status'] != 'fail' for c in checks),
               'checks': checks, 'not_run': ['installed-Pi', 'rootless-Podman', 'live-provider', '24-hour-soak', 'cross-distribution-deployment']}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
