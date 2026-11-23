#!/usr/bin/env python3
"""Reproducible local daemon-CPU measurement, not an LLM throughput claim.

Spawns real ``mizu daemon`` subprocesses against a synthetic deployment and
attributes their CPU time over an idle window and a one-change window. CPU is
sampled per PID from /proc when available, else from reaped children. Relative
before/after only. Unix-only.

By default the freshly armed project has an unconsumed operator wake generation,
so the daemon keeps attempting its first unit: the idle window is a retry-storm
case, not steady-state polling. ``--settled`` publishes a settled wait snapshot
recording that generation first, so idle is genuinely quiescent (no dispatch)
and the one-change notice is what wakes the daemon. Use the same flag before and
after a change; the two modes are not comparable to each other.
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


def _pid_cpu(pid):
    """Child CPU seconds from /proc/<pid>/stat, or None off Linux."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        ticks = int(stat[11]) + int(stat[12])  # utime + stime after comm
        return ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write the measurement receipt')
    parser.add_argument('--projects', type=int, default=2)
    parser.add_argument('--messages', type=int, default=200)
    parser.add_argument('--window', type=float, default=4.0)
    parser.add_argument('--warmup', type=float, default=0.5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--idle-seconds', type=int, default=1)
    parser.add_argument('--settled', action='store_true',
                        help='Publish a settled wait snapshot per project so idle is quiescent '
                             'instead of a fresh unconsumed-wake retry storm')
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
        if args.settled:
            # A fresh project's initial snapshot has not observed the arm's wake
            # generation, so should_run keeps admitting the first unit. Publish a
            # settled wait snapshot that records it, making idle genuinely quiet.
            for name in names:
                project = Project(config, name)
                snap = project.snapshots.get()
                captured = {'files': snap['files'], 'code_digest': snap['code_digest'],
                            'goal_digest': snap['goal_digest'], 'goal': snap['goal']}
                settled = project.snapshots.create(
                    captured, goal=snap['goal'], state='wait', run=None, outcome='wait',
                    summary='settled idle',
                    wake_generation=project.control().get('wake_generation', ''),
                    parent=snap['id'])
                project.snapshots.publish(settled)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))

        def _scenario(change):
            procs = []
            for name in names:
                cmd = [sys.executable, '-m', 'mizu', '--config', str(cfg),
                       'daemon', name, '--role', 'worker']
                procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, env=env))
            start = {}
            fallback = None
            try:
                time.sleep(args.warmup)
                # Measure only the steady-state window, excluding interpreter startup.
                # /proc gives per-PID CPU for the live children; where it is absent,
                # fall back to reaped-children CPU (which includes startup).
                start = {p.pid: _pid_cpu(p.pid) for p in procs}
                fallback = _cpu()
                if change:
                    # Notices wake role:worker: this is the measured change.
                    Project(load(cfg), 'p0').kernel_queues.append(
                        'notices', {'body': 'one change'}, 'operator')
                time.sleep(args.window)
                end = {p.pid: _pid_cpu(p.pid) for p in procs}
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
            if start and all(v is not None for v in start.values()):
                return round(sum(end[pid] - start[pid] for pid in start), 6)
            return round(_cpu() - fallback, 6)

        def _summary(samples):
            return {'median_seconds': round(statistics.median(samples), 6),
                    'min_seconds': round(min(samples), 6),
                    'max_seconds': round(max(samples), 6), 'samples': samples}

        idle = [_scenario(False) for _ in range(args.repeats)]
        change = [_scenario(True) for _ in range(args.repeats)]
        receipt = {
            'kind': 'local-daemon-cpu',
            'note': 'Synthetic local deployment; real mizu daemon subprocesses; '
                    'CPU sampled per PID over the steady-state window via /proc on '
                    'Linux (interpreter startup excluded); reaped-children '
                    'getrusage fallback elsewhere includes startup. Relative '
                    'before/after only, not a performance ceiling.',
            'projects': args.projects, 'retained_messages': args.messages,
            'idle_seconds': args.idle_seconds, 'window_seconds': args.window,
            'warmup_seconds': args.warmup, 'repeats': args.repeats,
            'settled': args.settled,
            'python': sys.version.split()[0], 'platform': platform.platform(),
            'idle': _summary(idle), 'one_change': _summary(change)}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
