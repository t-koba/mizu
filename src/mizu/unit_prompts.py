"""Unit-opening prompt projections: full and resumed delta views.

This module owns one model-facing contract: the full unit prompt
(:func:`prompt_for`) and the resumed-session delta (:func:`prompt_delta_for`)
plus their tightly coupled bounded projection helpers and exact
unit-opening literal. Everything stays byte- and key-order identical to the
former ``runtime`` definitions.

Session selection, rotation/recovery/fallback policy (:func:`runtime.session_prompt`
and below), work-ready scheduling (``runtime._has_ready_work``,
``runtime._ready_work_changed``, ``runtime.should_run``), admission and the
scheduler stay in ``runtime``. This module also owns the display-only ready
work projection and its age formatter; the projection preserves its
``ready_work_signature`` side effect without depending back on runtime.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import TYPE_CHECKING

from . import check_jobs
from .ci_observation import ci_failures_projection
from .fs import canonical, read_json
from .protocol import tool_definitions
from .report import previous as _previous_report

if TYPE_CHECKING:  # pragma: no cover - annotations only, never a runtime cycle.
    from .runtime import Context


def _prompt_clock(context: Context) -> dict:
    """Current wall-clock time plus the configured zone for agent reasoning.

    Schema: ``{"now": <ISO-8601 with offset>, "timezone": <name>}`` appended
    last so the stable prefix keeps working caches warm. Bounds: two small
    string fields. Trust: local clock plus operator ``timezone`` (default
    UTC). Retry/cancellation: n/a (pure projection). Evidence: recorded in
    ``prompt_projection.json`` bytes only. Failure: raises on broken
    configuration (fail closed, never a silent UTC day).
    """
    import datetime as _dt
    tz = context.config.tzinfo
    name = context.config.timezone
    return {"now": _dt.datetime.now(tz).isoformat(timespec="seconds"), "timezone": str(name)}


def _prompt_research_state(context: Context):
    """Own role's current research record for prompt injection.

    Returns None without the read capability; ``absent``/``unavailable``
    statuses inject bare so absence never reads as "nothing
    researched"; current records inject whole while within
    ``role_state_prompt_bytes``, else ``truncated`` with generation
    and sizes and no partial content. Constant-size by construction,
    so resumed-session deltas may carry it without unbounded growth.
    """
    caps = set(context.role.capabilities)
    if "research_read" not in caps and "research" not in caps:
        return None
    seen = context.project.role_state.read(context.role.name)
    if seen["status"] != "current":
        return {"status": seen["status"]}
    record = seen["record"]
    body = canonical(record["state"])
    cap = context.config.limits.role_state_prompt_bytes
    if len(body) > cap:
        return {"status": "truncated", "generation": seen["generation"],
                "state_bytes": len(body), "prompt_bytes": cap,
                "updated_at": record.get("updated_at")}
    return {"status": "current", "generation": seen["generation"],
            "updated_at": record.get("updated_at"), "state": record["state"]}


def _decision_task(context: Context) -> str | None:
    """Delivery description for a run carrying delivered decision events.

    Schema: None when no decision events were delivered, else a short
    factual description naming which kinds were delivered and what counts
    as handled. Bounds: one small string. Trust: local dispatch state
    only. Failure: never raises.
    """
    events = list(getattr(context, "decision_events", []) or [])
    diagnostics = list(getattr(context, "decision_receipt_diagnostics", []) or [])
    if not events and not diagnostics:
        return None
    if events and diagnostics:
        return (f"Delivered decision events: {len(events)}. "
                f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "Select each handled row's ack_token in finish.ack_events; omitted rows "
                "remain pending oldest-first, including deferrals. Finishing alone "
                "does not acknowledge decisions. Handling is defined by role policy.")
    if diagnostics:
        return (f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "No deliverable decision events; handling is defined by role policy.")
    return (f"Delivered decision events: {len(events)}. "
            "Select each handled row's ack_token in finish.ack_events; omitted rows "
            "remain pending oldest-first, including deferrals. Finishing alone "
            "does not acknowledge decisions. Handling is defined by role policy.")


def _open_backlog(context: Context, caps: set) -> dict:
    """Still-undecided backlog statement for prompts (never raises).

    Schema: ``{"ids", "total", "truncated"}`` over the same bounded pending
    set the full prompt carries as entries, so a delta that omits
    already-offered entries still states the open backlog instead of reading
    as empty. Bounds: ids follow ``[limits] pending_insights`` (newest kept)
    with explicit ``truncated``. Trust: local recorded state only.
    """
    stated: dict = {"ids": [], "total": 0, "truncated": False}
    if "insights" not in caps and "decide" not in caps:
        return stated
    try:
        backlogged = context.project.insights.projection(
            limit=context.config.limits.pending_insights)
        return {"ids": [item["id"] for item in backlogged["items"]],
                "total": backlogged["total"],
                "truncated": backlogged["truncated"]}
    except Exception:
        return stated


#: Fixed statement opening every dispatch prompt, full and delta alike.
#: A resumed session's delta is otherwise bare data; without this a model
#: can read it as an acknowledgement of the previous seal ("sealed
#: successfully") instead of a new unit that must do work and finish.
#: Constant text in a fixed first position, so prompt caches stay warm.
UNIT_BEGINS_STATEMENT = (
    "A new work unit begins. This is not an acknowledgement of a previous "
    "seal: do useful work within your role and capabilities, then finish "
    "with mizu_finish and a short state.")


# Saturating display bound, not an admission/aging policy.
MAX_READY_WORK_AGE_SECONDS = 2 ** 31 - 1


def _ready_work_age(updated_at, current_time):
    """Idle seconds since the latest ledger transition; unknown stays unknown."""
    try:
        timestamp = dt.datetime.fromisoformat(updated_at)
        if timestamp.tzinfo is None:
            return None
        age = current_time - timestamp.timestamp()
        if age != age or age in (float("inf"), float("-inf")):
            return None
        return int(min(MAX_READY_WORK_AGE_SECONDS, max(0, age)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _ready_work_projection(context):
    """Independent bounded ready page, never inferred from the general page."""
    page = context.project.work_items.page_ready(
        limit=context.config.limits.work_items_prompt, role_name=context.role.name)
    # Capture the same locked scan that supplies the full/delta input, never
    # the finish-time inventory: concurrent new work must remain unconsumed.
    context.ready_work_signature = page["signature"]
    current_time = time.time()
    return {"items": [{**{key: row[key] for key in
                        ("id", "title", "status", "stale", "redecision", "updated_at")},
                       "age_seconds": _ready_work_age(row["updated_at"], current_time),
                       **({"wait": row["wait"], "wait_satisfied": True}
                          if row["status"] == "blocked" else {})}
                      for row in page["items"]],
            **{key: page[key] for key in ("total", "overflow_count", "truncated",
                                         "unseeded_decisions", "coverage_complete")}}


def _prompt_tool_surface(context: Context) -> dict:
    """Names-only broker surface, bounded by this dispatch role's configured grants.

    This is an opening snapshot, not a live grant: narrowed re-dispatches
    prepend their notice to the original prompt later in the same unit.
    """
    return {
        "scope": (
            "UNIT-OPENING host broker tool surface for this work unit. This "
            "supersedes restrictions from earlier work units, not later "
            "restrictions within this same unit. Host tools and enforcement "
            "remain authoritative; this text grants nothing and does not "
            "advertise native engine tools."),
        "tools": [tool["name"] for tool in tool_definitions(context.role.capabilities)],
    }


def _prompt_run_identity(context: Context) -> dict:
    """Bounded run identity for the opening prompt (never fails).

    The run directory name is the run id. ``started_at`` is read back from
    the run's own ``started.json`` when present; prompts built before it is
    written (or with an unreadable record) carry the id alone.
    """
    identity = {"id": context.run_dir.name}
    try:
        started = read_json(context.run_dir / "started.json", {})
    except Exception:
        started = {}
    if isinstance(started, dict) and isinstance(started.get("started_at"), str):
        identity["started_at"] = started["started_at"]
    return identity


def _prompt_capture_limit(context: Context) -> str | None:
    """The capture-limit cause that still applies to this role, if any.

    A workspace file that cannot be captured ends the unit that produced it.
    The cause is set when a capture fails, kept while it applies, and
    cleared when a capture succeeds, so the next unit reads a live cause
    instead of repeating the same capture failure. Bounded to the stored
    health record value.
    """
    try:
        health = read_json(context.project.root / "health" / f"{context.role.name}.json", {})
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(health, dict):
        return None
    cause = health.get("capture_limit")
    return cause if isinstance(cause, str) and cause else None


def prompt_for(context: Context) -> str:
    # The charter body belongs to stable engine system context on every
    # dispatch (including resume); full and delta unit prompts carry its pin.
    caps = set(context.role.capabilities)
    # Mechanism enforces grants, not policy choice: capability-gated context
    # is offered where appropriate, alongside the always-present, bounded
    # project-wide work-item page. `pending_insights` count follows the
    # operator-selected `[limits] pending_insights` bound (newest kept), and
    # `open_insights` states the same bounded backlog with explicit total;
    # `recent_snapshots` follows `[limits] prompt_snapshots`. Exact snapshot
    # references are always pinned. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    pending = context.project.insights.list(limit=context.config.limits.pending_insights) \
        if ("insights" in caps or "decide" in caps) else []
    open_work_items = context.project.work_items.page_open(
        limit=context.config.limits.work_items_prompt)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in caps:
        ci_branch = None
    ci_failures = ci_failures_may_be_truncated = None
    if "vcs_read" in caps:
        ci_failures, ci_failures_may_be_truncated = ci_failures_projection(context.project)
    return json.dumps({"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       "published_snapshot": {**{k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                           "effective_verification": context.project.effective_verification(context.snapshot)},
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "pending_insights": pending,
                       "open_insights": _open_backlog(context, caps),
                       "open_work_items": open_work_items,
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_receipt_diagnostics": list(
                           getattr(context, "decision_receipt_diagnostics", []) or []),
                       **({"work_item_events": list(context.work_item_events)}
                          if getattr(context, "work_item_events", None) else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       **check_jobs.prompt_projection(context),
                       "acceptance_commands": acceptance,
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "ci_branch": ci_branch,
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": _ready_work_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)},
                      ensure_ascii=False)


def prompt_delta_for(context: Context, pending: list) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: the constant ``unit`` opening, then the names-only
    ``unit_tool_surface``, the ``goal_digest`` pin, the
    pinned ``published_snapshot`` and a small ``snapshot_delta``
    (previous/current ids), then only the pending
    proposals not yet offered, an ``open_insights`` statement of the still
    undecided backlog (ids plus total, so an already-offered insight never
    reads as done), the always-present bounded ``open_work_items`` page,
    workspace markers, capability-gated acceptance commands, and a
    capability-gated ``previous_report`` preview for roles holding
    ``report``. The independent bounded ``ready_work`` projection follows
    this stable context in the volatile suffix, before run identity and clock.
    The charter body is supplied separately in stable system instructions;
    ``recent_snapshots`` history is not repeated in deltas. Key order is fixed (stable context
    first, volatile ready-work age, run identity and clock last) so
    repeated prompts preserve as much identical prefix as possible.
    Bounds: pending and the open insight backlog both follow
    ``[limits] pending_insights`` (newest kept; ``truncated`` says so
    explicitly); the work-item page follows ``[limits] work_items_prompt`` and
    includes its overflow count, cursor, truncation, and legacy coverage status;
    the delta carries no workspace file contents and no history
    array, so resumed-unit bytes stay flat as history grows apart from the
    fixed previous-report preview (at most ``report.PREVIOUS_REPORT_BYTES``).
    Trust: local recorded state, which may be untrusted model input.
    Retry/cancellation: local bounded reads; the work-item page scan preserves
    the store's fail-closed errors. ``previous()`` never raises, so an
    unreadable artifact reads as no
    previous edition. Evidence: the dispatching run records
    ``prompt_mode=delta`` and ``prompt_bytes`` in ``prompt_projection.json``.
    Failure: missing non-work-item state follows its existing fallback; the
    fail-closed work-item page reader propagates store errors rather than
    synthesizing an empty inventory.
    """
    caps = set(context.role.capabilities)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_failures = ci_failures_may_be_truncated = None
    if "vcs_read" in caps:
        ci_failures, ci_failures_may_be_truncated = ci_failures_projection(context.project)
    open_backlog = _open_backlog(context, caps)
    open_work_items = context.project.work_items.page_open(
        limit=context.config.limits.work_items_prompt)
    return json.dumps({"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       "published_snapshot": {**{k: context.snapshot[k] for k in (
                           "id", "code_digest", "created_at", "state", "summary", "verification")},
                           "effective_verification": context.project.effective_verification(context.snapshot)},
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "pending_insights": pending,
                       "open_insights": open_backlog,
                       "open_work_items": open_work_items,
                       "decision_events": list(getattr(context, "decision_events", []) or []),
                       "decision_receipt_diagnostics": list(
                           getattr(context, "decision_receipt_diagnostics", []) or []),
                       **({"work_item_events": list(context.work_item_events)}
                          if getattr(context, "work_item_events", None) else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": list(getattr(context, "wait_events", []) or []),
                       "obligations": list(getattr(context, "obligations", []) or []),
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       **check_jobs.prompt_projection(context),
                       "acceptance_commands": acceptance,
                       "ci_branch": ((context.config.vcs.get("ci_branch") or None)
                                     if "vcs_read" in caps else None),
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": _ready_work_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)},
                      ensure_ascii=False)
