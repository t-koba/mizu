#!/usr/bin/env python3
"""Reproducible local snapshot microbenchmark, not an LLM throughput claim."""
from __future__ import annotations
import argparse
import dataclasses
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu.snapshot import Snapshots
from mizu.cli import configure
from mizu.config import load
from mizu.project import initialize
from mizu.fs import write_json
from mizu.usage import summarize
from mizu.storage import backup, restore, prune
from mizu.budget import Budget
from mizu.distribution import source_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Write the benchmark receipt (regenerable evidence, e.g. private-validation/benchmark.json)')
    parser.add_argument('--files', type=int, default=500)
    parser.add_argument('--bytes-per-file', type=int, default=4096)
    parser.add_argument('--records', type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.files <= 10000 or not 128 <= args.bytes_per_file <= 65536:
        parser.error('Choose 1..10000 files and 128..65536 bytes per file')
    if not 10 <= args.records <= 5000:
        parser.error('Choose 10..5000 synthetic records')
    with tempfile.TemporaryDirectory(prefix='mizu-benchmark-') as td:
        root = Path(td); workspace = root / 'workspace'; workspace.mkdir()
        for n in range(args.files):
            data = (str(n) + '\n').encode()
            (workspace / f'file-{n:05}.txt').write_bytes(data + b'x' * (args.bytes_per_file - len(data)))
        snapshots = Snapshots(root / 'store', excludes=(), max_file=65536,
                              max_bytes=args.files * args.bytes_per_file, max_files=args.files)
        def timed(function):
            tracemalloc.start()
            start = time.perf_counter(); result = function()
            seconds = round(time.perf_counter() - start, 6)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return result, {"seconds": seconds, "peak_python_bytes": peak}
        first, initial = timed(lambda: snapshots.capture_files(workspace))
        second, unchanged = timed(lambda: snapshots.capture_files(workspace))
        _, materialize = timed(lambda: snapshots.materialize(first, root / 'observer'))
        objects = sum(1 for _ in (root / 'store/objects').iterdir())
        assert first['code_digest'] == second['code_digest'] and objects == args.files
        config_file = root/'config.toml'
        configure(config_file, None)
        config = load(config_file)
        config = dataclasses.replace(config, data=root/'projects',
            limits=dataclasses.replace(config.limits, free_disk_mb=0,
                snapshot_files=max(args.files, config.limits.snapshot_files),
                snapshot_bytes=max(args.files*args.bytes_per_file, config.limits.snapshot_bytes)))
        goal = root/'goal.md';goal.write_text('Synthetic filesystem benchmark.\n')
        project = initialize(config,'benchmark',workspace,goal,['worker'],['python3 -m unittest'])
        stamp = '2026-10-03T00:00:00+00:00'
        # Dataset setup is excluded from measurements; all records are synthetic.
        for n in range(args.records):
            item_id=f'item-{n:05d}'
            project.insights.submit(source='benchmark',title='synthetic',body='x'*128,
                                    base_snapshot=None,insight_id=item_id)
            write_json(project.root/'decisions'/f'{item_id}.json',
                       {'id':item_id,'created_at':stamp,'action':'reject','reason':'synthetic'})
            write_json(project.root/'runs'/f'run-{n:05d}'/'result.json',
                       {'run':f'run-{n:05d}','finished_at':stamp,'status':'completed',
                        'model':{'engine':'codex','provider':'synthetic','model':'synthetic',
                                 'requests':1,'usage_known':True,'usage':[{'input_tokens':17,'output_tokens':3}]}})
        for n in range(min(args.records,128)):
            snap=snapshots.create(first,goal='g',state=str(n),run=None,outcome='wait',summary='synthetic')
            snapshots.publish(snap)
        _, history = timed(lambda:snapshots.history(snap['id']))
        _, difference = timed(lambda:snapshots.changes(snap['id']))
        _, insights = timed(lambda:project.insights.projection(limit=30))
        _, usage = timed(lambda:summarize(project))
        _, generation = timed(project.insights.generation)
        _, gc = timed(project.insights.gc_decided)
        for n in range(args.records):
            write_json(project.root/'spool/editor'/f'spool-{n:05d}.json',{'title':'synthetic','body':'synthetic','base_snapshot':None})
            write_json(project.root/'artifacts'/f'artifact-{n:05d}'/'evidence.json',{'published_at':stamp})
            path=project.root/'runs'/f'run-{n:05d}'/'input';path.mkdir();(path/'temporary').write_bytes(b'x')
        _, ingest = timed(project.insights.ingest_editor)
        inventory_files, inventory = timed(lambda:list(source_files(ROOT)))
        budget=Budget(root/'budget',daily=1)
        import datetime as dt
        for n in range(args.records):
            day=(dt.date.today()-dt.timedelta(days=n)).isoformat()
            write_json(root/'budget'/f'{day}.json',{'day':day,'requests':[]})
        _, budget_gc = timed(budget.gc)
        project.set_control(paused=True,armed=False)
        _, pruning = timed(lambda:prune(project,apply=False))
        archive=root/'backup.tar.gz'
        _, archived = timed(lambda:backup(project,archive,verify=True))
        _, restored = timed(lambda:restore(config,'restored',archive))
        receipt = {'kind':'local-filesystem-and-evidence-benchmark',
                          'files':args.files,'bytes_per_file':args.bytes_per_file,
                          'records_per_insight_decision_run':args.records,'spool_artifact_budget_records':args.records,'history_records':min(args.records,128),
                          'measurements':{'initial_capture':initial,'unchanged_capture':unchanged,
                                          'materialize':materialize,'history':history,'diff':difference,
                                          'insight_projection':insights,'usage_scan':usage,
                                          'backup':archived,'restore':restored,'insight_generation':generation,'insight_gc':gc,
                                          'editor_ingest':ingest,'source_inventory':inventory,'budget_gc':budget_gc,'prune_dry_run':pruning},
                          'objects_after_two_captures':objects,'public_source_files':len(inventory_files),
                          'note':'Synthetic local data; Python allocation peaks, not process RSS. No provider/container/competitor claim.'}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
