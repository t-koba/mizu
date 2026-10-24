"""Shared normalization of observable waits; no registration or scheduling."""
from __future__ import annotations

import datetime as dt

from .errors import Denied
from .smoke_refs import validate_refs
from .fs import DIGEST, identifier

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



def _stored_wait(value) -> dict:
    """Validate normalized observable waits without rebasing code evidence."""
    if not isinstance(value, dict):
        raise Denied("Blocked work items require a structured wait")
    kind = value.get("kind")
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
