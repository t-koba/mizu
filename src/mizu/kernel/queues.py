"""Declared message queues: authenticated append, cursors, refs and retention.

Layout under the project root:
  queues/<queue>/messages/<seq:020d>.json  immutable messages, create-only
  queues/<queue>/head.json                 last assigned sequence (derived)
  queues/<queue>/cursors/<principal>.json  one durable cursor per reader principal
  queues/<queue>/woken/<principal>.json    last sequence that has already woken the principal
  queues/<queue>/retain.json               first sequence not yet considered for removal
  queues/ids/<id>.json                     global id -> (queue, seq), kept as a tombstone
  queues/refs/<id>/to.json                 bounded pointers to messages referring to <id>
  queues/intent.json                       the one in-flight append (crash recovery)
  locks/queues.lock                        serialises writes
  wake/<role>                              touched on a due append to a waking queue

Trust: queue declarations are operator configuration; writers supply only a
body and are bound to an authenticated principal by the caller. Validation is
fail-closed and carries no queue name, role name or message meaning. Message
files are create-only and complete; the queue lock serialises writes and the
bounded retention that appends and reads may run, completing an interrupted
append before starting another operation.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import re
import secrets
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from ..errors import ConfigError, Denied
from ..fs import atomic_write, canonical, identifier, loads_finite, lock, mkdir, now as clock, safe_read

#: Mechanism bounds: one MiB body (docs/kernel.md), pages of 64 capped at 512,
#: bounded reference indexes and retention work per append/read.
MAX_BODY_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024 + 65536
PAGE_MAX = 512
PAGE_DEFAULT = 64
MAX_REFS = 512
MAX_RETENTION_SCAN = 256
DEFAULT_RETAIN_DAYS = 30
INTEGER_LIMIT = (1 << 63) - 1
#: Queue and field names: lowercase identifiers.
NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")

#: Queue principals include the role wildcard; an actor is always concrete.
PRINCIPAL = re.compile(r"(?:role:(?:\*|[a-z][a-z0-9_-]{0,62})|host|operator)\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TYPES = ("string", "integer", "boolean", "instant", "digest", "id", "principal", "list")
_FIELD_KEYS = frozenset({"type", "required", "max", "min", "values", "item", "max_items"})


def _keys(table: Any, allowed: frozenset[str], where: str) -> None:
    if not isinstance(table, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in {where}: {', '.join(sorted(unknown))}")


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ConfigError(f"{where} must be a lowercase identifier")
    return value


def _principals(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list of principals")
    result = []
    for item in value:
        if not isinstance(item, str) or not PRINCIPAL.fullmatch(item):
            raise ConfigError(f"{where}: invalid principal {item!r}")
        result.append(item)
    if len(set(result)) != len(result):
        raise ConfigError(f"{where} has duplicates")
    return tuple(result)


def _bound(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}")
    return value


@dataclasses.dataclass(frozen=True)
class QueueField:
    name: str
    type: str
    required: bool = False
    max: int | None = None
    min: int | None = None
    values: tuple[str, ...] = ()
    item: "QueueField | None" = None
    max_items: int = 0

    def validate(self, value: Any) -> Any:
        """Return the value if it conforms to the declaration, else refuse it."""
        where = f"field {self.name}"
        if value is None:
            if self.required:
                raise Denied(f"{where} is required")
            return None
        if self.type == "string":
            if not isinstance(value, str) or len(value) > (self.max or MAX_BODY_BYTES):
                raise Denied(f"{where} exceeds its declared string bound")
            if self.values and value not in self.values:
                raise Denied(f"{where} must be one of {', '.join(self.values)}")
        elif self.type == "integer":
            if (type(value) is not int or (self.min is not None and value < self.min)
                    or (self.max is not None and value > self.max)):
                raise Denied(f"{where} must be an integer within its declared bounds")
        elif self.type == "boolean":
            if type(value) is not bool:
                raise Denied(f"{where} must be a boolean")
        elif self.type == "instant":
            _instant(value, where)
        elif self.type == "digest":
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise Denied(f"{where} must be 64 lowercase hex characters")
        elif self.type == "id":
            _id(value, where)
        elif self.type == "principal":
            if not isinstance(value, str) or not PRINCIPAL.fullmatch(value):
                raise Denied(f"{where} must be a principal")
        elif self.type == "list":
            if not isinstance(value, list) or len(value) > self.max_items:
                raise Denied(f"{where} must be a list of at most {self.max_items} items")
            assert self.item is not None
            return [self.item.validate(item) for item in value]
        return value


@dataclasses.dataclass(frozen=True)
class Queue:
    name: str
    write: tuple[str, ...]
    read: tuple[str, ...]
    wake: tuple[str, ...] = ()
    retain_days: int = DEFAULT_RETAIN_DAYS
    fields: Mapping[str, QueueField] = dataclasses.field(default_factory=dict)


def _field(name: str, raw: Any, where: str, *, nested: bool = False) -> QueueField:
    _keys(raw, _FIELD_KEYS, where)
    kind = raw.get("type")
    if kind not in _TYPES or (nested and kind == "list"):
        raise ConfigError(f"{where}.type must be one of {', '.join(_TYPES)}")
    required = raw.get("required", False)
    if type(required) is not bool:
        raise ConfigError(f"{where}.required must be a boolean")
    spec: dict[str, Any] = {"name": name, "type": kind, "required": required}
    if kind == "list":
        if "item" not in raw or "max_items" not in raw:
            raise ConfigError(f"{where} needs item and max_items")
        spec["item"] = _field(name, raw["item"], f"{where}.item", nested=True)
        spec["max_items"] = _bound(raw["max_items"], f"{where}.max_items", 1, MAX_REFS)
    elif {"item", "max_items"} & set(raw):
        raise ConfigError(f"{where}: item and max_items apply to lists only")
    if "max" in raw:
        if kind not in ("string", "integer"):
            raise ConfigError(f"{where}.max applies to strings and integers only")
        spec["max"] = _bound(raw["max"], f"{where}.max", 1,
                             MAX_BODY_BYTES if kind == "string" else INTEGER_LIMIT)
    if "min" in raw:
        if kind != "integer":
            raise ConfigError(f"{where}.min applies to integers only")
        spec["min"] = _bound(raw["min"], f"{where}.min", -INTEGER_LIMIT, INTEGER_LIMIT)
    if "values" in raw:
        if kind != "string":
            raise ConfigError(f"{where}.values applies to strings only")
        values = raw["values"]
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v or "\x00" in v for v in values)
                or len(set(values)) != len(values)):
            raise ConfigError(f"{where}.values must be a non-empty list of distinct strings")
        spec["values"] = tuple(values)
    return QueueField(**spec)


def parse_queues(raw: Any) -> Mapping[str, Queue]:
    """Validate deployment-declared queues; refuse the whole set on any error."""
    if not isinstance(raw, Mapping):
        raise ConfigError("queues must be a table")
    result = {}
    for name, spec in raw.items():
        where = f"queues.{name}"
        _name(name, where)
        _keys(spec, frozenset({"write", "read", "wake", "retain_days", "schema"}), where)
        write = _principals(spec.get("write"), f"{where}.write")
        read = _principals(spec.get("read"), f"{where}.read")
        wake = _principals(spec["wake"], f"{where}.wake") if "wake" in spec else ()
        # Wake files are named for one concrete role. A wildcard or a
        # non-role principal cannot name one, so refuse it rather than
        # silently never waking anybody.
        for principal in wake:
            if (principal == "role:*" or not principal.startswith("role:")
                    or not allows(read, principal)):
                raise ConfigError(f"{where}.wake must name concrete reader roles")
        days = spec.get("retain_days", DEFAULT_RETAIN_DAYS)
        if type(days) is not int or days < 1:
            raise ConfigError(f"{where}.retain_days must be a positive integer")
        try:
            dt.timedelta(days=days)
        except OverflowError:
            raise ConfigError(f"{where}.retain_days is out of range") from None
        schema = spec.get("schema", {})
        if not isinstance(schema, Mapping):
            raise ConfigError(f"{where}.schema must be a table")
        fields = {_name(_key, f"{where}.schema"): _field(_key, value, f"{where}.schema.{_key}")
                  for _key, value in schema.items()}
        result[name] = Queue(name, write, read, wake, days, MappingProxyType(fields))
    return MappingProxyType(result)


def allows(principals: tuple[str, ...], actor: str) -> bool:
    """Whether a principal tuple grants access to one concrete authenticated actor."""
    return actor in principals or ("role:*" in principals and actor.startswith("role:"))


def _instant(value: Any, where: str) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise Denied(f"{where} must be an aware ISO time")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise Denied(f"{where} must be an aware ISO time")
    return parsed


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise Denied(f"{where} must be a 32-hex id")
    return value


class QueueStore:
    def __init__(self, root: Path, queues: Mapping[str, Queue]):
        self.root = Path(root)
        self.queues = dict(queues)

    # -- helpers -----------------------------------------------------------------------------
    def _decl(self, queue: Any) -> Queue:
        if not isinstance(queue, str) or queue not in self.queues:
            raise Denied(f"Undeclared queue {queue!r}")
        return self.queues[queue]

    @staticmethod
    def _actor(actor: Any) -> str:
        if (not isinstance(actor, str) or not PRINCIPAL.fullmatch(actor)
                or actor == "role:*"):
            raise Denied("Invalid actor principal")
        return actor

    def _grant(self, queue: Any, actor: Any, *, write: bool) -> Queue:
        declaration, actor = self._decl(queue), self._actor(actor)
        principals = declaration.write if write else declaration.read
        if not allows(principals, actor):
            raise Denied(f"{actor} may not {'write to' if write else 'read'} {declaration.name}")
        return declaration

    def _relative(self, *parts: str) -> str:
        return "/".join(("queues", *parts))

    def _body(self, declaration: Queue, body: Any) -> dict:
        if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
            raise Denied("body must be a JSON object")
        try:
            if len(canonical(body)) > MAX_BODY_BYTES:
                raise Denied("body exceeds its size bound")
        except (TypeError, ValueError, RecursionError) as exc:
            raise Denied("body must be a JSON object") from exc
        if not declaration.fields:
            return dict(body)
        unknown = set(body) - set(declaration.fields)
        if unknown:
            raise Denied(f"Undeclared fields in body: {', '.join(sorted(unknown))}")
        values = {}
        for name, field in declaration.fields.items():
            if name in body:
                values[name] = field.validate(body[name])
            elif field.required:
                raise Denied(f"field {name} is required")
        return values

    def _refs(self, refs: Any) -> tuple[str, ...]:
        if refs is None:
            return ()
        if not isinstance(refs, (list, tuple)) or len(refs) > MAX_REFS:
            raise Denied("refs must be a bounded list of message ids")
        return tuple(_id(ref, "reference") for ref in refs)

    def _check_refs(self, refs: tuple[str, ...], actor: str) -> None:
        for ref in refs:
            target = self._message_by_id(ref)
            if target is None or not allows(self._decl(target["queue"]).read, actor):
                raise Denied(f"Reference {ref} is not a message the actor may read")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        mkdir(self.root / "locks")
        with lock(self.root / "locks" / "queues.lock"):
            self._recover()
            yield

    # -- writing -----------------------------------------------------------------------------
    def _stored_head(self, queue: str) -> int:
        stored = self._optional_json(self._relative(queue, "head.json"))
        value = stored.get("seq") if isinstance(stored, Mapping) else None
        return value if type(value) is int and value >= 0 else 0

    def _head(self, queue: str) -> int:
        seq = self._stored_head(queue)
        # The head is derived: a crash after a message file and before the head
        # moves forward must not open a gap in the per-queue sequence.
        while (self.root / self._relative(queue, "messages", f"{seq + 1:020d}.json")).exists():
            seq += 1
        return seq

    def _read_json(self, name: str) -> Any:
        return loads_finite(safe_read(self.root, name, MAX_MESSAGE_BYTES))

    def _optional_json(self, name: str) -> Any | None:
        """Absent files (including absent parents) are normal; unsafe ones are not."""
        path = self.root / name
        if not path.exists():
            return None
        try:
            return self._read_json(name)
        except Denied:
            if path.exists():
                raise
            return None

    def _message(self, queue: str, seq: int) -> dict | None:
        message = self._optional_json(self._relative(queue, "messages", f"{seq:020d}.json"))
        if message is None:
            return None
        if (not isinstance(message, Mapping) or message.get("queue") != queue
                or message.get("seq") != seq):
            raise Denied("Corrupt queue message")
        return dict(message)

    def _id_pointer(self, message_id: str) -> dict | None:
        message_id = _id(message_id, "message id")
        pointer = self._optional_json(self._relative("ids", f"{message_id}.json"))
        if pointer is None:
            return None
        if not isinstance(pointer, Mapping):
            raise Denied("Corrupt message id index")
        queue, seq = pointer.get("queue"), pointer.get("seq")
        if (not isinstance(queue, str) or queue not in self.queues
                or type(seq) is not int or seq < 1):
            raise Denied("Corrupt message id index")
        return dict(pointer)

    def _message_by_id(self, message_id: str) -> dict | None:
        pointer = self._id_pointer(message_id)
        if pointer is None:
            return None
        return self._message(pointer["queue"], pointer["seq"])

    def _replies_to(self, message: Mapping, actor: str) -> bool:
        """Whether ``message`` refers to a message ``actor`` wrote (a reply in its thread)."""
        for ref in message.get("refs", ()):
            try:
                target = self._message_by_id(ref)
            except Denied:
                continue
            if target is not None and target.get("author") == actor:
                return True
        return False

    def _wakes(self, declaration: Queue, message: Mapping, actor: str) -> bool:
        """Whether a due ``message`` wakes ``actor``: a queue wake or a reply to its message.

        A reader is not woken by its own message unless it is delayed, which is
        a wake its author asked for.
        """
        own = message.get("author") == actor
        if actor in declaration.wake and (not own or message.get("deliver_at") is not None):
            return True
        return not own and self._replies_to(message, actor)

    def _refs_to(self, message_id: str) -> list[dict]:
        entries = self._optional_json(self._relative("refs", message_id, "to.json"))
        if entries is None:
            return []
        if not isinstance(entries, list) or len(entries) > MAX_REFS:
            raise Denied("Corrupt reference index")
        return entries

    def _add_ref(self, target: str, message: dict) -> None:
        entries = self._refs_to(target)
        if any(entry.get("id") == message["id"] for entry in entries if isinstance(entry, dict)):
            return
        if len(entries) >= MAX_REFS:
            raise Denied("Reference index is full")
        entries.append({"id": message["id"], "queue": message["queue"], "seq": message["seq"]})
        path = self.root / self._relative("refs", target, "to.json")
        atomic_write(path, canonical(entries))

    def _prepare_publish(self, message: Mapping) -> None:
        """Refuse deterministic publish failures before recording an intent."""
        for ref in message["refs"]:
            entries = self._refs_to(ref)
            if (len(entries) >= MAX_REFS
                    and not any(isinstance(entry, Mapping) and entry.get("id") == message["id"]
                                for entry in entries)):
                raise Denied("Reference index is full")

    def _due(self, message: Mapping, stamp: dt.datetime) -> bool:
        value = message.get("deliver_at")
        return value is None or _instant(value, "deliver_at") <= stamp

    def _publish(self, message: dict, stamp: dt.datetime) -> None:
        queue, seq, message_id = message["queue"], message["seq"], message["id"]
        path = self.root / self._relative(queue, "messages", f"{seq:020d}.json")
        atomic_write(path, canonical(message), exclusive=True)
        for ref in message["refs"]:
            self._add_ref(ref, message)
        atomic_write(self.root / self._relative("ids", f"{message_id}.json"),
                     canonical({"queue": queue, "seq": seq}), exclusive=True)
        if seq > self._stored_head(queue):
            atomic_write(self.root / self._relative(queue, "head.json"), canonical({"seq": seq}))
        declaration = self.queues.get(queue)
        if self._due(message, stamp) and declaration is not None:
            woken = [principal for principal in declaration.wake
                     if self._wakes(declaration, message, principal)]
            for ref in message["refs"]:
                target = self._message_by_id(ref)
                author = target.get("author") if target else None
                if (isinstance(author, str) and author.startswith("role:") and author not in woken
                        and allows(declaration.read, author)
                        and self._wakes(declaration, message, author)):
                    woken.append(author)
            for principal in woken:
                role = identifier(principal[5:])
                wake = self.root / "wake" / role
                mkdir(wake.parent)
                atomic_write(wake, canonical({"at": clock()}))

    def _recover(self) -> None:
        path = self.root / self._relative("intent.json")
        if not path.exists():
            return
        intent = self._read_json(self._relative("intent.json"))
        if not isinstance(intent, Mapping) or not isinstance(intent.get("message"), Mapping):
            raise Denied("Corrupt queue append intent")
        try:
            self._prepare_publish(intent["message"])
            self._publish(dict(intent["message"]), _instant(clock(), "now"))
        except Denied:
            # Denied is deterministic: a message that cannot be published must
            # not wedge every later operation on replay. Transient I/O failures
            # are not Denied and keep the intent for true crash recovery.
            path.unlink(missing_ok=True)
            raise
        path.unlink()

    def append(self, queue: str, body: Mapping, actor: str, *, refs=None, deliver_at=None,
               id=None, now=None) -> dict:
        declaration = self._grant(queue, actor, write=True)
        values = self._body(declaration, body)
        ref_ids = self._refs(refs)
        deliver = None if deliver_at is None else _instant(deliver_at, "deliver_at")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        if id is not None:
            _id(id, "id")
        with self._locked():
            self._check_refs(ref_ids, actor)
            if id is not None:
                pointer = self._id_pointer(id)
                existing = self._message_by_id(id) if pointer is not None else None
                if pointer is not None and existing is None:
                    raise Denied(f"Message id {id} was removed and cannot be reused")
                if existing is not None:
                    same = (existing["queue"] == declaration.name
                            and existing["author"] == actor and existing["body"] == values
                            and existing["refs"] == list(ref_ids)
                            and existing["deliver_at"] == (None if deliver is None else deliver_at))
                    if not same:
                        raise Denied(f"Message id {id} already carries different content")
                    return existing
            message_id = id or secrets.token_hex(16)
            while self._id_pointer(message_id) is not None:
                if id is not None:
                    raise Denied(f"Message id {id} already exists")
                message_id = secrets.token_hex(16)
            seq = self._head(declaration.name) + 1
            message = {"queue": declaration.name, "seq": seq, "id": message_id,
                       "author": actor, "at": stamp.isoformat(timespec="seconds"),
                       "deliver_at": None if deliver is None else deliver_at,
                       "refs": list(ref_ids), "body": values}
            self._prepare_publish(message)
            atomic_write(self.root / self._relative("intent.json"), canonical({"message": message}))
            self._recover()
            self._retain(declaration.name, declaration, stamp)
            return message

    # -- reading -----------------------------------------------------------------------------
    def _cursor_path(self, queue: str, actor: str) -> Path:
        return self.root / self._relative(queue, "cursors", actor.replace(":", "_") + ".json")

    def _cursor(self, queue: str, actor: str) -> int:
        stored = self._optional_json(self._relative(queue, "cursors", actor.replace(":", "_") + ".json"))
        value = stored.get("seq") if isinstance(stored, Mapping) else None
        return value if type(value) is int and value >= 0 else 0  # corrupt cursors license no removal

    def _woken_path(self, queue: str, actor: str) -> Path:
        return self.root / self._relative(queue, "woken", actor.replace(":", "_") + ".json")

    def _woken(self, queue: str, actor: str) -> int:
        stored = self._optional_json(self._relative(queue, "woken", actor.replace(":", "_") + ".json"))
        value = stored.get("seq") if isinstance(stored, Mapping) else None
        return value if type(value) is int and value >= 0 else 0

    def mark_woken(self, queue: str, actor: str, seq: int) -> int:
        """Record that messages through ``seq`` have woken ``actor``; monotonic.

        A message wakes each reader once, when it is due. Later wakes need a
        later message; the unacknowledged ones stay pending and readable.
        """
        declaration = self._grant(queue, actor, write=False)
        if type(seq) is not int or seq < 0:
            raise Denied("woken sequence must be nonnegative")
        with self._locked():
            seq = min(seq, self._head(declaration.name))
            current = self._woken(declaration.name, actor)
            if seq > current:
                atomic_write(self._woken_path(declaration.name, actor), canonical({"seq": seq}))
                return seq
            return current

    def read(self, queue: str, actor: str, *, cursor=None, limit=PAGE_DEFAULT,
             now=None) -> dict:
        declaration = self._grant(queue, actor, write=False)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Denied(f"limit must be from 1 to {PAGE_MAX}")
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise Denied("cursor must be a nonnegative sequence")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        with self._locked():
            head = self._head(declaration.name)
            if cursor is not None and cursor > head:
                raise Denied("cursor is beyond the queue head")
            seq = self._cursor(declaration.name, actor) if cursor is None else cursor
            rows, delayed = [], False
            while seq < head and len(rows) < limit:
                candidate = seq + 1
                message = self._message(declaration.name, candidate)
                if message is not None and not self._due(message, stamp):
                    delayed = True  # head-of-line: no cursor may cross it
                    break
                if message is not None:
                    rows.append(message)
                seq = candidate
            self._retain(declaration.name, declaration, stamp)
            return {"messages": rows, "next": seq, "head": head,
                    "more": seq < head, "delayed": delayed}

    def ack(self, queue: str, actor: str, seq: int, *, now=None) -> int:
        declaration = self._grant(queue, actor, write=False)
        if type(seq) is not int or seq < 0:
            raise Denied("acknowledged sequence must be nonnegative")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        with self._locked():
            if seq > self._head(declaration.name):
                raise Denied("acknowledged sequence is beyond the queue head")
            current = self._cursor(declaration.name, actor)
            for candidate in range(current + 1, seq + 1):
                message = self._message(declaration.name, candidate)
                if message is None:
                    # Retention removes only messages every reader had already
                    # acknowledged; a gap is proof of delivery, not the reverse.
                    continue
                if not self._due(message, stamp):
                    raise Denied("Cannot acknowledge an undelivered message")
            if seq > current:
                atomic_write(self._cursor_path(declaration.name, actor), canonical({"seq": seq}))
                return seq
            return current

    def pending(self, queue: str, actor: str, *, newest: int = PAGE_DEFAULT, now=None) -> dict:
        """Count the due unacknowledged messages a read would deliver to ``actor``.

        The count covers the contiguous due run after the actor's cursor (a
        delayed message ends it, as it ends a read), scanning at most
        ``PAGE_MAX`` messages; ``more`` reports a longer run. ``messages``
        holds the newest ``newest`` (0..PAGE_MAX) of the counted run. Reads
        nothing else and acknowledges nothing; no lock is taken.
        """
        declaration = self._grant(queue, actor, write=False)
        if type(newest) is not int or not 0 <= newest <= PAGE_MAX:
            raise Denied(f"newest must be from 0 to {PAGE_MAX}")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        head = self._head(declaration.name)
        seq = self._cursor(declaration.name, actor)
        count, rows = 0, []
        while seq < head and count < PAGE_MAX:
            seq += 1
            message = self._message(declaration.name, seq)
            if message is None:
                continue
            if not self._due(message, stamp):
                break
            count += 1
            if newest:
                rows.append(message)
                del rows[:-newest]
        more = count == PAGE_MAX and seq < head
        through = seq if count else self._cursor(declaration.name, actor)
        return {"queue": declaration.name, "count": count, "more": more, "messages": rows,
                "through": through}

    def due(self, queue: str, actor: str, *, now=None) -> bool:
        """Whether a due message that has not yet woken ``actor`` is waiting for it.

        Wakes are edges, not levels: a message wakes a reader once (see
        ``mark_woken``), and an unacknowledged message does not wake it again.
        Besides the queue's ``wake`` list, a reply wakes the author of the
        message it refers to (see ``_wakes``), so a thread reaches whoever
        started it.
        """
        declaration = self._grant(queue, actor, write=False)
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        head = self._head(declaration.name)
        seq = max(self._cursor(declaration.name, actor), self._woken(declaration.name, actor))
        while seq < head:
            seq += 1
            message = self._message(declaration.name, seq)
            if message is None:
                continue
            if not self._due(message, stamp):
                return False  # head-of-line: a read stops here too
            if self._wakes(declaration, message, actor):
                return True
        return False

    def message(self, id: str, actor: str) -> dict:
        message_id = _id(id, "message id")
        actor = self._actor(actor)
        message = self._message_by_id(message_id)
        if message is None:
            raise Denied(f"No message {message_id}")
        self._grant(message["queue"], actor, write=False)
        return message

    def refs(self, id: str, direction: str, actor: str, *, cursor=None, limit=PAGE_DEFAULT) -> dict:
        message = self.message(id, actor)
        if direction not in ("to", "from"):
            raise Denied("direction must be 'to' or 'from'")
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Denied(f"limit must be from 1 to {PAGE_MAX}")
        if cursor is not None and (not isinstance(cursor, str) or not _ID.fullmatch(cursor)):
            raise Denied("cursor must be a message id")
        if direction == "from":
            ids = sorted(set(message["refs"]))
        else:
            ids = sorted({entry["id"] for entry in self._refs_to(message["id"])
                          if isinstance(entry, dict) and isinstance(entry.get("id"), str)})
        head = ids[-1] if ids else None
        candidates = [item for item in ids if cursor is None or item > cursor]
        rows, last = [], cursor
        for item in candidates:
            if len(rows) == limit:
                break
            last = item
            try:
                referred = self._message_by_id(item)
            except Denied:
                continue
            if referred is None or not allows(self._decl(referred["queue"]).read, actor):
                continue
            rows.append(referred)
        more = head is not None and last is not None and last < head
        return {"messages": rows, "next": last, "head": head, "more": more}

    # -- retention ---------------------------------------------------------------------------
    def _remove_ref(self, target: str, message_id: str) -> None:
        entries = self._refs_to(target)
        remaining = [entry for entry in entries
                     if not (isinstance(entry, dict) and entry.get("id") == message_id)]
        path = self.root / self._relative("refs", target, "to.json")
        if remaining:
            atomic_write(path, canonical(remaining))
        else:
            path.unlink(missing_ok=True)

    def _retain(self, queue: str, declaration: Queue, stamp: dt.datetime) -> None:
        """Remove bounded old messages only when every reader acknowledged them."""
        path = self.root / self._relative(queue, "retain.json")
        stored = self._optional_json(self._relative(queue, "retain.json"))
        cursor = stored.get("seq") if isinstance(stored, Mapping) else None
        if type(cursor) is not int or cursor < 0:
            cursor = 0
        head = self._head(queue)
        stop = min(head, cursor + MAX_RETENTION_SCAN)
        try:
            cutoff = stamp - dt.timedelta(days=declaration.retain_days)
        except OverflowError:
            return
        last, blocked = cursor, None
        for seq in range(cursor + 1, stop + 1):
            message = self._message(queue, seq)
            if message is None:
                if blocked is None:
                    last = seq  # already removed: never re-examine it
                continue
            try:
                if _instant(message["at"], "message time") > cutoff:
                    break
            except (Denied, KeyError):
                break
            # role:* has no concrete actor cursor, so it stays at zero and
            # fails closed rather than dropping a message another role may read.
            if any(self._cursor(queue, reader) < seq for reader in declaration.read):
                break
            if self._refs_to(message["id"]):
                if blocked is None:
                    blocked = seq  # do not advance the cursor past a live referrer
                continue
            message_path = self.root / self._relative(queue, "messages", f"{seq:020d}.json")
            message_path.unlink(missing_ok=True)
            for ref in message.get("refs", []):
                self._remove_ref(ref, message["id"])
            (self.root / self._relative("refs", message["id"], "to.json")).unlink(missing_ok=True)
            if blocked is None:
                last = seq
        if last != cursor:
            atomic_write(path, canonical({"seq": last}))
