"""Bounded local observer-cursor reads and wake-generation comparison.

Runtime re-exports these helpers to preserve its established call seam.
"""
from __future__ import annotations

from .errors import Busy, Denied, LimitExceeded
from .fs import digest, read_json


#: Bound for role cursors: snapshot/code, wake generation, and (for read-only
#: completion) state digest, outcome and validated compact progress evidence.
OBSERVED_CURSOR_MAX_BYTES = 4096


def _normalized_wake_generation(value) -> str:
    """Fail closed on malformed local control generations without crashing a run."""
    return value if isinstance(value, str) else ""


def _wake_generation_digest(value) -> str:
    """Bound observer cursor state to a valid generation or the empty sentinel."""
    return digest(_normalized_wake_generation(value).encode())


def _role_cursor(project, role_name) -> dict:
    """Read bounded local observation evidence; unsafe/malformed cursors close."""
    try:
        cursor = read_json(project.root / "observed" / f"{role_name}.json", {},
                           max_bytes=OBSERVED_CURSOR_MAX_BYTES)
        return cursor if isinstance(cursor, dict) else {}
    except (OSError, ValueError, Busy, Denied, LimitExceeded):
        return {}


def _operator_woke(control, cursor, *, observer=False) -> bool:
    raw_generation = control.get("wake_generation", "")
    if not isinstance(raw_generation, str):
        return False
    generation = raw_generation
    if observer:
        # A digest keeps the observer cursor bounded without narrowing the
        # publisher's existing control-generation contract. Malformed local
        # control state is the empty sentinel, never a wake or a crash.
        return (_wake_generation_digest(generation) !=
                cursor.get("wake_generation_digest", _wake_generation_digest("")))
    seen = _normalized_wake_generation(cursor.get("wake_generation", ""))
    return generation != seen
