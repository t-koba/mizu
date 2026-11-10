"""Observable wait normalization and read-only liveness; no registration."""
from __future__ import annotations

import datetime as dt

from .errors import Denied, LimitExceeded
from .smoke_refs import validate_refs
from .fs import DIGEST, canonical, digest, identifier, now

#: Locally observable defer/task conditions, reevaluated from recorded state.
WAIT_KINDS = ("deadline", "code_change", "insight_decided", "dependency")


def _normalize_wait(wait, action: str, baseline=None) -> dict | None:
    """Normalize raw defer/work-item waits; capture code baseline only on creation."""
    if wait is None:
        return None
    if action != "defer":
        raise Denied("Structured waits may only be registered with a deferral")
    if not isinstance(wait, dict):
        raise Denied(f"Unsupported wait; supported kinds: {', '.join(WAIT_KINDS)}")
    kind = wait.get("kind")
    if kind not in WAIT_KINDS:
        raise Denied(f"Unsupported wait; supported kinds: {', '.join(WAIT_KINDS)}")
    if kind == "deadline":
        at = wait.get("at")
        try:
            moment = dt.datetime.fromisoformat(at) if isinstance(at, str) else None
        except (ValueError, TypeError):
            moment = None
        if moment is None or moment.tzinfo is None:
            raise Denied("A deadline wait needs a timezone-aware ISO timestamp in 'at'")
        return {"kind": kind, "at": moment.isoformat(timespec="seconds")}
    if kind == "insight_decided":
        target = wait.get("insight")
        if not isinstance(target, str) or not target:
            raise Denied("An insight_decided wait needs an 'insight' id")
        identifier(target)
        return {"kind": kind, "target": target}
    if kind == "dependency":
        recipient = wait.get("recipient")
        if not isinstance(recipient, str) or not recipient:
            raise Denied("A dependency wait needs a 'recipient' role")
        identifier(recipient)
        requires = wait.get("requires")
        if not isinstance(requires, dict):
            raise Denied("A dependency wait needs 'requires' with insight, rev, and action")
        target = requires.get("insight")
        if not isinstance(target, str) or not target:
            raise Denied("A dependency wait needs a 'requires.insight' id")
        identifier(target)
        required_rev = requires.get("rev")
        if type(required_rev) is not int or required_rev < 1:
            raise Denied("A dependency wait binds the required result revision in 'requires.rev'")
        required_action = requires.get("action")
        if required_action not in ("accept", "modify", "reject"):
            raise Denied("A dependency wait needs a substantive 'requires.action'")
        if set(wait) - {"kind", "recipient", "requires"}:
            raise Denied("A dependency wait takes only recipient and requires")
        if set(requires) - {"insight", "rev", "action"}:
            raise Denied("A dependency wait requires only insight, rev, and action")
        return {"kind": kind, "recipient": recipient,
                "requires": {"insight": target, "rev": required_rev, "action": required_action}}
    # code_change falls through: no fields beyond kind, baseline is the
    # current published digest.
    if set(wait) - {"kind"}:
        raise Denied("A code_change wait takes no fields")
    try:
        baseline = baseline()
    except (OSError, ValueError, TypeError, AttributeError):
        raise Denied("A code_change wait needs a published snapshot baseline")
    return {"kind": kind, "digest": baseline}



def normalize_record_wait(wait):
    """Exact immutable subject; this wait is deliberately work-item-only."""
    from .operator_records import _subject
    if (type(wait) is not dict or wait.get('kind') != 'record_closed'
            or set(wait) - {'smoke_refs'} != {'kind', 'subject'}):
        raise Denied('A record_closed wait requires only an exact record subject')
    subject = wait['subject']
    _subject(subject)
    if subject['type'] != 'record':
        raise Denied('A record_closed wait needs a record subject')
    result = {'kind': 'record_closed', 'subject': dict(subject)}
    if 'smoke_refs' in wait:
        result['smoke_refs'] = validate_refs(wait['smoke_refs'])
    return result


def _stored_wait(value) -> dict:
    """Validate normalized observable waits without rebasing code evidence."""
    if not isinstance(value, dict):
        raise Denied("Blocked work items require a structured wait")
    kind = value.get("kind")
    if kind == "record_closed":
        return normalize_record_wait(value)
    if kind == "code_change":
        baseline = value.get("digest")
        if set(value) != {"kind", "digest"} or not isinstance(baseline, str) or not DIGEST.fullmatch(baseline):
            raise Denied("Invalid stored code-change wait")
        return value
    raw = {"kind": kind, "insight": value.get("target")} if kind == "insight_decided" else value
    normalized = normalize_wait(raw, "defer")
    if value != normalized:
        raise Denied("Invalid normalized work-item wait")
    return normalized


def normalize_wait(wait, action, baseline=None):
    refs = wait.get("smoke_refs") if isinstance(wait, dict) else None
    raw = {k: v for k, v in wait.items() if k != "smoke_refs"} if isinstance(wait, dict) else wait
    result = _normalize_wait(raw, action, baseline)
    if refs is not None:
        result = {**result, "smoke_refs": validate_refs(refs)}
    elif isinstance(wait, dict) and "smoke_refs" in wait:
        validate_refs(refs)
    return result


def stored_wait(value):
    refs = value.get("smoke_refs") if isinstance(value, dict) else None
    raw = {k: v for k, v in value.items() if k != "smoke_refs"} if isinstance(value, dict) else value
    result = _stored_wait(raw)
    if refs is not None:
        result = {**result, "smoke_refs": validate_refs(refs)}
    elif isinstance(value, dict) and "smoke_refs" in value:
        validate_refs(refs)
    return result


def blocked_start(since, record):
    """One wait episode ends only at an explicit open revision."""
    if record["status"] == "open":
        return None
    if record["status"] == "blocked":
        return since or record["updated_at"]
    return since


def blocked_age(store, item):
    since = item.get("blocked_since")
    age = (None if since is None else max(0, int(
        (dt.datetime.fromisoformat(now()) - dt.datetime.fromisoformat(since)).total_seconds())))
    wait = item.get("wait") or {}
    return {"blocked_since": since, "blocked_age_seconds": age,
            "blocked_max_age_seconds": None if wait.get("kind") == "deadline" else store.blocked_max_age_seconds}


def liveness(store, wait, snapshot, *, item=None, writer_roles=None, operator_answer=None, record_question=None):
    observed = _liveness(store, wait, snapshot, item=item, writer_roles=writer_roles,
                         operator_answer=operator_answer, record_question=record_question)
    if item is None:
        return observed
    age = blocked_age(store, item)
    # Unknown/unsafe evidence never becomes a terminal claim. Expiration is
    # local replanning authority only, not a remote failure or satisfaction.
    if (observed["state"] in ("pending", "receipt_bound")
            and age["blocked_max_age_seconds"] is not None
            and age["blocked_age_seconds"] is not None
            and age["blocked_age_seconds"] >= age["blocked_max_age_seconds"]):
        observed = {**observed, "state": "expired", "reason": "blocked_max_age_reached"}
    return {**observed, **age}


def _liveness(store, wait, snapshot, *, item=None, writer_roles=None, operator_answer=None, record_question=None) -> dict:
    """Read-only work replanning evidence, never completion authority.

    Insight-decided waits follow the current revision; dependency waits bind an
    exact revision/action and use pure satisfaction evidence for due checks.
    Missing/withdrawn targets and superseded/conflicting dependencies are impossible;
    unavailable bodies or unsafe/inconsistent observations are unknown. CI and
    proposal merge waits use only their receipt-bound routes when a current
    item is supplied (readiness); generic diagnostics leave them receipt_bound.
    """
    holder = item.get("holder") if item is not None else None
    self_resolvable = None

    def result(state, reason):
        return {"state": state, "reason": reason,
                "wait_kind": wait.get("kind") if isinstance(wait, dict) else None,
                **({"self_resolvable": self_resolvable} if writer_roles is not None else {})}

    try:
        if wait is None:
            return result("absent", "no_wait")
        wait = store._stored_wait(wait)
        kind = wait["kind"]
        if kind in ("ci", "publication_ci", "proposal_merged"):
            if item is None:
                return result("receipt_bound", f"{kind}_receipt_route")
            if item["status"] != "blocked" or not item["holder"] or item["wait"] != wait:
                raise Denied("Terminal readiness requires the current held waiter")
            from . import ci_waits, proposal_waits
            route = proposal_waits if kind == "proposal_merged" else ci_waits
            observed = route.evidence(store, item)
            if observed is None:
                return result("pending", "terminal_evidence_absent")
            envelope, computed = observed
            event = envelope["payload"]
            outcome = (event["state"] if kind == "proposal_merged" else
                       event["conclusion"] if event["kind"] == "ci" else
                       event["outcome"]["outcome"])
            return {**result("terminal", "receipt_route_terminal"), "outcome": outcome,
                    "evidence": {"record_digest": computed or digest(canonical(envelope))}}
        if kind == "record_closed":
            if record_question is None:
                factory = store.record_question_lookup
                if factory is None:
                    return result("unknown", "record_question_unavailable")
                record_question = factory()
            closed = record_question(wait["subject"])
            if closed is None:
                return result("unknown", "record_question_unavailable")
            return result("due" if closed else "pending",
                          "record_closed" if closed else "record_answer_pending")
        if kind == "deadline":
            due = dt.datetime.now(dt.timezone.utc) >= dt.datetime.fromisoformat(wait["at"])
        elif kind == "code_change":
            if snapshot is None:
                snapshot = store.snapshots.get()
            current = snapshot.get("code_digest") if isinstance(snapshot, dict) else None
            if not isinstance(current, str) or not DIGEST.fullmatch(current):
                return result("unknown", "snapshot_unavailable")
            due = current != wait["digest"]
            if holder is not None and writer_roles == {holder}:
                self_resolvable = "sole_writer_code_change"
        else:
            from . import insight_archive
            from .fs import PREVIEW_BYTES
            target = wait["target"] if kind == "insight_decided" else wait["requires"]["insight"]
            item = store._optional_record(f"inbox/{target}.json")
            if item is None:
                item = insight_archive.read_immutable(store.root, target, include_unavailable=True)
            decision = store._optional_record(f"decisions/{target}.json")
            if decision is not None:
                action, decision_rev = store._validate_current_decision_shape(decision, target)
            if item is None:
                # Old GC could retain identity but lose the body. That is not
                # evidence of a missing target. Nor is an orphan decision.
                identity = store._optional_record(f"insight-ids/{target}.json")
                if identity is not None or decision is not None:
                    return result("unknown", "target_unavailable")
                return result("impossible", "target_missing")
            identifier(item.get("source"))
            rev = item.get("rev", 1)
            if (len(canonical(item)) > PREVIEW_BYTES or item.get("body_unavailable") is True
                    or item.get("id") != target or type(rev) is not int or rev < 1
                    or not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 200
                    or not isinstance(item.get("body"), str) or not item["body"].strip()
                    or "base_snapshot" not in item
                    or (item["base_snapshot"] is not None and
                        (not isinstance(item["base_snapshot"], str) or not DIGEST.fullmatch(item["base_snapshot"])))
                    or not isinstance(item.get("evidence", []), list)
                    or dt.datetime.fromisoformat(item.get("created_at", "")).tzinfo is None):
                return result("unknown", "target_unavailable")
            if decision is not None and decision_rev > rev:
                return result("unknown", "inconsistent_revision")
            if kind == "dependency":
                if decision is not None and decision_rev == rev and action == "withdraw":
                    return result("impossible", "target_withdrawn")
                if rev > wait["requires"]["rev"]:
                    return result("impossible", "target_superseded")
                if rev < wait["requires"]["rev"]:
                    return result("pending", "future_revision")
            own_question = False
            if (kind == "insight_decided" and writer_roles is not None and holder in writer_roles
                    and item["source"] == holder):
                from .insights import insight_metadata
                own_question = insight_metadata(item).get("kind", "proposal") == "question"
                if (own_question and (decision is None or decision_rev < rev or action == "defer")
                        and operator_answer is not None and operator_answer(item)):
                    self_resolvable = "answered_own_question"
            if decision is None:
                return result("pending", "undecided")
            if decision_rev < rev:
                return result("pending", "superseded_decision")
            if kind == "insight_decided":
                if action == "withdraw":
                    return result("impossible", "target_withdrawn")
                due = action in ("accept", "modify", "reject")
                if due and own_question:
                    self_resolvable = "answered_own_question"
            else:
                if action in ("accept", "modify", "reject") and action != wait["requires"]["action"]:
                    return result("impossible", "decision_conflict")
                from .insights import satisfaction_evidence
                due = satisfaction_evidence(wait, item, decision) is not None
        return result("due" if due else "pending", "condition_met" if due else "condition_pending")
    except (Denied, OSError, ValueError, TypeError, AttributeError, KeyError, LimitExceeded):
        return result("unknown", "unreadable")
