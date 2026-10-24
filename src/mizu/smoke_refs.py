"""Typed smoke IDs and the shared, short reference/publication critical section.

IDs are scoped by their containing project/owner record; Insight entries also
store the exact revision. History keeps IDs, but only current live owners pin.
"""
from __future__ import annotations

import contextlib
import re
import stat
from pathlib import Path

from .errors import Denied
from .fs import lock

SMOKE_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_SMOKE_REFS = 64


def validate_refs(value) -> list:
    if (not isinstance(value, list) or len(value) > MAX_SMOKE_REFS
            or any(not isinstance(x, str) or not SMOKE_ID.fullmatch(x) for x in value)
            or len(set(value)) != len(value)):
        raise Denied("smoke_refs must be at most 64 unique exact 32-hex smoke IDs")
    return value


@contextlib.contextmanager
def shared_lock(data: Path, *, blocking=False):
    # Refuse linked lock directories/inodes before the generic lock opener.
    for path in (data, data / "locks"):
        path.mkdir(exist_ok=True, mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or getattr(info, "st_reparse_tag", 0):
            raise Denied("Unsafe shared smoke lock directory")
    path = data / "locks" / "smoke-retention.lock"
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise Denied("Unsafe shared smoke lock file")
    with lock(path, blocking=blocking):
        yield


@contextlib.contextmanager
def record_lock(root: Path):
    # Standalone record stores (e.g. import tools/tests) have no shared data
    # owner. Managed project roots always use <data>/projects/<project>.
    shared = shared_lock(root.parent.parent, blocking=True) if root.parent.name == "projects" else contextlib.nullcontext()
    with shared:
        with lock(root / "locks" / "insights.lock"):
            yield


def require_pairs(root: Path, refs) -> None:
    validate_refs(refs)
    if refs:
        if root.parent.name != "projects":
            raise Denied("Smoke references require a managed shared data directory")
        from .smoke_retention import read_pair, Metadata
        metadata = Metadata(root.parent.parent)
        for smoke_id in refs:
            read_pair(metadata, smoke_id)
