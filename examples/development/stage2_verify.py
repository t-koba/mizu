#!/usr/bin/env python3
"""Operator-run, read-only gate for the converted stage-2 queues.

This is example userland, not kernel code: it recomputes the current legacy
conversion plan with ``convert_legacy._plan`` and compares it with the declared
queues, including converted receipts, as stored. Every read uses the ``operator`` principal, an
explicit cursor and a fixed far-future instant, so delayed revisits are
included and the result does not depend on the clock. Nothing is appended,
acknowledged or advanced. A record is compared on its queue, id, author,
body, order-sensitive refs and delivery instant (``None`` for immediate);
missing, extra and changed messages are reported as a deterministic list.
Duplicate ids, ungrowing pagination and a queue beyond the stage-2 view's
page bound refuse the check. Mismatch is a failed gate, not a rewrite: the
operator decides what to do with legacy records and stored messages.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import convert_legacy  # noqa: E402  (example userland module)
import stage2_view  # noqa: E402  (shared page bound)
from mizu.config import load  # noqa: E402
from mizu.errors import MizuError  # noqa: E402
from mizu.fs import canonical  # noqa: E402
from mizu.project import Project  # noqa: E402

QUEUES = convert_legacy.QUEUES
READER = "operator"
#: Later than any four-digit-year revisit, and aware so delivery is total.
NOW = "9999-12-31T23:59:59+00:00"
PAGE = stage2_view.PAGE
MAX_PAGES = stage2_view.MAX_PAGES
FIELDS = ("queue", "id", "author", "body", "refs", "deliver_at")


class VerifyError(Exception):
    """A queue or record cannot be compared without inventing facts."""


def _record(message: Mapping, source: str) -> dict:
    """Copy exactly the six comparable fields, never a store-local field."""
    if not isinstance(message, Mapping):
        raise VerifyError(f"Malformed {source} message")
    for field in ("queue", "id", "author", "refs", "body"):
        if field not in message:
            raise VerifyError(f"Malformed {source} message lacks {field}")
    queue, ident, author = message["queue"], message["id"], message["author"]
    if not isinstance(queue, str) or queue not in QUEUES:
        raise VerifyError(f"{source} message has an undeclared queue {queue!r}")
    if not isinstance(ident, str) or not ident:
        raise VerifyError(f"{source} message has no id")
    if not isinstance(author, str) or not author:
        raise VerifyError(f"{source} message {ident} has no author")
    body = message["body"]
    if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
        raise VerifyError(f"{source} message {ident} has a malformed body")
    refs = message["refs"]
    if not isinstance(refs, (list, tuple)) or any(not isinstance(ref, str) for ref in refs):
        raise VerifyError(f"{source} message {ident} has malformed refs")
    deliver_at = message.get("deliver_at")
    if deliver_at is not None and not isinstance(deliver_at, str):
        raise VerifyError(f"{source} message {ident} has a malformed delivery instant")
    return {"queue": queue, "id": ident, "author": author, "body": dict(body),
            "refs": list(refs), "deliver_at": deliver_at}


def _index(messages, source: str, target: dict) -> None:
    for message in messages:
        record = _record(message, source)
        ident = record["id"]
        if ident in target:
            raise VerifyError(f"Duplicate {source} message id {ident}")
        try:
            text = canonical(record).decode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise VerifyError(f"{source} message {ident} is not encodable JSON") from exc
        target[ident] = (record, text)


def _read_queue(store, name: str) -> list[dict]:
    """Read every due message without touching the stored reader cursor."""
    cap = MAX_PAGES * PAGE
    messages: list[dict] = []
    cursor = 0
    for _ in range(MAX_PAGES):
        page = store.read(name, READER, cursor=cursor, limit=PAGE, now=NOW)
        head = page["head"]
        if type(head) is not int or head < 0:
            raise VerifyError(f"{name} reported a malformed queue head")
        if head > cap:
            raise VerifyError(f"{name} exceeds the {cap}-message page bound")
        rows = page["messages"]
        if not isinstance(rows, list):
            raise VerifyError(f"{name} returned a malformed page")
        messages.extend(rows)
        if page["delayed"] or not page["more"]:
            return messages
        following = page["next"]
        if type(following) is not int or following <= cursor:
            raise VerifyError(f"{name} pagination did not advance")
        cursor = following
    raise VerifyError(f"{name} exceeded {MAX_PAGES} pages")


def _differences(planned: dict, stored: dict) -> list[dict]:
    result = []
    for ident in sorted(set(planned) | set(stored)):
        want, have = planned.get(ident), stored.get(ident)
        queue = (want or have)[0]["queue"]
        if want is None:
            result.append({"kind": "extra", "queue": queue, "id": ident})
        elif have is None:
            result.append({"kind": "missing", "queue": queue, "id": ident})
        elif want[1] != have[1]:
            fields = [field for field in FIELDS
                      if canonical(want[0][field]) != canonical(have[0][field])]
            result.append({"kind": "changed", "queue": queue, "id": ident,
                           "fields": fields})
    return result


def verify(project) -> dict:
    """Compare the current conversion plan with the declared stored queues."""
    plan_messages, _ = convert_legacy._plan(project)
    planned: dict = {}
    _index(plan_messages, "plan", planned)

    store = project.kernel_queues
    stored: dict = {}
    sizes = {}
    for queue in QUEUES:
        messages = _read_queue(store, queue)
        _index(messages, "store", stored)
        sizes[queue] = len(messages)
    counts = Counter(record["queue"] for record, _ in planned.values())
    checked = {queue: {"planned": counts[queue], "stored": sizes[queue]}
               for queue in QUEUES}
    differences = _differences(planned, stored)
    return {"project": project.name, "checked": checked,
            "ok": not differences, "differences": differences}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="Deployment configuration declaring the stage-2 queues")
    parser.add_argument("--project", required=True, help="Managed project name")
    args = parser.parse_args(argv)
    try:
        config = load(Path(args.config))
        for queue in QUEUES:
            if queue not in config.queues:
                raise VerifyError(f"Configuration does not declare the {queue} queue")
        project = Project(config, args.project, read_only=True)
        report = verify(project)
    except (VerifyError, convert_legacy.ConversionError, MizuError, OSError, ValueError) as exc:
        print(f"stage2 verify refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    if not report["ok"]:
        print(f"stage2 verify mismatch: {len(report['differences'])} difference(s)",
              file=sys.stderr)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
