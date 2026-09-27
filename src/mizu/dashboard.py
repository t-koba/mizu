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

from .fs import canonical, digest, now, read_json, publish_entry
from .usage import summarize

SCHEMA = 1
#: Pending proposals surfaced per dashboard (matches the insight list bound).
MAX_PENDING = 30
#: Recent decisions surfaced per dashboard; older history stays on disk.
MAX_DECISIONS = 10
#: Long decision prose is truncated to this many characters for small screens.
REASON_TRUNCATE = 500


def _truncate(text: str, maximum: int = REASON_TRUNCATE) -> dict:
    if len(text) <= maximum:
        return {"text": text, "truncated": False}
    return {"text": text[:maximum], "truncated": True}


def collect(project) -> dict:
    """Gather the dashboard core from already-recorded project files.

    Takes no locks and admits no model requests. The only write is the
    best-effort reaping of budget day-files older than 31 days.
    Raises the project's own errors (missing project, corrupt state) instead
    of inventing placeholder values.
    """
    base = project.status()
    control = base["control"]
    pending = base["pending_insights"][:MAX_PENDING]
    slim_pending = []
    for item in pending:
        entry = {k: item[k] for k in ("id", "source", "title", "created_at", "base_snapshot")}
        decision = item.get("decision")
        if decision:
            entry["decision"] = {
                "action": decision.get("action"),
                "created_at": decision.get("created_at"),
                "reason": _truncate(str(decision.get("reason", ""))),
                "revisit": _truncate(str(decision.get("revisit", ""))),
            }
        else:
            entry["decision"] = None
        slim_pending.append(entry)
    decision_paths = sorted(p for p in (project.root / "decisions").glob("*.json") if not p.is_symlink())
    recent = []
    for path in decision_paths[-MAX_DECISIONS:]:
        record = read_json(path, {})
        if not isinstance(record, dict) or "id" not in record:
            continue
        recent.append({
            "id": record.get("id"),
            "action": record.get("action"),
            "created_at": record.get("created_at"),
            "reason": _truncate(str(record.get("reason", ""))),
        })
    recent.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    facts = summarize(project)
    recent_groups = facts["groups"][-31:]
    artifact_pointer = project.root / "artifacts" / "latest.json"
    core = {
        "schema": SCHEMA,
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
        "answered_count": len(decision_paths),
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
    publish_entry(root, f"{dashboard_id}.json", canonical(payload),
                  {"dashboard": dashboard_id, "snapshot": core["snapshot"]["id"], "published_at": published_at})
    return {"dashboard": dashboard_id, "snapshot": core["snapshot"]["id"],
            "document": str(root / f"{dashboard_id}.json"),
            "pending_count": core["pending_count"],
            "needs_operator_input": core["needs_operator_input"]}
