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
    identifier(role)
    if (sources not in ("own", "any") or len(set(actions)) != len(actions)
            or any(a not in ("done", "dropped") for a in actions)):
        raise Denied("Invalid work-item event subscription")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Work-item event limit must be in [1, 100]")
    selected = []
    acknowledged_heap = []
    acknowledged = eligible = unseeded = unknown = 0
    if actions:
        for event_id, event in store._iter_decision_events(recover=False):
            kind, seed = store._seed_for_event(event_id, event, readonly=True)
            if kind == "unseeded":
                unseeded += 1
            if kind != "seed":
                continue
            item = store._get(seed["id"], seed_event=event,
                              history_revisions=store._history_revisions(seed["id"], recover=False))
            if item["status"] not in actions:
                continue
            row = _payload(store, item)
            if row["origin_source"] is None:
                unknown += 1
            if sources == "own" and row["origin_source"] != role:
                continue
            key = (_timestamp_order(item["updated_at"]), item["id"], item["rev"])
            if _received(store, role, item):
                acknowledged += 1
                candidate = (key, _identity(item))
                # Keep only the most recent ten exact acknowledgements so the
                # read view shows position without an unbounded receipt listing.
                if len(acknowledged_heap) < ACKED_ID_SAMPLE:
                    heapq.heappush(acknowledged_heap, candidate)
                elif key > acknowledged_heap[0][0]:
                    heapq.heapreplace(acknowledged_heap, candidate)
                continue
            eligible += 1
            candidate = (_ReverseKey(key), key, row)
            if len(selected) < limit:
                heapq.heappush(selected, candidate)
            elif key < selected[0][1]:
                heapq.heapreplace(selected, candidate)
    rows = [entry[2] for entry in sorted(selected, key=lambda value: value[1])]
    acknowledged_ids = [event_id for _, event_id in sorted(acknowledged_heap)]
    result = {"role": role, "subscribed": list(actions), "sources": sources,
              "events": rows, "pending": eligible, "acknowledged": acknowledged,
              "acknowledged_event_ids": acknowledged_ids,
              "acknowledged_ids_truncated": acknowledged > len(acknowledged_ids),
              "overflow_count": eligible - len(rows), "truncated": eligible > len(rows),
              "unknown_routing": unknown, "routing_complete": unknown == 0,
              "unseeded_decisions": unseeded,
              "coverage_complete": unseeded == 0}
    if len(canonical(result)) > MAX_CLOSED_PAGE_BYTES:
        raise LimitExceeded("Work-item events exceed page byte bound")
    return result


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
            if not isinstance(event, dict):
                raise Denied("Invalid work-item acknowledgement event")
            item_id = event.get("id")
            # Reuse full seed/chain validation, never trust a delivered mutable id.
            item = store._get(item_id, history_revisions=store._history_revisions(item_id, recover=False))
            if item["status"] not in ("done", "dropped") or event != _payload(store, item):
                raise Denied("Work-item acknowledgement differs from terminal event")
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
            except FileExistsError:
                # Concurrent runs may finish the same batch. A valid existing
                # exact receipt wins; malformed or conflicting data fails closed.
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
    for name in names[:limit]:
        role = roles[name]
        page = pending(store, name, role.work_item_events, sources=role.decision_sources, limit=10)
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
