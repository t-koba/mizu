"""Shared immutable work-item seed construction, independent of persistence."""
from __future__ import annotations

import datetime as dt
import re

from .errors import Denied
from .smoke_refs import validate_refs
from .work_item_waits import normalize_wait, stored_wait
from .fs import PREVIEW_BYTES, canonical, digest, identifier

ITEM_ID = re.compile(r"wi_([0-9a-f]{32})\Z")
DESCRIPTION_UNSET = object()
SCOPE_UNSET = object()
SEED_FIELDS = {"id", "title", "origin", "status", "rev", "holder", "created_at", "updated_at"}
#: Seed shapes: the base fields, an optional description, an optional accepted
#: scope, or both. A seed written before the scope existed keeps the older
#: shape and stays readable history.
SEED_SHAPES = (frozenset(SEED_FIELDS),
               frozenset(SEED_FIELDS | {"description"}),
               frozenset(SEED_FIELDS | {"scope"}),
               frozenset(SEED_FIELDS | {"description", "scope"}))


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
    """Validate one accepted-scope statement (1-4000 nonblank characters)."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 4000
            or not value.strip() or "\x00" in value):
        raise Denied("An accepted scope must be 1-4000 nonblank characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Denied("An accepted scope must be valid UTF-8") from None


def validate_seed(seed, *, decision_task=False) -> dict:
    if not isinstance(seed, dict):
        raise Denied("Invalid work-item seed fields")
    blocked = decision_task and "wait" in seed
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
    validate_origin(seed["origin"])
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
              description=DESCRIPTION_UNSET, scope=SCOPE_UNSET, wait=None, smoke_refs=None) -> dict:
    # Absence is part of the immutable v1 shape, not a null description or a
    # null scope: a seed states an accepted scope only when one was accepted.
    seed = {"id": item_id, "title": title, "origin": origin,
            "status": "open", "rev": 1, "holder": None,
            "created_at": stamp, "updated_at": stamp}
    if smoke_refs is not None:
        seed["smoke_refs"] = validate_refs(smoke_refs)
    if description is not DESCRIPTION_UNSET:
        seed["description"] = description
    if scope is not SCOPE_UNSET:
        seed["scope"] = scope
    if wait is not None:
        seed.update(status="blocked", wait=wait)
    return validate_seed(seed, decision_task=wait is not None)


MAX_DECISION_TASKS = 16


def validate_tasks(tasks) -> list:
    """Explicit task specs only: no prose parsing or task-specific scope."""
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_DECISION_TASKS:
        raise Denied("work_items must contain 1-16 task specifications")
    for task in tasks:
        if (not isinstance(task, dict)
                or set(task) not in ({"title", "description"}, {"title", "description", "wait"})):
            raise Denied("Each task requires title, description and optionally wait")
        if "wait" in task:
            validate_task_wait(task["wait"])
        title = task["title"]
        if (not isinstance(title, str) or not 1 <= len(title) <= 200
                or not title.strip() or "\x00" in title):
            raise Denied("Task title must be 1-200 nonblank characters")
        try:
            title.encode("utf-8")
        except UnicodeEncodeError:
            raise Denied("Task title must be valid UTF-8") from None
        validate_text(task["description"], "description")
    return tasks


def validate_task_wait(wait) -> None:
    """Validate raw task wait shape without capturing a mutable code baseline."""
    fields = {"deadline": {"kind", "at"}, "code_change": {"kind"},
              "insight_decided": {"kind", "insight"},
              "dependency": {"kind", "recipient", "requires"}}
    if (not isinstance(wait, dict) or not isinstance(wait.get("kind"), str)
            or set(wait) - {"smoke_refs"} != fields.get(wait["kind"], set())):
        raise Denied("Invalid decision task wait")
    if "smoke_refs" in wait:
        validate_refs(wait["smoke_refs"])
    # The shared normalizer validates values. Code-change has only its kind;
    # its baseline must be captured at commit, not during request/replay checks.
    if wait["kind"] != "code_change":
        normalize_wait(wait, "defer")


def task_id(key: str, tasks: list, index: int) -> str:
    # Bind each identity to the complete ordered scope, not just one member.
    return "wi_" + digest(canonical(["decision-task", key, tasks, index]))[:32]


def task_seeds(event: dict) -> list:
    """Validate a complete atomic task manifest, including every member ID."""
    tasks = validate_tasks(event.get("work_item_specs"))
    seeds = event.get("work_items")
    if (event.get("action") not in ("accept", "modify")
            or event.get("work_item_intent") != "create" or "work_item" in event
            or not isinstance(seeds, list) or len(seeds) != len(tasks)
            or len(canonical(event)) > PREVIEW_BYTES):
        raise Denied("Invalid decision task manifest")
    ids = [task_id(event["decision_key"], tasks, index) for index in range(len(tasks))]
    if len(set(ids)) != len(ids):
        raise Denied("Decision task IDs collide")
    specs_have_waits = any("wait" in task for task in tasks)
    if "work_item_waits" in event:
        waits = event["work_item_waits"]
        if (not specs_have_waits or not isinstance(waits, list)
                or len(waits) != len(tasks)):
            raise Denied("Invalid normalized decision task wait manifest")
    else:
        if specs_have_waits:
            raise Denied("Decision task wait manifest is missing")
        waits = [None] * len(tasks)
    for task, seed, item_id, normalized in zip(tasks, seeds, ids, waits):
        validate_seed(seed, decision_task=True)
        expected = make_seed(item_id, task["title"],
                             {"insight_id": event["id"], "rev": event["rev"]}, event["created_at"],
                             description=task["description"],
                             **({"smoke_refs": event["smoke_refs"]} if "smoke_refs" in event else {}),
                             **({"scope": event["scope"]} if "scope" in event else {}))
        if "wait" in task:
            normalized = stored_wait(normalized)
            if (normalized["kind"] != task["wait"]["kind"]
                    or (normalized["kind"] != "code_change"
                        and normalized != normalize_wait(task["wait"], "defer"))):
                raise Denied("Task wait does not match raw decision manifest")
            expected.update(status="blocked", wait=normalized)
        elif normalized is not None:
            raise Denied("Unexpected normalized wait for an open task")
        if seed != expected:
            raise Denied("Task seed does not match complete decision manifest")
    return seeds
