"""Insight tool orchestration for Context handlers."""
from __future__ import annotations

from .errors import Denied
from .fs import PREVIEW_BYTES, page, write_json

#: Explicit paged insight retrieval may walk the whole pending set (tool
#: page limit, same bound as `mizu insight list`); prompt
#: selection stays bounded by `[limits] pending_insights`.
INSIGHT_RETRIEVAL_LIMIT = 1000


def insights(context, args: dict) -> dict:
    if args.get("id"):
        record = context.project.insights.read(args["id"])
        # The original submitter may follow an accepted proposal into its
        # current item without receiving a separate work-item capability.
        # This is a read-only projection; all item validation remains in
        # WorkItems, and list paths/non-owners keep their existing shape.
        if record.get("source") == context.role.name:
            status = context.project.work_items.submitter_status(record["id"])
            if status is not None:
                record["work_item_status"] = status
        return record
    # Prompt selection stays at pending_insights; explicit retrieval pages
    # a stable offset-independent selection capped at INSIGHT_RETRIEVAL_LIMIT,
    # so overflow items remain discoverable and small pages enumerate each
    # item once. An explicit action instead pages decided insights (accepted
    # work stays retrievable after it leaves the pending projection).
    action = args.get("action")
    if action is not None:
        if action not in ("accept", "modify", "defer", "reject"):
            raise Denied("Insight listing needs a supported action: accept, modify, defer, or reject")
    projection = context.project.insights.projection(
        limit=INSIGHT_RETRIEVAL_LIMIT, pending=action is None, action=action)
    response = page(projection['items'],args.get('offset',0),args.get('limit',1000),maximum=PREVIEW_BYTES*2)
    response['insights'] = response.pop('items')
    response['total'] = projection['total']
    response['selection_truncated'] = projection['truncated']
    return response


def decide(context, args: dict) -> dict:
    return context.project.insights.decide(args["id"], args["action"], args["reason"],
                                        args.get("revisit", ""), context.run_dir.name,
                                        wait=args.get("wait"),
                                        expected_rev=args["rev"], work_item_intent=args.get("work_item_intent"),
                                        scope=args.get("scope"), work_items=args.get("work_items"))


def submit_insight(context, args: dict) -> dict:
    # Model submissions carry runtime-bound provenance only (source/run);
    # no origin field is exposed, so stored origin stays unknown and can
    # never elevate to operator authority.
    return context.project.insights.submit(source=context.role.name, title=args["title"], body=args["body"],
                                         base_snapshot=context.snapshot["id"], run=context.run_dir.name,
                                         origin=None)


def revise_insight(context, args: dict) -> dict:
    # Owner-only revision: source is fixed to the caller's role and the
    # mechanism (insights.revise) enforces the source match plus
    # compare-and-swap on the read rev. The revision is rebased onto the
    # current snapshot, like a fresh submission about the current tree.
    return context.project.insights.revise(args["id"], source=context.role.name, title=args["title"],
                                         body=args["body"], base_snapshot=context.snapshot["id"],
                                         run=context.run_dir.name, expected_rev=args["rev"])


def withdraw_insight(context, args: dict) -> dict:
    # Owner-only withdrawal: source is fixed to the caller's role and the
    # mechanism enforces source match plus compare-and-swap on the read rev.
    # Hiding a current accept/modify additionally needs the supplemental
    # withdraw_decided grant; the operator CLI passes no such permission.
    capabilities = set(context.role.capabilities)
    allow_decided = ("withdraw_insight" in capabilities
                     and "withdraw_decided" in capabilities)
    return context.project.insights.withdraw(args["id"], source=context.role.name, reason=args["reason"],
                                           expected_rev=args["rev"], run=context.run_dir.name,
                                           allow_decided=allow_decided)


def attach_evidence(context, args: dict) -> dict:
    # Attributed annotation of another insight: source is fixed to the
    # caller's role and the mechanism (insights.attach_evidence) binds to
    # the read rev without changing title, body, or rev, so ownership and
    # revise/withdraw rights stay with the submitter only.
    record = context.project.insights.attach_evidence(
        args["id"], source=context.role.name, text=args["text"],
        run=context.run_dir.name, expected_rev=args["rev"], smoke_refs=args.get("smoke_refs"))
    write_json(context.run_dir / "evidence-attach.json",
               {"insight": record["id"], "rev": record["rev"],
                "author": context.role.name, "run": context.run_dir.name,
                "evidence_count": len(record.get("evidence") or [])})
    return record
