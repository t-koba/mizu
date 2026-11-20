"""Shared immutable work-item seed construction, independent of persistence."""
from __future__ import annotations

import datetime as dt
import re

from .errors import Denied
from .smoke_refs import validate_refs
from .work_item_waits import stored_wait
from .fs import canonical

ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
DESCRIPTION_UNSET = object()
SCOPE_UNSET = object()
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}
#: Seed shapes: the base fields, an optional description, an optional
#: scope, or both. A seed written before the scope existed keeps the older
#: shape and stays readable history.
SEED_SHAPES = (frozenset(SEED_FIELDS),
               frozenset(SEED_FIELDS | {"description"}),
               frozenset(SEED_FIELDS | {"scope"}),
               frozenset(SEED_FIELDS | {"description", "scope"}))


#: Bound of a work item's opaque origin reference.
MAX_ORIGIN_CHARS = 512


def validate_origin(origin, *, stored: bool = False) -> None:
    """An origin is an opaque reference: a message id or free text, or None.

    New items take a nonblank string of at most ``MAX_ORIGIN_CHARS``
    characters. Stored items keep their origin as written, which may be a
    small JSON object; it is shown as is and never interpreted.
    """
    if origin is None:
        return
    if isinstance(origin, str):
        if not 1 <= len(origin) <= MAX_ORIGIN_CHARS or not origin.strip() or "\x00" in origin:
            raise Denied(f"A work-item origin must be 1-{MAX_ORIGIN_CHARS} nonblank characters")
        return
    if stored and isinstance(origin, dict):
        try:
            if len(canonical(origin)) <= MAX_ORIGIN_CHARS:
                return
        except (TypeError, ValueError, RecursionError):
            pass
    raise Denied("Invalid work-item origin")


def validate_text(value, label: str) -> None:
    """Validate one bounded nonblank seed text (the description)."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 4000
            or not value.strip() or "\x00" in value):
        raise Denied(f"Work-item {label} must be 1-4000 nonblank characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Denied(f"Work-item {label} must be valid UTF-8") from None


def validate_scope(value) -> None:
    """Validate one scope statement (1-4000 nonblank characters)."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 4000
            or not value.strip() or "\x00" in value):
        raise Denied("A work-item scope must be 1-4000 nonblank characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Denied("A work-item scope must be valid UTF-8") from None


def validate_seed(seed, *, imported=False) -> dict:
    """Validate an immutable seed; an imported seed may start blocked on a wait."""
    if not isinstance(seed, dict):
        raise Denied("Invalid work-item seed fields")
    blocked = imported and "wait" in seed
    fields = frozenset(set(seed) - ({"wait"} if blocked else set()) - {"smoke_refs"})
    if "smoke_refs" in seed:
        validate_refs(seed["smoke_refs"])
    if fields not in SEED_SHAPES:
        raise Denied("Invalid work-item seed fields")
    if blocked:
        stored_wait(seed["wait"])
    if "description" in seed:
        validate_text(seed["description"], "description")
    if "scope" in seed:
        validate_scope(seed["scope"])
    validate_origin(seed["origin"], stored=True)
    try:
        stamp = dt.datetime.fromisoformat(seed["created_at"])
    except (TypeError, ValueError):
        raise Denied("Invalid work-item seed timestamp") from None
    if (not isinstance(seed["id"], str) or ITEM_ID.fullmatch(seed["id"]) is None
            or not isinstance(seed["title"], str) or not 1 <= len(seed["title"]) <= 200
            or type(seed["rev"]) is not int or seed["rev"] != 1
            or seed["status"] != ("blocked" if blocked else "open") or seed["holder"] is not None
            or stamp.tzinfo is None or seed["updated_at"] != seed["created_at"]):
        raise Denied("Invalid work-item seed")
    return seed


def make_seed(item_id: str, title: str, origin, stamp: str, *,
              description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, smoke_refs=None) -> dict:
    # Absence is part of the immutable v1 shape, not a null description or a
    # null scope: a seed states a scope only when one was given.
    seed = {"id": item_id, "title": title, "origin": origin,
            "status": "open", "rev": 1, "holder": None,
            "created_at": stamp, "updated_at": stamp}
    if smoke_refs is not None:
        seed["smoke_refs"] = validate_refs(smoke_refs)
    if description is not DESCRIPTION_UNSET:
        seed["description"] = description
    if scope is not SCOPE_UNSET:
        seed["scope"] = scope
    return validate_seed(seed)
