"""Exact VCS change-proposal merge waits on work items.

Registration requires an exact publication receipt and a host-recorded complete
enumeration observation in the configured queue; stored waits keep only their
receipt-pinned endpoints. Only a complete poll can write the immutable,
per-revision shared merge evidence, and only the current holder can receive its
exact-receipted delivery. Observations and acknowledgements never transition a
work item: the holder decides what the merge means.
"""
from __future__ import annotations

import heapq
import json
import re
from dataclasses import asdict

from .ci_observation import POLL_STATE_FILE
from .errors import Denied, LimitExceeded
from .fs import MAX_FRAME, DIGEST, canonical, digest, identifier, lock, read_json
from .kernel.queues import QueueStore
from .publication import Publication
from .shared_records import (RECORD_BYTES, RECEIPT_BYTES, KindPolicy,
                             SharedRecords, _token)
from .smoke_refs import validate_refs
from .work_items import MAX_REVISIONS, _timestamp_order
from . import vcs

KIND = "proposal_merged"
FIELDS = {"kind", "publication_id", "proposal_id"}
_MESSAGE_ID = re.compile("[0-9a-f]{32}\\Z")
#: Singleton fact lines of a proposal observation used by a wait.
_FACTS = frozenset({"id", "state", "head", "base", "enumeration"})


def _proposal_id(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128):
        raise Denied("Invalid VCS proposal id for a merge wait")
    vcs._reject_terminal_controls(value, "Invalid VCS proposal id for a merge wait")
    return value


def _facts(body: str) -> dict:
    found = {}
    for line in body.splitlines():
        name, sep, value = line.partition(": ")
        if sep and name in _FACTS:
            if name in found:
                raise Denied("Ambiguous proposal observation")
            found[name] = value
    return found


def _observation(store, proposal_id: str) -> dict:
    """Read the latest complete observation by its fact-derived message id.

    The poll publishes one bounded proposal-id -> message-id inventory for each
    complete enumeration before writing any row. Registration reads that exact
    id, then the host-authored message it names; a missing, foreign, misplaced
    or edited body cannot pin a wait. No observation store is invented here.
    """
    queue = store.vcs.get("observation_queue")
    if not isinstance(queue, str) or not queue or queue not in store.queues:
        raise Denied("Proposal merge wait requires a configured observation queue")
    try:
        inventory = read_json(store.root / POLL_STATE_FILE, {})
        if not isinstance(inventory, dict):
            raise Denied("Invalid VCS poll state")
        complete = inventory.get("proposal_complete_messages")
        if not isinstance(complete, dict) or len(complete) > vcs.MAX_PROPOSALS:
            raise Denied("Invalid complete proposal inventory")
        for key, message_id in complete.items():
            _proposal_id(key)
            if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
                raise Denied("Invalid complete proposal inventory")
    except (OSError, ValueError, TypeError) as exc:
        raise Denied("Complete proposal inventory is unavailable") from exc
    message_id = complete.get(proposal_id)
    if message_id is None:
        raise Denied("Proposal merge wait requires a complete proposal observation")
    messages = QueueStore(store.root, store.queues)
    message = messages._message_by_id(message_id)
    if (message is None or message.get("id") != message_id
            or message.get("queue") != queue or message.get("author") != "host"):
        raise Denied("Proposal observation is not a host-recorded complete message")
    body = message.get("body")
    if (not isinstance(body, dict) or set(body) != {"title", "body"}
            or not isinstance(body["title"], str) or not isinstance(body["body"], str)):
        raise Denied("Proposal observation body is unavailable")
    if vcs._proposal_message_id(proposal_id, body["title"], body["body"]) != message_id:
        raise Denied("Proposal observation id differs from its facts")
    return {"id": message_id, "title": body["title"], "body": body["body"],
            "facts": _facts(body["body"])}


def normalize(store, value, *, stored=False):
    """Validate a new wait against the observation, or a stored wait against its pins."""
    fields = FIELDS | {"head", "base"} if stored else FIELDS
    if not isinstance(value, dict) or set(value) != fields or value.get("kind") != KIND:
        raise Denied("Invalid proposal_merged wait fields")
    proposal_id = _proposal_id(value["proposal_id"])
    receipt = Publication.read_receipt(store.root, value["publication_id"])
    if stored:
        endpoints = {key: vcs._check_endpoint(value[key]) for key in ("head", "base")}
    else:
        observation = _observation(store, proposal_id)
        facts = observation["facts"]
        state = facts.get("state")
        title = f"Proposal {proposal_id} {state}"[:vcs.MAX_PROPOSAL_TITLE_CHARS]
        if (state not in vcs.PROPOSAL_STATES or observation["title"] != title):
            raise Denied("Proposal observation is not a complete proposal fact")
        endpoints = {}
        for key in ("head", "base"):
            parts = facts.get(key, "").rsplit(" ", 2)
            if len(parts) != 3:
                raise Denied("Unpinned proposal endpoint")
            endpoints[key] = vcs._check_endpoint(dict(zip(("repo", "ref", "sha"), parts)))
    if any(set(endpoint) != {"repo", "ref", "sha"} for endpoint in endpoints.values()):
        raise Denied("Proposal wait requires pinned head and base endpoints")
    if (vcs._short_branch_name(endpoints["head"]["ref"])
            != vcs._short_branch_name(receipt["branch"])
            or endpoints["head"]["sha"] != receipt["published_sha"]):
        raise Denied("Proposal head differs from the publication receipt")
    return {**value, **endpoints}


def _items(store):
    for item in store.current_items(statuses=("blocked",)):
        if (item.get("wait") or {}).get("kind") == KIND:
            yield item


def _record_id(item):
    return f"proposal-wait:{item['id']}:{item['rev']}"


def _validate(subject, payload):
    if (set(subject) != {"work_item", "rev", "holder"}
            or type(subject["rev"]) is not int or not 1 <= subject["rev"] <= MAX_REVISIONS
            or set(payload) != {"kind", "trust", "work_item", "rev", "holder", "wait", "state"}
            or payload["kind"] != KIND or payload["state"] != "merged"
            or payload["trust"] != "external-untrusted"
            or any(payload[key] != subject[key] for key in subject)):
        raise Denied("Invalid proposal merge event")
    identifier(subject["work_item"])
    identifier(subject["holder"])
    wait = payload["wait"]
    if (not isinstance(wait, dict)
            or set(wait) - {"smoke_refs"} != FIELDS | {"head", "base"}
            or wait.get("kind") != KIND
            or not isinstance(wait.get("publication_id"), str)
            or not DIGEST.fullmatch(wait["publication_id"])):
        raise Denied("Invalid proposal merge binding")
    _proposal_id(wait["proposal_id"])
    validate_refs(wait.get("smoke_refs", []))
    for key in ("head", "base"):
        if set(vcs._check_endpoint(wait[key])) != {"repo", "ref", "sha"}:
            raise Denied("Unpinned proposal merge endpoint")


def shared_store(store):
    return SharedRecords(store.root, {"proposal.wait": KindPolicy(
        frozenset({"host.vcs"}), _validate, maximum=8192)})


def _envelope(item):
    subject = {"work_item": item["id"], "rev": item["rev"], "holder": item["holder"]}
    return {"schema": 1, "id": _record_id(item), "kind": "proposal.wait",
            "created_at": item["updated_at"], "writer": "host.vcs", "subject": subject,
            "payload": {**subject, "kind": KIND, "trust": "external-untrusted",
                        "wait": item["wait"], "state": "merged"}}


def observe(project, rows):
    """Host poll only: write immutable merge evidence for complete enumerations.

    Serialized with registration and delivery under the work-item record lock.
    One event per matching blocked revision; exact replay is a no-op and no
    adapter call, raw text or separate evidence directory is involved.
    """
    if not isinstance(rows, list) or len(rows) > vcs.MAX_PROPOSALS:
        raise Denied("Invalid complete proposal enumeration")
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise Denied("Duplicate VCS proposal id")
    store = project.work_items
    with lock(store.root / "locks" / "records.lock"):
        shared = shared_store(store)
        for item in _items(store):
            wait = item["wait"]
            row = by_id.get(wait["proposal_id"])
            if (row is not None and row["state"] == "merged"
                    and all(row[key] == wait[key] for key in ("head", "base"))):
                normalize(store, wait, stored=True)
                shared.put(_envelope(item), writer="host.vcs")


def evidence(store, item):
    """Read the complete host-observed merge route, before receipt filtering.

    Only the complete-enumeration poll writes this envelope. Callers hold the
    item lock; neither proposal facts nor an acknowledgement proves a merge.
    """
    shared = shared_store(store)
    record_id = _record_id(item)
    # Distinguish absence from unsafe/corrupt evidence; never repair it.
    if shared._read(shared.record_name(record_id), RECORD_BYTES) is None:
        if shared._read(shared._receipt_name(item["holder"], record_id), RECEIPT_BYTES) is not None:
            raise Denied("Proposal receipt has no record")
        return None
    envelope, computed = shared.get(record_id)
    if envelope != _envelope(item):
        raise Denied("Proposal evidence binding changed")
    return envelope, computed


def _event(role, envelope, computed):
    return {**envelope["payload"], "record_id": envelope["id"],
            "record_digest": computed,
            "ack_token": _token(role, envelope["id"], computed)}


def _page_bytes(events) -> int:
    return len(json.dumps({"wait_events": events}, ensure_ascii=True).encode("utf-8"))


def _candidates(store, role, *, limit):
    shared = shared_store(store)
    selected = []
    for item in _items(store):
        if item["holder"] != role:
            continue
        observed = evidence(store, item)
        if observed is None:
            continue
        envelope, computed = observed
        if shared.received(role, envelope["id"], computed):
            continue
        selected.append(((_timestamp_order(item["updated_at"]), item["id"]), envelope))
        selected = heapq.nsmallest(limit, selected, key=lambda pair: pair[0])
    if _page_bytes([]) > MAX_FRAME:
        raise LimitExceeded("Proposal wait page metadata exceeds byte bound")
    return shared, selected


def _events(shared, role, selected, *, persist):
    events, manifest = [], ()
    for (_, _), envelope in selected:
        computed = digest(canonical(envelope))
        event = _event(role, envelope, computed)
        if _page_bytes(events + [event]) > MAX_FRAME:
            if not events:
                raise LimitExceeded("Proposal wait event exceeds the generic prompt frame bound")
            break
        if persist:
            shared.put(envelope, writer="host.vcs")
            manifest += shared.freeze(role, [envelope["id"]])
        events.append(event)
    return events, manifest


def delivery(store, role, *, limit=10):
    """Freeze only current holder deliveries; no model-supplied manifest."""
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid proposal wait delivery limit")
    with lock(store.root / "locks" / "records.lock"):
        shared, selected = _candidates(store, role, limit=limit)
        return _events(shared, role, selected, persist=True)


def pending(store, role, *, limit=10):
    return delivery(store, role, limit=limit)[0]


def readiness_init(store, role, *, limit=10):
    """Start a read-only accumulator; the caller holds ``records.lock``."""
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid proposal wait delivery limit")
    return {"role": role, "limit": limit, "shared": shared_store(store), "selected": []}


def readiness_feed(state, store, item):
    """Feed one fully reconstructed work item to the proposal accumulator."""
    if (item["status"] != "blocked" or (item.get("wait") or {}).get("kind") != KIND
            or item["holder"] != state["role"]):
        return
    try:
        observed = evidence(store, item)
        if observed is None:
            return
        envelope, computed = observed
        if shared_store(store).received(state["role"], envelope["id"], computed):
            return
        key = ((_timestamp_order(item["updated_at"]), item["id"]), envelope)
    except (Denied, LimitExceeded, OSError, ValueError):
        return  # one broken waiter cannot hide another item's evidence
    state["selected"].append(key)
    state["selected"] = heapq.nsmallest(state["limit"], state["selected"],
                                        key=lambda pair: pair[0])


def readiness_events(state):
    """Finalize without writes: no read-only probe may create evidence."""
    return _events(state["shared"], state["role"], state["selected"], persist=False)[0]


def _selected(context, tokens):
    store = context.project.work_items
    shared = shared_store(store)
    allowed = shared._manifest(context.role.name, context.proposal_wait_manifest)
    if len(set(tokens)) != len(tokens) or any(token not in allowed for token in tokens):
        raise Denied("Proposal token is not in this unit's manifest")
    entries = [allowed[token] for token in tokens]
    for entry in entries:
        envelope, _ = shared.get(entry.record_id, expected_digest=entry.record_digest)
        item = store.current(envelope["subject"]["work_item"])
        if (item["status"] != "blocked" or item["holder"] != context.role.name
                or envelope != _envelope(item)):
            raise Denied("Proposal waiter revision or holder changed")
        shared.received(context.role.name, entry.record_id, entry.record_digest)
    return shared, entries


def validate_selection(context, tokens):
    with lock(context.project.root / "locks" / "records.lock"):
        _selected(context, tokens)


def complete(context, finished):
    """Apply explicitly selected current proposal-wait receipts after success."""
    if (not context.proposal_wait_manifest or finished.get("wait_for_check")
            or finished.get("outcome") == "bounded" or context.bound_hit):
        return
    tokens = set(finished.get("ack_events", []))
    selected = [entry.ack_token for entry in context.proposal_wait_manifest
                if entry.ack_token in tokens]
    if not selected:
        return
    with lock(context.project.root / "locks" / "records.lock"):
        shared, entries = _selected(context, selected)
        shared.acknowledge(context.role.name, [asdict(entry) for entry in entries],
                           manifest=context.proposal_wait_manifest)
