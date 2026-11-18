"""Revisioned objects of declared kinds, with requirements and events.

Layout under the project root:
  objects/<kind>/<id>/<rev:08d>.json   create-only revisions; the highest is current
  objects/intent.json                  the one in-flight write (crash recovery)
  timers/<kind>.<id>.<field>.json      pending instants that emit an event when due
  locks/kernel.lock                    serialises object writes and event appends

A write validates fully, records an intent, publishes the revision, appends its
event, schedules instants and clears the intent, all under the kernel lock. A
process that finds an intent on acquiring the lock completes it (appending the
event only if the revision exists and the event does not), so an event is never
lost or duplicated. Reads take no lock. Listing scans one kind directory and is
bounded per page; an index is a later optimisation, not a semantic change.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import secrets
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..errors import Denied
from ..fs import atomic_write, canonical, loads_finite, lock, mkdir, now, safe_read
from .events import EventLog
from .kinds import PRINCIPAL, Kind

OBJECT_BYTES = 1024 * 1024 + 4096
REVISION_MAX = 10 ** 8 - 1
PAGE_MAX = 512
_ID = re.compile(r"[0-9a-f]{32}\Z")


class Objects:
    def __init__(self, root: Path, kinds: Mapping[str, Kind], events: EventLog):
        self.root, self.kinds, self.events = Path(root), dict(kinds), events

    # -- helpers ----------------------------------------------------------------------------
    def _kind(self, name: str) -> Kind:
        kind = self.kinds.get(name)
        if kind is None:
            raise Denied(f"Undeclared kind {name!r}")
        return kind

    @staticmethod
    def _actor(actor: str) -> str:
        if not isinstance(actor, str) or not PRINCIPAL.fullmatch(actor):
            raise Denied("Invalid actor principal")
        return actor

    def _dir(self, kind: str, object_id: str) -> Path:
        if not isinstance(object_id, str) or not _ID.fullmatch(object_id):
            raise Denied("Invalid object id")
        return self.root / "objects" / kind / object_id

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        mkdir(self.root / "locks")
        with lock(self.root / "locks" / "kernel.lock"):
            self._recover()
            yield

    # -- reads ------------------------------------------------------------------------------
    def revisions(self, kind: str, object_id: str) -> list[int]:
        self._kind(kind)
        try:
            names = os.listdir(self._dir(kind, object_id))
        except FileNotFoundError:
            return []
        return sorted(int(n[:-5]) for n in names if re.fullmatch(r"\d{8}\.json", n))

    def read(self, kind: str, object_id: str, rev: int | None = None) -> dict:
        revs = self.revisions(kind, object_id)
        if not revs:
            raise Denied(f"No {kind} object {object_id}")
        rev = revs[-1] if rev is None else rev
        if rev not in revs:
            raise Denied(f"{kind} {object_id} has no revision {rev}")
        return loads_finite(safe_read(self.root, f"objects/{kind}/{object_id}/{rev:08d}.json", OBJECT_BYTES))

    def list(self, kind: str, *, state: str | None = None, where: Mapping[str, Any] | None = None,
             limit: int = 64, after: str | None = None) -> dict:
        """Current objects of a kind ordered by id, filtered, one bounded page."""
        self._kind(kind)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Denied(f"limit must be from 1 to {PAGE_MAX}")
        try:
            ids = sorted(n for n in os.listdir(self.root / "objects" / kind) if _ID.fullmatch(n))
        except FileNotFoundError:
            ids = []
        rows, last = [], None
        for object_id in ids:
            if after is not None and object_id <= after:
                continue
            last = object_id
            current = self.read(kind, object_id)
            if state is not None and current["state"] != state:
                continue
            if where and any(current["fields"].get(k) != v for k, v in where.items()):
                continue
            rows.append(current)
            if len(rows) == limit:
                break
        more = last is not None and last != (ids[-1] if ids else None)
        return {"objects": rows, "next": last if more else None}

    # -- writes -----------------------------------------------------------------------------
    def create(self, kind_name: str, fields: Mapping[str, Any], actor: str) -> dict:
        kind, actor = self._kind(kind_name), self._actor(actor)
        if actor not in kind.create:
            raise Denied(f"{actor} may not create {kind_name}")
        values = self._fields(kind, fields, allowed=set(kind.fields))
        missing = [name for name, field in kind.fields.items() if field.required and name not in values]
        if missing:
            raise Denied(f"Missing required fields: {', '.join(missing)}")
        object_id = secrets.token_hex(16)
        stamp = now()
        record = {"kind": kind_name, "id": object_id, "rev": 1, "state": kind.initial,
                  "fields": values, "author": actor, "actor": actor, "transition": None,
                  "created_at": stamp, "updated_at": stamp}
        with self._locked():
            self._publish(kind, record, event=kind.created_event, previous_state=None)
        return record

    def transition(self, kind_name: str, object_id: str, name: str, *, expected_rev: int,
                   fields: Mapping[str, Any] | None, actor: str, snapshot: str | None = None) -> dict:
        kind, actor = self._kind(kind_name), self._actor(actor)
        transition = kind.transitions.get(name)
        if transition is None:
            raise Denied(f"{kind_name} has no transition {name!r}")
        with self._locked():
            current = self.read(kind_name, object_id)
            if type(expected_rev) is not int or current["rev"] != expected_rev:
                raise Denied("Object changed; re-read before transitioning")
            if current["rev"] >= REVISION_MAX:
                raise Denied("Object reached its revision bound")
            if not transition.leaves(current["state"]):
                raise Denied(f"Transition {name} does not leave state {current['state']}")
            if not self._permitted(transition.by, actor, current):
                raise Denied(f"{actor} may not make transition {name}")
            values = {**current["fields"], **self._fields(kind, fields or {}, allowed=set(transition.fields))}
            proposed = {**current, "fields": values, "state": transition.to}
            for requirement in transition.requires:
                if not self._holds(kind, requirement, proposed, snapshot):
                    raise Denied(f"Requirement not met: {requirement.describe()}")
            record = {**proposed, "rev": current["rev"] + 1, "actor": actor, "transition": name,
                      "updated_at": now()}
            self._publish(kind, record, event=transition.emits, previous_state=current["state"])
        return record

    # -- timers -----------------------------------------------------------------------------
    def next_instant(self) -> str | None:
        """The earliest pending instant, for the scheduler's single timed wake."""
        times = [row["at"] for row in self._timers()]
        return min(times, key=lambda t: dt.datetime.fromisoformat(t)) if times else None

    def fire_due(self, at: dt.datetime | None = None) -> list[dict]:
        """Append the events of every instant that has passed; each fires once."""
        at = at or dt.datetime.now(dt.timezone.utc)
        fired = []
        with self._locked():
            for row in self._timers():
                if dt.datetime.fromisoformat(row["at"]) <= at:
                    fired.append(self.events.append(row["event"], {
                        "kind": row["kind"], "id": row["id"], "field": row["field"], "due": row["at"]}, "host"))
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(row["path"])
        return fired

    def _timers(self) -> list[dict]:
        directory = self.root / "timers"
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        rows = []
        for name in names:
            if name.endswith(".json"):
                row = loads_finite(safe_read(self.root, f"timers/{name}", 4096))
                rows.append({**row, "path": directory / name})
        return rows

    # -- internals ----------------------------------------------------------------------------
    @staticmethod
    def _fields(kind: Kind, fields: Mapping[str, Any], *, allowed: set) -> dict:
        if not isinstance(fields, Mapping):
            raise Denied("fields must be an object")
        unknown = set(fields) - allowed
        if unknown:
            raise Denied(f"Fields not settable here: {', '.join(sorted(unknown))}")
        return {name: kind.fields[name].validate(value) for name, value in fields.items()}

    @staticmethod
    def _permitted(by: tuple[str, ...], actor: str, current: Mapping[str, Any]) -> bool:
        for principal in by:
            if principal == "author" and actor == current["author"]:
                return True
            if principal == "holder" and actor == current["fields"].get("holder"):
                return True
            if principal == actor:
                return True
        return False

    def _holds(self, kind: Kind, requirement, proposed: Mapping[str, Any], snapshot: str | None) -> bool:
        values = proposed["fields"]
        if requirement.field is not None:
            return requirement.field in values
        target_id = values.get(requirement.ref)
        if target_id is None:
            return False
        targets = kind.fields[requirement.ref].kinds or tuple(self.kinds)
        for target_kind in targets:
            if self.revisions(target_kind, target_id):
                target = self.read(target_kind, target_id)
                if requirement.state and target["state"] not in requirement.state:
                    return False
                for key, want in requirement.where:
                    if want == "$snapshot":
                        want = snapshot
                    elif isinstance(want, str) and want.startswith("$field."):
                        want = values.get(want[7:])
                    if want is None or target["fields"].get(key) != want:
                        return False
                return True
        return False

    def _publish(self, kind: Kind, record: dict, *, event: str, previous_state: str | None) -> None:
        data = canonical(record)
        if len(data) > OBJECT_BYTES:
            raise Denied("Object exceeds its size bound")
        intent = {"kind": record["kind"], "id": record["id"], "rev": record["rev"], "event": event,
                  "from": previous_state, "to": record["state"], "actor": record["actor"]}
        atomic_write(self.root / "objects" / "intent.json", canonical(intent))
        directory = self._dir(record["kind"], record["id"])
        mkdir(directory)
        atomic_write(directory / f"{record['rev']:08d}.json", data, exclusive=True)
        self._complete(intent, kind, record)

    def _complete(self, intent: Mapping[str, Any], kind: Kind, record: Mapping[str, Any], *,
                  append: bool = True) -> None:
        if append:
            body = {k: intent[k] for k in ("kind", "id", "rev", "from", "to")}
            self.events.append(intent["event"], body, intent["actor"])
        for field, at, event in kind.instant_events(record["fields"]):
            path = self.root / "timers" / f"{record['kind']}.{record['id']}.{field}.json"
            mkdir(path.parent)
            atomic_write(path, canonical({"kind": record["kind"], "id": record["id"], "field": field,
                                          "at": at, "event": event}))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.root / "objects" / "intent.json")

    def _recover(self) -> None:
        if not (self.root / "objects" / "intent.json").exists():
            return
        intent = loads_finite(safe_read(self.root, "objects/intent.json", 4096))
        kind = self.kinds.get(intent.get("kind"))
        if kind is None or intent.get("rev") not in self.revisions(intent["kind"], intent["id"]):
            os.unlink(self.root / "objects" / "intent.json")  # the revision never landed: nothing happened
            return
        head = self.events.head()
        if head:
            last = self.events.read(head)
            if (last.get("kind"), last.get("id"), last.get("rev")) == (intent["kind"], intent["id"], intent["rev"]):
                # The event landed; finish the idempotent remainder (timers, intent removal).
                self._complete(intent, kind, self.read(intent["kind"], intent["id"], intent["rev"]), append=False)
                return
        self._complete(intent, kind, self.read(intent["kind"], intent["id"], intent["rev"]))
