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

from .delegation_jobs import DelegationJobs
from .admission import check_slot_exclusions, role_exclusions
from .errors import Busy, Denied, LimitExceeded
from .fs import lock, read_json, relative_parts, write_json
from .process import run
from .project import git_env


class _Refused(Denied):
    """A fixed, path-free preflight reason safe to retain."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _directory(path):
    info = path.lstat()
    # Windows junctions/reparse points are links for this purpose too.
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise _Refused('link')
    if not stat.S_ISDIR(info.st_mode):
        raise _Refused('not-directory')


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


def _preflight(project, policy):
    """Prove every candidate safe before deleting any; return paths and bytes.

    A local Git worktree rooted exactly at the workspace is required to prove
    index non-membership; ordinary and linked worktrees are supported. Missing
    roots are harmless, but even missing candidates must be excluded.
    Entry/time budgets and refusals cover the entire set; depth is per root.
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
        raise _Refused('git-metadata')
    top = run(['git', '-C', str(workspace), 'rev-parse', '--show-toplevel'],
              timeout=policy.git_timeout_seconds, maximum=4096, env=git_env())
    if top.exit_code != 0 or top.reason != 'exited':
        raise _Refused('git-root')
    try:
        git_root = Path(top.stdout.strip()).resolve(strict=True)
        if git_root != workspace.resolve(strict=True):
            raise _Refused('git-root')
    except (OSError, RuntimeError, ValueError) as exc:
        raise _Refused('git-root') from exc
    indexed = run(['git', '-c', 'core.fsmonitor=false', '-C', str(workspace),
                   'ls-files', '--cached', '-z'], timeout=policy.git_timeout_seconds,
                  maximum=policy.git_output_bytes, env=git_env())
    if indexed.exit_code != 0 or indexed.reason != 'exited':
        raise _Refused('git-index')
    # Conservatively compare aliases on case-insensitive/normalizing hosts.
    def alias(name):
        return unicodedata.normalize('NFC', name).casefold()
    tracked = [alias(name) for name in indexed.stdout.split('\0')]
    deadline = time.monotonic() + policy.scan_seconds
    count = total = 0
    candidates = []
    for root in policy.roots:
        parts = relative_parts(root)
        if (any(part.casefold() == '.git' for part in parts)
                or not project.snapshots.excluded(root)
                or _snapshot_protected(project.snapshots, root)):
            raise _Refused('ineligible-root')
        key = alias(root)
        if any(name and (name == key or name.startswith(key + '/') or key.startswith(name + '/'))
               for name in tracked):
            raise _Refused('tracked-path')
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
            if depth > policy.max_depth or time.monotonic() > deadline:
                raise _Refused('scan-bound')
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > policy.max_entries or time.monotonic() > deadline:
                        raise _Refused('scan-bound')
                    relative = (directory.relative_to(workspace) / entry.name).as_posix()
                    if entry.name.casefold() == '.git':
                        raise _Refused('nested-repository')
                    if _snapshot_protected(project.snapshots, relative):
                        raise _Refused('protected-path')
                    # Inspect metadata without following links/reparse points.
                    info = (directory / entry.name).lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise _Refused('link')
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((directory / entry.name, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    else:
                        raise _Refused('nonregular-file')
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
            DelegationJobs.guard(project.root)
            paths = sorted(set(role_exclusions(project.root, tuple(project.config.roles))
                               + check_slot_exclusions(project.root, project.config.limits.parallel_checks)))
            for path in paths:
                stack.enter_context(lock(path, blocking=False))
            # Take this even with cache reuse disabled: a runner admitted
            # under earlier configuration may still hold it.
            stack.enter_context(lock(project.root / 'locks/check-cache.lock', blocking=False))
            if not _due(state, instant, policy.check_interval_seconds):
                return {'status': 'not-due'}
            from . import safe_point
            if safe_point.read(project.snapshots) is not None:
                return {'status': 'fenced'}
            result = {'checked_at': instant, 'status': 'unsafe', 'removed_roots': 0, 'bytes': 0}
            try:
                candidates, total = _preflight(project, policy)
                result.update(status='below-threshold', bytes=total)
                if total >= policy.max_bytes:
                    result['status'] = 'removed'
                    for path in candidates:
                        shutil.rmtree(path)
                        result['removed_roots'] += 1
            except (Denied, OSError) as exc:
                # No candidate is removed on a preflight refusal. Deletion
                # I/O failure can be partial; do not claim full reclamation.
                reason = (exc.reason if isinstance(exc, _Refused) else
                          'deletion-failed' if result['status'] == 'removed' else 'preflight-failed')
                result.update(status='refused', reason=reason)
            write_json(state, result)
            return result
    except Busy:
        return {'status': 'busy'}
    except (Denied, LimitExceeded, OSError, ValueError):
        return {'status': 'unavailable'}
