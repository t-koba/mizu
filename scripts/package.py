#!/usr/bin/env python3
"""Build deterministic, inspectable source archives without runtime/private state."""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mizu import PI_MINIMUM, NODE_MINIMUM, __version__


def version_tuple(value) -> tuple:
    try:
        parts = tuple(map(int, str(value).split(".")))
    except ValueError:
        return ()
    return parts if len(parts) == 3 else ()

EXCLUDED = {'.git', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache',
            '.venv', 'dist', 'build', 'private-validation', '.DS_Store'}
PRIVATE = {'credentials.env', 'config.local.toml', 'installation.json', 'source-manifest.json'}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files(output: Path):
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts) or output in path.parents:
            continue
        if path.name in PRIVATE or path.name.startswith('.env') or path.suffix in ('.pyc', '.pyo', '.zip') or path.name.endswith('.tar.gz'):
            continue
        if path.is_symlink():
            raise ValueError('Refusing source symlink: ' + str(relative))
        if path.is_file():
            yield str(relative), path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--release', action='store_true', help='Require a complete dependency lock before building a public release')
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output in ROOT.parents:
        parser.error('Output must not be the source root or its ancestor')
    lock = ROOT / 'adapters/pi/package-lock.json'
    if args.release:
        if not lock.exists():
            parser.error('Strict release requires reviewed adapters/pi/package-lock.json; run scripts/lock-pi.sh first')
        content = json.loads(lock.read_text())
        if content.get('lockfileVersion', 0) < 2 or not content.get('packages'):
            parser.error('Dependency lock is incomplete')
        for name in ('@earendil-works/pi-coding-agent', '@earendil-works/pi-ai'):
            item = content['packages'].get('node_modules/' + name, {})
            if not version_tuple(item.get('version')) or version_tuple(item.get('version')) < version_tuple(PI_MINIMUM) \
                    or not item.get('integrity'):
                parser.error('Lock must pin reviewed Pi packages at or above the minimum with registry integrity')
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    if epoch < 0:
        parser.error('SOURCE_DATE_EPOCH cannot be negative')
    files = list(source_files(output))
    metadata = {'schema': 1, 'name': 'mizu', 'version': __version__, 'kind': 'locked-source' if lock.exists() else 'source-unlocked',
                'pi_minimum': PI_MINIMUM, 'node_minimum': NODE_MINIMUM, 'dependency_lock_present': lock.exists(),
                'integration_acceptance': 'See docs/VALIDATION.md; no live acceptance implied by packaging'}
    files.append(('DISTRIBUTION.json', (json.dumps(metadata, indent=2) + '\n').encode(), 0o644))
    files.sort(key=lambda x: x[0])
    manifest = ''.join(f'{sha(data)}  {name}\n' for name, data, mode in files).encode()
    files.append(('MANIFEST.sha256', manifest, 0o644))
    files.sort(key=lambda x: x[0])
    output.mkdir(parents=True, exist_ok=True)
    stem = 'mizu-' + __version__
    tarpath, zippath = output / (stem + '.tar.gz'), output / (stem + '.zip')
    with tarpath.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name, data, mode in files:
                info = tarfile.TarInfo(stem + '/' + name)
                info.size, info.mode, info.mtime = len(data), mode, epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                archive.addfile(info, io.BytesIO(data))
    date = datetime.datetime.fromtimestamp(max(epoch, 315532800), datetime.timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(zippath, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in files:
            info = zipfile.ZipInfo(stem + '/' + name, date_time=date)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    sums = ''.join(f'{sha(path.read_bytes())}  {path.name}\n' for path in (tarpath, zippath))
    (output / 'SHA256SUMS').write_text(sums)
    print(json.dumps({'archives': [str(tarpath), str(zippath)], 'files': len(files), 'kind': metadata['kind']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
