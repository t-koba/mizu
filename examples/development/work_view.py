"""Project declared work messages into one current item view.

Operator-run userland proof, not a product command and not a runtime path::

    python3 examples/development/work_view.py PROJECT [--pages N]

``--pages`` is 1-64 (default 64). Each page is read through the public CLI as
``mizu queue PROJECT read work --host --cursor <seq> --limit 512``. The explicit
cursor pages history, including rows already delivered to the host: reading
records delivery facts, but it does not change work-item authority. A final
page that still reports ``more`` is a refusal, never a partial view.
``MIZU_CONFIG`` and ``PYTHONPATH`` are handled by the CLI helper in
``convert_work.py``.

Every row is checked for kernel shape (integer ``seq``, 32-hex ``id``, object
``body``) and every body is checked against ``queues.toml``
``[queues.work.schema]``, including undeclared keys.

``project(messages)`` is a pure replay of kernel rows in queue order. An
``item`` seeds revision 1; a ``transition`` advances exactly one revision and
replaces the declared field set; a ``wait`` must name the current revision and
adds the parsed wait object under ``wait``; a ``completion`` must name the
current terminal revision. Immutable seed ``origin`` is carried forward, and
declared ``holder`` principals are kept verbatim. Duplicate message ids,
unknown kinds, duplicate seeds, gaps, conflicts and malformed or unbounded
values fail closed. The result is one current view per item, sorted by id,
with the fields of its latest declared body plus ``wait`` when present.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from convert_work import (
    ConversionError, IDENTIFIER, ITEM_ID, bounded, canonical, cli, declaration,
    declared, integer, reject_constant, validate,
)

QUEUE = "work"
PAGE_LIMIT = 512
MAX_PAGES = 64
MESSAGE_ID = re.compile(r"[0-9a-f]{32}\Z")
KINDS = frozenset({"item", "transition", "wait", "completion"})
TERMINAL = frozenset({"done", "dropped"})


class Parser(argparse.ArgumentParser):
    """Turn argument failures into the same bounded exit as replay failures."""

    def error(self, message):
        raise ConversionError("argument error: " + bounded(message))


def parser() -> argparse.ArgumentParser:
    cli_parser = Parser(description=__doc__.splitlines()[0])
    cli_parser.add_argument("project", help="Managed project name")
    cli_parser.add_argument("--pages", type=int, default=None,
                            help="Pages of at most 512 messages (1-64; default: 64)")
    return cli_parser


def check_row(row: object, index: int) -> dict:
    """Check one kernel row's shape before its body is interpreted."""
    if not isinstance(row, dict):
        raise ConversionError(f"queue row {index} is not an object")
    if type(row.get("seq")) is not int or row["seq"] < 1:
        raise ConversionError(f"queue row {index} has an invalid sequence")
    identifier = row.get("id")
    if not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier):
        raise ConversionError(f"queue row {index} has an invalid message id")
    if not isinstance(row.get("body"), dict):
        raise ConversionError(f"queue row {index} has a non-object body")
    return row


def read_page(project: str, cursor: int) -> tuple[list[dict], bool, int]:
    raw = cli(["queue", project, "read", QUEUE, "--host", "--cursor", str(cursor),
               "--limit", str(PAGE_LIMIT)])
    try:
        page = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConversionError("invalid queue page JSON: " + bounded(exc)) from exc
    if not isinstance(page, dict):
        raise ConversionError("queue page is not an object")
    rows, more, following = page.get("messages"), page.get("more"), page.get("next")
    if not isinstance(rows, list) or not isinstance(more, bool):
        raise ConversionError("queue page has no message list or continuation flag")
    if type(following) is not int or following < cursor:
        raise ConversionError("queue page has an invalid continuation cursor")
    return rows, more, following


def read_messages(project: str, pages: int = MAX_PAGES) -> list[dict]:
    """Read bounded pages of history; refuse an unfinished final page."""
    cursor, collected = 0, []
    for _ in range(pages):
        rows, more, following = read_page(project, cursor)
        for index, row in enumerate(rows):
            collected.append(check_row(row, len(collected) + index))
        if not more:
            return collected
        if following <= cursor:
            raise ConversionError("queue page continuation did not advance")
        cursor = following
    raise ConversionError(f"work queue exceeds {pages} pages")


def checked_body(row: dict, schema: dict) -> dict:
    """Validate a declared body; the schema owns every value bound."""
    body = row["body"]
    unknown = set(body) - set(schema)
    if unknown:
        raise ConversionError("undeclared work fields: " + ", ".join(sorted(unknown)))
    for name in sorted(body):
        validate(body[name], declared(schema, name), name)
    if body.get("kind") not in KINDS:
        raise ConversionError("unknown work message kind")
    item = body.get("item")
    if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
        raise ConversionError("malformed work item id")
    if type(body.get("rev")) is not int or body["rev"] < 1:
        raise ConversionError("malformed work message revision")
    if "title" not in body or "status" not in body:
        raise ConversionError("work message lacks title or status")
    return body


def parsed_wait(payload: object, schema: dict) -> dict:
    """Decode a declared wait body into its bounded object."""
    if not isinstance(payload, str):
        raise ConversionError("work wait body is not a string")
    try:
        wait = json.loads(payload, parse_constant=reject_constant)
        size = len(canonical(wait)) - 1  # canonical framing adds one LF
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConversionError("malformed work wait: " + bounded(exc)) from exc
    if not isinstance(wait, dict):
        raise ConversionError("work wait is not an object")
    if size > declared(schema, "body").get("max", 32768):
        raise ConversionError("work wait exceeds its declared bound")
    return wait


def project(messages, schema=None) -> list[dict]:
    """Replay rows into current item views without reading or writing state.

    A supplied schema makes the replay entirely pure; the CLI uses the declared
    default. State changes are keyed by item id, so messages for different
    items may interleave.
    """
    schema = declaration() if schema is None else schema
    states: dict[str, dict] = {}
    seen: set[str] = set()
    for index, raw in enumerate(messages):
        row = check_row(raw, index)
        message_id = row["id"]
        if message_id in seen:
            raise ConversionError("duplicate work message id")
        seen.add(message_id)
        body = checked_body(row, schema)
        kind, item, rev = body["kind"], body["item"], body["rev"]
        current = states.get(item)
        if kind == "item":
            if current is not None:
                raise ConversionError("duplicate work item seed")
            if rev != 1:
                raise ConversionError("work item seed is not at revision 1")
            states[item] = dict(body)
        elif kind == "transition":
            if current is None:
                raise ConversionError("work transition has no seed")
            if rev != current["rev"] + 1:
                raise ConversionError("work transition revision gap")
            replacement = dict(body)
            if "origin" in current:
                replacement["origin"] = current["origin"]
            states[item] = replacement
        elif kind == "wait":
            if current is None:
                raise ConversionError("work wait has no state")
            if rev != current["rev"]:
                raise ConversionError("work wait revision mismatch")
            if body["status"] != current["status"]:
                raise ConversionError("work wait disagrees with the current status")
            updated = dict(current)
            updated["wait"] = parsed_wait(body.get("body"), schema)
            states[item] = updated
        else:
            if current is None:
                raise ConversionError("work completion has no state")
            if rev != current["rev"]:
                raise ConversionError("work completion revision mismatch")
            if body["status"] not in TERMINAL:
                raise ConversionError("work completion status is not terminal")
            if current.get("kind") == "completion":
                raise ConversionError("duplicate work completion")
            updated = dict(current)
            updated["kind"] = kind
            updated["status"] = body["status"]
            for name in ("title", "body", "scope", "reason"):
                if name in body:
                    updated[name] = body[name]
            for name in ("holder", "snapshot", "checks", "proof"):
                if name in body:
                    updated[name] = body[name]
                else:
                    updated.pop(name, None)
            updated.pop("wait", None)
            states[item] = updated
    return [states[item] for item in sorted(states)]


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if not IDENTIFIER.fullmatch(args.project):
            raise ConversionError("invalid project name")
        pages = integer(args.pages if args.pages is not None else MAX_PAGES,
                        1, MAX_PAGES, "--pages")
        schema = declaration()
        items = project(read_messages(args.project, pages), schema)
        print(json.dumps({"project": args.project, "items": items},
                         ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except ConversionError as exc:
        print("work_view: " + bounded(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
