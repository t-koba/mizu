"""Static dashboard payload: bounded, already-recorded facts plus an atomic pointer.

The mechanism collects only facts that the harness has already recorded
(control state, published snapshot, verification, health, pending proposals
and their decisions, latest artifact pointer, shared request-budget usage)
and publishes them as one UTF-8 JSON document under a content-addressed ID
with an atomic latest pointer in ``dashboard/``. It never renders HTML,
never sends network traffic, never runs model inference and never exposes
secrets or workspace file contents. Presentation (HTML, per-project panels)
is operator policy: see ``examples/render-dashboard.py`` and
``docs/dashboard.md``. Dashboard documents are disposable projections, never
the source of project truth.
"""
from __future__ import annotations

import datetime as dt
import heapq

from .fs import canonical, digest, now, read_json, publish_pointer, atomic_write, lock
from .usage import summarize, record_time

#: Pending-proposal projection bound is operator-selected
#: ([limits] pending_insights, default 30). Raw inbox/decisions stay on disk.
#: Recent-decision count and reason truncation are operator-selected
#: ([limits] dashboard_decisions default 10, dashboard_reason_chars default
#: 500, flagged); raw reasons stay in decisions/ on disk. The constants
#: below are fallback defaults when no configured limits are present.
MAX_PENDING_FALLBACK = 30
#: Fallback default for [limits] dashboard_decisions; older history stays on disk.
MAX_DECISIONS = 10
#: Fallback default for [limits] dashboard_reason_chars for small screens.
REASON_TRUNCATE = 500


def _configured(project, name: str, fallback: int) -> int:
    limits = getattr(getattr(project, "config", None), "limits", None)
    value = getattr(limits, name, fallback) if limits is not None else fallback
    return value if type(value) is int else fallback


def _truncate(text: str, maximum: int = REASON_TRUNCATE) -> dict:
    if len(text) <= maximum:
        return {"text": text, "truncated": False}
    return {"text": text[:maximum], "truncated": True}


def collect(project) -> dict:
    """Gather the dashboard core from already-recorded project files.

    Schema/bounds: pending slice follows ``[limits] pending_insights``;
    recent decisions follow ``[limits] dashboard_decisions`` (default 10),
    reason truncation follows ``[limits] dashboard_reason_chars`` (default
    500 chars, flagged), day-groups follow ``[limits] retention_days``. Trust: recorded harness state + agent prose
    (data, not proof). Retry: read-only, lock-free, idempotent publish.
    Evidence: disposable projection; snapshots/runs/decisions stay on disk.
    Failure: raises instead of inventing placeholders. The only write is the
    best-effort reaping of budget day-files under the retention window.
    Raises the project's own errors (missing project, corrupt state) instead
    of inventing placeholder values.
    """
    base = project.status()
    control = base["control"]
    max_pending = getattr(getattr(project, "config", None), "limits", None)
    max_pending = max_pending.pending_insights if max_pending is not None else MAX_PENDING_FALLBACK
    projection = project.insights.projection(limit=max_pending)
    pending = projection["items"]
    slim_pending = []
    reason_chars = _configured(project, "dashboard_reason_chars", REASON_TRUNCATE)
    max_decisions = _configured(project, "dashboard_decisions", MAX_DECISIONS)
    for item in pending:
        entry = {k: item[k] for k in ("id", "source", "title", "created_at", "base_snapshot")}
        decision = item.get("decision")
        if decision:
            entry["decision"] = {
                "action": decision.get("action"),
                "created_at": decision.get("created_at"),
                "reason": _truncate(str(decision.get("reason", "")), maximum=reason_chars),
                "revisit": _truncate(str(decision.get("revisit", "")), maximum=reason_chars),
            }
        else:
            entry["decision"] = None
        slim_pending.append(entry)
    decision_paths = (p for p in (project.root / "decisions").glob("*.json") if not p.is_symlink())
    decision_count = 0
    recent = []
    for path in decision_paths:
        try:
            record = read_json(path, {})
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(record, dict) or "id" not in record:
            continue
        decision_count += 1
        entry = {
            "id": record.get("id"),
            "action": record.get("action"),
            "created_at": record.get("created_at"),
            "reason": _truncate(str(record.get("reason", "")), maximum=reason_chars),
        }
        key = (record_time(entry.get("created_at")), str(entry.get("id") or ""), path.name)
        value = (*key, entry)
        if len(recent) < max_decisions:
            heapq.heappush(recent, value)
        elif key > recent[0][:3]:
            heapq.heapreplace(recent, value)
    recent = [e[3] for e in recent]
    recent.sort(key=lambda item: (record_time(item.get("created_at")), str(item.get("id") or "")))
    facts = summarize(project)
    retention = getattr(getattr(project, "config", None), "limits", None)
    retention = retention.retention_days if retention is not None else 31
    _tz = project.config.tzinfo
    cutoff = (dt.datetime.now(_tz).date() - dt.timedelta(days=max(0, retention-1))).isoformat()
    recent_groups = [g for g in facts["groups"] if not retention or g["day"] >= cutoff]
    artifact_pointer = project.root / "artifacts" / "latest.json"
    core = {
        "project": project.name,
        "control": {
            "armed": bool(control.get("armed")),
            "paused": bool(control.get("paused")),
            "reason": str(control.get("reason", ""))[:1000],
            "updated_at": control.get("updated_at"),
        },
        "snapshot": {
            "id": base["snapshot"],
            "code_digest": base["code_digest"],
            "created_at": base["created_at"],
            "outcome": base["outcome"],
            "summary": base["summary"],
            "state": base["state"],
            "verification": base["verification"],
            "goal_digest": base["goal_digest"],
            "wake_at": base["wake_at"],
        },
        "needs_operator_input": base["outcome"] == "blocked",
        "pending_insights": slim_pending,
        "pending_count": len(slim_pending),
        "pending_total": projection["total"],
        "pending_truncated": projection["truncated"],
        "answered_count": decision_count,
        "recent_decisions": recent,
        "health": base["health"],
        "active": base["active"],
        "latest_artifact": read_json(artifact_pointer) if not artifact_pointer.is_symlink() else None,
        # Request counts only. This is not money, tokens, or cached-token ratios.
        "budget": base["budget"],
        # Provider/model/token/day facts for presentation policy (e.g. pinned-rate
        # estimates). Groups assume one rate per (day, provider, model);
        # recent_entries keep full per-run timestamps for finer structures
        # such as time-of-day rates. Amounts are provider-reported, not a bill.
        "usage": {"totals": facts["totals"], "recent_groups": recent_groups,
                  "recent_entries": facts["recent_entries"],
                  "entries_truncated": facts["entries_truncated"],
                  "scanned_records": facts["scanned_records"],
                  "skipped_records": facts["skipped_records"],
                  "unknown_shapes": facts["unknown_shapes"],
                  "truncated": facts["truncated"]},
    }
    return core


def publish(project) -> dict:
    """Write the collected core under a content digest and move the pointer.

    Entry first, pointer last (see fs.publish_entry). Re-publishing unchanged
    facts yields the same ID and is safe to retry. Concurrent publishers
    serialize on filesystem atomicity, last writer wins, and every entry file
    remains independently readable.
    """
    core = collect(project)
    dashboard_id = digest(canonical(core))
    root = project.root / "dashboard"
    published_at = now()
    payload = {"id": dashboard_id, **core, "published_at": published_at}
    with lock(root / ".publish.lock"):
        existing = read_json(root / f"{dashboard_id}.json")
        if existing is not None:
            payload["published_at"] = existing["published_at"]
        atomic_write(root / f"{dashboard_id}.json", canonical(payload), exclusive=True)
        publish_pointer(root, {"dashboard": dashboard_id, "snapshot": core["snapshot"]["id"], "published_at": published_at})
    return {"dashboard": dashboard_id, "snapshot": core["snapshot"]["id"],
            "document": str(root / f"{dashboard_id}.json"),
            "pending_count": core["pending_count"],
            "needs_operator_input": core["needs_operator_input"]}
