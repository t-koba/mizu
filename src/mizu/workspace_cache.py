"""Best-effort cache reclamation before execution locks.

No admission, pause, drain, or queue state is changed. All exclusions are
nonblocking and held through preflight and deletion; partial acquisition is
rolled back before returning. The lock order is workspace, sorted role/slot
exclusions, check-cache. Unlike execution admission this never waits, so it
cannot introduce a lock-order cycle with a role or check runner.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import time
import unicodedata
from pathlib import Path

from .admission import check_slot_exclusions, role_exclusions
from .errors import Busy, Denied, LimitExceeded
from .fs import lock, read_json, relative_parts, write_json
from .process import run
from .project import git_env

# Bound metadata work, not cache bytes read (no file contents are opened).
MAX_ENTRIES = 100000
MAX_DEPTH = 64
SCAN_SECONDS = 10
GIT_BYTES = 8 * 1024 * 1024


def _directory(path):
    info = path.lstat()
    # Windows junctions/reparse points are links for this purpose too.
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise Denied('Cache path is not a plain directory')


def _snapshot_protected(snapshots, name):
    """Protect every product-reserved component omitted from captures."""
    if snapshots._reserved_prefix(name) or snapshots._reserved_prefix(name.casefold()):
        return True
    for part in name.split('/'):
        folded = unicodedata.normalize('NFC', part).casefold()
        if any(check(component) for check in (
                snapshots._private_evidence, snapshots._private, snapshots._host_state)
                for component in (part, folded)):
            return True
    return False


def _preflight(project, roots):
    """Prove every candidate safe before deleting any; return paths and bytes.

    A local Git worktree rooted exactly at the workspace is required to prove
    index non-membership; ordinary and linked worktrees are supported. Missing
    roots are harmless, but even missing candidates must be excluded.
    Bounds/refusals cover the entire set, not each root independently.
    """
    workspace = project.workspace
    _directory(workspace)
    git_metadata = workspace / '.git'
    info = git_metadata.lstat()
    # Ordinary worktrees use a directory; linked worktrees use a regular
    # pointer file. Reject links/reparse points and require Git to identify
    # this exact workspace as the top-level before trusting index paths.
    if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise Denied('Workspace Git metadata is not local and regular')
    top = run(['git', '-C', str(workspace), 'rev-parse', '--show-toplevel'],
              timeout=10, maximum=4096, env=git_env())
    if top.exit_code != 0 or top.reason != 'exited':
        raise Denied('Cannot prove workspace Git root')
    try:
        git_root = Path(top.stdout.strip()).resolve(strict=True)
        if git_root != workspace.resolve(strict=True):
            raise Denied('Git root does not match workspace')
    except (OSError, RuntimeError, ValueError) as exc:
        raise Denied('Cannot prove workspace Git root') from exc
    indexed = run(['git', '-c', 'core.fsmonitor=false', '-C', str(workspace),
                   'ls-files', '--cached', '-z'], timeout=10, maximum=GIT_BYTES, env=git_env())
    if indexed.exit_code != 0 or indexed.reason != 'exited':
        raise Denied('Cannot prove cache paths untracked')
    # Conservatively compare aliases on case-insensitive/normalizing hosts.
    def alias(name):
        return unicodedata.normalize('NFC', name).casefold()
    tracked = [alias(name) for name in indexed.stdout.split('\0')]
    deadline = time.monotonic() + SCAN_SECONDS
    count = total = 0
    candidates = []
    for root in roots:
        parts = relative_parts(root)
        if (any(part.casefold() == '.git' for part in parts)
                or not project.snapshots.excluded(root)
                or _snapshot_protected(project.snapshots, root)):
            raise Denied('Cache root is not an eligible snapshot-excluded path')
        key = alias(root)
        if any(name and (name == key or name.startswith(key + '/') or key.startswith(name + '/'))
               for name in tracked):
            raise Denied('Cache root intersects tracked paths')
        path = workspace
        missing = False
        for part in parts:
            path = path / part
            try:
                _directory(path)
            except FileNotFoundError:
                missing = True
                break
        if missing:
            continue
        pending = [(path, 0)]
        while pending:
            directory, depth = pending.pop()
            if depth > MAX_DEPTH or time.monotonic() > deadline:
                raise Denied('Cache scan bound exceeded')
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_ENTRIES or time.monotonic() > deadline:
                        raise Denied('Cache scan bound exceeded')
                    relative = (directory.relative_to(workspace) / entry.name).as_posix()
                    if entry.name.casefold() == '.git':
                        raise Denied('Cache contains a nested repository')
                    if _snapshot_protected(project.snapshots, relative):
                        raise Denied('Cache contains snapshot-protected state')
                    info = entry.stat(follow_symlinks=False)
                    if getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise Denied('Cache contains a reparse point')
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((directory / entry.name, depth + 1))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        total += info.st_size
                    else:
                        raise Denied('Cache contains a link or special file')
        candidates.append(path)
    return candidates, total


def _due(path, instant, interval):
    missing = object()
    record = read_json(path, missing, max_bytes=4096)
    if record is missing:
        return True
    if (not isinstance(record, dict) or type(record.get('checked_at')) is not int
            or not 0 <= record['checked_at'] <= instant):
        raise Denied('Invalid workspace cache maintenance record')
    return instant - record['checked_at'] >= interval


def maintain(project):
    """Attempt one due sweep, or skip. Never wait for an execution to finish.

    The latest bounded result is retained in maintenance/workspace-cache.json.
    Busy acquisition does not consume the cadence. Unsafe/oversized candidates
    consume one interval, without deletion; I/O failures are best-effort skips.
    Call only before acquiring the caller's own role/workspace/check locks.
    """
    policy = project.config.workspace_cache
    if not policy.roots:
        return {'status': 'disabled'}
    state = project.root / 'maintenance/workspace-cache.json'
    instant = int(time.time())
    try:
        if not _due(state, instant, policy.check_interval_seconds):
            return {'status': 'not-due'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(project.root / 'locks/workspace.lock', blocking=False))
            paths = sorted(set(role_exclusions(project.root, tuple(project.config.roles))
                               + check_slot_exclusions(project.root, project.config.limits.parallel_checks)))
            for path in paths:
                stack.enter_context(lock(path, blocking=False))
            # Take this even with cache reuse disabled: a runner admitted
            # under earlier configuration may still hold it.
            stack.enter_context(lock(project.root / 'locks/check-cache.lock', blocking=False))
            if not _due(state, instant, policy.check_interval_seconds):
                return {'status': 'not-due'}
            result = {'checked_at': instant, 'status': 'unsafe', 'removed_roots': 0, 'bytes': 0}
            try:
                candidates, total = _preflight(project, policy.roots)
                result.update(status='below-threshold', bytes=total)
                if total >= policy.max_bytes:
                    result['status'] = 'removed'
                    for path in candidates:
                        shutil.rmtree(path)
                        result['removed_roots'] += 1
            except (Denied, OSError):
                # No candidate is removed on a preflight refusal. Deletion
                # I/O failure can be partial; do not claim full reclamation.
                result['status'] = 'refused'
            write_json(state, result)
            return result
    except Busy:
        return {'status': 'busy'}
    except (Denied, LimitExceeded, OSError, ValueError):
        return {'status': 'unavailable'}
