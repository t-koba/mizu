"""Internal append-audited work-item CAS; no scheduling or public interface.

Seeds are immutable accept/modify decision events. Reads and transitions share
insights.lock; each successful CAS appends one full state, never a pointer.
Unknown/unsafe records, incomplete chains and bounds fail closed. Retrying a
committed CAS requires a re-read (same-status and terminal changes are refused).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, PREVIEW_BYTES, canonical, identifier, lock, now, safe_read,
                 sync_dir, write_json)

MAX_REVISIONS = 256  # Includes the immutable seed at revision 1.
ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
STATUSES = ("open", "doing", "blocked", "done", "dropped")
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}
EVENT_FIELDS = SEED_FIELDS | {"actor", "transition", "reason", "wait", "snapshot_id", "verification"}
MAX_PROJECTION_BYTES = PREVIEW_BYTES + 256
_TEMP_NAME = re.compile(r"\.new-[A-Za-z0-9_]{8}\Z")
_REVISION_NAME = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\.json\Z")


def _timestamp(value) -> bool:
    try:
        return isinstance(value, str) and dt.datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


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
            value = json.loads(safe_read(self.root, name, PREVIEW_BYTES))
        except (ValueError, UnicodeError) as exc:
            raise Denied("Malformed work-item record") from exc
        if not isinstance(value, dict):
            raise Denied("Malformed work-item record")
        return value

    def _seed(self, item_id: str) -> dict:
        match = ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None:
            raise Denied("Invalid work-item ID")
        event = self._read(f"decision-history/{match[1]}.json")
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

    def _current_decision(self, insight_id: str) -> dict | None:
        decision = self._optional_record(f"decisions/{insight_id}.json")
        if decision is None:
            return None
        revision = decision.get("rev", 1)
        if (decision.get("id") != insight_id or type(revision) is not int or revision < 1
                or decision.get("action") not in ("accept", "modify", "defer", "reject", "withdraw")):
            raise Denied("Invalid current work-item origin decision")
        return {"rev": revision, "action": decision["action"]}

    def _recover_temporary_writes(self, item_id: str) -> None:
        """Remove only atomic-write leftovers while holding insights.lock.

        A regular orphan temporary is uncommitted and ignored. On POSIX, a
        crash after link() can leave a .new-* hard link beside its committed
        revision; remove that alias only when exactly one valid revision name
        identifies the same inode. Unknown names and unsafe links fail closed.
        """
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return
        temporary_entries = []
        revision_inodes = []
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_REVISIONS:
                    raise Denied("Work-item directory exceeds recovery bound")
                if _TEMP_NAME.fullmatch(entry.name):
                    temporary_entries.append(entry)
                elif _REVISION_NAME.fullmatch(entry.name):
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise Denied("Unstable work-item revision entry") from exc
                    if (int(entry.name[:-5]) <= MAX_REVISIONS
                            and stat.S_ISREG(info.st_mode)
                            and not getattr(info, "st_file_attributes", 0) & 0x400):
                        revision_inodes.append((info.st_dev, info.st_ino, entry))
        removed = False
        for entry in temporary_entries:
            try:
                info = entry.stat(follow_symlinks=False)
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

    def _get(self, item_id: str) -> dict:
        current = self._seed(item_id)  # Direct event path, never scan decision history/inbox.
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return current
        revisions = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(revisions) >= MAX_REVISIONS - 1:
                    raise Denied("Work-item history exceeds revision bound")
                if not re.fullmatch(r"(?:[2-9]|[1-9][0-9]{1,2})\.json", entry.name):
                    raise Denied("Invalid work-item history entry")
                revision = int(entry.name[:-5])
                if revision > MAX_REVISIONS:
                    raise Denied("Work-item history exceeds revision bound")
                revisions.add(revision)
        if revisions != set(range(2, len(revisions) + 2)):
            raise Denied("Gapped work-item history")
        for revision in range(2, len(revisions) + 2):
            record = self._read(f"work-items/{item_id}/{revision}.json")
            self._validate_transition(current, record)
            current = record
        return current

    def get(self, item_id: str) -> dict:
        """Read one seed and at most 255 bounded full-state events under lock.

        No list/index or mutable pointer. Unsafe, unknown, oversized or malformed
        records raise Denied/LimitExceeded; local I/O/lock failures propagate.
        """
        with lock(self.root / "locks" / "insights.lock"):
            self._recover_temporary_writes(item_id)
            return self._get(item_id)

    def projection(self, item_id: str) -> dict:
        """Return one bounded item plus revision-derived origin staleness.

        The immutable item remains the CAS authority. A newer origin revision
        stays stale until a current-revision accept/modify/defer/reject decision
        exists; author withdrawal is not a substantive re-decision. A missing
        origin is stale, never silently treated as current. This is an internal
        read projection, not prompt/tool/CLI/status exposure.
        """
        with lock(self.root / "locks" / "insights.lock"):
            self._recover_temporary_writes(item_id)
            item = self._get(item_id)
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
                decision = self._current_decision(origin_id)
                if decision is not None and decision["rev"] > current_rev:
                    raise Denied("Origin decision revision is ahead of its insight")
                redecision = (decision if decision is not None and decision["rev"] == current_rev
                              and decision["action"] in ("accept", "modify", "defer", "reject")
                              else None)
                stale = redecision is None
            view = {"item": item, "current_origin_rev": current_rev,
                    "stale": stale, "redecision": redecision}
            if len(canonical(view)) > MAX_PROJECTION_BYTES:
                raise LimitExceeded("Work-item projection exceeds byte bound")
            return view

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
            self._recover_temporary_writes(item_id)
            current = self._get(item_id)
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
