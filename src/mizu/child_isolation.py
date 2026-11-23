"""Pure conflict report for isolated child workspaces.

This module is a mechanism, not a runner: it neither creates workspaces nor
starts children. Callers provide exact per-path SHA-256 digests for a base tree
and for the parent and child trees derived from it; absence is ``None``.
The report names every path whose parent and child outcomes differ after both
have moved away from the base. Identical convergent edits are not a conflict.

Every input is validated before comparison. Unknown path spellings, malformed
digests, excessive inventories and non-mapping inputs refuse instead of being
interpreted, so an incomplete observation can never be reported as clean.
"""
from __future__ import annotations

from .errors import Denied, LimitExceeded

MAX_PATHS = 10_000
_HEX = frozenset("0123456789abcdef")


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path or len(path) > 4096:
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _valid_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in _HEX for char in value))


def _checked(tree: object, name: str) -> dict[str, str | None]:
    if not isinstance(tree, dict) or len(tree) > MAX_PATHS:
        raise Denied(f"Child conflict report has an oversized {name} inventory")
    checked = {}
    for path, value in tree.items():
        if not _valid_path(path) or not (value is None or _valid_digest(value)):
            raise Denied(f"Child conflict report has an invalid {name} path or digest")
        checked[path] = value
    return checked


def report_conflicts(base: object, parent: object, child: object) -> list[dict]:
    """Return explicit parent/child conflicts, or ``[]`` only for complete proof.

    A path is changed when its digest differs from the base, including a
    deletion (``None``). Conflict requires both sides to have changed and
    their resulting digests to differ. A path changed on only one side, or
    changed identically on both, is not a conflict. Every input must still be
    a bounded, well-formed inventory; otherwise the call refuses.
    """
    base_tree = _checked(base, "base")
    parent_tree = _checked(parent, "parent")
    child_tree = _checked(child, "child")
    paths = set(base_tree) | set(parent_tree) | set(child_tree)
    if len(paths) > MAX_PATHS:
        raise LimitExceeded("Child conflict report inventory exceeds its bound")
    conflicts = []
    for path in sorted(paths):
        before = base_tree.get(path)
        ours = parent_tree.get(path)
        theirs = child_tree.get(path)
        if ours == before or theirs == before or ours == theirs:
            continue
        conflicts.append({
            'path': path,
            'base': before,
            'parent': ours,
            'child': theirs,
            'kind': _kind(before, ours, theirs),
        })
    return conflicts


def _kind(before, ours, theirs) -> str:
    if before is None:
        return 'both-added-differently'
    if ours is None or theirs is None:
        return 'modify-delete'
    return 'both-modified'
