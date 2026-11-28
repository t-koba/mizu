"""Queries over declared typed relations, evaluated within explicit bounds.

A schema names each relation and its typed fields. A query is plain data in a
fixed grammar: field comparisons, boolean combinations and nested existence
checks that join rows on equal field values. Evaluation is pure over the rows
supplied by the caller. A caller-supplied visibility function selects the rows
and fields available to that caller; it runs before any comparison, ordering
or output shaping, so a value that was not exposed cannot affect a result.
Caller-named ordering requires visible fields; automatic tiebreakers use only
fields present in a row's visible view and skip the rest.

``max_rows`` is an internal mechanism limit on authorized rows examined per
call, including rows examined by nested checks; a row denied by ``visible``
consumes none of it. The bound limits work, not caller-visible data: the
response holds only rows, the next cursor, whether more rows follow and the
matching count when requested. Relation sequences are bounded in-memory
candidates supplied by the caller; the eventual kernel supplies only
authorized candidates, with index slices making that pre-filter bounded.
Each call also emits at most ``max_bytes`` of serialized rows. Exhausting the
authorized-row bound raises; exhausting the byte bound yields a continuation
cursor so caller-driven paging never truncates a result silently.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import unicodedata
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from ..errors import Denied, LimitExceeded

#: Mechanism bounds; none is an operator setting.
MAX_NAME_CHARS = 64
MAX_RELATIONS = 64
MAX_FIELDS = 64
MAX_VALUE_CHARS = 65536
MAX_ROW_BYTES = 256 * 1024
MAX_IN_VALUES = 64
MAX_ARGS = 32
MAX_ON = 32
MAX_DEPTH = 8
MAX_PATTERN_CHARS = 128
MAX_QUANTIFIERS = 2
MAX_ORDER = 8
# Deterministic prefix when a keyless relation has neither seq nor id.
MAX_FALLBACK_TIE = 4
MAX_SELECT = 64
MAX_LIMIT = 4096
MAX_CALL_ROWS = 65536
MAX_CALL_BYTES = 64 * 1024 * 1024
DEFAULT_LIMIT = 256
INTEGER_LIMIT = (1 << 63) - 1

_TYPES = frozenset({"str", "int", "bool", "datetime"})
_COMPARISONS = frozenset({"eq", "lt", "le", "gt", "ge"})
_BOOLEANS = frozenset({"and", "or"})
_JOINS = frozenset({"exists", "not_exists"})
_QUERY_KEYS = frozenset({"relation", "where", "select", "order", "limit", "count"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BACKREF = re.compile(r"\\[1-9]")


def _identifier(value: Any, where: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_NAME_CHARS
            or _IDENT.fullmatch(value) is None):
        raise Denied(f"{where} must be a bounded identifier")
    return value


def _text(value: Any, where: str, maximum: int = MAX_VALUE_CHARS) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or any(unicodedata.category(char) == "Cc" for char in value)):
        raise Denied(f"{where} must be a single-line string of at most {maximum} characters")
    return value


def _check_value(value: Any, type_name: str, where: str) -> None:
    if type_name == "str":
        _text(value, where)
    elif type_name == "int":
        if type(value) is not int or not -INTEGER_LIMIT <= value <= INTEGER_LIMIT:
            raise Denied(f"{where} must be a bounded integer")
    elif type_name == "bool":
        if type(value) is not bool:
            raise Denied(f"{where} must be a boolean")
    else:
        if not isinstance(value, dt.datetime):
            raise Denied(f"{where} must be a datetime")
        try:
            aware = value.utcoffset() is not None
        except Exception:
            aware = False
        if not aware:
            raise Denied(f"{where} must be a timezone-aware datetime")


def _literal(value: Any, type_name: str, where: str) -> Any:
    """Coerce a JSON-like scalar to the declared field type, or refuse it."""
    if type_name == "datetime" and isinstance(value, str):
        raw = _text(value, where)
        try:
            value = dt.datetime.fromisoformat(raw)
        except ValueError as exc:
            raise Denied(f"{where} is not a datetime literal") from exc
    _check_value(value, type_name, where)
    return value


def _dump(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    raise Denied("Value has no bounded serialization")


def _serialize(row: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False,
                          default=_dump).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise Denied("Row cannot be serialized") from exc


@dataclasses.dataclass(frozen=True)
class Relation:
    """One declared relation: field names to type names, with an optional key field."""
    name: str
    fields: Mapping[str, str]
    key: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, "relation name")
        if not isinstance(self.fields, Mapping) or not 0 < len(self.fields) <= MAX_FIELDS:
            raise Denied("Relation fields must be a bounded table")
        for field, type_name in self.fields.items():
            _identifier(field, "field name")
            if not isinstance(type_name, str) or type_name not in _TYPES:
                raise Denied(f"Field {field!r} has an unknown type")
        if self.key is not None:
            _identifier(self.key, "key field")
            if self.key not in self.fields:
                raise Denied("Key field is not declared")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


def _schemas(schemas: Any) -> Mapping[str, Relation]:
    if not isinstance(schemas, Mapping) or not 0 < len(schemas) <= MAX_RELATIONS:
        raise Denied("Schemas must be a bounded table")
    for name, relation in schemas.items():
        _identifier(name, "schema name")
        if not isinstance(relation, Relation) or relation.name != name:
            raise Denied(f"Schema {name!r} is not a matching declaration")
    return schemas


def _row(raw: Any, relation: Relation, where: str, *, subset: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise Denied(f"{where} must be a table")
    declared = set(relation.fields)
    keys = set(raw)
    valid = keys <= declared if subset else keys == declared
    if not valid:
        raise Denied(f"{where} fields do not match the declaration")
    for field, value in raw.items():
        _check_value(value, relation.fields[field], f"{where}.{field}")
    if len(_serialize(raw)) > MAX_ROW_BYTES:
        raise Denied(f"{where} exceeds its byte bound")
    return raw


def _visible_field(row: Mapping[str, Any], field: str) -> Any:
    if field not in row:
        raise Denied(f"Field {field!r} is not visible")
    return row[field]


class _Budget:
    """Per-call count of authorized rows examined, including nested relations."""
    __slots__ = ("max_rows", "examined")

    def __init__(self, max_rows: int):
        self.max_rows = max_rows
        self.examined = 0

    def take(self) -> None:
        if self.examined >= self.max_rows:
            raise LimitExceeded(f"Query exceeded the bound of {self.max_rows} examined rows")
        self.examined += 1


def _authorized(raw: Any, name: str, relation: Relation,
                visible: Callable[[str, dict], dict | None], budget: _Budget) -> dict | None:
    _row(raw, relation, "declared row")
    try:
        view = visible(name, raw)
    except Exception as exc:
        raise Denied("Visibility check failed") from exc
    if view is None:
        return None
    checked = _row(view, relation, "visible row", subset=True)
    budget.take()
    return checked


def _pattern(raw: Any) -> re.Pattern[str]:
    """Compile a conservative pattern: no groups, backrefs or nested repeats."""
    pattern = _text(raw, "regex pattern", MAX_PATTERN_CHARS)
    if "(" in pattern or ")" in pattern or _BACKREF.search(pattern):
        raise Denied("Regex pattern uses a refused construct")
    if sum(pattern.count(char) for char in "*+?{}") > MAX_QUANTIFIERS:
        raise Denied("Regex pattern has too many repetitions")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise Denied("Regex pattern is invalid") from exc


def _compile(expr: Any, schemas: Mapping[str, Relation], name: str, depth: int) -> tuple:
    if depth >= MAX_DEPTH:
        raise Denied("Expression nesting exceeds the bound")
    if not isinstance(expr, dict):
        raise Denied("Expression must be a table")
    op = expr.get("op")
    if not isinstance(op, str):
        raise Denied("Expression has no operation")
    relation = schemas[name]
    if op in _COMPARISONS:
        if set(expr) != {"op", "field", "value"}:
            raise Denied(f"Operation {op!r} has unexpected keys")
        field = _identifier(expr["field"], "predicate field")
        if field not in relation.fields:
            raise Denied(f"Field {field!r} is not declared")
        return (op, field, _literal(expr["value"], relation.fields[field], f"{op} value"))
    if op == "between":
        if set(expr) != {"op", "field", "low", "high"}:
            raise Denied("between has unexpected keys")
        field = _identifier(expr["field"], "predicate field")
        if field not in relation.fields:
            raise Denied(f"Field {field!r} is not declared")
        kind = relation.fields[field]
        low = _literal(expr["low"], kind, "between low")
        high = _literal(expr["high"], kind, "between high")
        if high < low:
            raise Denied("between range is empty")
        return (op, field, low, high)
    if op == "in":
        if set(expr) != {"op", "field", "values"}:
            raise Denied("in has unexpected keys")
        field = _identifier(expr["field"], "predicate field")
        if field not in relation.fields:
            raise Denied(f"Field {field!r} is not declared")
        values = expr["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_IN_VALUES:
            raise Denied(f"in values must be a list of at most {MAX_IN_VALUES} items")
        kind = relation.fields[field]
        return (op, field, frozenset(_literal(item, kind, "in value") for item in values))
    if op == "regex":
        if set(expr) != {"op", "field", "pattern"}:
            raise Denied("regex has unexpected keys")
        field = _identifier(expr["field"], "predicate field")
        if field not in relation.fields or relation.fields[field] != "str":
            raise Denied("regex requires a declared string field")
        return (op, field, _pattern(expr["pattern"]))
    if op in _BOOLEANS:
        if set(expr) != {"op", "args"}:
            raise Denied(f"Operation {op!r} has unexpected keys")
        args = expr["args"]
        if not isinstance(args, list) or not 1 <= len(args) <= MAX_ARGS:
            raise Denied(f"{op} args must be a list of at most {MAX_ARGS} items")
        return (op, tuple(_compile(item, schemas, name, depth + 1) for item in args))
    if op == "not":
        if set(expr) != {"op", "arg"}:
            raise Denied("not has unexpected keys")
        return (op, _compile(expr["arg"], schemas, name, depth + 1))
    if op in _JOINS:
        allowed = {"op", "relation", "on"} | ({"where"} if "where" in expr else set())
        if set(expr) != allowed:
            raise Denied(f"Operation {op!r} has unexpected keys")
        other = _identifier(expr["relation"], "joined relation")
        if other not in schemas:
            raise Denied(f"Relation {other!r} is not declared")
        on = expr["on"]
        if not isinstance(on, Mapping) or not 1 <= len(on) <= MAX_ON:
            raise Denied(f"{op} on must be a table of at most {MAX_ON} equalities")
        pairs = []
        for left, right in on.items():
            left = _identifier(left, "left join field")
            right = _identifier(right, "right join field")
            if left not in relation.fields or right not in schemas[other].fields:
                raise Denied("Join names an undeclared field")
            pairs.append((left, right))
        nested = (_compile(expr["where"], schemas, other, depth + 1)
                  if "where" in expr else None)
        return (op, other, tuple(pairs), nested)
    raise Denied(f"Unknown operation {op!r}")


def _compile_query(query: Any, schemas: Mapping[str, Relation]) -> dict:
    if not isinstance(query, dict) or set(query) - _QUERY_KEYS or "relation" not in query:
        raise Denied("Query has unexpected or missing keys")
    name = _identifier(query["relation"], "query relation")
    if name not in schemas:
        raise Denied(f"Relation {name!r} is not declared")
    relation = schemas[name]
    where = _compile(query["where"], schemas, name, 1) if "where" in query else None
    select: list[str] = []
    if "select" in query:
        raw = query["select"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SELECT:
            raise Denied(f"select must be a list of at most {MAX_SELECT} fields")
        for item in raw:
            field = _identifier(item, "select field")
            if field not in relation.fields or field in select:
                raise Denied("select names an undeclared or repeated field")
            select.append(field)
    order: list[tuple[str, bool]] = []
    seen: set[str] = set()
    if "order" in query:
        raw = query["order"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ORDER:
            raise Denied(f"order must be a list of at most {MAX_ORDER} fields")
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"field", "desc"}:
                raise Denied("order items name a field and a direction")
            field = _identifier(item["field"], "order field")
            if field not in relation.fields or field in seen or type(item["desc"]) is not bool:
                raise Denied("order names an undeclared or repeated field")
            seen.add(field)
            order.append((field, item["desc"]))
    limit = query.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise Denied("limit must be a positive bounded integer")
    count = query.get("count", False)
    if type(count) is not bool:
        raise Denied("count must be a boolean")
    candidates = ([relation.key] if relation.key is not None
                  else [field for field in ("seq", "id") if field in relation.fields])
    if not candidates:
        candidates = sorted(relation.fields)[:MAX_FALLBACK_TIE]
    tie: list[str] = []
    for field in candidates:
        if field in seen:
            continue
        # Explicit, automatic and position slots share one key bound.
        if len(order) + len(tie) + 1 >= MAX_ORDER:
            break
        tie.append(field)
        seen.add(field)
    return {"relation": name, "where": where, "select": tuple(select),
            "order": tuple(order), "tie": tuple(tie),
            "limit": limit, "count": count}


def _sort_value(value: Any, type_name: str) -> Any:
    if type_name == "datetime":
        return value.astimezone(dt.timezone.utc).isoformat()
    if type_name == "bool":
        return int(value)
    return value


def _tie_slot(row: Mapping[str, Any], field: str, type_name: str) -> tuple[int, Any]:
    """A comparable slot for an automatic tie field, using only visible values."""
    if field not in row:
        return (0, "")
    return (1, _sort_value(row[field], type_name))


def _rows(relations: Mapping[str, Sequence[dict]], name: str) -> Sequence[dict]:
    try:
        rows = relations.get(name)
    except Exception as exc:
        raise Denied("Relation table is unreadable") from exc
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise Denied(f"Relation {name!r} has no row sequence")
    return rows


def _eval(node: tuple, row: dict, schemas: Mapping[str, Relation],
          relations: Mapping[str, Sequence[dict]], visible: Callable[[str, dict], dict | None],
          budget: _Budget) -> bool:
    op = node[0]
    if op == "eq":
        return _visible_field(row, node[1]) == node[2]
    if op in ("lt", "le", "gt", "ge"):
        left, right = _visible_field(row, node[1]), node[2]
        if op == "lt":
            return left < right
        if op == "le":
            return left <= right
        if op == "gt":
            return left > right
        return left >= right
    if op == "between":
        return node[2] <= _visible_field(row, node[1]) <= node[3]
    if op == "in":
        return _visible_field(row, node[1]) in node[2]
    if op == "regex":
        value = _visible_field(row, node[1])
        return node[2].search(value) is not None
    if op == "and":
        return all(_eval(item, row, schemas, relations, visible, budget) for item in node[1])
    if op == "or":
        return any(_eval(item, row, schemas, relations, visible, budget) for item in node[1])
    if op == "not":
        return not _eval(node[1], row, schemas, relations, visible, budget)
    _, other, pairs, nested = node
    other_rows = _rows(relations, other)
    found = False
    for raw in other_rows:
        peer = _authorized(raw, other, schemas[other], visible, budget)
        if peer is None:
            continue
        if (all(_visible_field(row, left) == _visible_field(peer, right)
                for left, right in pairs)
                and (nested is None or _eval(nested, peer, schemas, relations, visible, budget))):
            found = True
            break
    return found if op == "exists" else not found


def evaluate(relations: Mapping[str, Sequence[dict]], schemas: Mapping[str, Relation],
             query: dict, *, visible: Callable[[str, dict], dict | None],
             max_rows: int, max_bytes: int, cursor: int = 0) -> dict:
    """Evaluate one bounded page of a query over caller-supplied relations.

    ``visible`` is applied to every row examined, at every nesting level,
    before that row takes part in a check. It returns the permitted fields or
    ``None`` when the row itself is not permitted. ``max_rows`` bounds
    authorized rows examined in this call, never a response field. The result
    holds ``rows``, the next ``cursor``, whether ``more`` rows follow and the
    matching ``count`` when requested.
    """
    checked = _schemas(schemas)
    plan = _compile_query(query, checked)
    if not isinstance(relations, Mapping):
        raise Denied("Relations must be a table")
    if not callable(visible):
        raise Denied("Visibility must be callable")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_CALL_ROWS:
        raise Denied("max_rows must be a positive bounded integer")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CALL_BYTES:
        raise Denied("max_bytes must be a positive bounded integer")
    if type(cursor) is not int or not 0 <= cursor <= MAX_CALL_ROWS:
        raise Denied("cursor must be a nonnegative bounded integer")

    name = plan["relation"]
    relation = checked[name]
    budget = _Budget(max_rows)
    matched: list[dict] = []
    for raw in _rows(relations, name):
        row = _authorized(raw, name, relation, visible, budget)
        if row is None:
            continue
        if plan["where"] is None or _eval(plan["where"], row, checked, relations, visible, budget):
            matched.append(row)

    keyed = []
    for position, row in enumerate(matched):
        slots = tuple(_sort_value(_visible_field(row, field), relation.fields[field])
                      for field, _ in plan["order"])
        slots += tuple(_tie_slot(row, field, relation.fields[field])
                       for field in plan["tie"])
        # A full explicit key leaves no room for a position slot; the stable
        # sort passes already preserve input order for rows that tie on it.
        if len(slots) < MAX_ORDER:
            slots += (position,)
        keyed.append(slots)
    ordered = sorted(zip(keyed, matched, strict=True), key=lambda item: item[0])
    for position in range(len(plan["order"]) - 1, -1, -1):
        order = plan["order"][position]
        ordered.sort(key=lambda item, i=position: item[0][i], reverse=order[1])
    if cursor > len(ordered):
        raise Denied("cursor is beyond the result set")

    chosen = [row for _, row in ordered[cursor:cursor + plan["limit"]]]
    rows: list[dict] = []
    used = 0
    for row in chosen:
        projected = ({field: _visible_field(row, field) for field in plan["select"]}
                     if plan["select"] else dict(row))
        size = len(_serialize(projected))
        if used + size > max_bytes:
            break
        rows.append(projected)
        used += size
    if not rows and chosen:
        raise LimitExceeded("Query byte bound cannot hold one row")
    next_cursor = cursor + len(rows)
    more = len(rows) < len(chosen) or next_cursor < len(ordered)
    return {"rows": rows, "next": next_cursor, "more": more,
            "count": len(ordered) if plan["count"] else None}
