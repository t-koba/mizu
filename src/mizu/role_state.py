"""Role-owned current research state: bounded replacement, never a log.

One JSON record per role holds the role's *current* coverage: what it
has investigated, what it concluded, what stays unknown, and when to
revisit. Updates replace the whole record under a generation
compare-and-swap; history lives only in run records, never here, and
insight submission is never a research-memory store.
"""
from __future__ import annotations

import math
from pathlib import Path

from .errors import Busy, Denied
from .fs import canonical, identifier, lock, now, read_json, write_json


#: Distinct read outcomes: ``absent`` (never written), ``unavailable``
#: (unreadable or malformed: fail closed, preserve for manual recovery),
#: ``current`` (a usable record). Absence of state never reads as
#: "nothing researched": callers report the status, not an empty summary.
READ_STATUSES = frozenset({"absent", "unavailable", "current"})

#: Maximum nesting depth of a stored state object.
MAX_DEPTH = 4

#: Maximum key length inside a stored state object.
MAX_KEY_CHARS = 64

#: Maximum length of the run token naming a replacement's author.
MAX_RUN_CHARS = 128


def _check_state(state) -> dict:
    """Validate a replacement state object's shape; ``Denied`` never touches stored state."""
    if not isinstance(state, dict):
        raise Denied("Role research state must be a JSON object")
    def depth(value, level: int) -> None:
        if level > MAX_DEPTH:
            raise Denied("Role research state is nested too deeply")
        if isinstance(value, dict):
            for key, item in value.items():
                if (not isinstance(key, str) or not key or len(key) > MAX_KEY_CHARS
                        or "\n" in key or "\x00" in key):
                    raise Denied("Invalid role research state key")
                depth(item, level + 1)
        elif isinstance(value, list):
            for item in value:
                depth(item, level + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise Denied("Role research state holds only finite numbers")
        elif not isinstance(value, (str, int, bool)) and value is not None:
            raise Denied("Role research state holds only JSON values")
    depth(state, 1)
    return state


def _check_run(run):
    """Validate the authoring run token: ``None`` or a short text token."""
    if run is None:
        return None
    if (not isinstance(run, str) or not run or len(run) > MAX_RUN_CHARS
            or "\n" in run or "\x00" in run):
        raise Denied("Invalid role research state run")
    return run


class RoleStateStore:
    """Bounded per-role current-state records under one directory."""

    def __init__(self, root: Path, *, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes

    def _path(self, role: str) -> Path:
        return self.root / f"{identifier(role)}.json"

    def read(self, role: str) -> dict:
        """Return the current record plus its read status.

        Schema: ``{"status", "generation", "record"}`` where ``record``
        is the stored object (with ``state``) on ``current``, else
        ``None``. Absent files read ``absent`` with generation 0;
        unreadable or malformed files read ``unavailable`` with
        generation ``None``: callers must not CAS against them, and a
        later ``replace`` refuses until an operator clears the file.
        Failure: ``Denied`` only on a bad role name.
        """
        missing = object()
        try:
            record = read_json(self._path(role), missing)
        except (OSError, ValueError):
            return {"status": "unavailable", "generation": None, "record": None}
        if record is missing:
            return {"status": "absent", "generation": 0, "record": None}
        if (not isinstance(record, dict) or record.get("role") != role
                or not isinstance(record.get("generation"), int)
                or record["generation"] < 1
                or not isinstance(record.get("state"), dict)):
            return {"status": "unavailable", "generation": None, "record": None}
        return {"status": "current", "generation": record["generation"],
                "record": record}

    def replace(self, role: str, state: dict, *, expected_generation: int,
                run=None) -> dict:
        """Replace the current record iff ``expected_generation`` is current.

        Schema: ``state`` is the full new state object (validated before
        any read of stored generations, so invalid input never disturbs
        stored state); ``expected_generation`` is the generation the
        caller read (0 when absent, an exact ``int``: booleans refuse).
        The read-compare-write runs under a per-role non-blocking lock,
        so overlapping writers never share a generation: the loser is
        ``Denied`` (stale or busy) and re-reads instead of overwriting
        newer evidence. Corrupt stored files refuse every replacement
        until cleared. Writes are atomic, so an interrupted update
        leaves the previous record intact; the byte bound covers the
        full stored record (envelope plus authoring run token), not just
        the state object. Evidence: returned ``{"role", "generation",
        "updated_at"}`` names the new generation. Failure: ``Denied``
        on bad roles, invalid states, bad runs, oversize records, stale
        expectations, contention, or unavailable stored files.
        """
        _check_state(state)
        run = _check_run(run)
        if type(expected_generation) is not int or expected_generation < 0:
            raise Denied("Role research state replacement needs a generation")
        try:
            with lock(self.root / f"{identifier(role)}.lock", blocking=False):
                seen = self.read(role)
                if seen["status"] == "unavailable":
                    raise Denied("Role research state is unavailable; "
                                 "clear it before replacing")
                if seen["generation"] != expected_generation:
                    raise Denied("Stale role research state; re-read before replacing")
                record = {"role": role, "generation": expected_generation + 1,
                          "updated_at": now(), "updated_by_run": run,
                          "state": state}
                if len(canonical(record)) > self.max_bytes:
                    raise Denied("Role research state exceeds the configured byte bound")
                write_json(self._path(role), record)
        except Busy as exc:
            raise Denied("Concurrent role research state update; "
                         "re-read before replacing") from exc
        return {"role": role, "generation": record["generation"],
                "updated_at": record["updated_at"]}
