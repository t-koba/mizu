"""One terminal sidecar for an exhausted, fully validated WorkItems chain.

Authority is the existing transition grant/runtime actor, not a new caller
field. The chain hash covers canonical seed and revisions, each followed by LF.
No state supplied by this record can alter scope, origin or delivery evidence.
"""
from .errors import Denied, LimitExceeded
from .fs import PREVIEW_BYTES, canonical, digest, identifier, now, write_json


def request(item_id, expected_rev, status, actor, reason):
    from .work_items import MAX_REVISIONS
    identifier(actor)
    if (type(expected_rev) is not int or expected_rev != MAX_REVISIONS
            or status != "dropped" or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 4000):
        raise Denied("Exhausted work requires explicit dropped retirement with a 1-4000 character reason")
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        raise Denied("Retirement reason must be valid UTF-8") from None
    return {"id": item_id, "expected_rev": expected_rev, "status": status,
            "actor": actor, "reason": reason.strip()}


def read(store, current, chain_sha256):
    """Validate without recovery/writes, after replaying every ordinary record."""
    from .work_items import MAX_REVISIONS, _timestamp
    name = f"work-items/{current['id']}/retirement.json"
    try:
        (store.root / name).lstat()
    except FileNotFoundError:
        return None
    record = store._read(name)
    fields = {"schema", "request", "request_sha256", "chain_sha256", "created_at"}
    req = record.get("request")
    if (set(record) != fields or type(record.get("schema")) is not int
            or record["schema"] != 1 or not isinstance(req, dict)
            or set(req) != {"id", "expected_rev", "status", "actor", "reason"}
            or current["rev"] != MAX_REVISIONS or current["status"] not in ("open", "doing", "blocked")
            or record.get("chain_sha256") != chain_sha256
            or not _timestamp(record.get("created_at"))):
        raise Denied("Invalid work-item retirement binding or fields")
    normalized = request(current["id"], req["expected_rev"], req["status"], req["actor"], req["reason"])
    if req != normalized or record["request_sha256"] != digest(canonical(normalized)):
        raise Denied("Invalid work-item retirement request identity")
    project(current, record)  # Enforce the ordinary event byte bound on reads too.
    return record


def project(current, record):
    if record is None:
        return current
    req = record["request"]
    projected = {**current, "status": "dropped", "holder": None, "wait": None,
            "actor": req["actor"], "reason": req["reason"], "updated_at": record["created_at"],
            "transition": {"from": current["status"], "to": "dropped"},
            "snapshot_id": None, "verification": None, "delivered_tree": None,
            "verification_scope": None}
    if len(canonical(projected)) > PREVIEW_BYTES:
        raise LimitExceeded("Work-item event exceeds byte bound")
    return projected


def retire(store, item_id, *, expected_rev, status, actor, reason, history_revisions,
           wait, snapshot_id, scope, smoke_refs):
    """Caller holds the ordinary transition locks; exact replay writes nothing."""
    req = request(item_id, expected_rev, status, actor, reason)
    if any(value is not None for value in (wait, snapshot_id, scope, smoke_refs)):
        raise Denied("Retirement cannot change scope, evidence, references or waits")
    state = {}
    current = None
    for current in store._iter_chain(item_id, history_revisions=history_revisions, retirement=state):
        pass
    from .work_items import MAX_REVISIONS
    if current["rev"] != MAX_REVISIONS or current["status"] not in ("open", "doing", "blocked"):
        raise Denied("Retirement requires an exhausted nonterminal work item")
    record = state.get("record")
    if record is not None:
        if record["request"] != req:
            raise Denied("Work-item retirement conflicts with the committed request")
    else:
        record = {"schema": 1, "request": req, "request_sha256": digest(canonical(req)),
                  "chain_sha256": state["chain_sha256"], "created_at": now()}
        if len(canonical(record)) > PREVIEW_BYTES:
            raise LimitExceeded("Work-item retirement exceeds byte bound")
        from .smoke_refs import require_pairs
        require_pairs(store.root, current.get("smoke_refs", []))
        project(current, record)  # Refuse an unreadable projection before committing.
        write_json(store.root / "work-items" / item_id / "retirement.json", record, exclusive=True)
        store._wake_terminal()
    return project(current, record)
