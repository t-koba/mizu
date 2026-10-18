"""Shared immutable work-item seed construction, independent of persistence."""
from __future__ import annotations

import datetime as dt
import re

from .errors import Denied
from .fs import identifier

ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
DESCRIPTION_UNSET = object()
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}


def validate_origin(origin) -> None:
    if origin is None:
        return
    if not isinstance(origin, dict) or set(origin) not in (
            {"insight_id", "rev"}, {"work_item_id", "rev"}):
        raise Denied("Invalid work-item origin")
    if type(origin["rev"]) is not int or origin["rev"] < 1:
        raise Denied("Invalid work-item origin revision")
    if "insight_id" in origin:
        identifier(origin["insight_id"])
    elif not isinstance(origin["work_item_id"], str) or ITEM_ID.fullmatch(origin["work_item_id"]) is None:
        raise Denied("Invalid parent work-item ID")


def validate_seed(seed) -> dict:
    if not isinstance(seed, dict) or set(seed) not in (SEED_FIELDS, SEED_FIELDS | {"description"}):
        raise Denied("Invalid work-item seed fields")
    if "description" in seed:
        description = seed["description"]
        if (not isinstance(description, str) or not 1 <= len(description) <= 4000
                or not description.strip() or "\x00" in description):
            raise Denied("Work-item description must be 1-4000 nonblank characters")
        try:
            description.encode("utf-8")
        except UnicodeEncodeError:
            raise Denied("Work-item description must be valid UTF-8") from None
    validate_origin(seed["origin"])
    try:
        stamp = dt.datetime.fromisoformat(seed["created_at"])
    except (TypeError, ValueError):
        raise Denied("Invalid work-item seed timestamp") from None
    if (not isinstance(seed["id"], str) or ITEM_ID.fullmatch(seed["id"]) is None
            or not isinstance(seed["title"], str) or not 1 <= len(seed["title"]) <= 200
            or type(seed["rev"]) is not int or seed["rev"] != 1
            or seed["status"] != "open" or seed["holder"] is not None
            or stamp.tzinfo is None or seed["updated_at"] != seed["created_at"]):
        raise Denied("Invalid work-item seed")
    return seed


def make_seed(item_id: str, title: str, origin, stamp: str, *,
              description=DESCRIPTION_UNSET) -> dict:
    # Absence is part of the immutable v1 shape, not a null description.
    seed = {"id": item_id, "title": title, "origin": origin,
            "status": "open", "rev": 1, "holder": None,
            "created_at": stamp, "updated_at": stamp}
    if description is not DESCRIPTION_UNSET:
        seed["description"] = description
    return validate_seed(seed)
