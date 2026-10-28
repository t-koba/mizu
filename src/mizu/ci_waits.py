"""Receipt-pinned work-item CI waits; host-only observation, never publication.

At most poll_max_branches adapter calls per due tick, rotating by item ID.
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
from .vcs import _reject_terminal_controls, parse_status_checks
from .work_items import _directory, _timestamp

MAX_CHECK_SET_BYTES = 2048


class _CorruptAck(Denied):
    """Safely read acknowledgement bytes failed content validation."""


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
    for event_id, event in store._iter_item_events(recover=False):
        kind, seed = store._seed_for_event(event_id, event, readonly=True)
        if kind == "seed":
            item = store._get(seed["id"], seed_event=event,
                              history_revisions=store._history_revisions(seed["id"], recover=False))
            if item["status"] == "blocked" and (item.get("wait") or {}).get("kind") == "ci":
                yield item


def _path(store, item, suffix="result"):
    identifier(item["id"])
    return store.root / "evidence" / "ci-waits" / f"{item['id']}.{item['rev']}.{suffix}.json"


def _read(store, item, suffix="result"):
    path = _path(store, item, suffix)
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
        raise (_CorruptAck if suffix == "ack" else Denied)("Invalid CI wait evidence") from exc
    if not isinstance(record, dict):
        raise (_CorruptAck if suffix == "ack" else Denied)("Invalid CI wait evidence")
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


def status_diagnostics(store, role, publication, data, *, shared=False):
    """Read-only, holder-only bounded projection using this status response.

    Stream local waits with bounded retention; safely read local evidence and acks,
    but never write, acknowledge, transition or call the adapter. Omitted items/rows
    are explicit; unsafe or unreadable evidence refuses the projection.
    """
    waits = (item for item in _items(store)
             if item["holder"] == role
             and all(item["wait"][key] == publication[receipt_key]
                     for key, receipt_key in (("publication_id", "publication_id"),
                                              ("branch", "branch"), ("sha", "published_sha"))))
    selected = heapq.nsmallest(MAX_DIAGNOSTIC_ITEMS + 1, waits, key=lambda item: item["id"])
    if not selected:
        return None
    rows = parse_status_checks(data)
    return {"items": [{"work_item": item["id"], "rev": item["rev"],
                       "checks": diagnose(item["wait"], data),
                       **({"acknowledgement": "corrupt"} if (_ack_corrupt(store, item)
                           or (shared and _shared_ack_corrupt(store, item))) else {})}
                      for item in selected[:MAX_DIAGNOSTIC_ITEMS]],
            "items_truncated": len(selected) > MAX_DIAGNOSTIC_ITEMS,
            "observed_checks": [{key: row[key] for key in ("check", "sha", "state", "run")
                                 if key in row} for row in rows[:MAX_DIAGNOSTIC_ROWS]],
            "observed_checks_truncated": len(rows) > MAX_DIAGNOSTIC_ROWS}


def _evidence(store, item, *, sync=True):
    record = _read(store, item)
    if record is None:
        return None
    fields = {"kind", "trust", "work_item", "rev", "holder", "publication_id", "branch", "sha",
              "wait_digest", "checks", "conclusion", "observed_at"}
    if (set(record) != fields or record["kind"] != "ci"
            or record["trust"] != "external-untrusted"
            or record["work_item"] != item["id"] or record["rev"] != item["rev"]
            or type(record["rev"]) is not int or record["holder"] != item["holder"]
            or record["wait_digest"] != digest(canonical(item["wait"]))
            or any(record[k] != item["wait"][k] for k in ("publication_id", "branch", "sha"))
            or not _timestamp(record["observed_at"])):
        raise Denied("CI evidence differs from blocked waiter")
    rows = terminal(item["wait"], {"checks": record["checks"]})
    if (rows != record["checks"] or rows is None
            or record["conclusion"] != _conclusion(rows)):
        raise Denied("Invalid terminal CI evidence")
    # Recover a previous post-rename directory-sync failure before delivery.
    if sync:
        sync_dir(_path(store, item).parent)
    return {**record, "evidence_digest": digest(canonical(record))}


def _acknowledged(store, item, event):
    ack = _read(store, item, "ack")
    if ack is None:
        return False
    if (set(ack) != {"role", "evidence_digest", "acknowledged_at"}
            or ack["role"] != item["holder"]
            or ack["evidence_digest"] != event["evidence_digest"]
            or not _timestamp(ack["acknowledged_at"])):
        raise _CorruptAck("Invalid CI wait acknowledgement")
    return True


def _ack_corrupt(store, item):
    # Validate evidence first: a broken shared store, unsafe path or bad result
    # must never be reported as acknowledgement corruption.
    event = _evidence(store, item, sync=False)
    try:
        if event is None:
            if _read(store, item, "ack") is not None:
                raise Denied("CI wait acknowledgement has no evidence")
        else:
            _acknowledged(store, item, event)
    except _CorruptAck:
        if event is None:
            raise Denied("CI wait acknowledgement has no evidence")
        return True
    return False


def _conclusion(rows):
    return "success" if all(r["state"] == "success" for r in rows) else "failure"


def pending(store, role, *, limit=10, config=None):
    """Freeze a current holder-routed delivery page under the lifecycle lock."""
    with lock(store.root / "locks" / "insights.lock"):
        return (_shared_page(store, role, limit=limit, persist=False)[0] if enabled(config)
                else _pending(store, role, limit=limit))


def _pending(store, role, *, limit):
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid CI wait delivery limit")
    selected = []
    for item in _items(store):
        if item["holder"] != role:
            continue
        try:
            event = _evidence(store, item)
            if event is None or _acknowledged(store, item, event):
                continue
        except (Denied, LimitExceeded, OSError, ValueError):
            # Bad per-waiter evidence must not hide another holder-routed event.
            continue
        # Oldest blocked revision first; retain only the bounded delivery page.
        selected.append((item["updated_at"], item["id"], event))
        selected.sort(key=lambda row: row[:2])
        del selected[limit:]
    return [row[2] for row in selected]


def acknowledge(store, role, events):
    identifier(role)
    with lock(store.root / "locks" / "insights.lock"):
        for event in events:
            if not isinstance(event, dict) or event.get("kind") != "ci" or "work_item" not in event:
                continue
            item = store._get(event["work_item"], history_revisions=store._history_revisions(
                event["work_item"], recover=False))
            if (item["status"] != "blocked" or item["holder"] != role
                    or item["rev"] != event.get("rev")):
                continue
            try:
                if _evidence(store, item) != event or _acknowledged(store, item, event):
                    continue
                write_json(_path(store, item, "ack"),
                           {"role": role, "evidence_digest": event["evidence_digest"],
                            "acknowledged_at": observed_now()}, exclusive=True)
            except (Denied, LimitExceeded, OSError, ValueError):
                # Leave this event unacknowledged and continue the frozen batch.
                continue


# CI is still the source authority. SharedRecords owns only immutable envelopes,
# manifests and explicit per-role receipts; legacy auto-acks are never imported.
def enabled(config):
    return config is not None and config.vcs.get("ci_wait_shared_records", False) is True


def _validate_record(subject, payload):
    if (set(subject) != {"work_item", "rev", "holder"}
            or any(subject[key] != payload.get(key) for key in subject)
            or payload.get("kind") != "ci" or payload.get("trust") != "external-untrusted"):
        raise Denied("Invalid shared CI wait binding")


def shared_store(store):
    # Generic record bound only. The per-source 128 KiB evidence limit stays at
    # poll/write (canonical source record); no smaller aggregate cap is added.
    return SharedRecords(store.root, {"ci.wait": KindPolicy(
        frozenset({"host.ci"}), _validate_record, maximum=RECORD_BYTES)})


def _envelope(item, event):
    return {"schema": 1, "id": f"ci-wait:{item['id']}:{item['rev']}",
            "kind": "ci.wait", "created_at": item["updated_at"], "writer": "host.ci",
            "subject": {key: item[key] for key in ("rev", "holder")} | {"work_item": item["id"]},
            "payload": event}


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
    return None


def _shared_ack_corrupt(store, item):
    event = _evidence(store, item, sync=False)
    if event is None:
        return False
    shared = shared_store(store)
    envelope = _envelope(item, event)
    computed = _existing(shared, envelope)
    if computed is None:
        return False
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
            event = _evidence(store, item, sync=persist)
            if event is None:
                continue
            envelope = _envelope(item, event)
            shared._validate(envelope)
            computed = _existing(shared, envelope)
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


def delivery(store, role, config):
    """Freeze only current holder deliveries; no model-supplied manifest."""
    if not enabled(config):
        return pending(store, role), ()
    with lock(store.root / "locks" / "insights.lock"):
        return _shared_page(store, role, persist=True)


def _selected(context, tokens):
    if not context.shared_ci_waits or not enabled(context.config):
        raise Denied("Shared CI wait authority changed")
    shared = shared_store(context.project.work_items)
    allowed = shared._manifest(context.role.name, context.ci_wait_manifest)
    if len(set(tokens)) != len(tokens) or any(token not in allowed for token in tokens):
        raise Denied("CI token is not in this unit's manifest")
    selected = [allowed[token] for token in tokens]
    for entry in selected:
        envelope, _ = shared.get(entry.record_id, expected_digest=entry.record_digest)
        store = context.project.work_items
        item_id = envelope["subject"]["work_item"]
        item = store._get(item_id, history_revisions=store._history_revisions(item_id, recover=False))
        if (item["status"] != "blocked" or item["holder"] != context.role.name
                or item["rev"] != envelope["subject"]["rev"]
                or (item.get("wait") or {}).get("kind") != "ci"):
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
    """Called only after successful completion, retaining the legacy no-ack gates."""
    if (not context.wait_events or finished.get("wait_for_check")
            or finished.get("outcome") == "bounded" or context.bound_hit):
        return
    if not context.shared_ci_waits:
        acknowledge(context.project.work_items, context.role.name, context.wait_events)
        return
    tokens = set(finished.get("ack_events", []))
    selected = [entry.ack_token for entry in context.ci_wait_manifest if entry.ack_token in tokens]
    with lock(context.project.root / "locks" / "insights.lock"):
        shared, entries = _selected(context, selected)
        shared.acknowledge(context.role.name, [asdict(entry) for entry in entries],
                           manifest=context.ci_wait_manifest)


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
                    errors.append(f"{item['id']}: {exc}"[:512])
                continue
            bucket = after if item["id"] > cursor else before
            # IDs are fixed lowercase hex: a negated integer is a max-heap key.
            key = -int(item["id"][3:], 16)
            entry = (key, item)
            if len(bucket) < limit:
                heapq.heappush(bucket, entry)
            elif key > bucket[0][0]:
                heapq.heapreplace(bucket, entry)
        targets = sorted(after, reverse=True) + sorted(before, reverse=True)
        targets = [entry[1] for entry in targets[:limit]]
        observed = 0
        for item in targets:
            state["ci_wait_cursor"] = item["id"]  # advance even if the adapter fails
            try:
                wait = item["wait"]
                # Preserve mismatched rows for fail-closed declared-set evaluation.
                params = {"branch": wait["branch"], "sha": wait["sha"]}
                data = (reader(config.vcs, "status", params) if reader else read_via(
                    capped_vcs_settings(config.vcs, vcs_poll_status_timeout(config)),
                    "status", params, preserve_status_rows=True))
                if data.get("branch", wait["branch"]) != wait["branch"]:
                    continue
                rows = terminal(wait, data)
                if rows is None:
                    continue
                with lock(project.root / "locks" / "insights.lock"):
                    current = project.work_items._get(item["id"], history_revisions=
                        project.work_items._history_revisions(item["id"], recover=False))
                    if current != item or _evidence(project.work_items, current) is not None:
                        continue
                    record = {"kind": "ci", "trust": "external-untrusted",
                              "work_item": item["id"], "rev": item["rev"],
                              "holder": item["holder"],
                              **{k: wait[k] for k in ("publication_id", "branch", "sha")},
                              "wait_digest": digest(canonical(wait)), "checks": rows,
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
                    errors.append(f"{item['id']}: {exc}"[:512])
        return {"event": "ci_wait_poll", "ok": not errors, "polled": len(targets),
                "observed": observed, "errors": errors}
    except (Denied, LimitExceeded, OSError, ValueError) as exc:
        return {"event": "ci_wait_poll", "ok": False, "error": str(exc)[:512]}
