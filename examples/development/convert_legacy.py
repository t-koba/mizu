#!/usr/bin/env python3
"""Operator-run, one-time conversion of legacy records to declared queues.

This is example userland, not kernel code: it reads the legacy Insight API and
the shared record store, then appends equivalent messages to the ``proposals``,
``decisions``, ``questions``, ``answers`` and ``notices`` queues declared in
``queues.toml``. Nothing here changes legacy authority, routes a runtime
message, writes a kernel object or grants a capability. The only writes are
QueueStore appends, and those go through the queue declarations' existing write
grants.

The current live insight and its current effective decision become one proposal
and (when present) one decision message. A deferral whose revisit carries an
aware instant also becomes a delayed decision message from the writer to itself,
referencing that proposal. Declared role questions become question messages,
their exact authenticated answers become referencing answer messages, and
operator notices become notice messages. Historical writers that differ from a
queue's declared author are retained in the message body, never used to bypass
a write grant. Message ids derive from legacy identity and content, so an exact
replay appends nothing: QueueStore returns the existing message. All input is
validated before the first append; a malformed or unbindable record refuses the
whole run. Archived revisions stay in the legacy archive; they are not silently
reinvented here.

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
from mizu.errors import MizuError
from mizu.fs import canonical, lock
from mizu.kernel.queues import allows
from mizu.kernel_shadow import instant_value
from mizu.project import Project

PROPOSALS = "proposals"
DECISIONS = "decisions"
QUESTIONS = "questions"
ANSWERS = "answers"
NOTICES = "notices"
QUEUES = (PROPOSALS, DECISIONS, QUESTIONS, ANSWERS, NOTICES)
PROPOSAL_AUTHOR = "operator"
#: The declaration names role:writer as the decision writer; historical
#: decisions are attributed to that principal without granting a runtime role.
DECISION_AUTHOR = "role:writer"
ANSWER_AUTHOR = "operator"
NOTICE_AUTHOR = "operator"
QUESTION_FIELDS = ("legacy_payload", "legacy_subject", "legacy_writer",
                   "legacy_id", "legacy_digest")
RECORD_FIELDS = ("legacy_addressee", "legacy_subject", "legacy_writer",
                 "legacy_id", "legacy_digest", "legacy_action")
NOTICE_FIELDS = (*RECORD_FIELDS, "legacy_target")
PAGE = 1000
MAX_PAGES = 1024
ID = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}")


class ConversionError(Exception):
    """Malformed or ambiguous legacy input; refuse before any append."""


def _message_id(kind: str, identity: str, rev: int | str) -> str:
    """Stable, domain-separated 32-hex id for one legacy identity/revision."""
    token = f"convert-legacy:v1:{kind}:{identity}:{rev}".encode("utf-8")
    return hashlib.sha256(token).hexdigest()[:32]


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
    """Copy a declared string bound; never truncate or invent a missing value."""
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConversionError(f"{label} is missing or empty")
    if field.max is not None and len(value) > field.max:
        raise ConversionError(f"{label} exceeds the declared {field.max}-character bound")
    if field.values and value not in field.values:
        raise ConversionError(f"{label} is not a declared value")
    return value


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
                                   "Legacy question digest")}
        key = (subject["kind"], subject["id"], subject["digest"])
        if key in index:
            raise ConversionError(f"Conflicting legacy role question {subject['id']}")
        index[key] = _message_id("question", f"{subject['kind']}/{subject['id']}", digest)
        messages.append({"queue": QUESTIONS, "id": index[key], "author": author,
                         "refs": (), "body": body})
    return messages, index


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
        else:
            value = _json(envelope["subject"], "Legacy subject")
        body[name] = _copy(value, declaration.fields[name], name.replace("_", " "))
    return body


def _operator_messages(project, question_index, answer_decl, notice_decl):
    """Enumerate authenticated answers and notices from the compact catalog."""
    try:
        with os.scandir(project.root / "shared-records" / "operator") as entries:
            if not any(entry.is_file(follow_symlinks=False) for entry in entries):
                return []  # A dry run creates no lock or index for an empty family.
    except FileNotFoundError:
        return []
    shared = legacy.store(project)
    with lock(project.root / "locks" / "operator-records.lock"):
        indexed = legacy.catalog(shared, readonly=True, scan_limit=_scan_limit(project))
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
            if not isinstance(subject, dict) or subject.get("type") != "record":
                raise ConversionError(
                    f"Legacy answer {record_id} does not address a declared role question")
            question_id = question_index.get(
                (subject.get("kind"), subject.get("id"), subject.get("digest")))
            if question_id is None:
                raise ConversionError(
                    f"Legacy answer {record_id} does not bind an exact role question")
            messages.append({"queue": ANSWERS,
                             "id": _message_id("answer", record_id, digest),
                             "author": ANSWER_AUTHOR, "refs": (question_id,),
                             "body": _record_body(envelope, digest, answer_decl, RECORD_FIELDS)})
        else:
            messages.append({"queue": NOTICES,
                             "id": _message_id("notice", record_id, digest),
                             "author": NOTICE_AUTHOR, "refs": (),
                             "body": _record_body(envelope, digest, notice_decl, NOTICE_FIELDS,
                                                  allow_empty=("body",))})
    return messages


def _plan(project) -> tuple[list[dict], dict]:
    proposal_decl = _fields(project, PROPOSALS, ("title", "body"))
    decision_decl = _fields(project, DECISIONS, ("action", "reason"))
    question_decl = _fields(project, QUESTIONS, ("question",), QUESTION_FIELDS)
    answer_decl = _fields(project, ANSWERS, ("body",), RECORD_FIELDS)
    notice_decl = _fields(project, NOTICES, ("body",), NOTICE_FIELDS)
    if not allows(proposal_decl.write, PROPOSAL_AUTHOR):
        raise ConversionError("operator may not append to the proposals queue")
    if not allows(decision_decl.write, DECISION_AUTHOR):
        raise ConversionError("the declared decision writer may not append to decisions")
    if not allows(answer_decl.write, ANSWER_AUTHOR):
        raise ConversionError("operator may not append to the answers queue")
    if not allows(notice_decl.write, NOTICE_AUTHOR):
        raise ConversionError("operator may not append to the notices queue")

    messages: list[dict] = []
    by_id: dict[str, dict] = {}
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
            proposal_id = _message_id("proposal", insight_id, rev)
            add({"queue": PROPOSALS, "id": proposal_id, "author": PROPOSAL_AUTHOR,
                 "refs": (), "body": {
                     "title": _text(record.get("title"), proposal_decl.fields["title"], "Title"),
                     "body": _text(record.get("body"), proposal_decl.fields["body"], "Body")}})

            decision = summary.get("decision")
            if decision is None:
                continue
            if not isinstance(decision, dict) or decision.get("id") != insight_id:
                raise ConversionError(f"Malformed legacy decision for insight {insight_id}")
            if _revision(decision.get("rev"), "Decision revision") != rev:
                raise ConversionError(f"Decision for {insight_id} does not bind its current revision")
            action = _text(decision.get("action"), decision_decl.fields["action"], "Action")
            reason = _text(decision.get("reason"), decision_decl.fields["reason"], "Reason")
            body = {"action": action, "reason": reason}
            if action == "defer":
                revisit = _text(decision.get("revisit"), decision_decl.fields["revisit"], "Revisit")
                body["revisit"] = revisit
            add({"queue": DECISIONS, "id": _message_id("decision", insight_id, rev),
                 "author": DECISION_AUTHOR, "refs": (proposal_id,), "body": dict(body)})
            if action == "defer":
                instant = _revisit(decision.get("revisit"), "Revisit")
                if instant is not None:
                    add({"queue": DECISIONS, "id": _message_id("revisit", insight_id, rev),
                         "author": DECISION_AUTHOR, "refs": (proposal_id,),
                         "deliver_at": instant, "body": dict(body)})
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        if not cursor:
            raise ConversionError("Insight pagination ended without a cursor")
    else:
        raise ConversionError(f"Legacy insight enumeration exceeded {MAX_PAGES} pages")

    questions, question_index = _role_questions(project, question_decl)
    for message in questions:
        add(message)
    for message in _operator_messages(project, question_index, answer_decl, notice_decl):
        add(message)

    report = {"project": project.name, "applied": False,
              "proposals": [m["id"] for m in messages if m["queue"] == PROPOSALS],
              "decisions": [m["id"] for m in messages
                            if m["queue"] == DECISIONS and "deliver_at" not in m],
              "revisits": [m["id"] for m in messages if "deliver_at" in m],
              "questions": [m["id"] for m in messages if m["queue"] == QUESTIONS],
              "answers": [m["id"] for m in messages if m["queue"] == ANSWERS],
              "notices": [m["id"] for m in messages if m["queue"] == NOTICES]}
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
        for queue in QUEUES:
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
