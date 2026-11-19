#!/usr/bin/env python3
"""Operator-run, one-time conversion of legacy insight/decision records to queues.

This is example userland, not kernel code: it reads the legacy Insight API and
appends equivalent messages to the ``proposals`` and ``decisions`` queues
declared in ``queues.toml``. Nothing here changes legacy authority, routes a
runtime message, writes a kernel object or grants a capability. The only writes
are QueueStore appends, and those go through the queue declarations' existing
write grants.

The current live insight and its current effective decision become one proposal
and (when present) one decision message. A deferral whose revisit carries an
aware instant also becomes a delayed decision message from the writer to itself,
referencing that proposal. Archived revisions stay in the legacy archive; they
are not silently reinvented here. Message ids are derived from legacy identity
and revision, so an exact replay appends nothing: QueueStore returns the
existing message. All input is validated before the first append; a malformed
or ambiguous record refuses the whole run.

Run with --apply to write; without it the command only prints the plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mizu.config import load
from mizu.errors import MizuError
from mizu.kernel.queues import allows
from mizu.kernel_shadow import instant_value
from mizu.project import Project

PROPOSALS = "proposals"
DECISIONS = "decisions"
PROPOSAL_AUTHOR = "operator"
#: The declaration names role:writer as the decision writer; historical
#: decisions are attributed to that principal without granting a runtime role.
DECISION_AUTHOR = "role:writer"
PAGE = 1000
MAX_PAGES = 1024
ID = re.compile(r"[0-9a-f]{32}\Z")
DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}")


class ConversionError(Exception):
    """Malformed or ambiguous legacy input; refuse before any append."""


def _message_id(kind: str, insight_id: str, rev: int) -> str:
    """Stable, domain-separated 32-hex id for one legacy identity/revision."""
    token = f"convert-legacy:v1:{kind}:{insight_id}:{rev}".encode("utf-8")
    return hashlib.sha256(token).hexdigest()[:32]


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ConversionError(f"{label} is not a 32-hex insight id")
    return value


def _revision(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ConversionError(f"{label} is not a positive integer revision")
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


def _fields(project, queue: str, names: tuple[str, ...]):
    declaration = project.config.queues.get(queue)
    if declaration is None:
        raise ConversionError(f"Configuration does not declare the {queue} queue")
    if not all(name in declaration.fields and declaration.fields[name].required for name in names):
        raise ConversionError(f"{queue} declaration lacks a required field in {', '.join(names)}")
    return declaration


def _text(value: object, field, label: str) -> str:
    """Copy declared field bounds; never truncate or invent a missing value."""
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"{label} is missing or empty")
    if field.max is not None and len(value) > field.max:
        raise ConversionError(f"{label} exceeds the declared {field.max}-character bound")
    if field.values and value not in field.values:
        raise ConversionError(f"{label} is not a declared value")
    return value


def _plan(project) -> tuple[list[dict], dict]:
    proposal_decl = _fields(project, PROPOSALS, ("title", "body"))
    decision_decl = _fields(project, DECISIONS, ("action", "reason"))
    if not allows(proposal_decl.write, PROPOSAL_AUTHOR):
        raise ConversionError("operator may not append to the proposals queue")
    if not allows(decision_decl.write, DECISION_AUTHOR):
        raise ConversionError("the declared decision writer may not append to decisions")

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

    report = {"project": project.name, "applied": False,
              "proposals": [m["id"] for m in messages if m["queue"] == PROPOSALS],
              "decisions": [m["id"] for m in messages
                            if m["queue"] == DECISIONS and "deliver_at" not in m],
              "revisits": [m["id"] for m in messages if "deliver_at" in m]}
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
    parser.add_argument("config", help="Deployment configuration declaring both queues")
    parser.add_argument("--project", required=True, help="Managed project name")
    parser.add_argument("--apply", action="store_true",
                        help="Append queue messages (default: print the plan only)")
    args = parser.parse_args(argv)
    try:
        config = load(Path(args.config))
        for queue in (PROPOSALS, DECISIONS):
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
