"""Snapshot- or receipt-pinned work-item CI waits; host-only observation, never publication.

At most poll_max_branches adapter calls per due tick, rotating by item/revision/candidate snapshot.
Ledger scans stream with O(limit) retained rows. Evidence is create-only, bounded
and no-follow; exact digest acknowledgements follow successful run completion.
All decisions still belong to the holder: neither observation nor ack transitions
an item. Missing/invalid evidence fails closed; interrupted delivery replays.
"""
from __future__ import annotations

import contextlib
import heapq
import json
import os
from dataclasses import asdict

from .errors import Denied, LimitExceeded
from .fs import (MAX_FRAME, PREVIEW_BYTES, canonical, digest, identifier, loads_finite, lock,
                 mkdir, safe_read, sync_dir, write_json)
from .fs import now as observed_now
from .shared_records import RECORD_BYTES, RECEIPT_BYTES, KindPolicy, SharedRecords, _token
from .publication import Publication
from .snapshot import SnapshotNotDescendant
from .vcs import _reject_terminal_controls, parse_status_checks
from .work_items import _directory, _timestamp

MAX_CHECK_SET_BYTES = 2048


def normalize(root, value, *, stored=False):
    fields = {"kind", "publication_id", "checks"}
    if stored:
        fields |= {"branch", "sha"}
    if not isinstance(value, dict) or set(value) != fields or value.get("kind") != "ci":
        raise Denied("Invalid work-item CI wait fields")
    checks = value["checks"]
    _check_names(checks)
    receipt = Publication.read_receipt(root, value["publication_id"])
    result = {"kind": "ci", "publication_id": receipt["publication_id"],
              "branch": receipt["branch"], "sha": receipt["published_sha"],
              "checks": sorted(checks)}
    if stored and result != value:
        raise Denied("CI wait differs from its immutable publication receipt")
    return result


def normalize_publication(value, *, snapshots, branch=None, stored=False):
    """Pin a published snapshot before a publication receipt exists.

    Registration holds publication.lock through the ledger append. Replay
    checks immutable content, not membership in the rolling history or current
    configuration. Receipt selection belongs only to the host poll/evidence path.
    """
    fields = {"kind", "snapshot_id", "checks"}
    if stored:
        fields |= {"code_digest", "branch"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("kind") != "publication_ci"):
        raise Denied("Invalid work-item publication CI wait fields")
    _check_names(value["checks"])
    snapshot = snapshots.binding(value["snapshot_id"])
    if not stored and snapshot["id"] not in snapshots._history_ids():
        raise Denied("Publication CI wait requires retained published history")
    from .vcs import check_branch
    pinned_branch = value["branch"] if stored else branch
    check_branch(pinned_branch)
    result = {"kind": "publication_ci", "snapshot_id": snapshot["id"],
              "code_digest": snapshot["code_digest"], "branch": pinned_branch,
              "checks": sorted(value["checks"])}
    if stored and result != value:
        raise Denied("Publication CI wait differs from its immutable snapshot")
    return result


def _resolved(store, wait, observed_snapshot=None, *, receipt_cache=None, wait_distance=None):
    if wait["kind"] == "ci":
        return wait
    target = wait
    if observed_snapshot is not None:
        if (not isinstance(observed_snapshot, dict)
                or set(observed_snapshot) != {"snapshot_id", "code_digest"}):
            raise Denied("Invalid observed CI snapshot")
        if wait_distance is None:
            snapshot = store.snapshots.require_descendant(
                wait["snapshot_id"], observed_snapshot["snapshot_id"])
        else:
            wait_distance(observed_snapshot["snapshot_id"])
            snapshot = store.snapshots.get(observed_snapshot["snapshot_id"])
        if snapshot["code_digest"] != observed_snapshot["code_digest"]:
            raise Denied("Observed CI snapshot digest mismatch")
        target = {**wait, **observed_snapshot}
    # Only reconciliation's advisory scan supplies a call-local cache. Locked
    # transitions and ordinary CI polling always list afresh by default.
    key = (wait["branch"], target["code_digest"])
    if receipt_cache is not None and key in receipt_cache:
        publication_id = receipt_cache[key]
    else:
        # The ordinary listing skips corrupt records; uniqueness needs strict
        # evidence. Exceptions escape without creating a cache entry.
        page = Publication.list_receipts(store.root, code_digest=target["code_digest"],
                                         branch=wait["branch"], limit=2, strict=True,
                                         policy=store.publication_evidence)
        publication_id = (page["publications"][0]["publication_id"]
                          if not page["truncated"] and not page["scan_stopped"]
                          and page["total"] == 1 and len(page["publications"]) == 1 else None)
    if publication_id is not None:
        receipt = Publication.read_receipt(store.root, publication_id)
        if receipt["code_digest"] != target["code_digest"] or receipt["branch"] != wait["branch"]:
            return None
    if receipt_cache is not None and len(receipt_cache) < store.snapshots.history_index:
        receipt_cache[key] = publication_id
    if publication_id is None:
        return None
    return {**wait, "publication_id": receipt["publication_id"], "sha": receipt["published_sha"],
            "snapshot_relation": "strict_descendant" if observed_snapshot is not None else "exact",
            **({"observed_snapshot": observed_snapshot} if observed_snapshot is not None else {})}


def _complete_status(wait, data):
    """No partial/unknown observation can resolve a pre-publication wait."""
    return (isinstance(data, dict)
            and data.get("branch", wait["branch"]) == wait["branch"]
            and data.get("sha", wait["sha"]) == wait["sha"]
            and data.get("complete", True) is True
            and data.get("truncated", False) is False
            and data.get("scan_stopped", False) is False
            and data.get("cursor") is None)


def _check_names(checks):
    if (not isinstance(checks, list) or not 1 <= len(checks) <= 64
            or any(not isinstance(c, str) or not c or len(c) > 256
                   for c in checks)):
        raise Denied("CI waits require 1-64 unique nonempty check names of at most 256 characters")
    for check in checks:
        _reject_terminal_controls(check, "Invalid CI wait check name")
    try:
        serialized_checks = canonical(checks)
    except UnicodeError as exc:
        raise Denied("Invalid CI check name encoding") from exc
    if len(serialized_checks) > MAX_CHECK_SET_BYTES:
        raise Denied("Serialized CI check set exceeds 2048 UTF-8 bytes")
    if len(set(checks)) != len(checks):
        raise Denied("Duplicate CI wait check name")


def _items(store):
    for item in store.current_items(statuses=("blocked",)):
        if (item.get("wait") or {}).get("kind") in ("ci", "publication_ci"):
            yield item


def _path(store, item, suffix="result"):
    identifier(item["id"])
    return store.root / "evidence" / "ci-waits" / f"{item['id']}.{item['rev']}.{suffix}.json"


def _read(store, item):
    path = _path(store, item)
    for parent in (path.parent.parent, path.parent):
        if not _directory(parent):
            return None
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    raw = safe_read(store.root, path.relative_to(store.root).as_posix(), PREVIEW_BYTES)
    try:
        record = loads_finite(raw)
    except (ValueError, UnicodeError) as exc:
        raise Denied("Invalid CI wait evidence") from exc
    if not isinstance(record, dict):
        raise Denied("Invalid CI wait evidence")
    return record


def terminal(wait, data):
    """Only the declared set, exactly once per name and exact revision."""
    rows = parse_status_checks(data)
    selected = []
    for name in wait["checks"]:
        matches = [row for row in rows if row["check"] == name]
        if (len(matches) != 1 or matches[0]["sha"] != wait["sha"]
                or matches[0]["state"] not in ("success", "failure")):
            return None
        row = matches[0]
        selected.append({"check": name, "sha": row["sha"], "state": row["state"],
                         **({"run": row["run"]} if "run" in row else {}),
                         **{key: row[key] for key in ("started_at", "completed_at", "job_seconds")
                            if key in row}})
    return selected


# Bounds on the additional status projection, independent of the ordinary checks.
MAX_DIAGNOSTIC_ITEMS = 10
MAX_DIAGNOSTIC_ROWS = 32


def diagnose(wait, data):
    """Pure current observation, not terminal evidence or a permanent failure.

    The wait is receipt-normalized by the caller. Validate check-name bounds
    here too so the returned rows are bounded independently of the adapter.
    A supplied branch mismatch is informative; read_via normally synthesizes
    the requested branch, so equality is not proof of an observed branch.
    """
    _check_names(wait["checks"])
    rows = parse_status_checks(data)
    result = []
    for name in wait["checks"]:
        matches = [row for row in rows if row["check"] == name]
        pinned = [row for row in matches if row["sha"] == wait["sha"]]
        if data.get("branch", wait["branch"]) != wait["branch"]:
            observation = "branch_mismatch"
        elif not matches:
            observation = "absent"
        elif len(matches) != 1:
            observation = "duplicate"
        elif not pinned:
            observation = "wrong_sha"
        elif pinned[0]["state"] not in ("success", "failure"):
            observation = "nonterminal"
        else:
            observation = "terminal"
        result.append({"check": name, "observation": observation,
                       "exact_name_count": len(matches),
                       "wrong_sha_count": len(matches) - len(pinned)})
    return result


def status_diagnostics(store, role, publication, data):
    """Read-only, holder-only bounded projection using this status response.

    Stream local waits with bounded retention; safely read source evidence and
    shared receipts, but never write, acknowledge, transition or call the adapter.
    Omitted items/rows
    are explicit; unsafe or unreadable evidence refuses the projection.
    """
    waits = (item for item in _items(store)
             if item["holder"] == role and item["wait"]["kind"] == "ci"
             and all(item["wait"][key] == publication[receipt_key]
                     for key, receipt_key in (("publication_id", "publication_id"),
                                              ("branch", "branch"), ("sha", "published_sha"))))
    selected = heapq.nsmallest(MAX_DIAGNOSTIC_ITEMS + 1, waits, key=lambda item: item["id"])
    if not selected:
        return None
    rows = parse_status_checks(data)
    return {"items": [{"work_item": item["id"], "rev": item["rev"],
                       "checks": diagnose(item["wait"], data),
                       **({"acknowledgement": "corrupt"}
                          if _shared_ack_corrupt(store, item) else {})}
                      for item in selected[:MAX_DIAGNOSTIC_ITEMS]],
            "items_truncated": len(selected) > MAX_DIAGNOSTIC_ITEMS,
            "observed_checks": [{key: row[key] for key in ("check", "sha", "state", "run")
                                 if key in row} for row in rows[:MAX_DIAGNOSTIC_ROWS]],
            "observed_checks_truncated": len(rows) > MAX_DIAGNOSTIC_ROWS}


def _evidence(store, item, *, sync=True):
    from .host_records import publication_result
    outcome = publication_result(store, item, sync=sync)
    record = _read(store, item)
    if record is None:
        return outcome
    if outcome is not None:
        raise Denied("Conflicting publication terminal evidence")
    wait = _resolved(store, item["wait"], record.get("observed_snapshot"))
    if wait is None:
        raise Denied("CI evidence has no unique matching publication")
    binding = ("publication_id", "branch", "sha")
    if wait["kind"] == "publication_ci":
        binding += ("snapshot_id", "code_digest")
    fields = {"kind", "trust", "work_item", "rev", "holder", "publication_id", "branch", "sha",
              "wait_digest", "checks", "conclusion", "observed_at"}
    if wait["kind"] == "publication_ci":
        fields |= {"snapshot_id", "code_digest"}
        if "snapshot_relation" in record:
            fields.add("snapshot_relation")
        relation = wait["snapshot_relation"]
        if record.get("snapshot_relation", relation) != relation:
            raise Denied("CI snapshot relation differs from validated evidence")
    if "observed_snapshot" in wait:
        fields.add("observed_snapshot")
        binding += ("observed_snapshot",)
    if (set(record) != fields or record["kind"] != "ci"
            or record["trust"] != "external-untrusted"
            or record["work_item"] != item["id"] or record["rev"] != item["rev"]
            or type(record["rev"]) is not int or record["holder"] != item["holder"]
            or record["wait_digest"] != digest(canonical(item["wait"]))
            or any(record[k] != wait[k] for k in binding)
            or not _timestamp(record["observed_at"])):
        raise Denied("CI evidence differs from blocked waiter")
    rows = terminal(wait, {"checks": record["checks"]})
    if (rows != record["checks"] or rows is None
            or record["conclusion"] != _conclusion(rows)):
        raise Denied("Invalid terminal CI evidence")
    # Recover a previous post-rename directory-sync failure before delivery.
    if sync:
        sync_dir(_path(store, item).parent)
    event = {**record}
    if wait["kind"] == "publication_ci":
        event["snapshot_relation"] = wait["snapshot_relation"]
    return {**event, "evidence_digest": digest(canonical(record))}


def _conclusion(rows):
    return "success" if all(r["state"] == "success" for r in rows) else "failure"


def pending(store, role, *, limit=10):
    """Read current holder deliveries from exact SharedRecords receipts."""
    with lock(store.root / "locks" / "insights.lock"):
        return _shared_page(store, role, limit=limit, persist=False)[0]


def readiness_init(store, role, *, limit=10):
    """Start a read-only readiness accumulator for the shared wait scan.

    Validates the role and limit exactly like the read-only page, retains
    only O(limit) candidates and performs no reads, writes, syncs or
    recovery. The caller holds ``insights.lock`` for the traversal and
    finalization.
    """
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid CI wait delivery limit")
    return {"role": role, "limit": limit, "shared": shared_store(store),
            "selected": []}


def readiness_feed(state, store, item):
    """Feed one fully reconstructed work item to the CI readiness accumulator.

    Items that are not blocked CI waiters for this holder are ignored.
    Per-item route failures use the exact read-only page contract
    ``(Denied, LimitExceeded, OSError, ValueError)`` and skip only that
    waiter so another holder's evidence is still found. Iterator, seed and
    full-chain reconstruction failures are never caught here; they abort
    the shared traversal. No recovery, writes or syncs are performed.
    """
    if item["status"] != "blocked":
        return
    if (item.get("wait") or {}).get("kind") not in ("ci", "publication_ci"):
        return
    if item["holder"] != state["role"]:
        return
    try:
        observed = evidence(store, item, sync=False)
        if observed is None:
            return
        envelope, computed = observed
        if (computed is not None
                and state["shared"].received(state["role"], envelope["id"], computed)):
            return
    except (Denied, LimitExceeded, OSError, ValueError):
        return  # one broken waiter cannot hide another holder's evidence
    state["selected"].append((item["updated_at"], item["id"], envelope))
    state["selected"].sort(key=lambda row: row[:2])
    del state["selected"][state["limit"]:]


def readiness_events(state):
    """Finalize the CI readiness accumulator without writes or syncs.

    Preserves the read-only page ordering and generic-frame byte checks.
    Raises ``LimitExceeded`` exactly like the read-only page when a single
    valid row cannot fit the frame.
    """
    selected = state["selected"]
    role = state["role"]
    if _page_bytes([]) > MAX_FRAME:
        raise LimitExceeded("CI wait page metadata exceeds byte bound")
    events = []
    for _, _, envelope in selected:
        computed = digest(canonical(envelope))
        event = {**envelope["payload"], "record_id": envelope["id"],
                 "record_digest": computed,
                 "ack_token": _token(role, envelope["id"], computed)}
        if _page_bytes(events + [event]) > MAX_FRAME:
            if not events:
                raise LimitExceeded("CI wait event exceeds the generic prompt frame bound")
            break
        events.append(event)
    return events


# CI and declared host publication outcomes remain source authorities.
# SharedRecords owns only immutable envelopes,
# manifests and explicit per-role receipts; legacy auto-acks are never imported.


def _validate_record(subject, payload):
    if (set(subject) != {"work_item", "rev", "holder"}
            or any(subject[key] != payload.get(key) for key in subject)
            or (payload.get("kind"), payload.get("trust")) not in
                (("ci", "external-untrusted"), ("publication_outcome", "host-record"))):
        raise Denied("Invalid shared CI wait binding")


def shared_store(store):
    # Generic record bound only. The per-source 128 KiB evidence limit stays at
    # poll/write (canonical source record); no smaller aggregate cap is added.
    return SharedRecords(store.root, {"ci.wait": KindPolicy(
        frozenset({"host.ci"}), _validate_record, maximum=RECORD_BYTES)})


def _envelope(item, event):
    identity = f"ci-wait:{item['id']}:{item['rev']}"
    if "snapshot_relation" in event:
        identity += ":snapshot-relation-v1"
    return {"schema": 1, "id": identity,
            "kind": "ci.wait", "created_at": item["updated_at"], "writer": "host.ci",
            "subject": {key: item[key] for key in ("rev", "holder")} | {"work_item": item["id"]},
            "payload": event}


_LEGACY_ACKNOWLEDGED = object()


def _legacy_acknowledged(shared, envelope):
    """Do not redeliver a receipted snapshot result under its versioned identity."""
    event = envelope["payload"]
    if "snapshot_relation" not in event:
        return False
    identity = envelope["id"].removesuffix(":snapshot-relation-v1")
    legacy = {**envelope, "id": identity,
              "payload": {key: value for key, value in event.items()
                          if key != "snapshot_relation"}}
    if any(shared._read(name, RECORD_BYTES) is not None
           for name in shared._record_names(identity)):
        _, computed = shared.get(identity)
        if computed != digest(canonical(legacy)):
            raise Denied("Legacy shared CI evidence differs from its source")
        return shared.received(envelope["subject"]["holder"], identity, computed)
    if shared._read(shared._receipt_name(envelope["subject"]["holder"], identity),
                    RECEIPT_BYTES) is not None:
        raise Denied("Legacy shared CI receipt has no record")
    return False


def _existing(shared, envelope):
    # Read-only eligibility/wake checks must not create records. Exact replay
    # also handles a previous create-only write whose directory sync failed.
    if any(shared._read(name, RECORD_BYTES) is not None
           for name in shared._record_names(envelope["id"])):
        _, computed = shared.get(envelope["id"])
        if computed != digest(canonical(envelope)):
            raise Denied("Shared CI evidence differs from current source")
        return computed
    if shared._read(shared._receipt_name(envelope["subject"]["holder"], envelope["id"]),
                    RECEIPT_BYTES) is not None:
        raise Denied("Shared CI receipt has no record")
    if _legacy_acknowledged(shared, envelope):
        return _LEGACY_ACKNOWLEDGED
    return None


def _terminal_observation(store, item, *, sync=False):
    event = _evidence(store, item, sync=sync)
    if event is None:
        return None
    shared = shared_store(store)
    envelope = _envelope(item, event)
    shared._validate(envelope)
    return envelope, _existing(shared, envelope)


def terminal_evidence(store, item, *, sync=False):
    """Current immutable terminal result, whether or not its notice was acked."""
    observed = _terminal_observation(store, item, sync=sync)
    if observed is None:
        return None
    envelope, existing = observed
    return envelope, None if existing is _LEGACY_ACKNOWLEDGED else existing


def evidence(store, item, *, sync=False):
    """Current notification candidate, suppressing receipted legacy identity.

    Readiness and delivery use this view to avoid redelivery. Terminal
    settlement uses :func:`terminal_evidence` so acknowledgement cannot erase
    the durable CI outcome.
    """
    observed = _terminal_observation(store, item, sync=sync)
    if observed is None or observed[1] is _LEGACY_ACKNOWLEDGED:
        return None
    return observed


def _shared_ack_corrupt(store, item):
    observed = evidence(store, item)
    if observed is None or observed[1] is None:
        return False
    envelope, computed = observed
    shared = shared_store(store)
    shared._read(shared._receipt_name(item["holder"], envelope["id"]), RECEIPT_BYTES)
    try:
        shared.received(item["holder"], envelope["id"], computed)
    except Denied:
        return True
    return False


def _page_bytes(events) -> int:
    # Actual prompt serialization for the wait_events contribution, not
    # canonical list framing. prompt_for uses json.dumps with the default
    # ensure_ascii=True, so emoji and framing measure larger here than in
    # canonical bytes; the generic 1 MiB frame is the only aggregate bound.
    return len(json.dumps({"wait_events": events}, ensure_ascii=True).encode("utf-8"))


def _shared_page(store, role, *, limit=10, persist):
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid CI wait delivery limit")
    shared = shared_store(store)
    selected = []
    for item in _items(store):  # full history replay and publication validation
        if item["holder"] != role:
            continue
        try:
            observed = evidence(store, item, sync=persist)
            if observed is None:
                continue
            envelope, computed = observed
            if computed is not None and shared.received(role, envelope["id"], computed):
                continue
        except (Denied, LimitExceeded, OSError, ValueError):
            continue  # one broken waiter cannot hide another holder's evidence
        selected.append((item["updated_at"], item["id"], envelope))
        selected.sort(key=lambda row: row[:2])
        del selected[limit:]
    # No smaller aggregate cap: per-source evidence stays bounded at 128 KiB at
    # poll/write, each shared envelope at the generic record bound, and the
    # page at the existing generic 1 MiB frame measured as serialized above.
    if _page_bytes([]) > MAX_FRAME:
        raise LimitExceeded("CI wait page metadata exceeds byte bound")
    events, manifest = [], ()
    for _, _, envelope in selected:
        computed = digest(canonical(envelope))
        event = {**envelope["payload"], "record_id": envelope["id"], "record_digest": computed,
                 "ack_token": _token(role, envelope["id"], computed)}
        # Keep a contiguous oldest-first prefix. A full page leaves this and
        # later rows pending; after the delivered prefix is receipted, replay
        # starts here. One valid row fits an empty frame under the source bound.
        if _page_bytes(events + [event]) > MAX_FRAME:
            if not events:
                raise LimitExceeded("CI wait event exceeds the generic prompt frame bound")
            break
        if persist:
            shared.put(envelope, writer="host.ci")
            manifest += shared.freeze(role, [envelope["id"]])
        events.append(event)
    return events, manifest


def delivery(store, role, *, limit=10):
    """Freeze only current holder deliveries; no model-supplied manifest."""
    with lock(store.root / "locks" / "insights.lock"):
        return _shared_page(store, role, limit=limit, persist=True)


def _selected(context, tokens):
    shared = shared_store(context.project.work_items)
    allowed = shared._manifest(context.role.name, context.ci_wait_manifest)
    if len(set(tokens)) != len(tokens) or any(token not in allowed for token in tokens):
        raise Denied("CI token is not in this unit's manifest")
    selected = [allowed[token] for token in tokens]
    for entry in selected:
        envelope, _ = shared.get(entry.record_id, expected_digest=entry.record_digest)
        store = context.project.work_items
        item_id = envelope["subject"]["work_item"]
        item = store.current(item_id)
        if (item["status"] != "blocked" or item["holder"] != context.role.name
                or item["rev"] != envelope["subject"]["rev"]
                or (item.get("wait") or {}).get("kind") not in ("ci", "publication_ci")):
            raise Denied("CI waiter revision or holder changed")
        event = _evidence(store, item)
        if event is None or digest(canonical(_envelope(item, event))) != entry.record_digest:
            raise Denied("CI evidence changed")
        shared.received(context.role.name, entry.record_id, entry.record_digest)
    return shared, selected


def validate_selection(context, tokens):
    with lock(context.project.root / "locks" / "insights.lock"):
        _selected(context, tokens)


def complete(context, finished):
    """Apply explicitly selected current CI/publication-wait receipts after success."""
    if (not context.ci_wait_manifest or finished.get("wait_for_check")
            or finished.get("outcome") == "bounded" or context.bound_hit):
        return
    tokens = set(finished.get("ack_events", []))
    selected = [entry.ack_token for entry in context.ci_wait_manifest if entry.ack_token in tokens]
    if not selected:
        return
    with lock(context.project.root / "locks" / "insights.lock"):
        shared, entries = _selected(context, selected)
        shared.acknowledge(context.role.name, [asdict(entry) for entry in entries],
                           manifest=context.ci_wait_manifest)


def _candidates(store, item, descendants, *, newest_first=False, wait_distance=None):
    """Discover bounded descendants; a supplied checker requires exact negatives."""
    wait = item["wait"]
    if wait["kind"] == "ci":
        yield "", None
        return
    yield wait["snapshot_id"], None
    if descendants:
        # History order is not ancestry. Ordinary polling skips ineligible or
        # corrupt manifests so they cannot consume the status window.
        if wait_distance is None:
            try:
                distance = store.snapshots._descendant_distance_from(wait["snapshot_id"])
            except (Denied, LimitExceeded, OSError, ValueError):
                return
        else:
            distance = wait_distance
        seen = {wait["snapshot_id"]}
        history = store.snapshots._history_ids()[-store.snapshots.history_index:]
        for sid in reversed(history) if newest_first else history:
            if sid in seen:
                continue
            seen.add(sid)
            try:
                distance(sid)
            except (Denied, LimitExceeded, OSError, ValueError) as exc:
                # Reconciliation must stop on unknown ancestry, not fall back
                # to an older receipt. Ordinary polling keeps its skip policy.
                if wait_distance is not None and not isinstance(exc, SnapshotNotDescendant):
                    raise
                continue
            yield sid, sid


def _candidate_wait(store, wait, candidate):
    observed = None
    if candidate is not None:
        snapshot = store.snapshots.get(candidate)
        observed = {"snapshot_id": snapshot["id"], "code_digest": snapshot["code_digest"]}
    return _resolved(store, wait, observed)


def _poll_error_code(exc):
    """Return a bounded diagnostic class, never exception text or an item ID."""
    if isinstance(exc, LimitExceeded):
        return "limit"
    if isinstance(exc, Denied):
        return "denied"
    if isinstance(exc, OSError):
        return "io"
    return "invalid"


def poll(config, project, state, *, now, interval, reader=None):
    from .vcs_poll_policy import (capped_vcs_settings, upstream_fetch_due,
                                  vcs_poll_max_branches, vcs_poll_status_timeout)
    from .vcs import read_via
    if not config.vcs.get("poll_enabled") or not config.vcs.get("command"):
        return None
    if not upstream_fetch_due(state.get("last_ci_wait_poll"), now, interval):
        return None
    state["last_ci_wait_poll"] = now
    limit = vcs_poll_max_branches(config)
    cursor = state.get("ci_wait_cursor", "")
    if not isinstance(cursor, str):
        cursor = ""
    # Two bounded heaps implement a wrap-around window without storing the ledger.
    after, before = [], []
    errors = []
    try:
        for item in _items(project.work_items):
            try:
                if _evidence(project.work_items, item) is not None:
                    continue
            except (Denied, LimitExceeded, OSError, ValueError) as exc:
                # Never overwrite invalid immutable evidence or spend adapter calls on it.
                if len(errors) < limit:
                    errors.append(_poll_error_code(exc))
                continue
            for sid, candidate in _candidates(project.work_items, item,
                    config.vcs.get("ci_wait_descendants", True)):
                token = f"{item['id']}:{item['rev']:020d}:{sid}"
                bucket = after if token > cursor else before
                # Negated numeric identity tuple gives a bounded max-heap.
                key = (-int(item["id"][3:], 16), -item["rev"], -int(sid or "0", 16))
                entry = (key, token, item, candidate)
                if len(bucket) < limit:
                    heapq.heappush(bucket, entry)
                elif key > bucket[0][0]:
                    heapq.heapreplace(bucket, entry)
        targets = (sorted(after, reverse=True) + sorted(before, reverse=True))[:limit]
        observed = 0
        for _, token, item, candidate in targets:
            state["ci_wait_cursor"] = token  # advance even if the adapter fails
            try:
                wait = _candidate_wait(project.work_items, item["wait"], candidate)
                if wait is None:
                    continue
                # Preserve mismatched rows for fail-closed declared-set evaluation.
                params = {"branch": wait["branch"], "sha": wait["sha"]}
                data = (reader(config.vcs, "status", params) if reader else read_via(
                    capped_vcs_settings(config.vcs, vcs_poll_status_timeout(config)),
                    "status", params, preserve_status_rows=True))
                if wait["kind"] == "publication_ci" and not _complete_status(wait, data):
                    continue
                if data.get("branch", wait["branch"]) != wait["branch"]:
                    continue
                rows = terminal(wait, data)
                if rows is None:
                    continue
                with lock(project.root / "locks" / "insights.lock"):
                    current = project.work_items.current(item["id"])
                    if current != item or _evidence(project.work_items, current) is not None:
                        continue
                    if _candidate_wait(project.work_items, current["wait"], candidate) != wait:
                        continue
                    binding = ("publication_id", "branch", "sha")
                    if wait["kind"] == "publication_ci":
                        binding += ("snapshot_id", "code_digest", "snapshot_relation")
                    if "observed_snapshot" in wait:
                        binding += ("observed_snapshot",)
                    record = {"kind": "ci", "trust": "external-untrusted",
                              "work_item": item["id"], "rev": item["rev"],
                              "holder": item["holder"],
                              **{k: wait[k] for k in binding},
                              "wait_digest": digest(canonical(item["wait"])), "checks": rows,
                              "conclusion": _conclusion(rows), "observed_at": observed_now()}
                    if len(canonical(record)) > PREVIEW_BYTES:
                        raise Denied("CI wait evidence exceeds byte bound")
                    _read(project.work_items, item)  # screen parents before writing
                    path = _path(project.work_items, item)
                    mkdir(path.parent)
                    sync_dir(path.parent.parent)
                    sync_dir(project.root)
                    write_json(path, record, exclusive=True)
                    observed += 1
                    # Coarse platform-watch hint only, after durable evidence.
                    # Admission rechecks exact current holder/revision/digest.
                    with contextlib.suppress(OSError, NotImplementedError, Denied):
                        directory = project.root / "waits"
                        _directory(directory)
                        mkdir(directory)
                        os.utime(directory, None, follow_symlinks=False)
            except (Denied, LimitExceeded, OSError, ValueError) as exc:
                if len(errors) < limit:
                    errors.append(_poll_error_code(exc))
        return {"event": "ci_wait_poll", "ok": not errors, "polled": len(targets),
                "observed": observed, "errors": errors}
    except (Denied, LimitExceeded, OSError, ValueError) as exc:
        return {"event": "ci_wait_poll", "ok": False, "error": _poll_error_code(exc)}
