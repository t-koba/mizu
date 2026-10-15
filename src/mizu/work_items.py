"""Internal append-audited work-item CAS; no scheduling or public interface.

Seeds are immutable accept/modify decision events. Reads and transitions share
insights.lock; each successful CAS appends one full state, never a pointer.
Unknown/unsafe records, incomplete chains and bounds fail closed. Retrying a
committed CAS requires a re-read (same-status and terminal changes are refused).
"""
from __future__ import annotations

import datetime as dt
import heapq
import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, PREVIEW_BYTES, canonical, identifier, lock, now, safe_read,
                 sync_dir, text_preview, write_json)

MAX_REVISIONS = 256  # Includes the immutable seed at revision 1.
ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
STATUSES = ("open", "doing", "blocked", "done", "dropped")
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}
EVENT_FIELDS = SEED_FIELDS | {"actor", "transition", "reason", "wait", "snapshot_id", "verification"}
MAX_PROJECTION_BYTES = PREVIEW_BYTES + 256
MAX_OPEN_PAGE_ROWS = 100
MAX_OPEN_ROW_BYTES = 8192
MAX_OPEN_PAGE_BYTES = 1024 * 1024
MAX_HISTORY_TEMPORARIES = 64
_TEMP_NAME = re.compile(r"\.new-[A-Za-z0-9_]{8}\Z")
_REVISION_NAME = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\.json\Z")
_DECISION_EVENT_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")


def _reject_json_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


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
            value = json.loads(safe_read(self.root, name, PREVIEW_BYTES),
                               parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError) as exc:
            raise Denied("Malformed work-item record") from exc
        if not isinstance(value, dict):
            raise Denied("Malformed work-item record")
        return value

    def _seed(self, item_id: str, event: dict | None = None) -> dict:
        """Validate a seed, optionally reusing its already safe-read event."""
        match = ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None:
            raise Denied("Invalid work-item ID")
        if event is None:
            event = self._read(f"decision-history/{match[1]}.json")
        if not isinstance(event, dict):
            raise Denied("Invalid work-item seed event")
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
        action = decision.get("action")
        fields = {"id", "action", "reason", "revisit", "run", "created_at", "rev"}
        if action in ("accept", "modify"):
            fields.add("work_item")
        elif action == "withdraw":
            fields.add("actor")
        elif action not in ("defer", "reject"):
            raise Denied("Invalid current work-item origin decision")
        revision = decision.get("rev")
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
        if action == "withdraw":
            identifier(decision["actor"])
        elif action in ("accept", "modify"):
            seed = decision["work_item"]
            if (not isinstance(seed, dict) or not isinstance(seed.get("id"), str)
                    or ITEM_ID.fullmatch(seed["id"]) is None):
                raise Denied("Malformed current work-item origin decision seed")
            seed_id = seed["id"]
            event = self._read(f"decision-history/{seed_id[3:]}.json")
            if event != decision:
                raise Denied("Current work-item origin decision differs from its immutable event")
            self._seed(seed_id)  # Validate the immutable seed's complete field/value contract.
        return {"rev": revision, "action": action}

    def _recover_temporary_writes(self, item_id: str) -> bool:
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
            return False
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
        return True

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

    def _get(self, item_id: str, *, seed_event: dict | None = None,
             history_exists: bool | None = None) -> dict:
        current = self._seed(item_id, seed_event)  # Validate the seed; never scan history/inbox.
        # Reuse the immediate under-lock recovery result instead of restatting these paths.
        if history_exists is False:
            return current
        directory = self.root / "work-items" / item_id
        if history_exists is None and (not _directory(directory.parent) or not _directory(directory)):
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
            history_exists = self._recover_temporary_writes(item_id)
            return self._get(item_id, history_exists=history_exists)

    def _projection_unlocked(self, item_id: str, *, seed_event: dict | None = None) -> dict:
        """Build a projection while the caller holds insights.lock."""
        history_exists = self._recover_temporary_writes(item_id)
        item = self._get(item_id, seed_event=seed_event, history_exists=history_exists)
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
                    info = entry.stat(follow_symlinks=False)
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
                        info = entry.stat(follow_symlinks=False)
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

        Schema: item-ID ordered ``items`` with total seeded-open count, the
        exclusive input ``cursor``, ``next_cursor`` iff another matching item
        follows, ``truncated``, ``unseeded_decisions`` and
        ``coverage_complete``. Limits are 1..100 (exact int); the cursor is
        null or an ITEM_ID. Rows are at most 8192 canonical bytes, and the
        complete response at most 1 MiB. Selection keeps O(limit) rows while
        history is scanned. Legacy accept/modify events lacking a seed are
        counted, never guessed as open. The page is internal and read-only
        apart from recognized decision-history and per-item atomic-write
        recovery under the shared lock. Recovery removes recognized regular
        orphan temporaries without promotion; post-link aliases require exactly
        one matching event inode.
        Cursors page a live view: changes between calls can move rows; a last-key
        cursor avoids offset shifts on earlier closes but does not freeze
        membership. Unsafe/malformed committed JSON, seeds or state fail closed;
        unmatched/unsafe temporaries and unexpected names fail closed.
        """
        if cursor is not None and (not isinstance(cursor, str) or ITEM_ID.fullmatch(cursor) is None):
            raise Denied("Invalid work-item cursor")
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")

        history = self.root / "decision-history"
        with lock(self.root / "locks" / "insights.lock"):
            selected = []
            total = 0
            eligible = 0
            unseeded_decisions = 0
            if self._recover_decision_history_temporaries(history):
                with os.scandir(history) as entries:
                    for entry in entries:
                        if _TEMP_NAME.fullmatch(entry.name):
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except OSError as exc:
                                raise Denied("Unstable decision-history temporary") from exc
                            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                    or getattr(info, "st_file_attributes", 0) & 0x400):
                                raise Denied("Unsafe decision-history temporary")
                            continue  # Never promote uncommitted atomic-write leftovers.
                        if not entry.name.endswith(".json"):
                            raise Denied("Unknown decision-history entry")
                        if _DECISION_EVENT_NAME.fullmatch(entry.name) is None:
                            raise Denied("Invalid committed decision-history filename")
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise Denied("Unstable decision-history entry") from exc
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or getattr(info, "st_file_attributes", 0) & 0x400):
                            raise Denied("Unsafe committed decision-history entry")
                        event = self._read(f"decision-history/{entry.name}")
                        action = event.get("action")
                        event_id = entry.name[:-5]
                        if action not in ("accept", "modify", "defer", "reject", "withdraw"):
                            raise Denied("Malformed committed decision-history action")
                        has_seed = "work_item" in event
                        if action not in ("accept", "modify"):
                            if has_seed:
                                raise Denied("Non-accept decision carries a work-item seed")
                            continue
                        if not has_seed:
                            # Older accepted decisions have no stored lifecycle.
                            # Count them as unknown coverage, even when legacy
                            # fields needed by current seeds are absent.
                            unseeded_decisions += 1
                            continue
                        seed = event["work_item"]
                        if (not isinstance(seed, dict) or seed.get("id") != f"wi_{event_id}"):
                            raise Denied("Malformed committed work-item seed")
                        # Reuse this safely read event; _seed still validates its complete contract.
                        view = self._projection_unlocked(seed["id"], seed_event=event)
                        item = view["item"]
                        if item["status"] not in ("open", "doing", "blocked"):
                            continue
                        total += 1
                        if cursor is not None and item["id"] <= cursor:
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
                        entry_key = -int(item["id"][3:], 16)
                        candidate = (entry_key, item["id"], row)
                        if len(selected) < limit:
                            heapq.heappush(selected, candidate)
                        elif entry_key > selected[0][0]:
                            heapq.heapreplace(selected, candidate)

            rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
            more = eligible > len(rows)
            result = {"items": rows, "total": total, "cursor": cursor,
                      "next_cursor": rows[-1]["id"] if more and rows else None,
                      "truncated": more, "unseeded_decisions": unseeded_decisions,
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
            history_exists = self._recover_temporary_writes(item_id)
            current = self._get(item_id, history_exists=history_exists)
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
