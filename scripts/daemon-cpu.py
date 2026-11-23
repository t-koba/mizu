#!/usr/bin/env python3
"""Reproducible local daemon-CPU measurement, not an LLM throughput claim.

Spawns real ``mizu daemon`` subprocesses against a synthetic deployment and
attributes their reaped CPU time to an idle window and a one-change window.
Relative before/after only. Unix-only: requires ``resource.RUSAGE_CHILDREN``.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu.cli import configure
from mizu.config import load
from mizu.project import Project, initialize


def _cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write the measurement receipt')
    parser.add_argument('--projects', type=int, default=2)
    parser.add_argument('--messages', type=int, default=200)
    parser.add_argument('--window', type=float, default=4.0)
    parser.add_argument('--warmup', type=float, default=0.5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--idle-seconds', type=int, default=1)
    args = parser.parse_args()
    for name, value, low, high in (
            ('projects', args.projects, 1, 16), ('messages', args.messages, 0, 5000),
            ('window', args.window, 1, 120), ('warmup', args.warmup, 0.1, 30),
            ('repeats', args.repeats, 1, 20), ('idle-seconds', args.idle_seconds, 1, 86400)):
        if not low <= value <= high:
            parser.error(f'--{name.replace("_", "-")} must be {low}..{high}')

    with tempfile.TemporaryDirectory(prefix='mizu-daemon-cpu-') as td:
        root = Path(td)
        cfg = root / 'config/config.toml'
        configure(cfg, None)
        text = cfg.read_text()
        text = text.replace('data_dir = "~/.local/state/mizu"',
                            'data_dir = ' + json.dumps(str(root / 'projects')))
        text = text.replace('idle_seconds = 15', f'idle_seconds = {args.idle_seconds}')
        text = text.replace('free_disk_mb = 1024', 'free_disk_mb = 0')
        cfg.write_text(text)
        config = load(cfg)
        names = [f'p{n}' for n in range(args.projects)]
        for name in names:
            src = root / f'src-{name}'
            src.mkdir()
            (src / 'app.py').write_text('def main():\n    return "ok"\n')
            goal = root / f'goal-{name}.md'
            goal.write_text('Synthetic daemon CPU measurement project.\n')
            initialize(config, name, src, goal, ['worker'], ['python3 -m unittest'])
            Project(config, name).set_control(armed=True, paused=False,
                                              wake_generation='daemon-cpu')
        # Retained data only: questions has no worker wake and is role:* writable.
        for _ in range(args.messages):
            Project(config, 'p0').kernel_queues.append('questions',
                {'question': 'synthetic', 'default': 'x', 'context': 'cpu-harness'},
                'role:worker')
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

        def _scenario(change):
            before = _cpu()
            procs = []
            for name in names:
                cmd = [sys.executable, '-m', 'mizu', '--config', str(cfg),
                       'daemon', name, '--role', 'worker']
                procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, env=env))
            try:
                time.sleep(args.warmup)
                if change:
                    # Notices wake role:worker: this is the measured change.
                    Project(load(cfg), 'p0').kernel_queues.append(
                        'notices', {'body': 'one change'}, 'operator')
                time.sleep(args.window)
            finally:
                for p in procs:
                    if p.poll() is None:
                        p.terminate()
                for p in procs:
                    try:
                        p.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.communicate()
            # RUSAGE_CHILDREN accrues only reaped children.
            return round(_cpu() - before, 6)

        def _summary(samples):
            return {'median_seconds': round(statistics.median(samples), 6),
                    'min_seconds': round(min(samples), 6),
                    'max_seconds': round(max(samples), 6), 'samples': samples}

        idle = [_scenario(False) for _ in range(args.repeats)]
        change = [_scenario(True) for _ in range(args.repeats)]
        receipt = {
            'kind': 'local-daemon-cpu',
            'note': 'Synthetic local deployment; real mizu daemon subprocesses; '
                    'child CPU via getrusage(RUSAGE_CHILDREN) including interpreter '
                    'startup; relative before/after only, not a performance ceiling.',
            'projects': args.projects, 'retained_messages': args.messages,
            'idle_seconds': args.idle_seconds, 'window_seconds': args.window,
            'warmup_seconds': args.warmup, 'repeats': args.repeats,
            'python': sys.version.split()[0], 'platform': platform.platform(),
            'idle': _summary(idle), 'one_change': _summary(change)}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
