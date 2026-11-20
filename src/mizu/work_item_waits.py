"""Observable wait normalization and read-only liveness; no registration."""
from __future__ import annotations

import datetime as dt

from .errors import Busy, ConfigError, Denied, LimitExceeded
from .smoke_refs import validate_refs
from .fs import DIGEST, canonical, digest, now

#: Locally observable conditions, reevaluated from recorded state. Receipt
#: routes (``ci``, ``publication_ci``) and work-item subjects
#: (``object_resolved``) have their own normalizers.
WAIT_KINDS = ("deadline", "code_change")
#: Every kind the product evaluates. A stored wait of any other kind stays as
#: written: it is inert, never resolves on its own and replays unchecked.
EVALUATED_KINDS = frozenset({*WAIT_KINDS, "ci", "publication_ci", "object_resolved"})


def evaluated(wait) -> bool:
    """Whether the product evaluates this stored wait's kind (and subject)."""
    if not isinstance(wait, dict) or wait.get("kind") not in EVALUATED_KINDS:
        return False
    if wait["kind"] == "object_resolved":
        subject = wait.get("subject")
        return isinstance(subject, dict) and subject.get("type") == "work_item"
    return True


def _normalize_wait(wait, baseline=None) -> dict | None:
    """Normalize a raw work-item wait; capture the code baseline only on creation."""
    if wait is None:
        return None
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
        if set(wait) - {"kind", "at"}:
            raise Denied("A deadline wait takes only at")
        return {"kind": kind, "at": moment.isoformat(timespec="seconds")}
    # code_change has no fields beyond kind; its baseline is the current
    # published digest.
    if set(wait) - {"kind"}:
        raise Denied("A code_change wait takes no fields")
    try:
        baseline = baseline()
    except (OSError, ValueError, TypeError, AttributeError):
        raise Denied("A code_change wait needs a published snapshot baseline")
    return {"kind": kind, "digest": baseline}


#: Bound of a stored wait the product no longer evaluates.
MAX_INERT_WAIT_BYTES = 4096


def _stored_wait(value) -> dict:
    """Validate normalized observable waits without rebasing code evidence."""
    if not isinstance(value, dict):
        raise Denied("Blocked work items require a structured wait")
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise Denied("Invalid stored work-item wait")
    # Only a live work-item subject is authorized as an evaluated wait. A
    # retired object subject (insight, record) is inert stored history: keep
    # it bounded and replayable without resubscribing, while the creation path
    # still validates new work-item waits strictly.
    if kind == "object_resolved" and evaluated(value):
        from .object_waits import normalize
        return normalize(value)
    if kind == "code_change":
        baseline = value.get("digest")
        if set(value) != {"kind", "digest"} or not isinstance(baseline, str) or not DIGEST.fullmatch(baseline):
            raise Denied("Invalid stored code-change wait")
        return value
    if not evaluated(value):
        try:
            if len(canonical(value)) > MAX_INERT_WAIT_BYTES:
                raise Denied("Stored work-item wait exceeds its bound")
        except (TypeError, ValueError, RecursionError) as exc:
            raise Denied("Invalid stored work-item wait") from exc
        return value
    normalized = normalize_wait(value)
    if value != normalized:
        raise Denied("Invalid normalized work-item wait")
    return normalized


def normalize_wait(wait, baseline=None):
    refs = wait.get("smoke_refs") if isinstance(wait, dict) else None
    raw = {k: v for k, v in wait.items() if k != "smoke_refs"} if isinstance(wait, dict) else wait
    result = _normalize_wait(raw, baseline)
    if refs is not None:
        result = {**result, "smoke_refs": validate_refs(refs)}
    elif isinstance(wait, dict) and "smoke_refs" in wait:
        validate_refs(refs)
    return result


def stored_wait(value):
    refs = value.get("smoke_refs") if isinstance(value, dict) else None
    raw = {k: v for k, v in value.items() if k != "smoke_refs"} if isinstance(value, dict) else value
    if not evaluated(raw):
        return _stored_wait(value)
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


def blocked_age(item):
    since = item.get("blocked_since")
    age = (None if since is None else max(0, int(
        (dt.datetime.fromisoformat(now()) - dt.datetime.fromisoformat(since)).total_seconds())))
    return {"blocked_since": since, "blocked_age_seconds": age}


def liveness(store, wait, snapshot, *, item=None, writer_roles=None):
    observed = _liveness(store, wait, snapshot, item=item, writer_roles=writer_roles)
    return observed if item is None else {**observed, **blocked_age(item)}


def _liveness(store, wait, snapshot, *, item=None, writer_roles=None) -> dict:
    """Read-only work replanning evidence, never completion authority.

    Work-item subjects follow the target's current status. CI waits use only
    their receipt-bound routes when a current item is supplied (readiness);
    generic diagnostics leave them receipt_bound. A stored wait of a kind the
    product no longer evaluates is ``unknown``. Unsafe or inconsistent
    observations are unknown.
    """
    holder = item.get("holder") if item is not None else None
    self_resolvable = None

    def result(state, reason):
        return {"state": state, "reason": reason,
                "wait_kind": wait.get("kind") if isinstance(wait, dict) else None,
                **({"self_resolvable": self_resolvable}
                   if writer_roles is not None or self_resolvable is not None else {})}

    try:
        if wait is None:
            return result("absent", "no_wait")
        if not evaluated(wait):
            return result("unknown", "unsupported_wait")
        wait = store._stored_wait(wait)
        kind = wait["kind"]
        if kind in ("ci", "publication_ci"):
            if item is None:
                return result("receipt_bound", f"{kind}_receipt_route")
            if item["status"] != "blocked" or not item["holder"] or item["wait"] != wait:
                raise Denied("Terminal readiness requires the current held waiter")
            from . import ci_waits
            observed = ci_waits.terminal_evidence(store, item)
            if observed is None:
                if ci_waits.holder_only(store, item):
                    self_resolvable = "sole_requester_ci"
                return result("pending", "terminal_evidence_absent")
            envelope, computed = observed
            event = envelope["payload"]
            outcome = event["conclusion"] if event["kind"] == "ci" else event["outcome"]["outcome"]
            return {**result("terminal", "receipt_route_terminal"), "outcome": outcome,
                    "evidence": {"record_digest": computed or digest(canonical(envelope))}}
        if kind == "object_resolved":
            from .object_waits import work_item_state
            state, reason = work_item_state(store, wait["subject"])
            return result(state, reason)
        if kind == "deadline":
            due = dt.datetime.now(dt.timezone.utc) >= dt.datetime.fromisoformat(wait["at"])
            if holder is not None and writer_roles == {holder}:
                self_resolvable = "sole_holder_deadline"
        else:
            if snapshot is None:
                snapshot = store.snapshots.get()
            current = snapshot.get("code_digest") if isinstance(snapshot, dict) else None
            if not isinstance(current, str) or not DIGEST.fullmatch(current):
                return result("unknown", "snapshot_unavailable")
            due = current != wait["digest"]
            if holder is not None and writer_roles == {holder}:
                self_resolvable = "sole_writer_code_change"
        return result("due" if due else "pending", "condition_met" if due else "condition_pending")
    except (Busy, ConfigError, Denied, OSError, ValueError, TypeError, AttributeError, KeyError, LimitExceeded, RecursionError):
        return result("unknown", "unreadable")


#: Witness states that may prove a wait is holder-only. A due or terminal wait
#: needs no classifier; ``unknown`` and other non-pending states fail closed.
_HOLDER_ONLY_STATES = ("pending", "receipt_bound")

def holder_only(store, item, snapshot, *, writer_roles) -> str | None:
    """Positive witness for a pending wait only its holder can release."""
    try:
        if not isinstance(item, dict) or item.get("status") != "blocked" or not item.get("holder"):
            return None
        wait = item.get("wait") or {}
        if wait.get("kind") not in ("ci", "publication_ci"):
            if not isinstance(writer_roles, (set, frozenset)) or writer_roles != {item["holder"]}:
                return None
        observed = _liveness(store, wait, snapshot, item=item, writer_roles=writer_roles)
        if observed["state"] in _HOLDER_ONLY_STATES:
            return observed.get("self_resolvable")
    except (Denied, KeyError, TypeError, AttributeError, ValueError, OSError):
        return None
