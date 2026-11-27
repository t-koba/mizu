"""Convert work items into declared work messages.

Operator-run userland example, not a product command and not a runtime path.
It has two mutually exclusive modes.

Inventory mode (unchanged)::

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
error.

Chain mode converts one item's whole stored revision chain, including its
creation record and a retirement sidecar if one exists::

    python3 examples/development/convert_work.py \\
        --project-root DIR --item wi_<32hex> [--dry-run]

DIR is the managed project's root (``data/projects/<name>`` under the selected
config); its directory name is the project name passed to the public queue CLI.
A creation record becomes a ``kind=item`` message, each numbered revision a
``kind=transition``, every revision that carries a wait also a ``kind=wait``
(the wait object is the message's declared ``body`` string), and each
done/dropped revision plus a retirement sidecar a ``kind=completion``. All
fields with a declared counterpart are carried in full: there are no previews,
no skipped declared fields and no guessed values; a field absent from a source
record stays absent from the message. Values are checked against both the
store's durable shape and the declared schema before any append. Revisions
must be contiguous from 2 to the highest numbered file, with no unknown or
unsafe directory entries, matching the store's fail-closed replay rule. Ids
are SHA-256 over compact JSON ``[chain tag, item id, revision, kind]`` (first
32 hex characters), distinct from inventory ids, so retries are idempotent.
Store bookkeeping with no declared counterpart (timestamps, origin, actor,
delivery and verification proofs) is not renamed or invented here; this is a
message conversion, not an authority migration.

Both modes only read the dedicated work-item store and change no runtime
authority: it stays authoritative until cutover. There is no runtime
dual-write and no product migration command.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

QUEUE = "work"
SCHEMA_FILE = Path(__file__).resolve().parent / "queues.toml"
SOURCE = Path(__file__).resolve().parents[2] / "src"
ID_TAG = "work-item-conversion-v1"
CHAIN_TAG = "work-item-chain-conversion-v1"
ERROR_CHARS = 600

# Store bounds, restated here so this example needs no product imports.
MAX_FILE_BYTES = 131072
MAX_EVENT_BYTES = 131593
MAX_REVISIONS = 256
MAX_WAIT_BYTES = 4096
MAX_DIRECTORY_ENTRIES = MAX_REVISIONS + 3
MAX_TEXT = 4000
MAX_TITLE = 200

ITEM_ID = re.compile(r"wi_[0-9a-f]{32}\Z")
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
PRINCIPAL = re.compile(r"(?:role:(?:\*|[a-z][a-z0-9_-]{0,62})|host|operator)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REVISION_FILE = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\.json\Z")
STATUSES = frozenset({"open", "doing", "blocked", "done", "dropped"})
PREVIEW_KEYS = frozenset({"description_preview", "scope_preview", "reason_preview"})

SEED_KEYS = frozenset({"id", "title", "origin", "status", "rev", "holder",
                       "created_at", "updated_at"})
SEED_OPTIONAL = frozenset({"description", "scope", "smoke_refs"})
DIRECT_KEYS = frozenset({"kind", "actor", "idempotency_key", "work_item"})
IMPORTED_KEYS = frozenset({"kind", "work_item"})
REVISION_KEYS = frozenset({"id", "title", "origin", "status", "rev", "holder",
                           "created_at", "updated_at", "actor", "transition",
                           "reason", "wait", "snapshot_id"})
REVISION_OPTIONAL = frozenset({"description", "scope", "smoke_refs", "verification",
                               "delivered_tree", "delivery_schema", "delivery",
                               "verification_scope", "blocked_since", "wait_resolution",
                               "completion_policy", "completion_evidence", "reopen",
                               "checks"})
RETIREMENT_KEYS = frozenset({"schema", "request", "request_sha256", "chain_sha256",
                             "created_at"})
RETIREMENT_REQUEST_KEYS = frozenset({"id", "expected_rev", "status", "actor", "reason"})


class ConversionError(Exception):
    """A bounded, fail-closed refusal; no event is guessed or partially written."""


def bounded(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS] + "..."


def canonical(value: object) -> bytes:
    """The store's canonical JSON framing, used only for identity bindings."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def reject_constant(value: str):
    raise ValueError("non-finite JSON constant")


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


def _identity(tag: str, item: str, rev: int, kind: str) -> str:
    seed = json.dumps([tag, item, rev, kind], separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def message_id(item: str, rev: int, status: str) -> str:
    return _identity(ID_TAG, item, rev, status)


def chain_message_id(item: str, rev: int, kind: str) -> str:
    return _identity(CHAIN_TAG, item, rev, kind)


def message(row: object, schema: dict) -> dict:
    """Build and validate one declared inventory body; refuse any malformed row."""
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


# ---- Chain mode: bounded, read-only replay of one item's stored records ----

def read_store_file(root: Path, item: str, name: str, maximum: int) -> dict:
    """Read one pinned regular store file, refusing paths, links and oversize."""
    path = root / "work-items" / item / name
    try:
        info = path.lstat()
    except OSError:
        raise ConversionError(f"{name} is missing or unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ConversionError(f"{name} is not a regular store file")
    if info.st_size > maximum:
        raise ConversionError(f"{name} exceeds its byte bound")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ConversionError(f"{name} cannot be opened") from None
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > maximum or after.st_size > maximum or not stat.S_ISREG(after.st_mode):
        raise ConversionError(f"{name} exceeds its byte bound")
    try:
        value = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConversionError(f"{name} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ConversionError(f"{name} is not an object")
    return value


def revision_numbers(root: Path, item: str) -> list[int]:
    """Enumerate only contiguous, bounded, regular numbered revision files."""
    directory = root / "work-items" / item
    try:
        info = directory.lstat()
    except OSError:
        raise ConversionError("work item is missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ConversionError("work item is not a directory")
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        raise ConversionError("work item directory cannot be scanned") from None
    if len(names) > MAX_DIRECTORY_ENTRIES:
        raise ConversionError("work item directory exceeds its entry bound")
    revisions = []
    for name in names:
        if name in ("create.json", "retirement.json"):
            continue
        if REVISION_FILE.fullmatch(name) is None:
            raise ConversionError("unexpected work-item directory entry")
        revision = int(name[:-5])
        if revision > MAX_REVISIONS:
            raise ConversionError("work-item history exceeds its revision bound")
        try:
            info = (directory / name).lstat()
        except OSError:
            raise ConversionError("work-item revision is unreadable") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ConversionError("work-item revision is not a regular file")
        revisions.append(revision)
    if revisions != list(range(2, len(revisions) + 2)):
        raise ConversionError("gapped work-item history")
    return revisions


def text_field(value: object, where: str, *, limit: int = MAX_TEXT) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or not value.strip() or "\x00" in value):
        raise ConversionError(f"{where} is not full bounded text")
    return value


def timestamp(value: object, where: str) -> None:
    if not isinstance(value, str):
        raise ConversionError(f"{where} is not a timestamp")
    try:
        moment = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ConversionError(f"{where} is not a timestamp") from None
    if moment.tzinfo is None:
        raise ConversionError(f"{where} is not an aware timestamp")


def origin_field(value: object) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if not 1 <= len(value) <= 512 or not value.strip() or "\x00" in value:
            raise ConversionError("work-item origin is malformed")
        return
    if isinstance(value, dict):
        try:
            if len(canonical(value)) <= 512:
                return
        except (ValueError, TypeError, RecursionError):
            pass
    raise ConversionError("work-item origin is malformed")


def wait_field(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or not value["kind"].strip():
        raise ConversionError("work-item wait is malformed")
    try:
        size = len(canonical(value))
    except (ValueError, TypeError, RecursionError):
        raise ConversionError("work-item wait is malformed") from None
    if size > MAX_WAIT_BYTES:
        raise ConversionError("work-item wait exceeds its byte bound")


def checks_field(value: object) -> None:
    if (not isinstance(value, list) or len(value) > 64
            or any(not isinstance(entry, str) or len(entry) > 256 for entry in value)):
        raise ConversionError("work-item checks are malformed")


def validate_seed(seed: object, item: str, *, imported: bool) -> dict:
    if not isinstance(seed, dict):
        raise ConversionError("create.json has no work item")
    keys = frozenset(seed)
    allowed = SEED_KEYS | SEED_OPTIONAL | ({"wait"} if imported else set())
    if not SEED_KEYS <= keys or not keys <= allowed or PREVIEW_KEYS & keys:
        raise ConversionError("create.json has an unexpected work-item shape")
    if ("wait" in seed) != imported:
        raise ConversionError("create.json wait does not match its kind")
    if seed["id"] != item or not ITEM_ID.fullmatch(item):
        raise ConversionError("create.json item identity is invalid")
    if type(seed["rev"]) is not int or seed["rev"] != 1:
        raise ConversionError("create.json revision is invalid")
    if seed["status"] != ("blocked" if imported else "open"):
        raise ConversionError("create.json status is invalid")
    if seed["holder"] is not None:
        raise ConversionError("create.json holder must be null")
    if not isinstance(seed["title"], str) or not 1 <= len(seed["title"]) <= MAX_TITLE:
        raise ConversionError("create.json title is invalid")
    if "description" in seed:
        text_field(seed["description"], "create.json description")
    if "scope" in seed:
        text_field(seed["scope"], "create.json scope")
    if "checks" in seed:
        checks_field(seed["checks"])
    if "smoke_refs" in seed and not isinstance(seed["smoke_refs"], list):
        raise ConversionError("create.json smoke_refs are malformed")
    if "wait" in seed:
        wait_field(seed["wait"])
    origin_field(seed["origin"])
    timestamp(seed["created_at"], "create.json created_at")
    if seed["updated_at"] != seed["created_at"]:
        raise ConversionError("create.json timestamps disagree")
    return seed


def validate_creation(creation: dict, item: str) -> dict:
    kind = creation.get("kind")
    keys = frozenset(creation)
    if kind == "direct" and keys not in (DIRECT_KEYS, DIRECT_KEYS | {"requested_smoke_refs"}):
        raise ConversionError("create.json has an unexpected direct shape")
    if kind == "imported" and keys != IMPORTED_KEYS:
        raise ConversionError("create.json has an unexpected imported shape")
    if kind not in ("direct", "imported"):
        raise ConversionError("create.json has an unknown kind")
    if kind == "direct":
        if (not isinstance(creation["actor"], str) or not IDENTIFIER.fullmatch(creation["actor"])
                or not isinstance(creation["idempotency_key"], str)
                or not 1 <= len(creation["idempotency_key"]) <= 128):
            raise ConversionError("create.json request is malformed")
    return validate_seed(creation["work_item"], item, imported=kind == "imported")


def validate_revision(record: dict, previous: dict, item: str, rev: int) -> None:
    keys = frozenset(record)
    if (not REVISION_KEYS <= keys or not keys <= (REVISION_KEYS | REVISION_OPTIONAL)
            or PREVIEW_KEYS & keys):
        raise ConversionError("revision has an unexpected shape")
    if record["id"] != item or type(record["rev"]) is not int or record["rev"] != rev:
        raise ConversionError("revision identity is invalid")
    if record["title"] != previous["title"] or record["created_at"] != previous["created_at"]:
        raise ConversionError("revision changed immutable creation data")
    if "description" in previous and (
            "description" not in record or record["description"] != previous["description"]):
        raise ConversionError("revision changed the immutable description")
    if record["status"] not in STATUSES:
        raise ConversionError("revision status is invalid")
    transition = record["transition"]
    if (not isinstance(transition, dict) or set(transition) != {"from", "to"}
            or transition != {"from": previous["status"], "to": record["status"]}):
        raise ConversionError("revision transition is invalid")
    if not isinstance(record["actor"], str) or not IDENTIFIER.fullmatch(record["actor"]):
        raise ConversionError("revision actor is invalid")
    timestamp(record["updated_at"], "revision updated_at")
    if record["holder"] is not None and (
            not isinstance(record["holder"], str) or not ROLE.fullmatch(record["holder"])):
        raise ConversionError("revision holder is invalid")
    if record["reason"] is not None:
        text_field(record["reason"], "revision reason")
    if record["wait"] is not None:
        wait_field(record["wait"])
    if record["snapshot_id"] is not None and not DIGEST.fullmatch(record["snapshot_id"]):
        raise ConversionError("revision snapshot is invalid")
    if "description" in record:
        text_field(record["description"], "revision description")
    if "scope" in record:
        if record["scope"] is None:
            if "scope" in previous:
                raise ConversionError("revision cleared the accepted scope")
        else:
            text_field(record["scope"], "revision scope")
    elif "scope" in previous and previous["scope"] is not None:
        raise ConversionError("revision cleared the accepted scope")
    if "checks" in record:
        checks_field(record["checks"])
    if "smoke_refs" in record and not isinstance(record["smoke_refs"], list):
        raise ConversionError("revision smoke_refs are malformed")
    if "blocked_since" in record and record["blocked_since"] is not None:
        timestamp(record["blocked_since"], "revision blocked_since")


def validate_retirement(record: object, chain: list[dict], item: str) -> dict:
    if not isinstance(record, dict) or frozenset(record) != RETIREMENT_KEYS:
        raise ConversionError("retirement.json has an unexpected shape")
    request = record["request"]
    if not isinstance(request, dict) or frozenset(request) != RETIREMENT_REQUEST_KEYS:
        raise ConversionError("retirement request has an unexpected shape")
    if type(record["schema"]) is not int or record["schema"] != 1:
        raise ConversionError("retirement schema is invalid")
    if request["id"] != item or type(request["expected_rev"]) is not int:
        raise ConversionError("retirement identity is invalid")
    if request["status"] != "dropped" or not isinstance(request["actor"], str) or not IDENTIFIER.fullmatch(request["actor"]):
        raise ConversionError("retirement request is invalid")
    text_field(request["reason"], "retirement reason")
    if request["reason"].strip() != request["reason"]:
        raise ConversionError("retirement reason is not normalized")
    timestamp(record["created_at"], "retirement created_at")
    if not isinstance(record["request_sha256"], str) or not DIGEST.fullmatch(record["request_sha256"]):
        raise ConversionError("retirement request digest is invalid")
    if not isinstance(record["chain_sha256"], str) or not DIGEST.fullmatch(record["chain_sha256"]):
        raise ConversionError("retirement chain digest is invalid")
    if record["request_sha256"] != hashlib.sha256(canonical(request)).hexdigest():
        raise ConversionError("retirement request digest does not bind")
    chain_digest = hashlib.sha256()
    for entry in chain:
        chain_digest.update(canonical(entry) + b"\n")
    if record["chain_sha256"] != chain_digest.hexdigest():
        raise ConversionError("retirement chain digest does not bind")
    last = chain[-1]
    if request["expected_rev"] != last["rev"] or last["status"] not in ("open", "doing", "blocked"):
        raise ConversionError("retirement does not bind the terminal chain state")
    return request


def load_chain(root: Path, item: str) -> tuple[list[dict], dict | None]:
    creation = read_store_file(root, item, "create.json", MAX_FILE_BYTES)
    seed = validate_creation(creation, item)
    chain = [seed]
    previous = seed
    for rev in revision_numbers(root, item):
        record = read_store_file(root, item, f"{rev}.json", MAX_EVENT_BYTES)
        validate_revision(record, previous, item, rev)
        chain.append(record)
        previous = record
    try:
        (root / "work-items" / item / "retirement.json").lstat()
    except OSError:
        return chain, None
    retirement = read_store_file(root, item, "retirement.json", MAX_FILE_BYTES)
    return chain, validate_retirement(retirement, chain, item)


def chain_body(record: dict, item: str, rev: int, kind: str, schema: dict,
               *, payload: str | None = None, reason: str | None = None,
               status: str | None = None, terminal: bool = False) -> dict:
    """Build one declared body from a stored record, without guessing values."""
    body = {"kind": kind, "item": item, "rev": rev, "title": record["title"]}
    body["status"] = status if status is not None else record["status"]
    if payload is not None:
        body["body"] = payload
    elif "description" in record:
        body["body"] = record["description"]
    if "scope" in record and record["scope"] is not None:
        body["scope"] = record["scope"]
    if reason is not None:
        body["reason"] = reason
    elif record.get("reason") is not None:
        body["reason"] = record["reason"]
    holder = record.get("holder")
    if not terminal and isinstance(holder, str) and ROLE.fullmatch(holder):
        body["holder"] = "role:" + holder
    if record.get("snapshot_id") is not None:
        body["snapshot"] = record["snapshot_id"]
    if "checks" in record and record["checks"] is not None:
        body["checks"] = record["checks"]

    unknown = set(body) - set(schema)
    if unknown:
        raise ConversionError("undeclared work fields: " + ", ".join(sorted(unknown)))
    for name, value in body.items():
        validate(value, declared(schema, name), name)
    return body


def chain_plan(root: Path, item: str, schema: dict) -> tuple[str, list[dict]]:
    if not isinstance(item, str) or not ITEM_ID.fullmatch(item):
        raise ConversionError("--item must be wi_ plus 32 lowercase hex characters")
    if not isinstance(root, Path) or not root.is_dir() or root.is_symlink():
        raise ConversionError("--project-root is not a directory")
    project = root.name
    if not IDENTIFIER.fullmatch(project):
        raise ConversionError("--project-root name is not a project name")
    chain, retirement = load_chain(root, item)
    messages = []

    def append(record: dict, kind: str, **kwargs) -> None:
        rev = kwargs.pop("rev", record["rev"])
        body = chain_body(record, item, rev, kind, schema, **kwargs)
        messages.append({"id": chain_message_id(item, rev, kind), "body": body})

    for index, record in enumerate(chain):
        append(record, "item" if index == 0 else "transition")
        wait = record.get("wait")
        if wait is not None:
            try:
                payload = json.dumps(wait, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                raise ConversionError("work-item wait cannot be encoded") from None
            append(record, "wait", payload=payload)
        if record["status"] in ("done", "dropped"):
            append(record, "completion")
    if retirement is not None:
        append(chain[-1], "completion", rev=retirement["expected_rev"],
               reason=retirement["reason"], status="dropped", terminal=True)
    if len({row["id"] for row in messages}) != len(messages):
        raise ConversionError("chain conversion produced duplicate message ids")
    return project, messages


def integer(value: object, low: int, high: int, where: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConversionError(f"{where} must be an integer from {low} to {high}")
    return value


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cli.add_argument("project", nargs="?", help="Managed project name (inventory mode)")
    cli.add_argument("--limit", type=int, default=None, help="Open page size (1-100; default: 25)")
    cli.add_argument("--closed", type=int, default=None, help="Newest closed items (0-100; default: 10)")
    cli.add_argument("--dry-run", action="store_true", help="Print planned bodies without appending")
    cli.add_argument("--project-root", metavar="DIR", help="Chain mode: managed project root")
    cli.add_argument("--item", metavar="WI", help="Chain mode: one wi_<32hex> item")
    return cli


VALUE_OPTIONS = frozenset({"--limit", "--closed", "--project-root", "--item"})


def mixed_options(raw: list[str]) -> bool:
    """Catch a positional project supplied beside chain options before argparse."""
    chain = any(token == "--item" or token.startswith("--item=")
                or token == "--project-root" or token.startswith("--project-root=")
                for token in raw)
    if not chain:
        return False
    expect_value = False
    for token in raw:
        if expect_value:
            expect_value = False
        elif token in VALUE_OPTIONS:
            expect_value = True
        elif not token.startswith("-"):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if mixed_options(raw):
        print("convert_work: chain options cannot be mixed with inventory options",
              file=sys.stderr)
        return 1
    args = parser().parse_args(argv)
    try:
        schema = declaration()
        chain_mode = args.project_root is not None or args.item is not None
        if chain_mode:
            if args.project is not None or args.limit is not None or args.closed is not None:
                raise ConversionError("chain options cannot be mixed with inventory options")
            if args.project_root is None or args.item is None:
                raise ConversionError("--project-root and --item must be given together")
            project, messages = chain_plan(Path(args.project_root), args.item, schema)
        else:
            if args.project is None:
                raise ConversionError("a project name or --project-root is required")
            limit = integer(args.limit if args.limit is not None else 25, 1, 100, "--limit")
            closed = integer(args.closed if args.closed is not None else 10, 0, 100, "--closed")
            raw = cli(["work-items", args.project, "--limit", str(limit),
                       "--closed", str(closed)])
            try:
                inventory = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ConversionError("invalid inventory JSON: " + bounded(exc)) from exc
            project, messages = args.project, planned(inventory, schema)
        if args.dry_run:
            for current in messages:
                print(json.dumps(current, ensure_ascii=False, sort_keys=True))
            return 0
        for current in messages:
            cli(["queue", project, "write", QUEUE, "--body", "-",
                 "--id", current["id"], "--host"],
                stdin=json.dumps(current["body"], ensure_ascii=False, sort_keys=True))
        print(json.dumps({"project": project, "appended": len(messages)},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except ConversionError as exc:
        print("convert_work: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
