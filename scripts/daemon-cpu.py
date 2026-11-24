#!/usr/bin/env python3
"""Reproducible local daemon-CPU measurement, not an LLM throughput claim.

Spawns real ``mizu daemon`` subprocesses against a synthetic deployment and
attributes their CPU time over an idle window and a one-change window. CPU is
sampled per PID from /proc when available, else from reaped children. The
measured window starts only after a minimum warmup plus an observed quiet CPU
trend: interpreter startup is excluded only when the receipt's validity block
reports steady_state_observed=true. Without /proc, the reaped-children getrusage
fallback includes startup and cannot evidence a steady window. Relative
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
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_WARMUP = 3.0
PROBE_SECONDS = 0.25
STEADY_THRESHOLD = 0.01  # CPU-seconds per child per probe
STEADY_PROBES = 2
MAX_SETTLE_SECONDS = 30.0

ROOT = Path(__file__).resolve().parents[1]
# The measured source can be a different checkout (e.g. the revision before a
# daemon change). Both this harness's fixture driver and the daemon subprocesses
# load mizu from that one source, so the receipt's provenance names exactly the
# code that ran. MIZU_CPU_SOURCE_ROOT overrides the tree; MIZU_CPU_SOURCE_REVISION
# carries its git label when the tree has no .git (e.g. an exported archive).
SOURCE_ROOT = Path(os.environ.get('MIZU_CPU_SOURCE_ROOT', str(ROOT))).resolve()
SOURCE_REVISION = os.environ.get('MIZU_CPU_SOURCE_REVISION')
sys.path.insert(0, str(SOURCE_ROOT / 'src'))
from mizu.cli import configure
from mizu.config import load
from mizu.project import Project, initialize


def _source_digest(src: Path) -> str:
    """Content digest of the daemon source actually placed on PYTHONPATH."""
    digest = hashlib.sha256()
    for path in sorted((src / 'mizu').rglob('*.py')):
        digest.update(str(path.relative_to(src)).encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _git_head(tree: Path) -> str | None:
    try:
        result = subprocess.run(['git', '-C', str(tree), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _provenance(daemon_cmd, *, launched_at=None) -> dict:
    """Bind a receipt to the exact source and command it measured, at launch."""
    head = _git_head(SOURCE_ROOT)
    return {
        'source_root': str(SOURCE_ROOT),
        'source_revision': head or SOURCE_REVISION,
        'source_revision_resolved_from_git': head is not None,
        'daemon_src_sha256': _source_digest(SOURCE_ROOT / 'src'),
        'harness_src_sha256': _source_digest(ROOT / 'src'),
        'python_executable': sys.executable,
        'daemon_command': list(daemon_cmd),
        'launched_at_utc': (launched_at or datetime.now(timezone.utc)).isoformat(),
    }


def _cpu():
    # Only the fallback measurement path needs this Unix-only module. Import it
    # lazily so the harness's pure functions and argument checks stay testable
    # on every platform; actual measurement remains Unix-only.
    import resource

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


def _steady_state(procs, *, min_warmup, sleep=time.sleep, clock=time.monotonic,
                  probe_seconds=PROBE_SECONDS, threshold=STEADY_THRESHOLD,
                  required_probes=STEADY_PROBES, max_settle=MAX_SETTLE_SECONDS):
    """Wait out the minimum warmup, then require a quiet per-PID CPU trend.

    Linux polls the live children through /proc. RUSAGE_CHILDREN accrues only
    reaped children, so off Linux there is no live signal to observe and the
    fallback cannot evidence a steady window.
    """
    started = clock()
    sleep(min_warmup)
    samples = []
    values = [_pid_cpu(p.pid) for p in procs]
    method = ('proc-per-pid' if values and all(v is not None for v in values)
              else 'reaped-children-getrusage')
    observed = False
    if method == 'proc-per-pid':
        deadline = started + min_warmup + max_settle
        quiet = 0
        while clock() < deadline:
            before = [_pid_cpu(p.pid) for p in procs]
            if any(v is None for v in before):
                break
            sleep(probe_seconds)
            after = [_pid_cpu(p.pid) for p in procs]
            if any(v is None for v in after):
                break
            growth = (sum(after) - sum(before)) / len(procs)
            samples.append(round(growth, 6))
            quiet = quiet + 1 if growth <= threshold else 0
            if quiet >= required_probes:
                observed = True
                break
    info = {'method': method, 'steady_state_observed': observed,
            'observed_warmup_seconds': round(clock() - started, 3), 'probes': samples}
    return observed, info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write the measurement receipt')
    parser.add_argument('--projects', type=int, default=2)
    parser.add_argument('--messages', type=int, default=200)
    parser.add_argument('--window', type=float, default=4.0)
    parser.add_argument('--warmup', type=float, default=DEFAULT_WARMUP,
                        help='Minimum warmup seconds before steady-state probing')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--idle-seconds', type=int, default=1)
    parser.add_argument('--settled', action='store_true',
                        help='Publish a settled wait snapshot per project so idle is quiescent '
                             'instead of a fresh unconsumed-wake retry storm')
    args = parser.parse_args()
    for name, value, low, high in (
            ('projects', args.projects, 1, 16), ('messages', args.messages, 0, 5000),
            ('window', args.window, 1, 120), ('warmup', args.warmup, 0.1, 60),
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
        env = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT / 'src'))

        def _scenario(change, label, repeat):
            procs = []
            for name in names:
                cmd = [sys.executable, '-m', 'mizu', '--config', str(cfg),
                       'daemon', name, '--role', 'worker']
                procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, env=env))
            start = {}
            end = {}
            fallback = None
            try:
                _, info = _steady_state(procs, min_warmup=args.warmup)
                info['scenario'] = label
                info['repeat'] = repeat
                # The window starts only once the CPU trend has been seen quiet.
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
            if all(start.get(p.pid) is not None and end.get(p.pid) is not None
                   for p in procs):
                value = round(sum(end[p.pid] - start[p.pid] for p in procs), 6)
            else:
                # Reaped children are then the only CPU source; that delta
                # includes startup and cannot evidence a steady window.
                info['method'] = 'reaped-children-getrusage'
                info['steady_state_observed'] = False
                value = round(_cpu() - fallback, 6)
            return value, info

        def _summary(samples):
            return {'median_seconds': round(statistics.median(samples), 6),
                    'min_seconds': round(min(samples), 6),
                    'max_seconds': round(max(samples), 6), 'samples': samples}

        sample_cmd = [sys.executable, '-m', 'mizu', '--config', '<synthetic>',
                      'daemon', names[0], '--role', 'worker']
        launched_at = datetime.now(timezone.utc)
        provenance = _provenance(sample_cmd, launched_at=launched_at)
        idle_results = [_scenario(False, 'idle', n) for n in range(args.repeats)]
        change_results = [_scenario(True, 'one_change', n) for n in range(args.repeats)]
        idle = [value for value, _ in idle_results]
        change = [value for value, _ in change_results]
        scenarios = [info for _, info in idle_results + change_results]
        validity = {
            'method': scenarios[0]['method'],
            'steady_state_observed': all(info['steady_state_observed'] for info in scenarios),
            'minimum_requested_warmup_seconds': args.warmup,
            'observed_warmup_seconds': {
                label: [info['observed_warmup_seconds'] for info in scenarios
                        if info['scenario'] == label]
                for label in ('idle', 'one_change')},
            'probe_seconds': PROBE_SECONDS,
            'steady_threshold_cpu_seconds_per_child': STEADY_THRESHOLD,
            'consecutive_quiet_probes': STEADY_PROBES,
            'max_settle_seconds': MAX_SETTLE_SECONDS,
            'scenarios': scenarios,
        }
        receipt = {
            'kind': 'local-daemon-cpu',
            'note': 'Synthetic local deployment; real mizu daemon subprocesses. The '
                    'window starts only after a quiet per-PID /proc CPU trend, so '
                    'interpreter startup is excluded only when validity.steady_state_observed '
                    'is true; the reaped-children getrusage fallback includes startup and '
                    'cannot evidence a steady window. Relative before/after only, not a '
                    'performance ceiling.',
            'projects': args.projects, 'retained_messages': args.messages,
            'idle_seconds': args.idle_seconds, 'window_seconds': args.window,
            'warmup_seconds': args.warmup, 'repeats': args.repeats,
            'settled': args.settled,
            'python': sys.version.split()[0], 'platform': platform.platform(),
            'source': provenance,
            'idle': _summary(idle), 'one_change': _summary(change),
            'validity': validity}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
        return 0 if validity['steady_state_observed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
