"""Cheap witness of the project state tree for the resident daemon.

Mizu publishes project state by atomic replace (``fs.atomic_write`` and the
rename-based writers; the explicit ``os.utime`` calls on ``work-items`` and
``check-jobs`` bump the same signal), so any state change alters the identity
(inode, device, ``st_mtime_ns``) of the directory that contains it. The witness
records those identities for every directory below the project root, plus the
identity and size of top-level regular files. It lists directories but never
opens a file, so its cost is one ``scandir`` and one ``stat`` per directory.

Trees that are large and never readiness inputs are skipped (``SKIPPED_TREES``):
``workspace`` (role working copies), ``runs`` and ``sessions`` (per-run records
written after dispatch), ``objects`` (content-addressed payloads), ``check-cache``,
``spool`` (editor spool, ingested into ``inbox`` before readiness is evaluated)
and ``locks`` (rewritten in place by design, no state). Everything else is
included, because ``should_run`` reads it or could.

The witness is evidence of change, not a proof of absence: a reader must still
re-evaluate periodically for time-dependent or out-of-tree inputs, and must not
trust a witness whose newest identity is within the filesystem timestamp
granularity of the moment it was taken (see ``settled``).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

#: Top-level trees that readiness never reads and that dominate the entry count.
SKIPPED_TREES = frozenset({
    "workspace", "runs", "objects", "sessions", "locks", "check-cache", "spool"})

#: Coarse kernel timestamps can leave a later write with an equal ``st_mtime_ns``.
GRANULARITY_NS = 50_000_000


def witness(root: Path) -> tuple[tuple, int, int]:
    """Return ``(identities, newest_mtime_ns, taken_ns)`` for the state tree.

    A missing or unreadable directory contributes a distinguishable marker, so
    its appearance, disappearance or permission change is also a difference.
    """
    taken = time.time_ns()
    rows: list[tuple] = []
    newest = 0
    pending = [("", os.fspath(root))]
    while pending:
        relative, path = pending.pop()
        try:
            info = os.stat(path, follow_symlinks=False)
            rows.append((relative, info.st_dev, info.st_ino, info.st_mtime_ns))
            newest = max(newest, info.st_mtime_ns)
            with os.scandir(path) as entries:
                children = list(entries)
        except OSError as exc:
            rows.append((relative, exc.errno))
            continue
        for entry in children:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not relative and name in SKIPPED_TREES:
                        continue
                    pending.append((f"{relative}/{name}", entry.path))
                elif not relative:
                    info = entry.stat(follow_symlinks=False)
                    rows.append((name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
                    newest = max(newest, info.st_mtime_ns)
            except OSError as exc:
                rows.append((f"{relative}/{name}", exc.errno))
    rows.sort(key=lambda row: row[0])
    return tuple(rows), newest, taken


def settled(newest_mtime_ns: int, taken_ns: int) -> bool:
    """True when nothing was modified within the timestamp granularity.

    Only then can a later change be guaranteed to move some identity.
    """
    return newest_mtime_ns < taken_ns - GRANULARITY_NS
