"""Terminal work-item delivery, independent of decision timestamp cursors.

The work-item store remains authoritative. Receipts are exact, source-scoped,
per-role identities, not watermarks. Reads never recover, lock or write. A
partial acknowledgement can only cause replay of the remaining identities.
"""
from __future__ import annotations

import heapq
import json
import os

from . import insight_archive
from .errors import Denied, LimitExceeded
from .fs import canonical, digest, identifier, mkdir, now, safe_read, write_json
from .work_items import (_ReverseKey, _directory, _timestamp, _timestamp_order,
                         MAX_CLOSED_ROW_BYTES, MAX_CLOSED_PAGE_BYTES)

SOURCE = "work_item"
ACKED_ID_SAMPLE = 10


def _identity(item):
    return f"{SOURCE}:{item['id']}:{item['rev']}"


def _origin_source(store, origin):
    """Resolve submitter, never the transition actor (including legacy holes)."""
    if origin is None or "work_item_id" in origin:
        return None
    insight_id = origin["insight_id"]
    record = store._optional_record(f"inbox/{insight_id}.json")
    if record is None:
        record = insight_archive.read_immutable(store.root, insight_id, include_unavailable=True)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("id") != insight_id:
        raise Denied("Invalid work-item event origin")
    return identifier(record["source"]) if record.get("source") is not None else None


def _payload(store, item):
    _, row = store._closed_row(item)
    row.update(event=_identity(item), source=SOURCE, type=item["status"],
               terminal_at=item["updated_at"], transition=item["transition"],
               origin_source=_origin_source(store, item["origin"]),
               verification=({"sha256": digest(canonical(item["verification"])),
                              "passed": True, "code_digest": item["verification"]["code_digest"]}
                             if item["status"] == "done" else None))
    row["routing"] = "known" if row["origin_source"] is not None else "unknown"
    if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
        raise LimitExceeded("Work-item event exceeds row byte bound")
    return row


def _receipt_path(store, role, item):
    identifier(role)
    return store.root / "work-item-receipts" / role / f"{item['id']}.{item['rev']}.json"


def _received(store, role, item):
    path = _receipt_path(store, role, item)
    if not _directory(path.parent.parent) or not _directory(path.parent):
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
    """Bounded oldest-first pending page plus exact progress counts.

    Validates immutable seeds and the *entire* current chain via WorkItems'
    existing reader (including legacy seeds). Excludes exact receipts before
    the heap limit; UTC microseconds/id/rev order survives ties and rollback.
    At most 100 rows, 8192 bytes/row, 1 MiB/response. Unseeded legacy decisions
    are counted as incomplete coverage, never guessed into terminal events.
    Unsafe/malformed stores fail closed. No reads have recovery side effects.
    """
    return _pending_pages(store, [(role, actions, sources)], limit)[0]


class _PendingPage:
    """Bounded per-role heaps and counts for a shared validated traversal."""

    def __init__(self, role, actions, sources, limit):
        identifier(role)
        if (sources not in ("own", "any") or len(set(actions)) != len(actions)
                or any(a not in ("done", "dropped") for a in actions)):
            raise Denied("Invalid work-item event subscription")
        self.role, self.actions, self.sources, self.limit = role, actions, sources, limit
        self.selected = []
        self.acknowledged_heap = []
        self.acknowledged = self.eligible = self.unknown = 0

    def add(self, store, item, row):
        if row["origin_source"] is None:
            self.unknown += 1
        if self.sources == "own" and row["origin_source"] != self.role:
            return
        key = (_timestamp_order(item["updated_at"]), item["id"], item["rev"])
        if _received(store, self.role, item):
            self.acknowledged += 1
            candidate = (key, _identity(item))
            # Only the most recent ten exact acknowledgements, not a receipt listing.
            if len(self.acknowledged_heap) < ACKED_ID_SAMPLE:
                heapq.heappush(self.acknowledged_heap, candidate)
            elif key > self.acknowledged_heap[0][0]:
                heapq.heapreplace(self.acknowledged_heap, candidate)
            return
        self.eligible += 1
        candidate = (_ReverseKey(key), key, row)
        if len(self.selected) < self.limit:
            heapq.heappush(self.selected, candidate)
        elif key < self.selected[0][1]:
            heapq.heapreplace(self.selected, candidate)

    def result(self, unseeded):
        rows = [entry[2] for entry in sorted(self.selected, key=lambda value: value[1])]
        acknowledged_ids = [event_id for _, event_id in sorted(self.acknowledged_heap)]
        result = {"role": self.role, "subscribed": list(self.actions), "sources": self.sources,
                  "events": rows, "pending": self.eligible, "acknowledged": self.acknowledged,
                  "acknowledged_event_ids": acknowledged_ids,
                  "acknowledged_ids_truncated": self.acknowledged > len(acknowledged_ids),
                  "overflow_count": self.eligible - len(rows), "truncated": self.eligible > len(rows),
                  "unknown_routing": self.unknown, "routing_complete": self.unknown == 0,
                  "unseeded_decisions": unseeded if self.actions else 0,
                  "coverage_complete": unseeded == 0 or not self.actions}
        if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
            raise LimitExceeded("Work-item events exceed page byte bound")
        return result


def _pending_pages(store, subscriptions, limit):
    pages = [_PendingPage(*subscription, limit) for subscription in subscriptions]
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Work-item event limit must be in [1, 100]")
    unseeded = 0
    if any(page.actions for page in pages):
        for event_id, event in store._iter_item_events(recover=False):
            kind, seed = store._seed_for_event(event_id, event, readonly=True)
            if kind == "unseeded":
                unseeded += 1
            if kind != "seed":
                continue
            item = store._get(seed["id"], seed_event=event,
                              history_revisions=store._history_revisions(seed["id"], recover=False))
            matching = [page for page in pages if item["status"] in page.actions]
            if matching:
                row = _payload(store, item)
                for page in matching:
                    page.add(store, item, row)
    return [page.result(unseeded) for page in pages]


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
    item = store._get(item_id, history_revisions=store._history_revisions(item_id, recover=False))
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
    writing. One atomic file per identity avoids read/modify/write lost updates.
    Never touches decision-cursors. I/O failure leaves remaining events pending;
    leftover atomic-write temporaries are not receipts and need no recovery.
    """
    identifier(role)
    if not isinstance(events, list) or len(events) > 100:
        raise Denied("Invalid work-item acknowledgement batch")
    changed = False
    try:
        for event in events:
            item = _validate_payload(store, event)
            if _received(store, role, item):
                continue
            path = _receipt_path(store, role, item)
            mkdir(path.parent)
            # These paths were screened by _received; no symlink-following reads.
            # Mark before the atomic write: if it fails after the commit point,
            # a harmless wake lets admission distinguish replay from pending.
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
    finally:
        if changed:
            # Reuse the watched work-items parent as a non-authoritative drain
            # hint; do not depend on a role receipt directory existing at install.
            try:
                os.utime(store.root / "work-items", None, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass


def progress(store, roles, *, limit=100):
    """Read-only summaries, at most 100 roles/1 MiB, no receipt enumeration."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Work-item progress role limit must be in [1, 100]")
    # This view is specifically for terminal work-item subscriptions; decision-
    # only roles do not have a work-item acknowledgement position to report.
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
              "coverage_complete": len(names) == len(rows) and all(r["coverage_complete"] for r in rows)}
    if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Work-item event progress exceeds byte bound")
    return result


def closed_page(store, roles, *, limit=10):
    """Read-only closed page with exact, independently routed role receipts.

    Reuse the validated terminal payload, not the progress summary's samples.
    Every matching role is included in sorted order, or the entire read fails
    closed at the existing row/page byte bounds. No locks or recovery.
    """
    page, items = store._page_closed_impl(limit=limit, readonly=True, include_items=True)
    for row, item in zip(page["items"], items):
        payload = _payload(store, item)
        acknowledgements = {}
        row["event_acknowledgements"] = acknowledgements
        for name in sorted(roles):
            role = roles[name]
            if payload["type"] not in role.work_item_events:
                continue
            if role.work_item_sources not in ("own", "any"):
                raise Denied("Invalid work-item event source route")
            if role.work_item_sources == "own" and payload["origin_source"] != name:
                continue
            acknowledgements[name] = _received(store, name, item)
            if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
                raise LimitExceeded("Closed work-item acknowledgements exceed row byte bound")
        if len(canonical(row)) > MAX_CLOSED_ROW_BYTES:
            raise LimitExceeded("Closed work-item acknowledgements exceed row byte bound")
    if len(canonical(page)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Closed work-item acknowledgements exceed page byte bound")
    return page
