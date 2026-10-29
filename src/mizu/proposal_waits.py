"""Exact published-proposal merge waits, using proposal insights and shared receipts.

Only complete, single-enumeration polls (at most MAX_PROPOSALS rows) can bind or
release a waiter. Registration also requires exact membership in the latest
durable complete-enumeration inventory; stored waits never consult that cache.
Paginated observations remain useful proposal facts, but never prove completeness
here. A positive merge is retained per blocked revision;
missing or changed later observations do not manufacture or erase that fact.
No observation or acknowledgement transitions a work item.
"""
from __future__ import annotations

import heapq
from dataclasses import asdict

from .errors import Denied
from .fs import DIGEST, canonical, digest, identifier, lock, read_json
from .ci_observation import POLL_STATE_FILE
from .publication import Publication
from .smoke_refs import validate_refs
from .work_items import _timestamp_order
from .shared_records import KindPolicy, SharedRecords
from . import vcs

KIND = "proposal_merged"
FIELDS = {"kind", "publication_id", "proposal_id"}


def normalize(root, value, *, insights=None, stored=False):
    fields = FIELDS | {"head", "base"} if stored else FIELDS
    if not isinstance(value, dict) or set(value) != fields or value.get("kind") != KIND:
        raise Denied("Invalid proposal_merged wait fields")
    observation_id = vcs.proposal_insight_id(value["proposal_id"])
    receipt = Publication.read_receipt(root, value["publication_id"])
    if stored:
        endpoints = {k: vcs._check_endpoint(value[k]) for k in ("head", "base")}
    else:
        observation = insights.read(observation_id)
        if observation.get("source") != "vcs" or observation.get("id") != observation_id:
            raise Denied("Proposal wait requires a VCS proposal observation")
        body = observation.get("body")
        if not isinstance(body, str):
            raise Denied("Proposal observation body is unavailable")
        lines = body.splitlines()
        def fact(name):
            found = [line[len(name) + 2:] for line in lines if line.startswith(name + ": ")]
            if len(found) != 1:
                raise Denied("Missing or ambiguous proposal observation")
            return found[0]
        if (fact("id") != value["proposal_id"] or fact("state") not in vcs.PROPOSAL_STATES
                or fact("enumeration") != "complete"):
            raise Denied("Proposal wait requires a complete proposal observation")
        endpoints = {}
        for key in ("head", "base"):
            parts = fact(key).rsplit(" ", 2)
            if len(parts) != 3:
                raise Denied("Unpinned proposal endpoint")
            endpoints[key] = vcs._check_endpoint(dict(zip(("repo", "ref", "sha"), parts)))
        # WorkItems registration holds insights.lock, shared with the poll's
        # inventory publication. A row's old completeness marker alone cannot
        # prove it belongs to the latest durable complete enumeration.
        try:
            inventory = read_json(root / POLL_STATE_FILE)["proposal_complete_facts"]
            if not isinstance(inventory, dict) or len(inventory) > vcs.MAX_PROPOSALS:
                raise Denied("Invalid complete proposal inventory")
            for key, computed in inventory.items():
                vcs.proposal_insight_id(key)
                if not isinstance(computed, str) or not DIGEST.fullmatch(computed):
                    raise Denied("Invalid complete proposal inventory")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise Denied("Complete proposal inventory is unavailable") from exc
        if inventory.get(value["proposal_id"]) != digest(canonical(
                {"title": observation.get("title"), "body": body})):
            raise Denied("Proposal observation differs from latest complete enumeration")
    if any(set(e) != {"repo", "ref", "sha"} for e in endpoints.values()):
        raise Denied("Proposal wait requires pinned head and base endpoints")
    if (vcs._short_branch_name(endpoints["head"]["ref"]) != vcs._short_branch_name(receipt["branch"])
            or endpoints["head"]["sha"] != receipt["published_sha"]):
        raise Denied("Proposal head differs from the publication receipt")
    return {**value, **endpoints}


def _items(store):
    # Stream the authoritative history, retaining no ledger-sized collection.
    for event_id, event in store._iter_item_events(recover=False):
        kind, seed = store._seed_for_event(event_id, event, readonly=True)
        if kind == "seed":
            item = store._get(seed["id"], seed_event=event,
                              history_revisions=store._history_revisions(seed["id"], recover=False))
            if item["status"] == "blocked" and (item.get("wait") or {}).get("kind") == KIND:
                yield item


def _record_id(item):
    return f"proposal-wait:{item['id']}:{item['rev']}"


def _validate(subject, payload):
    if (set(subject) != {"work_item", "rev", "holder"}
            or type(subject["rev"]) is not int or not 1 <= subject["rev"] <= 256
            or set(payload) != {"kind", "trust", "work_item", "rev", "holder", "wait", "state"}
            or payload["kind"] != KIND or payload["state"] != "merged"
            or payload["trust"] != "external-untrusted"
            or any(payload[k] != subject[k] for k in subject)):
        raise Denied("Invalid proposal merge event")
    identifier(subject["work_item"])
    identifier(subject["holder"])
    wait = payload["wait"]
    if (not isinstance(wait, dict) or set(wait) - {"smoke_refs"} != FIELDS | {"head", "base"}
            or wait.get("kind") != KIND or not isinstance(wait.get("publication_id"), str)
            or not DIGEST.fullmatch(wait["publication_id"])):
        raise Denied("Invalid proposal merge binding")
    vcs.proposal_insight_id(wait["proposal_id"])
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
    """Host poll only, after validating the entire complete enumeration.

    Serialized with registration/CAS. One immutable event per matching blocked
    revision, exact repeats are no-ops; interrupted batches safely replay. No
    adapter calls, raw text, or separate source receipt/evidence directories.
    """
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise Denied("Duplicate VCS proposal id")
    store = project.work_items
    with lock(store.root / "locks" / "insights.lock"):
        shared = shared_store(store)
        for item in _items(store):
            wait = item["wait"]
            row = by_id.get(wait["proposal_id"])
            if (row is not None and row["state"] == "merged"
                    and all(row[k] == wait[k] for k in ("head", "base"))):
                shared.put(_envelope(item), writer="host.vcs")


def delivery(store, role, *, limit=10):
    """Read-only bounded oldest-first delivery from current blocked revisions.

    Hold the same source lock as WorkItems transitions while validating the
    current holder/revision and freezing the manifest, so a concurrent reopen
    or reassignment cannot be serialized as a current wake.
    """
    identifier(role)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Denied("Invalid proposal wait delivery limit")
    with lock(store.root / "locks" / "insights.lock"):
        shared = shared_store(store)
        selected = []
        for item in _items(store):
            if item["holder"] != role:
                continue
            record_id = _record_id(item)
            # Distinguish absence from unsafe or corrupt evidence; never repair it.
            names = shared._record_names(record_id)
            if not any(shared._read(name, 8192) is not None for name in names):
                if shared._read(shared._receipt_name(role, record_id), 2048) is not None:
                    raise Denied("Proposal receipt has no record")
                continue
            envelope, computed = shared.get(record_id)
            if envelope != _envelope(item):
                raise Denied("Proposal evidence binding changed")
            if shared.received(role, record_id, computed):
                continue
            selected.append(((_timestamp_order(item["updated_at"]), item["id"]), envelope))
            selected = heapq.nsmallest(limit, selected, key=lambda pair: pair[0])
        manifest = shared.freeze(role, [e["id"] for _, e in selected])
        events = [{**e["payload"], "ack_token": entry.ack_token}
                  for (_, e), entry in zip(selected, manifest)]
        return events, manifest


def pending(store, role, *, limit=10):
    return delivery(store, role, limit=limit)[0]


def _selected(context, tokens):
    store = context.project.work_items
    shared = shared_store(store)
    allowed = shared._manifest(context.role.name, context.proposal_wait_manifest)
    if len(set(tokens)) != len(tokens) or any(token not in allowed for token in tokens):
        raise Denied("Proposal token is not in this unit's manifest")
    entries = [allowed[token] for token in tokens]
    for entry in entries:
        envelope, _ = shared.get(entry.record_id, expected_digest=entry.record_digest)
        subject = envelope["subject"]
        item = store._get(subject["work_item"], history_revisions=store._history_revisions(
            subject["work_item"], recover=False))
        if item["status"] != "blocked" or item["holder"] != context.role.name or envelope != _envelope(item):
            raise Denied("Proposal waiter revision or holder changed")
        shared.received(context.role.name, entry.record_id, entry.record_digest)
    return shared, entries


def validate_selection(context, tokens):
    with lock(context.project.root / "locks" / "insights.lock"):
        _selected(context, tokens)


def complete(context, finished):
    if (finished.get("wait_for_check") or finished.get("outcome") == "bounded"
            or context.bound_hit):
        return  # Same check-join completion boundary as other event sources.
    tokens = set(finished.get("ack_events", []))
    selected = [e.ack_token for e in context.proposal_wait_manifest if e.ack_token in tokens]
    with lock(context.project.root / "locks" / "insights.lock"):
        shared, entries = _selected(context, selected)
        shared.acknowledge(context.role.name, [asdict(e) for e in entries],
                           manifest=context.proposal_wait_manifest)
