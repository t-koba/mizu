"""Append-audited work-item store with bounded indexed reads; no scheduling.

Seeds are immutable creation records in ``work-items/<id>/create.json``: a
``direct`` record created by a role (its ID is bound to the actor and
idempotency key), or an ``imported`` record written by host tooling. Each
successful CAS appends one full state under records.lock, never a pointer,
after replaying and validating the item's whole chain. Current-state reads
come only from the ledger index (:mod:`mizu.ledger`), which every write
updates in its critical section; no read replays history. Full-chain
validation runs at write time, when the index is built from the files, and in
:meth:`WorkItems.audit`. Hold ages and wait liveness are computed from current
records on every read. An item's origin is an opaque reference and is never
interpreted. Retrying a committed CAS requires a re-read. Same-status events
require a scope/reference change, held-open release, or delivery binding; only
a proven forward dropped-to-done correction or explicit audited completion
reopening may follow a terminal event.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import heapq
import json
import os
import re
import stat
from pathlib import Path

from .smoke_refs import record_lock, validate_refs, require_pairs
from . import lock_diagnostics
from . import work_item_retirement
from .work_item_waits import (normalize_wait, stored_wait, liveness, blocked_start,
                              blocked_age, holder_only, ci_wait_identity)
from .work_item_resolution import MAX_RESOLUTION_BYTES
from .config import (DEFAULT_HISTORY_TEMPORARIES, MAX_HISTORY_TEMPORARIES,
                     CompletionEvidence, PublicationEvidence)
from .completion_evidence import (VERIFIED_SNAPSHOT, HOLDER_ATTESTATION, recorded_policy,
                                  validate_attestation)
from .errors import Denied, LimitExceeded
from .fs import (DIGEST, PREVIEW_BYTES, canonical, digest, identifier, lock, now,
                 safe_read, sync_dir, text_preview, write_json)
from .platform import IS_WINDOWS
from .ledger import rows as _rows
from . import check_job_index
from .work_item_index import ITEMS
from .work_item_seed import (DESCRIPTION_UNSET, ITEM_ID, SCOPE_UNSET, SEED_FIELDS, make_seed,
                             validate_origin, validate_scope, validate_seed)

MAX_REVISIONS = 256  # Includes the immutable seed at revision 1.
MAX_REOPEN_REV = 252  # Leaves four appends for reopen/claim/bind/close before 256.
STATUSES = ("open", "doing", "blocked", "done", "dropped")
#: Records written before the delivery claim existed keep the older field
#: set. They stay readable history; a closure written now always carries the
#: claim, and the ledger refuses one that cannot state it.
LEGACY_EVENT_FIELDS = SEED_FIELDS | {"actor", "transition", "reason", "wait", "snapshot_id",
                                    "verification"}
#: Records written before the accepted scope existed carry the delivery claim
#: but no scope. They stay readable history.
DELIVERED_EVENT_FIELDS = LEGACY_EVENT_FIELDS | {"delivered_tree"}
#: Pre-delivery-schema records carry scope but no explicit delivery binding.
SCOPED_EVENT_FIELDS = DELIVERED_EVENT_FIELDS | {"scope"}
#: Every transition written now carries accepted scope and the delivery-schema
#: projection; older shapes remain readable without rewriting their history.
DELIVERY_EVENT_FIELDS = SCOPED_EVENT_FIELDS | {"delivery_schema", "delivery", "verification_scope"}
MAX_EVENT_BYTES = PREVIEW_BYTES + MAX_RESOLUTION_BYTES
MAX_PROJECTION_BYTES = MAX_EVENT_BYTES + 256
MAX_OPEN_PAGE_ROWS = 100
# Prior progress plus the current open page can contribute the dispatch set.
MAX_DELIVERED_WAIT_RESOLUTION_TOKENS = 3 * MAX_OPEN_PAGE_ROWS
MAX_OPEN_CURSOR_CHARS = 128
MAX_OPEN_ROW_BYTES = 8192
OPEN_STATUS_ORDER = {"doing": 0, "blocked": 1, "open": 2}
MAX_OPEN_PAGE_BYTES = 1024 * 1024
MAX_HISTORY_PAGE_BYTES = MAX_OPEN_PAGE_BYTES
MAX_CLOSED_PAGE_ROWS = 100
MAX_CLOSED_ROW_BYTES = MAX_OPEN_ROW_BYTES
MAX_CLOSED_PAGE_BYTES = MAX_OPEN_PAGE_BYTES
_TEMP_NAME = re.compile(r"\.new-[A-Za-z0-9_]{8}\Z")
_REVISION_NAME = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\.json\Z")
_DECISION_EVENT_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
_WAIT_RESOLUTION_TOKEN = re.compile(r"wi_[0-9a-f]{32}:[1-9][0-9]{0,2}\Z")
_HISTORY_UNSET = object()
_OPEN_CURSOR_TIME = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")
_CLOSED_CURSOR_TIME = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")
_CLOSED_STATUSES = frozenset({"done", "dropped"})


def wait_resolution_token(item: dict) -> str:
    """Identify one immutable wait resolution, distinct across item waits."""
    item_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
        raise Denied("Invalid wait-resolution item identity")
    proof = item.get("wait_resolution")
    wait_rev = proof.get("wait_rev") if isinstance(proof, dict) else None
    if type(wait_rev) is not int or not 1 <= wait_rev <= MAX_REVISIONS:
        raise Denied("Invalid wait-resolution revision")
    return f"{item_id}:{wait_rev}"


def wait_resolution_tokens(*pages, role_name: str, retained=(),
                           maximum_tokens=2 * MAX_OPEN_PAGE_ROWS) -> list[str]:
    """Capture shown proofs and still-current delivered proofs within one bound."""
    if (type(maximum_tokens) is not int or not 0 <= maximum_tokens <= 2 * MAX_OPEN_PAGE_ROWS
            or not isinstance(retained, (list, tuple, set, frozenset))
            or len(retained) > maximum_tokens
            or any(not isinstance(token, str)
                   or _WAIT_RESOLUTION_TOKEN.fullmatch(token) is None
                   or not 1 <= int(token.rsplit(":", 1)[1]) <= MAX_REVISIONS
                   for token in retained)
            or len(set(retained)) != len(retained)):
        raise Denied("Invalid retained wait-resolution tokens")
    tokens = set(retained)
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise Denied("Invalid wait-resolution delivery page")
        for item in page["items"]:
            if not isinstance(item, dict):
                raise Denied("Invalid wait-resolution delivery row")
            if item.get("holder") == role_name and "wait_resolution" in item:
                tokens.add(wait_resolution_token(item))
                if len(tokens) > maximum_tokens:
                    raise Denied("Wait-resolution delivery exceeds progress bound")
    return sorted(tokens)


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

def _closed_cursor_key(cursor: str | None):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > MAX_OPEN_CURSOR_CHARS:
        raise Denied("Invalid closed work-item cursor")
    parts = cursor.split("|")
    if (len(parts) != 4 or parts[0] != "wc2" or parts[1] not in _CLOSED_STATUSES
            or _CLOSED_CURSOR_TIME.fullmatch(parts[2]) is None
            or ITEM_ID.fullmatch(parts[3]) is None):
        raise Denied("Invalid closed work-item cursor")
    return (parts[1], int(parts[2]), parts[3])


def _closed_cursor_token(row: dict) -> str:
    updated = _timestamp_order(row["updated_at"])
    token = f"wc2|{row['status']}|{updated}|{row['id']}"
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


def _present(path: Path) -> bool:
    """A directory entry is present even if it is a dangling symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True



class WorkItems:
    def __init__(self, root: Path, snapshots, ledger, *, vcs=None, queues=None, effective_verification=None,
                 doing_limit=1, doing_limits=None, stale_seconds=86400, record_kinds=None,
                 effective_verification_projection=None, writer_roles_lookup=None,
                 event_roles_lookup=None, ci_wait_settings=None, ci_request_roles=None,
                 publication_evidence: PublicationEvidence | None = None,
                 completion_evidence: CompletionEvidence | None = None,
                 history_temporaries: int = DEFAULT_HISTORY_TEMPORARIES):
        self.root, self.snapshots = root, snapshots
        self.writer_roles_lookup = writer_roles_lookup
        #: Configured role names whose pending-event index rows are maintained
        #: on every terminal write; standalone stores have no event roles.
        self.event_roles_lookup = event_roles_lookup
        self.ci_wait_settings = ci_wait_settings
        self.ci_request_roles = ci_request_roles
        self.vcs = vcs or {}
        self.queues = queues or {}
        #: Declared publisher record kinds (publication results of CI waits).
        self.record_kinds = record_kinds or {}
        self.publication_evidence = (publication_evidence if publication_evidence is not None
                                     else PublicationEvidence())
        self.completion_evidence = (completion_evidence if completion_evidence is not None
                                    else CompletionEvidence())
        self.effective_verification = effective_verification
        # Optional proof+diagnostic callback; legacy proof-only callers keep
        # their existing contract. The selected callback runs once per attempt.
        self.effective_verification_projection = effective_verification_projection
        self.doing_limit, self.doing_limits = doing_limit, dict(doing_limits or {})
        self.stale_seconds = stale_seconds
        if (type(history_temporaries) is not int
                or not 1 <= history_temporaries <= MAX_HISTORY_TEMPORARIES):
            raise Denied(f"Work-item history temporary limit must be in [1, {MAX_HISTORY_TEMPORARIES}]")
        self.history_temporaries = history_temporaries
        self._ledger = ledger
        self._ledger.attach(ITEMS, self)
        if any(type(value) is not int or not 1 <= value <= MAX_OPEN_PAGE_ROWS
               for value in (doing_limit, *self.doing_limits.values())):
            raise Denied("Work-item doing limit must be an integer in [1, 100]")
        if type(stale_seconds) is not int or not 1 <= stale_seconds <= 31536000:
            raise Denied("Work-item stale threshold must be an integer in [1, 31536000]")


    def event_roles(self) -> tuple[str, ...]:
        """Project roles known to the event index, read from live settings."""
        return tuple(sorted(set(self.event_roles_lookup() if self.event_roles_lookup else ())))

    def _require_doing_capacity(self, actor: str) -> None:
        """Count the actor's doing items in the index; caller holds records.lock."""
        limit = self.doing_limits.get(actor, self.doing_limit)
        with self._ledger.read("items", locked=True) as db:
            count = _rows(db, "SELECT COUNT(*) FROM items WHERE status='doing' AND holder=?",
                          (actor,))[0][0]
        if count >= limit:
            raise Denied(f"Work-item doing slots full for {actor}: {count}/{limit}; "
                         "release or block held work before claiming another item")

    def _hold_projection(self, since: str | None) -> dict:
        """Visibility only; future starts clamp to zero and cannot be stale."""
        elapsed = None if since is None else _timestamp_order(now()) - _timestamp_order(since)
        return {"doing_since": since,
                "doing_age_seconds": None if elapsed is None else max(0, elapsed) // 1_000_000,
                "doing_stale": elapsed is not None and elapsed >= self.stale_seconds * 1_000_000}

    def _count_open(self, *, locked: bool) -> int:
        with self._ledger.read("items", locked=locked) as db:
            return _rows(db, "SELECT COUNT(*) FROM items WHERE status IN ('open', 'doing', 'blocked')")[0][0]

    def count_open(self) -> int:
        """Count seeded open/doing/blocked work while holding records.lock."""
        return self._count_open(locked=True)

    def count_open_readonly(self) -> int:
        """Count seeded open/doing/blocked work without taking the lock or writing."""
        return self._count_open(locked=False)

    def _read(self, name: str) -> dict:
        event = name.startswith("work-items/") and _REVISION_NAME.fullmatch(Path(name).name)
        raw = safe_read(self.root, name, MAX_EVENT_BYTES if event else PREVIEW_BYTES)
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            if len(raw) > PREVIEW_BYTES:
                raise LimitExceeded("Work-item event exceeds byte bound") from exc
            raise Denied("Malformed work-item record") from exc
        if not isinstance(value, dict):
            raise Denied("Malformed work-item record")
        if event and len(raw) > (MAX_EVENT_BYTES if "wait_resolution" in value else PREVIEW_BYTES):
            raise LimitExceeded("Work-item event exceeds byte bound")
        return value

    @staticmethod
    def _create_id(actor: str, key: str) -> str:
        identifier(actor)
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise Denied("Work-item idempotency key must be 1-128 characters")
        return "wi_" + hashlib.sha256(canonical(["direct-work-item", actor, key])).hexdigest()[:32]

    def _creation_record(self, item_id: str) -> dict | None:
        """Read and validate one item's immutable creation record, if present."""
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return None
        if not _present(directory / "create.json"):
            return None
        record = self._read(f"work-items/{item_id}/create.json")
        if record.get("kind") == "imported":
            if set(record) != {"kind", "work_item"}:
                raise Denied("Invalid imported work-item creation record")
            seed = validate_seed(record["work_item"], imported=True)
            if seed["id"] != item_id:
                raise Denied("Invalid imported work-item binding")
            return record
        if set(record) - {"requested_smoke_refs"} != {"kind", "actor", "idempotency_key", "work_item"} or record["kind"] != "direct":
            raise Denied("Invalid direct work-item creation record")
        seed = validate_seed(record["work_item"])
        if "requested_smoke_refs" in record:
            if record["requested_smoke_refs"] is not None:
                validate_refs(record["requested_smoke_refs"])
            if "smoke_refs" not in seed or not set(record["requested_smoke_refs"] or []).issubset(seed["smoke_refs"]):
                raise Denied("Invalid inherited smoke reference request")
        if self._create_id(record["actor"], record["idempotency_key"]) != item_id or seed["id"] != item_id:
            raise Denied("Invalid direct work-item binding")
        return record

    def _create_seed(self, item_id: str, title: str, origin, stamp: str, *,
                     description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, smoke_refs=None) -> dict:
        """Preflight a new immutable seed while the caller holds records.lock.

        Validate an empty safe storage slot and the optional description and
        scope. Recognized regular orphan temporaries are recovered (removed
        without promotion) before the emptiness check; any numeric revision
        history, creation record, unknown entry, or unsafe/ambiguous temporary
        still fails closed. This does not commit.
        """
        if smoke_refs is not None:
            validate_refs(smoke_refs)
        seed = make_seed(item_id, title, origin, stamp, description=description, scope=scope,
                         smoke_refs=smoke_refs)
        if smoke_refs is not None:
            require_pairs(self.root, smoke_refs)
        directory = self.root / "work-items" / item_id
        if _directory(directory.parent) and _directory(directory):
            revisions = self._history_revisions(item_id, recover=True)
            if revisions or _present(directory / "create.json"):
                raise Denied("Work-item creation has pre-existing history")
        return seed

    def _validate_seed_replay(self, seed: dict) -> None:
        """Validate a committed seed and its chain under records.lock.

        Recognized atomic-write leftovers may be recovered as on normal reads.
        Valid transitions never alter the immutable creation result.
        """
        item_id = seed["id"]
        revisions = self._recover_temporary_writes(item_id)
        for index, record in enumerate(self._iter_chain(item_id, history_revisions=revisions)):
            if index == 0 and record != seed:
                raise Denied("Conflicting work-item seed replay")

    def create(self, *, idempotency_key, title, actor, origin=None,
               description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, smoke_refs=None) -> dict:
        """Commit one immutable seed; exact retries return the original record.

        Keys are actor-scoped, 1-128 characters. The optional origin,
        description and scope are part of that immutable request. Caller holds
        no lock; runtime binds actor.
        """
        item_id = self._create_id(actor, idempotency_key)
        validate_origin(origin)
        with lock_diagnostics.operation("write"), record_lock(self.root):
            directory = self.root / "work-items" / item_id
            existing = None
            if _directory(directory.parent) and _directory(directory):
                try:
                    (directory / "create.json").lstat()
                except FileNotFoundError:
                    pass
                else:
                    # Recover post-link aliases only for an already committed seed.
                    self._recover_temporary_writes(item_id)
                    existing = self._creation_record(item_id)
                    if existing is not None and existing["kind"] != "direct":
                        raise Denied("Conflicting work-item creation retry")
            if existing is not None:
                seed = make_seed(item_id, title, origin, existing["work_item"]["created_at"],
                                 description=description, scope=scope, smoke_refs=smoke_refs)
                record = {"kind": "direct", "actor": actor, "idempotency_key": idempotency_key,
                          "work_item": seed}
                if {key: value for key, value in existing.items() if key != "requested_smoke_refs"} != record:
                    raise Denied("Conflicting work-item creation retry")
                self._validate_seed_replay(existing["work_item"])
                return {"record": existing, "created": False}
            seed = self._create_seed(item_id, title, origin, now(), description=description,
                                     scope=scope, smoke_refs=smoke_refs)
            record = {"kind": "direct", "actor": actor, "idempotency_key": idempotency_key,
                      "work_item": seed}
            directory.mkdir(parents=True, exist_ok=True)
            with self.committing([item_id]):
                write_json(directory / "create.json", record, exclusive=True)
            return {"record": record, "created": True}

    def _iter_items(self, *, recover: bool):
        """Yield (item id, creation record, validated revisions) for every seeded item."""
        directory = self.root / "work-items"
        if not _directory(directory):
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                if ITEM_ID.fullmatch(entry.name) is None or not _directory(directory / entry.name):
                    raise Denied("Invalid work-item directory entry")
                revisions = self._history_revisions(entry.name, recover=recover)
                try:
                    (directory / entry.name / "create.json").lstat()
                except FileNotFoundError:
                    # A failed first create can leave only a temporary or empty
                    # directory. Validate/recover it, but never invent a seed.
                    if revisions:
                        raise Denied("Work-item history has no immutable seed")
                    continue
                yield entry.name, self._creation_record(entry.name), revisions

    def _seed(self, item_id: str) -> dict:
        """Validate and return an item's immutable seed."""
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        record = self._creation_record(item_id)
        if record is None:
            raise Denied("Work item has no immutable seed")
        return record["work_item"]

    def _history_revisions(self, item_id: str, *, recover: bool, require_clean=False) -> set[int] | None:
        """Validate per-item history names, optionally recovering leftovers.

        Single scan shared by locked and read-only readers. With
        ``recover=True`` (caller holds records.lock) recognized regular
        orphan temporaries and post-link aliases are unlinked and the
        directory synced; with ``recover=False`` they are only validated and
        ignored, never deleted or synced. A safe orphan (``st_nlink == 1``)
        is uncommitted, and a post-link alias (``st_nlink == 2``) is accepted
        only when exactly one regular committed target identifies the same inode.
        The single fixed retirement sidecar is separate from the unchanged
        ordinary-name/temporary budget; it never consumes a revision.
        Anything else, including unsafe temporaries and unexpected names,
        fails closed. Returns the validated revision set, or None when the
        item directory is absent. The storage auditor requires a clean directory
        and refuses even recognized leftovers until a locked read recovers them.
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
            for count, entry in enumerate(entry for entry in entries if entry.name != "retirement.json"):
                if count >= MAX_REVISIONS + 1:
                    raise Denied("Work-item directory exceeds recovery bound")
                if _TEMP_NAME.fullmatch(entry.name):
                    temporary_entries.append(entry)
                    continue
                if entry.name == "create.json":
                    info = _entry_stat(entry)
                    if (not stat.S_ISREG(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise Denied("Unsafe work-item creation record")
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
        retirement_path = directory / "retirement.json"
        try:
            info = retirement_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise Denied("Unsafe work-item retirement")
            revision_inodes.append((info.st_dev, info.st_ino, retirement_path))
        if require_clean and temporary_entries:
            raise Denied("Unrecovered work-item temporary")
        removed = False
        for entry in temporary_entries:
            try:
                info = _entry_stat(entry)
            except FileNotFoundError:
                continue  # A concurrent writer finished its atomic write.
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
            if not recover:
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            removed = True
        if recover and removed:
            sync_dir(directory)
        return revisions

    def _recover_temporary_writes(self, item_id: str) -> set[int] | None:
        """Recover atomic-write leftovers and return validated revision names.

        Locked entry point sharing validation with the read-only scan.
        A regular orphan temporary is uncommitted. On POSIX, a crash after
        link() can leave a .new-* alias beside its committed revision; remove
        it only when exactly one regular revision name identifies the same
        inode. The returned set lets the locked reader reuse this bounded scan.
        """
        return self._history_revisions(item_id, recover=True)

    def _stored_wait(self, value) -> dict:
        """Validate normalized waits without rebasing stored code-change evidence."""
        if not isinstance(value, dict):
            raise Denied("Blocked work items require a structured wait")
        kind = value.get("kind")
        if kind == "proposal_merged":
            from .proposal_waits import normalize
            refs = validate_refs(value.get("smoke_refs", []))
            raw = {k: v for k, v in value.items() if k != "smoke_refs"}
            result = normalize(self, raw, stored=True)
            return {**result, "smoke_refs": refs} if "smoke_refs" in value else result
        if kind in ("ci", "publication_ci"):
            refs = validate_refs(value.get("smoke_refs", []))
            raw = {k: v for k, v in value.items() if k != "smoke_refs"}
            if kind == "publication_ci":
                from .ci_waits import normalize_publication
                result = normalize_publication(raw, snapshots=self.snapshots, stored=True)
            else:
                from .ci_waits import normalize
                result = normalize(self.root, raw, stored=True)
            return {**result, "smoke_refs": refs} if "smoke_refs" in value else result
        return stored_wait(value)

    def _published_digest(self) -> str:
        """Current published code digest: the baseline of a new code_change wait."""
        current = self.snapshots.get()
        value = current.get("code_digest") if isinstance(current, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError("No published code digest")
        return value

    @staticmethod
    def _scope_digest(scope):
        return digest(canonical(scope)) if scope is not None else None

    def _validate_delivery(self, previous, record, *, verify_snapshots: bool = True):
        """Replay the explicit binding, without consulting mutable history/config."""
        snapshot = None
        strict_done = record["status"] == "done" and recorded_policy(record) == VERIFIED_SNAPSHOT
        attested_done = record["status"] == "done" and not strict_done
        marked = "delivery_schema" in record
        if "delivery_schema" in previous and not marked:
            raise Denied("A work-item transition cannot remove the delivery schema")
        if not marked:
            return False, None
        if type(record["delivery_schema"]) is not int or record["delivery_schema"] != 1:
            raise Denied("Invalid work-item delivery schema")
        delivery = record["delivery"]
        # Only the validated reopen event may discard an active binding.
        clearing = "reopen" in record and delivery is None
        binding = delivery != previous.get("delivery") and not clearing
        if binding:
            if (previous["status"] != "doing" or record["status"] != "doing"
                    or previous.get("scope") != record.get("scope")):
                raise Denied("Delivery binding requires doing-to-doing with unchanged scope")
            if (not isinstance(delivery, dict)
                    or set(delivery) != {"snapshot_id", "code_digest", "scope_digest", "binding_rev"}
                    or type(delivery["binding_rev"]) is not int
                    or delivery["binding_rev"] != record["rev"]
                    or delivery["scope_digest"] != self._scope_digest(record.get("scope"))):
                raise Denied("Invalid work-item delivery binding")
        if delivery is not None:
            if (not isinstance(delivery, dict) or not isinstance(delivery.get("snapshot_id"), str)
                    or not DIGEST.fullmatch(delivery["snapshot_id"])
                    or not isinstance(delivery.get("code_digest"), str)
                    or not DIGEST.fullmatch(delivery["code_digest"])):
                raise Denied("Invalid delivery snapshot")
            if verify_snapshots and not attested_done:
                snapshot = self.snapshots.binding(delivery["snapshot_id"])
                if (snapshot.get("id") != delivery["snapshot_id"]
                        or snapshot.get("code_digest") != delivery["code_digest"]):
                    raise Denied("Delivery does not match its immutable snapshot")
        if strict_done:
            if (delivery is None or delivery != previous.get("delivery")
                    or record["snapshot_id"] != delivery["snapshot_id"]
                    or delivery["scope_digest"] != self._scope_digest(record.get("scope"))):
                raise Denied("verified_snapshot_v1: Completion requires a prior exact snapshot and scope delivery binding")
        return binding, snapshot

    def _retained_verification(self, verification, code_digest, scope_digest, snapshot_id):
        from .acceptance import get
        if (not isinstance(verification, dict)
                or set(verification) != {"source", "receipt_id", "passed", "code_digest"}
                or verification["source"] != "retained_acceptance"
                or verification["passed"] is not True
                or verification["code_digest"] != code_digest
                or not isinstance(verification["receipt_id"], str)
                or not DIGEST.fullmatch(verification["receipt_id"])
                or not isinstance(scope_digest, str) or not DIGEST.fullmatch(scope_digest)):
            raise Denied("verified_snapshot_v1: Invalid retained completion verification")
        receipt = get(self.root, verification["receipt_id"])
        if (receipt["code_digest"] != code_digest
                or digest(canonical(receipt["scope"])) != scope_digest
                or receipt["schema"] == 2 and receipt["scope"]["ci"]["snapshot_id"] != snapshot_id):
            raise Denied("verified_snapshot_v1: Completion acceptance receipt code or scope mismatch")

    def _validate_transition(self, previous: dict, record: dict, *, verify_snapshots: bool = True) -> None:
        """Validate transitions, always checking current completion evidence."""
        fields = set(record) - {"smoke_refs", "blocked_since", "wait_resolution",
                                "completion_policy", "completion_evidence", "reopen"}
        policy = recorded_policy(record)
        if ("completion_policy" in record) != ("completion_evidence" in record):
            raise Denied(f"{policy}: completion policy and evidence fields must appear together")
        if record.get("status") != "done" and "completion_policy" in record:
            raise Denied(f"{policy}: only done records carry completion policy and evidence")
        if policy == VERIFIED_SNAPSHOT and record.get("completion_evidence") is not None:
            raise Denied(f"{policy}: inline evidence is not accepted")
        if "blocked_since" in previous and "blocked_since" not in record:
            raise Denied("A transition cannot remove its blocked start")
        if "blocked_since" in record:
            since = record["blocked_since"]
            if since is not None and not _timestamp(since):
                raise Denied("Invalid blocked start timestamp")
            if ("blocked_since" in previous or previous["status"] == "open" or previous["rev"] == 1):
                start = previous.get("blocked_since", blocked_start(None, previous))
                expected = blocked_start(start, record)
                if since != expected:
                    raise Denied("A blocked start resets only on an explicit open transition")
        if "smoke_refs" in record:
            validate_refs(record["smoke_refs"])
        if "smoke_refs" in previous and "smoke_refs" not in record:
            raise Denied("A transition cannot remove its smoke reference field")
        # A record written now always carries the scope in force; a record
        # written before the scope existed keeps the older field set and stays
        # readable history.
        description = {"description"} if "description" in previous else set()
        allowed = {frozenset(base | description | scope)
                   for base in (DELIVERY_EVENT_FIELDS, SCOPED_EVENT_FIELDS, DELIVERED_EVENT_FIELDS, LEGACY_EVENT_FIELDS)
                   for scope in (set(), {"scope"})}
        if frozenset(fields) not in allowed:
            raise Denied("Invalid work-item transition fields")
        actor = identifier(record["actor"])
        reopening = "reopen" in record
        if reopening:
            marker, reason = record["reopen"], record["reason"]
            if (previous["status"] != "done" or record["status"] != "open"
                    or previous["rev"] > MAX_REOPEN_REV
                    or not isinstance(marker, dict)
                    or set(marker) != {"challenged_rev", "challenger", "reason_digest"}
                    or type(marker["challenged_rev"]) is not int
                    or marker["challenged_rev"] != previous["rev"]
                    or marker["challenger"] != actor
                    or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                    or marker["reason_digest"] != digest(canonical(reason))
                    or record.get("scope") != previous.get("scope")
                    or any(key in record for key in ("completion_policy", "completion_evidence", "wait_resolution"))
                    or any(record.get(key) is not None for key in
                           ("holder", "wait", "snapshot_id", "verification", "delivered_tree",
                            "verification_scope", "delivery", "blocked_since"))):
                raise Denied("Invalid audited completion reopen marker or state")
        validate_origin(record["origin"], stored=True)
        status = record["status"]
        previous_scope = previous.get("scope")
        record_scope = record.get("scope")
        if record_scope is not None:
            validate_scope(record_scope)
        scope_changed = record_scope != previous_scope
        refs_changed = (record.get("smoke_refs", []) != previous.get("smoke_refs", [])
                        or (record.get("wait") or {}).get("smoke_refs", []) !=
                           (previous.get("wait") or {}).get("smoke_refs", []))
        # Status projections may skip historical delivery manifests, never the
        # current proof of completion, including on a source-backed sidecar hit.
        binding, delivery_snapshot = self._validate_delivery(
            previous, record, verify_snapshots=verify_snapshots or status == "done")
        release = (status == previous["status"] == "open" and previous["holder"] is not None
                   and record["holder"] is None and "wait_resolution" not in record)
        if (status not in STATUSES or (previous["status"] == "done" and not reopening)
                or (previous["status"] == "dropped" and status != "done")
                or (status == previous["status"] and not scope_changed and not refs_changed and not binding and not release)):
            raise Denied("Invalid work-item status transition")
        # The scope in force is inherited by every revision; it is never
        # cleared, and a closure may not change it.
        if previous_scope is not None and record_scope is None:
            raise Denied("A work-item transition cannot clear the accepted scope")
        if (status == "done" and (previous_scope is not None or "delivery_schema" in record)
                and record_scope != previous_scope):
            raise Denied(f"{policy}: Completion cannot change the accepted scope; "
                         "record the scope change in an earlier transition")
        if (status == "done" and policy == HOLDER_ATTESTATION
                and previous["status"] != "dropped" and actor != previous["holder"]):
            raise Denied(f"{policy}: ordinary completion requires the current holder")
        if previous["status"] == "open" and previous["holder"] not in (None, actor):
            raise Denied("Held-open work belongs to its holder")
        resolved = "wait_resolution" in record
        if resolved:
            from .work_item_resolution import validate
            validate(self, previous, record)
        holder = (previous["holder"] if resolved else actor if status == "doing" else
                  (previous["holder"] or actor) if status == "blocked" else None)
        if (any(record[key] != previous[key] for key in ("id", "title", "origin", "created_at")
                + (("description",) if "description" in previous else ()))
                or type(record["rev"]) is not int or record["rev"] != previous["rev"] + 1
                or record["rev"] > MAX_REVISIONS or record["holder"] != holder
                or not _timestamp(record["updated_at"])
                or record["transition"] != {"from": previous["status"], "to": status}):
            raise Denied("Invalid work-item revision chain")
        reason = record["reason"]
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise Denied(f"{policy}: Work-item reason must be nonempty text" if status == "done"
                         else "Work-item reason must be nonempty text")
        # A scope change is its own recorded event, so it carries the reason
        # that names what it supersedes.
        if scope_changed and (reason is None or not reason.strip()):
            raise Denied("A scope change requires a reason")
        if status == "dropped" and reason is None:
            raise Denied("Dropping a work item requires a reason")
        if status == "blocked":
            if record["wait"] is not None:
                self._stored_wait(record["wait"])
        elif record["wait"] is not None:
            raise Denied("Only blocked work items carry waits")
        if status == "done" and policy == HOLDER_ATTESTATION:
            validate_attestation(record.get("completion_evidence"))
            if any(record.get(key) is not None for key in
                   ("snapshot_id", "verification", "delivered_tree", "verification_scope")):
                raise Denied(f"{policy}: snapshot-related fields must be null")
            if previous["status"] == "dropped" and reason is None:
                raise Denied(f"{policy}: dropped correction requires a nonempty reason")
        elif status == "done":
            sid, verification = record["snapshot_id"], record["verification"]
            if (not isinstance(sid, str) or not DIGEST.fullmatch(sid)
                    or not isinstance(verification, dict) or verification.get("passed") is not True
                    or not isinstance(verification.get("code_digest"), str)
                    or not DIGEST.fullmatch(verification["code_digest"])):
                raise Denied("verified_snapshot_v1: passing digest-bound verification required")
            snapshot = delivery_snapshot if delivery_snapshot is not None else self.snapshots.binding(sid)
            code_digest = snapshot["code_digest"]
            if verification["code_digest"] != code_digest:
                raise Denied("verified_snapshot_v1: Completion evidence does not match its delivery")
            retained = verification.get("source") == "retained_acceptance"
            if retained and "delivery_schema" in record:
                self._retained_verification(verification, code_digest,
                                            record["verification_scope"], sid)
            elif (record.get("verification_scope") is not None
                    or snapshot.get("id") != sid
                    or snapshot.get("verification") != verification
                    or verification["code_digest"] != snapshot.get("code_digest")):
                raise Denied("verified_snapshot_v1: Completion evidence does not match its verified snapshot")
            if previous["status"] == "dropped" and (not retained or "delivery_schema" not in record):
                raise Denied("verified_snapshot_v1: Dropped correction requires independently retained acceptance")
            # A closure states the tree that carries its delivery. A record
            # written before the claim existed stays readable history; a
            # closure that states a tree its snapshot does not carry fails.
            if "delivered_tree" in record and record["delivered_tree"] != code_digest:
                raise Denied("verified_snapshot_v1: Completion does not name the tree that carries the delivery")
        elif (record["snapshot_id"] is not None or record["verification"] is not None
                or record.get("delivered_tree") is not None
                or record.get("verification_scope") is not None):
            raise Denied("Only completed work items carry snapshot evidence")

    def _iter_chain(self, item_id: str, *, history_revisions=_HISTORY_UNSET, retirement=None, verify_snapshots: bool = True):
        """Replay and validate one append-only chain, yielding one record at a time.

        The seed and each revision are immutable full-state records. Callers may
        retain only the current record or a bounded page; replay itself never
        materializes the complete history. On exhaustion, validate the terminal
        sidecar separately; never yield it as an invented revision. Optional
        retirement collects that validated record and the full chain hash.
        Status-only callers may skip superseded delivery snapshot reads, not
        chain/field validation or the current active binding. Done completion
        proofs always validate their recorded versioned contract.
        """
        current = self._seed(item_id)
        chain_hash = hashlib.sha256()
        chain_hash.update(canonical(current) + b"\n")
        blocked_since = blocked_start(None, current)
        yield current
        # The auditor uses the same safe scan, without recovery or writes.
        revisions = (self._history_revisions(item_id, recover=False, require_clean=True)
                     if history_revisions is _HISTORY_UNSET else history_revisions) or set()
        if revisions != set(range(2, len(revisions) + 2)):
            raise Denied("Gapped work-item history")
        for revision in range(2, len(revisions) + 2):
            record = self._read(f"work-items/{item_id}/{revision}.json")
            self._validate_transition(current, record, verify_snapshots=verify_snapshots)
            blocked_since = blocked_start(blocked_since, record)
            if "blocked_since" in record and record["blocked_since"] != blocked_since:
                raise Denied("Blocked start does not match work-item history")
            current = record
            chain_hash.update(canonical(current) + b"\n")
            yield current
        chain_sha256 = chain_hash.hexdigest()
        terminal = work_item_retirement.read(self, current, chain_sha256)
        if retirement is not None:
            retirement.update(chain_sha256=chain_sha256, record=terminal)

    def _get(self, item_id: str, *, history_revisions=_HISTORY_UNSET, verify_snapshots: bool = True,
             verify_current_delivery: bool = False) -> dict:
        """Replay one item's current record from its revision files."""
        return self._current_hold(item_id, history_revisions=history_revisions,
                                  verify_snapshots=verify_snapshots,
                                  verify_current_delivery=verify_current_delivery)[0]

    def _current_hold(self, item_id: str, *, history_revisions=_HISTORY_UNSET, verify_snapshots: bool = True,
                      verify_current_delivery: bool = False, blocked_waiter=None) -> tuple:
        """Replay a chain into (current record, doing start, blocked start).

        The optional waiter receives the current blocked record with its
        derived start, so a writer cannot hide a stale prior waiter.
        """
        since = None
        blocked_since = None
        current = None
        previous = None
        last_transition = None
        retirement = {}
        for current in self._iter_chain(item_id, history_revisions=history_revisions, retirement=retirement,
                                        verify_snapshots=verify_snapshots):
            if previous is not None:
                last_transition = (previous, current)
            previous = current
            blocked_since = blocked_start(blocked_since, current)
            if blocked_waiter is not None:
                if current["status"] == "open":
                    blocked_waiter.clear()
                elif current["status"] == "blocked":
                    blocked_waiter.clear()
                    blocked_waiter.update(current, blocked_since=blocked_since)
            since = (since or current["updated_at"]) if current["status"] == "doing" else None
        terminal = retirement["record"]
        if (verify_current_delivery and not verify_snapshots and terminal is None
                and current["status"] in OPEN_STATUS_ORDER and current.get("delivery") is not None):
            if last_transition is None:
                raise Denied("Current delivery lacks a validated transition")
            self._validate_delivery(*last_transition, verify_snapshots=True)
        return (work_item_retirement.project(current, terminal),
                None if terminal else since, blocked_since)

    # Index source: the only code that turns revision files into index rows.

    def _ledger_row(self, item_id, revisions, *, strict=False) -> tuple:
        creator = self._creation_record(item_id).get("actor")
        item, since, blocked_since = self._current_hold(
            item_id, history_revisions=revisions,
            verify_snapshots=strict, verify_current_delivery=True)
        origin = item["origin"]
        return (item["id"], item["rev"], item["status"], item["holder"],
                _timestamp_order(item["created_at"]), _timestamp_order(item["updated_at"]),
                origin if origin is None or isinstance(origin, str) else canonical(origin).decode(),
                creator, (item.get("wait") or {}).get("kind"), since, blocked_since,
                canonical(item).decode())

    def ledger_entries(self, *, recover: bool, skip=frozenset(), strict=False, errors=None):
        """Yield every index entry, replayed and validated from the files.

        With ``errors`` (a list), an item whose chain fails validation is
        reported there as ``(item_id, exception)`` instead of aborting.
        """
        for item_id, _, revisions in self._iter_items(recover=recover):
            if item_id in skip:
                continue
            try:
                yield ("item", self._ledger_row(item_id, revisions, strict=strict))
            except (Denied, LimitExceeded) as exc:
                if errors is None:
                    raise
                errors.append((item_id, exc))

    def ledger_item(self, item_id: str, hint, *, recover: bool) -> list:
        """Replay the index entries of one item (none when it has no seed)."""
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        if self._creation_record(item_id) is None:
            return []
        revisions = self._history_revisions(item_id, recover=recover)
        return [("item", self._ledger_row(item_id, revisions))]

    def committing(self, item_ids):
        """Bracket a revision-file commit for ``item_ids``; caller holds the record lock."""
        return self._ledger.write("items", item_ids)

    # Current state: every read below comes from the index, never from history.

    @staticmethod
    def _decode(raw, since=None, blocked_since=None):
        item = json.loads(raw)
        if blocked_since is not None:
            item["blocked_since"] = blocked_since
        return item, since

    def _indexed(self, item_id: str, *, locked: bool):
        """Return (item, doing start) with any derived blocked start, or raise."""
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        with self._ledger.read("items", locked=locked) as db:
            found = _rows(db, "SELECT item, doing_since, blocked_since FROM items WHERE id=?",
                          (item_id,))
        if not found:
            raise Denied("Work item has no immutable seed")
        return self._decode(*found[0])

    def current(self, item_id: str) -> dict:
        """Current record of one item, without locks or writes."""
        return self._plain(item_id)

    def _plain(self, item_id: str, *, locked: bool = False, durable: bool = False) -> dict:
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        with self._ledger.read("items", locked=locked, durable=durable) as db:
            found = _rows(db, "SELECT item FROM items WHERE id=?", (item_id,))
        if not found:
            raise Denied("Work item has no immutable seed")
        return json.loads(found[0][0])

    def current_items(self, *, statuses=None) -> list[dict]:
        """Current records of every seeded item in ID order, optionally by status."""
        query = "SELECT item FROM items"
        if statuses is not None:
            if not statuses or any(status not in STATUSES for status in statuses):
                raise Denied("Invalid work-item status filter")
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
        with self._ledger.read("items", locked=False) as db:
            found = _rows(db, query + " ORDER BY id", tuple(statuses or ()))
        return [json.loads(raw) for (raw,) in found]


    def creators(self) -> dict:
        """``{item id: creating role or None}``; imported items have no creator."""
        with self._ledger.read("items", locked=False) as db:
            return dict(_rows(db, "SELECT id, creator FROM items"))

    def closed_items(self, *, limit: int):
        """Return the closed page and the current records of its rows."""
        return self._page_closed_impl(limit=limit, readonly=True, include_items=True)

    def history(self, item_id: str):
        """Replay and validate one item's committed chain from its revision files."""
        return self._iter_chain(item_id)


    def audit(self, *, repair: bool = False) -> dict:
        """Replay every chain strictly and compare it with the index.

        Holds the record lock for the whole run. Reports corrupt chains and
        index divergence (at most MAX_OPEN_PAGE_ROWS IDs each, with counts),
        for work items and check jobs; ``repair`` then rebuilds a diverged
        index from the files (work items require every chain to validate).
        The audit itself never changes files.
        """
        with lock_diagnostics.operation("write"), record_lock(self.root):
            errors = []
            files = {}
            for kind, value in self.ledger_entries(recover=False, strict=True, errors=errors):
                if kind != "item":
                    raise Denied("Invalid work-item index entry")
                files[value[0]] = value
            index = self._ledger.snapshot("items")
            if index is None:
                state, indexed = "missing", {}
            else:
                state, indexed = index
            divergent = sorted(item_id for item_id in set(files) | set(indexed)
                               if files.get(item_id) != indexed.get(item_id)
                               and item_id not in dict(errors))
            consistent = not errors and not divergent and state == "complete"
            repaired = False
            if repair and not errors and not consistent:
                self._ledger.rebuild("items")
                repaired = True
            jobs = None
            if "check_jobs" in self._ledger.domains:
                with lock(self.root / "locks" / "check-jobs.lock"):
                    jobs = check_job_index.audit(self._ledger, repair=repair)
            return {"items": len(files) + len(errors),
                    "index": state, "consistent": consistent and (jobs is None or jobs["consistent"]),
                    "repaired": repaired or bool(jobs and jobs["repaired"]),
                    **({"check_jobs": jobs} if jobs is not None else {}),
                    "corrupt": [{"id": item_id, "error": str(exc)}
                                for item_id, exc in errors[:MAX_OPEN_PAGE_ROWS]],
                    "corrupt_count": len(errors),
                    "divergent": divergent[:MAX_OPEN_PAGE_ROWS], "divergent_count": len(divergent)}

    def get(self, item_id: str) -> dict:
        """Read one item's current record under records.lock."""
        with lock(self.root / "locks" / "records.lock"):
            return self._plain(item_id, locked=True)


    def _projection_unlocked(self, item_id: str) -> dict:
        """Build a bounded projection while the caller holds records.lock."""
        item, since = self._indexed(item_id, locked=True)
        view = {"item": item, **self._hold_projection(since), **blocked_age(item)}
        if len(canonical(view)) > MAX_PROJECTION_BYTES:
            raise LimitExceeded("Work-item projection exceeds byte bound")
        return view

    def explain(self, item_id: str, *, after_rev=0, limit=25) -> dict:
        """Return current status plus a byte-bounded ascending history page.

        ``after_rev`` is an exclusive revision cursor (0..MAX_REVISIONS); the
        immutable seed is revision 1. The full chain is replayed and validated
        even after the page fills or its response byte budget is reached. Snapshot
        manifests are checked only for current returned evidence and selected
        history rows.
        """
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        if type(after_rev) is not int or not 0 <= after_rev <= MAX_REVISIONS:
            raise Denied(f"Work-item history cursor must be an integer in [0, {MAX_REVISIONS}]")
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item history limit must be an integer in [1, 100]")

        with lock(self.root / "locks" / "records.lock"):
            history_revisions = self._recover_temporary_writes(item_id)
            history = []
            history_bytes = 0
            stopped_for_bytes = False
            # Reserve the maximum independently bounded current projection and
            # ample fixed envelope space before admitting immutable history rows.
            row_budget = MAX_HISTORY_PAGE_BYTES - MAX_PROJECTION_BYTES - PREVIEW_BYTES - 1024
            current = None
            since = None
            blocked_since = None
            previous = None
            last_transition = None
            retirement = {}
            for record in self._iter_chain(item_id, history_revisions=history_revisions,
                                          retirement=retirement, verify_snapshots=False):
                if previous is not None:
                    last_transition = (previous, record)
                previous = record
                current = record
                blocked_since = blocked_start(blocked_since, record)
                since = (since or record["updated_at"]) if record["status"] == "doing" else None
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
            terminal = retirement["record"]
            verified_snapshots = {}
            current_delivery = current.get("delivery")
            if current_delivery is not None:
                if current["status"] == "done":
                    if recorded_policy(current) == VERIFIED_SNAPSHOT:
                        # Only snapshot completion validated this manifest as proof.
                        snapshot_id = current_delivery["snapshot_id"]
                        verified_snapshots[snapshot_id] = current_delivery["code_digest"]
                else:
                    if last_transition is None:
                        raise Denied("Current delivery lacks a validated transition")
                    _, snapshot = self._validate_delivery(*last_transition, verify_snapshots=True)
                    verified_snapshots[current_delivery["snapshot_id"]] = snapshot["code_digest"]
            for record in history:
                delivery = record.get("delivery")
                if delivery is None:
                    continue
                snapshot_id = delivery["snapshot_id"]
                if snapshot_id not in verified_snapshots:
                    snapshot = self.snapshots.binding(snapshot_id)
                    if snapshot.get("id") != snapshot_id:
                        raise Denied("Delivery does not match its immutable snapshot")
                    verified_snapshots[snapshot_id] = snapshot.get("code_digest")
                if verified_snapshots[snapshot_id] != delivery["code_digest"]:
                    raise Denied("Delivery does not match its immutable snapshot")
            current = work_item_retirement.project(current, terminal)
            if terminal:
                since = None
            projection = {"item": current, **self._hold_projection(since),
                          **blocked_age({**current, "blocked_since": blocked_since})}
            if len(canonical(projection)) > MAX_PROJECTION_BYTES:
                raise LimitExceeded("Work-item projection exceeds byte bound")
            history_total = current["rev"]
            last_returned = history[-1]["rev"] if history else after_rev
            more = last_returned < history_total
            result = {**projection, "history": history, "history_total": history_total,
                      "cursor": after_rev, "next_cursor": last_returned if more else None,
                      "truncated": more}
            if terminal:
                result["retirement"] = terminal
            if len(canonical(result)) > MAX_HISTORY_PAGE_BYTES:
                raise LimitExceeded("Work-item history response exceeds byte bound")
            return result


    def projection(self, item_id: str) -> dict:
        """Return one bounded item with its hold and blocked ages.

        The immutable item remains the CAS authority. This is an internal
        read projection, not prompt/tool/CLI/status exposure.
        """
        with lock(self.root / "locks" / "records.lock"):
            return self._projection_unlocked(item_id)


    def page_open(self, *, cursor=None, limit=25) -> dict:
        """Return a bounded live page of seeded, currently open work items.

        Schema: items ordered by status (doing, blocked, open), then oldest
        ``created_at`` and ID; includes total seeded-open count, the exclusive
        input ``cursor``, ``next_cursor`` iff another matching item follows,
        ``overflow_count`` (matching rows after the cursor beyond this page),
        and ``truncated`` (exactly ``overflow_count > 0``).
        Limits are 1..100 (exact int); the opaque cursor carries the last row's
        status, UTC creation-order key and ID. Rows are at most 8192 canonical
        bytes and the complete response at most 1 MiB. Selection streams the
        indexed active rows keeping O(limit) of them.
        The page serves the runtime prompt projection and the explicitly granted
        ``work_items_read`` operation. It does not transition items. Cursors
        page a live view: changes between calls can move rows; a last-key
        cursor avoids offset shifts on earlier closes but does not freeze
        membership.
        """
        with lock(self.root / "locks" / "records.lock"):
            return self._page_open_impl(cursor=cursor, limit=limit, readonly=False)

    def page_open_for_order(self, *, limit=25) -> tuple[dict, str]:
        """Return the normal open page and an order-only full-inventory fingerprint.

        The same locked scan folds bounded scopes into constant-size, order-independent evidence. No page or ready-set
        signature is changed, and no bodies or history are retained.
        """
        with lock(self.root / "locks" / "records.lock"):
            return self._page_open_impl(cursor=None, limit=limit, readonly=False,
                                        order_fingerprint=True)

    def page_ready(self, *, limit=25, role_name=None, writer_roles=None,
                   _delivered_wait_resolution_tokens=None,
                   _wait_resolution_high_water=None) -> dict:
        """Return open/doing work and blocked work needing replanning.

        Same bounds, ordering, recovery, lock and fail-closed contract as
        page_open. Filter before selection/counting, scanning the ledger with
        O(limit) retained rows: blocked rows cannot hide ready work. ``signature``
        is a fixed 64-hex fingerprint of the full eligible ID set and terminal
        evidence and wait-resolution proof bindings (not just the selected page),
        after role filtering. ``_wait_resolution_pending`` reports a validated
        holder-owned resolution anywhere in that set, not a delivery receipt.
        Host dispatch may supply its bounded exact delivered-token set; then
        ``_wait_resolution_undelivered`` reports a current eligible proof new to
        that input, ``_wait_resolution_delivered_current`` returns only supplied
        tokens still present in the full eligible scan, and unseen proof rows sort
        first in exact token order. A private high-water receipt covers a shown
        prefix only while its role-eligible resolution-token-set signature matches;
        a mismatch or missing boundary retries selection without that prefix under
        the same lock. The returned high water is prospective: persist it only
        when these pages have actually been delivered. Ordinary read pages omit
        these private fields and keep their normal ordering.
        It is independent of scan order, other row metadata and limit, with
        constant extra memory. It is change detection evidence, not authorization or a collision-free set proof. No state
        transitions are performed. With role_name, only unheld open items
        and that role's held open/doing/blocked items are eligible. Held-open
        ownership is also enforced at the transition boundary; other grant
        authorization is checked by the caller. Satisfied generic waits re-enter
        this page for reassessment, as do absent or confidently impossible
        releasers; they never imply completion. Holder-only waits (whose holder is
        the sole possible resolver) appear in the holder's page as ordinary
        held-open work, without satisfying the registered wait.
        Code-change authority comes from writer_roles or the configured lookup.
        CI holder-only readiness requires the independent requester grant and
        fresh exact local absence evidence. Otherwise CI waits are ready
        only from their validated terminal route, independent of event ack.
        Unreadable
        liveness (``unknown``) excludes only that blocked item and never
        releases it; known ready rows are still returned with
        ``coverage_complete`` false and a bounded ``unknown_blocked`` count,
        so a zero total is not an exhaustive claim. Malformed ledger or
        unsafe work-item state still fails closed.
        """
        if role_name is not None:
            identifier(role_name)
        # The adapter is slow and owns its timeout; gather its answers while
        # other work-item I/O can still proceed, never under records.lock.
        ci_unreachable = self._ci_unreachable_hints(role_name=role_name)
        with lock(self.root / "locks" / "records.lock"):
            if _wait_resolution_high_water is not None:
                from .snapshot import validate_wait_resolution_high_water
                validate_wait_resolution_high_water(_wait_resolution_high_water)
                if _delivered_wait_resolution_tokens is None or role_name is None:
                    raise Denied("Wait-resolution high water requires scoped delivery tokens")
            if writer_roles is None and self.writer_roles_lookup is not None:
                writer_roles = self.writer_roles_lookup()
            args = dict(cursor=None, limit=limit, readonly=False, ready_only=True,
                        role_name=role_name, writer_roles=writer_roles,
                        delivered_wait_resolution_tokens=_delivered_wait_resolution_tokens,
                        ci_unreachable=ci_unreachable)
            result = self._page_open_impl(
                **args, wait_resolution_high_water=_wait_resolution_high_water)
            if (_wait_resolution_high_water is not None
                    and (result["_wait_resolution_high_water"]["signature"]
                         != _wait_resolution_high_water["signature"]
                         or not result["_wait_resolution_boundary_current"])):
                # The whole proof set (not unrelated ready work) changed. A
                # fresh locked scan must select from the beginning; a boundary
                # from a different set could otherwise skip a newly inserted proof.
                result = self._page_open_impl(**args)
            result.pop("_wait_resolution_boundary_current", None)
            return result


    @staticmethod
    def _open_row(item: dict, view: dict, *, wait_liveness=None) -> tuple:
        """Build one bounded open-page row and its ordering key."""
        sort_key = (OPEN_STATUS_ORDER[item["status"]],
                    _timestamp_order(item["created_at"]),
                    int(item["id"][3:], 16))
        row = {key: item[key] for key in
               ("id", "title", "origin", "status", "rev", "holder",
                "created_at", "updated_at")}
        row.update({key: view[key] for key in
                    ("doing_since", "doing_age_seconds", "doing_stale")})
        if "wait_resolution" in item:
            row["wait_resolution"] = item["wait_resolution"]
        if item["status"] == "blocked":
            row["wait"] = item["wait"]
            row.update({key: view[key] for key in
                        ("blocked_since", "blocked_age_seconds")})
            if wait_liveness is not None:
                row["wait_liveness"] = wait_liveness
        if "description" in item:
            preview, clipped = text_preview(item["description"], 1024)
            row["description_preview"] = preview
            row["description_truncated"] = clipped
        scope = item.get("scope")
        if scope is not None:
            preview, clipped = text_preview(scope, 1024)
            row["scope_preview"] = preview
            row["scope_truncated"] = clipped
        reason = item.get("reason")
        if reason is not None:
            preview, clipped = text_preview(reason, 1024)
            row["reason_preview"] = preview
            row["reason_truncated"] = clipped
        row_bytes = len(canonical(row))
        if row_bytes > MAX_OPEN_ROW_BYTES:
            # New visibility fields share the existing fixed row budget. When
            # necessary, shorten previews (and mark them) rather than making a
            # formerly readable row fail or raising the established cap.
            for source, preview_key, truncated_key in (
                    ("description", "description_preview", "description_truncated"),
                    ("scope", "scope_preview", "scope_truncated"),
                    ("reason", "reason_preview", "reason_truncated")):
                if source not in item or preview_key not in row or row_bytes <= MAX_OPEN_ROW_BYTES:
                    continue
                old_bytes = len(row[preview_key].encode("utf-8"))
                if not old_bytes:
                    continue
                excess = row_bytes - MAX_OPEN_ROW_BYTES
                preview, clipped = text_preview(item[source], max(0, old_bytes - excess))
                row[preview_key] = preview
                row[truncated_key] = clipped
                row_bytes = len(canonical(row))
        if row_bytes > MAX_OPEN_ROW_BYTES:
            raise LimitExceeded("Work-item page row exceeds byte bound")
        return sort_key, row

    @staticmethod
    def closed_row(item: dict) -> tuple:
        """Build one bounded closed-page row and its newest-first key."""
        sort_key = (-_timestamp_order(item["updated_at"]), int(item["id"][3:], 16))
        row = {key: item[key] for key in ("id", "title", "status", "origin", "rev", "updated_at")}
        row["actor"] = item.get("actor")
        if not isinstance(row["actor"], str) or not row["actor"]:
            raise Denied("Closed work item lacks actor attribution")
        if "description" in item:
            preview, clipped = text_preview(item["description"], 1024)
            row["description_preview"] = preview
            row["description_truncated"] = clipped
        scope = item.get("scope")
        if scope is not None:
            preview, clipped = text_preview(scope, 1024)
            row["scope_preview"] = preview
            row["scope_truncated"] = clipped
        reason = item.get("reason")
        if reason is not None:
            preview, clipped = text_preview(reason, 1024)
            row["reason_preview"] = preview
            row["reason_truncated"] = clipped
        row["snapshot_id"] = item.get("snapshot_id")
        row["delivered_tree"] = item.get("delivered_tree")
        if "completion_policy" in item:
            row["completion_policy"] = item["completion_policy"]
        if item["status"] == "done" and recorded_policy(item) == VERIFIED_SNAPSHOT:
            if not isinstance(row["snapshot_id"], str) or not DIGEST.fullmatch(row["snapshot_id"]):
                raise Denied("Closed done item lacks snapshot evidence")
            if row["delivered_tree"] is not None and (
                    not isinstance(row["delivered_tree"], str)
                    or not DIGEST.fullmatch(row["delivered_tree"])):
                raise Denied("Closed done item carries an invalid delivery tree")
        elif row["snapshot_id"] is not None or row["delivered_tree"] is not None:
            raise Denied("Only completed work items carry snapshot evidence")
        if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
            raise LimitExceeded("Closed work-item page row exceeds byte bound")
        return sort_key, row

    def _active_rows(self, *, locked: bool, durable: bool = False):
        """Stream every active row in ID order."""
        with self._ledger.read("items", locked=locked, durable=durable) as db:
            yield from db.execute(
                "SELECT item, doing_since, blocked_since FROM items"
                " WHERE status IN ('open', 'doing', 'blocked') ORDER BY id")

    def _ci_unreachable_hints(self, *, role_name=None) -> dict:
        """Ask the adapter about current CI waiters without records.lock.

        The index query selects only the blocked CI waiters visible to the
        role, so no unrelated row is decoded. Each answer is keyed by the exact
        wait identity, so a changed record inside the lock cannot inherit a
        stale answer. An unreadable or unavailable index gives no hints, and
        locked liveness then stays pending without querying the adapter.
        """
        from . import ci_waits
        hints = {}
        sql = ("SELECT item, doing_since, blocked_since FROM items"
               " WHERE status='blocked' AND wait_kind IN ('ci', 'publication_ci')")
        params = ()
        if role_name is not None:
            sql += " AND (holder IS NULL OR holder=?)"
            params = (role_name,)
        sql += " ORDER BY id"
        try:
            with self._ledger.read("items", locked=False) as db:
                for entry in _rows(db, sql, params):
                    item, _ = self._decode(*entry)
                    # Defensive validation of decoded content; the SQL filter is
                    # what bounds the pre-scan to blocked CI waiters.
                    wait = item.get("wait")
                    if (item.get("status") != "blocked" or not isinstance(wait, dict)
                            or wait.get("kind") not in ("ci", "publication_ci")):
                        continue
                    if role_name is not None and item.get("holder") not in (None, role_name):
                        continue
                    reason = ci_waits.unreachable(self, item)
                    if reason is not None:
                        hints[ci_wait_identity(item)] = reason
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError):
            return {}
        return hints

    def _transition_ci_unreachable(self, item_id: str):
        """Gather one transition's CI hint from the target alone, outside any lock.

        ``None`` means the durable index could not be verified; an empty
        mapping means the target is not a blocked CI waiter. Otherwise the
        mapping holds one exact wait identity and its answer, where ``None``
        is a legitimate still-pending result from an adapter query.
        """
        from . import ci_waits
        bounded = (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError)
        try:
            item = self._plain(item_id, locked=False, durable=True)
        except bounded:
            return None
        wait = item.get("wait")
        if (item.get("status") != "blocked" or not isinstance(wait, dict)
                or wait.get("kind") not in ("ci", "publication_ci")):
            return {}
        try:
            reason = ci_waits.unreachable(self, item)
        except (*bounded, KeyError):
            # An unanswered adapter keeps the wait pending; only an
            # unverifiable index may justify refusing a locked CI waiter.
            reason = None
        return {ci_wait_identity(item): reason}

    def _page_open_impl(self, *, cursor, limit, readonly: bool, ready_only: bool = False,
                        role_name: str | None = None, order_fingerprint: bool = False,
                        blocked_only: bool = False, include_open_count: bool = False, writer_roles=None,
                        wait_resolution_role: str | None = None, _active_transform=None,
                        delivered_wait_resolution_tokens=None, wait_resolution_high_water=None,
                        ci_unreachable=None):
        """Collect the bounded open page from the index.

        Callers hold records.lock when ``readonly`` is False; the read-only
        path never locks or writes. Locked ready callers supply pre-gathered
        ``ci_unreachable`` hints so no adapter runs under that lock. Cursor,
        limit, ordering, overflow and byte
        bounds match :meth:`page_open`. Host-only opening orchestration
        can supply an active transform on the unlocked readonly path; public
        read entry points never do. The returned state is used before selection,
        counting and fingerprints, so transitions cannot leave a stale page.
        """
        if _active_transform is not None and not readonly:
            raise Denied("Lifecycle transform requires an unlocked read-only scan")
        cursor_key = _open_cursor_key(cursor)
        if delivered_wait_resolution_tokens is None:
            delivered_resolution_tokens = None
        else:
            if (not isinstance(delivered_wait_resolution_tokens, (list, tuple, set, frozenset))
                    or len(delivered_wait_resolution_tokens) > MAX_DELIVERED_WAIT_RESOLUTION_TOKENS
                    or any(not isinstance(token, str)
                           or _WAIT_RESOLUTION_TOKEN.fullmatch(token) is None
                           or not 1 <= int(token.rsplit(":", 1)[1]) <= MAX_REVISIONS
                           for token in delivered_wait_resolution_tokens)
                    or len(set(delivered_wait_resolution_tokens)) != len(delivered_wait_resolution_tokens)):
                raise Denied("Invalid delivered wait-resolution tokens")
            delivered_resolution_tokens = frozenset(delivered_wait_resolution_tokens)
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")
        active = self._active_rows(locked=not readonly)
        published_snapshot = None
        selected = []
        counts = {state: 0 for state in
                  ("absent", "pending", "due", "impossible", "receipt_bound", "terminal", "unknown")}
        total = 0
        open_count = 0
        eligible = 0
        unknown_blocked = 0
        ready_sum = 0
        wait_resolution_pending = False
        wait_resolution_undelivered = False
        delivered_resolution_current = set()
        boundary = (wait_resolution_high_water["token"]
                    if wait_resolution_high_water is not None else None)
        boundary_current = boundary is None
        resolution_sum = 0
        resolution_count = 0
        resolution_max = None
        order_sum = 0
        for entry in active:
            item, since = self._decode(*entry)
            if _active_transform is not None:
                transformed = _active_transform(item)
                if transformed is not item:
                    item, since = transformed, None
                if item["status"] not in ("open", "doing", "blocked"):
                    continue
            state = None
            if (wait_resolution_role is not None and item["status"] == "open"
                    and "wait_resolution" in item and item.get("holder") == wait_resolution_role):
                wait_resolution_pending = True
            view = {"item": item, **self._hold_projection(since), **blocked_age(item)}
            if len(canonical(view)) > MAX_PROJECTION_BYTES:
                raise LimitExceeded("Work-item projection exceeds byte bound")
            open_count += 1
            if order_fingerprint:
                leaf = canonical(["work-order-scope-v1", item["id"], item.get("scope")])
                order_sum = (order_sum + int(digest(leaf), 16)) % (1 << 256)
            if ready_only and role_name is not None:
                if item["status"] == "open":
                    if item.get("holder") not in (None, role_name):
                        continue
                elif item.get("holder") != role_name and not (
                        item["status"] == "blocked" and item.get("holder") is None):
                    continue
            # Operational effective status only: the raw ledger record stays
            # blocked with its wait, but holder-only waits have no external
            # resolver, so read projections treat the item as ordinary
            # held-open work (no wait diagnostics, open order). This runs
            # after role filtering because holder-only items are always held:
            # the holder passes both the blocked and the open eligibility
            # check while any other named role fails both, so no liveness
            # work is observable for items the role cannot see.
            if item["status"] == "blocked" and writer_roles is not None:
                wait_probe = item.get("wait")
                if (isinstance(wait_probe, dict) and wait_probe.get("kind") == "code_change"
                        and published_snapshot is None):
                    published_snapshot = self.snapshots.get()
                if holder_only(self, item, published_snapshot, writer_roles=writer_roles,
                               ci_unreachable=ci_unreachable) is not None:
                    item = {key: value for key, value in item.items()
                            if key not in ("wait", "blocked_since")}
                    item["status"] = "open"
            if (ready_only or blocked_only) and item["status"] == "blocked":
                wait = item.get("wait")
                if wait and wait["kind"] == "code_change" and published_snapshot is None:
                    published_snapshot = self.snapshots.get()
                state = liveness(self, wait, published_snapshot, item=item,
                                 writer_roles=writer_roles, ci_unreachable=ci_unreachable)
                if ready_only:
                    if state["state"] == "unknown":
                        unknown_blocked += 1
                        continue
                    if state["state"] not in ("due", "impossible", "absent", "terminal"):
                        continue
                if blocked_only:
                    counts[state["state"]] += 1
            elif blocked_only or (ready_only and item.get("wait") is not None):
                continue
            if ready_only:
                # Order-independent fingerprint of IDs and terminal bindings,
                # with no retained ID list. Domain-separated SHA-256 leaves summed
                # modulo 2**256, then hashed with scope and cardinality.
                leaf = b"ready-item-v1\0" + item["id"].encode()
                if item["status"] == "blocked" and state["state"] == "terminal":
                    leaf += canonical(state["evidence"])
                if "wait_resolution" in item:
                    leaf += b"\0wait-resolution-v1\0" + canonical(item["wait_resolution"])
                    holder = item.get("holder")
                    if (item["status"] == "open" and holder is not None
                            and (role_name is None or holder == role_name)):
                        wait_resolution_pending = True
                        if delivered_resolution_tokens is not None:
                            token = wait_resolution_token(item)
                            resolution_sum = (resolution_sum + int(digest(
                                b"wait-resolution-token-v1\0" + token.encode()), 16)) % (1 << 256)
                            resolution_count += 1
                            resolution_max = max(resolution_max or token, token)
                            boundary_current = boundary_current or token == boundary
                            if token in delivered_resolution_tokens:
                                delivered_resolution_current.add(token)
                            elif boundary is None or token > boundary:
                                wait_resolution_undelivered = True
                ready_sum = (ready_sum + int(digest(leaf), 16)) % (1 << 256)
            total += 1
            sort_key, row = self._open_row(item, view, wait_liveness=(
                state if ready_only and item["status"] == "blocked" else None))
            if blocked_only:
                sort_key = (state["state"] in ("pending", "receipt_bound"), *sort_key)
                row = {key: item[key] for key in ("id", "rev", "holder")}
                row.update(state)
            if ready_only and delivered_resolution_tokens is not None:
                unseen_resolution = (
                    item["status"] == "open" and item.get("holder") == role_name
                    and "wait_resolution" in item
                    and wait_resolution_token(item) not in delivered_resolution_tokens
                    and (boundary is None or wait_resolution_token(item) > boundary))
                # Proof-token order is the prefix receipt's order, independent
                # of mutable row timestamps and the ordinary ready-set order.
                sort_key = ((0, wait_resolution_token(item)) if unseen_resolution
                            else (1, *sort_key))
            if cursor_key is not None and sort_key <= cursor_key:
                continue
            eligible += 1
            candidate = (_ReverseKey(sort_key), sort_key, row)
            if len(selected) < limit:
                heapq.heappush(selected, candidate)
            elif sort_key < selected[0][1]:
                heapq.heapreplace(selected, candidate)
        rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
        overflow_count = max(0, eligible - len(rows))
        more = overflow_count > 0
        result = {"items": rows, "total": total, "cursor": cursor,
                  "next_cursor": _open_cursor_token(rows[-1]) if more and rows and not blocked_only else None,
                  "overflow_count": overflow_count, "truncated": more}
        if blocked_only:
            complete = counts["unknown"] == 0
            result = {"items": rows, "total": total if complete else None,
                    "counts": counts if complete else None,
                    "overflow_count": overflow_count, "truncated": more,
                    "coverage_complete": complete, "diagnostic": None if complete else "incomplete"}
            if include_open_count:
                result["open_count"] = open_count
        if ready_only or wait_resolution_role is not None:
            result["_wait_resolution_pending"] = wait_resolution_pending
        if ready_only and delivered_resolution_tokens is not None:
            result["_wait_resolution_undelivered"] = wait_resolution_undelivered
            result["_wait_resolution_delivered_current"] = sorted(delivered_resolution_current)
            shown_unseen = [wait_resolution_token(row) for row in rows
                            if row.get("holder") == role_name and "wait_resolution" in row
                            and wait_resolution_token(row) not in delivered_resolution_tokens
                            and (boundary is None or wait_resolution_token(row) > boundary)]
            # Only the selected earliest unseen rows extend the contiguous
            # delivered prefix. Out-of-order open-page proofs remain exact tokens.
            next_boundary = (max(shown_unseen) if shown_unseen else boundary)
            if not wait_resolution_undelivered:
                next_boundary = resolution_max
            result["_wait_resolution_high_water"] = {
                "signature": digest(canonical(["wait-resolution-set-v1", role_name,
                                                resolution_count, f"{resolution_sum:064x}"])),
                "token": next_boundary}
            result["_wait_resolution_boundary_current"] = boundary_current
        if ready_only:
            complete = unknown_blocked == 0
            result["unknown_blocked"] = unknown_blocked
            result["coverage_complete"] = complete
            result["signature"] = digest(canonical(
                ["ready-set-v1", total, f"{ready_sum:064x}"]))
        if len(canonical(result)) > MAX_OPEN_PAGE_BYTES:
            raise LimitExceeded("Work-item page exceeds byte bound")
        if order_fingerprint:
            return result, digest(canonical(["work-order-inventory-v1", total, f"{order_sum:064x}"]))
        return result

    def _page_closed_impl(self, *, limit, readonly: bool, include_items=False):
        """Collect the newest seeded terminal items from the index.

        Internal receipt projections can retain the current items of just the
        selected page.
        """
        if type(limit) is not int or not 0 <= limit <= MAX_CLOSED_PAGE_ROWS:
            raise Denied("Closed work-item page limit must be an integer in [0, 100]")
        with self._ledger.read("items", locked=not readonly) as db:
            total = _rows(db, "SELECT COUNT(*) FROM items WHERE status IN ('done', 'dropped')")[0][0]
            newest = _rows(db, "SELECT item FROM items WHERE status IN ('done', 'dropped') "
                               "ORDER BY updated_key DESC, id LIMIT ?", (limit,))
        selected = []
        for (raw,) in newest:
            item = json.loads(raw)
            sort_key, row = self.closed_row(item)
            selected.append((None, sort_key, row, item if include_items else None))
        rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
        overflow_count = max(0, total - len(rows))
        more = overflow_count > 0
        result = {"items": rows, "total": total,
                  "overflow_count": overflow_count, "truncated": more}
        if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
            raise LimitExceeded("Closed work-item page exceeds byte bound")
        if include_items:
            return result, [entry[3] for entry in sorted(selected, key=lambda value: value[1])]
        return result

    def page_changes_readonly(self, *, cursor=None, limit=25) -> dict:
        """Bounded coalesced current records, not an event history or live waits.

        Each call uses one persistent SQLite read snapshot. A sequence has a
        fixed high-water mark; a row updated past it is delivered on the next
        poll, even if it moved before being visited. Retain next_cursor on
        every response (including empty/completed pages) and upsert by ID/rev.
        Rebuilds invalidate cursors; pending intents refuse reads until repair.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")
        with self._ledger.read("items", locked=False, durable=True) as db:
            key, after, fence = ITEMS.change_position(db, cursor)
            found = _rows(db, "SELECT change_seq, item, doing_since, blocked_since FROM items "
                          "WHERE change_seq>? AND change_seq<=? ORDER BY change_seq LIMIT ?",
                          (after, fence, limit + 1))
            selected = []
            size = 1024  # Envelope and two bounded opaque cursors.
            position = after
            for seq, raw, doing_since, blocked_since in found[:limit]:
                item = json.loads(raw)
                row = {"item": item, "doing_since": doing_since,
                       "blocked_since": blocked_since}
                row_bytes = len(canonical(row))
                if row_bytes > MAX_PROJECTION_BYTES:
                    raise LimitExceeded("Work-item projection exceeds byte bound")
                if size + row_bytes + 1 > MAX_OPEN_PAGE_BYTES:
                    break
                selected.append(row)
                size += row_bytes + 1
                position = seq
            if found and not selected:
                raise LimitExceeded("Work-item change row exceeds page byte bound")
            more = len(found) > len(selected)
            next_cursor = (ITEMS.change_cursor(key, position, fence) if more
                           else ITEMS.change_cursor(key, fence))
            result = {"items": selected, "cursor": cursor, "next_cursor": next_cursor,
                      "has_more": more}
            if len(canonical(result)) > MAX_OPEN_PAGE_BYTES:
                raise LimitExceeded("Work-item page exceeds byte bound")
            return result

    def page_open_readonly(self, *, cursor=None, limit=25) -> dict:
        """Return the live open page without acquiring records.lock.

        Same schema, ordering, cursor, overflow and byte bounds as
        :meth:`page_open`, and never writes the index. A last-key cursor
        pages a live view and does not freeze membership.
        """
        return self._page_open_impl(cursor=cursor, limit=limit, readonly=True)

    def blocked_liveness_readonly(self, *, limit=25) -> dict:
        """Bounded diagnostic only; unknown inventory never becomes a zero.

        Uses the open-page row/byte limits and read-only scan (no lock,
        recovery or writes). Counts cover the full seeded scan, even when
        rows truncate. Problem/actionable rows precede pending/receipt-bound rows,
        with ledger order within each group. Unknown
        observations invalidate counts/total; malformed or unsafe inventory
        invalidates the whole view.
        The ledger, not this live projection, remains authoritative.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")
        try:
            return self._page_open_impl(cursor=None, limit=limit, readonly=True, blocked_only=True)
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError):
            return {"items": [], "total": None, "counts": None, "overflow_count": None,
                    "truncated": None,
                    "coverage_complete": False, "diagnostic": "unreadable"}

    def open_state_readonly(self, *, limit=25, writer_roles=None) -> dict:
        """Return the seeded open count and blocked liveness from one scan.

        The caller gets the same fail-closed diagnostic shape as
        :meth:`blocked_liveness_readonly` and an ``open_count`` derived from the
        same validated projections. Optional configured writer names reclassify
        holder-only waits as open work and annotate self-resolvable causes on the
        remaining blocked rows without changing their liveness states or ordering.
        A read error reports both as unavailable;
        it never falls back to a zero or an unvalidated cache.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")
        try:
            blocked = self._page_open_impl(cursor=None, limit=limit, readonly=True,
                                           blocked_only=True, include_open_count=True,
                                           writer_roles=writer_roles)
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError):
            return {"open_count": None,
                    "blocked_work": {"items": [], "total": None, "counts": None,
                                     "overflow_count": None, "truncated": None,
                                     "coverage_complete": False, "diagnostic": "unreadable"}}
        open_count = blocked.pop("open_count")
        return {"open_count": open_count, "blocked_work": blocked}

    def page_closed(self, *, cursor=None, limit=25, status=None, origin=None,
                    closed_after=None, closed_before=None) -> dict:
        """Return a bounded cursor page of seeded terminal work items.

        Items are ordered by status, then newest ``updated_at`` first, then
        ascending ID. The exclusive ``cursor``
        carries the last row's status, update-order key and ID so callers reach
        the whole set. ``status`` (done or dropped), ``origin`` (exact origin text),
        ``closed_after`` and ``closed_before`` (timezone-aware timestamps)
        narrow matching; they never cap ``total``. Each row is at most
        MAX_CLOSED_ROW_BYTES and the page at most MAX_CLOSED_PAGE_BYTES. Reads
        never lock or mutate. Malformed cursors, filters or committed rows fail
        closed.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_CLOSED_PAGE_ROWS:
            raise Denied("Closed work-item page limit must be an integer in [1, 100]")
        cursor_key = _closed_cursor_key(cursor)
        if status is not None and (not isinstance(status, str) or status not in _CLOSED_STATUSES):
            raise Denied("Closed work-item status filter must be done or dropped")
        if origin is not None and (not isinstance(origin, str) or not origin
                                   or len(origin) > MAX_OPEN_CURSOR_CHARS):
            raise Denied("Invalid closed work-item origin filter")
        after_key = _timestamp_order(closed_after) if closed_after is not None else None
        before_key = _timestamp_order(closed_before) if closed_before is not None else None
        if after_key is not None and before_key is not None and after_key > before_key:
            raise Denied("Closed work-item time range is inverted")

        clauses = ["status IN ('done', 'dropped')"]
        filters: list = []
        if status is not None:
            clauses.append("status = ?")
            filters.append(status)
        if origin is not None:
            clauses.append("origin = ?")
            filters.append(origin)
        if after_key is not None:
            clauses.append("updated_key >= ?")
            filters.append(after_key)
        if before_key is not None:
            clauses.append("updated_key <= ?")
            filters.append(before_key)
        where = " AND ".join(clauses)

        after_cursor = ""
        cursor_params: list = []
        if cursor_key is not None:
            after_cursor = (" AND (status > ? OR (status = ? AND "
                            "(updated_key < ? OR (updated_key = ? AND id > ?))))")
            cursor_params = [cursor_key[0], cursor_key[0], cursor_key[1],
                             cursor_key[1], cursor_key[2]]

        with self._ledger.read("items", locked=False) as db:
            total = _rows(db, "SELECT COUNT(*) FROM items WHERE " + where,
                          tuple(filters))[0][0]
            eligible = total
            if cursor_key is not None:
                eligible = _rows(db, "SELECT COUNT(*) FROM items WHERE "
                                 + where + after_cursor,
                                 tuple(filters + cursor_params))[0][0]
            query = "SELECT item FROM items WHERE " + where + after_cursor
            params = filters + cursor_params
            query += " ORDER BY status ASC, updated_key DESC, id ASC LIMIT ?"
            params.append(limit + 1)
            found = _rows(db, query, tuple(params))

        selected = []
        for (raw,) in found[:limit]:
            item = json.loads(raw)
            _sort_key, row = self.closed_row(item)
            selected.append(row)
        more = len(found) > limit
        result = {"items": selected, "total": total, "cursor": cursor,
                  "next_cursor": _closed_cursor_token(selected[-1]) if more and selected else None,
                  "overflow_count": max(0, eligible - len(selected)),
                  "truncated": more, "coverage_complete": True}
        if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
            raise LimitExceeded("Closed work-item page exceeds byte bound")
        return result

    def page_closed_readonly(self, *, limit=10) -> dict:
        """Return the newest seeded terminal items without lock or recovery.

        Schema: ``items`` newest ``updated_at`` first with a stable ascending
        ID tie-break, plus ``total`` seeded closed count, ``overflow_count``
        (closed rows beyond this page) and ``truncated`` (exactly
        ``overflow_count > 0``). Limits are 0..100 (exact int); 0 returns no
        rows but still reports the total. Each row carries ``id``, ``title``,
        ``status`` (``done`` or ``dropped``), ``origin``, ``rev``,
        ``updated_at``, ``actor``, a bounded ``reason_preview`` with
        ``reason_truncated`` when a reason is stored, ``snapshot_id``
        (the verified snapshot for ``done``, null for ``dropped``) and
        ``delivered_tree`` (the digest of the tree that carries the delivery,
        null for ``dropped`` and for records written before the claim
        existed). Rows are
        at most 8192 canonical bytes and the complete response at most 1 MiB.
        """
        return self._page_closed_impl(limit=limit, readonly=True)

    def reopen(self, item_id: str, *, expected_rev, reason, actor) -> dict:
        """Append an audited completion challenge; caller authenticates/authorizes actor.

        Replays recorded contracts, not current policy. Old proof remains only
        in history. The marker binds this event's actor, reason and source CAS.
        The separate runtime capability and operator allowlist gate access.
        """
        identifier(actor)
        if type(expected_rev) is not int or expected_rev < 1:
            raise Denied("Expected revision must be a positive integer")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise Denied("Completion reopen reason must be nonblank text of 1-4000 characters")
        with record_lock(self.root):
            revisions = self._recover_temporary_writes(item_id)
            current = self._get(item_id, history_revisions=revisions)
            if current["rev"] != expected_rev:
                raise Denied("Work item changed; re-read before reopening")
            if current["status"] != "done":
                raise Denied("Completion reopening requires a done work item")
            if current["rev"] > MAX_REOPEN_REV:
                raise Denied("Completion reopening requires revision headroom: current rev must be <= 252")
            record = {**current, "rev": current["rev"] + 1, "status": "open",
                      "updated_at": now(), "actor": actor,
                      "transition": {"from": "done", "to": "open"},
                      "holder": None, "wait": None, "blocked_since": None,
                      "snapshot_id": None, "verification": None, "delivered_tree": None,
                      "verification_scope": None, "scope": current.get("scope"),
                      "delivery_schema": current.get("delivery_schema", 1), "delivery": None,
                      "reason": reason,
                      "reopen": {"challenged_rev": current["rev"], "challenger": actor,
                                 "reason_digest": digest(canonical(reason))}}
            for key in ("completion_policy", "completion_evidence", "wait_resolution"):
                record.pop(key, None)
            self._validate_transition(current, record)
            if len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Work-item event exceeds byte bound")
            from .work_item_events import index_terminal, terminal_marker
            marker = terminal_marker(self)
            with self.committing([item_id]):
                write_json(self.root / "work-items" / item_id / f"{record['rev']}.json",
                           record, exclusive=True)
            index_terminal(self, record, terminal=False, marker=marker)
            self._wake_terminal()
            return record

    def transition(self, item_id: str, *, expected_rev, status, actor, reason=None,
                   wait=None, snapshot_id=None, scope=None, smoke_refs=None, evidence=None, _resolve_wait=False) -> dict:
        """Append one immutable full-state CAS event; terminal metadata wakes subscribers.

        The event states the scope in force for the item at that revision: a
        supplied scope sets it from this revision on, an omitted one inherits
        the previous revision's scope, a scope is never cleared, and a
        completion may not change the scope it inherits. Actor is trusted
        caller attribution, not authorization. Done records the resolved
        completion contract. The default validates an
        exact pre-existing delivery revision, never the live workspace; configured
        holder attestation instead records a bounded inline attachment. A doing
        transition carrying snapshot_id records that delivery binding (event
        snapshot_id stays null); done carrying snapshot_id must equal it.
        Delivery binding validates retained membership under publication.lock
        and the owning project's record lock. Completion
        copies valid embedded proof or
        pins an independently retained acceptance receipt by immutable ID.
        All validation precedes the exclusive atomic
        write. Stale CAS writes nothing; cancellation/process death releases
        the shared project lock.
        """
        policy = self.completion_evidence.for_actor(actor)
        if evidence is not None and status != "done":
            raise Denied(f"{policy}: inline evidence is accepted only for done")
        if status == "done":
            if policy == VERIFIED_SNAPSHOT and evidence is not None:
                raise Denied(f"{policy}: inline evidence is not accepted")
            if policy == HOLDER_ATTESTATION:
                validate_attestation(evidence, self.completion_evidence.maximum_evidence_bytes)
                if snapshot_id is not None:
                    raise Denied(f"{policy}: snapshot-related fields must be null")
        if type(expected_rev) is not int or expected_rev < 1:
            raise Denied("Expected revision must be a positive integer")
        # Publication membership and this record append stay ordered together.
        # Smoke retention serializes candidate admission and then scans this
        # project's record lock, so no machine-wide lock belongs on this write.
        binding_request = (status == "doing" and snapshot_id is not None)
        publication_wait = (status == "blocked" and isinstance(wait, dict)
                            and wait.get("kind") == "publication_ci")
        # Peek at the target alone before locking: a held CI waiter needs an
        # adapter answer, and that answer must be gathered without records.lock.
        # The peek never scans other items or repairs the index. An unavailable
        # durable index yields an unverifiable sentinel, so locked liveness
        # cannot silently mistake a held waiter for a pending one.
        ci_unreachable = self._transition_ci_unreachable(item_id)
        publication = (lock(self.snapshots.root / "locks" / "publication.lock")
                       if binding_request or publication_wait or _resolve_wait else contextlib.nullcontext())
        with publication, record_lock(self.root):
            history_revisions = self._recover_temporary_writes(item_id)
            if expected_rev == MAX_REVISIONS:
                if _resolve_wait:
                    raise Denied("Automatic resolution cannot retire an item")
                return work_item_retirement.retire(
                    self, item_id, expected_rev=expected_rev, status=status, actor=actor, reason=reason,
                    history_revisions=history_revisions, wait=wait, snapshot_id=snapshot_id,
                    scope=scope, smoke_refs=smoke_refs)
            blocked_waiter = {}
            current, _, blocked = self._current_hold(
                item_id, history_revisions=history_revisions, blocked_waiter=blocked_waiter)
            if blocked is not None:
                current = {**current, "blocked_since": blocked}
            if current["rev"] != expected_rev:
                raise Denied("Work item changed; re-read before transitioning")
            resolution = None
            if (status == "done" and policy == HOLDER_ATTESTATION
                    and current["status"] != "dropped" and actor != current["holder"]):
                raise Denied(f"{policy}: ordinary completion requires the current holder")
            if current["status"] == "open" and current["holder"] not in (None, actor):
                raise Denied("Held-open work belongs to its holder")
            if _resolve_wait:
                if (current["status"] != "blocked" or current["holder"] != actor
                        or status != "open" or any(value is not None for value in
                            (wait, snapshot_id, scope, smoke_refs))):
                    raise Denied("Automatic resolution requires the current held waiter")
                from .work_item_resolution import observe
                resolution = observe(self, current)
                if resolution is None:
                    raise Denied("Wait has no exact resolution evidence")
            if status == "blocked" and wait is None:
                raise Denied("New blocked transitions require a supported observable wait")
            if blocked_waiter:
                blocked_wait = blocked_waiter.get("wait")
                if (isinstance(blocked_wait, dict)
                        and blocked_wait.get("kind") in ("ci", "publication_ci")):
                    if ci_unreachable is None:
                        raise Denied("Cannot verify CI wait reachability: work-item change index "
                                     "unavailable; retry after locked recovery")
                    if (current["status"] == "blocked"
                            and ci_wait_identity(blocked_waiter) not in ci_unreachable):
                        raise Denied("Cannot verify CI wait reachability: the blocked wait changed; "
                                     "re-read and retry")
                # A stale waiter from an earlier blocked episode has no pre-read
                # identity, but it must never trigger a direct adapter query
                # while records.lock is held.
                hints = ci_unreachable if ci_unreachable is not None else {}
                state = liveness(self, blocked_wait, None, item=blocked_waiter,
                                 ci_unreachable=hints)
                needs_open = state["state"] in ("terminal", "impossible")
                if needs_open and (status == "blocked" or (status == "doing" and current["status"] == "blocked")):
                    raise Denied(f"Reopen blocked work with a reason before renewing its wait: {state['reason']}")
                if (status == "open" and needs_open
                        and (not isinstance(reason, str) or not reason.strip())):
                    raise Denied("Reopening blocked work requires a reason")
            registered = None
            if wait is not None:
                if status != "blocked":
                    raise Denied("Only blocked work items carry waits")
                if isinstance(wait, dict) and wait.get("kind") in ("ci", "publication_ci"):
                    if not self.vcs.get("poll_enabled") or not self.vcs.get("command"):
                        raise Denied("CI waits require automatic VCS polling and an adapter")
                    raw_wait = {k: v for k, v in wait.items() if k != "smoke_refs"}
                    if wait["kind"] == "publication_ci":
                        from .ci_waits import normalize_publication
                        registered = normalize_publication(raw_wait, snapshots=self.snapshots,
                                                           branch=self.vcs.get("ci_branch"))
                    else:
                        from .ci_waits import normalize
                        registered = normalize(self.root, raw_wait)
                    if "smoke_refs" in wait:
                        registered["smoke_refs"] = validate_refs(wait["smoke_refs"])
                elif isinstance(wait, dict) and wait.get("kind") == "proposal_merged":
                    if (not self.vcs.get("poll_enabled")
                            or not self.vcs.get("poll_proposals_enabled")
                            or not self.vcs.get("command")):
                        raise Denied("Proposal waits require proposal polling, an adapter and a queue")
                    from .proposal_waits import normalize
                    registered = normalize(self, {k: v for k, v in wait.items()
                                                  if k != "smoke_refs"})
                    if "smoke_refs" in wait:
                        registered["smoke_refs"] = validate_refs(wait["smoke_refs"])
                elif isinstance(wait, dict) and wait.get("kind") == "object_resolved":
                    from .object_waits import normalize, require_acyclic
                    refs = validate_refs(wait.get("smoke_refs", []))
                    registered = normalize({k: v for k, v in wait.items() if k != "smoke_refs"})
                    require_acyclic(self, item_id, registered["subject"])
                    if "smoke_refs" in wait:
                        registered["smoke_refs"] = refs
                else:
                    registered = normalize_wait(wait, self._published_digest)
            verification = None
            verification_scope = None
            delivered_tree = None
            if status == "done" and policy == VERIFIED_SNAPSHOT:
                delivery = current.get("delivery")
                if not delivery or snapshot_id != delivery["snapshot_id"]:
                    raise Denied("verified_snapshot_v1: Completion requires a prior exact snapshot delivery binding")
                snapshot = self.snapshots.get(snapshot_id)
                verification = snapshot.get("verification")
                if (current["status"] == "dropped" or not isinstance(verification, dict)
                        or verification.get("passed") is not True
                        or verification.get("code_digest") != snapshot["code_digest"]):
                    diagnostic = None
                    if self.effective_verification is not None:
                        # Explicit proof-only callbacks remain authoritative for
                        # standalone callers and existing project integrations.
                        verification = self.effective_verification(snapshot)
                    elif self.effective_verification_projection is not None:
                        verification, diagnostic = self.effective_verification_projection(snapshot)
                    else:
                        verification = None
                    if not isinstance(verification, dict):
                        message = "verified_snapshot_v1: Completion snapshot must have passing digest-bound verification"
                        # Emit only fixed reason tokens, never receipt contents,
                        # paths, or arbitrary diagnostic text from a callback.
                        if isinstance(diagnostic, dict):
                            reason = diagnostic.get("reason")
                            if reason in ("nonempty_cache_input", "cache_input_unbound",
                                          "cache_input_unclassified", "cache_owned"):
                                message += f": {reason}"
                                if reason == "nonempty_cache_input" and diagnostic.get("allow_nonempty_cache") is False:
                                    message += " (allow_nonempty_cache=false)"
                        raise Denied(message)
                    from .acceptance import get
                    receipt = get(self.root, verification.get("receipt_id"))
                    verification_scope = digest(canonical(receipt["scope"]))
                delivered_tree = delivery["code_digest"]
            elif binding_request:
                pass
            elif snapshot_id is not None:
                raise Denied("Only completed work items carry snapshot evidence")
            # The scope in force is inherited from the previous revision. A
            # supplied scope states a change of it, which is this revision's
            # own scope-change event; a closure may not change it.
            effective_scope = current.get("scope")
            if scope is None:
                in_force = effective_scope
            else:
                validate_scope(scope)
                if status == "done" and scope != effective_scope:
                    raise Denied(f"{policy}: Completion cannot change the accepted scope; "
                                 "record the scope change in an earlier transition")
                if scope != effective_scope and (reason is None or not reason.strip()):
                    raise Denied("A scope change requires a reason")
                in_force = scope
            delivery = current.get("delivery")
            stored_snapshot_id = snapshot_id
            if binding_request:
                if current["status"] != "doing" or status != "doing" or in_force != effective_scope:
                    raise Denied("Delivery binding requires doing-to-doing with unchanged scope")
                snapshot = self.snapshots.get(snapshot_id)
                if snapshot_id not in self.snapshots._history_ids():
                    raise Denied("Delivery snapshot must be retained published history")
                delivery = {"snapshot_id": snapshot_id, "code_digest": snapshot["code_digest"],
                            "scope_digest": self._scope_digest(in_force), "binding_rev": current["rev"] + 1}
                stored_snapshot_id = None
            record = {**current, "rev": current["rev"] + 1, "status": status,
                      "updated_at": now(), "actor": actor,
                      "transition": {"from": current["status"], "to": status},
                      "holder": actor if status == "doing" else
                          (current["holder"] or actor) if status == "blocked" else None,
                      "reason": reason, "wait": registered, "snapshot_id": stored_snapshot_id,
                      "verification": verification, "delivered_tree": delivered_tree,
                      "scope": in_force, "delivery_schema": 1, "delivery": delivery,
                      "verification_scope": verification_scope}
            if status == "done":
                record["completion_policy"] = policy
                record["completion_evidence"] = evidence
            record.pop("reopen", None)
            record.pop("wait_resolution", None)
            if resolution is not None:
                record["holder"] = current["holder"]
                record["wait_resolution"] = {
                    **resolution, "wait_rev": current["rev"],
                    "wait_digest": digest(canonical(current["wait"])),
                    "resolved_at": record["updated_at"]}
            record["blocked_since"] = blocked_start(current.get("blocked_since"), record)
            effective_smoke_refs = (current.get("smoke_refs") if smoke_refs is None else smoke_refs)
            if effective_smoke_refs is not None:
                validate_refs(effective_smoke_refs)
                require_pairs(self.root, effective_smoke_refs)
                record["smoke_refs"] = effective_smoke_refs
            if registered is not None:
                require_pairs(self.root, registered.get("smoke_refs", []))
            self._validate_transition(current, record)
            # A same-status doing revision by another role transfers the
            # holder, so it is a new claim against that role's active slots.
            if (status == "doing"
                    and (current["status"] != "doing" or current["holder"] != actor)):
                self._require_doing_capacity(actor)
            if len(canonical(record)) > (MAX_EVENT_BYTES if resolution is not None else PREVIEW_BYTES):
                raise LimitExceeded("Work-item event exceeds byte bound")
            marker = None
            if status in ("done", "dropped"):
                from .work_item_events import terminal_marker, validate_terminal_payload
                validate_terminal_payload(self, record)
                marker = terminal_marker(self)
            with self.committing([item_id]):
                write_json(self.root / "work-items" / item_id / f"{record['rev']}.json",
                           record, exclusive=True)
            if status in ("done", "dropped"):
                from .work_item_events import index_terminal
                index_terminal(self, record, marker=marker)
                self._wake_terminal()
            return record

    def _wake_terminal(self):
        # Non-authoritative hint for non-recursive directory watches. Admission
        # and event delivery always read committed state, never metadata.
        with contextlib.suppress(OSError, NotImplementedError):
            os.utime(self.root / "work-items", None, follow_symlinks=False)
