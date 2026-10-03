#!/usr/bin/env python3
"""Explicit operator installation of the isolated official Python SDK environment."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True,help='New adapter environment directory')
    args=parser.parse_args()
    destination=args.directory.expanduser().resolve()
    if destination.exists():parser.error('Choose a new environment directory; existing content is never replaced')
    subprocess.run([sys.executable,'-m','venv',str(destination)],check=True)
    interpreter=destination/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    subprocess.run([str(interpreter),'-m','pip','install','--require-hashes','--only-binary=:all:',
                    '-r',str(ROOT/'adapters/claude/requirements.lock')],check=True)
    subprocess.run([str(interpreter),str(ROOT/'adapters/claude/launcher.py'),'--check-contract'],check=True)
    print('Set engines.claude.command to the interpreter:',interpreter)

if __name__=='__main__':main()
