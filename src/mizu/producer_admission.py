"""Producer admission gating at configured congestion thresholds.

Reads only the project limits that are enabled, delegates the decision to the
pure scheduling policy, and fails closed on malformed limits or unreadable
load. A held attempt claims no run resources and stays durable until an
existing schedule or subscription re-admits the producer.
"""
from __future__ import annotations

from . import scheduling as _scheduling
from .errors import Denied


#: Skip label for a held producer admission. The attempt claims no run
#: directory, model slot, or execution lock, and acknowledges no event or
#: cursor; pending input stays durable until an existing schedule or
#: subscription re-admits the producer.
PRODUCER_HELD_SKIPPED = "producer-held"


def _producer_limit(project, name: str) -> int:
    """Read one validated producer/open-work limit, fail-closed.

    Limits are exact integers 0-1073741824 (0 disables); booleans and any
    other type, range breach, or unreadable value raises so the caller
    holds instead of silently disabling the bound.
    """
    value = getattr(project.insights, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Denied(f"Invalid {name}")
    if not 0 <= value <= 1073741824:
        raise Denied(f"Invalid {name}")
    return value


def _producer_raw_limit(project, name: str):
    """Best-effort raw limit for hold diagnostics; None when unreadable."""
    try:
        return getattr(project.insights, name)
    except Exception:
        return None


def producer_hold(project, role_name: str, role=None) -> dict | None:
    """Hold one producer role at a configured congestion threshold, if held.

    Only configured non-writer project roles holding ``submit_insight`` are
    gated; the writable role remains free to resolve, merge, defer, or reject
    input at capacity. Operator/vcs/editor input channels are never role names
    (config refuses them), so they are never held here. Reads only the metrics required by
    enabled limits: the live unresolved input count for this role-name
    source (undecided plus deferred; resolved and archive-only history
    excluded) when the per-source bound is enabled, and the authoritative
    WorkItems open/doing/blocked count only when this role opts in with
    ``held_at_capacity`` and the open-work bound is enabled.
    Disabling a bound never holds for its metric; when both bounds are 0
    the producer is admitted without reading either store. Fail closed when
    a required limit or load cannot be read. A running unit is never
    cancelled; this only holds the next admission. Returns ``None`` when
    admitted, else a bounded hold record with ``reasons``,
    ``source_pending``, ``open_count`` and ``limits``.
    """
    try:
        current = role if role is not None else project.config.roles.get(role_name)
    except Exception:
        current = None
    if current is None:
        return None
    try:
        if role_name not in project.roles:
            return None
        if (getattr(current, "workspace", None) == "write"
                or "submit_insight" not in set(getattr(current, "capabilities", ()) or ())):
            return None
        held_at_capacity = getattr(current, "held_at_capacity", False)
        if type(held_at_capacity) is not bool:
            raise Denied("Invalid held_at_capacity")
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": None, "open_count": None,
                "limits": {"producer_inputs_per_source": _producer_raw_limit(project, "producer_inputs_per_source"),
                           "open_work_items": _producer_raw_limit(project, "open_work_items")}}
    try:
        per_source_limit = _producer_limit(project, "producer_inputs_per_source")
        open_limit = (_producer_limit(project, "open_work_items")
                      if held_at_capacity else 0)
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": None, "open_count": None,
                "limits": {"producer_inputs_per_source": _producer_raw_limit(project, "producer_inputs_per_source"),
                           "open_work_items": _producer_raw_limit(project, "open_work_items")}}
    if not per_source_limit and not open_limit:
        return None
    source_pending = None
    open_count = None
    try:
        if per_source_limit:
            source_pending = project.insights.unresolved_counts([role_name])[role_name]
        if open_limit:
            open_count = project.work_items.count_open_readonly()
            if type(open_count) is not int or open_count < 0:
                open_count = None
                raise Denied("Invalid open work count")
    except Exception:
        return {"reasons": ("load_unavailable",), "reason": "load_unavailable",
                "source_pending": source_pending, "open_count": open_count,
                "limits": {"producer_inputs_per_source": per_source_limit,
                           "open_work_items": open_limit}}
    decision = _scheduling.evaluate(_scheduling.ProducerFacts(
        is_producer=True, per_source_limit=per_source_limit,
        open_work_limit=open_limit, held_at_capacity=held_at_capacity,
        source_pending=source_pending or 0,
        open_count=open_count or 0, load_ok=True))
    if not decision.held:
        return None
    reasons = tuple(decision.reasons)
    return {"reasons": reasons, "reason": reasons[0] if reasons else "load_unavailable",
            "source_pending": source_pending, "open_count": open_count,
            "limits": {"producer_inputs_per_source": per_source_limit,
                       "open_work_items": open_limit}}
