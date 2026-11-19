"""Queue-backed stage-2 record delivery, outside ``src/mizu/kernel``.

A host marker (see :mod:`mizu.authority_marker`) selects this adapter for all
operator/role record-family delivery. It rebuilds the exact legacy envelope,
``SharedRecords.Delivery`` and ack token from the declared ``legacy_*`` message
metadata, so the shared record delivery page, work-item callbacks and host CLI
keep the same shapes and bytes. Settled acknowledgements are appended only to
the declared host-writable ``record-receipts`` queue with stable message ids.

Trust and bounds: principals come only from the trusted context/project (a
role name, ``operator`` for host views, ``host`` for receipts); no tool argument
can name a principal. Reads start at cursor zero, use the kernel page bound and
the stage-2 page-count bound, and never move a durable cursor. Envelope
reconstruction verifies the pinned legacy digest, and malformed, ambiguous or
undeclared data refuses instead of falling back. Legacy storage, kernel shadow
audit and record migration keep their own authority in this stage.
"""
from __future__ import annotations

from dataclasses import asdict, replace

from . import authority_marker, host_records, operator_records
from .config import MAX_DECISION_RECONCILIATION_BYTES
from .errors import Denied
from .fs import DIGEST, READ_PAGE_CHARS, canonical, digest, identifier, loads_finite, now
from .queue_decision_delivery import (CONVERSION_QUEUES, AdapterError, _body, _canonical_json,
                                      _field, _instant, _read_queue, _string)
from .shared_records import Delivery, SharedRecords, _name, _token

QUESTIONS = "questions"
ANSWERS = "answers"
NOTICES = "notices"
RECORD_RECEIPTS = "record-receipts"
QUEUES = (QUESTIONS, ANSWERS, NOTICES, RECORD_RECEIPTS)
#: The decision gate's original six plus the record receipts queue.
VERIFY_QUEUES = (*CONVERSION_QUEUES, RECORD_RECEIPTS)
KIND_BY_QUEUE = {ANSWERS: "answer", NOTICES: "operator.notice"}
#: Kind -> queue view of the same single declaration, for trusted kind inputs.
QUEUE_BY_KIND = {kind: queue for queue, kind in KIND_BY_QUEUE.items()}

AUTHORITY_FILE = "record-authority.json"
PLAN_FILE = "record-activation-plan.json"
REPORT_FILE = "record-activation-report.json"
KIND = "operator.records"
REPORT_BYTES = MAX_DECISION_RECONCILIATION_BYTES

#: Legacy pending selection is a 20-row heap; the queue adapter preserves it.
PENDING_ROWS = 20

QUESTION_FIELDS = ("question", "legacy_payload", "legacy_subject", "legacy_writer",
                   "legacy_id", "legacy_digest", "legacy_kind", "legacy_created_at")
RECORD_FIELDS = ("body", "legacy_addressee", "legacy_subject", "legacy_writer",
                 "legacy_id", "legacy_digest", "legacy_action", "legacy_created_at")
NOTICE_FIELDS = (*RECORD_FIELDS, "legacy_target")
RECEIPT_FIELDS = ("ack_token", "legacy_role", "legacy_record_id", "legacy_record_digest")
ACTIONS = ("publish", "withdraw", "supersede")


def authority(root, *, report_bytes=REPORT_BYTES):
    """Return the validated record marker, or None when records stay legacy."""
    return authority_marker.read(root, file=AUTHORITY_FILE, plan_file=PLAN_FILE,
                                 report_file=REPORT_FILE, kind=KIND, label="record",
                                 report_bytes=report_bytes)


def authority_kind(root, *, report_bytes=REPORT_BYTES):
    """Return None or the selected durable record authority."""
    marker = authority(root, report_bytes=report_bytes)
    return None if marker is None else marker["authority"]


def _require_queues(project):
    """A marker with missing or malformed queue declarations never falls back."""
    needed = {QUESTIONS: QUESTION_FIELDS, ANSWERS: RECORD_FIELDS,
              NOTICES: NOTICE_FIELDS, RECORD_RECEIPTS: RECEIPT_FIELDS}
    for queue, fields in needed.items():
        declaration = project.config.queues.get(queue)
        if declaration is None or any(name not in declaration.fields for name in fields):
            raise AdapterError(f"{queue} declaration lacks record delivery metadata")


def _digest_value(project, queue, body, label, name="legacy_digest"):
    value = _field(project, queue, name, body, label)
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise AdapterError(f"{label} has an invalid {name}")
    return value


def _json_object(project, queue, body, name, label):
    value = _canonical_json(_string(project, queue, name, body, label), label)
    if not isinstance(value, dict):
        raise AdapterError(f"{label} has a non-object {name}")
    return value


def _question_text(payload, field):
    """Same derivation as conversion: named body first, else canonical JSON."""
    if isinstance(payload, dict):
        for key in ("body", "question"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _copy(value, field, f"Legacy question {key}")
    try:
        value = canonical(payload).decode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("Legacy question payload is not encodable JSON") from exc
    return _copy(value, field, "Legacy question payload")


def _copy(value, field, label):
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{label} is missing or empty")
    try:
        field.validate(value)
    except Denied as exc:
        raise AdapterError(f"{label} exceeds its declaration: {exc}") from exc
    return value


def _record_envelope(project, message, queue):
    """Rebuild and verify one byte-exact legacy record envelope."""
    body = _body(message, queue)
    if message.get("deliver_at") is not None:
        raise AdapterError(f"{queue} record cannot be delayed")
    if queue == ANSWERS:
        if len(message["refs"]) != 1:
            raise AdapterError("An answer must bind one converted question or proposal")
    elif message["refs"]:
        raise AdapterError(f"{queue} record cannot reference another message")
    record_id = _name(_string(project, queue, "legacy_id", body, "Record"))
    record_digest = _digest_value(project, queue, body, "Record")
    writer = _string(project, queue, "legacy_writer", body, "Record")
    created_at = _instant(project, queue, "legacy_created_at", body, "Record")
    subject = _json_object(project, queue, body, "legacy_subject", "Record")
    if queue == QUESTIONS:
        kind = _string(project, queue, "legacy_kind", body, "Question")
        payload = _json_object(project, queue, body, "legacy_payload", "Question")
        field = project.config.queues[queue].fields["question"]
        _question_text(payload, field)
    else:
        kind = KIND_BY_QUEUE[queue]
        body_text = _field(project, queue, "body", body, "Record")
        if not isinstance(body_text, str) or len(body_text) > 32768:
            raise AdapterError("Record body is not a bounded string")
        target = _canonical_json(_string(project, queue, "legacy_target", body, "Notice"),
                                 "Notice target") if queue == NOTICES else None
        payload = {"action": _field(project, queue, "legacy_action", body, "Record"),
                   "body": body_text,
                   "addressee": _string(project, queue, "legacy_addressee", body, "Record"),
                   "target": target}
        if payload["action"] not in ACTIONS or (queue == ANSWERS and payload["action"] != "publish"):
            raise AdapterError("Record action is undeclared")
        if not body_text.strip() and (queue == ANSWERS or payload["action"] != "withdraw"):
            raise AdapterError("Record body is required")
    envelope = {"schema": 1, "id": record_id, "kind": kind, "created_at": created_at,
                "writer": writer, "subject": subject, "payload": payload}
    try:
        raw = canonical(envelope)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("Record envelope is not encodable JSON") from exc
    if digest(raw) != record_digest:
        raise AdapterError("Record envelope does not reproduce its legacy digest")
    _validate_envelope(project, queue, envelope)
    return {"queue": queue, "id": record_id, "digest": record_digest, "kind": kind,
            "envelope": envelope, "message": message, "refs": tuple(message["refs"])}


def _validate_envelope(project, queue, envelope):
    if queue == QUESTIONS:
        policies = host_records.configuration(project.config.record_kinds)
        policy = policies.get(envelope["kind"])
        if (policy is None or policy["route"] != "operator_question"
                or not envelope["writer"].startswith("role.")):
            raise AdapterError("Question kind or writer is not declared")
        store = host_records.store(project.root, project.snapshots, policies)
    else:
        store = operator_records.store(project)
    try:
        store._validate(envelope)
    except Denied as exc:
        raise AdapterError(f"Converted record envelope is invalid: {exc}") from exc


def _question_role(project, row, policies):
    envelope = row["envelope"]
    role = identifier(envelope["writer"][5:])
    prefix = f"role/{role}/{envelope['kind']}/"
    if (envelope["writer"] != policies[envelope["kind"]]["writer"]
            or not envelope["id"].startswith(prefix)
            or not envelope["id"][len(prefix):]):
        raise AdapterError("Question identity differs from its declaration")
    return operator_records._role(project, role)


def _load(project, actor):
    """Read all record families and settled receipts without moving a cursor."""
    _require_queues(project)
    store = project.kernel_queues
    records = []
    for queue in (QUESTIONS, ANSWERS, NOTICES):
        for message in _read_queue(store, queue, actor):
            row = _record_envelope(project, message, queue)
            if any(seen["id"] == row["id"] for seen in records):
                raise AdapterError(f"Duplicate converted record {row['id']}")
            records.append(row)
    receipts = {}
    for message in _read_queue(store, RECORD_RECEIPTS, actor):
        body = _body(message, RECORD_RECEIPTS)
        if message.get("deliver_at") is not None or message["refs"]:
            raise AdapterError("A record receipt cannot be delayed or reference a message")
        token = _field(project, RECORD_RECEIPTS, "ack_token", body, "Receipt")
        role = _string(project, RECORD_RECEIPTS, "legacy_role", body, "Receipt")
        record_id = _name(_string(project, RECORD_RECEIPTS, "legacy_record_id", body, "Receipt"))
        record_digest = _digest_value(project, RECORD_RECEIPTS, body, "Receipt",
                                      "legacy_record_digest")
        if not isinstance(token, str) or not DIGEST.fullmatch(token):
            raise AdapterError("Receipt has an invalid ack_token")
        if _token(role, record_id, record_digest) != token:
            raise AdapterError("Receipt token does not bind its record")
        if token in receipts and canonical(receipts[token]) != canonical(body):
            raise AdapterError("Conflicting settled record receipts")
        receipts[token] = body
    return records, receipts


def _by_id(records, record_id):
    for row in records:
        if row["id"] == record_id:
            return row
    return None


def _closure_ids(records):
    closed = {}
    for row in records:
        target = row["envelope"]["payload"]["target"] if row["queue"] == NOTICES else None
        if target is None:
            continue
        operator_records._subject({"type": "record", "kind": "operator.notice", **target})
        if (not isinstance(target, dict) or set(target) != {"id", "digest"}
                or not isinstance(target["digest"], str) or not DIGEST.fullmatch(target["digest"])
                or not isinstance(target["id"], str)):
            raise AdapterError("Notice closure has an invalid target")
        original = _by_id(records, target["id"])
        if (original is None or original["digest"] != target["digest"]
                or original["queue"] != NOTICES):
            raise AdapterError("Notice closure target is unavailable or changed")
        old, new = original["envelope"], row["envelope"]
        if (old["writer"] != new["writer"] or old["id"] == new["id"]
                or old["payload"]["action"] == "withdraw"
                or old["payload"]["addressee"] != new["payload"]["addressee"]):
            raise AdapterError("Only the notice writer may close an exact notice")
        if target["id"] in closed and closed[target["id"]] != row["id"]:
            raise AdapterError("Notice has conflicting closure events")
        closed[target["id"]] = row["id"]
    return closed


def _asking(project, subject, records):
    operator_records._subject(subject)
    if subject["type"] != "record":
        return operator_records._question(project, subject).get("source")
    policies = host_records.configuration(project.config.record_kinds)
    policy = policies.get(subject["kind"])
    if policy is None or policy["route"] != "operator_question":
        raise AdapterError("Record question route is undeclared")
    row = _by_id(records, subject["id"])
    if (row is None or row["queue"] != QUESTIONS or row["digest"] != subject["digest"]
            or row["kind"] != subject["kind"]):
        raise AdapterError("Answer does not bind an exact converted question")
    return _question_role(project, row, policies)


def pending(project, role):
    """Reconstruct the exact legacy pending page and frozen manifest."""
    _require_queues(project)
    name = operator_records._role(project, identifier(role.name))
    policy = operator_records.configuration(project.config.operator_records)
    records, receipts = _load(project, f"role:{name}")
    closed = _closure_ids(records)
    eligible = []
    count, wake_pending = 0, False
    for row in records:
        if row["queue"] not in (ANSWERS, NOTICES) or row["id"] in closed:
            continue
        envelope = row["envelope"]
        kind = row["kind"]
        payload = envelope["payload"]
        if not policy[kind]["enabled"] or payload["action"] == "withdraw":
            continue
        if payload["addressee"] != name:
            continue
        if kind == "answer" and _asking(project, envelope["subject"], records) != name:
            raise AdapterError("Answer must address the authenticated asking role")
        token = _token(name, row["id"], row["digest"])
        if token in receipts:
            continue
        count += 1
        wake_pending |= policy[kind]["wake"]
        eligible.append(row)
    eligible.sort(key=lambda row: (row["envelope"]["created_at"], row["id"]))
    selected = eligible[:PENDING_ROWS]
    manifest = tuple(Delivery(name, row["id"], row["digest"],
                              _token(name, row["id"], row["digest"])) for row in selected)
    events = [dict(asdict(entry), kind=row["kind"], subject=row["envelope"]["subject"])
              for entry, row in zip(manifest, selected)]
    page = {"events": events, "pending_count": count,
            "truncated": count > len(events), "wake_pending": wake_pending}
    return page, manifest


def due(project, role_name=None):
    if role_name is not None:
        operator_records._role(project, role_name)
    names = [role_name] if role_name is not None else [
        name for name in project.roles if project.config.roles[name].workspace == "write"]
    return any(pending(project, project.config.roles[name])[0]["wake_pending"] for name in names)


def bind(context, state=None):
    context.operator_record_page, context.operator_record_manifest = (
        pending(context.project, context.role) if state is None else state)
    if context.operator_record_manifest and "record_read" not in context.role.capabilities:
        context.role = replace(context.role, capabilities=(*context.role.capabilities, "record_read"))


def _manifest(context):
    if not context.operator_record_manifest:
        return {}
    return SharedRecords._manifest(context.role.name, context.operator_record_manifest)


def validate(context, tokens):
    allowed = _manifest(context)
    for token in tokens:
        if token not in allowed:
            raise Denied("Record token is not in this unit manifest")
        entry = allowed[token]
        records, _ = _load(context.project, f"role:{context.role.name}")
        row = _by_id(records, entry.record_id)
        if row is None or row["digest"] != entry.record_digest:
            raise Denied("Record delivery binding changed")


def _receipt_id(role, token):
    return digest(canonical({"queue": RECORD_RECEIPTS, "role": role,
                             "ack_token": token}))[:32]


def acknowledge(context, tokens):
    allowed = _manifest(context)
    selected = [allowed[token] for token in tokens if token in allowed]
    if len(selected) != len(tokens):
        raise Denied("Record acknowledgement is not in this unit manifest")
    validate(context, tokens)
    _, receipts = _load(context.project, f"role:{context.role.name}")
    store = context.project.kernel_queues
    for entry in selected:
        if entry.ack_token in receipts:
            continue
        body = {"ack_token": entry.ack_token, "legacy_role": context.role.name,
                "legacy_record_id": entry.record_id,
                "legacy_record_digest": entry.record_digest}
        store.append(RECORD_RECEIPTS, body, "host", id=_receipt_id(context.role.name,
                                                                  entry.ack_token))


def _page(row, entry, offset, limit):
    if (type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= READ_PAGE_CHARS):
        raise Denied("Invalid shared record page bounds")
    text = canonical(row["envelope"]).decode("utf-8")
    if offset > len(text):
        raise Denied("Shared record offset exceeds text length")
    end = min(offset + limit, len(text))
    return {"record_id": entry.record_id, "record_digest": entry.record_digest,
            "offset": offset, "total_chars": len(text),
            "next_offset": end if end < len(text) else None, "text": text[offset:end]}


def read(context, args):
    if set(args) - {"ack_token", "offset", "limit"} or "ack_token" not in args:
        raise Denied("Record read takes an exact token and optional paging only")
    allowed = _manifest(context)
    token = args.get("ack_token")
    if not isinstance(token, str) or token not in allowed:
        raise Denied("Record is not in the frozen record manifest")
    records, _ = _load(context.project, f"role:{context.role.name}")
    row = _by_id(records, allowed[token].record_id)
    if row is None or row["digest"] != allowed[token].record_digest:
        raise Denied("Record delivery binding changed")
    return _page(row, allowed[token], args.get("offset", 0), args.get("limit", READ_PAGE_CHARS))


def read_role(context, args):
    configured_role = context.config.roles.get(context.role.name)
    if configured_role is None or "record_read" not in configured_role.capabilities:
        raise Denied("Role record reading requires an explicit record_read grant")
    if "ack_token" in args:
        raise Denied("Role record read takes kind and id, not ack_token")
    if set(args) - {"kind", "id", "offset", "limit"} or {"kind", "id"} - set(args):
        raise Denied("Role record read requires exactly kind and id with optional paging")
    kind = args["kind"]
    if type(kind) is not str:
        raise Denied("Expected a declared record kind")
    writer = "role." + identifier(context.role.name)
    policies = host_records.configuration(context.config.record_kinds)
    policy = policies.get(kind)
    if policy is None or policy["writer"] != writer:
        raise Denied("Role record kind or writer is undeclared")
    raw_id = args["id"]
    if not isinstance(raw_id, str):
        raise Denied("Expected a bounded record name")
    record_id = _name(f"role/{context.role.name}/{kind}/{raw_id}")
    # The envelope writer stays dotted; the kernel queue actor is role:<name>.
    records, _ = _load(context.project, f"role:{context.role.name}")
    row = _by_id(records, record_id)
    if (row is None or row["queue"] != QUESTIONS or row["kind"] != kind
            or row["envelope"]["writer"] != writer):
        raise Denied("Shared record canonical identity mismatch")
    return _page(row, Delivery(context.role.name, record_id, row["digest"],
                               _token(context.role.name, record_id, row["digest"])),
                 args.get("offset", 0), args.get("limit", READ_PAGE_CHARS))


def _store_message(project, queue, record_id, actor):
    _require_queues(project)
    records, _ = _load(project, actor)
    return _by_id(records, record_id), records


def _new_id(queue, identity):
    return digest(canonical({"queue": queue, "record_id": identity}))[:32]


def write_role(context, args):
    """Runtime-authenticated role question write; no caller principal."""
    if "record_write" not in context.role.capabilities:
        raise Denied("Role record writing requires the record_write grant")
    if set(args) != {"kind", "id", "subject", "payload"}:
        raise Denied("Role record write requires exactly kind, id, subject and payload")
    writer = "role." + identifier(context.role.name)
    policies = host_records.configuration(context.config.record_kinds)
    kind = args["kind"]
    if type(kind) is not str or kind not in policies or policies[kind]["writer"] != writer:
        raise Denied("Role record kind or writer is undeclared")
    if policies[kind]["route"] != "operator_question":
        raise Denied("Role record kind is not an operator question")
    raw_id = args["id"]
    if not isinstance(raw_id, str):
        raise Denied("Expected a bounded record name")
    record_id = _name(f"role/{context.role.name}/{kind}/{raw_id}")
    try:
        subject, payload = (loads_finite(args[key]) for key in ("subject", "payload"))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied("Role record subject and payload must be JSON objects") from exc
    actor = f"role:{context.role.name}"
    existing, _ = _store_message(context.project, QUESTIONS, record_id, actor)
    created_at = now()
    if existing is not None:
        envelope = existing["envelope"]
        if (envelope["subject"] != subject or envelope["payload"] != payload
                or envelope["id"] != record_id or envelope["kind"] != kind
                or envelope["writer"] != writer):
            raise Denied("Role record identity conflict")
        created_at = envelope["created_at"]
    envelope = {"schema": 1, "id": record_id, "kind": kind, "created_at": created_at,
                "writer": writer, "subject": subject, "payload": payload}
    store = host_records.store(context.project.root, context.project.snapshots, policies)
    store._validate(envelope)
    computed = digest(canonical(envelope))
    field = context.config.queues[QUESTIONS].fields["question"]
    body = {
        "question": _question_text(payload, field),
        "legacy_payload": _copy(canonical(payload).decode("utf-8"),
                                context.config.queues[QUESTIONS].fields["legacy_payload"],
                                "Legacy question payload"),
        "legacy_subject": _copy(canonical(subject).decode("utf-8"),
                                context.config.queues[QUESTIONS].fields["legacy_subject"],
                                "Legacy question subject"),
        "legacy_writer": _copy(writer, context.config.queues[QUESTIONS].fields["legacy_writer"],
                               "Legacy question writer"),
        "legacy_id": _copy(record_id, context.config.queues[QUESTIONS].fields["legacy_id"],
                           "Legacy question id"),
        "legacy_digest": _copy(computed, context.config.queues[QUESTIONS].fields["legacy_digest"],
                               "Legacy question digest"),
        "legacy_kind": _copy(kind, context.config.queues[QUESTIONS].fields["legacy_kind"],
                             "Legacy question kind"),
        "legacy_created_at": _copy(created_at,
                                   context.config.queues[QUESTIONS].fields["legacy_created_at"],
                                   "Legacy question instant"),
    }
    message_id = existing["message"]["id"] if existing is not None else _new_id(QUESTIONS, record_id)
    context.project.kernel_queues.append(QUESTIONS, body, actor, id=message_id)
    return {"record": envelope, "record_digest": computed}


def write(project, *, event_id, kind, body="", addressee=None, subject=None,
          action="publish", target=None):
    """Trusted operator CLI write; identity is bound by this host entry point."""
    identifier(event_id)
    policy = operator_records.configuration(project.config.operator_records)
    if not isinstance(kind, str) or kind not in policy or not policy[kind]["enabled"]:
        raise Denied("Operator record kind is disabled or undeclared")
    if kind not in QUEUE_BY_KIND:
        raise Denied("Operator record kind has no declared queue")
    subject = {} if subject is None else subject
    records, _ = _load(project, "operator")
    if kind == "answer":
        asking = operator_records._role(project, _asking(project, subject, records))
        if addressee is not None and addressee != asking:
            raise Denied("Answer addressee differs from asking role")
        addressee = asking
    elif subject:
        operator_records._question(project, subject)
    envelope_id = f"operator/{project.name}/{event_id}"
    existing, _ = _store_message(project, QUEUE_BY_KIND[kind], envelope_id, "operator")
    created_at = now()
    if existing is not None:
        old = existing["envelope"]
        if old["id"] != envelope_id or old["kind"] != kind:
            raise Denied("Operator record identity conflict")
        if old["payload"]["target"] is not None:
            target = old["payload"]["target"]
        created_at = old["created_at"]
    envelope = {"schema": 1, "id": envelope_id, "kind": kind, "created_at": created_at,
                "writer": operator_records.WRITER,
                "subject": subject,
                "payload": {"action": action, "body": body, "addressee": addressee,
                            "target": target}}
    store = operator_records.store(project)
    store._validate(envelope)
    operator_records._route(project, envelope)
    if target is not None:
        by_id = {row["id"]: row for row in records}
        original = by_id.get(target["id"])
        if (original is None or original["digest"] != target["digest"]
                or original["queue"] != NOTICES):
            raise Denied("Closure target is unavailable or changed")
        old = original["envelope"]
        if (old["kind"] != "operator.notice" or old["writer"] != envelope["writer"]
                or old["payload"]["action"] == "withdraw" or old["id"] == envelope_id
                or old["payload"]["addressee"] != addressee):
            raise Denied("Only the notice writer may close an exact notice")
        for row in records:
            other = row["envelope"]["payload"].get("target")
            if (row["id"] != envelope_id and isinstance(other, dict)
                    and other.get("id") == target["id"]):
                raise Denied("Notice already closed by another event")
    computed = digest(canonical(envelope))
    queue = QUEUE_BY_KIND[kind]
    declaration = project.config.queues[queue]
    fields = declaration.fields
    message_body = {
        "body": _copy(body, fields["body"], "Legacy body") if body else
            _field(project, queue, "body", {"body": body}, "Record"),
        "legacy_addressee": _copy(addressee, fields["legacy_addressee"], "Legacy addressee"),
        "legacy_subject": _copy(canonical(subject).decode("utf-8"), fields["legacy_subject"],
                                "Legacy subject"),
        "legacy_writer": _copy(envelope["writer"], fields["legacy_writer"], "Legacy writer"),
        "legacy_id": _copy(envelope_id, fields["legacy_id"], "Legacy id"),
        "legacy_digest": _copy(computed, fields["legacy_digest"], "Legacy digest"),
        "legacy_action": _copy(action, fields["legacy_action"], "Legacy action"),
        "legacy_created_at": _copy(created_at, fields["legacy_created_at"], "Legacy instant"),
    }
    if queue == NOTICES:
        message_body["legacy_target"] = _copy(canonical(target).decode("utf-8"),
                                               fields["legacy_target"], "Legacy target")
    refs = () if kind != "answer" else (existing["refs"] if existing is not None else
                                        _answer_ref(project, subject, records))
    message_id = (existing["message"]["id"] if existing is not None
                  else _new_id(queue, envelope_id))
    project.kernel_queues.append(queue, message_body, "operator", refs=refs, id=message_id)
    return {"record": envelope, "record_digest": computed}


def _answer_ref(project, subject, records):
    if not isinstance(subject, dict) or subject.get("type") not in ("record", "insight"):
        raise Denied("Answer requires an exact question subject")
    if subject["type"] == "record":
        row = _by_id(records, subject.get("id"))
        if (row is None or row["digest"] != subject.get("digest")
                or row["kind"] != subject.get("kind")):
            raise Denied("Answer does not bind an exact converted question")
        return (row["message"]["id"],)
    for message in _read_queue(project.kernel_queues, "proposals", "operator"):
        if message["body"].get("legacy_insight") == subject["id"] \
                and int(message["body"].get("legacy_rev", 0)) == subject["rev"]:
            return (message["id"],)
    raise Denied("Answer does not bind a converted insight revision")


def questions(project, *, limit, scan_limit):
    """Bounded host-only role-question listing with exact legacy rows."""
    if (type(limit) is not int or limit < 1 or type(scan_limit) is not int or scan_limit < 1):
        raise Denied("Question listing bounds must be positive integers")
    policies = host_records.configuration(project.config.record_kinds)
    if not any(row["route"] == "operator_question" for row in policies.values()):
        return {"questions": [], "scanned": 0, "truncated": False}
    records, _ = _load(project, "operator")
    scanned = 0
    rows = []
    for row in records:
        if row["queue"] != QUESTIONS:
            continue
        scanned += 1
        if scanned > scan_limit:
            break
        envelope = row["envelope"]
        policy = policies.get(envelope["kind"])
        if policy is None or policy["route"] != "operator_question" or policy["writer"] != envelope["writer"]:
            continue
        role = _question_role(project, row, policies)
        subject = {"type": "record", "kind": envelope["kind"],
                   "id": envelope["id"], "digest": row["digest"]}
        rows.append({"subject": subject, "record": envelope, "record_digest": row["digest"],
                     "closed": False})
        if len(rows) == limit:
            break
    answers = [row for row in records if row["queue"] == ANSWERS]
    for answer in answers:
        subject = answer["envelope"]["subject"]
        if subject.get("type") != "record":
            continue
        for entry in rows:
            if entry["subject"] == subject:
                if answer["envelope"]["payload"]["addressee"] != entry["record"]["writer"][5:]:
                    raise Denied("Answer must address the authenticated asking role")
                entry["closed"] = True
    return {"questions": rows[:limit], "scanned": min(scanned, scan_limit),
            "truncated": len(rows) > limit or scanned > scan_limit}


def _answer_rows(records, insight_id):
    rows = []
    for row in records:
        if row["queue"] != ANSWERS:
            continue
        subject = row["envelope"]["subject"]
        if (subject.get("type") == "insight" and subject.get("id") == insight_id):
            rows.append((row["envelope"], row["digest"]))
    return rows, True


def insight_answers(project, insight_id):
    records, _ = _load(project, "operator")
    return _answer_rows(records, insight_id)


def reproject_answers(project, insight_id):
    answers, complete = insight_answers(project, insight_id)
    for envelope, computed in answers:
        try:
            insight = operator_records._question(project, envelope["subject"]) \
                if envelope["subject"].get("type") == "insight" else None
            project.insights.kernel_shadow.answered(envelope, insight)
        except Exception as exc:
            project.insights.kernel_shadow.note_failure("answer", envelope["id"], exc)
    return answers, complete


def register_question_wait(project, subject, holder):
    if not operator_records.configuration(project.config.operator_records)["answer"]["enabled"]:
        raise Denied("Record waits require the operator answer route")
    if _asking(project, subject, _load(project, "operator")[0]) != holder:
        raise Denied("Record question writer must be the current work-item holder")


def record_question_lookup(project):
    """Lazy closure evidence for exact record questions, from the queue view."""
    answers, loaded = None, False
    questions = {}

    def closed(subject):
        nonlocal answers, loaded
        try:
            operator_records._subject(subject)
            if subject["type"] != "record":
                return None
            key = (subject["kind"], subject["id"], subject["digest"])
            if key not in questions:
                questions[key] = _asking(project, subject, _load(project, "operator")[0])
            if questions[key] is None:
                return None
            if not loaded:
                loaded = True
                answers = {}
                records, _ = _load(project, "operator")
                for row in records:
                    if row["queue"] != ANSWERS:
                        continue
                    target = row["envelope"]["subject"]
                    if target.get("type") == "record":
                        answer_key = (target["kind"], target["id"], target["digest"])
                        answers.setdefault(answer_key, set()).add(
                            row["envelope"]["payload"]["addressee"])
            if answers is None:
                return None
            recipients = answers.get(key, set())
            if any(recipient != questions[key] for recipient in recipients):
                return None
            return bool(recipients)
        except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    return closed


def record_answer_evidence(project, subject, holder, proof=None):
    """Select or replay one exact authenticated answer from the queue view."""
    records, _ = _load(project, "operator")
    if proof is None:
        if _asking(project, subject, records) != holder:
            raise Denied("Answer resolution question writer mismatch")
        candidates = [row for row in records if row["queue"] == ANSWERS
                      and row["envelope"]["subject"] == subject]
        if any(row["envelope"]["payload"]["addressee"] != holder for row in candidates):
            raise Denied("Answer resolution route mismatch")
        if not candidates:
            return None
        row = min(candidates, key=lambda item: item["id"])
        return {"event": row["id"], "digest": row["digest"], "rev": subject["digest"],
                "state": "answered"}
    if proof["rev"] != subject["digest"] or proof["state"] != "answered":
        raise Denied("Answer resolution subject/state mismatch")
    row = _by_id(records, proof["event"])
    if (row is None or row["queue"] != ANSWERS or row["digest"] != proof["digest"]
            or row["envelope"]["subject"] != subject
            or row["envelope"]["payload"]["addressee"] != holder):
        raise Denied("Answer resolution route mismatch")
    return proof
