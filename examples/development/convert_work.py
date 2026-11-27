"""Convert one bounded work-item inventory snapshot into declared work messages.

Operator-run userland example, not a product command and not a runtime path.
The inventory exposes bounded previews rather than full descriptions, scopes
or reasons, so the emitted messages are a bounded projection, not a lossless
conversion and not sufficient authority for a cutover.

Invocation::

    python3 examples/development/convert_work.py PROJECT \\
        [--limit N] [--closed N] [--dry-run]

``--limit`` bounds the open page (1-100, default 25) and ``--closed`` the
newest closed page (0-100, default 10). ``MIZU_CONFIG`` selects a non-default
private config; otherwise the CLI's platform default applies.

The inventory is read through ``python3 -m mizu work-items`` and each current
row is appended through ``python3 -m mizu queue PROJECT write work --body -``
with a caller id and the host principal. The message id is the first 32 hex
characters of SHA-256 over compact JSON ``[tag, item id, rev, status]``, so a
retry with an unchanged row is idempotent under the kernel's caller-id rule.

Only keys declared in ``queues.toml`` ``[queues.work.schema]`` are emitted. The
inventory exposes bounded ``description_preview``, ``scope_preview`` and
``reason_preview`` values rather than full text; those previews are carried in
``body``/``scope``/``reason`` when present, with no undeclared truncation flags.
It exposes no ``checks``, so that declared field is omitted. A work-item holder
is a role name in the store and becomes a ``role:`` principal in the message.
Closed rows carry ``snapshot_id``, mapped to the declared ``snapshot`` digest;
open rows do not expose one. Every row is validated before the first append;
a malformed row, invalid JSON or a CLI failure stops the run with a bounded
error. The dedicated work-item store is read-only here: no dual-write, no
authority change, and the runtime still treats it as authoritative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

QUEUE = "work"
SCHEMA_FILE = Path(__file__).resolve().parent / "queues.toml"
SOURCE = Path(__file__).resolve().parents[2] / "src"
ID_TAG = "work-item-conversion-v1"
ERROR_CHARS = 600

ITEM_ID = re.compile(r"wi_[0-9a-f]{32}\Z")
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
PRINCIPAL = re.compile(r"(?:role:(?:\*|[a-z][a-z0-9_-]{0,62})|host|operator)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ConversionError(Exception):
    """A bounded, fail-closed refusal; no row is guessed or partially written."""


def bounded(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS] + "..."


def cli(argv: list[str], *, stdin: str | None = None) -> str:
    """Invoke the public CLI, returning stdout or refusing with bounded detail."""
    command = [sys.executable, "-m", "mizu"]
    config = os.environ.get("MIZU_CONFIG")
    if config:
        command += ["--config", config]
    command += argv
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE), env.get("PYTHONPATH", "")) if part)
    try:
        result = subprocess.run(command, input=stdin, capture_output=True,
                                text=True, env=env, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConversionError("CLI invocation failed: " + bounded(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise ConversionError("CLI failed: " + bounded(detail))
    return result.stdout


def declaration() -> dict:
    try:
        raw = tomllib.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        schema = raw["queues"][QUEUE]["schema"]
        if not isinstance(schema, dict):
            raise ConversionError("work queue schema is not a table")
        return schema
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConversionError("cannot read work queue schema: " + bounded(exc)) from exc


def validate(value: object, spec: dict, where: str) -> None:
    """Enforce the declared shape of one emitted field, using its own bounds."""
    if value is None:
        if spec.get("required"):
            raise ConversionError(f"{where} is required")
        return
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str) or len(value) > spec.get("max", 1 << 20):
            raise ConversionError(f"{where} exceeds its declared string bound")
        if spec.get("values") and value not in spec["values"]:
            raise ConversionError(f"{where} has a value outside the declaration")
    elif kind == "integer":
        if (type(value) is not int or (spec.get("min") is not None and value < spec["min"])
                or (spec.get("max") is not None and value > spec["max"])):
            raise ConversionError(f"{where} is outside its declared integer bound")
    elif kind == "digest":
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            raise ConversionError(f"{where} is not a declared digest")
    elif kind == "principal":
        if not isinstance(value, str) or not PRINCIPAL.fullmatch(value):
            raise ConversionError(f"{where} is not a declared principal")
    elif kind == "list":
        item_spec = spec.get("item")
        if not isinstance(value, list) or len(value) > spec.get("max_items", 0) or not isinstance(item_spec, dict):
            raise ConversionError(f"{where} exceeds its declared list bound")
        for index, entry in enumerate(value):
            validate(entry, item_spec, f"{where}[{index}]")
    else:
        raise ConversionError(f"{where} has an unsupported declared type")


def declared(schema: dict, name: str) -> dict:
    spec = schema.get(name)
    if not isinstance(spec, dict):
        raise ConversionError(f"{name} is not declared in the work queue")
    return spec


def preview(row: dict, fields: tuple[str, ...], dest: str, body: dict, schema: dict) -> None:
    for key in fields:
        if key in row:
            value = row[key]
            if value is None:
                return
            if not isinstance(value, str):
                raise ConversionError(f"malformed inventory row: {key} is not a string")
            validate(value, declared(schema, dest), dest)
            body[dest] = value
            return


def holder_of(row: dict, schema: dict) -> str | None:
    if "holder" not in row or row["holder"] is None:
        return None
    value = row["holder"]
    if not isinstance(value, str):
        raise ConversionError("malformed inventory row: holder is not a string")
    if PRINCIPAL.fullmatch(value):
        principal = value
    elif ROLE.fullmatch(value):
        principal = "role:" + value
    else:
        raise ConversionError("malformed inventory row: unrecognised holder")
    validate(principal, declared(schema, "holder"), "holder")
    return principal


def message_id(item: str, rev: int, status: str) -> str:
    seed = json.dumps([ID_TAG, item, rev, status], separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def message(row: object, schema: dict) -> dict:
    """Build and validate one declared body; refuse any malformed row."""
    if not isinstance(row, dict):
        raise ConversionError("malformed inventory row: not an object")
    item, rev = row.get("id"), row.get("rev")
    status, title = row.get("status"), row.get("title")
    if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
        raise ConversionError("malformed inventory row: invalid item id")
    if type(rev) is not int or rev < 1:
        raise ConversionError("malformed inventory row: invalid revision")
    if not isinstance(title, str):
        raise ConversionError("malformed inventory row: title is not a string")
    if not isinstance(status, str):
        raise ConversionError("malformed inventory row: status is not a string")

    body = {"kind": "item", "item": item, "rev": rev, "title": title, "status": status}
    preview(row, ("description", "description_preview"), "body", body, schema)
    preview(row, ("scope", "scope_preview"), "scope", body, schema)
    preview(row, ("reason", "reason_preview"), "reason", body, schema)
    holder = holder_of(row, schema)
    if holder is not None:
        body["holder"] = holder
    if row.get("snapshot_id") is not None:
        snapshot = row["snapshot_id"]
        validate(snapshot, declared(schema, "snapshot"), "snapshot")
        body["snapshot"] = snapshot
    if "checks" in row:
        validate(row["checks"], declared(schema, "checks"), "checks")
        body["checks"] = row["checks"]

    unknown = set(body) - set(schema)
    if unknown:
        raise ConversionError("undeclared work fields: " + ", ".join(sorted(unknown)))
    for name, value in body.items():
        validate(value, schema[name], name)
    return {"id": message_id(item, rev, status), "body": body}


def planned(inventory: object, schema: dict) -> list[dict]:
    if not isinstance(inventory, dict):
        raise ConversionError("malformed inventory: not an object")
    result, seen = [], set()
    for section in ("open", "closed"):
        page = inventory.get(section)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ConversionError(f"malformed inventory: {section} page has no item list")
        for row in page["items"]:
            current = message(row, schema)
            item = current["body"]["item"]
            if item in seen:
                raise ConversionError("malformed inventory: duplicate work item")
            seen.add(item)
            result.append(current)
    return result


def integer(value: object, low: int, high: int, where: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConversionError(f"{where} must be an integer from {low} to {high}")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cli.add_argument("project", help="Managed project name")
    cli.add_argument("--limit", type=int, default=25, help="Open page size (1-100; default: 25)")
    cli.add_argument("--closed", type=int, default=10, help="Newest closed items (0-100; default: 10)")
    cli.add_argument("--dry-run", action="store_true", help="Print planned bodies without appending")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        limit = integer(args.limit, 1, 100, "--limit")
        closed = integer(args.closed, 0, 100, "--closed")
        schema = declaration()
        raw = cli(["work-items", args.project, "--limit", str(limit),
                   "--closed", str(closed)])
        try:
            inventory = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ConversionError("invalid inventory JSON: " + bounded(exc)) from exc
        messages = planned(inventory, schema)
        if args.dry_run:
            for current in messages:
                print(json.dumps(current, ensure_ascii=False, sort_keys=True))
            return 0
        for current in messages:
            cli(["queue", args.project, "write", QUEUE, "--body", "-",
                 "--id", current["id"], "--host"],
                stdin=json.dumps(current["body"], ensure_ascii=False, sort_keys=True))
        print(json.dumps({"project": args.project, "appended": len(messages)},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except ConversionError as exc:
        print("convert_work: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
