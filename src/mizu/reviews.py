"""Typed review receipts, bound only by the host to a reader's input snapshot.

The observed cursor keeps the reviewed input snapshot separate from its
immutable review-receipt snapshot. Only a validated typed receipt marks that
input reviewed for scheduling; an observed attempt without a verdict remains
pending. Neither prose, insights nor role-authored shared records are approval
evidence.
"""
from __future__ import annotations

import re
import time

from .errors import Denied, LimitExceeded
from .fs import DIGEST, ID, loads_finite, safe_read, write_json

RUN = re.compile(r"[0-9a-f]{32}\Z")
VERDICTS = frozenset({"approve", "changes_requested"})
#: Safety envelope for the small immutable receipt and existing observed cursor.
RECORD_BYTES = 4096


def authorized(config, role) -> bool:
    return ("review_verdict" in role.capabilities
            and role.workspace == "read" and "finish" in role.capabilities
            and "delegate" not in role.capabilities and "vcs_publish" not in role.capabilities)


def check(review) -> dict:
    if (not isinstance(review, dict)
            or set(review) != {"verdict", "reviewed_snapshot", "role", "run"}
            or not isinstance(review.get("verdict"), str)
            or review["verdict"] not in VERDICTS
            or not isinstance(review.get("reviewed_snapshot"), str)
            or not DIGEST.fullmatch(review["reviewed_snapshot"])
            or not isinstance(review.get("run"), str) or not RUN.fullmatch(review["run"])
            or not isinstance(review.get("role"), str) or not ID.fullmatch(review["role"])):
        raise Denied("Invalid typed review receipt")
    return review


def context_authorized(context) -> bool:
    return authorized(context.config, context.role) and not getattr(context, "ephemeral", False)


def prompt(context) -> dict:
    allowed = context_authorized(context)
    return {"review_authority": {"authorized": allowed,
                                 "reviewed_snapshot": context.snapshot["id"] if allowed else None}}


def finish(context, verdict):
    if not context_authorized(context):
        raise Denied("Review verdict requires the configured independent reviewer")
    return check({"verdict": verdict, "reviewed_snapshot": context.snapshot["id"],
                  "role": context.role.name, "run": context.run_dir.name})


def record(context, finished):
    """Create one immutable review snapshot and its create-only run receipt.

    Read-only runs normally update only their observed cursor. A review needs
    an immutable snapshot to bind the typed verdict, so this creates that single
    snapshot while retaining the ordinary reader wake/lifecycle metadata. The
    observed cursor commits it only after finalization succeeds.
    """
    review = finished.get("review")
    if review is None or finished["outcome"] == "bounded":
        return None
    check(review)
    source = context.snapshot
    captured = {key: source[key] for key in ("files", "code_digest", "skipped", "bytes")}
    receipt = context.project.snapshots.create(
        captured, goal=context.goal, state=finished.get("state", ""),
        run=context.run_dir.name, outcome=finished["outcome"], summary=finished["summary"],
        wake_at=(time.time() + finished["wait_seconds"]
                 if finished["outcome"] == "wait" else None),
        inbox_seen=context.inbox_seen, wake_generation=context.wake_generation,
        next_step=finished.get("next_step"), parent=source["id"], review=review)
    write_json(context.run_dir / "review.json",
               {"snapshot": receipt["id"], "review": review}, exclusive=True)
    return receipt


def cursor_review(project, config, expected_role: str, cursor, source):
    """Return the exact typed review represented by a source cursor.

    expected_role is the trusted host-supplied scheduled or policy-selected
    holder, never a model argument. The cursor is only complete when its
    immutable snapshot and create-only run receipt both validate. A no-verdict observed attempt is not a review.
    """
    role = config.roles.get(expected_role)
    if not expected_role or role is None or not authorized(config, role):
        raise Denied("No authorized reviewer")
    if not isinstance(cursor, dict) or not isinstance(source, dict):
        raise Denied("Invalid review cursor")
    source_id, code_digest = source.get("id"), source.get("code_digest")
    if (not isinstance(source_id, str) or not DIGEST.fullmatch(source_id)
            or not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest)):
        raise Denied("Invalid review source")
    has_receipt_cursor = "review_receipt" in cursor
    receipt_id = cursor.get("review_receipt") if has_receipt_cursor else cursor.get("snapshot")
    if not isinstance(receipt_id, str) or not DIGEST.fullmatch(receipt_id):
        raise Denied("Invalid review receipt reference")
    receipt = project.snapshots.get(receipt_id)
    review = check(receipt.get("review"))
    if (receipt.get("id") != receipt_id or cursor.get("review") != review
            or cursor.get("snapshot") != (source_id if has_receipt_cursor else receipt_id)
            or cursor.get("code_digest") != code_digest
            or review["role"] != expected_role or review["reviewed_snapshot"] != source_id
            or receipt.get("parent") != source_id or receipt.get("run") != review["run"]
            or receipt.get("code_digest") != code_digest):
        raise Denied("Review does not bind this exact source")
    saved = loads_finite(safe_read(
        project.root, f"runs/{review['run']}/review.json", RECORD_BYTES))
    if saved != {"snapshot": receipt_id, "review": review}:
        raise Denied("Review run binding differs")
    return receipt, review, saved


def require_approval(context, code_digest):
    """Consume only the latest host-sealed exact-snapshot typed fact.

    Missing, unsafe, malformed, unauthorized or mismatched evidence denies;
    no scan/retry/fallback to older approvals. Human GO remains independently
    required by the caller. Reads are bounded; model arguments select neither
    identity nor reviewed snapshot.
    """
    try:
        name = context.config.publication.reviewer_role
        if not name:
            raise Denied("No publication reviewer selected")
        cursor = loads_finite(safe_read(
            context.project.root, f"observed/{name}.json", RECORD_BYTES))
        source = context.snapshot
        receipt, review, saved = cursor_review(
            context.project, context.config, name, cursor, source)
        if (review["verdict"] != "approve" or receipt["code_digest"] != code_digest
                or source["code_digest"] != code_digest):
            raise Denied("Review does not approve this exact snapshot")
        return saved
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, Denied, LimitExceeded) as exc:
        raise Denied("Publication requires an authorized typed approval for the exact snapshot") from exc
