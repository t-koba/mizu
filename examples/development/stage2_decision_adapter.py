#!/usr/bin/env python3
"""Read-only, queue-backed reconstruction of the legacy decision page.

This is example userland, not kernel code and not a runtime call-site switch.
It reads only the declared ``proposals``, ``decisions`` and ``receipts``
queues through ``project.kernel_queues`` and rebuilds the exact non-shared
``decision_delivery.pending`` dictionary (``shared``, ``events``,
``diagnostics``, ``page``, ``manifest``) from the metadata retained by
``convert_legacy.py``. Converted receipts suppress settled candidates and
yield the legacy per-token diagnostics; the adapter never opens a legacy
insight, history, receipt or shared record after conversion, and legacy page
calls belong in comparison tests.

Because the queue has no per-message filter and any configured role may be the
addressee of an insight, the example ``queues.toml`` grants every role read of
``proposals``; write and wake grants are unchanged.

Trust and bounds: the role comes from the host's trusted configuration object.
The only actor presented to the queue is ``role:<role.name>``; no argument can
supply a principal. Reads start at cursor zero, use the kernel page bound and
the shared stage-2 page-count bound, and never acknowledge or append. A far-
future ``now`` makes delayed revisits due; their messages are then skipped,
because the ordinary message is the one current decision. Malformed or
ambiguous converted metadata refuses the read instead of inventing a value.

The ``receipts`` messages carry only the declared token, role, inferred action,
enumeration order and optional reason; diagnostics take their action from the
candidate event itself, exactly as legacy does, and are ordered by the converted
legacy enumeration order (decision-history glob order then pending-envelope
order) so multi-corrupt pages match legacy bytes. The legacy diagnostics cap
is reused. The
remaining limit of this read slice is the shared-authority page, which has no
queue facts here; the runtime keeps its legacy authority until the one-commit
switch.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import stage2_view  # noqa: E402  (shared userland page bounds)
from mizu.config import role_decision_events_limit  # noqa: E402
from mizu.errors import Denied, LimitExceeded  # noqa: E402
from mizu.fs import MAX_FRAME, canonical, digest, identifier  # noqa: E402

PROPOSALS = "proposals"
DECISIONS = "decisions"
RECEIPTS = "receipts"
QUEUES = (PROPOSALS, DECISIONS, RECEIPTS)
#: Later than any four-digit-year revisit, and aware so delivery is total.
NOW = "9999-12-31T23:59:59+00:00"
PAGE = stage2_view.PAGE
MAX_PAGES = stage2_view.MAX_PAGES
#: The legacy decision-event bound, not a new runtime limit.
MAX_DECISIONS = 100
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
    for _ in range(MAX_PAGES):
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
            order = _field(project, RECEIPTS, "legacy_order", body, "Receipt")
            if not isinstance(order, str) or not ORDER_RE.fullmatch(order):
                raise AdapterError("Converted receipt has an invalid legacy_order")
            reason_present = "legacy_reason" in body
            reason = None
            if reason_present:
                reason = _field(project, RECEIPTS, "legacy_reason", body, "Receipt")
                if reason is None:
                    raise AdapterError("Converted receipt has a null legacy_reason")
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
            if token in self.settled:
                # Settled receipts also carry order for a complete conversion;
                # retain it for fail-closed coverage without affecting suppression.
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


def pending(project, role, *, actions=None, limit=None, include_insights=True,
            sources=None) -> dict:
    """Reconstruct the legacy decision page for one trusted role from queues."""
    if not isinstance(role.name, str) or role.name == "operator":
        raise Denied("Operator insights belong to the human host inbox")
    name = identifier(role.name)
    if sources is None:
        sources = getattr(role, "decision_sources", "own") or "own"
    if sources not in ("own", "any"):
        raise Denied("Decision event sources must be own or any")
    wanted_actions = role.decision_events if actions is None else actions
    if limit is None:
        limit = role_decision_events_limit(project.config, role)
    if type(limit) is not int or not 1 <= limit <= MAX_DECISIONS:
        raise Denied(f"Decision event limit must be an integer in [1, {MAX_DECISIONS}]")
    wanted = {action for action in (wanted_actions or ()) if isinstance(action, str)}
    wanted &= DELIVERED_ACTIONS
    actor = f"role:{name}"
    store = project.kernel_queues
    reads = {queue: _read_queue(store, queue, actor) for queue in QUEUES}
    receipts = _ReceiptIndex(project, name, reads[RECEIPTS])
    cap = project.insights.decision_receipt_diagnostics
    decision_diagnostics: list[tuple[str, dict]] = []

    proposals: dict[str, dict] = {}
    for message in reads[PROPOSALS]:
        item = _proposal(project, message)
        if item["id"] in proposals:
            raise AdapterError(f"Duplicate converted proposal {item['id']}")
        proposals[item["id"]] = item
    decisions = [_decision(project, message, proposals) for message in reads[DECISIONS]]
    decided = {decision["proposal"]["id"] for decision in decisions}

    def deliverable_decisions():
        for item in decisions:
            if item["deliver_at"] is not None or item["action"] not in wanted:
                continue
            event = _decision_event(name, item, sources)
            if event is None:
                continue
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
            yield event

    events = sorted(deliverable_decisions(),
                    key=lambda event: (_stamp(event), event["event"]))[:limit]
    decision_ordered = [diagnostic for _, diagnostic in
                        sorted(decision_diagnostics, key=lambda entry: entry[0])[:cap]]
    diagnostics = list(decision_ordered)

    if include_insights:
        configured_roles = set(project.roles) & project.config.roles.keys()
        pending_diagnostics: list[tuple[str, dict]] = []

        def deliverable_insights():
            for item in proposals.values():
                if item["id"] in decided:
                    continue
                event = _pending_event(name, item, configured_roles)
                if event is None:
                    continue
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
                yield event

        insights = sorted(deliverable_insights(),
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
                for index in range(len(delivered) - 1, -1, -1):
                    if delivered[index].get("kind") == INSIGHT_PENDING:
                        del delivered[index]
                        break
                else:
                    break
            if len(delivered) < len(limited):
                page = {"events": list(delivered), **legacy_summary(delivered)}
                return dict(shared=False, events=delivered, diagnostics=diagnostics,
                            page=page, manifest=())
        events = delivered
    return dict(shared=False, events=events, diagnostics=diagnostics,
                page=None, manifest=())
