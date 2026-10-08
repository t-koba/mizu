#!/usr/bin/env python3
"""Explicit operator installation of the isolated official Python SDK environment."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

# GHSA-wf93-45jw-7689 (CVE-2026-8643): entry-point traversal on pip install, fixed in 26.1.2.
PIP_MINIMUM=(26,1,2)


def _pip_version(interpreter):
    out=subprocess.check_output([str(interpreter),'-m','pip','--version'],text=True,
                                stdin=subprocess.DEVNULL,timeout=30).strip()
    parts=out.split()
    if len(parts)<2:raise ValueError('Unparseable pip version: '+out)
    nums=[]
    for piece in parts[1].split('.'):
        digits=''.join(c for c in piece if c.isdigit())
        if not digits:break
        nums.append(int(digits))
    return tuple(nums)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True,help='New adapter environment directory')
    args=parser.parse_args()
    destination=args.directory.expanduser().resolve()
    if destination.exists():parser.error('Choose a new environment directory; existing content is never replaced')
    subprocess.run([sys.executable,'-m','venv',str(destination)],check=True)
    interpreter=destination/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    try:
        found=_pip_version(interpreter)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        parser.error(f'Cannot verify pip version in the new environment: {exc}')
    if found<PIP_MINIMUM:
        parser.error('Refusing install: new-environment pip %s is below the 26.1.2 floor '
                     '(GHSA-wf93-45jw-7689); upgrade pip first' % ('.'.join(map(str,found)) or 'unknown'))
    subprocess.run([str(interpreter),'-m','pip','install','--require-hashes','--only-binary=:all:',
                    '-r',str(ROOT/'adapters/claude/requirements.lock')],check=True)
    subprocess.run([str(interpreter),str(ROOT/'adapters/claude/launcher.py'),'--check-contract'],check=True)
    print('Set engines.claude.command to the interpreter:',interpreter)

if __name__=='__main__':main()
