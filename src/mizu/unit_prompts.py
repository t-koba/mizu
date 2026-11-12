"""Unit-opening prompt projections: full and resumed delta views.

This module owns one model-facing contract: the full unit prompt
(:func:`prompt_for`) and the resumed-session delta (:func:`prompt_delta_for`)
plus their tightly coupled bounded projection helpers and exact
unit-opening literal. Snapshot proof and its diagnostic are projected together
for both prompt variants.

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

from . import acceptance, check_jobs, prompt_sections
from .errors import ConfigError, Denied
from .fs import DIGEST, canonical, digest, read_json
from .protocol import WRITER_CONTINUATION_CONTRACT, tool_definitions
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


def _ci_wait_contract(context: Context) -> dict:
    if not getattr(context, "ci_wait_manifest", ()):
        return {}
    return {"ci_wait_delivery": (
        "CI wait ack_tokens are enabled. Select only handled tokens in finish.ack_events; "
        "omitted/empty/partial selections leave other rows pending oldest-first. "
        "Finishing alone acknowledges no CI rows. Current blocked revision, holder, "
        "publication and evidence are revalidated. Bounded/bound-hit units and finishes "
        "waiting for another check acknowledge no CI rows. Evidence is external-untrusted; "
        "handling never transitions a work item.")}


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
    legacy_page_note = (
        " The legacy decision page was byte-bounded; decision_page marks omitted newest attention rows, "
        "which remain pending."
        if not getattr(context, "shared_decisions", False)
        and isinstance(getattr(context, "decision_page", None), dict)
        and getattr(context, "decision_page", {}).get("byte_truncated") else "")
    if events and diagnostics:
        return (f"Delivered decision/insight attention events: {len(events)}. "
                f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "Select each handled row's ack_token in finish.ack_events; omitted rows "
                "remain pending oldest-first, including deferrals. Finishing alone "
                "does not acknowledge events. insight.pending requests attention only, not decision authority; "
                "handling never answers, decides or changes insight status automatically. Handling is defined by role policy."
                + legacy_page_note)
    if diagnostics:
        return (f"Corrupt receipt diagnostics shown: {len(diagnostics)} (bounded; more may exist; see "
                "decision_receipt_diagnostics; do not acknowledge those tokens; "
                "corrupt bytes stay unchanged pending operator recovery). "
                "No deliverable decision events; handling is defined by role policy.")
    return (f"Delivered decision/insight attention events: {len(events)}. "
            "Select each handled row's ack_token in finish.ack_events; omitted rows "
            "remain pending oldest-first, including deferrals. Finishing alone "
            "does not acknowledge events. insight.pending requests attention only, not decision authority; "
            "handling never answers, decides or changes insight status automatically. Handling is defined by role policy." +
            (" Oversized rows carry ref: use decision_read with that row's ack_token "
             "and offset/limit to read its digest-pinned full envelope. decision_page "
             "states pending count and truncation; omitted rows stay pending."
             if getattr(context, "shared_decisions", False) else legacy_page_note))


def _open_backlog(context: Context, caps: set) -> dict:
    """Still-undecided backlog statement for prompts (never raises).

    Schema: ``{"ids", "total", "truncated"}`` over the same bounded pending
    set the full prompt carries as entries, so a delta that omits
    already-offered entries still states the open backlog instead of reading
    as empty. Bounds: ids follow ``[limits] pending_insights`` (newest kept)
    with explicit ``truncated``. Trust: local recorded state only.
    Reuses the unit-frozen capture when present so the prompt and the
    work-order generation share one reading.
    """
    try:
        cached = getattr(context, "_work_order_inputs", None)
        if isinstance(cached, dict) and cached.get("_captured") is True:
            backlog = cached.get("open_backlog")
            if isinstance(backlog, dict):
                return {"ids": list(backlog.get("ids", [])),
                        "total": backlog.get("total", 0),
                        "truncated": bool(backlog.get("truncated", False))}
    except Exception:
        pass
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
    "with mizu_finish and a short state. " + WRITER_CONTINUATION_CONTRACT)


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


def _ready_work_projection(page, *, context=None):
    """Display one captured ready page and retain its signature for the unit.

    The projection carries the bounded ``unknown_blocked`` count and
    ``coverage_complete`` from the ready scan, so an incomplete scan never
    reads as an exhaustive zero.
    """
    if type(page.get("unknown_blocked")) is not int or page["unknown_blocked"] < 0:
        raise Denied("Invalid ready-work unknown count")
    if type(page.get("coverage_complete")) is not bool:
        raise Denied("Invalid ready-work coverage")
    if context is not None:
        context.ready_work_signature = page["signature"]
    current_time = time.time()
    return {"items": [{**{key: row[key] for key in
                        ("id", "title", "status", "stale", "redecision", "updated_at",
                         "doing_since", "doing_age_seconds", "doing_stale")},
                       "age_seconds": _ready_work_age(row["updated_at"], current_time),
                       **({key: row[key] for key in ("rev", "holder", "wait_resolution")}
                          if "wait_resolution" in row else {}),
                       **({"wait": row["wait"], "wait_liveness": row["wait_liveness"],
                           "wait_satisfied": row["wait_liveness"]["state"] == "due"}
                          if row["status"] == "blocked" else {})}
                      for row in page["items"]],
            **{key: page[key] for key in ("total", "overflow_count", "truncated",
                                         "unseeded_decisions", "coverage_complete",
                                         "unknown_blocked")}}


def _work_order_projection(context) -> dict | None:
    """Writer's explicit order freshness for this unit opening (never raises).

    Uses the unit-frozen semantic generation so full and delta share one
    review; arrivals after opening stay unreviewed until the next unit.
    ``absent`` means no plan, ``current`` means the stored generation
    equals the opening one, ``review_due`` means inputs moved, and
    ``unavailable`` means corrupt/unsafe state or an uncomputable opening
    (fail closed, never empty/current). Only roles holding ``work_order``
    see the field; others omit it. A valid plan only changes this display,
    never ready-work, admission or selection.
    """
    try:
        if "work_order" not in set(getattr(context.role, "capabilities", ())):
            return None
    except Exception:
        return {"status": "unavailable"}
    try:
        from . import work_order as _plan
        projected = _plan.projection_for(context)
    except Exception:
        return {"status": "unavailable"}
    if projected is None:
        return None
    try:
        import json as _json
        from .fs import canonical as _canonical
        if len(_canonical(projected)) > 1024 * 1024:
            return {"status": "unavailable"}
    except Exception:
        return {"status": "unavailable"}
    return projected


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


def broker_grant(context: Context) -> dict:
    """Names-only effective broker grant for one physical dispatch, not authority."""
    return {"tools": _prompt_tool_surface(context)["tools"],
            "bound": context.bound_hit, "wrap_up": context.wrap_up_attempted}


def _grant_session(saved, directory):
    from .engine_config import session_generation
    if not isinstance(saved, dict) or not isinstance(saved.get("id"), str) or not saved["id"]:
        return None
    return digest(canonical({"id": saved["id"], "created_at": saved.get("created_at"),
                             "generation": session_generation(directory)}))


def session_grant_evidence(context: Context, directory) -> dict | None:
    """Tail-only evidence, bound to an observed persistent driver selection.

    No provider identifier is projected. Unknown selection, selector fallback,
    failed dispatch, or unreadable session data supplies no transition evidence.
    This record is replaced with the existing prompt cursor after settlement.
    """
    try:
        from .engine_config import session_generation
        attempt = context._dispatch_evidence["attempts"][-1]
        session = attempt["session"]
        if (attempt["status"] != "returned" or session["unknown_reason"] is not None
                or session["key"] != directory.name
                or session["generation"] != session_generation(directory)):
            return None
        identity = _grant_session(read_json(directory / "session.json", None), directory)
        if identity is not None:
            return {"session": identity, **attempt["broker_grant"]}
    except (AttributeError, ConfigError, KeyError, IndexError, OSError, TypeError, ValueError):
        pass
    return None


def session_grant_transition(context: Context, last, saved, directory) -> dict:
    """Explain only an evidenced broker-grant change on this same session.

    Legacy/malformed/unknown records remain usable as inbox cursors but cannot
    justify a grant claim. A prior unit's per-run bound explains a wrap-up-only
    reduction; otherwise the exact changed tool lists are stated without
    guessing why they changed.
    """
    from .protocol import DEFINITIONS
    try:
        if (not isinstance(last, dict) or not isinstance(last.get("snapshot"), str)
                or not DIGEST.fullmatch(last["snapshot"])):
            return {}
        prior = last["broker_grant"]
        tools = prior["tools"]
        current = _prompt_tool_surface(context)["tools"]
        identity = _grant_session(saved, directory)
        bound = prior["bound"]
        wrapped = prior["wrap_up"]
        if (identity is None or set(prior) != {"session", "tools", "bound", "wrap_up"}
                or prior["session"] != identity or type(wrapped) is not bool
                or bound is not None and bound not in ("drain", "deadline", "requests", "tools")
                or wrapped and bound is None
                or not isinstance(tools, list) or len(tools) > len(DEFINITIONS)
                or any(not isinstance(tool, str) or not tool.startswith("mizu_")
                       or tool.removeprefix("mizu_") not in DEFINITIONS for tool in tools)
                or len(set(tools)) != len(tools)
                or context.bound_hit is not None or context.wrap_up_attempted
                or (bound == "drain" and context.project.control().get("draining"))):
            return {}
        old_tools, current_tools = set(tools), set(current)
        if old_tools == current_tools:
            return {}
        added = [tool for tool in current if tool not in old_tools]
        removed = [tool for tool in tools if tool not in current_tools]
        if bound is not None and wrapped:
            message = (f"The prior unit reached its per-run {bound} bound and then had a "
                       "wrap-up-only broker grant; that restriction applied only to the prior "
                       "unit and ended with it. This resumed unit's unit_tool_surface lists its "
                       "current grants; host enforcement remains authoritative.")
        else:
            message = ("The recorded broker tool grants differ from the prior unit. "
                       "The previous tool names are included for comparison; the record does "
                       "not establish a per-run bound as the cause, so do not infer one. "
                       "This unit's unit_tool_surface lists its current grants; host "
                       "enforcement remains authoritative.")
        return {"grant_transition": {"previous_bound": bound, "previous_tools": tools,
                                     "added_tools": added, "removed_tools": removed,
                                     "message": message}}
    except (AttributeError, ConfigError, KeyError, OSError, TypeError, ValueError):
        return {}


def session_row_details(context: Context, last, saved, directory) -> dict | None:
    """Validate the entire bounded cursor before omitting any row details.

    Only observed session identity/generation is used, never an inferred
    adapter compaction signal. Invalid/legacy cursors resend full details.
    """
    try:
        required = {"snapshot", "inbox_generation", "created_at"}
        allowed = required | {"broker_grant", "row_details"}
        if (not isinstance(last, dict) or not required <= set(last) or not set(last) <= allowed
                or not isinstance(last["snapshot"], str) or not DIGEST.fullmatch(last["snapshot"])
                or not isinstance(last["inbox_generation"], str)
                or not DIGEST.fullmatch(last["inbox_generation"])):
            return None
        created = last["created_at"]
        if (not isinstance(created, str) or len(created) > 64
                or dt.datetime.fromisoformat(created).tzinfo is None):
            return None
        details = last["row_details"]
        rows = details["rows"]
        grant = last["broker_grant"]
        from .protocol import DEFINITIONS
        tools = grant["tools"]
        if (not isinstance(grant, dict) or set(grant) != {"session", "tools", "bound", "wrap_up"}
                or not isinstance(tools, list) or len(tools) > len(DEFINITIONS)
                or any(not isinstance(tool, str) or not tool.startswith("mizu_")
                       or tool.removeprefix("mizu_") not in DEFINITIONS for tool in tools)
                or len(set(tools)) != len(tools)
                or type(grant["wrap_up"]) is not bool
                or grant["bound"] is not None and grant["bound"] not in ("drain", "deadline", "requests", "tools")
                or set(details) != {"session", "key", "rows"}
                or details["key"] != directory.name
                or details["session"] != grant["session"]
                or details["session"] != _grant_session(saved, directory)
                or details["session"] is None
                or set(rows) != {"open_work_items", "check_jobs", "shared_checks"}):
            return None
        for group, bound in (("open_work_items", context.config.limits.work_items_prompt),
                             ("check_jobs", 32), ("shared_checks", 32)):
            page = rows[group]
            if (not isinstance(page, dict) or len(page) > bound
                    or any(not isinstance(key, str) or not 0 < len(key) <= 64
                           or not isinstance(value, str) or not DIGEST.fullmatch(value)
                           for key, value in page.items())):
                return None
        return rows
    except (AttributeError, ConfigError, KeyError, OSError, TypeError, ValueError):
        return None


def session_row_evidence(context: Context, directory, grant) -> dict | None:
    """Replace cursor rows only after the driver proved same-session settlement."""
    rows = getattr(context, "prompt_row_fingerprints", None)
    if grant is None or rows is None:
        return None
    return {"session": grant["session"], "key": directory.name, "rows": rows}


def _row_detail_projection(context: Context, work: dict, checks: dict, prior=None) -> tuple:
    """Copy only display rows; never mutate captured work/order or discovery state.

    Fingerprint the complete bounded row, excluding only volatile age counters.
    Identity, status, revision, wait, liveness, truncation and discovery metadata
    always remain explicit. With elision enabled, exact-read-capable roles get
    detail references in full and delta prompts; only validated prior rows can
    additionally be marked unchanged. Current fingerprints replace (never accumulate in)
    the existing session cursor after a settled dispatch.
    """
    fingerprints = {}
    projected = {}
    caps = set(context.role.capabilities)
    for group, rows in (("open_work_items", work["items"]),
                        ("check_jobs", checks["check_jobs"]),
                        ("shared_checks", checks["shared_checks"])):
        fingerprints[group] = {}
        projected[group] = []
        for row in rows:
            identifier = row["id"]
            stable = {k: v for k, v in row.items()
                      if k not in {"age_seconds", "doing_age_seconds", "blocked_age_seconds"}}
            fingerprint = digest(canonical(stable))
            fingerprints[group][identifier] = fingerprint
            detail_keys = ({"description_preview", "scope_preview", "reason_preview"}
                           if group == "open_work_items" else {"command_preview", "error"})
            # Shared candidates are projected before full input validation.
            # `join` revalidates and subscribes, so it is not a guaranteed read.
            if (group != "shared_checks"
                    and context.config.limits.session_delta_details == "elide"
                    and ("work_items_read" if group == "open_work_items" else "check_job") in caps
                    and detail_keys.intersection(row)):
                row = {k: v for k, v in row.items() if k not in detail_keys}
                request = ({"tool": "mizu_work_items_read", "arguments": {"id": identifier}}
                           if group == "open_work_items" else
                           {"tool": "mizu_check_job", "arguments": {
                               "action": "result", "id": identifier}})
                row = {**row, "details_reference": request}
                if prior is not None and prior[group].get(identifier) == fingerprint:
                    row["details_unchanged"] = True
            projected[group].append(row)
    context.prompt_row_fingerprints = fingerprints
    return ({**work, "items": projected["open_work_items"]},
            {**checks, **{k: projected[k] for k in ("check_jobs", "shared_checks")}})


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


def _published_snapshot(context: Context) -> dict:
    proof, diagnostic = acceptance.effective_projection(context.project, context.snapshot)
    return {**{k: context.snapshot[k] for k in (
        "id", "code_digest", "created_at", "state", "summary", "verification")},
        "effective_verification": proof, "effective_verification_diagnostic": diagnostic}


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
    # references are always pinned. `admission` is the exact normalized
    # dispatch label selected by the host. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    from . import work_order as _plan
    opening = _plan._capture_opening(context)
    pending = opening["pending_insights"]
    open_work_items, checks = _row_detail_projection(
        context, opening["open_work_items"], check_jobs.prompt_projection(context))
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in caps:
        ci_branch = None
    ci_failures = opening["ci_failures"]
    ci_failures_may_be_truncated = opening["ci_truncated"]
    return json.dumps(prompt_sections.shape(context, {"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       **({"task": context.opening_task} if getattr(context, "opening_task", None) else {}),
                       "admission": context.admission,
                       "published_snapshot": _published_snapshot(context),
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       "pending_insights": pending,
                       "open_insights": opening["open_backlog"],
                       "open_work_items": open_work_items,
                       "operator_records": getattr(context, "operator_record_page", {"events": []}),
                       "decision_events": opening["decision_events"],
                       **({"decision_page": opening["decision_page"]}
                          if opening["decision_page"] is not None else {}),
                       "decision_receipt_diagnostics": opening["diagnostics"],
                       **({"work_item_events": opening["work_item_events"]}
                          if opening["work_item_events"] else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": opening["wait_events"],
                       **_ci_wait_contract(context),
                       "obligations": opening["obligations"],
                       **checks,
                       "acceptance_commands": acceptance,
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "ci_branch": ci_branch,
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": opening["ready_work"],
                       "work_order": _work_order_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)}),
                      ensure_ascii=False)


def prompt_delta_for(context: Context, pending: list, *, grant_transition: dict | None = None,
                     prior_rows: dict | None = None) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: the constant ``unit`` opening, then the names-only
    ``unit_tool_surface``, the ``goal_digest`` pin, the
    pinned ``published_snapshot`` and a small ``snapshot_delta``
    (previous/current ids), plus the normalized dispatch ``admission`` label
    and an optional ``grant_transition`` when bounded same-session evidence
    proves the broker tool surface changed, then only the pending
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
    includes its overflow count, cursor, truncation, and legacy coverage status.
    ``session_delta_details=elide`` replaces bulky previews with exact read
    references when the role holds that read capability, as in full prompts.
    A validated same-session ``prior_rows`` cursor additionally marks unchanged
    rows. Shared candidates, roles without exact reads, and the ``full`` setting
    retain their previews; state/liveness and discovery metadata always remain.
    The delta carries no workspace file contents and no history
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
    if not isinstance(pending, list):
        raise Denied("Invalid pending insights")
    from . import work_order as _plan
    opening = _plan._capture_opening(context)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_failures = opening["ci_failures"]
    ci_failures_may_be_truncated = opening["ci_truncated"]
    open_backlog = opening["open_backlog"]
    open_work_items, checks = _row_detail_projection(
        context, opening["open_work_items"], check_jobs.prompt_projection(context), prior_rows)
    return json.dumps(prompt_sections.shape(context, {"unit": UNIT_BEGINS_STATEMENT,
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       **({"task": context.opening_task} if getattr(context, "opening_task", None) else {}),
                       **(grant_transition or {}),
                       "admission": context.admission,
                       "published_snapshot": _published_snapshot(context),
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "pending_insights": pending,
                       "open_insights": open_backlog,
                       "open_work_items": open_work_items,
                       "operator_records": getattr(context, "operator_record_page", {"events": []}),
                       "decision_events": opening["decision_events"],
                       **({"decision_page": opening["decision_page"]}
                          if opening["decision_page"] is not None else {}),
                       "decision_receipt_diagnostics": opening["diagnostics"],
                       **({"work_item_events": opening["work_item_events"]}
                          if opening["work_item_events"] else {}),
                       "decision_task": _decision_task(context),
                       "wait_events": opening["wait_events"],
                       **_ci_wait_contract(context),
                       "obligations": opening["obligations"],
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       **checks,
                       "acceptance_commands": acceptance,
                       "ci_branch": ((context.config.vcs.get("ci_branch") or None)
                                     if "vcs_read" in caps else None),
                       "ci_failures": ci_failures,
                       "ci_failures_may_be_truncated": ci_failures_may_be_truncated,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "ready_work": opening["ready_work"],
                       "work_order": _work_order_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)}),
                      ensure_ascii=False)
