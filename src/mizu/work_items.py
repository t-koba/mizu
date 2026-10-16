"""Append-audited work-item store with bounded runtime reads; no scheduling.

Seeds are immutable accept/modify decision events or hash-bound legacy sidecars. Reads and transitions share
insights.lock; each successful CAS appends one full state, never a pointer.
Unknown/unsafe records, incomplete chains and bounds fail closed. Retrying a
committed CAS requires a re-read (same-status and terminal changes are refused).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import heapq
import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, PREVIEW_BYTES, canonical, identifier, lock, now, safe_read,
                 sync_dir, text_preview, write_json)
from .insights import _decision_key
from .platform import IS_WINDOWS

MAX_REVISIONS = 256  # Includes the immutable seed at revision 1.
ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
STATUSES = ("open", "doing", "blocked", "done", "dropped")
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}
EVENT_FIELDS = SEED_FIELDS | {"actor", "transition", "reason", "wait", "snapshot_id", "verification"}
MAX_PROJECTION_BYTES = PREVIEW_BYTES + 256
MAX_OPEN_PAGE_ROWS = 100
MAX_OPEN_CURSOR_CHARS = 128
MAX_OPEN_ROW_BYTES = 8192
OPEN_STATUS_ORDER = {"doing": 0, "blocked": 1, "open": 2}
MAX_OPEN_PAGE_BYTES = 1024 * 1024
MAX_HISTORY_PAGE_BYTES = MAX_OPEN_PAGE_BYTES
MAX_HISTORY_TEMPORARIES = 64
_TEMP_NAME = re.compile(r"\.new-[A-Za-z0-9_]{8}\Z")
_REVISION_NAME = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\.json\Z")
_DECISION_EVENT_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
_HISTORY_UNSET = object()
_OPEN_CURSOR_TIME = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")


def _reject_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


def _timestamp(value) -> bool:
    try:
        return isinstance(value, str) and dt.datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _timestamp_order(value: str) -> int:
    """Convert a timezone-aware timestamp to exact UTC microseconds."""
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        delta = parsed - dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)
    except (OverflowError, TypeError, ValueError) as exc:
        raise Denied("Invalid work-item cursor timestamp") from exc
    return ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds


def _open_cursor_key(cursor: str | None):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > MAX_OPEN_CURSOR_CHARS:
        raise Denied("Invalid work-item cursor")
    parts = cursor.split("|")
    if (len(parts) != 4 or parts[0] != "wi1" or parts[1] not in OPEN_STATUS_ORDER
            or _OPEN_CURSOR_TIME.fullmatch(parts[2]) is None or ITEM_ID.fullmatch(parts[3]) is None):
        raise Denied("Invalid work-item cursor")
    return (OPEN_STATUS_ORDER[parts[1]], int(parts[2]), int(parts[3][3:], 16))


def _open_cursor_token(row: dict) -> str:
    created = _timestamp_order(row["created_at"])
    token = f"wi1|{row['status']}|{created}|{row['id']}"
    if len(token) > MAX_OPEN_CURSOR_CHARS:
        raise LimitExceeded("Work-item cursor exceeds character bound")
    return token


class _ReverseKey:
    """Heap wrapper that keeps the worst (largest) natural key at the root."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return isinstance(other, _ReverseKey) and self.value == other.value


def _entry_stat(entry):
    """Get non-following metadata, including inode/link fields on Windows."""
    if IS_WINDOWS:
        # Windows DirEntry.stat() deliberately returns zero for st_dev/ino/nlink.
        return os.stat(entry.path, follow_symlinks=False)
    return entry.stat(follow_symlinks=False)


def _directory(path: Path) -> bool:
    """Screen store-owned directories before enumeration or append."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise Denied("Unsafe work-item directory")
    return True


class WorkItems:
    def __init__(self, root: Path, snapshots, insights):
        self.root, self.snapshots, self.insights = root, snapshots, insights

    def _read(self, name: str) -> dict:
        try:
            value = json.loads(safe_read(self.root, name, PREVIEW_BYTES),
                               parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError) as exc:
            raise Denied("Malformed work-item record") from exc
        if not isinstance(value, dict):
            raise Denied("Malformed work-item record")
        return value

    @staticmethod
    def _legacy_source(event: dict) -> dict:
        """Only valid origin fields are exposed; incomplete legacy events may drop."""
        source = {"insight_id": None, "rev": None, "created_at": None}
        try:
            source["insight_id"] = identifier(event.get("id"))
        except Denied:
            pass
        rev = event.get("rev")
        if type(rev) is int and rev >= 1:
            source["rev"] = rev
        if _timestamp(event.get("created_at")):
            source["created_at"] = event["created_at"]
        return source

    @staticmethod
    def _legacy_seed(event_id: str, event: dict, title) -> dict:
        source = WorkItems._legacy_source(event)
        if (any(value is None for value in source.values())
                or not isinstance(title, str) or not 1 <= len(title) <= 200):
            raise Denied("Opening legacy work requires valid origin and a 1-200 character title")
        return {"id": f"wi_{event_id}", "title": title,
                "origin": {"insight_id": source["insight_id"], "rev": source["rev"]},
                "status": "open", "rev": 1, "holder": None,
                "created_at": source["created_at"], "updated_at": source["created_at"]}

    def _legacy_resolution(self, event_id: str, event: dict, *, recover=False,
                           history_revisions=_HISTORY_UNSET) -> dict | None:
        """Validate the immutable sidecar against the exact canonical source."""
        intent = event.get("work_item_intent")
        if "work_item_intent" in event and (
                event.get("action") not in ("accept", "modify")
                or intent not in ("create", "none")
                or (intent == "create") != ("work_item" in event)):
            raise Denied("Invalid decision work-item intent or seed")
        if intent == "none" and any(value is None for value in self._legacy_source(event).values()):
            raise Denied("Explicit no-work decision requires valid origin binding")
        item_id = f"wi_{event_id}"
        if recover:
            revisions = self._recover_temporary_writes(item_id)
        else:
            revisions = history_revisions
        directory = self.root / "work-items" / item_id
        if revisions is None:
            return None
        if revisions is _HISTORY_UNSET:
            if not _directory(directory.parent) or not _directory(directory):
                return None
        path = directory / "legacy.json"
        try:
            path.lstat()
        except FileNotFoundError:
            if revisions is not _HISTORY_UNSET and revisions and "work_item" not in event:
                raise Denied("Unresolved legacy event carries active history")
            return None
        if intent == "none":
            raise Denied("Explicit no-work decision cannot carry legacy reconciliation")
        record = self._read(f"work-items/{item_id}/legacy.json")
        fields = {"event_id", "event_sha256", "action", "revision", "outcome",
                  "actor", "reason", "created_at"}
        if record.get("outcome") == "open":
            fields.add("work_item")
        if (set(record) != fields or record.get("event_id") != event_id
                or record.get("event_sha256") != hashlib.sha256(canonical(event)).hexdigest()
                or event.get("action") not in ("accept", "modify") or "work_item" in event
                or record.get("action") != event["action"]
                or record.get("revision") != self._legacy_source(event)["rev"]
                or (record.get("revision") is not None and type(record["revision"]) is not int)
                or record.get("outcome") not in ("open", "drop")
                or not isinstance(record.get("reason"), str) or not record["reason"].strip()
                or len(record["reason"]) > 4000 or not _timestamp(record.get("created_at"))):
            raise Denied("Invalid legacy reconciliation binding or fields")
        identifier(record["actor"])
        if record["outcome"] == "open":
            seed = record["work_item"]
            if (not isinstance(seed, dict)
                    or seed != self._legacy_seed(event_id, event, seed.get("title"))
                    or type(seed.get("rev")) is not int
                    or type(seed.get("origin", {}).get("rev")) is not int):
                raise Denied("Invalid legacy reconciliation seed")
        elif revisions is not _HISTORY_UNSET and revisions:
            raise Denied("Dropped legacy resolution carries active history")
        return record

    def _audit_drop_only(self, item_id: str) -> bool:
        """Recognize a valid legacy drop sidecar without recovery or writes.

        Only a directory containing exactly one ordinary ``legacy.json`` can
        be covered here. The shared resolution validator binds that bounded,
        non-hardlinked sidecar to its immutable decision event. Open records
        deliberately fall through to the regular seed/history reader.
        """
        match = ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None:
            return False
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return False
        try:
            (directory / "legacy.json").lstat()
        except FileNotFoundError:
            return False
        with os.scandir(directory) as entries:
            first = next(entries, None)
            if first is None or first.name != "legacy.json":
                return False
            info = _entry_stat(first)
            if (not stat.S_ISREG(info.st_mode) or first.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                return False
            if next(entries, None) is not None:
                return False

        event_id = match[1]
        event = self._read(f"decision-history/{event_id}.json")
        resolution = self._legacy_resolution(event_id, event, history_revisions=[])
        return resolution is not None and resolution["outcome"] == "drop"

    def reconcile(self, event_id, *, expected_action, outcome, reason, actor,
                  expected_rev=None, title=None) -> dict:
        """Resolve exactly one unseeded event, exclusively and retry-idempotently."""
        if (not isinstance(event_id, str) or len(event_id) != 32
                or _DECISION_EVENT_NAME.fullmatch(event_id + ".json") is None):
            raise Denied("Invalid legacy event ID")
        if (expected_action not in ("accept", "modify") or outcome not in ("open", "drop")
                or (expected_rev is not None and (type(expected_rev) is not int or expected_rev < 1))
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                or (outcome == "drop" and title is not None)):
            raise Denied("Invalid legacy reconciliation request")
        identifier(actor)
        with lock(self.root / "locks" / "insights.lock"):
            self._recover_decision_history_temporaries(self.root / "decision-history")
            event = self._read(f"decision-history/{event_id}.json")
            if (event.get("action") != expected_action or "work_item" in event
                    or "work_item_intent" in event
                    or (expected_rev is not None
                        and (type(event.get("rev")) is not int or event["rev"] != expected_rev))):
                raise Denied("Legacy decision changed, already seeded, or has explicit work-item intent")
            existing = self._legacy_resolution(event_id, event, recover=True)
            record = {"event_id": event_id,
                      "event_sha256": hashlib.sha256(canonical(event)).hexdigest(),
                      "action": expected_action, "revision": self._legacy_source(event)["rev"],
                      "outcome": outcome, "actor": actor, "reason": reason,
                      "created_at": existing["created_at"] if existing else now()}
            if outcome == "open":
                record["work_item"] = self._legacy_seed(event_id, event, title)
            if existing is not None:
                if record != existing:
                    raise Denied("Conflicting legacy reconciliation retry")
                return {"record": existing, "created": False}
            if len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Legacy reconciliation exceeds byte bound")
            write_json(self.root / "work-items" / f"wi_{event_id}" / "legacy.json",
                       record, exclusive=True)
            return {"record": record, "created": True}

    def page_legacy(self, *, cursor=None, limit=25) -> dict:
        """Live audit inventory, ordered by exclusive event ID, O(limit) rows.

        No proposal bodies. Unresolved, reconciled and explicit no-work events are included.
        Recognized atomic-write leftovers may be recovered under insights.lock.
        """
        if (cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_OPEN_CURSOR_CHARS
                or re.fullmatch(r"wl1\|[0-9a-f]{32}", cursor) is None)):
            raise Denied("Invalid legacy cursor")
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Legacy page limit must be an integer in [1, 100]")
        after = cursor[4:] if cursor else None
        with lock(self.root / "locks" / "insights.lock"):
            selected, total, eligible, unresolved = [], 0, 0, 0
            for event_id, event in self._decision_events():
                if event["action"] not in ("accept", "modify") or "work_item" in event:
                    self._legacy_resolution(event_id, event, recover=True)
                    if "work_item" in event:
                        self._seed(f"wi_{event_id}", event)
                    continue
                resolution = self._legacy_resolution(event_id, event, recover=True)
                total += 1
                no_work = event.get("work_item_intent") == "none"
                unresolved += resolution is None and not no_work
                if after is not None and event_id <= after:
                    continue
                eligible += 1
                # Bound row selection before constructing previews.
                if len(selected) >= limit and event_id >= selected[0][1]:
                    continue
                source = self._legacy_source(event)
                reason = event.get("reason")
                preview, clipped = text_preview(reason if isinstance(reason, str) else "", 1024)
                row = {"event_id": event_id, "action": event["action"], **source,
                       "reason_preview": preview, "reason_truncated": clipped,
                       "openable": resolution is None and not no_work and all(value is not None for value in source.values()),
                       "resolution": {"outcome": "none"} if no_work else None}
                if no_work:
                    row["work_item_intent"] = "none"
                if resolution is not None:
                    preview, clipped = text_preview(resolution["reason"], 1024)
                    row["resolution"] = {key: resolution[key] for key in
                                         ("outcome", "actor", "created_at", "event_sha256")}
                    row["resolution"].update(reason_preview=preview, reason_truncated=clipped)
                    if resolution["outcome"] == "open":
                        row["resolution"]["work_item_id"] = f"wi_{event_id}"
                if len(canonical(row)) > MAX_OPEN_ROW_BYTES:
                    raise LimitExceeded("Legacy page row exceeds byte bound")
                candidate = (_ReverseKey(event_id), event_id, row)
                if len(selected) < limit:
                    heapq.heappush(selected, candidate)
                else:
                    heapq.heapreplace(selected, candidate)
            rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
            overflow = max(0, eligible - len(rows))
            result = {"items": rows, "total": total, "cursor": cursor,
                      "next_cursor": f"wl1|{rows[-1]['event_id']}" if overflow and rows else None,
                      "overflow_count": overflow, "truncated": overflow > 0,
                      "unseeded_decisions": unresolved, "coverage_complete": unresolved == 0}
            if len(canonical(result)) > MAX_OPEN_PAGE_BYTES:
                raise LimitExceeded("Legacy page exceeds byte bound")
            return result

    def _decision_events(self):
        history = self.root / "decision-history"
        if not self._recover_decision_history_temporaries(history):
            return
        with os.scandir(history) as entries:
            for entry in entries:
                if _DECISION_EVENT_NAME.fullmatch(entry.name) is None:
                    raise Denied("Invalid committed decision-history filename")
                event = self._read(f"decision-history/{entry.name}")
                if event.get("action") not in ("accept", "modify", "defer", "reject", "withdraw"):
                    raise Denied("Malformed committed decision-history action")
                if event["action"] not in ("accept", "modify") and "work_item" in event:
                    raise Denied("Non-accept decision carries a work-item seed")
                yield entry.name[:-5], event

    def _seed(self, item_id: str, event: dict | None = None, *,
              history_revisions=_HISTORY_UNSET) -> dict:
        """Validate a seed, optionally reusing its already safe-read event."""
        match = ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None:
            raise Denied("Invalid work-item ID")
        if event is None:
            event = self._read(f"decision-history/{match[1]}.json")
        if not isinstance(event, dict):
            raise Denied("Invalid work-item seed event")
        resolution = self._legacy_resolution(match[1], event, history_revisions=history_revisions)
        if event.get("work_item_intent") == "none":
            raise Denied("Explicit no-work decision has no work item")
        if "work_item" not in event:
            if resolution is None or resolution["outcome"] != "open":
                raise Denied("Work item has no open legacy resolution")
            event = {**event, "work_item": resolution["work_item"]}
        seed = event.get("work_item")
        if (event.get("action") not in ("accept", "modify")
                or type(event.get("rev")) is not int or event["rev"] < 1
                or not _timestamp(event.get("created_at"))
                or not isinstance(seed, dict) or set(seed) != SEED_FIELDS):
            raise Denied("Invalid work-item seed event")
        identifier(event.get("id"))
        if (seed["id"] != item_id
                or not isinstance(seed["title"], str) or not 1 <= len(seed["title"]) <= 200
                or seed["origin"] != {"insight_id": event["id"], "rev": event["rev"]}
                or not isinstance(seed["origin"], dict) or type(seed["origin"].get("rev")) is not int
                or type(seed["rev"]) is not int or seed["rev"] != 1
                or seed["status"] != "open" or seed["holder"] is not None
                or seed["created_at"] != event["created_at"] or seed["updated_at"] != event["created_at"]):
            raise Denied("Work-item seed does not match decision event")
        return seed

    def _optional_record(self, name: str) -> dict | None:
        """Read one optional bounded record, refusing unsafe store paths."""
        parts = name.split("/")
        if (len(parts) != 2 or parts[0] not in ("inbox", "decisions")
                or not parts[1].endswith(".json")):
            raise Denied("Invalid work-item origin record path")
        identifier(parts[1][:-5])
        if not _directory(self.root / parts[0]):
            return None
        path = self.root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400):
            raise Denied("Unsafe work-item origin record")
        return self._read(name)

    def _origin_revision(self, insight_id: str) -> int | None:
        current = self._optional_record(f"inbox/{insight_id}.json")
        if current is None:
            return None
        revision = current.get("rev", 1)
        if (current.get("id") != insight_id or type(revision) is not int or revision < 1):
            raise Denied("Invalid current work-item origin insight")
        return revision

    def _current_decision(self, insight_id: str, *, include_work_item: bool = False) -> dict | None:
        decision = self._optional_record(f"decisions/{insight_id}.json")
        if decision is None:
            return None
        action = decision.get("action")
        fields = {"id", "action", "reason", "revisit", "run", "created_at", "rev"}
        if action in ("accept", "modify"):
            if "work_item_intent" in decision:
                fields.add("work_item_intent")
            if "work_item" in decision:
                fields.add("work_item")
        elif action == "withdraw":
            fields.add("actor")
        elif action not in ("defer", "reject"):
            raise Denied("Invalid current work-item origin decision")
        revision = decision.get("rev")
        if "decision_key" in decision:
            fields.add("decision_key")
            if action == "withdraw" or decision["decision_key"] != _decision_key(insight_id, revision):
                raise Denied("Invalid current decision key")
        if (set(decision) != fields or decision.get("id") != insight_id
                or type(revision) is not int or revision < 1
                or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()
                or not isinstance(decision.get("revisit"), str)
                or (action == "defer" and not decision["revisit"].strip())
                or (action == "withdraw" and decision["revisit"] != "")
                or (action == "withdraw" and decision.get("run") is not None
                    and (not isinstance(decision["run"], str) or not decision["run"]))
                or (action != "withdraw"
                    and (not isinstance(decision.get("run"), str) or not decision["run"]))
                or not _timestamp(decision.get("created_at"))):
            raise Denied("Malformed current work-item origin decision")
        work_item_id = None
        if action == "withdraw":
            identifier(decision["actor"])
        elif action in ("accept", "modify") and "work_item" in decision:
            seed = decision["work_item"]
            if (not isinstance(seed, dict) or not isinstance(seed.get("id"), str)
                    or ITEM_ID.fullmatch(seed["id"]) is None):
                raise Denied("Malformed current work-item origin decision seed")
            seed_id = seed["id"]
            event = self._read(f"decision-history/{seed_id[3:]}.json")
            if event != decision:
                raise Denied("Current work-item origin decision differs from its immutable event")
            self._seed(seed_id)  # Validate the immutable seed's complete field/value contract.
            work_item_id = seed_id
        elif action in ("accept", "modify"):
            # Legacy current-decision files have no event ID. Resolve the exact
            # old-schema record against one immutable history event rather than
            # trusting a well-shaped mutable pointer by itself.
            match = None
            for event_id, event in self._decision_events():
                if event == decision:
                    if match is not None:
                        raise Denied("Ambiguous current legacy decision history")
                    match = (event_id, event)
            if match is None:
                raise Denied("Current legacy decision has no immutable history event")
            resolution = self._legacy_resolution(*match, recover=True)
            if resolution is not None and resolution["outcome"] == "open":
                work_item_id = f"wi_{match[0]}"
        result = {"rev": revision, "action": action}
        if include_work_item:
            result["work_item_id"] = work_item_id
        return result

    def _recover_temporary_writes(self, item_id: str) -> set[int] | None:
        """Recover atomic-write leftovers and return validated revision names.

        A regular orphan temporary is uncommitted. On POSIX, a crash after
        link() can leave a .new-* alias beside its committed revision; remove
        it only when exactly one regular revision name identifies the same
        inode. The returned set lets the locked reader reuse this bounded scan.
        """
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return None
        temporary_entries = []
        revision_inodes = []
        revisions = set()
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_REVISIONS + 1:
                    raise Denied("Work-item directory exceeds recovery bound")
                if _TEMP_NAME.fullmatch(entry.name):
                    temporary_entries.append(entry)
                    continue
                if entry.name == "legacy.json":
                    info = _entry_stat(entry)
                    if (not stat.S_ISREG(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise Denied("Unsafe legacy reconciliation")
                    revision_inodes.append((info.st_dev, info.st_ino, entry))
                    continue
                if len(revisions) >= MAX_REVISIONS - 1:
                    raise Denied("Work-item history exceeds revision bound")
                if _REVISION_NAME.fullmatch(entry.name) is None:
                    raise Denied("Invalid work-item history entry")
                revision = int(entry.name[:-5])
                if revision > MAX_REVISIONS:
                    raise Denied("Work-item history exceeds revision bound")
                revisions.add(revision)
                try:
                    info = _entry_stat(entry)
                except OSError as exc:
                    raise Denied("Unstable work-item revision entry") from exc
                if (stat.S_ISREG(info.st_mode)
                        and not getattr(info, "st_file_attributes", 0) & 0x400):
                    revision_inodes.append((info.st_dev, info.st_ino, entry))
        removed = False
        for entry in temporary_entries:
            try:
                info = _entry_stat(entry)
            except OSError as exc:
                raise Denied("Unsafe work-item temporary") from exc
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise Denied("Unsafe work-item temporary")
            if info.st_nlink == 1:
                pass  # An uncommitted temporary; never promote it to a revision.
            elif info.st_nlink == 2:
                matches = [(dev, ino, revision) for dev, ino, revision in revision_inodes
                           if dev == info.st_dev and ino == info.st_ino]
                if len(matches) != 1:
                    raise Denied("Unrecognized work-item temporary hard link")
            else:
                raise Denied("Unrecognized work-item temporary link count")
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            removed = True
        if removed:
            sync_dir(directory)
        return revisions

    def _stored_wait(self, value) -> dict:
        """Validate normalized waits without rebasing stored code-change evidence."""
        if not isinstance(value, dict):
            raise Denied("Blocked work items require a structured wait")
        kind = value.get("kind")
        if kind == "code_change":
            baseline = value.get("digest")
            if set(value) != {"kind", "digest"} or not isinstance(baseline, str) or not DIGEST.fullmatch(baseline):
                raise Denied("Invalid stored code-change wait")
            return value
        raw = {"kind": kind, "insight": value.get("target")} if kind == "insight_decided" else value
        normalized = self.insights._normalize_wait(raw, "defer")
        if value != normalized:
            raise Denied("Invalid normalized work-item wait")
        return normalized

    def _validate_transition(self, previous: dict, record: dict) -> None:
        if set(record) != EVENT_FIELDS:
            raise Denied("Invalid work-item transition fields")
        actor = identifier(record["actor"])
        status = record["status"]
        if (status not in STATUSES or previous["status"] in ("done", "dropped")
                or status == previous["status"]):
            raise Denied("Invalid work-item status transition")
        holder = actor if status == "doing" else (previous["holder"] or actor) if status == "blocked" else None
        if (any(record[key] != previous[key] for key in ("id", "title", "origin", "created_at"))
                or not isinstance(record["origin"], dict) or type(record["origin"].get("rev")) is not int
                or type(record["rev"]) is not int or record["rev"] != previous["rev"] + 1
                or record["rev"] > MAX_REVISIONS or record["holder"] != holder
                or not _timestamp(record["updated_at"])
                or record["transition"] != {"from": previous["status"], "to": status}):
            raise Denied("Invalid work-item revision chain")
        reason = record["reason"]
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise Denied("Work-item reason must be nonempty text")
        if status == "dropped" and reason is None:
            raise Denied("Dropping a work item requires a reason")
        if status == "blocked":
            if record["wait"] is not None:
                self._stored_wait(record["wait"])
        elif record["wait"] is not None:
            raise Denied("Only blocked work items carry waits")
        if status == "done":
            sid, verification = record["snapshot_id"], record["verification"]
            if (not isinstance(sid, str) or not DIGEST.fullmatch(sid)
                    or not isinstance(verification, dict) or verification.get("passed") is not True
                    or not isinstance(verification.get("code_digest"), str)
                    or not DIGEST.fullmatch(verification["code_digest"])):
                raise Denied("Completion requires verified snapshot evidence")
            snapshot = self.snapshots.get(sid)
            stored_verification = snapshot.get("verification")
            if (snapshot.get("id") != sid or not isinstance(stored_verification, dict)
                    or stored_verification != verification
                    or stored_verification.get("passed") is not True
                    or stored_verification.get("code_digest") != snapshot.get("code_digest")):
                raise Denied("Completion evidence does not match its verified snapshot")
        elif record["snapshot_id"] is not None or record["verification"] is not None:
            raise Denied("Only completed work items carry snapshot evidence")

    def _iter_chain(self, item_id: str, *, seed_event: dict | None = None,
                    history_revisions=_HISTORY_UNSET):
        """Replay and validate one append-only chain, yielding one record at a time.

        The seed and each revision are immutable full-state records. Callers may
        retain only the current record or a bounded page; replay itself never
        materializes the complete history.
        """
        current = self._seed(item_id, seed_event, history_revisions=history_revisions)
        yield current
        # The storage auditor calls this read-only without recovery. Keep its
        # bounded fail-closed scan; locked readers pass the recovery scan result.
        if history_revisions is _HISTORY_UNSET:
            directory = self.root / "work-items" / item_id
            if not _directory(directory.parent) or not _directory(directory):
                return
            revisions = set()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name == "legacy.json":
                        continue
                    if len(revisions) >= MAX_REVISIONS - 1:
                        raise Denied("Work-item history exceeds revision bound")
                    if _REVISION_NAME.fullmatch(entry.name) is None:
                        raise Denied("Invalid work-item history entry")
                    revision = int(entry.name[:-5])
                    if revision > MAX_REVISIONS:
                        raise Denied("Work-item history exceeds revision bound")
                    revisions.add(revision)
        elif history_revisions is None:
            return
        else:
            revisions = history_revisions
        if revisions != set(range(2, len(revisions) + 2)):
            raise Denied("Gapped work-item history")
        for revision in range(2, len(revisions) + 2):
            record = self._read(f"work-items/{item_id}/{revision}.json")
            self._validate_transition(current, record)
            current = record
            yield current

    def _get(self, item_id: str, *, seed_event: dict | None = None,
             history_revisions=_HISTORY_UNSET) -> dict:
        current = None
        for current in self._iter_chain(item_id, seed_event=seed_event,
                                        history_revisions=history_revisions):
            pass
        return current

    def get(self, item_id: str) -> dict:
        """Read one seed and at most 255 bounded full-state events under lock.

        No list/index or mutable pointer. Unsafe, unknown, oversized or malformed
        records raise Denied/LimitExceeded; local I/O/lock failures propagate.
        """
        with lock(self.root / "locks" / "insights.lock"):
            history_revisions = self._recover_temporary_writes(item_id)
            return self._get(item_id, history_revisions=history_revisions)

    def _origin_projection(self, item: dict, *, current_decision=_HISTORY_UNSET) -> dict:
        """Project current origin staleness from the immutable item's origin."""
        origin_id = item["origin"]["insight_id"]
        origin_rev = item["origin"]["rev"]
        current_rev = self._origin_revision(origin_id)
        redecision = None
        if current_rev is None:
            stale = True
        elif current_rev < origin_rev:
            raise Denied("Origin insight revision predates its work item")
        elif current_rev == origin_rev:
            stale = False
        else:
            decision = (self._current_decision(origin_id)
                        if current_decision is _HISTORY_UNSET else current_decision)
            if decision is not None and decision["rev"] > current_rev:
                raise Denied("Origin decision revision is ahead of its insight")
            redecision = (decision if decision is not None and decision["rev"] == current_rev
                          and decision["action"] in ("accept", "modify", "defer", "reject")
                          else None)
            stale = redecision is None
        return {"current_origin_rev": current_rev, "stale": stale,
                "redecision": redecision}

    def _projection_unlocked(self, item_id: str, *, seed_event: dict | None = None) -> dict:
        """Build a projection while the caller holds insights.lock."""
        history_revisions = self._recover_temporary_writes(item_id)
        item = self._get(item_id, seed_event=seed_event, history_revisions=history_revisions)
        view = {"item": item, **self._origin_projection(item)}
        if len(canonical(view)) > MAX_PROJECTION_BYTES:
            raise LimitExceeded("Work-item projection exceeds byte bound")
        return view

    def explain(self, item_id: str, *, after_rev=0, limit=25) -> dict:
        """Return current status/origin plus a byte-bounded ascending history page.

        ``after_rev`` is an exclusive revision cursor (0..MAX_REVISIONS); the
        immutable seed is revision 1. The full chain is replayed and validated
        even after the page fills or its response byte budget is reached.
        """
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        if type(after_rev) is not int or not 0 <= after_rev <= MAX_REVISIONS:
            raise Denied(f"Work-item history cursor must be an integer in [0, {MAX_REVISIONS}]")
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item history limit must be an integer in [1, 100]")

        with lock(self.root / "locks" / "insights.lock"):
            history_revisions = self._recover_temporary_writes(item_id)
            history = []
            history_bytes = 0
            stopped_for_bytes = False
            # Reserve the maximum independently bounded current projection and
            # ample fixed envelope space before admitting immutable history rows.
            row_budget = MAX_HISTORY_PAGE_BYTES - MAX_PROJECTION_BYTES - 1024
            current = None
            for record in self._iter_chain(item_id, history_revisions=history_revisions):
                current = record
                revision = record["rev"]
                if revision <= after_rev or len(history) >= limit or stopped_for_bytes:
                    continue
                row_size = len(canonical(record))
                added = row_size + (1 if history else 0)
                if history_bytes + added > row_budget:
                    # Keep replaying to validate the entire chain and find the
                    # live current item; later rows cannot form a contiguous page.
                    stopped_for_bytes = True
                    continue
                history.append(record)
                history_bytes += added

            if after_rev > current["rev"]:
                raise Denied("Work-item history cursor is ahead of current revision")
            projection = {"item": current, **self._origin_projection(current)}
            if len(canonical(projection)) > MAX_PROJECTION_BYTES:
                raise LimitExceeded("Work-item projection exceeds byte bound")
            history_total = current["rev"]
            last_returned = history[-1]["rev"] if history else after_rev
            more = last_returned < history_total
            result = {**projection, "history": history, "history_total": history_total,
                      "cursor": after_rev, "next_cursor": last_returned if more else None,
                      "truncated": more}
            if len(canonical(result)) > MAX_HISTORY_PAGE_BYTES:
                raise LimitExceeded("Work-item history response exceeds byte bound")
            return result

    def submitter_status(self, insight_id: str) -> dict | None:
        """Return one compact status for an insight's current accepted work.

        This is a read projection, not a transition. Current decisions and
        legacy resolutions use the same fail-closed validation as origin
        projections; seeded item state replays its bounded immutable chain.
        Unresolved and drop-only legacy decisions have no item status.
        """
        with lock(self.root / "locks" / "insights.lock"):
            decision = self._current_decision(insight_id, include_work_item=True)
            if (decision is None or decision["action"] not in ("accept", "modify")
                    or decision["work_item_id"] is None):
                return None
            item_id = decision["work_item_id"]
            history_revisions = self._recover_temporary_writes(item_id)
            item = self._get(item_id, history_revisions=history_revisions)
            origin_decision = {"rev": decision["rev"], "action": decision["action"]}
            origin = self._origin_projection(item, current_decision=origin_decision)
            status = {"id": item["id"], "status": item["status"], "rev": item["rev"],
                      "holder": item["holder"], "updated_at": item["updated_at"],
                      "current_origin_rev": origin["current_origin_rev"],
                      "stale": origin["stale"]}
            reason = item.get("reason")
            if reason is not None:
                preview, clipped = text_preview(reason, 512)
                status["reason_preview"] = preview
                status["reason_truncated"] = clipped
            if item["status"] == "blocked":
                status["wait"] = item["wait"]
            elif item["status"] == "done":
                status["snapshot_id"] = item["snapshot_id"]
            if len(canonical(status)) > 4096:
                raise LimitExceeded("Submitter work-item status exceeds byte bound")
            return status

    def projection(self, item_id: str) -> dict:
        """Return one bounded item plus revision-derived origin staleness.

        The immutable item remains the CAS authority. A newer origin revision
        stays stale until a current-revision accept/modify/defer/reject decision
        exists; author withdrawal is not a substantive re-decision. A missing
        origin is stale, never silently treated as current. This is an internal
        read projection, not prompt/tool/CLI/status exposure.
        """
        with lock(self.root / "locks" / "insights.lock"):
            return self._projection_unlocked(item_id)

    def _recover_decision_history_temporaries(self, history: Path) -> bool:
        """Remove safe atomic-write leftovers while insights.lock is held.

        A lone regular temporary is uncommitted. A post-link alias is removed
        only when exactly one regular event filename identifies the same inode.
        Recovery state is bounded independently of the append-only history.
        """
        if not _directory(history):
            return False
        temporary_info = []
        aliases = {}
        with os.scandir(history) as entries:
            for entry in entries:
                if not _TEMP_NAME.fullmatch(entry.name):
                    continue
                if len(temporary_info) >= MAX_HISTORY_TEMPORARIES:
                    raise Denied("Too many decision-history temporaries")
                try:
                    info = _entry_stat(entry)
                except OSError as exc:
                    raise Denied("Unstable decision-history temporary") from exc
                if (not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise Denied("Unsafe decision-history temporary")
                if (info.st_nlink not in (1, 2)
                        or (info.st_nlink == 2 and info.st_ino <= 0)):
                    raise Denied("Unrecognized decision-history temporary link count")
                temporary_info.append((entry.path, info))
                if info.st_nlink == 2:
                    key = (info.st_dev, info.st_ino)
                    aliases.setdefault(key, []).append(entry.path)

        if aliases:
            matches = {key: [] for key in aliases}
            with os.scandir(history) as entries:
                for entry in entries:
                    if _DECISION_EVENT_NAME.fullmatch(entry.name) is None:
                        continue
                    try:
                        info = _entry_stat(entry)
                    except OSError as exc:
                        raise Denied("Unstable decision-history entry") from exc
                    key = (info.st_dev, info.st_ino)
                    if key not in matches:
                        continue
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 2
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise Denied("Unsafe decision-history alias target")
                    matches[key].append(entry.name)
                    if len(matches[key]) > 1:
                        raise Denied("Ambiguous decision-history alias target")
            if any(len(paths) != 1 or len(matches[key]) != 1
                   for key, paths in aliases.items()):
                raise Denied("Unrecognized decision-history temporary hard link")

        removed = False
        for path, _ in temporary_info:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            removed = True
        if removed:
            sync_dir(history)
        return True

    def page_open(self, *, cursor=None, limit=25) -> dict:
        """Return a bounded live page of seeded, currently open work items.

        Schema: items ordered by status (doing, blocked, open), then oldest
        ``created_at`` and ID; includes total seeded-open count, the exclusive
        input ``cursor``, ``next_cursor`` iff another matching item follows,
        ``overflow_count`` (matching rows after the cursor beyond this page),
        ``truncated`` (exactly ``overflow_count > 0``), ``unseeded_decisions``
        and ``coverage_complete``.
        Limits are 1..100 (exact int); the opaque cursor carries the last row's
        status, UTC creation-order key and ID. Rows are at most 8192 canonical
        bytes and the complete response at most 1 MiB. Selection keeps O(limit)
        rows while scanning history. Unresolved legacy accept/modify events are
        counted, never guessed as open; hash-bound sidecars explicitly resolve them.
        The page serves the runtime prompt projection and the explicitly granted ``work_items_read`` operation.
        It does not transition items; it may remove recognized decision-history
        and per-item atomic-write leftovers during recovery under the shared
        lock. Recovery removes recognized regular orphan temporaries without
        promotion; post-link aliases require exactly
        one matching event inode.
        Cursors page a live view: changes between calls can move rows; a last-key
        cursor avoids offset shifts on earlier closes but does not freeze
        membership. Unsafe/malformed committed JSON, seeds or state fail closed;
        unmatched/unsafe temporaries and unexpected names fail closed.
        """
        cursor_key = _open_cursor_key(cursor)
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")

        with lock(self.root / "locks" / "insights.lock"):
            selected = []
            total = 0
            eligible = 0
            unseeded_decisions = 0
            for event_id, event in self._decision_events():
                action = event["action"]
                has_seed = "work_item" in event
                if action not in ("accept", "modify"):
                    self._legacy_resolution(event_id, event, recover=True)
                    continue
                if not has_seed:
                    resolution = self._legacy_resolution(event_id, event, recover=True)
                    if event.get("work_item_intent") == "none":
                        continue
                    if resolution is None:
                        unseeded_decisions += 1
                        continue
                    if resolution["outcome"] == "drop":
                        continue
                    seed = resolution["work_item"]
                else:
                    seed = event["work_item"]
                if (not isinstance(seed, dict) or seed.get("id") != f"wi_{event_id}"):
                    raise Denied("Malformed committed work-item seed")
                # Reuse this safely read event; _seed still validates its complete contract.
                view = self._projection_unlocked(seed["id"], seed_event=event)
                item = view["item"]
                if item["status"] not in ("open", "doing", "blocked"):
                    continue
                total += 1
                sort_key = (OPEN_STATUS_ORDER[item["status"]],
                            _timestamp_order(item["created_at"]),
                            int(item["id"][3:], 16))
                if cursor_key is not None and sort_key <= cursor_key:
                    continue
                eligible += 1
                row = {key: item[key] for key in
                       ("id", "title", "origin", "status", "rev", "holder",
                        "created_at", "updated_at")}
                row.update({key: view[key] for key in
                            ("current_origin_rev", "stale", "redecision")})
                if item["status"] == "blocked":
                    row["wait"] = item["wait"]
                reason = item.get("reason")
                if reason is not None:
                    preview, clipped = text_preview(reason, 1024)
                    row["reason_preview"] = preview
                    row["reason_truncated"] = clipped
                row_size = len(canonical(row))
                if row_size > MAX_OPEN_ROW_BYTES:
                    raise LimitExceeded("Work-item page row exceeds byte bound")
                candidate = (_ReverseKey(sort_key), sort_key, row)
                if len(selected) < limit:
                    heapq.heappush(selected, candidate)
                elif sort_key < selected[0][1]:
                    heapq.heapreplace(selected, candidate)

            rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
            overflow_count = max(0, eligible - len(rows))
            more = overflow_count > 0
            result = {"items": rows, "total": total, "cursor": cursor,
                      "next_cursor": _open_cursor_token(rows[-1]) if more and rows else None,
                      "overflow_count": overflow_count, "truncated": more,
                      "unseeded_decisions": unseeded_decisions,
                      "coverage_complete": unseeded_decisions == 0}
            if len(canonical(result)) > MAX_OPEN_PAGE_BYTES:
                raise LimitExceeded("Work-item page exceeds byte bound")
            return result

    def transition(self, item_id: str, *, expected_rev, status, actor, reason=None,
                   wait=None, snapshot_id=None) -> dict:
        """Append one immutable full-state CAS event; no scheduler/wake effects.

        Actor is trusted caller attribution, not authorization. Done validates an
        exact retained published snapshot and its embedded stored verification;
        that evidence is copied so later history retention cannot erase it.
        All validation precedes the exclusive atomic write. Stale CAS writes
        nothing; cancellation/process death releases the shared project lock.
        """
        if type(expected_rev) is not int or expected_rev < 1:
            raise Denied("Expected revision must be a positive integer")
        with lock(self.root / "locks" / "insights.lock"):
            history_revisions = self._recover_temporary_writes(item_id)
            current = self._get(item_id, history_revisions=history_revisions)
            if current["rev"] != expected_rev:
                raise Denied("Work item changed; re-read before transitioning")
            registered = None
            if wait is not None:
                if status != "blocked":
                    raise Denied("Only blocked work items carry waits")
                registered = self.insights._normalize_wait(wait, "defer")
            verification = None
            if snapshot_id is not None:
                if status != "done":
                    raise Denied("Only completed work items carry snapshot evidence")
                # get(None) is a moving-head convenience, never completion proof.
                snapshot = self.snapshots.get(snapshot_id)
                if snapshot_id not in self.snapshots._history_ids():
                    raise Denied("Completion snapshot must be retained published history")
                verification = snapshot.get("verification")
                if (not isinstance(verification, dict) or verification.get("passed") is not True
                        or verification.get("code_digest") != snapshot["code_digest"]):
                    raise Denied("Completion snapshot must have passing digest-bound verification")
            record = {**current, "rev": current["rev"] + 1, "status": status,
                      "updated_at": now(), "actor": actor,
                      "transition": {"from": current["status"], "to": status},
                      "holder": actor if status == "doing" else
                          (current["holder"] or actor) if status == "blocked" else None,
                      "reason": reason, "wait": registered, "snapshot_id": snapshot_id,
                      "verification": verification}
            self._validate_transition(current, record)
            if len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Work-item event exceeds byte bound")
            write_json(self.root / "work-items" / item_id / f"{record['rev']}.json", record, exclusive=True)
            return record
