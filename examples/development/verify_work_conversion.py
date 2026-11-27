"""Bounded stage-3 pre-cutover verification gate.

Operator-run userland example, not a product command and not a runtime path::

    python3 examples/development/verify_work_conversion.py --project-root DIR [--pages N]

DIR is the managed project root; its directory name is the project name used
for the public queue CLI. ``--pages`` is 1-64 (default 64). The gate refuses a
cutover unless the declared work queue reproduces the dedicated work-item
store: it reads one bounded inventory with ``mizu work-items PROJECT --limit
100 --closed 100``, requires both pages to be complete, and replays each
stored item's full chain into a current view through the same declared replay
as the queue. It reads the queue once and compares both current views as exact
canonical JSON restricted to the inventoried item ids, also refusing queue
items absent from the store.

Every step is read-only: the gate writes no work items, switches no authority
and adds no runtime migration command. Incomplete pages, duplicate or
malformed ids, a missing chain, a malformed row, a mismatch and any extra item
are bounded refusals with exit 1. Success prints only
``{"project": name, "checked": N, "matched": true}``. ``MIZU_CONFIG`` and
``PYTHONPATH`` are handled by the CLI helper in ``convert_work.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_EXAMPLE_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLE_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLE_DIR)

from convert_work import (  # noqa: E402
    ITEM_ID, IDENTIFIER, ConversionError, bounded, canonical, chain_plan, cli,
    declaration, integer,
)
from work_view import MAX_PAGES, Parser, project, read_messages  # noqa: E402

INVENTORY_LIMIT = 100
INVENTORY_CLOSED = 100
PAGE_SECTIONS = ("open", "closed")


def parser() -> Parser:
    gate = Parser(description=__doc__.splitlines()[0])
    gate.add_argument("--project-root", required=True, metavar="DIR",
                      help="Managed project root; its name is the project name")
    gate.add_argument("--pages", type=int, default=None,
                      help="Queue pages of at most 512 messages (1-64; default: 64)")
    return gate


def checked_page(page: object, section: str) -> list:
    """Require one complete inventory page, using the CLI's own page fields."""
    if not isinstance(page, dict):
        raise ConversionError(f"malformed inventory: {section} page is not an object")
    items = page.get("items")
    if not isinstance(items, list) or len(items) > INVENTORY_LIMIT:
        raise ConversionError(f"malformed inventory: {section} page has no bounded item list")
    if page.get("cursor") is not None:
        raise ConversionError(f"{section} page is incomplete: input cursor")
    if page.get("next_cursor") is not None:
        raise ConversionError(f"{section} page is incomplete: next cursor")
    if type(page.get("overflow_count")) is not int or page["overflow_count"] != 0:
        raise ConversionError(f"{section} page is incomplete: overflow")
    if page.get("truncated") is not False:
        raise ConversionError(f"{section} page is incomplete: truncated")
    total = page.get("total")
    if type(total) is not int or total != len(items):
        raise ConversionError(f"malformed inventory: {section} page total disagrees")
    return items


def inventory_items(inventory: object) -> list[str]:
    """Collect every inventoried item id, refusing malformed or duplicate rows."""
    if not isinstance(inventory, dict):
        raise ConversionError("malformed inventory: not an object")
    ids, seen = [], set()
    for section in PAGE_SECTIONS:
        for row in checked_page(inventory.get(section), section):
            if not isinstance(row, dict):
                raise ConversionError("malformed inventory row: not an object")
            item = row.get("id")
            if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
                raise ConversionError("malformed inventory row: invalid work item id")
            if item in seen:
                raise ConversionError("malformed inventory: duplicate work item")
            seen.add(item)
            ids.append(item)
    return ids


def read_inventory(project: str) -> object:
    raw = cli(["work-items", project, "--limit", str(INVENTORY_LIMIT),
               "--closed", str(INVENTORY_CLOSED)])
    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConversionError("invalid inventory JSON: " + bounded(exc)) from exc


def chain_rows(root: Path, items: list[str], schema: dict) -> list[dict]:
    """Convert every stored chain into kernel rows with deterministic sequences."""
    rows, seen = [], set()
    for item in items:
        if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
            raise ConversionError("malformed inventory row: invalid work item id")
        converted_project, messages = chain_plan(root, item, schema)
        if converted_project != root.name:
            raise ConversionError("chain conversion named a different project")
        if not isinstance(messages, list):
            raise ConversionError("malformed converted chain")
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("body"), dict):
                raise ConversionError("malformed converted chain row")
            identifier = message.get("id")
            if not isinstance(identifier, str) or identifier in seen:
                raise ConversionError("duplicate converted message id")
            seen.add(identifier)
            rows.append({"seq": len(rows) + 1, "id": identifier,
                         "body": message["body"]})
    return rows


def checked_view(view: object) -> list[dict]:
    """Validate the ids of a projected current view before comparing it."""
    if not isinstance(view, list):
        raise ConversionError("malformed item view")
    for row in view:
        if not isinstance(row, dict):
            raise ConversionError("malformed item view row")
        item = row.get("item")
        if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
            raise ConversionError("malformed item view id")
    return view


def restricted(view: list[dict], items: list[str]) -> list[dict]:
    """Canonically sort a view restricted to the inventoried item ids."""
    wanted = set(items)
    return sorted((row for row in checked_view(view) if row["item"] in wanted),
                  key=lambda row: row["item"])


def compare_views(store_view: list[dict], queue_view: list[dict], items: list[str]) -> None:
    """Refuse any extra, missing or inexactly projected queue item."""
    queue_ids = {row["item"] for row in checked_view(queue_view)}
    expected_ids = set(items)
    extra = queue_ids - expected_ids
    if extra:
        raise ConversionError("work queue has items absent from the store inventory: "
                              + ", ".join(sorted(extra)))
    missing = expected_ids - queue_ids
    if missing:
        raise ConversionError("work queue is missing stored work items: "
                              + ", ".join(sorted(missing)))
    if canonical(restricted(store_view, expected_ids)) != canonical(
            restricted(queue_view, expected_ids)):
        raise ConversionError("work queue view does not reproduce the stored chain view")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        root = Path(args.project_root)
        name = root.name
        if not IDENTIFIER.fullmatch(name):
            raise ConversionError("invalid project name")
        pages = integer(args.pages if args.pages is not None else MAX_PAGES,
                        1, MAX_PAGES, "--pages")
        schema = declaration()
        items = inventory_items(read_inventory(name))
        expected = project(chain_rows(root, items, schema), schema)
        actual = project(read_messages(name, pages), schema)
        compare_views(expected, actual, items)
        print(json.dumps({"project": name, "checked": len(items), "matched": True},
                         ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except ConversionError as exc:
        print("verify_work_conversion: " + bounded(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
