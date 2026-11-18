"""Append-only per-project event log, subscriptions, cursors and wake files.

Layout under the project root:
  events/<seq:020d>.json   one create-only event per sequence number
  events/head.json         the last appended sequence (derived; repaired from files)
  events/cursors/<role>.json  the role's last acknowledged sequence
  wake/<role>              touched when an appended event matches the role

Callers append while holding the kernel lock (``Objects`` does). A fully
constructed event can also be published at a reserved sequence, which is
idempotent for an identical retry and refuses conflicting content. Reads take
no lock: an event file is complete or absent, and a reader never sees a
sequence beyond the head it read. Bounds: page sizes are caller-supplied and
capped.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import Denied
from ..fs import atomic_write, canonical, identifier, loads_finite, mkdir, now, safe_read
from .kinds import EVENT

EVENT_BYTES = 64 * 1024
PAGE_MAX = 512


def matches(pattern: str, name: str) -> bool:
    """``*`` matches one dotted component; terminal ``**`` any non-empty tail.

    A non-terminal ``**`` never matches: validation refuses it, and this
    matcher also fails closed for a pattern written by an older configuration.
    """
    want, have = pattern.split("."), name.split(".")
    for index, part in enumerate(want):
        if part == "**":
            return index == len(want) - 1 and len(have) > index
        if index >= len(have) or (part != "*" and part != have[index]):
            return False
    return len(want) == len(have)


class EventLog:
    def __init__(self, root: Path, subscriptions: Mapping[str, Iterable[str]] | None = None):
        self.root = Path(root)
        self.dir = self.root / "events"
        self.subscriptions = {role: tuple(patterns) for role, patterns in (subscriptions or {}).items()}

    # -- writing (caller holds the kernel lock) -------------------------------------------
    def _stored_head(self) -> int:
        value = None
        if (self.dir / "head.json").exists():
            value = loads_finite(safe_read(self.root, "events/head.json", 64)).get("seq")
        return value if type(value) is int and value >= 0 else 0

    def head(self) -> int:
        seq = self._stored_head()
        # The head is derived: a crash after an event file and before the head moves forward.
        while (self.dir / f"{seq + 1:020d}.json").exists():
            seq += 1
        return seq

    def append(self, name: str, body: Mapping[str, Any], actor: str) -> dict:
        if not isinstance(name, str) or not EVENT.fullmatch(name):
            raise Denied("Invalid event name")
        seq = self.head() + 1
        return self.append_at({**body, "seq": seq, "name": name, "actor": actor, "at": now()})

    def append_at(self, event: Mapping[str, Any]) -> dict:
        """Publish a fully constructed event at its reserved sequence.

        An identical event file at that sequence is a retry: it stays
        untouched, the head advances past it and matching subscribers are
        woken again. Different content at the same sequence fails closed, so
        no event is silently overwritten or appended twice. Callers hold the
        kernel lock.
        """
        if not isinstance(event, Mapping):
            raise Denied("Invalid event")
        seq, name, at = event.get("seq"), event.get("name"), event.get("at")
        if type(seq) is not int or seq < 1:
            raise Denied("Invalid event sequence")
        if not isinstance(name, str) or not EVENT.fullmatch(name):
            raise Denied("Invalid event name")
        if not isinstance(at, str) or not at:
            raise Denied("Invalid event timestamp")
        data = canonical(event)
        if len(data) > EVENT_BYTES:
            raise Denied("Event exceeds its size bound")
        path = self.dir / f"{seq:020d}.json"
        if path.exists():
            if safe_read(self.root, f"events/{path.name}", EVENT_BYTES) != data:
                raise Denied(f"Event sequence {seq} conflicts with existing content")
        else:
            atomic_write(path, data, exclusive=True)
        if seq > self._stored_head():
            mkdir(self.dir)
            atomic_write(self.dir / "head.json", canonical({"seq": seq}))
        self._wake(name)
        return dict(event)

    def _wake(self, name: str) -> None:
        for role, patterns in self.subscriptions.items():
            if any(matches(pattern, name) for pattern in patterns):
                path = self.root / "wake" / identifier(role)
                mkdir(path.parent)
                atomic_write(path, canonical({"at": now()}))

    # -- reading ------------------------------------------------------------------------------
    def read(self, seq: int) -> dict:
        return loads_finite(safe_read(self.root, f"events/{seq:020d}.json", EVENT_BYTES))

    def cursor(self, role: str) -> int:
        try:
            path = f"events/cursors/{identifier(role)}.json"
            if not (self.root / path).exists():
                return 0
            value = loads_finite(safe_read(self.root, path, 64)).get("seq")
        except (OSError, ValueError, AttributeError):
            return 0
        return value if type(value) is int and value >= 0 else 0

    def pending(self, role: str, *, limit: int = 64, after: int | None = None) -> dict:
        """Subscribed events after the role's cursor (or ``after``), oldest first, bounded."""
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Denied(f"limit must be from 1 to {PAGE_MAX}")
        patterns = self.subscriptions.get(role, ())
        start, head, rows = (self.cursor(role) if after is None else after), self.head(), []
        seq = start
        while seq < head and len(rows) < limit:
            seq += 1
            event = self.read(seq)
            if any(matches(pattern, event["name"]) for pattern in patterns):
                rows.append(event)
        return {"events": rows, "next": seq, "head": head, "more": seq < head}

    def ack(self, role: str, seq: int) -> int:
        """Advance the role's cursor monotonically; never past the head."""
        if type(seq) is not int or seq < 0 or seq > self.head():
            raise Denied("Acknowledged sequence is outside the log")
        current = self.cursor(role)
        if seq > current:
            path = self.dir / "cursors" / f"{identifier(role)}.json"
            mkdir(path.parent)
            atomic_write(path, canonical({"seq": seq}))
            return seq
        return current
