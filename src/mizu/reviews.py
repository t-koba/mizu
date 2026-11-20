"""Typed review receipts, bound by the host to the reviewed code digest.

The observed cursor keeps the reviewed input snapshot as evidence alongside
its immutable review-receipt snapshot. Only a validated typed receipt marks
that code reviewed for scheduling and publication; an observed attempt without
a verdict remains pending. A same-code successor needs no new verdict, while a
changed digest does. Neither prose nor queue messages are approval evidence.
"""
from __future__ import annotations

import re
import time

from . import config
from .errors import Denied, LimitExceeded
from .fs import DIGEST, ID, loads_finite, safe_read, write_json
from .publication import Publication

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
        wake_generation=context.wake_generation,
        next_step=finished.get("next_step"), parent=source["id"], review=review)
    write_json(context.run_dir / "review.json",
               {"snapshot": receipt["id"], "review": review}, exclusive=True)
    return receipt


def cursor_review(project, config, expected_role: str, cursor, source):
    """Return the typed review validated for the source's code digest.

    expected_role is the trusted host-supplied scheduled or policy-selected
    holder, never a model argument. The reviewed snapshot remains evidence of
    what was read: the cursor, the review and the receipt's parent must name
    the same snapshot, but a later same-code snapshot does not invalidate it.
    The cursor is only complete when its immutable snapshot and create-only run
    receipt both validate. A no-verdict observed attempt is not a review.
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
    reviewed = review["reviewed_snapshot"]
    # Legacy cursors named the receipt in `snapshot`; current ones name the
    # reviewed source and hold the receipt separately. The receipt's parent
    # still binds both to the reviewed source.
    cursor_source = reviewed if has_receipt_cursor else receipt_id
    if (receipt.get("id") != receipt_id or cursor.get("review") != review
            or cursor.get("snapshot") != cursor_source or receipt.get("parent") != reviewed
            or cursor.get("code_digest") != code_digest
            or review["role"] != expected_role or receipt.get("run") != review["run"]
            or receipt.get("code_digest") != code_digest):
        raise Denied("Review does not bind this code digest")
    saved = loads_finite(safe_read(
        project.root, f"runs/{review['run']}/review.json", RECORD_BYTES))
    if saved != {"snapshot": receipt_id, "review": review}:
        raise Denied("Review run binding differs")
    return receipt, review, saved


def _current_reviews(context, holders):
    """Yield validated current receipts in configuration order, never history."""
    for holder in holders:
        try:
            cursor = loads_finite(safe_read(
                context.project.root, f"observed/{holder}.json", RECORD_BYTES))
            receipt, review, saved = cursor_review(
                context.project, context.config, holder, cursor, context.snapshot)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, Denied, LimitExceeded):
            continue
        yield receipt, review, saved


def publication_state(context) -> dict:
    """Describe recorded publication and review evidence without granting publication."""
    state = {"cause": "awaiting_review", "verdict": None, "reviewer": None}
    code_digest = context.snapshot["code_digest"]
    if code_digest is not None:  # None would list receipts without a digest filter.
        try:
            page = Publication.list_receipts(context.project.root, code_digest=code_digest,
                                             limit=1, policy=context.config.publication_evidence)
            # list_receipts filters by exact digest and returns only validated
            # receipts, so a match is conclusive; scan_stopped means only that
            # more may exist beyond the bound, never that a match is invalid.
            if page["publications"]:
                return {**state, "cause": "published"}
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, Denied, LimitExceeded):
            pass  # An unavailable receipt is not proof of publication.

    holders, required = config.publication_review_requirement(context.config.publication)
    if not holders or required < 1:
        return {**state, "cause": "review_not_configured"}

    approvals = set()
    rejecting_holder = None
    for _, review, _ in _current_reviews(context, holders):
        if review["verdict"] == "approve":
            approvals.add(review["role"])
            if len(approvals) >= required:
                return {**state, "cause": "approved_queued"}
        elif rejecting_holder is None:
            rejecting_holder = review["role"]
    if rejecting_holder is None:
        return state
    return {"cause": "changes_requested", "verdict": "changes_requested",
            "reviewer": rejecting_holder}


def require_approvals(context, code_digest) -> tuple[dict, ...]:
    """Consume selected holders' latest host-sealed approvals of this digest.

    Missing, unsafe, malformed, unauthorized or mismatched evidence contributes
    nothing; no scan/retry/fallback to older approvals. Human GO remains
    independently required by the caller. Reads are bounded per selected holder;
    model arguments select neither identity, reviewed snapshot nor digest.
    """
    holders, required = config.publication_review_requirement(context.config.publication)
    denied = "Publication requires an authorized typed approval for the reviewed code digest"
    if not holders or required < 1:
        raise Denied(denied)
    approvals = tuple(saved for receipt, review, saved in _current_reviews(context, holders)
                      if (review["verdict"] == "approve" and receipt["code_digest"] == code_digest
                          and context.snapshot["code_digest"] == code_digest))
    if len(approvals) < required:
        raise Denied(denied)
    return approvals
