"""Declared message queues: authenticated append, delivery facts, refs and retention.

Layout under the project root:
  queues/<queue>/messages/<seq:020d>.json  immutable messages, create-only
  queues/<queue>/head.json                 last assigned sequence (derived)
  queues/<queue>/delivery/<seq:020d>/<principal>.json  create-only delivery facts
  queues/<queue>/skip/<principal>.json     derived delivered runs and deadline gates
  queues/delivery-migration.json           one-time cursor/woken conversion marker
  queues/<queue>/retain.json               first sequence not yet considered for removal
  queues/ids/<id>.json                     global id -> (queue, seq); a generated id retires
                                         with its message, a caller id stays a tombstone
  queues/refs/<id>/to.json                 bounded pointers to messages referring to <id>
  queues/intent.json                       the one in-flight append (crash recovery)
  queues/retire/<id>.json                  durable retirement intent naming a removed
                                           message's outgoing refs and generated-id
                                           fact, finished by a bounded retention pass
  locks/queues.lock                        serialises writes
  wake/<role>                              touched on a due append to a waking queue

Trust: queue declarations are operator configuration; writers supply only a
body and are bound to an authenticated principal by the caller. Validation is
fail-closed and carries no queue name, role name or message meaning. Message
files are create-only and complete; the queue lock serialises writes and the
bounded retention that appends and reads may run, completing an interrupted
append before starting another operation. A removal records its outgoing refs
and generated-id fact before unlinking the body; later retention passes
reconcile those intents, finishing delivery, skip and ref cleanup and
unlinking a pointer only when its body is gone and the pointer still matches
the generated one named by the intent.
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
#: Retirement intents reconciled per retention pass, never unbounded storage.
MAX_RETIRE_RECONCILE = MAX_RETENTION_SCAN
MAX_RETAIN_LAST = 64
DELIVERY_MAX_PRINCIPALS = 512
DEFAULT_RETAIN_DAYS = 30
INTEGER_LIMIT = (1 << 63) - 1
#: Queue and field names: lowercase identifiers.
NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")

#: Queue principals include the role wildcard; an actor is always concrete.
PRINCIPAL = re.compile(r"(?:role:(?:\*|[a-z][a-z0-9_-]{0,62})|host|operator)\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DELIVERY_KEYS = frozenset({"unit", "at"})
_SKIP_KEYS = frozenset({"runs", "gates"})
_RETIRE_KEYS = frozenset({"id", "queue", "seq", "id_generated", "refs"})
# Prior versions wrote intents without outgoing refs; accept them so an
# interrupted removal survives an upgrade and its pointer still retires.
_LEGACY_RETIRE_KEYS = frozenset({"id", "queue", "seq", "id_generated"})
_DELIVERY_UNIT_MAX = 128
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
    retain_last: int | None = None
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
        _keys(spec, frozenset({"write", "read", "wake", "retain_days", "retain_last", "schema"}), where)
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
        retain_last = None
        if "retain_last" in spec:
            if "retain_days" in spec:
                raise ConfigError(f"{where}: retain_days and retain_last are alternatives")
            retain_last = _bound(spec["retain_last"], f"{where}.retain_last", 1, MAX_RETAIN_LAST)
            if wake:
                raise ConfigError(f"{where}.retain_last cannot be combined with wake")
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
        result[name] = Queue(name=name, write=write, read=read, wake=wake, retain_days=days,
                             retain_last=retain_last, fields=MappingProxyType(fields))
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
    def _locked(self, *, migrate: bool = True) -> Iterator[None]:
        # ``migrate=False`` is only for migrate_cursors itself, which enters
        # the lock and then runs the conversion without re-acquiring it.
        mkdir(self.root / "locks")
        with lock(self.root / "locks" / "queues.lock"):
            self._recover()
            if migrate:
                self._migrate_cursors()
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

    def _publish(self, message: dict, stamp: dt.datetime, *, id_generated=False) -> None:
        queue, seq, message_id = message["queue"], message["seq"], message["id"]
        path = self.root / self._relative(queue, "messages", f"{seq:020d}.json")
        atomic_write(path, canonical(message), exclusive=True)
        for ref in message["refs"]:
            self._add_ref(ref, message)
        # A caller-supplied id is an idempotency key: its pointer must survive
        # removal so a retry cannot reuse an id whose message is gone. A
        # generated id has no such contract and its pointer would otherwise
        # outlive the message forever, so mark it for retirement with the body.
        pointer = {"queue": queue, "seq": seq}
        if id_generated is True:
            pointer["id_generated"] = True
        atomic_write(self.root / self._relative("ids", f"{message_id}.json"),
                     canonical(pointer), exclusive=True)
        if seq > self._stored_head(queue):
            atomic_write(self.root / self._relative(queue, "head.json"), canonical({"seq": seq}))
        declaration = self.queues.get(queue)
        if declaration is not None and message.get("deliver_at") is None:
            # An immediate message credits its author when the author may read
            # the queue. First write wins; a retry or crash replay cannot add
            # a second credit. Delayed messages are not credited: their author
            # asked to be woken when they become due.
            try:
                author = self._actor(message.get("author"))
                at = _instant(message.get("at"), "message time").isoformat(timespec="seconds")
            except Denied:
                author, at = None, None
            if author is not None and allows(declaration.read, author):
                self._write_delivery(queue, seq, author, "append", at)
        if not self._due(message, stamp):
            # A future delivery writes no role wake, so a daemon caching its
            # next deadline would otherwise sleep through it. Bump the wake
            # directory to invalidate that cache; admission stays authoritative.
            pending = self.root / "wake" / "delivery-pending"
            mkdir(pending.parent)
            atomic_write(pending, canonical({"at": clock()}))
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
            self._publish(dict(intent["message"]), _instant(clock(), "now"),
                          id_generated=intent.get("id_generated") is True)
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
            if declaration.retain_last is not None and (deliver is not None or ref_ids):
                raise Denied(f"Queue {declaration.name} with retain_last does not accept "
                             "refs or delayed messages")
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
            id_generated = id is None
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
            atomic_write(self.root / self._relative("intent.json"),
                         canonical({"message": message, "id_generated": id_generated}))
            self._recover()
            if declaration.retain_last is not None:
                self._retain_last(declaration.name, declaration)
            else:
                self._retain(declaration.name, declaration, stamp)
            return message

    # -- reading -----------------------------------------------------------------------------
    def _delivery_dir(self, queue: str, seq: int) -> Path:
        if type(seq) is not int or seq < 1:
            raise Denied("delivery sequence must be positive")
        return self.root / self._relative(queue, "delivery", f"{seq:020d}")

    def _delivery_path(self, queue: str, seq: int, actor: str) -> Path:
        return self._delivery_dir(queue, seq) / (self._actor(actor).replace(":", "_") + ".json")

    def _delivery(self, queue: str, seq: int, actor: str) -> dict | None:
        """One create-only fact, or None when absent; malformed facts fail closed."""
        actor = self._actor(actor)
        name = self._relative(queue, "delivery", f"{seq:020d}",
                              actor.replace(":", "_") + ".json")
        try:
            stored = self._optional_json(name)
        except Denied:
            raise
        except (ValueError, RecursionError) as exc:
            raise Denied("Corrupt delivery record") from exc
        if stored is None:
            return None
        unit = stored.get("unit") if isinstance(stored, Mapping) else None
        if (not isinstance(stored, Mapping) or set(stored) != _DELIVERY_KEYS
                or not isinstance(unit, str) or len(unit) > _DELIVERY_UNIT_MAX
                or "\x00" in unit):
            raise Denied("Corrupt delivery record")
        _instant(stored["at"], "delivery time")
        return dict(stored)

    def _write_delivery(self, queue: str, seq: int, actor: str, unit: str, at: str) -> dict:
        """Create one fact inside the lock; the first writer's bytes win."""
        if (not isinstance(unit, str) or len(unit) > _DELIVERY_UNIT_MAX
                or "\x00" in unit):
            raise Denied("delivery unit must be a bounded string")
        at = _instant(at, "delivery time").isoformat(timespec="seconds")
        existing = self._delivery(queue, seq, actor)
        if existing is not None:
            return existing
        record = {"unit": unit, "at": at}
        atomic_write(self._delivery_path(queue, seq, actor), canonical(record), exclusive=True)
        # Every fact write extends the derived runs; callers hold the queue
        # lock, so the index and the facts it summarizes move together.
        self._extend_skip(queue, seq, actor)
        return record

    def _skip_path(self, queue: str, actor: str) -> Path:
        return (self.root / self._relative(queue, "skip")
                / (self._actor(actor).replace(":", "_") + ".json"))

    def _skip_index(self, queue: str, actor: str, head: int) -> tuple[tuple, tuple]:
        """Validated delivered runs and delayed gates for one reader.

        Each run is ``(start, end)``: every message in it has a delivery fact
        for ``actor``. Each gate is ``(seq, deliver_at)`` for a delivered
        delayed message in a run. Missing or malformed state fails closed to
        empty runs, i.e. a full scan; no fact is inferred from the index.
        """
        name = self._relative(queue, "skip", self._actor(actor).replace(":", "_") + ".json")
        try:
            stored = self._optional_json(name)
        except (Denied, ValueError, RecursionError):
            return (), ()
        if not isinstance(stored, Mapping) or set(stored) != _SKIP_KEYS:
            return (), ()
        raw_runs, raw_gates = stored["runs"], stored["gates"]
        if (not isinstance(raw_runs, list) or not isinstance(raw_gates, list)
                or len(raw_runs) > head or len(raw_gates) > head):
            return (), ()
        runs: list[tuple[int, int]] = []
        previous = 0
        for item in raw_runs:
            if (not isinstance(item, list) or len(item) != 2
                    or type(item[0]) is not int or type(item[1]) is not int
                    or item[0] <= previous or item[1] < item[0] or item[1] > head):
                return (), ()
            runs.append((item[0], item[1]))
            previous = item[1]
        gates: list[tuple[int, str]] = []
        previous = 0
        for item in raw_gates:
            if (not isinstance(item, list) or len(item) != 2
                    or type(item[0]) is not int or type(item[1]) is not str
                    or item[0] <= previous or item[0] > head
                    or not any(start <= item[0] <= end for start, end in runs)):
                return (), ()
            try:
                _instant(item[1], "deliver_at")
            except Denied:
                return (), ()
            gates.append((item[0], item[1]))
            previous = item[0]
        return tuple(runs), tuple(gates)

    @staticmethod
    def _skip_to(index: tuple[tuple, tuple], seq: int, stamp: dt.datetime) -> int:
        """Skip delivered, due messages starting at ``seq + 1``.

        A delivered message with a future ``deliver_at`` is a head-of-line
        boundary at this observation time, so the scan stops just before it.
        All other messages in a delivered run are due at every ``now`` and can
        be crossed without changing what a scan sees.
        """
        runs, gates = index
        for start, end in runs:
            if end < seq + 1:
                continue
            if start > seq + 1:
                break
            for gate_seq, deadline in gates:
                if gate_seq > end:
                    break
                if gate_seq > seq and _instant(deadline, "deliver_at") > stamp:
                    return gate_seq - 1
            return end
        return seq

    def _extend_skip(self, queue: str, seq: int, actor: str) -> None:
        """Add one delivery to ``actor``'s runs; called under the lock.

        An out-of-order by-ID handoff joins neighbouring runs when the gap is
        filled. Each fact write touches one small index file, not the retained
        bodies. A message we cannot summarize leaves no index to skip with.
        """
        path = self._skip_path(queue, actor)
        try:
            message = self._message(queue, seq)
            if message is None:
                return
            runs, gates = self._skip_index(queue, actor, self._head(queue))
            runs, gates = list(runs), list(gates)
            gates = [gate for gate in gates if gate[0] != seq]
            deadline = message.get("deliver_at")
            if deadline is not None:
                _instant(deadline, "deliver_at")
                gates.append((seq, deadline))
            gates.sort()
            before = next((i for i, run in enumerate(runs) if run[1] == seq - 1), None)
            after = next((i for i, run in enumerate(runs) if run[0] == seq + 1), None)
            if before is not None and after is not None:
                runs[before] = (runs[before][0], runs[after][1])
                del runs[after]
            elif before is not None:
                runs[before] = (runs[before][0], seq)
            elif after is not None:
                runs[after] = (seq, runs[after][1])
            else:
                runs.append((seq, seq))
                runs.sort()
            atomic_write(path, canonical({"runs": [list(run) for run in runs],
                                          "gates": [list(gate) for gate in gates]}))
        except (Denied, ValueError, RecursionError):
            path.unlink(missing_ok=True)  # a corrupt summary licenses no skip

    def _cursor_principal(self, name: str) -> str:
        """Decode the cursor filename convention back to a concrete principal."""
        if name in ("host", "operator"):
            return self._actor(name)
        if name.startswith("role_"):
            return self._actor("role:" + name[len("role_"):])
        raise Denied("Corrupt cursor principal")

    def read(self, queue: str, actor: str, *, cursor=None, limit=PAGE_DEFAULT,
             unit=None, now=None) -> dict:
        """Return one page of messages, recording delivery for the rows returned.

        ``cursor`` is only a page position; without it the scan starts at the
        retained prefix and skips messages already delivered to ``actor``.
        ``unit`` names the consuming operation when delivery is recorded.
        """
        declaration = self._grant(queue, actor, write=False)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise Denied(f"limit must be from 1 to {PAGE_MAX}")
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise Denied("cursor must be a nonnegative sequence")
        if cursor is not None and cursor > self._head(declaration.name):
            raise Denied("cursor is beyond the queue head")
        if unit is not None and (not isinstance(unit, str) or len(unit) > _DELIVERY_UNIT_MAX
                                 or "\x00" in unit):
            raise Denied("delivery unit must be a bounded string")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        with self._locked():
            head = self._head(declaration.name)
            seq = (cursor if cursor is not None
                   else max(self._retained(declaration.name), 0))
            # An explicit cursor pages history: delivered rows are returned.
            index = (self._skip_index(declaration.name, actor, head)
                     if cursor is None else ((), ()))
            rows, delayed = [], False
            # Bound the page by rows returned, not by positions examined:
            # already-delivered rows are skipped and must never push a later
            # undelivered message out of reach. The scan still stops at a
            # not-yet-due message (head-of-line) and at the queue head.
            while seq < head and len(rows) < limit:
                seq = self._skip_to(index, seq, stamp)
                if seq >= head:
                    break
                candidate = seq + 1
                message = self._message(declaration.name, candidate)
                if message is not None and not self._due(message, stamp):
                    delayed = True  # head-of-line: no page may cross it
                    break
                seq = candidate
                if message is None or (cursor is None
                                       and self.delivered(declaration.name, candidate, actor)):
                    continue
                if unit is not None:
                    self._write_delivery(declaration.name, candidate, actor, unit,
                                         stamp.isoformat(timespec="seconds"))
                rows.append(message)
            if declaration.retain_last is not None:
                self._retain_last(declaration.name, declaration)
            else:
                self._retain(declaration.name, declaration, stamp)
            return {"messages": rows, "next": seq, "head": head,
                    "more": seq < head, "delayed": delayed}

    def pending(self, queue: str, actor: str, *, newest: int = PAGE_DEFAULT, now=None) -> dict:
        """Count the due messages a read would deliver to ``actor``.

        The scan starts at the retained prefix, skips gaps and messages already
        delivered to ``actor``, and ends at a not-yet-due message (head-of-line)
        or the queue head. A delivered prefix is skipped, not counted, so it
        cannot hide a later undelivered message. ``messages`` holds the oldest
        ``newest`` (0..PAGE_MAX) of the counted messages. Nothing is written.
        """
        declaration = self._grant(queue, actor, write=False)
        if type(newest) is not int or not 0 <= newest <= PAGE_MAX:
            raise Denied(f"newest must be from 0 to {PAGE_MAX}")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        head = self._head(declaration.name)
        seq = max(self._retained(declaration.name), 0)
        index = self._skip_index(declaration.name, actor, head)
        count, rows = 0, []
        # Count undelivered messages, not examined positions: a delivered
        # run of any length cannot hide the next undelivered message.
        # A not-yet-due message still ends the contiguous run (head-of-line).
        while seq < head:
            seq = self._skip_to(index, seq, stamp)
            if seq >= head:
                break
            candidate = seq + 1
            message = self._message(declaration.name, candidate)
            if message is not None and not self._due(message, stamp):
                break  # head-of-line: a read stops here too, so do not cross it
            seq = candidate
            if message is None or self.delivered(declaration.name, candidate, actor):
                continue
            count += 1
            if newest and len(rows) < newest:
                rows.append(message)
        more = count > len(rows) or seq < head
        return {"queue": declaration.name, "count": count, "more": more, "messages": rows,
                "through": seq}

    def due(self, queue: str, actor: str, *, now=None) -> bool:
        """Whether a due, wake-eligible message awaiting delivery wakes ``actor``.

        Delivery facts are the only reader state: a message wakes a reader once,
        when it is delivered. Besides the queue's ``wake`` list, a reply wakes
        the author of the message it refers to (see ``_wakes``), so a thread
        reaches whoever started it. An undelivered message stays read-pending;
        it just does not wake again.
        """
        declaration = self._grant(queue, actor, write=False)
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        head = self._head(declaration.name)
        seq = max(self._retained(declaration.name), 0)
        index = self._skip_index(declaration.name, actor, head)
        # Scan to the head, stopping at the first not-yet-due message. A long
        # delivered run is skipped, not counted, so it cannot hide a wake.
        while seq < head:
            seq = self._skip_to(index, seq, stamp) + 1
            if seq > head:
                break
            message = self._message(declaration.name, seq)
            if message is None:
                continue
            if not self._due(message, stamp):
                return False  # head-of-line: a read stops here too
            if (self._wakes(declaration, message, actor)
                    and not self.delivered(declaration.name, seq, actor)):
                return True
        return False

    def delivered(self, queue: str, seq: int, actor: str) -> bool:
        """Whether ``actor`` has a delivery fact for message ``seq``; read-only."""
        self._grant(queue, actor, write=False)
        if type(seq) is not int or seq < 1:
            raise Denied("delivery sequence must be positive")
        return self._delivery(queue, seq, actor) is not None

    def record_delivery(self, queue: str, seq: int, actor: str, unit: str,
                        *, now=None, handed=False) -> dict:
        """Record that ``seq`` was delivered to ``actor``; idempotent, first write wins.

        ``handed`` is set only by the opening handoff path, which has just
        serialized the body into a dispatched prompt. The handoff itself proves
        delivery, so a future ``deliver_at`` cannot veto that fact. Eligibility
        paths (append, ``queue_read``, ``message_read``, wake) never set it and
        still refuse to record delivery before the deadline.
        """
        declaration = self._grant(queue, actor, write=False)
        if type(seq) is not int or seq < 1:
            raise Denied("delivery sequence must be positive")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        if seq > self._head(declaration.name):
            raise Denied("delivery sequence is beyond the queue head")
        message = self._message(declaration.name, seq)
        if message is None:
            raise Denied("No message to deliver")
        if not handed and not self._due(message, stamp):
            raise Denied("Cannot record delivery for an undelivered message")
        if (not isinstance(unit, str) or len(unit) > _DELIVERY_UNIT_MAX
                or "\x00" in unit):
            raise Denied("delivery unit must be a bounded string")
        at = stamp.isoformat(timespec="seconds")
        with self._locked():
            return self._write_delivery(declaration.name, seq, actor, unit, at)

    def _migrate_cursors(self) -> dict:
        """Convert cursors to facts once; called under the queue lock."""
        marker = self._relative("delivery-migration.json")
        try:
            done = self._optional_json(marker)
        except Denied:
            raise
        except (ValueError, RecursionError) as exc:
            raise Denied("Corrupt delivery migration marker") from exc
        if done is not None:
            return {"migrated": 0}
        # Validate every cursor before writing anything: a malformed one must
        # leave the whole conversion unchanged for a later, resumable run.
        pending: list[tuple[str, str, int, Path]] = []
        for queue in sorted(self.queues):
            directory = self.root / self._relative(queue, "cursors")
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise Denied("Corrupt cursor directory")
            files = sorted(path for path in directory.iterdir() if path.suffix == ".json")
            if len(files) > DELIVERY_MAX_PRINCIPALS:
                raise Denied("Cursor directory exceeds its bound")
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise Denied("Corrupt cursor file")
                principal = self._cursor_principal(path.stem)
                stored = self._optional_json(self._relative(queue, "cursors", path.name))
                seq = stored.get("seq") if isinstance(stored, Mapping) else None
                if type(seq) is not int or seq < 0:
                    raise Denied("Corrupt cursor")
                pending.append((queue, principal, seq, path))
        at = clock()
        for queue, principal, seq, path in pending:
            for candidate in range(1, min(seq, self._head(queue)) + 1):
                if self._message(queue, candidate) is not None:
                    self._write_delivery(queue, candidate, principal, "migration", at)
            path.unlink()
        root = self.root / "queues"
        if root.is_dir():
            for directory in sorted(root.glob("*/woken")):
                if directory.is_symlink() or not directory.is_dir():
                    raise Denied("Corrupt woken directory")
                files = sorted(path for path in directory.iterdir() if path.suffix == ".json")
                if len(files) > DELIVERY_MAX_PRINCIPALS:
                    raise Denied("Woken directory exceeds its bound")
                for path in files:
                    if path.is_symlink() or not path.is_file():
                        raise Denied("Corrupt woken file")
                    path.unlink()
        atomic_write(self.root / marker, canonical({"at": at}))
        return {"migrated": len(pending)}

    def migrate_cursors(self) -> dict:
        """Convert declared queues' cursors into delivery facts once."""
        with self._locked(migrate=False):
            return self._migrate_cursors()

    def next_delivery(self, queue: str, actor: str, *, now=None) -> dt.datetime | None:
        """The aware ``deliver_at`` of the first future message ``actor``'s
        due-scan would reach but that has not been delivered to it, or None.

        Schema: returns an aware ``datetime`` or None; ``now`` is an ISO time
        used for the due comparison. Bounds: scans exactly the range ``due``
        scans, from the retained prefix to the head, skipping retention gaps,
        stopping at the first not-due message (head-of-line), and examining at
        the queue head. Only messages that would wake ``actor``
        (see ``_wakes``) and have no delivery fact for it count. Trust:
        ``deliver_at`` is validated through ``_instant`` and the read grant is
        checked. Retry/cancellation: none. Evidence: the message's stored
        ``deliver_at`` field. Failure: raises ``Denied`` for an undeclared
        queue, an invalid actor, a missing read grant or a corrupt
        ``deliver_at``. Read-only; takes no lock and records no delivery.
        """
        declaration = self._grant(queue, actor, write=False)
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        head = self._head(declaration.name)
        seq = max(self._retained(declaration.name), 0)
        index = self._skip_index(declaration.name, actor, head)
        # As in ``due``, delivered rows are skipped without ending the scan,
        # so a delivered run cannot hide a later actionable deadline.
        while seq < head:
            seq = self._skip_to(index, seq, stamp) + 1
            if seq > head:
                break
            message = self._message(declaration.name, seq)
            if message is None:
                continue
            if not self._due(message, stamp):
                if (self._wakes(declaration, message, actor)
                        and not self.delivered(declaration.name, seq, actor)):
                    return _instant(message["deliver_at"], "deliver_at")
                return None  # head-of-line: no later deadline can be reached
        return None

    def _read_by_id(self, id: str, actor: str) -> dict:
        """Grant-checked body lookup by id; callers decide its due contract.

        ``refs`` uses this listing path so an authorized reader can still
        follow a reference to a pending delayed message; only a reader handoff
        (``message``, including nested opening bodies) requires the body due.
        """
        message_id = _id(id, "message id")
        actor = self._actor(actor)
        message = self._message_by_id(message_id)
        if message is None:
            raise Denied(f"No message {message_id}")
        self._grant(message["queue"], actor, write=False)
        return message

    def message(self, id: str, actor: str, *, unit=None, now=None) -> dict:
        """Resolve one readable, due message, optionally recording its delivery.

        ``unit`` names the consuming operation; without it this read is pure.
        The body is handed over only at or after its ``deliver_at``; a body
        withheld by the deadline is neither returned nor credited.
        """
        message = self._read_by_id(id, actor)
        actor = self._actor(actor)
        if unit is not None and (not isinstance(unit, str) or len(unit) > _DELIVERY_UNIT_MAX
                                 or "\x00" in unit):
            raise Denied("delivery unit must be a bounded string")
        stamp = _instant(now, "now") if now is not None else _instant(clock(), "now")
        if not self._due(message, stamp):
            raise Denied("Message is not due yet")
        if unit is not None:
            with self._locked():
                self._write_delivery(message["queue"], message["seq"], actor, unit,
                                     stamp.isoformat(timespec="seconds"))
        return message

    def refs(self, id: str, direction: str, actor: str, *, cursor=None, limit=PAGE_DEFAULT) -> dict:
        """List readable messages referring to ``id``, or those ``id`` refers to.

        Listing is a lookup, not a reader handoff: it stays usable for pending
        delayed messages, whose bodies only become handoff-eligible by
        ``message`` once due.
        """
        message = self._read_by_id(id, actor)
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
    def _remove_delivery(self, queue: str, seq: int) -> None:
        """Remove one message's bounded delivery directory and its facts."""
        directory = self._delivery_dir(queue, seq)
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise Denied("Corrupt delivery directory")
        entries = list(directory.iterdir())
        if len(entries) > DELIVERY_MAX_PRINCIPALS:
            raise Denied("Delivery directory exceeds its bound")
        for entry in entries:
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                raise Denied("Corrupt delivery record")
            entry.unlink()
        directory.rmdir()

    def _remove_skip(self, queue: str, seq: int) -> None:
        """Retire one removed message from every reader's derived runs."""
        directory = self.root / self._relative(queue, "skip")
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise Denied("Corrupt skip directory")
        # The removed seq may have been the last body: include it so its own
        # index entry stays readable and can be retired rather than left stale.
        head = max(self._head(queue), seq)
        for path in sorted(directory.glob("*.json")):
            try:
                actor = self._cursor_principal(path.stem)
            except Denied:
                path.unlink(missing_ok=True)  # an unnamed summary cannot be used
                continue
            runs, gates = self._skip_index(queue, actor, head)
            remaining = []
            for start, end in runs:
                if seq < start or seq > end:
                    remaining.append((start, end))
                    continue
                if start < seq:
                    remaining.append((start, seq - 1))
                if seq < end:
                    remaining.append((seq + 1, end))
            gates = [gate for gate in gates if gate[0] != seq]
            if remaining or gates:
                atomic_write(path, canonical({"runs": [list(run) for run in remaining],
                                              "gates": [list(gate) for gate in gates]}))
            else:
                path.unlink(missing_ok=True)

    def _retire_path(self, message_id: str) -> Path:
        """Durable intent licensing the retirement of one removed message."""
        return self.root / self._relative("retire", f"{message_id}.json")

    def _write_retire_intent(self, message_id: str, queue: str, seq: int,
                             id_generated: bool, refs: list) -> None:
        """Record refs and the generated-id fact before any body unlink."""
        _id(message_id, "message id")
        if queue not in self.queues:
            raise Denied("Corrupt queue message")
        if type(seq) is not int or seq < 1:
            raise Denied("Corrupt queue message")
        if type(id_generated) is not bool:
            raise Denied("Corrupt retirement intent")
        if (not isinstance(refs, list) or len(refs) > MAX_REFS
                or any(not isinstance(ref, str) or not _ID.fullmatch(ref)
                       for ref in refs)):
            raise Denied("Corrupt message refs")
        atomic_write(self._retire_path(message_id),
                     canonical({"id": message_id, "queue": queue, "seq": seq,
                                "id_generated": id_generated, "refs": list(refs)}))

    def _read_retire_intent(self, path: Path) -> dict | None:
        """Validate one intent strictly; malformed or unsafe ones license nothing."""
        if path.is_symlink() or not path.is_file() or not _ID.fullmatch(path.stem):
            return None
        try:
            stored = self._optional_json(self._relative("retire", path.name))
        except (Denied, ValueError, RecursionError):
            return None
        if (not isinstance(stored, Mapping)
                or set(stored) not in (_RETIRE_KEYS, _LEGACY_RETIRE_KEYS)
                or stored["id"] != path.stem or not isinstance(stored["queue"], str)
                or stored["queue"] not in self.queues
                or type(stored["seq"]) is not int or stored["seq"] < 1
                or type(stored["id_generated"]) is not bool):
            return None
        refs = stored.get("refs", [])
        if (not isinstance(refs, list) or len(refs) > MAX_REFS
                or any(not isinstance(ref, str) or not _ID.fullmatch(ref)
                       for ref in refs)):
            return None
        return {**stored, "refs": list(refs)}

    @staticmethod
    def _matches_retire_intent(pointer: Mapping, intent: Mapping) -> bool:
        """Whether a pointer is exactly the generated one an intent names."""
        return (intent.get("id_generated") is True
                and pointer.get("queue") == intent["queue"]
                and pointer.get("seq") == intent["seq"]
                and pointer.get("id_generated") is True)

    def _retire_pointer(self, intent: Mapping) -> None:
        """Unlink a validated generated pointer, then its intent.

        Called only after the body and its dependent facts are gone. An absent
        pointer means an earlier pass finished the unlink and only the intent
        remains. A caller tombstone is never unlinked: an intent with
        ``id_generated`` False expires while keeping its pointer. A pointer
        that is corrupt or is not the pointer the intent names licenses no
        deletion, and keeps both records.
        """
        message_id = intent["id"]
        try:
            pointer = self._id_pointer(message_id)
        except Denied:
            return
        if intent.get("id_generated") is True:
            if pointer is not None and not self._matches_retire_intent(pointer, intent):
                return
            if pointer is not None:
                (self.root / self._relative("ids", message_id + ".json")).unlink(missing_ok=True)
        else:
            # Caller idempotency keys stay: expire only over the expected
            # tombstone (or when it is already gone).
            if pointer is not None:
                if (pointer.get("queue") != intent["queue"]
                        or pointer.get("seq") != intent["seq"]):
                    return
                if pointer.get("id_generated") is True:
                    return
        self._retire_path(message_id).unlink(missing_ok=True)

    def _reconcile_retirements(self) -> None:
        """Finish a bounded number of outstanding removals.

        Retention starts each pass with this: a body that is still present
        stays with retention, a removed body has its delivery facts, skip
        entries and outgoing refs finished and its own ref index unlinked,
        and a generated pointer is unlinked only when it still matches the
        intent. Malformed or unsafe intents license no deletion and remain
        for a later pass.
        """
        directory = self.root / self._relative("retire")
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise Denied("Corrupt retirement directory")
        paths = []
        for path in directory.glob("*.json"):
            paths.append(path)
            if len(paths) >= MAX_RETIRE_RECONCILE:
                break
        for path in paths:
            intent = self._read_retire_intent(path)
            if intent is None:
                continue
            try:
                if self._message(intent["queue"], intent["seq"]) is not None:
                    continue  # a live body is retention's decision, not an intent's
                pointer = self._id_pointer(intent["id"])
                if intent.get("id_generated") is True:
                    if (pointer is not None
                            and not self._matches_retire_intent(pointer, intent)):
                        continue  # a caller tombstone or changed pointer licenses nothing
                else:
                    if pointer is not None:
                        if (pointer.get("queue") != intent["queue"]
                                or pointer.get("seq") != intent["seq"]):
                            continue  # a changed pointer licenses nothing
                        if pointer.get("id_generated") is True:
                            continue  # an unexpected generated pointer licenses nothing
                self._remove_delivery(intent["queue"], intent["seq"])
                self._remove_skip(intent["queue"], intent["seq"])
                for ref in intent["refs"]:
                    self._remove_ref(ref, intent["id"])
                (self.root / self._relative("refs", intent["id"], "to.json")).unlink(missing_ok=True)
                self._retire_pointer(intent)
            except Denied:
                continue  # unsafe state licenses no deletion; leave the intent

    def _remove_message(self, queue: str, seq: int, message: Mapping) -> None:
        """Unlink a message and finish its recoverable removal in one place.

        The durable retirement intent (outgoing refs plus the generated-id
        fact) is recorded before the body is unlinked, so an interrupted
        cleanup can be finished by retention on a later pass. Caller-supplied
        ids keep their tombstone (the idempotency contract); generated ids
        retire with the message. Outgoing refs and the message's own ref
        index are removed here, so normal and reconciled paths share one
        owner.
        """
        if (not isinstance(message, Mapping) or message.get("queue") != queue
                or message.get("seq") != seq):
            raise Denied("Corrupt queue message")
        message_id = message.get("id")
        refs = message.get("refs", [])
        if (not isinstance(message_id, str) or not _ID.fullmatch(message_id)
                or not isinstance(refs, list) or len(refs) > MAX_REFS
                or any(not isinstance(ref, str) or not _ID.fullmatch(ref)
                       for ref in refs)):
            raise Denied("Corrupt queue message")
        try:
            pointer = self._id_pointer(message_id)
        except Denied:
            pointer = None  # never delete a pointer we cannot validate
        generated = (isinstance(pointer, Mapping) and pointer.get("id_generated") is True
                     and pointer.get("queue") == queue and pointer.get("seq") == seq)
        self._write_retire_intent(message_id, queue, seq, generated, list(refs))
        (self.root / self._relative(queue, "messages", f"{seq:020d}.json")).unlink(missing_ok=True)
        self._remove_delivery(queue, seq)
        self._remove_skip(queue, seq)
        for ref in refs:
            self._remove_ref(ref, message_id)
        (self.root / self._relative("refs", message_id, "to.json")).unlink(missing_ok=True)
        self._retire_pointer({"id": message_id, "queue": queue, "seq": seq,
                              "id_generated": generated, "refs": list(refs)})

    def _remove_ref(self, target: str, message_id: str) -> None:
        entries = self._refs_to(target)
        remaining = [entry for entry in entries
                     if not (isinstance(entry, dict) and entry.get("id") == message_id)]
        path = self.root / self._relative("refs", target, "to.json")
        if remaining:
            atomic_write(path, canonical(remaining))
            return
        path.unlink(missing_ok=True)
        # A count-retained message kept only for this referrer is past its
        # queue's scan prefix, so nothing will reconsider it. Its last
        # referrer is gone: remove it here, where the ref was written, rather
        # than leave a file nothing can reach by sequence or by ref.
        try:
            message = self._message_by_id(target)
        except Denied:
            return
        if message is None:
            return
        declaration = self._decl(message["queue"])
        if (declaration.retain_last is None
                or message["seq"] > self._retained(message["queue"])):
            return
        self._remove_message(message["queue"], message["seq"], message)

    def _retained(self, queue: str) -> int:
        """Validated processed prefix of ``retain.json`` (0 when absent/corrupt)."""
        try:
            stored = self._optional_json(self._relative(queue, "retain.json"))
        except (Denied, ValueError):
            return 0  # unreadable prefix licenses rescanning, not dropping messages
        value = stored.get("seq") if isinstance(stored, Mapping) else None
        return value if type(value) is int and value >= 0 else 0

    def _retain_last(self, queue: str, declaration: Queue) -> None:
        """Keep at most ``retain_last`` newest messages regardless of acks.

        The retained prefix marks messages already considered for removal so
        scans skip their holes. A message still referred to is left on disk
        for id-addressable reads, but it does not hold the prefix back: the
        scan advances past it so newer messages stay bounded and readers see
        the newest facts. Its writer removes it when the last ref goes away
        (see ``_remove_ref``).
        """
        self._reconcile_retirements()
        path = self.root / self._relative(queue, "retain.json")
        cursor = self._retained(queue)
        head = self._head(queue)
        target = head - declaration.retain_last
        stop = min(target, cursor + MAX_RETENTION_SCAN)
        last = cursor
        for seq in range(cursor + 1, stop + 1):
            message = self._message(queue, seq)
            if message is not None and not self._refs_to(message["id"]):
                self._remove_message(queue, seq, message)
            last = seq  # holes and referred messages advance the prefix too
        if last != cursor:
            atomic_write(path, canonical({"seq": last}))

    def _retain(self, queue: str, declaration: Queue, stamp: dt.datetime) -> None:
        """Remove bounded old messages only when every reader was delivered to."""
        self._reconcile_retirements()
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
            # A role:* reader has no concrete delivery fact, so coverage
            # cannot be proven: fail closed rather than drop a message another
            # role may read.
            if any(reader == "role:*" or not self.delivered(queue, seq, reader)
                   for reader in declaration.read):
                break
            if self._refs_to(message["id"]):
                if blocked is None:
                    blocked = seq  # do not advance the cursor past a live referrer
                continue
            self._remove_message(queue, seq, message)
            if blocked is None:
                last = seq
        if last != cursor:
            atomic_write(path, canonical({"seq": last}))
