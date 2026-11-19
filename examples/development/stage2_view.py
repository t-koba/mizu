#!/usr/bin/env python3
"""Operator-run, read-only projection of the example workflow's stage-2 queues.

This is example userland, not kernel code. It derives the view entirely from
messages in the declared queues through :meth:`QueueStore.read`; it never reads
a legacy insight or operator record, never acknowledges a message, never
appends and never mutates a durable reader cursor. Every queue is read from
sequence one with an explicit cursor and a bounded page size, so a call with
the same ``now`` is repeatable and a queue larger than the bound is refused
rather than partially projected. A head-of-line message that is not yet due
stops that queue's read: its ``delayed`` flag is reported and later messages
are not silently skipped.

The projection contains only fields carried by messages:

    {"proposals": [{"id", "title", "body", "decision"?,
                    "revisits": [{"id", "deliver_at", "body"}, ...]}, ...],
     "questions": [{"id", "question", "context"?, "default"?,
                    "answer"?}, ...],
     "notices": [{"body"}, ...],
     "queues": {name: {"head", "delayed"}, ...}}

Optional message fields are omitted when absent. ``decision`` is the ordinary
decision referring to a proposal (its ``body`` fields ``action``, ``reason``
and, when present, ``revisit``); ``revisits`` holds the delayed messages
referring to it that are due at ``now``. ``answer`` is the answering message's
body. An ambiguous reference (more than one deciding decision or answer) is
refused. The command is for an operator to inspect the project; it writes
nothing and grants no capability.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mizu.config import load
from mizu.errors import MizuError
from mizu.kernel.queues import PAGE_MAX
from mizu.project import Project

READER = "operator"
QUEUES = ("proposals", "decisions", "questions", "answers", "notices")
PAGE = PAGE_MAX  # The kernel bounds a read page at 512 messages.
MAX_PAGES = 64   # At most 32768 messages per queue; more is refused.


class ViewError(Exception):
    """A queue or message cannot be projected without inventing facts."""


def _read_queue(store, name: str, now: str | None) -> tuple[list[dict], dict]:
    """Read the due prefix of one queue without touching the stored cursor."""
    messages: list[dict] = []
    cursor = 0
    for _ in range(MAX_PAGES):
        page = store.read(name, READER, cursor=cursor, limit=PAGE, now=now)
        messages.extend(page["messages"])
        meta = {"head": page["head"], "delayed": page["delayed"]}
        if page["delayed"] or not page["more"]:
            return messages, meta
        following = page["next"]
        if type(following) is not int or following <= cursor:
            raise ViewError(f"{name} pagination did not advance")
        cursor = following
    raise ViewError(f"{name} exceeded {MAX_PAGES} pages")


def _body(message: dict, name: str) -> Mapping:
    body = message.get("body")
    if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
        raise ViewError(f"Malformed body in {name} message {message.get('id')}")
    return body


def _fields(message: dict, name: str, keys: tuple[str, ...],
            required: tuple[str, ...] = ()) -> dict:
    """Copy declared message fields only; never add a value that is absent."""
    body = _body(message, name)
    result = {key: body[key] for key in keys if key in body}
    missing = [key for key in required if key not in result]
    if missing:
        raise ViewError(f"{name} message {message.get('id')} lacks {', '.join(missing)}")
    return result


def _refs(message: dict, name: str) -> list:
    refs = message.get("refs", [])
    if not isinstance(refs, (list, tuple)):
        raise ViewError(f"Malformed refs in {name} message {message.get('id')}")
    return list(refs)


def _referrers(messages: list[dict], name: str) -> dict:
    index: dict[str, list[dict]] = {}
    for message in messages:
        for ref in _refs(message, name):
            index.setdefault(ref, []).append(message)
    return index


def _one(index: dict[str, list[dict]], message_id: str, label: str,
         predicate=None) -> dict | None:
    matches = [message for message in index.get(message_id, ())
               if predicate is None or predicate(message)]
    if len(matches) > 1:
        raise ViewError(f"Ambiguous {label} for {message_id}")
    return matches[0] if matches else None


def project_view(project, now: str | None = None) -> dict:
    """Project the declared queues as stage-2 facts, using ``now`` for delivery."""
    store = project.kernel_queues
    reads = {name: _read_queue(store, name, now) for name in QUEUES}
    decisions = _referrers(reads["decisions"][0], "decisions")
    answers = _referrers(reads["answers"][0], "answers")

    proposals = []
    for message in reads["proposals"][0]:
        proposal_id = message["id"]
        fields = {"id": proposal_id,
                  **_fields(message, "proposals", ("title", "body"),
                            ("title", "body"))}
        deciding = _one(decisions, proposal_id, "decision",
                        lambda item: item.get("deliver_at") is None)
        if deciding is not None:
            fields["decision"] = _fields(deciding, "decisions",
                                         ("action", "reason", "revisit"),
                                         ("action", "reason"))
        fields["revisits"] = [
            {"id": item["id"], "deliver_at": item["deliver_at"],
             "body": dict(_body(item, "decisions"))}
            for item in decisions.get(proposal_id, ())
            if item.get("deliver_at") is not None]
        proposals.append(fields)

    questions = []
    for message in reads["questions"][0]:
        question_id = message["id"]
        fields = {"id": question_id,
                  **_fields(message, "questions", ("question", "context", "default"),
                            ("question",))}
        answering = _one(answers, question_id, "answer")
        if answering is not None:
            fields["answer"] = dict(_body(answering, "answers"))
        questions.append(fields)

    return {
        "proposals": proposals,
        "questions": questions,
        "notices": [dict(_body(message, "notices")) for message in reads["notices"][0]],
        "queues": {name: meta for name, (_, meta) in reads.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="Deployment configuration declaring the stage-2 queues")
    parser.add_argument("--project", required=True, help="Managed project name")
    parser.add_argument("--at", help="Aware ISO instant used for delayed delivery")
    args = parser.parse_args(argv)
    try:
        config = load(Path(args.config))
        for queue in QUEUES:
            if queue not in config.queues:
                raise ViewError(f"Configuration does not declare the {queue} queue")
        project = Project(config, args.project, read_only=True)
        view = project_view(project, now=args.at)
    except (ViewError, MizuError, OSError, ValueError) as exc:
        print(f"stage2 view refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(view, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
