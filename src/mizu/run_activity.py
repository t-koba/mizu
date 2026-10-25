"""Per-run activity accounting and CI-request summaries for runtime Contexts."""
from __future__ import annotations

import time

from .fs import now


def activity_bucket(context) -> str:
    if context._activity_join is not None:
        return "join:" + context._activity_join
    if context._activity_operation is not None:
        kind = "local_exec" if context._activity_operation in ("exec", "experiment", "verify") else "tool"
        return kind + ":" + context._activity_operation
    if context.request_deadlines:
        return "model_request"
    return "other"


def activity_change(context, change, *, count: str | None = None) -> None:
    current = time.monotonic()
    context._activity_seconds[context._activity_current] = (
        context._activity_seconds.get(context._activity_current, 0.0)
        + max(0.0, current - context._activity_last))
    # Advance the accounting boundary even if a state mutation fails, then
    # derive the bucket from the state that actually remains. This prevents
    # an exception from charging the same interval again on the next read.
    context._activity_last = current
    try:
        change()
    finally:
        context._activity_current = activity_bucket(context)
    if context._activity_current != context._activity_published:
        publish_activity(context)
    if count is not None:
        context._activity_counts[count] = context._activity_counts.get(count, 0) + 1


def publish_activity(context) -> None:
    """Best-effort publish the bounded live activity gauge."""
    try:
        from .fs import read_json as _read, write_json as _write
        path = context.project.root / "active" / f"{context.role.name}.json"
        record = _read(path, {})
        owns_active = isinstance(record, dict) and record.get("run") == context.run_dir.name
        if not owns_active and not getattr(context, "parent_run", None):
            return
        seconds = dict(context._activity_seconds)
        total = sum(seconds.values())
        seconds[context._activity_current] = seconds.get(context._activity_current, 0.0)
        gauge = {"activity": context._activity_current,
                 "activity_since": now(),
                 "activity_seconds": {key: round(value, 3) for key, value in sorted(seconds.items())},
                 "activity_total_seconds": round(total, 3)}
        _write(context.run_dir / "activity.json", {
            "schema": 1, "run": context.run_dir.name, "role": context.role.name,
            "parent_run": getattr(context, "parent_run", None), **gauge})
        context._activity_published = context._activity_current
        if owns_active:
            _write(path, {**record, **gauge})
    except Exception:
        return


def activity_summary(context) -> dict:
    """Return bounded exclusive wall-time totals for this Context only."""
    with context.mutex:
        current = time.monotonic()
        seconds = dict(context._activity_seconds)
        seconds[context._activity_current] = (
            seconds.get(context._activity_current, 0.0)
            + max(0.0, current - context._activity_last))
        return {"schema": 1, "elapsed_seconds": round(current - context._activity_origin, 6),
                "exclusive_seconds": {key: round(value, 6)
                                      for key, value in sorted(seconds.items())},
                "counts": dict(sorted(context._activity_counts.items())),
                "max_concurrent_model_requests": context._activity_max_requests,
                "open_model_requests": len(context.request_deadlines),
                "reaped_model_requests": context._activity_reaped_requests}


def record_ci_request(context, request_id, date, workflows, covered, *, state="unresolved"):
    """Record or update one bounded per-run CI request entry."""
    with context.mutex:
        entry = {"request_id": request_id, "date": date,
                 "workflows": list(workflows), "covered": list(covered),
                 "state": state}
        for existing in context.ci_requests:
            if existing["request_id"] == request_id:
                # Preserve the date fixed when the request was dispatched.
                existing.update({**entry, "date": existing["date"]})
                return
        context.ci_requests.append(entry)


def ci_request_summary(context) -> dict:
    """Return the bounded CI-request list and total for this run."""
    with context.mutex:
        entries = list(context.ci_requests)
    return {"requests": entries[-16:], "total": len(entries)}
