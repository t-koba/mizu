#!/usr/bin/env python3
"""Reproducible local snapshot microbenchmark, not an LLM throughput claim."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu.snapshot import Snapshots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=500)
    parser.add_argument('--bytes-per-file', type=int, default=4096)
    args = parser.parse_args()
    if not 1 <= args.files <= 10000 or not 128 <= args.bytes_per_file <= 65536:
        parser.error('Choose 1..10000 files and 128..65536 bytes per file')
    with tempfile.TemporaryDirectory(prefix='mizu-benchmark-') as td:
        root = Path(td); workspace = root / 'workspace'; workspace.mkdir()
        for n in range(args.files):
            data = (str(n) + '\n').encode()
            (workspace / f'file-{n:05}.txt').write_bytes(data + b'x' * (args.bytes_per_file - len(data)))
        snapshots = Snapshots(root / 'store', excludes=(), max_file=65536,
                              max_bytes=args.files * args.bytes_per_file, max_files=args.files)
        def timed(function):
            start = time.perf_counter(); result = function()
            return result, round(time.perf_counter() - start, 6)
        first, initial = timed(lambda: snapshots.capture_files(workspace))
        second, unchanged = timed(lambda: snapshots.capture_files(workspace))
        _, materialize = timed(lambda: snapshots.materialize(first, root / 'observer'))
        objects = sum(1 for _ in (root / 'store/objects').iterdir())
        assert first['code_digest'] == second['code_digest'] and objects == args.files
        print(json.dumps({'schema': 1, 'kind': 'local-filesystem-microbenchmark',
                          'files': args.files, 'bytes_per_file': args.bytes_per_file,
                          'seconds': {'initial_capture': initial, 'unchanged_capture': unchanged, 'materialize': materialize},
                          'objects_after_two_captures': objects,
                          'note': 'Measured only on the current filesystem; excludes Pi, providers, containers and production workload.'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
