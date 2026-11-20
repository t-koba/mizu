"""Work-item tool orchestration for Context handlers."""
from __future__ import annotations

from .errors import Denied


_CLOSED_FILTERS = ("status", "origin", "closed_after", "closed_before")


def read(context, args: dict) -> dict:
    closed = args.get("closed", False)
    if type(closed) is not bool:
        raise Denied("Work-item closed selector must be a boolean")
    filters = {key: args[key] for key in _CLOSED_FILTERS if key in args}
    if "id" in args:
        if "cursor" in args:
            raise Denied("Work-item by-ID history cannot be combined with open-page cursor")
        if closed or filters:
            raise Denied("Work-item by-ID history cannot select a closed page")
        return context.project.work_items.explain(
            args["id"], after_rev=args.get("after_rev", 0), limit=args.get("limit", 25))
    if closed:
        if "after_rev" in args:
            raise Denied("Work-item closed page cannot use by-ID after_rev")
        return context.project.work_items.page_closed(
            cursor=args.get("cursor"), limit=args.get("limit", 25), **filters)
    if filters:
        raise Denied("Work-item closed filters require closed=true")
    if "after_rev" in args:
        raise Denied("Work-item after_rev requires id")
    return context.project.work_items.page_open(cursor=args.get("cursor"),
                                                limit=args.get("limit", 25))


def create(context, args: dict) -> dict:
    return context.project.work_items.create(
        idempotency_key=args["idempotency_key"], title=args["title"],
        origin=args.get("origin"), actor=context.role.name, smoke_refs=args.get("smoke_refs"),
        **({"description": args["description"]} if "description" in args else {}),
        **({"scope": args["scope"]} if "scope" in args else {}))


def transition(context, args: dict) -> dict:
    return context.project.work_items.transition(
        args["id"], expected_rev=args["expected_rev"], status=args["status"],
        actor=context.role.name, reason=args.get("reason"), wait=args.get("wait"),
        evidence=args.get("evidence"), snapshot_id=args.get("snapshot_id"), scope=args.get("scope"), smoke_refs=args.get("smoke_refs"))


def reopen(context, args: dict) -> dict:
    # Also guard direct handler use; identity comes only from the runtime role.
    if "work_items_reopen" not in context.role.capabilities:
        raise Denied("Role has no capability: work_items_reopen")
    if context.role.name not in context.project.config.completion_evidence.reopen_roles:
        raise Denied("Role is not allowed by completion_evidence.reopen_roles")
    return context.project.work_items.reopen(
        args["id"], expected_rev=args["expected_rev"], reason=args["reason"], actor=context.role.name)
