"""Install-time conversion of retained insight identities into archive rows."""
from __future__ import annotations

from . import insight_archive
from .errors import Denied, MizuError
from .storage import quiescent


def _error(exc):
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    if isinstance(exc, (ValueError, TypeError)):
        return "Invalid retained insight identity"
    return str(exc)


def present(project) -> bool:
    """Detect identity-only records with no archive tombstone, refusing bad state."""
    try:
        return bool(insight_archive.identity_conversion_state(project.root)["pending"])
    except (MizuError, OSError) as exc:
        raise Denied(f"Cannot check insight identities: {_error(exc)}") from exc


def plan(project) -> dict:
    """Preview the exact pending identities without writing or taking a lock."""
    try:
        state = insight_archive.identity_conversion_state(project.root)
    except (MizuError, OSError) as exc:
        return dict(ok=False, scan_complete=False, truncated=False, entries_examined=0,
                    candidates=0, converted=0, records=[], conflicts=[],
                    errors=[dict(path="insight-ids", error=_error(exc))])
    return dict(ok=True, scan_complete=True, truncated=False,
                entries_examined=state["entries_examined"], candidates=len(state["pending"]),
                converted=0, records=[dict(id=key) for key in state["pending"]],
                conflicts=[], errors=[])


def apply(project) -> dict:
    """Convert under the same exclusion as archive writes; verify before commit."""
    with quiescent(project):
        return insight_archive.convert_identities(project.root)
