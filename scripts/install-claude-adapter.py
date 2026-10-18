#!/usr/bin/env python3
"""Explicit operator installation of the isolated official Python SDK environment."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

# GHSA-wf93-45jw-7689 (CVE-2026-8643): entry-point traversal on pip install, fixed in 26.1.2.
PIP_MINIMUM=(26,1,2)


def _pip_version(interpreter):
    out=subprocess.check_output([str(interpreter),'-m','pip','--version'],text=True,
                                stdin=subprocess.DEVNULL,timeout=30).strip()
    parts=out.split()
    if len(parts)<2 or parts[0] != 'pip':
        raise ValueError('Unparseable pip version: '+out)
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)*)(?:\.post([0-9]+))?',parts[1],re.IGNORECASE)
    if match is None:
        raise ValueError('Unsupported non-stable pip version: '+parts[1])
    nums=[int(piece) for piece in match.group(1).split('.')]
    while len(nums)>1 and nums[-1]==0:
        nums.pop()
    if match.group(2) is not None:
        nums.append(int(match.group(2)))
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
