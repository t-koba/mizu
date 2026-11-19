"""Queue-backed stage-2 intermediate for non-shared insight decisions.

This is userland adapter code, outside ``src/mizu/kernel``. The kernel only
provides authenticated append and bounded reads; queue names, schemas and
their meanings stay in ``examples/development/queues.toml`` and the role
policies. The workflow vocabulary here is the stage-2 intermediate that exists
until stage 2 is deleted.

A host-only authority marker (see :mod:`mizu.decision_delivery`) selects this
adapter for non-shared decision reads, receipts and the single live producer.
It rebuilds the exact ``decision_delivery.pending`` page from the declared ``proposals``,
``decisions`` and ``receipts`` queues, appends settled receipts only to the
declared host-writable ``receipts`` queue, and appends each post-activation
proposal/decision once through :class:`DecisionQueueWriter`. Ordinary
``Insights.submit``/``Insights.decide`` call that writer under the same marker
that switches the readers, so the live envelope has one authoritative path.
The shared-authority branch is unaffected.

Trust and bounds: the role comes from the host's trusted configuration object.
The only actor presented to a queue for reads is ``role:<role.name>``; receipt
appends use the granted ``host`` principal. No argument can supply a principal.
Reads start at cursor zero, use the kernel page bound and the stage-2
page-count bound, and never move a durable cursor. A far-future ``now`` makes
delayed revisits due; their messages are then skipped, because the ordinary
message is the one current decision. Malformed or ambiguous converted metadata
refuses the read instead of inventing a value.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping

from .config import MAX_DECISION_RECEIPT_DIAGNOSTICS, role_decision_events_limit
from .errors import Denied, LimitExceeded
from .fs import DIGEST, MAX_FRAME, canonical, digest, identifier
from .kernel.queues import PAGE_MAX, allows

PROPOSALS = "proposals"
DECISIONS = "decisions"
RECEIPTS = "receipts"
QUEUES = (PROPOSALS, DECISIONS, RECEIPTS)
#: The complete example conversion gate, including the record families still
#: owned by other stage-2 slices until their own switches.
CONVERSION_QUEUES = (PROPOSALS, DECISIONS, "questions", "answers", "notices", RECEIPTS)
#: Later than any four-digit-year revisit, and aware so delivery is total.
NOW = "9999-12-31T23:59:59+00:00"
PAGE = PAGE_MAX
#: The shared stage-2 page-count bound; a larger queue refuses the read.
MAX_PAGES = 64
#: The legacy decision-event ceiling, equal to the existing diagnostic ceiling.
MAX_DECISIONS = MAX_DECISION_RECEIPT_DIAGNOSTICS
#: Captured at import so the prompt-frame bound can be varied independently.
DECISION_PAYLOAD_BYTES = MAX_FRAME
INSIGHT_PENDING = "insight.pending"
RECEIPT_PENDING = "pending"
DELIVERED_ACTIONS = frozenset({"accept", "modify", "defer", "reject"})
ID = re.compile(r"[0-9a-f]{32}\Z")
#: Fixed-width hex enumeration order; lexical order matches legacy diagnostic order.
ORDER_RE = re.compile(r"[0-9a-f]{8}\Z")
MISSING = object()
PROPOSAL_FIELDS = ("legacy_insight", "legacy_rev", "legacy_source", "legacy_addressee",
                   "legacy_evidence", "legacy_created_at", "legacy_kind", "legacy_urgency")


class AdapterError(Denied):
    """Converted queue metadata cannot reproduce a legacy fact."""


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AdapterError(f"{label} is not a 32-hex id")
    return value


def _field(project, queue: str, name: str, body: Mapping, label: str):
    declaration = project.config.queues.get(queue)
    if declaration is None or name not in declaration.fields:
        raise AdapterError(f"{queue} declaration lacks the field {name}")
    field = declaration.fields[name]
    if name not in body:
        raise AdapterError(f"{label} lacks {name}")
    value = body[name]
    try:
        field.validate(value)
    except Denied as exc:
        raise AdapterError(f"{label} has an invalid {name}: {exc}") from exc
    return value


def _string(project, queue: str, name: str, body: Mapping, label: str) -> str:
    value = _field(project, queue, name, body, label)
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{label} has an empty {name}")
    return value


def _instant(project, queue: str, name: str, body: Mapping, label: str) -> str:
    value = _string(project, queue, name, body, label)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise AdapterError(f"{label} has an unparseable {name}") from exc
    if parsed.tzinfo is None:
        raise AdapterError(f"{label} has a naive {name}")
    return value


def _canonical_json(raw: str, label: str):
    """Decode a value the conversion stored as canonical JSON."""
    try:
        value = json.loads(raw)
        if canonical(value).decode("utf-8") != raw:
            raise ValueError("not canonical")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError(f"{label} is not canonical JSON") from exc
    return value


def _body(message, queue: str) -> Mapping:
    if not isinstance(message, Mapping):
        raise AdapterError(f"Malformed {queue} message")
    _identity(message.get("id"), f"{queue} message id")
    body = message.get("body")
    if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
        raise AdapterError(f"Malformed body in {queue} message {message.get('id')}")
    refs = message.get("refs")
    if not isinstance(refs, (list, tuple)) or any(not isinstance(ref, str) for ref in refs):
        raise AdapterError(f"Malformed refs in {queue} message {message.get('id')}")
    return body


def _read_queue(store, name: str, actor: str) -> list[dict]:
    """Read the due prefix from sequence one without moving any cursor."""
    cap = MAX_PAGES * PAGE
    messages: list[dict] = []
    cursor = 0
    pages = 0
    while pages < MAX_PAGES:
        pages += 1
        page = store.read(name, actor, cursor=cursor, limit=PAGE, now=NOW)
        head = page["head"]
        if type(head) is not int or head < 0:
            raise AdapterError(f"{name} reported a malformed head")
        if head > cap:
            raise AdapterError(f"{name} exceeds the {cap}-message page bound")
        rows = page["messages"]
        if not isinstance(rows, list):
            raise AdapterError(f"{name} returned a malformed page")
        messages.extend(rows)
        if page["delayed"] or not page["more"]:
            return messages
        following = page["next"]
        if type(following) is not int or following <= cursor:
            raise AdapterError(f"{name} pagination did not advance")
        cursor = following
    raise AdapterError(f"{name} exceeded {MAX_PAGES} pages")


class _ReceiptIndex:
    """Token-keyed view of one role's converted legacy receipts."""

    def __init__(self, project, role_name: str, messages: list[dict]):
        self.settled: set[str] = set()
        self.corrupt: dict[str, str] = {}
        self.actions: dict[str, str] = {}
        self.orders: dict[str, str] = {}
        for message in messages:
            body = _body(message, RECEIPTS)
            if message.get("deliver_at") is not None or message["refs"]:
                raise AdapterError("A converted receipt cannot be delayed or reference a message")
            token = _field(project, RECEIPTS, "ack_token", body, "Receipt")
            receipt_role = _string(project, RECEIPTS, "legacy_role", body, "Receipt")
            action = _field(project, RECEIPTS, "legacy_action", body, "Receipt")
            # Converted corrupt receipts carry the fixed-width enumeration
            # order; a newly appended settled receipt needs none because it
            # suppresses a candidate instead of producing a diagnostic.
            order = None
            if "legacy_order" in body:
                order = _field(project, RECEIPTS, "legacy_order", body, "Receipt")
                if not isinstance(order, str) or not ORDER_RE.fullmatch(order):
                    raise AdapterError("Converted receipt has an invalid legacy_order")
            reason_present = "legacy_reason" in body
            reason = None
            if reason_present:
                reason = _field(project, RECEIPTS, "legacy_reason", body, "Receipt")
                if reason is None:
                    raise AdapterError("Converted receipt has a null legacy_reason")
                if order is None:
                    raise AdapterError("Corrupt receipt lacks legacy_order")
            if receipt_role != role_name:
                continue
            if token in self.settled or token in self.corrupt:
                raise AdapterError(f"Duplicate converted receipt for {token}")
            if not reason_present:
                self.settled.add(token)
            else:
                if action not in DELIVERED_ACTIONS | {RECEIPT_PENDING}:
                    raise AdapterError("Converted receipt has an undeclared action")
                self.corrupt[token] = reason
                self.actions[token] = action
                self.orders[token] = order
            if token in self.settled and order is not None:
                # Converted settled receipts carry order too; retain it for
                # fail-closed coverage without affecting suppression.
                self.orders[token] = order

    def verdict(self, token: str, action: str) -> str | None:
        """Return None when deliverable, "valid" when settled, or a reason."""
        if token in self.settled:
            return "valid"
        reason = self.corrupt.get(token)
        if reason is not None and self.actions[token] != action:
            raise AdapterError("Converted receipt action does not bind its candidate")
        return reason

    def order(self, token: str) -> str:
        """Return the converted legacy enumeration order for one corrupt token."""
        try:
            return self.orders[token]
        except KeyError as exc:
            raise AdapterError("Converted receipt lacks legacy_order") from exc


def _proposal(project, message) -> dict:
    queue = PROPOSALS
    body = _body(message, queue)
    if message.get("deliver_at") is not None:
        raise AdapterError("A proposal cannot be a delayed message")
    if message["refs"]:
        raise AdapterError("A proposal cannot reference another message")
    required = ("title", "body", "legacy_insight", "legacy_rev", "legacy_source",
                "legacy_addressee", "legacy_evidence")
    fields = {name: _string(project, queue, name, body, "Proposal") for name in required}
    fields["legacy_created_at"] = _instant(project, queue, "legacy_created_at", body, "Proposal")
    for name in ("legacy_kind", "legacy_urgency"):
        if name in body:
            fields[name] = _string(project, queue, name, body, "Proposal")
    fields["legacy_insight"] = _identity(fields["legacy_insight"], "Legacy insight id")
    try:
        rev = int(fields["legacy_rev"])
    except ValueError as exc:
        raise AdapterError("Proposal legacy revision is not an integer") from exc
    if rev < 1:
        raise AdapterError("Proposal legacy revision is not positive")
    fields["rev"] = rev
    fields["created_at"] = fields["legacy_created_at"]
    evidence = _canonical_json(fields["legacy_evidence"], "Proposal evidence")
    if not isinstance(evidence, list) or any(not isinstance(entry, dict) for entry in evidence):
        raise AdapterError("Proposal evidence is not a list of evidence entries")
    fields["evidence"] = evidence
    for source, target in (("legacy_kind", "kind"), ("legacy_urgency", "urgency")):
        if source in fields:
            fields[target] = fields[source]
    for key in ("legacy_source", "legacy_addressee"):
        identifier(fields[key])
    return {"id": message["id"], **fields}


def _decision(project, message, proposals: dict) -> dict:
    queue = DECISIONS
    body = _body(message, queue)
    refs = message["refs"]
    if len(refs) != 1 or refs[0] not in proposals:
        raise AdapterError(f"Decision {message['id']} does not bind one converted proposal")
    proposal = proposals[refs[0]]
    fields = {name: _string(project, queue, name, body, "Decision")
              for name in ("action", "reason")}
    for name in PROPOSAL_FIELDS:
        if body.get(name, MISSING) != proposal.get(name, MISSING):
            raise AdapterError(f"Decision {message['id']} metadata does not bind its proposal")
    event = _identity(_string(project, queue, "legacy_event", body, "Decision"),
                      "Legacy decision event")
    decided_at = _instant(project, queue, "legacy_decided_at", body, "Decision")
    record = _canonical_json(_string(project, queue, "legacy_record", body, "Decision"),
                             "Legacy decision record")
    if not isinstance(record, dict):
        raise AdapterError("Legacy decision record is not an object")
    if (record.get("id") != proposal["legacy_insight"] or record.get("rev") != proposal["rev"]
            or record.get("action") != fields["action"]
            or record.get("created_at") != decided_at):
        raise AdapterError("Legacy decision record does not bind its proposal")
    if "legacy_decision_key" in body:
        key = _identity(_string(project, queue, "legacy_decision_key", body, "Decision"),
                        "Legacy decision key")
        if record.get("decision_key") != key:
            raise AdapterError("Legacy decision key does not bind its record")
    elif "decision_key" in record:
        raise AdapterError("Legacy decision key is missing from the converted message")
    if "legacy_run" in body:
        run = _field(project, queue, "legacy_run", body, "Decision")
        if run is not None and record.get("run") != run:
            raise AdapterError("Legacy decision run does not bind its record")
    elif "run" in record:
        raise AdapterError("Legacy decision run is missing from the converted message")
    revisit = body.get("revisit", "")
    if revisit is not None:
        revisit = _string(project, queue, "revisit", body, "Decision") if "revisit" in body else ""
    if record.get("revisit", "") != revisit:
        raise AdapterError("Legacy decision revisit does not bind its record")
    return {"event": event, "action": fields["action"], "reason": fields["reason"],
            "revisit": revisit, "decided_at": decided_at, "record": record,
            "proposal": proposal, "deliver_at": message.get("deliver_at")}


def _decision_event(role_name: str, decision: dict, sources: str) -> dict | None:
    proposal = decision["proposal"]
    if sources == "own" and proposal["legacy_source"] != role_name:
        return None
    payload = {"event": decision["event"], "insight": proposal["legacy_insight"],
               "rev": proposal["rev"], "title": proposal["title"], "body": proposal["body"],
               "action": decision["action"], "reason": decision["reason"],
               "revisit": decision["revisit"], "decided_at": decision["decided_at"],
               "source": proposal["legacy_source"], "evidence": proposal["evidence"]}
    if len(canonical(payload)) > DECISION_PAYLOAD_BYTES:
        raise AdapterError("Decision projection payload exceeds byte bound")
    token = digest(canonical({"role": role_name, "decision": decision["record"],
                              "payload": payload}))
    return dict(payload, ack_token=token)


def _pending_event(role_name: str, proposal: dict, configured_roles) -> dict | None:
    """Rebuild one exact ``insight.pending`` envelope payload."""
    addressee = proposal["legacy_addressee"]
    source = proposal["legacy_source"]
    target = source if addressee == "writer" else addressee
    if target not in configured_roles or target != role_name:
        return None
    payload = {"title": proposal["title"], "body": proposal["body"], "source": source,
               "created_at": proposal["created_at"], "insight": proposal["legacy_insight"],
               "rev": proposal["rev"], "kind": INSIGHT_PENDING, "target": target,
               "addressee": addressee}
    if "kind" in proposal:
        payload["insight_kind"] = proposal["kind"]
    if "urgency" in proposal:
        payload["urgency"] = proposal["urgency"]
    for key in ("insight", "source", "target", "addressee"):
        identifier(payload[key])
    payload["event"] = digest(canonical(payload))[:32]
    identifier(payload["event"])
    token = digest(canonical({"role": role_name, "pending_insight": payload}))
    return dict(payload, ack_token=token)


def _stamp(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event.get("decided_at", event.get("created_at")))


def _roles_and_sources(project, role, sources):
    """Validate the trusted role/source pair; never accept a principal."""
    if not isinstance(role.name, str) or role.name == "operator":
        raise Denied("Operator insights belong to the human host inbox")
    name = identifier(role.name)
    if sources is None:
        sources = getattr(role, "decision_sources", "own") or "own"
    if sources not in ("own", "any"):
        raise Denied("Decision event sources must be own or any")
    return name, sources


def _candidates(project, name, wanted, sources, include_insights):
    """Rebuild all queue candidates for one role, before receipt suppression.

    Both lists carry the exact delivered envelope, including its ack token.
    Settled and corrupt receipts are deliberately not consulted here: an
    acknowledgement revalidates against the immutable queue facts, including
    events it has already settled.
    """
    actor = f"role:{name}"
    store = project.kernel_queues
    reads = {queue: _read_queue(store, queue, actor) for queue in QUEUES}
    proposals: dict[str, dict] = {}
    for message in reads[PROPOSALS]:
        item = _proposal(project, message)
        if item["id"] in proposals:
            raise AdapterError(f"Duplicate converted proposal {item['id']}")
        proposals[item["id"]] = item
    decisions = [_decision(project, message, proposals) for message in reads[DECISIONS]]
    decided = {decision["proposal"]["id"] for decision in decisions}
    delivered = []
    for item in decisions:
        if item["deliver_at"] is not None or item["action"] not in wanted:
            continue
        event = _decision_event(name, item, sources)
        if event is not None:
            delivered.append(event)
    insights = []
    if include_insights:
        configured_roles = set(project.roles) & project.config.roles.keys()
        for item in proposals.values():
            if item["id"] in decided:
                continue
            event = _pending_event(name, item, configured_roles)
            if event is not None:
                insights.append(event)
    return reads[RECEIPTS], delivered, insights


def pending(project, role, *, actions=None, limit=None, include_insights=True,
            sources=None) -> dict:
    """Reconstruct the legacy decision page for one trusted role from queues."""
    name, sources = _roles_and_sources(project, role, sources)
    wanted_actions = role.decision_events if actions is None else actions
    if limit is None:
        limit = role_decision_events_limit(project.config, role)
    if type(limit) is not int or not 1 <= limit <= MAX_DECISIONS:
        raise Denied(f"Decision event limit must be an integer in [1, {MAX_DECISIONS}]")
    wanted = {action for action in (wanted_actions or ()) if isinstance(action, str)}
    wanted &= DELIVERED_ACTIONS
    receipt_messages, candidates, pending_candidates = _candidates(
        project, name, wanted, sources, include_insights)
    receipts = _ReceiptIndex(project, name, receipt_messages)
    cap = project.insights.decision_receipt_diagnostics
    decision_diagnostics: list[tuple[str, dict]] = []

    deliverable = []
    for event in candidates:
        verdict = receipts.verdict(event["ack_token"], event["action"])
        if verdict == "valid":
            continue
        if verdict is not None:
            decision_diagnostics.append((receipts.order(event["ack_token"]),
                                         {"event": event["event"],
                                          "ack_token": event["ack_token"],
                                          "action": event["action"],
                                          "reason": verdict}))
            continue
        deliverable.append(event)
    events = sorted(deliverable, key=lambda event: (_stamp(event), event["event"]))[:limit]
    decision_ordered = [diagnostic for _, diagnostic in
                        sorted(decision_diagnostics, key=lambda entry: entry[0])[:cap]]
    diagnostics = list(decision_ordered)

    if include_insights:
        pending_diagnostics: list[tuple[str, dict]] = []
        deliverable_insights = []
        for event in pending_candidates:
            verdict = receipts.verdict(event["ack_token"], RECEIPT_PENDING)
            if verdict == "valid":
                continue
            if verdict is not None:
                pending_diagnostics.append((receipts.order(event["ack_token"]),
                                            {"event": event["event"],
                                             "ack_token": event["ack_token"],
                                             "action": RECEIPT_PENDING,
                                             "reason": verdict}))
                continue
            deliverable_insights.append(event)

        insights = sorted(deliverable_insights,
                          key=lambda event: (_stamp(event), event["event"]))[:limit]
        pending_ordered = [diagnostic for _, diagnostic in
                           sorted(pending_diagnostics, key=lambda entry: entry[0])[:cap]]
        diagnostics = (decision_ordered + pending_ordered)[:cap]
        decision_capped = bool(wanted) and len(events) == limit
        insight_capped = len(insights) == limit
        source_capped = decision_capped or insight_capped
        combined = [*events, *insights]
        combined.sort(key=lambda event: (_stamp(event),
                                         event.get("kind") == INSIGHT_PENDING,
                                         event["event"]))
        if source_capped:
            pending_total = None
            count_truncated = True
        else:
            pending_total = len(combined)
            count_truncated = pending_total > limit
        limited = combined[:limit]
        delivered = list(limited)

        def legacy_summary(rows):
            if source_capped:
                return {"pending_count": None, "truncated": True,
                        "count_truncated": True,
                        "byte_truncated": len(rows) < len(limited)}
            return {"pending_count": pending_total, "truncated": len(rows) < pending_total,
                    "count_truncated": count_truncated,
                    "byte_truncated": len(rows) < min(pending_total, limit)}

        if any(event.get("kind") == INSIGHT_PENDING for event in delivered):
            def legacy_size(rows):
                return len(json.dumps({"decision_events": rows,
                                       "decision_page": legacy_summary(rows)},
                                      ensure_ascii=True).encode("utf-8"))

            if legacy_size([]) > MAX_FRAME:
                raise LimitExceeded("Legacy decision page metadata exceeds byte bound")
            while delivered and legacy_size(delivered) > MAX_FRAME:
                index = len(delivered) - 1
                while index >= 0:
                    if delivered[index].get("kind") == INSIGHT_PENDING:
                        del delivered[index]
                        break
                    index -= 1
                else:
                    break
            if len(delivered) < len(limited):
                page = {"events": list(delivered), **legacy_summary(delivered)}
                return dict(shared=False, events=delivered, diagnostics=diagnostics,
                            page=page, manifest=())
        events = delivered
    return dict(shared=False, events=events, diagnostics=diagnostics,
                page=None, manifest=())


def _receipt_id(role_name: str, token: str) -> str:
    """Stable queue message id so an exact retry cannot append a second row."""
    return digest(canonical({"queue": RECEIPTS, "role": role_name,
                             "ack_token": token}))[:32]


def validate(project, role, events) -> None:
    """Revalidate tokens against queue candidates for a trusted role or name."""
    if isinstance(role, str):
        name = identifier(role)
        role = project.config.roles.get(name)
    else:
        name = identifier(role.name) if getattr(role, "name", None) else None
    if name is None or name == "operator" or role is None:
        raise Denied("Invalid decision acknowledgement role")
    sources = getattr(role, "decision_sources", "own") or "own"
    wanted = {action for action in (role.decision_events or ()) if isinstance(action, str)}
    wanted &= DELIVERED_ACTIONS
    _, decisions, insights = _candidates(project, name, wanted, sources, True)
    known = {}
    for event in [*decisions, *insights]:
        token = event["ack_token"]
        if token in known and canonical(known[token]) != canonical(event):
            raise AdapterError("Conflicting queue candidates for one ack token")
        known[token] = event
    if not isinstance(events, list) or len(events) > MAX_DECISIONS:
        raise Denied("Invalid decision acknowledgement batch")
    for event in events:
        if (not isinstance(event, dict) or not isinstance(event.get("ack_token"), str)
                or not DIGEST.fullmatch(event["ack_token"])):
            raise Denied("Invalid decision acknowledgement")
        candidate = known.get(event["ack_token"])
        if candidate is None or canonical(event) != canonical(candidate):
            raise Denied("Decision acknowledgement binding changed")


def acknowledge(project, role, events) -> None:
    """Append settled receipts to the declared host-writable receipts queue.

    A settled receipt is schema-valid, token-bound to a currently reconstructible
    queue candidate, and idempotent by stable message id. No legacy receipt file
    is read or written and no other queue is appended to.
    """
    name, _ = _roles_and_sources(project, role, None)
    validate(project, role, events)
    receipt_messages, _, _ = _candidates(project, name, DELIVERED_ACTIONS, "any", True)
    receipts = _ReceiptIndex(project, name, receipt_messages)
    store = project.kernel_queues
    for event in events:
        token = event["ack_token"]
        verdict = receipts.verdict(token, event.get("action", RECEIPT_PENDING))
        if verdict == "valid":
            continue
        if verdict is not None:
            raise Denied("Invalid decision acknowledgement for a corrupt receipt token")
        action = (RECEIPT_PENDING if event.get("kind") == INSIGHT_PENDING
                  else event.get("action"))
        if action not in DELIVERED_ACTIONS | {RECEIPT_PENDING}:
            raise Denied("Invalid decision acknowledgement action")
        body = {"ack_token": token, "legacy_role": name, "legacy_action": action}
        store.append(RECEIPTS, body, "host", id=_receipt_id(name, token))


# -- Runtime producer ---------------------------------------------------------
#
# The activation marker switches every decision-delivery read and receipt to
# these queues. The same marker therefore has to switch the envelope producer:
# after activation an ordinary submit/decide is delivered only through the
# declared queue, never through a legacy delivery projection. The legacy
# inbox/decision-history writes in Insights remain storage for revisions, work
# items and waits; they are not a second delivery path. One producer appends
# each envelope, with stable ids so an exact retry cannot append a second row.


def message_id(kind: str, identity: str, rev: int | str) -> str:
    """The conversion-stable 32-hex id for one insight identity and revision.

    Shared with the one-time conversion so a message converted before
    activation and a message produced after it have the same identity.
    """
    import hashlib
    token = f"convert-legacy:v1:{kind}:{identity}:{rev}".encode("utf-8")
    return hashlib.sha256(token).hexdigest()[:32]


def _require_producer_queues(project) -> None:
    """A missing or malformed declaration refuses the write instead of falling back."""
    needed = {
        PROPOSALS: ("title", "body", *PROPOSAL_FIELDS),
        DECISIONS: ("action", "reason", "revisit", *PROPOSAL_FIELDS,
                    "legacy_event", "legacy_decided_at", "legacy_run", "legacy_record"),
    }
    for queue, names in needed.items():
        declaration = project.config.queues.get(queue)
        if declaration is None or any(name not in declaration.fields for name in names):
            raise AdapterError(f"{queue} declaration lacks decision producer metadata")
    proposal = project.config.queues[PROPOSALS]
    decision = project.config.queues[DECISIONS]
    if (not allows(proposal.write, "operator") or not allows(proposal.read, "role:writer")
            or not allows(decision.write, "role:writer")):
        raise AdapterError("Declarations do not grant the decision producer its write and binding reads")


def _put(project, queue: str, name: str, value, label: str, *, allow_none: bool = False):
    field = project.config.queues[queue].fields[name]
    if value is None and allow_none:
        return field.validate(None)
    if not isinstance(value, str) or (not value.strip() and not allow_none):
        raise AdapterError(f"{label} has an empty {name}")
    try:
        field.validate(value)
    except Denied as exc:
        raise AdapterError(f"{label} has an invalid {name}: {exc}") from exc
    return value


def _proposal_body(project, record) -> dict:
    queue = PROPOSALS
    rev = record.get("rev")
    if type(rev) is not int or rev < 1:
        raise AdapterError("Proposal revision is not a positive integer")
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list) or any(not isinstance(entry, dict) for entry in evidence):
        raise AdapterError("Proposal evidence is not a list of evidence entries")
    body = {
        "title": _put(project, queue, "title", record.get("title"), "Proposal"),
        "body": _put(project, queue, "body", record.get("body"), "Proposal"),
        "legacy_insight": _put(project, queue, "legacy_insight", record.get("id"), "Proposal"),
        "legacy_rev": _put(project, queue, "legacy_rev", str(rev), "Proposal"),
        "legacy_source": _put(project, queue, "legacy_source", record.get("source"), "Proposal"),
        "legacy_addressee": _put(project, queue, "legacy_addressee",
                                 record.get("addressee"), "Proposal"),
        "legacy_evidence": _put(project, queue, "legacy_evidence",
                                canonical(evidence).decode("utf-8"), "Proposal"),
        "legacy_created_at": _put(project, queue, "legacy_created_at",
                                  record.get("created_at"), "Proposal"),
    }
    for key, name in (("kind", "legacy_kind"), ("urgency", "legacy_urgency")):
        if key in record:
            body[name] = _put(project, queue, name, record[key], "Proposal")
    return body


def _revisit_instant(value) -> str | None:
    """The exact aware instant conversion would delay, or None for a phrase."""
    from .kernel_shadow import instant_value
    return instant_value(value) if isinstance(value, str) else None


class DecisionQueueWriter:
    """The single authoritative decision-envelope writer once queues are active.

    Wired by ``Project`` into ``Insights``; ordinary submit/decide call it under
    the same project lock as the storage write, so an activated project switches
    producer and reader with one marker. Activation refuses to publish that
    marker unless this writer is present and its declarations validate.
    """

    def __init__(self, project):
        self.project = project
        self._active = False

    def active(self) -> bool:
        # The marker is create-only and one-way. Cache only the active state:
        # activation can happen later in this process, so an inactive answer
        # is rechecked on the next write. Avoid re-reading the retained report
        # (up to the history-scale reconciliation bound) on every submission.
        if self._active:
            return True
        from .decision_delivery import authority_kind
        self._active = authority_kind(self.project.root) == "queues"
        return self._active

    def require(self) -> None:
        _require_producer_queues(self.project)

    def submit(self, record) -> None:
        """Append the declared proposal for a newly stored insight record."""
        self.require()
        body = _proposal_body(self.project, record)
        identity = record["id"]
        if len(canonical(body)) > MAX_FRAME:  # transport frame, no new knob
            raise AdapterError("Proposal envelope exceeds the transport frame")
        self.project.kernel_queues.append(
            PROPOSALS, body, "operator", id=message_id(PROPOSALS, identity, record["rev"]))

    def decide(self, current, event, event_id: str) -> None:
        """Append the declared decision (and delayed revisit) for one event."""
        self.require()
        action = event.get("action")
        if action not in DELIVERED_ACTIONS:
            raise AdapterError("Decision producer requires a delivered action")
        proposal_id = message_id(PROPOSALS, current["id"], current["rev"])
        # A decision binds the exact proposal it answers. Read the stored
        # message rather than re-deriving metadata: evidence may have been
        # attached after the proposal was written, and conversion binds both
        # rows to the same values.
        store = self.project.kernel_queues
        try:
            proposal = store.message(proposal_id, "role:writer")
        except Denied as exc:
            raise AdapterError("Decision has no bound queue proposal") from exc
        if proposal["queue"] != PROPOSALS:
            raise AdapterError("Decision proposal id names another queue")
        source = proposal["body"]
        queue = DECISIONS
        body = {
            "action": _put(self.project, queue, "action", action, "Decision"),
            "reason": _put(self.project, queue, "reason", event.get("reason"), "Decision"),
            **{name: source[name] for name in PROPOSAL_FIELDS if name in source},
            "legacy_event": _put(self.project, queue, "legacy_event", event_id, "Decision"),
            "legacy_decided_at": _put(self.project, queue, "legacy_decided_at",
                                      event.get("created_at"), "Decision"),
            "legacy_record": _put(self.project, queue, "legacy_record",
                                  canonical(event).decode("utf-8"), "Decision"),
        }
        if "decision_key" in event:
            body["legacy_decision_key"] = _put(
                self.project, queue, "legacy_decision_key", event["decision_key"], "Decision")
        if "run" in event:
            body["legacy_run"] = _put(self.project, queue, "legacy_run",
                                      event["run"], "Decision", allow_none=True)
        if action == "defer":
            body["revisit"] = _put(self.project, queue, "revisit",
                                   event.get("revisit"), "Decision")
        if len(canonical(body)) > MAX_FRAME:
            raise AdapterError("Decision envelope exceeds the transport frame")
        identity = current["id"]
        rev = current["rev"]
        store.append(DECISIONS, body, "role:writer", refs=[proposal_id],
                     id=message_id(DECISIONS, identity, rev))
        if action == "defer":
            instant = _revisit_instant(event.get("revisit"))
            if instant is not None:
                store.append(DECISIONS, body, "role:writer", refs=[proposal_id],
                             deliver_at=instant, id=message_id("revisit", identity, rev))
