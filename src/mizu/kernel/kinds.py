"""Kind declarations: fields, states, transitions, requirements and events.

Trust: configuration (operator-owned). Validation is strict and fail-closed:
unknown keys, unknown types, undeclared states and malformed names refuse the
whole declaration. The module attaches no meaning to any declared name.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any, Mapping

from ..errors import ConfigError, Denied

NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
EVENT = re.compile(r"[a-z][a-z0-9_-]{0,62}(?:\.[a-z][a-z0-9_-]{0,62}){0,7}\Z")
_COMPONENT = r"(?:[a-z][a-z0-9_-]{0,62}|\*)"
#: ``**`` is only valid as the final component of a subscription pattern.
#: Exact patterns keep EVENT's component bound; a ``**`` prefix has one fewer
#: component so the terminal wildcard does not push the pattern past it.
PATTERN = re.compile(
    rf"(?:\*\*|{_COMPONENT}(?:\.{_COMPONENT}){{0,6}}\.\*\*"
    rf"|{_COMPONENT}(?:\.{_COMPONENT}){{0,7}})\Z"
)
PRINCIPAL = re.compile(r"(?:role:[a-z][a-z0-9_-]{0,62}|host|operator)\Z")
OBJECT_REFS = ("author", "holder")
TYPES = ("string", "integer", "boolean", "instant", "ref", "principal", "list")
STRING_MAX = 1024 * 1024
LIST_MAX = 1024
INTEGER_LIMIT = 2 ** 63 - 1


def _keys(table: Mapping, allowed: set, where: str) -> None:
    if not isinstance(table, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in {where}: {', '.join(sorted(unknown))}")


def _name(value: Any, where: str, pattern=NAME) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigError(f"{where} must be a lowercase name")
    return value


def _names(value: Any, where: str, pattern=NAME) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list")
    out = tuple(_name(item, where, pattern) for item in value)
    if len(set(out)) != len(out):
        raise ConfigError(f"{where} has duplicates")
    return out


def _principals(value: Any, where: str, *, object_refs: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list of principals")
    out = []
    for item in value:
        if not isinstance(item, str) or not (PRINCIPAL.fullmatch(item)
                                             or (object_refs and item in OBJECT_REFS)):
            raise ConfigError(f"{where}: invalid principal {item!r}")
        out.append(item)
    return tuple(out)


def _bound(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}")
    return value


@dataclasses.dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    max: int | None = None
    min: int | None = None
    kinds: tuple[str, ...] = ()
    item: "Field | None" = None
    max_items: int = 0
    event: str | None = None

    def validate(self, value: Any) -> Any:
        """Return the value if it conforms to the declared type, else refuse."""
        where = f"field {self.name}"
        if self.type == "string":
            if not isinstance(value, str) or len(value) > (self.max or STRING_MAX):
                raise Denied(f"{where} must be a string of at most {self.max or STRING_MAX} characters")
        elif self.type == "integer":
            if (type(value) is not int or (self.min is not None and value < self.min)
                    or (self.max is not None and value > self.max)):
                raise Denied(f"{where} must be an integer within its declared bounds")
        elif self.type == "boolean":
            if type(value) is not bool:
                raise Denied(f"{where} must be a boolean")
        elif self.type == "instant":
            try:
                parsed = dt.datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 64 else None
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None:
                raise Denied(f"{where} must be an aware ISO time")
        elif self.type == "ref":
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                raise Denied(f"{where} must be an object id")
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
class Requirement:
    """A predicate evaluated when a transition is requested (docs/kernel.md)."""
    ref: str | None = None
    state: tuple[str, ...] = ()
    where: tuple[tuple[str, Any], ...] = ()
    field: str | None = None
    present: bool = False

    def describe(self) -> str:
        if self.ref is not None:
            parts = [f"ref {self.ref}"]
            if self.state:
                parts.append("in state " + "/".join(self.state))
            if self.where:
                parts.append("where " + ", ".join(f"{k}={v}" for k, v in self.where))
            return " ".join(parts)
        return f"field {self.field} present"


@dataclasses.dataclass(frozen=True)
class Transition:
    name: str
    sources: tuple[str, ...]
    to: str
    by: tuple[str, ...]
    fields: tuple[str, ...]
    requires: tuple[Requirement, ...]
    emits: str

    def leaves(self, state: str) -> bool:
        return self.sources == ("*",) or state in self.sources


@dataclasses.dataclass(frozen=True)
class Kind:
    name: str
    states: tuple[str, ...]
    initial: str
    create: tuple[str, ...]
    fields: Mapping[str, Field]
    transitions: Mapping[str, Transition]
    created_event: str

    def instant_events(self, values: Mapping[str, Any]) -> list[tuple[str, str, str]]:
        """(field, instant, event) for every set instant field that declares an event."""
        return [(name, values[name], field.event) for name, field in self.fields.items()
                if field.type == "instant" and field.event and name in values]


def _field(name: str, raw: Any, where: str, *, nested: bool = False) -> Field:
    _keys(raw, {"type", "required", "max", "min", "kinds", "item", "max_items", "event"}, where)
    kind = raw.get("type")
    if kind not in TYPES or (nested and kind == "list"):
        raise ConfigError(f"{where}.type must be one of {', '.join(TYPES)}")
    required = raw.get("required", False)
    if type(required) is not bool:
        raise ConfigError(f"{where}.required must be a boolean")
    values: dict[str, Any] = {"name": name, "type": kind, "required": required}
    if "max" in raw:
        values["max"] = _bound(raw["max"], f"{where}.max", -INTEGER_LIMIT if kind == "integer" else 1,
                               INTEGER_LIMIT if kind == "integer" else STRING_MAX)
    if "min" in raw:
        if kind != "integer":
            raise ConfigError(f"{where}.min applies to integers only")
        values["min"] = _bound(raw["min"], f"{where}.min", -INTEGER_LIMIT, INTEGER_LIMIT)
    if "kinds" in raw:
        if kind != "ref":
            raise ConfigError(f"{where}.kinds applies to refs only")
        values["kinds"] = _names(raw["kinds"], f"{where}.kinds")
    if "event" in raw:
        if kind != "instant":
            raise ConfigError(f"{where}.event applies to instants only")
        values["event"] = _name(raw["event"], f"{where}.event", EVENT)
    if kind == "list":
        if "item" not in raw or "max_items" not in raw:
            raise ConfigError(f"{where} needs item and max_items")
        values["item"] = _field(name, raw["item"], f"{where}.item", nested=True)
        values["max_items"] = _bound(raw["max_items"], f"{where}.max_items", 1, LIST_MAX)
    elif "item" in raw or "max_items" in raw:
        raise ConfigError(f"{where}: item and max_items apply to lists only")
    return Field(**values)


def _requirement(raw: Any, where: str, fields: Mapping[str, Field], states_of) -> Requirement:
    _keys(raw, {"ref", "state", "where", "field", "present"}, where)
    if ("ref" in raw) == ("field" in raw):
        raise ConfigError(f"{where} needs exactly one of ref or field")
    if "field" in raw:
        name = _name(raw["field"], f"{where}.field")
        if name not in fields or raw.get("present") is not True or set(raw) - {"field", "present"}:
            raise ConfigError(f"{where}: field requirements are {{field = <declared field>, present = true}}")
        return Requirement(field=name, present=True)
    ref = _name(raw["ref"], f"{where}.ref")
    if ref not in fields or fields[ref].type != "ref":
        raise ConfigError(f"{where}.ref must name a ref field")
    state = _names(raw["state"], f"{where}.state") if "state" in raw else ()
    where_raw = raw.get("where", {})
    if not isinstance(where_raw, Mapping):
        raise ConfigError(f"{where}.where must be a table")
    pairs = []
    for key, value in sorted(where_raw.items()):
        _name(key, f"{where}.where key")
        if isinstance(value, str) and value.startswith("$"):
            if not (value == "$snapshot" or (value.startswith("$field.") and value[7:] in fields)):
                raise ConfigError(f"{where}.where.{key}: unknown reference {value}")
        elif not isinstance(value, (str, int, bool)):
            raise ConfigError(f"{where}.where.{key} must be a string, integer, boolean or $reference")
        pairs.append((key, value))
    return Requirement(ref=ref, state=state, where=tuple(pairs))


def parse_kinds(raw: Any) -> dict[str, Kind]:
    """Validate the root ``[kinds]`` table into immutable declarations."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError("kinds must be a table")
    out: dict[str, Kind] = {}
    for kind_name, table in raw.items():
        where = f"kinds.{_name(kind_name, 'kind name')}"
        _keys(table, {"states", "initial", "create", "fields", "transitions", "created_event"}, where)
        states = _names(table.get("states"), f"{where}.states")
        initial = _name(table.get("initial"), f"{where}.initial")
        if initial not in states:
            raise ConfigError(f"{where}.initial must be one of its states")
        create = _principals(table.get("create"), f"{where}.create", object_refs=False)
        fields_raw = table.get("fields", {})
        if not isinstance(fields_raw, Mapping):
            raise ConfigError(f"{where}.fields must be a table")
        fields = {_name(n, f"{where}.fields"): _field(n, f, f"{where}.fields.{n}") for n, f in fields_raw.items()}
        if "holder" in fields and fields["holder"].type != "principal":
            raise ConfigError(f"{where}.fields.holder must have type principal")
        created_event = _name(table.get("created_event", f"{kind_name}.created"), f"{where}.created_event", EVENT)
        transitions: dict[str, Transition] = {}
        rows = table.get("transitions", [])
        if not isinstance(rows, list):
            raise ConfigError(f"{where}.transitions must be an array of tables")
        for index, row in enumerate(rows):
            at = f"{where}.transitions[{index}]"
            _keys(row, {"name", "from", "to", "by", "fields", "requires", "emits"}, at)
            name = _name(row.get("name"), f"{at}.name")
            if name in transitions:
                raise ConfigError(f"{at}.name duplicates {name}")
            sources_raw = row.get("from")
            sources = ("*",) if sources_raw == ["*"] else _names(sources_raw, f"{at}.from")
            if sources != ("*",) and set(sources) - set(states):
                raise ConfigError(f"{at}.from names undeclared states")
            to = _name(row.get("to"), f"{at}.to")
            if to not in states:
                raise ConfigError(f"{at}.to names an undeclared state")
            by = _principals(row.get("by"), f"{at}.by", object_refs=True)
            if "holder" in by and "holder" not in fields:
                raise ConfigError(f"{at}.by names holder but the kind has no holder field")
            settable = tuple(_name(f, f"{at}.fields") for f in row.get("fields", []))
            if set(settable) - set(fields):
                raise ConfigError(f"{at}.fields names undeclared fields")
            requires = tuple(_requirement(r, f"{at}.requires[{i}]", fields, None)
                             for i, r in enumerate(row.get("requires", [])))
            emits = _name(row.get("emits", f"{kind_name}.{name}"), f"{at}.emits", EVENT)
            transitions[name] = Transition(name, sources, to, by, settable, requires, emits)
        out[kind_name] = Kind(kind_name, states, initial, create, fields, transitions, created_event)
    for kind in out.values():
        for field in kind.fields.values():
            for target in (field.kinds or (field.item.kinds if field.item else ())):
                if target not in out:
                    raise ConfigError(f"kinds.{kind.name}.fields.{field.name} refers to undeclared kind {target}")
        for transition in kind.transitions.values():
            for requirement in transition.requires:
                if requirement.state and requirement.ref is not None:
                    targets = kind.fields[requirement.ref].kinds or tuple(out)
                    known = set().union(*(out[t].states for t in targets))
                    if set(requirement.state) - known:
                        raise ConfigError(f"kinds.{kind.name}.transitions.{transition.name}: "
                                          "requirement names states no referenced kind declares")
    return out


def parse_subscriptions(raw: Any, where: str) -> tuple[str, ...]:
    """Validate a role's ``subscribe`` list of event-name patterns."""
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list of event patterns")
    return tuple(_name(item, where, PATTERN) for item in raw)
