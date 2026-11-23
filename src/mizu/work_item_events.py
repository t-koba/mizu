"""Terminal work-item delivery to the roles that subscribe to it.

The work-item store remains authoritative. A write-time index holds the
current pending rows and each role's exact acknowledgement counter plus its
newest-ten-receipt sample; reads touch only that index, never terminal chains
or receipt directories. A one-row ledger marker binds the index to the latest
terminal head change without counting history. Receipts are exact,
source-scoped, per-role identities, not watermarks. A partial acknowledgement
can only cause replay of the remaining identities.
"""
from __future__ import annotations

import heapq
import json
import os

from .completion_evidence import VERIFIED_SNAPSHOT, recorded_policy
from .errors import Denied, LimitExceeded
from .fs import canonical, digest, identifier, mkdir, now, safe_read, touch_directory, write_json
from .ledger import rows as _rows
from .smoke_refs import record_lock
from .work_item_seed import ITEM_ID
from .work_items import (_ReverseKey, _directory, _timestamp, _timestamp_order,
                         MAX_CLOSED_ROW_BYTES, MAX_CLOSED_PAGE_BYTES)

SOURCE = "work_item"
ACKED_ID_SAMPLE = 10
INDEX_NAME = "work-item-events-index.json"
HEADS_NAME = "work-item-events-heads"
INDEX_SCHEMA = 2
MAX_INDEX_BYTES = 16 * 1024 * 1024


def _identity(item):
    return f"{SOURCE}:{item['id']}:{item['rev']}"


def _payload(store, item, creators=None):
    _, row = store.closed_row(item)
    row.update(event=_identity(item), source=SOURCE, type=item["status"],
               terminal_at=item["updated_at"], transition=item["transition"],
               creator=(store.creators() if creators is None else creators).get(item["id"]),
               verification=({"sha256": digest(canonical(item["verification"])),
                              "passed": True, "code_digest": item["verification"]["code_digest"]}
                             if item["status"] == "done" and recorded_policy(item) == VERIFIED_SNAPSHOT else None))
    row["routing"] = "known" if row["creator"] is not None else "unknown"
    if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
        raise LimitExceeded("Work-item event exceeds row byte bound")
    return row


def _receipt_path(store, role, item):
    identifier(role)
    return store.root / "work-item-receipts" / role / f"{item['id']}.{item['rev']}.json"


def _receipts(store, role):
    """Receipt file names of one role, or None when it has no receipt directory."""
    directory = store.root / "work-item-receipts" / identifier(role)
    if not _directory(directory.parent) or not _directory(directory):
        return None
    with os.scandir(directory) as entries:
        return {entry.name for entry in entries}


def _event_key(row):
    """Oldest-first ordering key of an immutable terminal row."""
    return [_timestamp_order(row.get("terminal_at", row.get("updated_at"))),
            row["id"], row["rev"]]


def _valid_key(key):
    return (isinstance(key, list) and len(key) == 3 and type(key[0]) is int
            and isinstance(key[1], str) and type(key[2]) is int)


def _new_role():
    return {"acknowledged": 0, "acked": [], "pending": []}


def _new_index(roles, generation, terminal_seq):
    return {"schema": INDEX_SCHEMA, "generation": generation, "terminal_seq": terminal_seq,
            "unknown": {"done": 0, "dropped": 0}, "events": {},
            "roles": {role: _new_role() for role in roles}}


def _sample(state, key, event_id):
    """Keep the ten newest acknowledged identities, not receipt timestamps."""
    candidate = [key, event_id]
    kept = state["acked"]
    if len(kept) < ACKED_ID_SAMPLE:
        kept.append(candidate)
    elif candidate > kept[0]:
        kept[0] = candidate
    kept.sort()


def _validate_index(index):
    generation = index.get("generation") if isinstance(index, dict) else None
    if (not isinstance(index, dict)
            or set(index) != {"schema", "generation", "terminal_seq", "unknown", "events", "roles"}
            or type(index["schema"]) is not int or index["schema"] != INDEX_SCHEMA
            or not isinstance(generation, str) or len(generation) != 64
            or any(char not in "0123456789abcdef" for char in generation)
            or type(index["terminal_seq"]) is not int or index["terminal_seq"] < 0
            or not isinstance(index["events"], dict) or not isinstance(index["roles"], dict)):
        raise Denied("Malformed work-item event index")
    unknown = index["unknown"]
    if (not isinstance(unknown, dict) or set(unknown) != {"done", "dropped"}
            or any(type(value) is not int or value < 0 for value in unknown.values())):
        raise Denied("Malformed work-item event index")
    for event_id, entry in index["events"].items():
        if (not isinstance(event_id, str) or not isinstance(entry, dict)
                or set(entry) != {"key", "row"} or not _valid_key(entry["key"])):
            raise Denied("Malformed work-item event index row")
        row = entry["row"]
        if (not isinstance(row, dict) or row.get("event") != event_id
                or _identity(row) != event_id or row.get("source") != SOURCE
                or row.get("type") not in ("done", "dropped")
                or not _timestamp(row.get("terminal_at"))
                or (row.get("creator") is not None and not isinstance(row["creator"], str))
                or row.get("routing") != ("known" if row.get("creator") is not None else "unknown")
                or len(canonical(row)) > MAX_CLOSED_ROW_BYTES):
            raise Denied("Malformed work-item event index row")
    for name, state in index["roles"].items():
        identifier(name)
        if (not isinstance(state, dict) or set(state) != {"acknowledged", "acked", "pending"}
                or type(state["acknowledged"]) is not int or state["acknowledged"] < 0
                or not isinstance(state["acked"], list) or not isinstance(state["pending"], list)
                or len(state["acked"]) > ACKED_ID_SAMPLE):
            raise Denied("Malformed work-item event index role")
        seen = set()
        for event_id in state["pending"]:
            if (not isinstance(event_id, str) or event_id in seen
                    or event_id not in index["events"]):
                raise Denied("Malformed work-item event index pending row")
            seen.add(event_id)
        previous = None
        for sample in state["acked"]:
            if (not isinstance(sample, list) or len(sample) != 2
                    or not _valid_key(sample[0]) or not isinstance(sample[1], str)
                    or (previous is not None and sample <= previous)):
                raise Denied("Malformed work-item event index acknowledgement sample")
            previous = sample


def _validate_heads(heads):
    if (not isinstance(heads, dict) or set(heads) != {"schema", "heads"}
            or type(heads["schema"]) is not int or heads["schema"] != INDEX_SCHEMA
            or not isinstance(heads["heads"], dict)):
        raise Denied("Malformed work-item event head index")
    for item_id, event_id in heads["heads"].items():
        if (not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None
                or not isinstance(event_id, str) or event_id.split(":")[1:2] != [item_id]):
            raise Denied("Malformed work-item event index head")


def _read_state(store, name, validate, absent):
    """One pinned generation of a JSON index, or its absent value."""
    try:
        (store.root / name).lstat()
    except FileNotFoundError:
        return absent
    raw = safe_read(store.root, name, MAX_INDEX_BYTES)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Denied(f"Malformed {name}") from exc
    validate(value)
    return value


def _write_state(store, name, value, validate):
    validate(value)
    raw = canonical(value)
    if len(raw) > MAX_INDEX_BYTES:
        raise LimitExceeded("Work-item event index exceeds byte bound")
    write_json(store.root / name, value)


def _read_index(store):
    return _read_state(store, INDEX_NAME, _validate_index, None)


def _write_index(store, index):
    _write_state(store, INDEX_NAME, index, _validate_index)


def _head_path(store, item_id):
    if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None:
        raise Denied("Malformed work-item event head")
    return store.root / HEADS_NAME / f"{item_id}.json"


def _validate_head(value, item_id):
    if (not isinstance(value, dict) or set(value) != {"schema", "event"}
            or type(value["schema"]) is not int or value["schema"] != INDEX_SCHEMA
            or not isinstance(value["event"], str)
            or value["event"].split(":")[1:2] != [item_id]):
        raise Denied("Malformed work-item event head")


def _read_head(store, item_id):
    """One terminal head by item key; absent is None, never a full scan."""
    path = _head_path(store, item_id)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    raw = safe_read(store.root, f"{HEADS_NAME}/{item_id}.json", MAX_INDEX_BYTES)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Denied("Malformed work-item event head") from exc
    _validate_head(value, item_id)
    return value["event"]


def _write_head(store, item_id, event):
    """Publish only the changed terminal head key."""
    value = {"schema": INDEX_SCHEMA, "event": event}
    _validate_head(value, item_id)
    write_json(_head_path(store, item_id), value)


def _write_heads(store, heads):
    """Rebuild-time sync of per-item head keys from validated terminal rows."""
    directory = store.root / HEADS_NAME
    mkdir(directory)
    values = heads["heads"]
    _validate_heads(heads)
    wanted = {f"{item_id}.json" for item_id in values}
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in wanted and not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.path)
    for item_id, event in values.items():
        _write_head(store, item_id, event)


def _freshness(store):
    """Ledger generation and terminal head changes in one constant-cost row."""
    with store._ledger.read("items", locked=False) as db:
        state = _rows(db, "SELECT c.token_key, t.seq FROM item_changes AS c, "
                          "terminal_changes AS t WHERE c.singleton=1 AND t.singleton=1")
    if (len(state) != 1 or not isinstance(state[0][0], bytes)
            or len(state[0][0]) != 32 or type(state[0][1]) is not int
            or state[0][1] < 0):
        raise Denied("Work-item terminal event marker unreadable")
    return state[0][0].hex(), state[0][1]

#: Write-boundary evidence: the marker before this item's record is committed.
terminal_marker = _freshness


def _index_roles(store, index=None, extra=()):
    lookup = getattr(store, "event_roles", None)
    names = set(extra)
    if callable(lookup):
        names.update(lookup())
    if index is not None:
        names.update(index["roles"])
    for name in names:
        identifier(name)
    return tuple(sorted(names))


def _receipt_roles(store):
    directory = store.root / "work-item-receipts"
    if not _directory(directory):
        return ()
    with os.scandir(directory) as entries:
        names = tuple(sorted(entry.name for entry in entries
                             if entry.is_dir(follow_symlinks=False)))
    for name in names:
        identifier(name)
    return names


def _compose_index(store, found, roles, received):
    """Build compact state from validated terminal rows and exact receipts."""
    index = _new_index(roles, *_freshness(store))
    heads = {"schema": INDEX_SCHEMA, "heads": {}}
    for raw, creator, status in found:
        item = json.loads(raw)
        row = _payload(store, item, {item["id"]: creator})
        event_id, key = row["event"], _event_key(row)
        heads["heads"][item["id"]] = event_id
        if creator is None:
            index["unknown"][status] += 1
        pending_for = []
        for role in roles:
            state = index["roles"][role]
            if received(store, role, item):
                state["acknowledged"] += 1
                _sample(state, key, event_id)
            else:
                state["pending"].append(event_id)
                pending_for.append(role)
        if pending_for:
            index["events"][event_id] = {"key": key, "row": row}
    return index, heads


def _rebuild_index(store, extra=()):
    """Build the whole index once under the caller's record write lock.

    Terminal payloads and exact receipts are validated here, never on a
    read. The ledger stays authoritative and is not rebuilt: change cursors
    must survive an event-index rebuild. A rebuild is also used for a
    superseded terminal head or a newly seen role, where exact acknowledgement
    samples cannot be derived from the compact state.
    """
    with store._ledger.read("items", locked=True) as db:
        found = _rows(db, "SELECT item, creator, status FROM items "
                      "WHERE status IN ('done','dropped') ORDER BY id")
    roles = _index_roles(store, None, (*extra, *_receipt_roles(store)))
    listings = {role: _receipts(store, role) for role in roles}
    index, heads = _compose_index(
        store, found, roles,
        lambda store, role, item: _received(store, role, item, listings[role]))
    _write_heads(store, heads)
    _write_index(store, index)
    return index


def _load_index(store, marker):
    """Load an index pinned to a known marker, or None when it must be rebuilt."""
    try:
        index = _read_index(store)
        (store.root / HEADS_NAME).lstat()
    except (Denied, FileNotFoundError):
        return None
    if index is None or (index["generation"], index["terminal_seq"]) != marker:
        return None
    return index


def _ensure_index(store, extra=()):
    """Load a current index, rebuilding only from a write path."""
    index = _load_index(store, _freshness(store))
    if index is None:
        return _rebuild_index(store, extra)
    if (set(_index_roles(store)) | set(extra)) - set(index["roles"]):
        # A new subscriber needs validated history and exact receipts, not an
        # empty role state that would hide events until a later write.
        return _rebuild_index(store, extra)
    return index


def index_roles(store):
    """Recover the index and seed configured roles at a write boundary.

    Project start and settings refresh hold the record lock here. A missing,
    stale or role-incomplete index is rebuilt from validated terminal rows and
    exact receipts; reads between an interrupted write and this boundary keep
    failing closed.
    """
    with record_lock(store.root):
        try:
            index = _read_index(store)
        except Denied:
            index = None
        marker = _freshness(store)
        current = index is not None and (index["generation"], index["terminal_seq"]) == marker
        if current and set(_index_roles(store)) <= set(index["roles"]):
            return
        if not marker[1]:
            return  # No terminal history exists to seed.
        _rebuild_index(store)


def _terminal_row(store, item):
    """Validate the immutable payload of one terminal head before indexing."""
    creation = store._creation_record(item["id"])
    creator = creation.get("actor") if creation and creation.get("kind") == "direct" else None
    return creator, _payload(store, item, {item["id"]: creator})


def validate_terminal_payload(store, item):
    """Write-time preflight so an unindexable head is never committed."""
    _terminal_row(store, item)


def index_terminal(store, item, *, terminal=True, marker=None):
    """Index one committed terminal head, or drop a superseded one.

    Caller holds records.lock. ``marker`` is the freshness evidence read before
    the record commit; the index must still match it, so an earlier interrupted
    write cannot be mistaken for this one. Every payload is validated before it
    is indexed, and a changed head triggers a full write-time rebuild so exact
    acknowledgement samples never need recovery on a read.
    """
    index = None if marker is None else _load_index(store, marker)
    try:
        current = _freshness(store)
    except Denied:
        current = None
    if (index is None or current is None or current[0] != marker[0]
            or current[1] not in (marker[1], marker[1] + 1)
            or set(_index_roles(store)) - set(index["roles"])):
        # The in-flight head is already in the ledger, so the rebuild covers it.
        index = _rebuild_index(store)
    item_id = item["id"]
    try:
        head_event = _read_head(store, item_id)
    except Denied:
        index = _rebuild_index(store)
        head_event = _read_head(store, item_id)
    if not terminal:
        if head_event is not None:
            _rebuild_index(store)
        return
    event_id = _identity(item)
    if event_id == head_event:
        return
    if head_event is not None:
        _rebuild_index(store)
        return
    creator, row = _terminal_row(store, item)
    if current is not None:
        index["generation"], index["terminal_seq"] = current
    if creator is None:
        index["unknown"][item["status"]] += 1
    index["events"][event_id] = {"key": _event_key(row), "row": row}
    for state in index["roles"].values():
        state["pending"].append(event_id)
    _write_head(store, item_id, event_id)
    _write_index(store, index)


def index_ack(store, role, item):
    """Move one durable receipt out of a role's pending rows.

    Caller holds records.lock. Idempotent receipts that are already indexed
    are a no-op; a receipt written but not yet indexed is settled here.
    """
    index = _ensure_index(store, (role,))
    state = index["roles"][role]
    event_id = _identity(item)
    if event_id not in state["pending"]:
        return
    state["pending"].remove(event_id)
    if not any(event_id in other["pending"] for other in index["roles"].values()):
        index["events"].pop(event_id, None)
    state["acknowledged"] += 1
    _sample(state, _event_key(item), event_id)
    _write_index(store, index)


def _received(store, role, item, listing=...):
    path = _receipt_path(store, role, item)
    if listing is not ...:
        if listing is None or path.name not in listing:
            return False
    elif not _directory(path.parent.parent) or not _directory(path.parent):
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    try:
        receipt = json.loads(safe_read(store.root, path.relative_to(store.root).as_posix(), 1024))
    except (ValueError, UnicodeError) as exc:
        raise Denied("Malformed work-item event receipt") from exc
    if (not isinstance(receipt, dict)
            or set(receipt) != {"role", "source", "event", "acknowledged_at"}
            or receipt.get("role") != role or receipt.get("source") != SOURCE
            or receipt.get("event") != _identity(item)
            or not _timestamp(receipt.get("acknowledged_at"))):
        raise Denied("Invalid work-item event receipt")
    return True


def pending(store, role, actions, *, sources="own", limit=10):
    """Bounded oldest-first pending page plus exact progress counts from the index.

    ``own`` selects items the role created. UTC microseconds/id/rev order
    survives ties and rollback. At most 100 rows, 8192 bytes/row, 1 MiB per
    response. Malformed, missing or stale indexes fail closed; reads never
    lock, write, replay chains or enumerate receipts.
    """
    return _pending_pages(store, [(role, actions, sources)], limit)[0]


class _PendingPage:
    """Bounded per-role heaps and counts over indexed pending rows."""

    def __init__(self, role, actions, sources, limit):
        identifier(role)
        if (sources not in ("own", "any") or len(set(actions)) != len(actions)
                or any(a not in ("done", "dropped") for a in actions)):
            raise Denied("Invalid work-item event subscription")
        self.role, self.actions, self.sources, self.limit = role, actions, sources, limit
        self.selected = []
        self.acknowledged_heap = []
        self.acknowledged = self.eligible = self.unknown = 0

    def add(self, row, key):
        self.eligible += 1
        candidate = (_ReverseKey(key), key, row)
        if len(self.selected) < self.limit:
            heapq.heappush(self.selected, candidate)
        elif key < self.selected[0][1]:
            heapq.heapreplace(self.selected, candidate)

    def result(self):
        rows = [entry[2] for entry in sorted(self.selected, key=lambda value: value[1])]
        acknowledged_ids = [event_id for _, event_id in sorted(self.acknowledged_heap)]
        result = {"role": self.role, "subscribed": list(self.actions), "sources": self.sources,
                  "events": rows, "pending": self.eligible, "acknowledged": self.acknowledged,
                  "acknowledged_event_ids": acknowledged_ids,
                  "acknowledged_ids_truncated": self.acknowledged > len(acknowledged_ids),
                  "overflow_count": self.eligible - len(rows), "truncated": self.eligible > len(rows),
                  "unknown_routing": self.unknown, "routing_complete": self.unknown == 0}
        if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
            raise LimitExceeded("Work-item events exceed page byte bound")
        return result


def _pending_pages(store, subscriptions, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Work-item event limit must be in [1, 100]")
    pages = [_PendingPage(*subscription, limit) for subscription in subscriptions]
    index = _read_index(store)
    if index is None:
        if _freshness(store)[1]:
            raise Denied("Work-item event index missing; a write must rebuild it first")
        return [page.result() for page in pages]
    if (index["generation"], index["terminal_seq"]) != _freshness(store):
        raise Denied("Work-item event index stale; retry after a write rebuilds it")
    for page in pages:
        state = index["roles"].get(page.role, _new_role())
        page.acknowledged = state["acknowledged"]
        page.acknowledged_heap = [tuple(sample) for sample in state["acked"]]
        page.unknown = sum(index["unknown"][action] for action in page.actions)
        for event_id in state["pending"]:
            entry = index["events"][event_id]
            row = entry["row"]
            if row["type"] not in page.actions:
                continue
            if page.sources == "own" and row["creator"] != page.role:
                continue
            page.add(row, tuple(entry["key"]))
    return [page.result() for page in pages]


def _token(role, payload):
    identifier(role)
    return digest(canonical({"role": role, "payload": payload}))


def delivery(role, page):
    """Add role/payload-bound host tokens to a frozen pending page.

    Store/CLI pending views and durable receipt payloads remain unchanged.
    Tokens add no authority: finish also requires delivery in the current unit.
    Apply the existing byte bounds to the token-bearing projection too.
    """
    rows = [{**row, "ack_token": _token(role, row)} for row in page["events"]]
    if any(len(canonical(row)) > MAX_CLOSED_ROW_BYTES for row in rows):
        raise LimitExceeded("Work-item event exceeds row byte bound")
    result = {**page, "events": rows}
    if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Work-item events exceed page byte bound")
    return result


def _validate_payload(store, event):
    if not isinstance(event, dict):
        raise Denied("Invalid work-item acknowledgement event")
    item_id = event.get("id")
    # Reuse full seed/chain validation, never trust a delivered mutable id.
    item = store.current(item_id)
    if item["status"] not in ("done", "dropped") or event != _payload(store, item):
        raise Denied("Work-item acknowledgement differs from terminal event")
    return item


def acknowledgement_payload(store, role, event):
    """Validate a delivered token and current terminal binding; return raw row.

    Called before finish intent and again after durable success. The caller
    enforces current-unit membership. Tokens never enter durable receipts.
    """
    payload = {k: v for k, v in event.items() if k != "ack_token"}
    if event.get("ack_token") != _token(role, payload):
        raise Denied("Work-item acknowledgement binding changed")
    _validate_payload(store, payload)
    return payload


def acknowledge(store, role, events):
    """Host-only, after durable completed result; idempotent exact receipts.

    Validate each frozen payload against its immutable terminal chain before
    writing. Receipt publication and the pending index share records.lock with
    terminal transitions, so no read/modify/write race or recovery path exists.
    I/O failure leaves remaining events pending; leftover atomic-write
    temporaries are not receipts and need no recovery.
    """
    identifier(role)
    if not isinstance(events, list) or len(events) > 100:
        raise Denied("Invalid work-item acknowledgement batch")
    with record_lock(store.root):
        changed = False
        try:
            for event in events:
                item = _validate_payload(store, event)
                if _received(store, role, item):
                    index_ack(store, role, item)
                    continue
                path = _receipt_path(store, role, item)
                mkdir(path.parent)
                # These paths were screened by _received; no symlink reads.
                # Mark before the atomic write: if it fails after the commit
                # point, a harmless wake distinguishes replay from pending.
                receipt = {"role": role, "source": SOURCE, "event": _identity(item),
                           "acknowledged_at": now()}
                changed = True
                try:
                    write_json(path, receipt, exclusive=True)
                except (FileExistsError, Denied):
                    # Exclusive writes report differing timestamps as immutable
                    # conflicts. Only a fully validated existing receipt wins;
                    # malformed or unsafe data and missing receipts fail closed.
                    if not _received(store, role, item):
                        raise
                index_ack(store, role, item)
        finally:
            if changed:
                # Reuse the watched work-items parent as a non-authoritative
                # drain hint; no role receipt directory is needed at install.
                touch_directory(store.root / "work-items")


def progress(store, roles, *, limit=100):
    """Read-only summaries, at most 100 roles/1 MiB, no receipt enumeration."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Work-item progress role limit must be in [1, 100]")
    # Only roles subscribed to terminal work items have a position to report.
    names = sorted(name for name, role in roles.items() if role.work_item_events)
    rows = []
    subscriptions = [(name, roles[name].work_item_events, roles[name].work_item_sources)
                     for name in names[:limit]]
    for page in _pending_pages(store, subscriptions, 10):
        summary = {key: value for key, value in page.items()
                   if key not in ("events", "overflow_count", "truncated")}
        summary["pending_event_ids"] = [event["event"] for event in page["events"]]
        summary["pending_ids_truncated"] = page["truncated"]
        summary["pending_overflow_count"] = page["overflow_count"]
        rows.append(summary)
    result = {"roles": rows, "total": len(names), "truncated": len(names) > len(rows),
              "coverage_complete": len(names) == len(rows)}
    if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Work-item event progress exceeds byte bound")
    return result


def closed_page(store, roles, *, limit=10):
    """Read-only closed page with exact, independently routed role receipts.

    Reuse the validated terminal payload, not the progress summary's samples.
    Every matching role is included in sorted order, or the entire read fails
    closed at the existing row/page byte bounds. No locks or recovery.
    """
    page, items = store.closed_items(limit=limit)
    creators = store.creators()
    for row, item in zip(page["items"], items):
        payload = _payload(store, item, creators)
        acknowledgements = {}
        row["event_acknowledgements"] = acknowledgements
        for name in sorted(roles):
            role = roles[name]
            if payload["type"] not in role.work_item_events:
                continue
            if role.work_item_sources not in ("own", "any"):
                raise Denied("Invalid work-item event source route")
            if role.work_item_sources == "own" and payload["creator"] != name:
                continue
            acknowledgements[name] = _received(store, name, item)
            if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
                raise LimitExceeded("Closed work-item acknowledgements exceed row byte bound")
        if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
            raise LimitExceeded("Closed work-item acknowledgements exceed row byte bound")
    if len(canonical(page)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Closed work-item acknowledgements exceed page byte bound")
    return page
