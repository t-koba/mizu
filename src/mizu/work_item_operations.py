"""Work-item tool orchestration for Context handlers."""
from __future__ import annotations

from .errors import Denied


def read(context, args: dict) -> dict:
    if "id" in args:
        if "cursor" in args:
            raise Denied("Work-item by-ID history cannot be combined with open-page cursor")
        return context.project.work_items.explain(
            args["id"], after_rev=args.get("after_rev", 0), limit=args.get("limit", 25))
    if "after_rev" in args:
        raise Denied("Work-item after_rev requires id")
    return context.project.work_items.page_open(cursor=args.get("cursor"),
                                                limit=args.get("limit", 25))


def legacy_read(context, args: dict) -> dict:
    return context.project.work_items.page_legacy(cursor=args.get("cursor"),
                                                  limit=args.get("limit", 25))


def reconcile(context, args: dict) -> dict:
    return context.project.work_items.reconcile(
        args["event_id"], expected_action=args["expected_action"],
        expected_rev=args.get("expected_rev"), outcome=args["outcome"],
        reason=args["reason"], title=args.get("title"), actor=context.role.name)


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
        snapshot_id=args.get("snapshot_id"), scope=args.get("scope"), smoke_refs=args.get("smoke_refs"))
