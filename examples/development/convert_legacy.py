#!/usr/bin/env python3
"""Operator-run, one-time conversion of legacy records to declared queues.

This is example userland, not kernel code: it reads the legacy Insight API and
the shared record store, then appends equivalent messages to the ``proposals``,
``decisions``, ``questions``, ``answers``, ``notices``, ``receipts`` and
``record-receipts`` queues declared in ``queues.toml``. Nothing here changes
legacy authority, routes a runtime message, writes a kernel object or grants a
capability. The only writes are QueueStore appends, and those go through the
queue declarations' existing write grants. ``QUEUES`` stays the original six
conversion families; ``VERIFY_QUEUES`` adds record receipts for the record gate
without changing the decision gate's plan or report identity.

The current live insight and its current effective decision become one proposal
and (when present) one decision message, both carrying the exact legacy
decision-event metadata in declared optional fields: identity, revision,
routing, evidence, instant, kind and urgency on the proposal; the immutable
history event id and record (including its run and decision key) on the
decision. The event id is the decision-history filename, not the decision key:
deferrals and withdrawals use a different name, and the whole retained record
binds the legacy acknowledgement token byte for byte. A deferral whose revisit
carries an aware instant also becomes a delayed decision message from the
writer to itself, referencing that proposal. Declared role questions become
question messages carrying the exact envelope ``kind`` and ``created_at`` instant; their exact
authenticated answers become referencing answer messages, and operator
notices become notice messages, both carrying the envelope ``created_at``. A
legacy answer to an insight revision becomes an answer message referencing
that revision's converted proposal. An answer naming a retained earlier revision
converts that exact archived revision into its own stable proposal; a revision
that is neither current nor retained refuses the run. Legacy
decision-receipt files become per-token ``receipts`` messages: the exact
receipt reader classifies each token as settled or as one of its three
corrupt reasons, and the token is bound to a current legacy candidate event to
infer the action and its legacy enumeration order (decision-history glob order
then pending-envelope order, fixed-width hex). Receipts that no current
candidate binds are reported and left unconverted, since they cannot affect a
legacy page. Per-role, per-record SharedRecords acknowledgements become
host-authored ``record-receipts`` messages: each canonical receipt is bound to
its role, record id and pinned immutable record digest, and the token is
rebuilt with the same SharedRecords binding so an exact acknowledgement
suppresses the same delivery if record authority is switched later. Stable ids
make a replay idempotent. These are conversion messages, not a runtime switch.
Historical writers that differ from a queue's declared author are retained in
the message body, never used to bypass a write grant. Message ids derive from
legacy identity and content, so an exact replay appends nothing: QueueStore
returns the existing message. All input is validated before the first append; a malformed or unbindable record refuses the
whole run. Archived revisions stay in the legacy archive; conversion only
projects them, never rewrites legacy authority.

Run with --apply to write; without it the command only prints the plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mizu import operator_records as legacy
from mizu.config import load
from mizu import insight_archive
from mizu.errors import Denied, LimitExceeded, MizuError
from mizu.fs import MAX_FRAME, canonical, digest, identifier, loads_finite, lock, safe_read
from mizu.insights import CorruptDecisionReceipt
from mizu.kernel.queues import allows
from mizu.kernel_shadow import instant_value
from mizu.project import Project
from mizu.queue_decision_delivery import (DECISION_KIND, PROPOSAL_KIND, REVISIT_KIND,
                                          message_id)
from mizu.shared_records import (RECORD_BYTES, RECEIPT_BYTES, SharedRecords,
                                 _name, _token)

PROPOSALS = "proposals"
DECISIONS = "decisions"
QUESTIONS = "questions"
ANSWERS = "answers"
NOTICES = "notices"
RECEIPTS = "receipts"
RECORD_RECEIPTS = "record-receipts"
QUEUES = (PROPOSALS, DECISIONS, QUESTIONS, ANSWERS, NOTICES, RECEIPTS)
#: The original six stay pinned for decision activation reports and retries.
#: The record gate verifies this larger set; the record receipts never enter
#: the decision gate's plan/report identity.
VERIFY_QUEUES = (*QUEUES, RECORD_RECEIPTS)
RECORD_RECEIPT_FIELDS = ("ack_token", "legacy_role", "legacy_record_id", "legacy_record_digest")
RECEIPT_AUTHOR = "host"
RECEIPT_FIELDS = ("ack_token", "legacy_role", "legacy_action")
RECEIPT_OPTIONAL = ("legacy_reason", "legacy_order")
#: Fixed-width hex width for the legacy enumeration order; lexical == numeric.
RECEIPT_ORDER_WIDTH = 8
RECEIPT_ORDER_RE = re.compile(r"[0-9a-f]{8}\Z")
DECISION_ACTIONS = frozenset({"accept", "modify", "defer", "reject"})
PROPOSAL_AUTHOR = "operator"
#: The declaration names role:writer as the decision writer; historical
#: decisions are attributed to that principal without granting a runtime role.
DECISION_AUTHOR = "role:writer"
ANSWER_AUTHOR = "operator"
NOTICE_AUTHOR = "operator"
QUESTION_FIELDS = ("legacy_payload", "legacy_subject", "legacy_writer",
                   "legacy_id", "legacy_digest", "legacy_kind", "legacy_created_at")
RECORD_FIELDS = ("legacy_addressee", "legacy_subject", "legacy_writer",
                 "legacy_id", "legacy_digest", "legacy_action", "legacy_created_at")
NOTICE_FIELDS = (*RECORD_FIELDS, "legacy_target")
PROPOSAL_LEGACY_FIELDS = ("legacy_insight", "legacy_rev", "legacy_source",
                          "legacy_addressee", "legacy_evidence", "legacy_created_at",
                          "legacy_kind", "legacy_urgency")
DECISION_LEGACY_FIELDS = (*PROPOSAL_LEGACY_FIELDS, "legacy_event", "legacy_decision_key",
                          "legacy_run", "legacy_decided_at", "legacy_record")
PAGE = 1000
MAX_PAGES = 1024
ID = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}")


class ConversionError(Exception):
    """Malformed or ambiguous legacy input; refuse before any append."""


def _record_receipt_id(role: str, token: str) -> str:
    """Stable host-receipt message id: an exact replay cannot append twice."""
    return digest(canonical({"queue": RECORD_RECEIPTS, "role": role,
                             "ack_token": token}))[:32]


def _message_id(kind: str, identity: str, rev: int | str) -> str:
    """Stable id shared with the runtime producer, so conversion and live writes agree."""
    if kind not in (PROPOSAL_KIND, DECISION_KIND, REVISIT_KIND, "receipt", "question",
                    "answer", "notice"):
        raise ValueError(f"Unknown conversion message kind {kind!r}")
    return message_id(kind, identity, rev)


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ConversionError(f"{label} is not a 32-hex insight id")
    return value


def _revision(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ConversionError(f"{label} is not a positive integer revision")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ConversionError(f"{label} is not a 64-hex record digest")
    return value


def _revisit(value: object, label: str) -> str | None:
    """Extract the legacy revisit instant, or None for a non-instant phrase.

    Revisit text predates the queue declaration: a phrase with no instant has
    no deliverable message, but a date-shaped value that cannot be parsed as an
    aware instant is malformed and refuses the whole run before any append.
    ``instant_value`` returns the exact candidate string, never a rewrite.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConversionError(f"{label} is not a revisit string")
    instant = instant_value(value)
    if instant is None and DATEISH.search(value):
        raise ConversionError(f"{label} is not a parseable aware instant")
    return instant


def _json(value: object, label: str) -> str:
    """Encode an exact legacy JSON value as a declared string field."""
    try:
        return canonical(value).decode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ConversionError(f"{label} is not encodable JSON") from exc


def _fields(project, queue: str, names: tuple[str, ...],
            optional: tuple[str, ...] = ()):
    declaration = project.config.queues.get(queue)
    if declaration is None:
        raise ConversionError(f"Configuration does not declare the {queue} queue")
    for name in names:
        if name not in declaration.fields or not declaration.fields[name].required:
            raise ConversionError(f"{queue} declaration lacks a required field {name}")
    for name in optional:
        if name not in declaration.fields:
            raise ConversionError(f"{queue} declaration lacks an optional field {name}")
    return declaration


def _copy(value: object, field, label: str, *, allow_empty: bool = False) -> str:
    """Copy a declared bound; never truncate or invent a missing value."""
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConversionError(f"{label} is missing or empty")
    if field.max is not None and len(value) > field.max:
        raise ConversionError(f"{label} exceeds the declared {field.max}-character bound")
    if field.values and value not in field.values:
        raise ConversionError(f"{label} is not a declared value")
    try:
        field.validate(value)
    except Denied as exc:
        raise ConversionError(f"{label} is not a declared {field.type}: {exc}") from exc
    return value


def _evidence(value: object, label: str) -> list:
    """Require the exact legacy evidence list shape before encoding it."""
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise ConversionError(f"{label} is not a list of evidence entries")
    return value


def _nullable_copy(value: object, field, label: str):
    """Copy a declared field whose legacy value is explicitly null."""
    if value is None:
        return field.validate(None)
    return _copy(value, field, label)


def _receipt_files(root: Path) -> list[tuple[str, str]]:
    """Enumerate safe per-role, per-token legacy receipt file names."""
    if not insight_archive._directory(root):
        return []
    with os.scandir(root) as entries:
        roles = sorted(entry.name for entry in entries)
    if len(roles) > MAX_PAGES:
        raise ConversionError("Legacy receipt roles exceed the conversion scan bound")
    result: list[tuple[str, str]] = []
    for role in roles:
        identifier(role)
        parent = root / role
        if not insight_archive._directory(parent):
            raise ConversionError(f"Unsafe legacy decision receipt directory for {role}")
        with os.scandir(parent) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
        if len(names) > MAX_PAGES:
            raise ConversionError("Legacy receipts exceed the conversion scan bound")
        for name in names:
            token = name[:-5]
            if not DIGEST.fullmatch(token):
                raise ConversionError(f"Malformed legacy decision receipt token {name}")
            result.append((role, token))
    return result


def _receipt_candidates(project, role: str) -> dict[str, tuple[str, str, str]]:
    """Bind one role's current legacy candidates to (event id, action, order).

    A receipt file carries no action. Its token is bound to a current decision
    payload or pending envelope instead, which is the exact candidate legacy
    delivery uses; only that candidate can yield a receipt diagnostic. The
    order is the exact legacy diagnostic enumeration index: decision-history
    in the same ``Path.glob("*.json")`` order legacy ``_scan_decision_delivery``
    uses, then pending envelopes in the same ``_pending_insight_envelopes_locked``
    order legacy pending diagnostics use. The index is fixed-width hex so
    lexical order matches numeric order.
    """
    insights = project.insights
    candidates: dict[str, tuple[str, str, str]] = {}
    order = 0
    history = insights.root / "decision-history"
    if insight_archive._directory(history):
        paths = list(history.glob("*.json"))
        if len(paths) > MAX_PAGES:
            raise ConversionError("Legacy decision history exceeds the conversion scan bound")
        for path in paths:
            try:
                selected = insights._current_decision_event(
                    path, DECISION_ACTIONS, sources="any")
            except (Denied, LimitExceeded, OSError, ValueError, TypeError,
                    KeyError, AttributeError):
                continue
            if selected is None:
                continue
            payload = selected[1]
            try:
                identifier(payload["event"])
                token = insights._decision_token(role, payload)
            except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
            candidates[token] = (payload["event"], payload["action"],
                               f"{order:0{RECEIPT_ORDER_WIDTH}x}")
            order += 1
    configured = set(project.roles) & project.config.roles.keys()
    try:
        envelopes = list(insights._pending_insight_envelopes_locked(configured, role))
    except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ConversionError(f"Unreadable legacy pending candidates for {role}") from exc
    if len(envelopes) > MAX_PAGES:
        raise ConversionError("Legacy pending candidates exceed the conversion scan bound")
    for _, envelope in envelopes:
        payload = envelope["payload"]
        try:
            identifier(payload["event"])
            token = insights._decision_token(role, payload)
        except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        candidates[token] = (payload["event"], "pending",
                               f"{order:0{RECEIPT_ORDER_WIDTH}x}")
        order += 1
    return candidates


def _receipt_messages(project, declaration, bound_roles: dict) -> tuple[list[dict], list[str]]:
    """Project safe legacy receipt files through the exact receipt reader."""
    fields = declaration.fields
    messages: list[dict] = []
    unbound: list[str] = []
    for role, token in _receipt_files(project.insights.root / "decision-receipts"):
        if role not in bound_roles:
            bound_roles[role] = _receipt_candidates(project, role)
        candidate = bound_roles[role].get(token)
        if candidate is None:
            unbound.append(token)
            continue
        event, action, order = candidate
        if not RECEIPT_ORDER_RE.fullmatch(order):
            raise ConversionError("Legacy receipt order is not fixed-width hex")
        body = {
            "ack_token": _copy(token, fields["ack_token"], "Legacy receipt token"),
            "legacy_role": _copy(role, fields["legacy_role"], "Legacy receipt role"),
            "legacy_action": _copy(action, fields["legacy_action"], "Legacy receipt action"),
            "legacy_order": _copy(order, fields["legacy_order"], "Legacy receipt order"),
        }
        try:
            received = project.insights._decision_received(
                role, {"event": event, "ack_token": token})
        except CorruptDecisionReceipt as exc:
            body["legacy_reason"] = _copy(
                exc.reason, fields["legacy_reason"], "Legacy receipt reason")
        else:
            if not received:  # The file vanished between enumeration and the read.
                continue
        messages.append({"queue": RECEIPTS, "id": _message_id("receipt", role, token),
                         "author": RECEIPT_AUTHOR, "refs": (), "body": body})
    return messages, unbound


def _history_record(root, stem: str) -> dict:
    """Read one exact legacy decision-history event without rewriting it."""
    try:
        value = json.loads(safe_read(root, f"decision-history/{stem}.json", MAX_FRAME))
    except (Denied, OSError, ValueError, TypeError, UnicodeError) as exc:
        raise ConversionError(f"Unreadable legacy decision event {stem}") from exc
    if not isinstance(value, dict):
        raise ConversionError(f"Malformed legacy decision event {stem}")
    return value


def _decision_source(project, insight_id: str, rev: int, decision: dict) -> tuple[str, dict]:
    """Bind the effective decision to its exact history event id and record.

    The delivery event name is the history filename, not the decision key:
    deferrals and withdrawals use a random id. Substantive decisions are
    addressed by their key; other actions need one bounded history scan. The
    full immutable record is retained because work items and other fields also
    bind the legacy acknowledgement token.
    """
    root = project.insights.root
    key = decision.get("decision_key")
    if key is not None:
        key = _identity(key, "Legacy decision key")
        path = root / "decision-history" / f"{key}.json"
        found = path.is_file() and not path.is_symlink()
        candidates = [(key, _history_record(root, key))] if found else []
    else:
        candidates = []
    if not candidates:
        try:
            with os.scandir(root / "decision-history") as entries:
                names = sorted(entry.name for entry in entries
                               if entry.is_file(follow_symlinks=False))
        except FileNotFoundError:
            names = []
        if len(names) > MAX_PAGES:
            raise ConversionError("Legacy decision history exceeds the conversion scan bound")
        for name in names:
            stem = name[:-5] if name.endswith(".json") else name
            try:
                _identity(stem, "Legacy decision event")
            except ConversionError:
                continue
            candidates.append((stem, _history_record(root, stem)))
    matches = [(stem, record) for stem, record in candidates
               if record.get("id") == insight_id and record.get("rev") == rev
               and record.get("action") == decision.get("action")
               and record.get("reason") == decision.get("reason")
               and record.get("revisit") == decision.get("revisit")
               and record.get("decision_key") == decision.get("decision_key")]
    if len(matches) != 1:
        raise ConversionError(
            f"Cannot bind one legacy decision event for {insight_id} revision {rev}")
    return matches[0]


def _legacy_metadata(summary, record, decision, declaration, source=None) -> dict[str, str]:
    """Copy legacy decision-event metadata into one queue's declared fields.

    The values come from the validated insight page, current record and
    effective decision. Re-deriving them per queue enforces each queue's own
    declared bounds; a missing or malformed value refuses the whole run.
    """
    insight_id = _identity(summary.get("id"), "Insight id")
    rev = _revision(summary.get("rev"), "Insight revision")
    fields = declaration.fields
    metadata = {
        "legacy_insight": _copy(insight_id, fields["legacy_insight"], "Legacy insight id"),
        "legacy_rev": _copy(str(rev), fields["legacy_rev"], "Legacy insight revision"),
        "legacy_source": _copy(summary.get("source"), fields["legacy_source"],
                               "Legacy insight source"),
        "legacy_addressee": _copy(summary.get("addressee"), fields["legacy_addressee"],
                                  "Legacy insight addressee"),
        "legacy_evidence": _copy(
            _json(_evidence(record.get("evidence"), "Legacy evidence"), "Legacy evidence"),
            fields["legacy_evidence"], "Legacy evidence")}
    for name, label in (("created_at", "Legacy insight instant"),
                        ("kind", "Legacy insight kind"),
                        ("urgency", "Legacy insight urgency")):
        if name in record:
            field = f"legacy_{name}"
            metadata[field] = _copy(record[name], fields[field], label)
    if decision is not None:
        if source is None:
            raise ConversionError("Legacy decision event is not bound")
        stem, history = source
        metadata["legacy_event"] = _copy(stem, fields["legacy_event"], "Legacy decision event")
        metadata["legacy_decided_at"] = _copy(decision.get("created_at"),
                                              fields["legacy_decided_at"],
                                              "Legacy decision instant")
        if "decision_key" in decision:
            metadata["legacy_decision_key"] = _copy(
                decision["decision_key"], fields["legacy_decision_key"],
                "Legacy decision key")
        if "run" in decision:
            metadata["legacy_run"] = _nullable_copy(
                decision["run"], fields["legacy_run"], "Legacy decision run")
        metadata["legacy_record"] = _copy(
            _json(history, "Legacy decision event"), fields["legacy_record"],
            "Legacy decision event")
    return metadata


def _text(value: object, field, label: str) -> str:
    return _copy(value, field, label)


def _scan_limit(project) -> int:
    value = project.config.limits.operator_record_catalog_scan_entries
    if type(value) is not int or value < 1:
        raise ConversionError("Legacy operator record scan bound is not positive")
    return value


def _question_text(payload: object, field) -> str:
    """Derive the required question from the exact legacy payload.

    A named body is copied verbatim; other payloads are copied as canonical
    JSON. The whole original payload is also retained in ``legacy_payload``.
    """
    if isinstance(payload, dict):
        for key in ("body", "question"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _text(value, field, f"Legacy question {key}")
    return _text(_json(payload, "Legacy question payload"), field,
                 "Legacy question payload")


def _role_questions(project, declaration) -> tuple[list[dict], dict]:
    """Enumerate declared role questions through the host-only public view."""
    page = legacy.questions(project, limit=PAGE, scan_limit=_scan_limit(project))
    if page["truncated"]:
        raise ConversionError(
            f"Legacy role question enumeration exceeded {PAGE} rows or the scan bound")
    messages, index = [], {}
    for row in page["questions"]:
        subject, envelope = row.get("subject"), row.get("record")
        digest = _digest(row.get("record_digest"), "Role question digest")
        if (not isinstance(subject, dict)
                or set(subject) != {"type", "kind", "id", "digest"}
                or subject["type"] != "record"
                or not isinstance(subject["kind"], str)
                or not isinstance(subject["id"], str)
                or _digest(subject["digest"], "Role question subject digest") != digest):
            raise ConversionError("Malformed legacy role question subject")
        if (not isinstance(envelope, dict) or envelope.get("id") != subject["id"]
                or envelope.get("kind") != subject["kind"]):
            raise ConversionError("Ambiguous legacy role question identity")
        writer = envelope.get("writer")
        if not isinstance(writer, str) or not writer.startswith("role."):
            raise ConversionError("Legacy role question has no authenticated role writer")
        author = "role:" + writer[5:]
        if not allows(declaration.write, author):
            raise ConversionError(f"{author} may not append to the questions queue")
        payload = envelope.get("payload")
        body = {
            "question": _question_text(payload, declaration.fields["question"]),
            "legacy_payload": _copy(_json(payload, "Legacy question payload"),
                                    declaration.fields["legacy_payload"], "Legacy question payload"),
            "legacy_subject": _copy(_json(envelope.get("subject"), "Legacy question subject"),
                                    declaration.fields["legacy_subject"], "Legacy question subject"),
            "legacy_writer": _copy(writer, declaration.fields["legacy_writer"],
                                   "Legacy question writer"),
            "legacy_id": _copy(subject["id"], declaration.fields["legacy_id"],
                               "Legacy question id"),
            "legacy_digest": _copy(digest, declaration.fields["legacy_digest"],
                                   "Legacy question digest"),
            "legacy_kind": _copy(envelope["kind"], declaration.fields["legacy_kind"],
                                 "Legacy question kind"),
            "legacy_created_at": _copy(envelope["created_at"],
                                       declaration.fields["legacy_created_at"],
                                       "Legacy question instant")}
        key = (subject["kind"], subject["id"], subject["digest"])
        if key in index:
            raise ConversionError(f"Conflicting legacy role question {subject['id']}")
        index[key] = _message_id("question", f"{subject['kind']}/{subject['id']}", digest)
        messages.append({"queue": QUESTIONS, "id": index[key], "author": author,
                         "refs": (), "body": body})
    return messages, index


def _insight_subject(subject, label):
    """Parse an exact legacy insight revision; never default to the live rev."""
    if (not isinstance(subject, dict) or set(subject) != {"type", "id", "rev"}
            or subject["type"] != "insight"):
        raise ConversionError(f"{label} does not address an insight revision")
    return (_identity(subject["id"], "Legacy answer insight id"),
            _revision(subject["rev"], "Legacy answer insight revision"))


def _answer_insight_refs(indexed):
    """Distinct retained revision subjects named by published legacy answers."""
    refs = set()
    for record_id, (envelope, _) in indexed.items():
        if (envelope.get("kind") != "answer"
                or envelope["payload"]["action"] != "publish"):
            continue
        subject = envelope.get("subject")
        if isinstance(subject, dict) and subject.get("type") == "insight":
            refs.add(_insight_subject(subject, f"Legacy answer {record_id}"))
    return sorted(refs)


def _insight_proposal_ref(subject, proposal_index, label):
    """Bind an exact legacy insight answer subject to its converted proposal."""
    insight_id, rev = _insight_subject(subject, label)
    proposal_id = proposal_index.get((insight_id, rev))
    if proposal_id is None:
        raise ConversionError(f"{label} does not bind a converted insight revision")
    return proposal_id


def _record_body(envelope, digest, declaration, fields, *, allow_empty=()):
    """Copy the exact operator record content and its legacy routing metadata."""
    payload = envelope["payload"]
    body = {"body": _copy(payload["body"], declaration.fields["body"], "Legacy body",
                          allow_empty="body" in allow_empty)}
    for name in fields:
        if name == "legacy_addressee":
            value = payload["addressee"]
        elif name == "legacy_writer":
            value = envelope["writer"]
        elif name == "legacy_id":
            value = envelope["id"]
        elif name == "legacy_digest":
            value = digest
        elif name == "legacy_action":
            value = payload["action"]
        elif name == "legacy_target":
            value = _json(payload["target"], "Legacy target")
        elif name == "legacy_created_at":
            value = envelope["created_at"]
        else:
            value = _json(envelope["subject"], "Legacy subject")
        body[name] = _copy(value, declaration.fields[name], name.replace("_", " "))
    return body


def _operator_records(project):
    """Read the checked operator catalog once; None for an empty family."""
    try:
        with os.scandir(project.root / "shared-records" / "operator") as entries:
            if not any(entry.is_file(follow_symlinks=False) for entry in entries):
                return None  # A dry run creates no lock or index for an empty family.
    except FileNotFoundError:
        return None
    shared = legacy.store(project)
    with lock(project.root / "locks" / "operator-records.lock"):
        indexed = legacy.catalog(shared, readonly=True, scan_limit=_scan_limit(project))
    return shared, indexed


def _operator_messages(records, question_index, proposal_index, answer_decl, notice_decl):
    """Enumerate authenticated answers and notices from the compact catalog.

    Answers bind to either an exact declared role question or an insight
    revision whose proposal was converted in this run.
    """
    if records is None:
        return []
    shared, indexed = records
    messages = []
    for record_id in sorted(indexed):
        projection, digest = indexed[record_id]
        kind = projection["kind"]
        if kind not in ("answer", "operator.notice"):
            continue
        envelope, computed = shared.get(record_id, expected_digest=digest)
        if (computed != digest or envelope["id"] != record_id
                or envelope["kind"] != kind or envelope["writer"] != legacy.WRITER):
            raise ConversionError(f"Ambiguous legacy operator record {record_id}")
        if kind == "answer":
            if envelope["payload"]["action"] != "publish":
                raise ConversionError(f"Legacy answer {record_id} is not a published answer")
            subject = envelope["subject"]
            if (not isinstance(subject, dict)
                    or subject.get("type") not in ("record", "insight")):
                raise ConversionError(
                    f"Legacy answer {record_id} does not address a declared role question")
            if subject["type"] == "insight":
                target = _insight_proposal_ref(
                    subject, proposal_index, f"Legacy answer {record_id}")
            else:
                target = question_index.get(
                    (subject.get("kind"), subject.get("id"), subject.get("digest")))
                if target is None:
                    raise ConversionError(
                        f"Legacy answer {record_id} does not bind an exact role question")
            messages.append({"queue": ANSWERS,
                             "id": _message_id("answer", record_id, digest),
                             "author": ANSWER_AUTHOR, "refs": (target,),
                             "body": _record_body(envelope, digest, answer_decl, RECORD_FIELDS)})
        else:
            messages.append({"queue": NOTICES,
                             "id": _message_id("notice", record_id, digest),
                             "author": NOTICE_AUTHOR, "refs": (),
                             "body": _record_body(envelope, digest, notice_decl, NOTICE_FIELDS,
                                                  allow_empty=("body",))})
    return messages


def _record_receipt_messages(project, declaration) -> list[dict]:
    """Project exact per-role, per-record receipt files into host queue messages.

    Every receipt is validated as a canonical SharedRecords receipt and bound to
    an existing immutable record with the pinned digest. The legacy token uses
    the same SharedRecords binding the runtime adapter rebuilds, so an exact
    acknowledgement suppresses the same delivery after the switch.
    """
    root = project.insights.root / "shared-record-receipts"
    if not insight_archive._directory(root):
        return []
    with os.scandir(root) as entries:
        roles = sorted(entry.name for entry in entries)
    if len(roles) > MAX_PAGES:
        raise ConversionError("Legacy record receipt roles exceed the conversion scan bound")
    messages = []
    shared = SharedRecords(project.insights.root)
    for directory in roles:
        parent = root / directory
        if not insight_archive._directory(parent):
            raise ConversionError(f"Unsafe legacy record receipt directory for {directory}")
        with os.scandir(parent) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
        if len(names) > MAX_PAGES:
            raise ConversionError("Legacy record receipts exceed the conversion scan bound")
        for name in names:
            try:
                raw = safe_read(root, f"{directory}/{name}", RECEIPT_BYTES)
                receipt = loads_finite(raw)
                if (type(receipt) is not dict
                        or set(receipt) != {"role", "record_id", "record_digest", "acknowledged_at"}
                        or canonical(receipt) != raw):
                    raise ValueError("receipt shape")
                role = identifier(receipt["role"])
                if directory != hashlib.sha256(role.encode()).hexdigest():
                    raise ValueError("receipt role directory")
                if name != hashlib.sha256(
                        receipt["record_id"].encode()).hexdigest() + ".json":
                    raise ValueError("receipt record directory")
                record_id = _name(receipt["record_id"])
                record_digest = _digest(receipt["record_digest"], "Legacy record digest")
                token = _token(role, record_id, record_digest)
                record_name = shared.record_name(record_id)
                record_raw = safe_read(project.insights.root, record_name, RECORD_BYTES)
                if record_raw is None or digest(record_raw) != record_digest:
                    raise ValueError("record binding")
            except (Denied, OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
                raise ConversionError(f"Unbindable legacy record receipt {directory}/{name}") from exc
            body = {
                "ack_token": _copy(token, declaration.fields["ack_token"], "Record receipt token"),
                "legacy_role": _copy(role, declaration.fields["legacy_role"], "Record receipt role"),
                "legacy_record_id": _copy(record_id, declaration.fields["legacy_record_id"],
                                          "Record receipt id"),
                "legacy_record_digest": _copy(record_digest,
                                              declaration.fields["legacy_record_digest"],
                                              "Record receipt digest"),
            }
            messages.append({"queue": RECORD_RECEIPTS, "id": _record_receipt_id(role, token),
                             "author": RECEIPT_AUTHOR, "refs": (), "body": body})
    return messages


def _plan(project) -> tuple[list[dict], dict]:
    proposal_decl = _fields(project, PROPOSALS, ("title", "body"), PROPOSAL_LEGACY_FIELDS)
    decision_decl = _fields(project, DECISIONS, ("action", "reason"), DECISION_LEGACY_FIELDS)
    question_decl = _fields(project, QUESTIONS, ("question",), QUESTION_FIELDS)
    answer_decl = _fields(project, ANSWERS, ("body",), RECORD_FIELDS)
    notice_decl = _fields(project, NOTICES, ("body",), NOTICE_FIELDS)
    receipt_decl = _fields(project, RECEIPTS, RECEIPT_FIELDS, RECEIPT_OPTIONAL)
    record_receipt_decl = _fields(project, RECORD_RECEIPTS, RECORD_RECEIPT_FIELDS)
    if not allows(proposal_decl.write, PROPOSAL_AUTHOR):
        raise ConversionError("operator may not append to the proposals queue")
    if not allows(decision_decl.write, DECISION_AUTHOR):
        raise ConversionError("the declared decision writer may not append to decisions")
    if not allows(answer_decl.write, ANSWER_AUTHOR):
        raise ConversionError("operator may not append to the answers queue")
    if not allows(notice_decl.write, NOTICE_AUTHOR):
        raise ConversionError("operator may not append to the notices queue")
    if not allows(receipt_decl.write, RECEIPT_AUTHOR):
        raise ConversionError("host may not append to the receipts queue")
    if not allows(record_receipt_decl.write, RECEIPT_AUTHOR):
        raise ConversionError("host may not append to the record-receipts queue")

    messages: list[dict] = []
    by_id: dict[str, dict] = {}
    proposal_index: dict[tuple[str, int], str] = {}
    cursor = None

    def add(message: dict) -> None:
        previous = by_id.get(message["id"])
        if previous is not None and previous != message:
            raise ConversionError(f"Conflicting legacy records for message {message['id']}")
        if previous is None:
            by_id[message["id"]] = message
            messages.append(message)

    for _ in range(MAX_PAGES):
        page = project.insights.list_page(scope="live", limit=PAGE, cursor=cursor)
        for summary in page["items"]:
            insight_id = _identity(summary.get("id"), "Insight id")
            rev = _revision(summary.get("rev"), "Insight revision")
            record = project.insights.read(insight_id)
            if (not isinstance(record, dict) or record.get("id") != insight_id
                    or record.get("title") != summary.get("title")
                    or _revision(record.get("rev", 1), "Record revision") != rev):
                raise ConversionError(f"Ambiguous legacy insight {insight_id} at revision {rev}")
            proposal_id = _message_id(PROPOSAL_KIND, insight_id, rev)
            proposal_index[(insight_id, rev)] = proposal_id
            proposal_metadata = _legacy_metadata(summary, record, None, proposal_decl)
            add({"queue": PROPOSALS, "id": proposal_id, "author": PROPOSAL_AUTHOR,
                 "refs": (), "body": {
                     "title": _text(record.get("title"), proposal_decl.fields["title"], "Title"),
                     "body": _text(record.get("body"), proposal_decl.fields["body"], "Body"),
                     **proposal_metadata}})

            decision = summary.get("decision")
            if decision is None:
                continue
            if not isinstance(decision, dict) or decision.get("id") != insight_id:
                raise ConversionError(f"Malformed legacy decision for insight {insight_id}")
            if _revision(decision.get("rev"), "Decision revision") != rev:
                raise ConversionError(f"Decision for {insight_id} does not bind its current revision")
            action = _text(decision.get("action"), decision_decl.fields["action"], "Action")
            reason = _text(decision.get("reason"), decision_decl.fields["reason"], "Reason")
            source = _decision_source(project, insight_id, rev, decision)
            body = {"action": action, "reason": reason,
                    **_legacy_metadata(summary, record, decision, decision_decl, source)}
            if canonical(source[1]) != canonical(decision):
                raise ConversionError("Ambiguous legacy decision event metadata")
            if action == "defer":
                revisit = _text(decision.get("revisit"), decision_decl.fields["revisit"], "Revisit")
                body["revisit"] = revisit
            add({"queue": DECISIONS, "id": _message_id(DECISION_KIND, insight_id, rev),
                 "author": DECISION_AUTHOR, "refs": (proposal_id,), "body": dict(body)})
            if action == "defer":
                instant = _revisit(decision.get("revisit"), "Revisit")
                if instant is not None:
                    add({"queue": DECISIONS, "id": _message_id(REVISIT_KIND, insight_id, rev),
                         "author": DECISION_AUTHOR, "refs": (proposal_id,),
                         "deliver_at": instant, "body": dict(body)})
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        if not cursor:
            raise ConversionError("Insight pagination ended without a cursor")
    else:
        raise ConversionError(f"Legacy insight enumeration exceeded {MAX_PAGES} pages")

    operator_records = _operator_records(project)
    refs = _answer_insight_refs(operator_records[1]) if operator_records is not None else ()
    for insight_id, rev in refs:
        if (insight_id, rev) in proposal_index:
            continue  # The current live revision already has this exact proposal.
        archived = project.insights.read_revision(insight_id, rev)
        if archived is None:
            raise ConversionError("Legacy answer does not bind a converted insight revision")
        _identity(archived.get("id"), "Archived insight id")
        if _revision(archived.get("rev", 1), "Archived insight revision") != rev:
            raise ConversionError("Ambiguous legacy archived insight revision")
        proposal_id = _message_id(PROPOSAL_KIND, insight_id, rev)
        proposal_index[(insight_id, rev)] = proposal_id
        add({"queue": PROPOSALS, "id": proposal_id, "author": PROPOSAL_AUTHOR,
             "refs": (), "body": {
                 "title": _text(archived.get("title"), proposal_decl.fields["title"], "Title"),
                 "body": _text(archived.get("body"), proposal_decl.fields["body"], "Body"),
                 **_legacy_metadata(archived, archived, None, proposal_decl)}})

    questions, question_index = _role_questions(project, question_decl)
    for message in questions:
        add(message)
    for message in _operator_messages(operator_records, question_index, proposal_index,
                                      answer_decl, notice_decl):
        add(message)
    receipt_messages, unbound = _receipt_messages(project, receipt_decl, {})
    for message in receipt_messages:
        add(message)
    for message in _record_receipt_messages(project, record_receipt_decl):
        add(message)

    report = {"project": project.name, "applied": False,
              "proposals": [m["id"] for m in messages if m["queue"] == PROPOSALS],
              "decisions": [m["id"] for m in messages
                            if m["queue"] == DECISIONS and "deliver_at" not in m],
              "revisits": [m["id"] for m in messages if "deliver_at" in m],
              "questions": [m["id"] for m in messages if m["queue"] == QUESTIONS],
              "answers": [m["id"] for m in messages if m["queue"] == ANSWERS],
              "notices": [m["id"] for m in messages if m["queue"] == NOTICES],
              "receipts": [m["id"] for m in messages if m["queue"] == RECEIPTS],
              "record_receipts": [m["id"] for m in messages if m["queue"] == RECORD_RECEIPTS],
              "unbound_receipts": unbound}
    return messages, report


def plan(project) -> dict:
    """Preview the complete conversion without writing anything."""
    return _plan(project)[1]


def apply(project) -> dict:
    """Validate every legacy record first, then append idempotent messages."""
    messages, report = _plan(project)
    store = project.kernel_queues
    for message in messages:
        store.append(message["queue"], message["body"], message["author"],
                     refs=message["refs"], id=message["id"],
                     deliver_at=message.get("deliver_at"))
    report["applied"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="Deployment configuration declaring the queues")
    parser.add_argument("--project", required=True, help="Managed project name")
    parser.add_argument("--apply", action="store_true",
                        help="Append queue messages (default: print the plan only)")
    args = parser.parse_args(argv)
    try:
        config = load(Path(args.config))
        for queue in VERIFY_QUEUES:
            if queue not in config.queues:
                raise ConversionError(f"Configuration does not declare the {queue} queue")
        project = Project(config, args.project, read_only=not args.apply)
        report = apply(project) if args.apply else plan(project)
    except (ConversionError, MizuError, OSError, ValueError) as exc:
        print(f"conversion refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
