"""Append-audited work-item store with bounded indexed reads; no scheduling.

Seeds are immutable direct creation records, accept/modify decision events or
hash-bound legacy sidecars. Each successful CAS appends one full state under
insights.lock, never a pointer, after replaying and validating the item's
whole chain. Current-state reads come only from the ledger index
(:mod:`mizu.ledger`), which every write updates in its critical section; no
read replays history. Full-chain validation runs at write time, when the
index is built from the files, and in :meth:`WorkItems.audit`. Origin
staleness, hold ages and wait liveness are computed from current records on
every read. Retrying a committed CAS requires a re-read. Same-status events
require a scope/reference change, held-open release, or delivery binding; only
a proven forward dropped-to-done correction may follow a terminal event.
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
from . import insight_archive, work_item_retirement
from .work_item_waits import stored_wait, liveness, blocked_start, blocked_age
from .work_item_resolution import MAX_RESOLUTION_BYTES
from .config import (DEFAULT_HISTORY_TEMPORARIES, MAX_HISTORY_TEMPORARIES,
                     PublicationEvidence, TOML_INTEGER_MAX)
from .errors import Denied, LimitExceeded, OpenWorkItemsFull
from .fs import (DIGEST, PREVIEW_BYTES, canonical, digest, identifier, lock, now,
                 safe_read, sync_dir, text_preview, write_json)
from .insights import _decision_key, _validate_keyed_substantive_event
from .platform import IS_WINDOWS
from .ledger import rows as _rows
from . import insight_index
from .work_item_index import ITEMS
from .work_item_seed import (DESCRIPTION_UNSET, ITEM_ID, SCOPE_UNSET, SEED_FIELDS, make_seed,
                             task_id, task_seeds, validate_origin, validate_scope, validate_seed,
                             validate_tasks)

MAX_REVISIONS = 256  # Includes the immutable seed at revision 1.
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
_ORIGIN_COLUMNS = "n.id, n.valid, n.rev, d.insight_id, d.rev, d.action, d.linked"
_ORIGIN_SELECT = "SELECT " + _ORIGIN_COLUMNS
_ORIGIN_JOIN = ("LEFT JOIN insights n ON n.id = {key} "
                "LEFT JOIN decisions d ON d.insight_id = {key}")
_OPEN_CURSOR_TIME = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")


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
    def __init__(self, root: Path, snapshots, insights, *, vcs=None, effective_verification=None,
                 doing_limit=1, doing_limits=None, stale_seconds=86400, record_kinds=None,
                 blocked_max_age_seconds=604800,
                 effective_verification_projection=None, operator_answer_lookup=None,
                 publication_evidence: PublicationEvidence | None = None,
                 history_temporaries: int = DEFAULT_HISTORY_TEMPORARIES):
        self.root, self.snapshots, self.insights = root, snapshots, insights
        self.operator_answer_lookup = operator_answer_lookup
        self.vcs = vcs or {}
        self.publication_evidence = (publication_evidence if publication_evidence is not None
                                     else PublicationEvidence())
        self.record_kinds = record_kinds or {}
        self.effective_verification = effective_verification
        # Optional proof+diagnostic callback; legacy proof-only callers keep
        # their existing contract. The selected callback runs once per attempt.
        self.effective_verification_projection = effective_verification_projection
        self.doing_limit, self.doing_limits = doing_limit, dict(doing_limits or {})
        self.stale_seconds = stale_seconds
        if type(blocked_max_age_seconds) is not int or not 1 <= blocked_max_age_seconds <= TOML_INTEGER_MAX:
            raise Denied("Work-item blocked maximum age must be a positive TOML integer")
        self.blocked_max_age_seconds = blocked_max_age_seconds
        if (type(history_temporaries) is not int
                or not 1 <= history_temporaries <= MAX_HISTORY_TEMPORARIES):
            raise Denied(f"Work-item history temporary limit must be in [1, {MAX_HISTORY_TEMPORARIES}]")
        self.history_temporaries = history_temporaries
        self._ledger = insights.ledger
        self._ledger.attach(ITEMS, self)
        if any(type(value) is not int or not 1 <= value <= MAX_OPEN_PAGE_ROWS
               for value in (doing_limit, *self.doing_limits.values())):
            raise Denied("Work-item doing limit must be an integer in [1, 100]")
        if type(stale_seconds) is not int or not 1 <= stale_seconds <= 31536000:
            raise Denied("Work-item stale threshold must be an integer in [1, 31536000]")

    def _require_doing_capacity(self, actor: str) -> None:
        """Count the actor's doing items in the index; caller holds insights.lock."""
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

    def require_open_capacity(self, requested_delta: int) -> None:
        """Check a whole new-seed transaction while holding insights.lock.

        Zero-seed decisions and a disabled cap do not read the index.
        Existing over-limit work stays unchanged.
        """
        limit = self.insights.open_work_items
        if not requested_delta or not limit:
            return
        current_count = self.count_open()
        if current_count + requested_delta > limit:
            raise OpenWorkItemsFull(current_count=current_count, limit=limit,
                                    requested_delta=requested_delta)

    def _count_open(self, *, locked: bool) -> int:
        with self._ledger.read("items", locked=locked) as db:
            return _rows(db, "SELECT COUNT(*) FROM items WHERE status IN ('open', 'doing', 'blocked')")[0][0]

    def count_open(self) -> int:
        """Count seeded open/doing/blocked work while holding insights.lock."""
        return self._count_open(locked=True)

    def count_open_readonly(self) -> int:
        """Count seeded open/doing/blocked work without taking the lock or writing."""
        return self._count_open(locked=False)

    def _read(self, name: str) -> dict:
        event = name.startswith("work-items/") and _REVISION_NAME.fullmatch(Path(name).name)
        raw = safe_read(self.root, name, MAX_EVENT_BYTES if event else PREVIEW_BYTES)
        try:
            value = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError) as exc:
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

    def _direct_record(self, item_id: str) -> dict | None:
        directory = self.root / "work-items" / item_id
        if not _directory(directory.parent) or not _directory(directory):
            return None
        create = directory / "create.json"
        try:
            create.lstat()
        except FileNotFoundError:
            return None
        try:
            (directory / "legacy.json").lstat()
        except FileNotFoundError:
            pass
        else:
            raise Denied("Direct work item carries legacy reconciliation")
        decision = self.root / "decision-history" / f"{item_id[3:]}.json"
        if _directory(decision.parent):
            try:
                decision.lstat()
            except FileNotFoundError:
                pass
            else:
                raise Denied("Direct work-item ID collides with a decision event")
        record = self._read(f"work-items/{item_id}/create.json")
        if set(record) - {"requested_smoke_refs"} != {"kind", "actor", "idempotency_key", "work_item"} or record["kind"] != "direct":
            raise Denied("Invalid direct work-item creation record")
        seed = validate_seed(record["work_item"])
        if "requested_smoke_refs" in record:
            if record["requested_smoke_refs"] is not None:
                validate_refs(record["requested_smoke_refs"])
            if "smoke_refs" not in seed or not set(record["requested_smoke_refs"] or []).issubset(seed["smoke_refs"]):
                raise Denied("Invalid inherited smoke reference request")
        if (self._create_id(record["actor"], record["idempotency_key"]) != item_id
                or seed["id"] != item_id
                or seed["origin"] is not None and seed["origin"].get("work_item_id") == item_id):
            raise Denied("Invalid direct work-item binding")
        return record

    def create_seed(self, item_id: str, title: str, origin, stamp: str, *,
                    actor=None, idempotency_key=None, decision_key=None,
                    task_specs=None, task_index=None, wait=None,
                    description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, smoke_refs=None) -> dict:
        """Preflight a new immutable seed while the caller holds insights.lock.

        Validate the source's ID binding, an empty safe storage slot, the
        optional description and accepted scope, and the exact live origin
        revision. Recognized regular orphan temporaries are
        recovered (removed without promotion) before the emptiness check; any
        numeric revision history, direct/legacy record, unknown entry, or
        unsafe/ambiguous temporary still fails closed. This does not commit:
        direct callers commit create.json; decisions embed the seed in their
        atomic event. The committing caller checks capacity for the entire
        transaction; this preflight does not reserve slots. Existing commits
        must use validate_seed_replay instead.
        """
        if smoke_refs is not None:
            validate_refs(smoke_refs)
        if wait is not None and (task_specs is None or decision_key is None):
            raise Denied("Only decision task seeds carry waits")
        if decision_key is None and origin is not None and "insight_id" in origin:
            insight = self.insights.read(origin["insight_id"])
            inherited = {ref for entry in insight.get("evidence", [])
                         for ref in validate_refs(entry.get("smoke_refs", []))}
            if inherited:
                smoke_refs = sorted(inherited | set(validate_refs(smoke_refs or [])))
        seed = make_seed(item_id, title, origin, stamp, description=description, scope=scope,
                         wait=wait, smoke_refs=smoke_refs)
        if smoke_refs is not None:
            require_pairs(self.root, smoke_refs)
        if wait is not None:
            require_pairs(self.root, wait.get("smoke_refs", []))
        if task_specs is not None:
            validate_tasks(task_specs)
            if type(task_index) is not int or not 0 <= task_index < len(task_specs):
                raise Denied("Invalid task index")
        if decision_key is not None:
            if (actor is not None or idempotency_key is not None
                    or origin is None or "insight_id" not in origin
                    or decision_key != _decision_key(origin["insight_id"], origin["rev"])
                    or item_id != (task_id(decision_key, task_specs, task_index)
                                   if task_specs is not None else f"wi_{decision_key}")):
                raise Denied("Invalid decision work-item binding")
        elif item_id != self._create_id(actor, idempotency_key):
            raise Denied("Invalid direct work-item binding")
        directory = self.root / "work-items" / item_id
        if _directory(directory.parent) and _directory(directory):
            revisions = self._history_revisions(item_id, recover=True)
            if revisions:
                raise Denied("Work-item creation has pre-existing history")
            for name in ("create.json", "legacy.json"):
                try:
                    (directory / name).lstat()
                except FileNotFoundError:
                    continue
                else:
                    raise Denied("Work-item creation has pre-existing history")
            # _history_revisions already rejected unknown names and
            # unsafe/ambiguous temporaries; a recovered orphan leaves an empty
            # slot that may proceed to commit.
        decision = self.root / "decision-history" / f"{item_id[3:]}.json"
        if _directory(decision.parent):
            try:
                decision.lstat()
            except FileNotFoundError:
                pass
            else:
                raise Denied("Work-item ID collides with a decision event")
        if origin is not None:
            if "insight_id" in origin:
                revision = self._origin_revision(origin["insight_id"])
            else:
                parent_id = origin["work_item_id"]
                if parent_id == item_id:
                    raise Denied("Work item cannot be its own parent")
                try:
                    revision = self._get(parent_id, history_revisions=
                                         self._recover_temporary_writes(parent_id))["rev"]
                except FileNotFoundError:
                    raise Denied("Parent work item is missing") from None
            if revision != origin["rev"]:
                raise Denied("Work-item origin revision is missing or changed")
        return seed

    def validate_seed_replay(self, seed: dict, *, seed_event: dict) -> None:
        """Validate committed seed/storage/chain under insights.lock, not live origin.

        Recognized atomic-write leftovers may be recovered as on normal reads.
        Valid transitions never alter the immutable creation result.
        """
        validate_seed(seed, decision_task="work_item_specs" in seed_event)
        item_id = seed["id"]
        revisions = self._recover_temporary_writes(item_id)
        # An embedded decision seed must not hide a colliding direct record.
        self._direct_record(item_id)
        for index, record in enumerate(self._iter_chain(
                item_id, seed_event=seed_event, history_revisions=revisions)):
            if index == 0 and record != seed:
                raise Denied("Conflicting work-item seed replay")

    def create(self, *, idempotency_key, title, actor, origin=None,
               description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, smoke_refs=None) -> dict:
        """Commit one immutable seed; exact retries return the original record.

        Keys are actor-scoped, 1-128 characters. Reference revisions must be
        current at first commit; retries compare the immutable request before
        consulting live references. The optional description and accepted
        scope are part of that immutable request. Caller holds no lock;
        runtime binds actor.
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
                    existing = self._direct_record(item_id)
            if existing is not None:
                seed = make_seed(item_id, title, origin, existing["work_item"]["created_at"],
                                 description=description, scope=scope,
                                 smoke_refs=existing["work_item"].get("smoke_refs") if "requested_smoke_refs" in existing else smoke_refs)
                record = {"kind": "direct", "actor": actor, "idempotency_key": idempotency_key,
                          "work_item": seed}
                if "requested_smoke_refs" in existing:
                    record["requested_smoke_refs"] = smoke_refs
                if existing != record:
                    raise Denied("Conflicting work-item creation retry")
                self.validate_seed_replay(seed, seed_event=existing)
                return {"record": existing, "created": False}
            seed = self.create_seed(item_id, title, origin, now(), actor=actor,
                                    idempotency_key=idempotency_key, description=description,
                                    scope=scope, smoke_refs=smoke_refs)
            record = {"kind": "direct", "actor": actor, "idempotency_key": idempotency_key,
                      "work_item": seed}
            if seed.get("smoke_refs") != smoke_refs and origin is not None and "insight_id" in origin:
                record["requested_smoke_refs"] = smoke_refs
            self.require_open_capacity(1)
            directory.mkdir(parents=True, exist_ok=True)
            with self.committing([item_id]):
                write_json(directory / "create.json", record, exclusive=True)
            return {"record": record, "created": True}

    def _iter_item_events_with_revisions(self, *, recover: bool):
        task_sources = set()
        ambiguous_task_sources = set()
        for event_id, event in self._iter_decision_events(recover=recover):
            if "work_items" in event or "work_item_specs" in event:
                for seed in task_seeds(event):
                    if seed["id"] in task_sources:
                        ambiguous_task_sources.add(seed["id"])
                    else:
                        task_sources.add(seed["id"])
                    yield seed["id"][3:], event, None
            else:
                yield event_id, event, None
        directory = self.root / "work-items"
        if not _directory(directory):
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                if ITEM_ID.fullmatch(entry.name) is None or not _directory(directory / entry.name):
                    raise Denied("Invalid work-item directory entry")
                try:
                    (directory / entry.name / "create.json").lstat()
                except FileNotFoundError:
                    # A failed first create can leave only a temporary or empty
                    # directory. Validate/recover it, but never invent a seed.
                    decision = self.root / "decision-history" / f"{entry.name[3:]}.json"
                    try:
                        decision.lstat()
                    except FileNotFoundError:
                        # Reuse only this scan's fully validated task sources.
                        if entry.name in ambiguous_task_sources:
                            raise Denied("Ambiguous task seed sources")
                        revisions = self._history_revisions(entry.name, recover=recover)
                        if entry.name in task_sources:
                            continue
                        if revisions or (directory / entry.name / "legacy.json").exists():
                            raise Denied("Work-item history has no immutable seed")
                    continue
                revisions = self._history_revisions(entry.name, recover=recover)
                yield entry.name[3:], self._direct_record(entry.name), revisions

    def _iter_item_events(self, *, recover: bool):
        for event_id, event, _ in self._iter_item_events_with_revisions(recover=recover):
            yield event_id, event

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
    def _validate_decision_key(event_id: str | None, event: dict) -> None:
        """Validate new keyed decisions; legacy events without a key remain readable."""
        if "decision_key" not in event:
            if "work_item_intent" in event or "work_items" in event or "work_item_specs" in event:
                raise Denied("Explicit work-item intent lacks a decision key")
            return
        action = event.get("action")
        try:
            insight_id = identifier(event.get("id"))
        except Denied as exc:
            raise Denied("Invalid decision key binding") from exc
        rev = event.get("rev")
        key = event.get("decision_key")
        if (action not in ("accept", "modify", "defer", "reject")
                or type(rev) is not int or rev < 1
                or key != _decision_key(insight_id, rev)
                or (event_id is not None and action in ("accept", "modify", "reject")
                    and event_id != key)):
            raise Denied("Invalid decision key binding")
        if "work_items" in event or "work_item_specs" in event:
            _validate_keyed_substantive_event(event, insight_id, rev, key, "")

    @staticmethod
    def _legacy_seed(event_id: str, event: dict, title) -> dict:
        source = WorkItems._legacy_source(event)
        if (any(value is None for value in source.values())
                or not isinstance(title, str) or not 1 <= len(title) <= 200):
            raise Denied("Opening legacy work requires valid origin and a 1-200 character title")
        return make_seed(f"wi_{event_id}", title,
                         {"insight_id": source["insight_id"], "rev": source["rev"]}, source["created_at"],
                         smoke_refs=event.get("smoke_refs"))

    def _legacy_resolution(self, event_id: str, event: dict, *, recover=False,
                           history_revisions=_HISTORY_UNSET) -> dict | None:
        """Validate the immutable sidecar against the exact canonical source."""
        self._validate_decision_key(event_id, event)
        if "work_items" in event or "work_item_specs" in event:
            task_seeds(event)
            return None
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
        with lock_diagnostics.operation("write"), record_lock(self.root):
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
                require_pairs(self.root, record["work_item"].get("smoke_refs", []))
            if existing is not None:
                if record != existing:
                    raise Denied("Conflicting legacy reconciliation retry")
                return {"record": existing, "created": False}
            if len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Legacy reconciliation exceeds byte bound")
            self.require_open_capacity(1 if outcome == "open" else 0)
            with self.committing([f"wi_{event_id}"]):
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
                if "work_items" in event:
                    task_seeds(event)
                    continue
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

    def _iter_decision_events(self, *, recover: bool):
        """Enumerate committed decision events with an explicit recovery mode.

        With ``recover=True`` (caller holds insights.lock) recognized
        ``.new-*`` leftovers are recovered; with ``recover=False`` they are
        validated and ignored, never promoted or deleted. Anything unsafe or
        unexpected fails closed.
        """
        history = self.root / "decision-history"
        if not self._prepare_decision_history(history, recover=recover):
            return
            yield  # Make this a generator; the early return yields nothing.
        with os.scandir(history) as entries:
            for entry in entries:
                name = entry.name
                if _TEMP_NAME.fullmatch(name):
                    if not recover:
                        continue
                    # Locked recovery removed every validated temporary above.
                    raise Denied("Invalid committed decision-history filename")
                if _DECISION_EVENT_NAME.fullmatch(name) is None:
                    raise Denied("Invalid committed decision-history filename")
                event = self._read(f"decision-history/{name}")
                if event.get("action") not in ("accept", "modify", "defer", "reject", "withdraw"):
                    raise Denied("Malformed committed decision-history action")
                if event["action"] not in ("accept", "modify") and "work_item" in event:
                    raise Denied("Non-accept decision carries a work-item seed")
                self._validate_decision_key(name[:-5], event)
                yield name[:-5], event

    def _decision_events(self):
        yield from self._iter_decision_events(recover=True)

    def _task_event(self, item_id: str) -> dict | None:
        """Resolve hashed task IDs from atomic manifests, with no mutable index.

        No recovery here: reads must not promote interrupted events. The
        regular event iterator validates the whole group before matching.
        """
        found = None
        for _, event in self._iter_decision_events(recover=False):
            if "work_items" in event and any(seed["id"] == item_id for seed in event["work_items"]):
                if found is not None:
                    raise Denied("Ambiguous task seed sources")
                found = event
        return found

    def _seed(self, item_id: str, event: dict | None = None, *,
              history_revisions=_HISTORY_UNSET) -> dict:
        """Validate a seed, optionally reusing its already safe-read event."""
        match = ITEM_ID.fullmatch(item_id) if isinstance(item_id, str) else None
        if match is None:
            raise Denied("Invalid work-item ID")
        if event is not None and not isinstance(event, dict):
            raise Denied("Invalid work-item seed event")
        direct = (self._direct_record(item_id)
                  if event is None or event.get("kind") == "direct" else None)
        if direct is not None:
            if event is not None and event != direct:
                raise Denied("Conflicting work-item seed sources")
            return direct["work_item"]
        if event is None:
            try:
                (self.root / "decision-history" / f"{match[1]}.json").lstat()
            except FileNotFoundError:
                event = self._task_event(item_id)
                if event is None:
                    raise Denied("Work item has no immutable seed")
            else:
                event = self._read(f"decision-history/{match[1]}.json")
        if not isinstance(event, dict):
            raise Denied("Invalid work-item seed event")
        if "work_items" in event or "work_item_specs" in event:
            self._validate_decision_key(event.get("decision_key"), event)
            seeds = task_seeds(event)
            for seed in seeds:
                if seed["id"] == item_id:
                    try:
                        (self.root / "work-items" / item_id / "legacy.json").lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        raise Denied("Task seed cannot carry legacy reconciliation")
                    if self._direct_record(item_id) is not None:
                        raise Denied("Conflicting task seed sources")
                    return seed
            raise Denied("Work item is not a member of its decision manifest")
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
                or not isinstance(seed, dict)):
            raise Denied("Invalid work-item seed event")
        identifier(event.get("id"))
        validate_seed(seed)
        if seed != make_seed(item_id, seed["title"], {"insight_id": event["id"], "rev": event["rev"]},
                             event["created_at"],
                             **({"smoke_refs": event["smoke_refs"]} if "smoke_refs" in event else {}),
                             **({"description": event.get("reason")} if "description" in seed else {}),
                             **({"scope": event.get("scope")} if "scope" in seed else {})):
            raise Denied("Work-item seed does not match decision event")
        return seed

    def _optional_record(self, name: str) -> dict | None:
        """Read one optional bounded record, refusing unsafe store paths."""
        parts = name.split("/")
        if (len(parts) != 2 or parts[0] not in ("inbox", "decisions", "insight-ids")
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

    def _origin_lookup(self, insight_id: str, *, locked: bool = True) -> tuple:
        """Index columns of one origin insight: its row, then its current decision."""
        with self._ledger.read("insights", locked=locked) as db:
            return _rows(db, _ORIGIN_SELECT + " FROM (SELECT ? AS k) " + _ORIGIN_JOIN.format(key="k"),
                         (insight_id,))[0]

    def _origin_revision(self, insight_id: str) -> int | None:
        """Current revision of an origin insight; None when it is unknown."""
        return self._origin_rev(self._origin_lookup(insight_id))

    @staticmethod
    def _origin_rev(origin: tuple) -> int | None:
        known, valid, revision = origin[:3]
        if known is not None and not valid:
            raise Denied("Invalid current work-item origin insight")
        return revision

    @staticmethod
    def _validate_current_decision_shape(decision: dict, insight_id: str):
        """Validate the shared current-decision envelope; return (action, rev)."""
        action = decision.get("action")
        fields = {"id", "action", "reason", "revisit", "run", "created_at", "rev"}
        if action in ("accept", "modify"):
            if "work_item_intent" in decision:
                fields.add("work_item_intent")
            if "work_item" in decision:
                fields.add("work_item")
            if "work_items" in decision or "work_item_specs" in decision:
                fields.update({"work_items", "work_item_specs"})
                if "work_item_waits" in decision:
                    fields.add("work_item_waits")
        elif action == "withdraw":
            fields.add("actor")
        elif action not in ("defer", "reject"):
            raise Denied("Invalid current work-item origin decision")
        if "smoke_refs" in decision:
            validate_refs(decision["smoke_refs"])
            fields.add("smoke_refs")
        revision = decision.get("rev")
        WorkItems._validate_decision_key(None, decision)
        if "decision_key" in decision:
            fields.add("decision_key")
        # A decision written before the accepted scope existed keeps the older
        # field set; a decision that states one carries the extra key.
        allowed = {frozenset(fields)}
        if action in ("accept", "modify"):
            allowed.add(frozenset(fields | {"scope"}))
            if "scope" in decision:
                validate_scope(decision["scope"])
        if (frozenset(decision) not in allowed or decision.get("id") != insight_id
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
        return action, revision

    def _resolve_current_work_item(self, decision: dict, *, readonly: bool, events=None,
                                   resolve: bool = True):
        """Resolve the work-item ID (or ordered task IDs) for a current decision.

        The keyed ``work_item`` branch reuses the immutable seed validator,
        which never recovers; only the legacy no-event-ID branch needs the
        history scan, recovered when locked and validated read-only otherwise.
        ``events`` supplies the scanned history for callers that resolve many
        decisions; without ``resolve`` the legacy branch only proves the
        decision equals one immutable event.
        """
        action = decision.get("action")
        if action == "withdraw":
            identifier(decision["actor"])
            return None
        if "work_items" in decision or "work_item_specs" in decision:
            seeds = task_seeds(decision)
            event = self._read(f"decision-history/{decision['decision_key']}.json")
            if event != decision:
                raise Denied("Current task decision differs from its immutable event")
            for seed in seeds:
                self._seed(seed["id"], event)
            return [seed["id"] for seed in seeds]
        if action in ("accept", "modify") and "work_item" in decision:
            seed = decision["work_item"]
            if (not isinstance(seed, dict) or not isinstance(seed.get("id"), str)
                    or ITEM_ID.fullmatch(seed["id"]) is None):
                raise Denied("Malformed current work-item origin decision seed")
            seed_id = seed["id"]
            event = self._read(f"decision-history/{seed_id[3:]}.json")
            if event != decision:
                raise Denied("Current work-item origin decision differs from its immutable event")
            self._seed(seed_id)  # Validate the immutable seed's complete field/value contract.
            return seed_id
        if action in ("accept", "modify"):
            # Legacy current-decision files have no event ID. Resolve the exact
            # old-schema record against one immutable history event rather than
            # trusting a well-shaped mutable pointer by itself.
            match = None
            for event_id, event in (events() if events is not None
                                    else self._iter_decision_events(recover=not readonly)):
                if event == decision:
                    if match is not None:
                        raise Denied("Ambiguous current legacy decision history")
                    match = (event_id, event)
            if match is None:
                raise Denied("Current legacy decision has no immutable history event")
            if not resolve:
                return None
            if readonly:
                revisions = self._history_revisions(f"wi_{match[0]}", recover=False)
                resolution = self._legacy_resolution(*match, history_revisions=revisions)
            else:
                resolution = self._legacy_resolution(*match, recover=True)
            if resolution is not None and resolution["outcome"] == "open":
                return f"wi_{match[0]}"
            return None
        return None

    def _current_decision(self, insight_id: str, *, include_work_item: bool = False) -> dict | None:
        """Validated current decision of one insight with its work-item IDs, read from files."""
        decision = self._optional_record(f"decisions/{insight_id}.json")
        if decision is None:
            return None
        action, revision = self._validate_current_decision_shape(decision, insight_id)
        work_item_id = self._resolve_current_work_item(decision, readonly=False)
        result = {"rev": revision, "action": action}
        if include_work_item:
            if isinstance(work_item_id, list):
                result["work_item_ids"] = work_item_id
            else:
                result["work_item_id"] = work_item_id
        return result

    def _history_revisions(self, item_id: str, *, recover: bool, require_clean=False) -> set[int] | None:
        """Validate per-item history names, optionally recovering leftovers.

        Single scan shared by locked and read-only readers. With
        ``recover=True`` (caller holds insights.lock) recognized regular
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
                if entry.name in ("legacy.json", "create.json"):
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
        if kind in ("ci", "publication_ci", "proposal_merged"):
            if kind == "ci":
                from .ci_waits import normalize
            elif kind == "proposal_merged":
                from .proposal_waits import normalize
            refs = validate_refs(value.get("smoke_refs", []))
            raw = {k: v for k, v in value.items() if k != "smoke_refs"}
            if kind == "publication_ci":
                from .ci_waits import normalize_publication
                result = normalize_publication(raw, snapshots=self.snapshots, stored=True)
            else:
                result = normalize(self.root, raw, stored=True)
            return {**result, "smoke_refs": refs} if "smoke_refs" in value else result
        return stored_wait(value)

    @staticmethod
    def _scope_digest(scope):
        return digest(canonical(scope)) if scope is not None else None

    def _validate_delivery(self, previous, record, *, verify_snapshots: bool = True):
        """Replay the explicit binding, without consulting mutable history/config."""
        snapshot = None
        marked = "delivery_schema" in record
        if "delivery_schema" in previous and not marked:
            raise Denied("A work-item transition cannot remove the delivery schema")
        if not marked:
            return False, None
        if type(record["delivery_schema"]) is not int or record["delivery_schema"] != 1:
            raise Denied("Invalid work-item delivery schema")
        delivery = record["delivery"]
        binding = delivery != previous.get("delivery")
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
            if verify_snapshots:
                snapshot = self.snapshots.binding(delivery["snapshot_id"])
                if (snapshot.get("id") != delivery["snapshot_id"]
                        or snapshot.get("code_digest") != delivery["code_digest"]):
                    raise Denied("Delivery does not match its immutable snapshot")
        if record["status"] == "done":
            if (delivery is None or delivery != previous.get("delivery")
                    or record["snapshot_id"] != delivery["snapshot_id"]
                    or delivery["scope_digest"] != self._scope_digest(record.get("scope"))):
                raise Denied("Completion requires a prior exact snapshot and scope delivery binding")
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
            raise Denied("Invalid retained completion verification")
        receipt = get(self.root, verification["receipt_id"])
        if (receipt["code_digest"] != code_digest
                or digest(canonical(receipt["scope"])) != scope_digest
                or receipt["schema"] == 2 and receipt["scope"]["ci"]["snapshot_id"] != snapshot_id):
            raise Denied("Completion acceptance receipt code or scope mismatch")

    def _validate_transition(self, previous: dict, record: dict, *, verify_snapshots: bool = True) -> None:
        """Validate transitions, always checking current completion evidence."""
        fields = set(record) - {"smoke_refs", "blocked_since", "wait_resolution"}
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
        validate_origin(record["origin"])
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
        if (status not in STATUSES or previous["status"] == "done"
                or (previous["status"] == "dropped" and status != "done")
                or (status == previous["status"] and not scope_changed and not refs_changed and not binding and not release)):
            raise Denied("Invalid work-item status transition")
        # The scope in force is inherited by every revision; it is never
        # cleared, and a closure may not change it.
        if previous_scope is not None and record_scope is None:
            raise Denied("A work-item transition cannot clear the accepted scope")
        if (status == "done" and (previous_scope is not None or "delivery_schema" in record)
                and record_scope != previous_scope):
            raise Denied("Completion cannot change the accepted scope; "
                         "record the scope change in an earlier transition")
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
            raise Denied("Work-item reason must be nonempty text")
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
        if status == "done":
            sid, verification = record["snapshot_id"], record["verification"]
            if (not isinstance(sid, str) or not DIGEST.fullmatch(sid)
                    or not isinstance(verification, dict) or verification.get("passed") is not True
                    or not isinstance(verification.get("code_digest"), str)
                    or not DIGEST.fullmatch(verification["code_digest"])):
                raise Denied("Completion requires verified snapshot evidence")
            snapshot = delivery_snapshot if delivery_snapshot is not None else self.snapshots.binding(sid)
            code_digest = snapshot["code_digest"]
            if verification["code_digest"] != code_digest:
                raise Denied("Completion evidence does not match its delivery")
            retained = verification.get("source") == "retained_acceptance"
            if retained and "delivery_schema" in record:
                self._retained_verification(verification, code_digest,
                                            record["verification_scope"], sid)
            elif (record.get("verification_scope") is not None
                    or snapshot.get("id") != sid
                    or snapshot.get("verification") != verification
                    or verification["code_digest"] != snapshot.get("code_digest")):
                raise Denied("Completion evidence does not match its verified snapshot")
            if previous["status"] == "dropped" and (not retained or "delivery_schema" not in record):
                raise Denied("Dropped correction requires independently retained acceptance")
            # A closure states the tree that carries its delivery. A record
            # written before the claim existed stays readable history; a
            # closure that states a tree its snapshot does not carry fails.
            if "delivered_tree" in record and record["delivered_tree"] != code_digest:
                raise Denied("Completion does not name the tree that carries the delivery")
        elif (record["snapshot_id"] is not None or record["verification"] is not None
                or record.get("delivered_tree") is not None
                or record.get("verification_scope") is not None):
            raise Denied("Only completed work items carry snapshot evidence")

    def _iter_chain(self, item_id: str, *, seed_event: dict | None = None,
                    history_revisions=_HISTORY_UNSET, retirement=None, verify_snapshots: bool = True):
        """Replay and validate one append-only chain, yielding one record at a time.

        The seed and each revision are immutable full-state records. Callers may
        retain only the current record or a bounded page; replay itself never
        materializes the complete history. On exhaustion, validate the terminal
        sidecar separately; never yield it as an invented revision. Optional
        retirement collects that validated record and the full chain hash.
        Status-only callers may skip superseded delivery snapshot reads, not
        chain/field validation or the current active binding. Done completion
        proofs always retain strict verification.
        """
        current = self._seed(item_id, seed_event, history_revisions=history_revisions)
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

    def _get(self, item_id: str, *, seed_event: dict | None = None,
             history_revisions=_HISTORY_UNSET, verify_snapshots: bool = True,
             verify_current_delivery: bool = False) -> dict:
        """Replay one item's current record from its revision files."""
        return self._current_hold(item_id, seed_event=seed_event,
                                  history_revisions=history_revisions,
                                  verify_snapshots=verify_snapshots,
                                  verify_current_delivery=verify_current_delivery)[0]

    def _current_hold(self, item_id: str, *, seed_event=None,
                      history_revisions=_HISTORY_UNSET, verify_snapshots: bool = True,
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
        for current in self._iter_chain(item_id, seed_event=seed_event,
                                        history_revisions=history_revisions, retirement=retirement,
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

    def _ledger_row(self, item_id, event, revisions, *, strict=False) -> tuple:
        item, since, blocked_since = self._current_hold(
            item_id, seed_event=event, history_revisions=revisions,
            verify_snapshots=strict, verify_current_delivery=True)
        origin = item["origin"] or {}
        source = (event.get("decision_key") if isinstance(event, dict)
                  and ("work_items" in event or "work_item_specs" in event) else None)
        return (item["id"], item["rev"], item["status"], item["holder"],
                _timestamp_order(item["created_at"]), _timestamp_order(item["updated_at"]),
                origin.get("insight_id"), origin.get("rev") if "insight_id" in origin else None,
                (item.get("wait") or {}).get("kind"), since, blocked_since, source,
                canonical(item).decode())

    def ledger_entries(self, *, recover: bool, skip=frozenset(), strict=False, errors=None):
        """Yield every index entry, replayed and validated from the files.

        With ``errors`` (a list), an item whose chain fails validation is
        reported there as ``(item_id, exception)`` instead of aborting.
        """
        for event_id, event, scanned in self._iter_item_events_with_revisions(recover=recover):
            kind, seed = self._seed_for_event(event_id, event, readonly=not recover)
            if kind == "unseeded":
                yield ("unseeded", event_id)
            if kind != "seed" or seed["id"] in skip:
                continue
            revisions = (scanned if scanned is not None
                         else self._history_revisions(seed["id"], recover=recover))
            try:
                yield ("item", self._ledger_row(seed["id"], event, revisions, strict=strict))
            except (Denied, LimitExceeded) as exc:
                if errors is None:
                    raise
                errors.append((seed["id"], exc))

    def ledger_item(self, item_id: str, source_event, *, recover: bool) -> list:
        """Replay the index entries of one item (none when it has no seed)."""
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        history = self.root / "decision-history"
        if source_event is not None:
            if not (history / f"{source_event}.json").exists():
                return []
            event = self._read(f"decision-history/{source_event}.json")
        elif (history / f"{item_id[3:]}.json").exists():
            event = self._read(f"decision-history/{item_id[3:]}.json")
        elif (self.root / "work-items" / item_id / "create.json").exists():
            event = self._direct_record(item_id)
        else:
            event = self._task_event(item_id)
            if event is None:
                return []
        kind, seed = self._seed_for_event(item_id[3:], event, readonly=not recover)
        if kind == "unseeded":
            return [("unseeded", item_id[3:])]
        if kind != "seed":
            return []
        revisions = self._history_revisions(item_id, recover=recover)
        return [("item", self._ledger_row(item_id, event, revisions))]

    def committing(self, item_ids, *, source_event=None):
        """Bracket a revision-file commit for ``item_ids``; caller holds the record lock."""
        return self._ledger.write("items", item_ids, hint=source_event)

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

    def _plain(self, item_id: str, *, locked: bool = False) -> dict:
        if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
            raise Denied("Invalid work-item ID")
        with self._ledger.read("items", locked=locked) as db:
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

    def unseeded_decisions(self) -> int:
        """Count legacy accept/modify decisions that no reconciliation resolved."""
        with self._ledger.read("items", locked=False) as db:
            return _rows(db, "SELECT COUNT(*) FROM unseeded")[0][0]

    def closed_items(self, *, limit: int):
        """Return the closed page and the current records of its rows."""
        return self._page_closed_impl(limit=limit, readonly=True, include_items=True)

    def history(self, item_id: str):
        """Replay and validate one item's committed chain from its revision files."""
        return self._iter_chain(item_id)

    def is_drop_only(self, item_id: str) -> bool:
        """Whether an item directory holds only a valid legacy drop resolution."""
        return self._audit_drop_only(item_id)

    def audit(self, *, repair: bool = False) -> dict:
        """Replay every chain strictly and compare it with the index.

        Holds the record lock for the whole run. Reports corrupt chains and
        index divergence (at most MAX_OPEN_PAGE_ROWS IDs each, with counts),
        for work items and, under ``insights``, for the insight and decision
        records; ``repair`` then rebuilds a diverged index from the files
        (work items require every chain to validate). The audit itself never
        changes files.
        """
        with lock_diagnostics.operation("write"), record_lock(self.root):
            errors = []
            files = {}
            unseeded = set()
            for kind, value in self.ledger_entries(recover=False, strict=True, errors=errors):
                if kind == "item":
                    files[value[0]] = value
                else:
                    unseeded.add(value)
            index = self._ledger.snapshot("items")
            if index is None:
                state, indexed, indexed_unseeded = "missing", {}, set()
            else:
                state, (indexed, indexed_unseeded) = index
            divergent = sorted(item_id for item_id in set(files) | set(indexed)
                               if files.get(item_id) != indexed.get(item_id)
                               and item_id not in dict(errors))
            divergent_unseeded = sorted(unseeded ^ indexed_unseeded)
            consistent = not errors and not divergent and not divergent_unseeded and state == "complete"
            repaired = False
            if repair and not errors and not consistent:
                self._ledger.rebuild("items")
                repaired = True
            insights = insight_index.audit(self.insights, repair=repair)
            return {"items": len(files) + len(errors), "unseeded_decisions": len(unseeded),
                    "index": state, "consistent": consistent and insights["consistent"],
                    "repaired": repaired or insights["repaired"], "insights": insights,
                    "corrupt": [{"id": item_id, "error": str(exc)}
                                for item_id, exc in errors[:MAX_OPEN_PAGE_ROWS]],
                    "corrupt_count": len(errors),
                    "divergent": divergent[:MAX_OPEN_PAGE_ROWS], "divergent_count": len(divergent),
                    "divergent_unseeded": divergent_unseeded[:MAX_OPEN_PAGE_ROWS],
                    "divergent_unseeded_count": len(divergent_unseeded)}

    def get(self, item_id: str) -> dict:
        """Read one item's current record under insights.lock."""
        with lock(self.root / "locks" / "insights.lock"):
            return self._plain(item_id, locked=True)

    @staticmethod
    def _origin_decision(origin: tuple) -> dict | None:
        """Current decision of an origin insight from its index columns."""
        known, rev, action, linked = origin[3:]
        if known is None:
            return None
        if not linked:
            raise Denied("Malformed current work-item origin decision")
        return {"rev": rev, "action": action}

    def _origin_projection(self, item: dict, origin=None, *, current_decision=_HISTORY_UNSET) -> dict:
        """Project origin staleness from the item's origin and the insight index.

        ``origin`` holds the joined index columns (see ``_ORIGIN_SELECT``);
        without it the lookup runs under the record lock the callers hold.
        """
        if item["origin"] is None or "work_item_id" in item["origin"]:
            return {"current_origin_rev": None, "stale": False, "redecision": None}
        origin_rev = item["origin"]["rev"]
        if origin is None:
            origin = self._origin_lookup(item["origin"]["insight_id"])
        current_rev = self._origin_rev(origin)
        redecision = None
        if current_rev is None:
            stale = True
        elif current_rev < origin_rev:
            raise Denied("Origin insight revision predates its work item")
        elif current_rev == origin_rev:
            stale = False
        else:
            decision = (self._origin_decision(origin) if current_decision is _HISTORY_UNSET
                        else current_decision)
            if decision is not None and decision["rev"] > current_rev:
                raise Denied("Origin decision revision is ahead of its insight")
            redecision = (decision if decision is not None and decision["rev"] == current_rev
                          and decision["action"] in ("accept", "modify", "defer", "reject")
                          else None)
            stale = redecision is None
        return {"current_origin_rev": current_rev, "stale": stale,
                "redecision": redecision}

    def _projection_unlocked(self, item_id: str) -> dict:
        """Build a bounded projection while the caller holds insights.lock."""
        item, since = self._indexed(item_id, locked=True)
        view = {"item": item, **self._origin_projection(item),
                **self._hold_projection(since), **blocked_age(self, item)}
        if len(canonical(view)) > MAX_PROJECTION_BYTES:
            raise LimitExceeded("Work-item projection exceeds byte bound")
        return view

    def explain(self, item_id: str, *, after_rev=0, limit=25) -> dict:
        """Return current status/origin plus a byte-bounded ascending history page.

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

        with lock(self.root / "locks" / "insights.lock"):
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
                    # The full-chain validator checked the current completion.
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
            projection = {"item": current, **self._origin_projection(current),
                          **self._hold_projection(since),
                          **blocked_age(self, {**current, "blocked_since": blocked_since})}
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

    def submitter_status(self, insight_id: str) -> dict | None:
        """Return one compact status for an insight's current accepted work.

        This is a read projection, not a transition. Current decisions and
        legacy resolutions use the same fail-closed validation as origin
        projections; seeded item state replays its bounded immutable chain.
        Unresolved and drop-only legacy decisions have no item status.
        """
        with lock(self.root / "locks" / "insights.lock"):
            decision = self._current_decision(insight_id, include_work_item=True)
            if decision is None or decision["action"] not in ("accept", "modify"):
                return None
            if "work_item_ids" in decision:
                result = {"items": [self._submitter_item_status(item_id, decision)
                                    for item_id in decision["work_item_ids"]]}
                if len(canonical(result)) > PREVIEW_BYTES:
                    raise LimitExceeded("Submitter task statuses exceed byte bound")
                return result
            if decision["work_item_id"] is None:
                return None
            return self._submitter_item_status(decision["work_item_id"], decision)

    def _submitter_item_status(self, item_id: str, decision: dict) -> dict:
        item, since = self._indexed(item_id, locked=True)
        origin_decision = {"rev": decision["rev"], "action": decision["action"]}
        origin = self._origin_projection(item, current_decision=origin_decision)
        status = {"id": item["id"], "status": item["status"], "rev": item["rev"],
                  "holder": item["holder"], "updated_at": item["updated_at"],
                  "current_origin_rev": origin["current_origin_rev"],
                  "stale": origin["stale"], **self._hold_projection(since), **blocked_age(self, item)}
        if "wait_resolution" in item:
            status["wait_resolution"] = item["wait_resolution"]
        reason = item.get("reason")
        if reason is not None:
            preview, clipped = text_preview(reason, 512)
            status["reason_preview"] = preview
            status["reason_truncated"] = clipped
        if item["status"] == "blocked":
            status["wait"] = item["wait"]
        elif item["status"] == "done":
            status["snapshot_id"] = item["snapshot_id"]
            status["delivered_tree"] = item.get("delivered_tree")
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

    def _prepare_decision_history(self, history: Path, *, recover: bool) -> bool:
        """Validate decision-history temporaries, optionally removing safe ones.

        Single scan shared by locked and read-only readers. With
        ``recover=True`` (caller holds insights.lock) safe leftovers are
        unlinked and the directory synced; with ``recover=False`` they are
        only validated and ignored, never deleted. A lone regular temporary
        is uncommitted. A post-link alias is accepted only when exactly one
        regular event filename identifies the same inode. Recovery state is
        bounded independently of the append-only history. Returns False when
        the history directory is absent.
        """
        if not _directory(history):
            return False
        temporary_info = []
        aliases = {}
        with os.scandir(history) as entries:
            for entry in entries:
                if not _TEMP_NAME.fullmatch(entry.name):
                    continue
                if len(temporary_info) >= self.history_temporaries:
                    raise Denied("Too many decision-history temporaries")
                try:
                    info = _entry_stat(entry)
                except FileNotFoundError:
                    continue  # A concurrent writer finished its atomic write.
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

        if not recover:
            return True
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

    def prepare_decision_history(self) -> bool:
        """Screen the event directory before writes, with insights.lock held."""
        return self._recover_decision_history_temporaries(self.root / "decision-history")

    def prepare_decision_write(self, *, need_wait: bool = False) -> bool:
        """Screen every decision-transaction parent before any write, lock held.

        Validates decision-history with the existing scanner/recovery, then the
        decisions directory (always) and the waits directory when a registered
        wait will be written. A symlinked or otherwise unsafe parent raises
        Denied and is never followed; an absent directory is safe to create.
        Call under insights.lock immediately before each event/pointer/wait
        write (new events, exact-replay pointer repair, withdraw).
        """
        present = self._recover_decision_history_temporaries(self.root / "decision-history")
        _directory(self.root / "decisions")
        if need_wait:
            _directory(self.root / "waits")
        return present

    def _recover_decision_history_temporaries(self, history: Path) -> bool:
        """Remove safe atomic-write leftovers while insights.lock is held.

        Locked entry point sharing validation with the read-only scan.
        A lone regular temporary is uncommitted. A post-link alias is removed
        only when exactly one regular event filename identifies the same inode.
        Recovery state is bounded independently of the append-only history.
        """
        return self._prepare_decision_history(history, recover=True)

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
        bytes and the complete response at most 1 MiB. Selection streams the
        indexed active rows keeping O(limit) of them. Unresolved legacy
        accept/modify events are counted, never guessed as open.
        The page serves the runtime prompt projection and the explicitly granted
        ``work_items_read`` operation. It does not transition items. Cursors
        page a live view: changes between calls can move rows; a last-key
        cursor avoids offset shifts on earlier closes but does not freeze
        membership.
        """
        with lock(self.root / "locks" / "insights.lock"):
            return self._page_open_impl(cursor=cursor, limit=limit, readonly=False)

    def page_open_for_order(self, *, limit=25) -> tuple[dict, str]:
        """Return the normal open page and an order-only full-inventory fingerprint.

        The same locked scan folds bounded accepted scopes and origin decisions
        into constant-size, order-independent evidence. No page or ready-set
        signature is changed, and no bodies or history are retained.
        """
        with lock(self.root / "locks" / "insights.lock"):
            return self._page_open_impl(cursor=None, limit=limit, readonly=False,
                                        order_fingerprint=True)

    def page_ready(self, *, limit=25, role_name=None,
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
        releasers; they never imply completion. CI/proposal waits become ready only
        from their validated local terminal route, independent of event ack.
        Legacy unseeded decisions remain explicit coverage gaps, never inferred ready work. Unreadable
        liveness (``unknown``) excludes only that blocked item and never
        releases it; known ready rows are still returned with
        ``coverage_complete`` false and a bounded ``unknown_blocked`` count,
        so a zero total is not an exhaustive claim. Malformed ledger or
        unsafe work-item state still fails closed.
        """
        if role_name is not None:
            identifier(role_name)
        with lock(self.root / "locks" / "insights.lock"):
            if _wait_resolution_high_water is not None:
                from .snapshot import validate_wait_resolution_high_water
                validate_wait_resolution_high_water(_wait_resolution_high_water)
                if _delivered_wait_resolution_tokens is None or role_name is None:
                    raise Denied("Wait-resolution high water requires scoped delivery tokens")
            args = dict(cursor=None, limit=limit, readonly=False, ready_only=True,
                        role_name=role_name,
                        delivered_wait_resolution_tokens=_delivered_wait_resolution_tokens)
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

    def _seed_for_event(self, event_id: str, event: dict, *, readonly: bool):
        """Resolve the seed for one committed event with an explicit mode.

        Returns ``("skip", None)`` for non-accept decisions, explicit
        no-work intents and drop resolutions; ``("unseeded", None)`` for
        legacy accept/modify events without a resolution; otherwise
        ``("seed", seed)``. The legacy sidecar is validated through the
        shared :meth:`_legacy_resolution`, recovered when locked and read
        from the read-only scan otherwise. Malformed seeds fail closed.
        """
        if "work_items" in event or "work_item_specs" in event:
            return ("seed", self._seed(f"wi_{event_id}", event))
        if event.get("kind") == "direct":
            return ("seed", validate_seed(event["work_item"]))
        action = event["action"]
        has_seed = "work_item" in event
        if action not in ("accept", "modify"):
            if readonly:
                revisions = self._history_revisions(f"wi_{event_id}", recover=False)
                self._legacy_resolution(event_id, event, history_revisions=revisions)
            else:
                self._legacy_resolution(event_id, event, recover=True)
            return ("skip", None)
        if has_seed:
            seed = event["work_item"]
            if (not isinstance(seed, dict) or seed.get("id") != f"wi_{event_id}"):
                raise Denied("Malformed committed work-item seed")
            return ("seed", seed)
        if readonly:
            revisions = self._history_revisions(f"wi_{event_id}", recover=False)
            resolution = self._legacy_resolution(event_id, event, history_revisions=revisions)
        else:
            resolution = self._legacy_resolution(event_id, event, recover=True)
        if event.get("work_item_intent") == "none":
            return ("skip", None)
        if resolution is None:
            return ("unseeded", None)
        if resolution["outcome"] == "drop":
            return ("skip", None)
        seed = resolution["work_item"]
        if (not isinstance(seed, dict) or seed.get("id") != f"wi_{event_id}"):
            raise Denied("Malformed committed work-item seed")
        return ("seed", seed)

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
                    ("current_origin_rev", "stale", "redecision", "doing_since", "doing_age_seconds", "doing_stale")})
        if "wait_resolution" in item:
            row["wait_resolution"] = item["wait_resolution"]
        if item["status"] == "blocked":
            row["wait"] = item["wait"]
            row.update({key: view[key] for key in
                        ("blocked_since", "blocked_age_seconds", "blocked_max_age_seconds")})
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
        if item["status"] == "done":
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

    def _active_rows(self, *, locked: bool):
        """Yield the unseeded count, then stream every active row in ID order."""
        with self._ledger.read("items", "insights", locked=locked) as db:
            yield _rows(db, "SELECT COUNT(*) FROM unseeded")[0][0]
            yield from db.execute(
                f"SELECT i.item, i.doing_since, i.blocked_since, {_ORIGIN_COLUMNS} FROM items i "
                + _ORIGIN_JOIN.format(key="i.origin_insight")
                + " WHERE i.status IN ('open', 'doing', 'blocked') ORDER BY i.id")

    def _page_open_impl(self, *, cursor, limit, readonly: bool, ready_only: bool = False,
                        role_name: str | None = None, order_fingerprint: bool = False,
                        blocked_only: bool = False, include_open_count: bool = False, writer_roles=None,
                        wait_resolution_role: str | None = None, _active_transform=None,
                        delivered_wait_resolution_tokens=None, wait_resolution_high_water=None,
                        operator_answer=None):
        """Collect the bounded open page from the index.

        Callers hold insights.lock when ``readonly`` is False; the read-only
        path never locks or writes. Cursor, limit, ordering, overflow and byte
        bounds match :meth:`page_open`. Host-only opening orchestration
        can supply an active transform on the unlocked readonly path; public
        read entry points never do. The returned state is used before selection,
        counting and fingerprints, so transitions cannot leave a stale page.
        """
        if _active_transform is not None and not readonly:
            raise Denied("Lifecycle transform requires an unlocked read-only scan")
        if operator_answer is None and writer_roles is not None and self.operator_answer_lookup is not None:
            operator_answer = self.operator_answer_lookup()
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
        unseeded_decisions = next(active)
        published_snapshot = None
        selected = []
        counts = {state: 0 for state in
                  ("absent", "pending", "due", "impossible", "receipt_bound", "terminal", "expired", "unknown")}
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
            item, since = self._decode(*entry[:3])
            if _active_transform is not None:
                transformed = _active_transform(item)
                if transformed is not item:
                    item, since = transformed, None
                if item["status"] not in ("open", "doing", "blocked"):
                    continue
            if (wait_resolution_role is not None and item["status"] == "open"
                    and "wait_resolution" in item and item.get("holder") == wait_resolution_role):
                wait_resolution_pending = True
            view = {"item": item, **self._origin_projection(item, entry[3:]),
                    **self._hold_projection(since), **blocked_age(self, item)}
            if len(canonical(view)) > MAX_PROJECTION_BYTES:
                raise LimitExceeded("Work-item projection exceeds byte bound")
            open_count += 1
            if order_fingerprint:
                leaf = canonical(["work-order-scope-v1", item["id"], item.get("scope"),
                                  view["current_origin_rev"], view["stale"], view["redecision"]])
                order_sum = (order_sum + int(digest(leaf), 16)) % (1 << 256)
            if ready_only and role_name is not None:
                if item["status"] == "open":
                    if item.get("holder") not in (None, role_name):
                        continue
                elif item.get("holder") != role_name and not (
                        item["status"] == "blocked" and item.get("holder") is None):
                    continue
            if (ready_only or blocked_only) and item["status"] == "blocked":
                wait = item.get("wait")
                if wait and wait["kind"] == "code_change" and published_snapshot is None:
                    published_snapshot = self.snapshots.get()
                state = liveness(self, wait, published_snapshot,
                                 item=item, writer_roles=writer_roles, operator_answer=operator_answer)
                if ready_only:
                    if state["state"] == "unknown":
                        unknown_blocked += 1
                        continue
                    if state["state"] not in ("due", "impossible", "absent", "terminal", "expired"):
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
                  "overflow_count": overflow_count, "truncated": more,
                  "unseeded_decisions": unseeded_decisions,
                  "coverage_complete": unseeded_decisions == 0}
        if blocked_only:
            complete = unseeded_decisions == 0 and counts["unknown"] == 0
            result = {"items": rows, "total": total if complete else None,
                    "counts": counts if complete else None,
                    "overflow_count": overflow_count, "truncated": more,
                    "unseeded_decisions": unseeded_decisions,
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
            complete = unseeded_decisions == 0 and unknown_blocked == 0
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

    def page_open_readonly(self, *, cursor=None, limit=25) -> dict:
        """Return the live open page without acquiring insights.lock.

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
        with ledger order within each group. Unseeded decisions or unknown
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
                    "truncated": None, "unseeded_decisions": None,
                    "coverage_complete": False, "diagnostic": "unreadable"}

    def open_state_readonly(self, *, limit=25, writer_roles=None, operator_answer=None) -> dict:
        """Return the seeded open count and blocked liveness from one scan.

        The caller gets the same fail-closed diagnostic shape as
        :meth:`blocked_liveness_readonly` and an ``open_count`` derived from the
        same validated projections. Optional configured writer names annotate
        self-resolvable causes without changing liveness states or ordering.
        A read error reports both as unavailable;
        it never falls back to a zero or an unvalidated cache.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
            raise Denied("Work-item page limit must be an integer in [1, 100]")
        try:
            blocked = self._page_open_impl(cursor=None, limit=limit, readonly=True,
                                           blocked_only=True, include_open_count=True,
                                           writer_roles=writer_roles, operator_answer=operator_answer)
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError):
            return {"open_count": None,
                    "blocked_work": {"items": [], "total": None, "counts": None,
                                     "overflow_count": None, "truncated": None,
                                     "unseeded_decisions": None,
                                     "coverage_complete": False, "diagnostic": "unreadable"}}
        open_count = blocked.pop("open_count")
        return {"open_count": open_count, "blocked_work": blocked}

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
        Drop-only legacy sidecars without a seed remain in the legacy audit,
        not here.
        """
        return self._page_closed_impl(limit=limit, readonly=True)

    def transition(self, item_id: str, *, expected_rev, status, actor, reason=None,
                   wait=None, snapshot_id=None, scope=None, smoke_refs=None, _resolve_wait=False) -> dict:
        """Append one immutable full-state CAS event; terminal metadata wakes subscribers.

        The event states the scope in force for the item at that revision: a
        supplied scope sets it from this revision on, an omitted one inherits
        the previous revision's scope, a scope is never cleared, and a
        completion may not change the scope it inherits. Actor is trusted
        caller attribution, not authorization. Done validates an
        exact pre-existing delivery revision, never the live workspace. A doing
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
        if type(expected_rev) is not int or expected_rev < 1:
            raise Denied("Expected revision must be a positive integer")
        # Publication membership and this record append stay ordered together.
        # Smoke retention serializes candidate admission and then scans this
        # project's record lock, so no machine-wide lock belongs on this write.
        binding_request = (status == "doing" and snapshot_id is not None)
        publication_wait = (status == "blocked" and isinstance(wait, dict)
                            and wait.get("kind") == "publication_ci")
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
                state = liveness(self, blocked_waiter.get("wait"), None, item=blocked_waiter)
                bound, age = state["blocked_max_age_seconds"], state["blocked_age_seconds"]
                aged = (state["state"] not in ("due", "terminal", "impossible", "expired")
                        and bound is not None and age is not None and age >= bound)
                needs_open = state["state"] in ("terminal", "impossible", "expired") or aged
                reopen_reason = (state["reason"] if state["state"] in ("terminal", "impossible", "expired")
                                 else "blocked_max_age_reached" if aged else state["reason"])
                if needs_open and (status == "blocked" or (status == "doing" and current["status"] == "blocked")):
                    raise Denied(f"Reopen blocked work with a reason before renewing its wait: {reopen_reason}")
                if (status == "open" and needs_open
                        and (not isinstance(reason, str) or not reason.strip())):
                    raise Denied("Reopening blocked work requires a reason")
            registered = None
            if wait is not None:
                if status != "blocked":
                    raise Denied("Only blocked work items carry waits")
                if isinstance(wait, dict) and wait.get("kind") in ("ci", "publication_ci", "proposal_merged"):
                    if wait["kind"] == "ci":
                        from .ci_waits import normalize
                    elif wait["kind"] == "proposal_merged":
                        from .proposal_waits import normalize
                        if not self.vcs.get("poll_proposals_enabled"):
                            raise Denied("Proposal waits require automatic proposal polling")
                    if not self.vcs.get("poll_enabled") or not self.vcs.get("command"):
                        raise Denied(f"{'Proposal' if wait['kind'] == 'proposal_merged' else 'CI'} waits require automatic VCS polling and an adapter")
                    raw_wait = {k: v for k, v in wait.items() if k != "smoke_refs"}
                    if wait["kind"] == "publication_ci":
                        from .ci_waits import normalize_publication
                        registered = normalize_publication(raw_wait, snapshots=self.snapshots,
                                                           branch=self.vcs.get("ci_branch"))
                    else:
                        registered = (normalize(self.root, raw_wait, insights=self.insights)
                                      if wait["kind"] == "proposal_merged" else normalize(self.root, raw_wait))
                    if "smoke_refs" in wait:
                        registered["smoke_refs"] = validate_refs(wait["smoke_refs"])
                else:
                    registered = self.insights._normalize_wait(wait, "defer")
            verification = None
            verification_scope = None
            delivered_tree = None
            if status == "done":
                delivery = current.get("delivery")
                if not delivery or snapshot_id != delivery["snapshot_id"]:
                    raise Denied("Completion requires a prior exact snapshot delivery binding")
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
                        message = "Completion snapshot must have passing digest-bound verification"
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
                    raise Denied("Completion cannot change the accepted scope; "
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
            with self.committing([item_id]):
                write_json(self.root / "work-items" / item_id / f"{record['rev']}.json",
                           record, exclusive=True)
            if status in ("done", "dropped"):
                self._wake_terminal()
            return record

    def _wake_terminal(self):
        # Non-authoritative hint for non-recursive directory watches. Admission
        # and event delivery always read committed state, never metadata.
        with contextlib.suppress(OSError, NotImplementedError):
            os.utime(self.root / "work-items", None, follow_symlinks=False)
