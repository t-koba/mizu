"""Unit-opening prompt projections: full and resumed delta views.

This module owns one model-facing contract: the full unit prompt
(:func:`prompt_for`) and the resumed-session delta (:func:`prompt_delta_for`)
plus their tightly coupled bounded projection helpers and exact
unit-opening statement. Snapshot proof and its diagnostic are projected together
for both prompt variants.

Fixed ``ci_head_state`` facts come from the declared observation driver
(:func:`_head_state_prompt_facts`); its observation is a readable prompt fact
that does not wake the role, so CI admission stays kernel-side. Opt-in
``ci_summary`` aggregates are projected by the ``ci_summary`` observation
driver the same way. Session selection and its rotation/recovery/fallback
policy (:func:`session_prompt`) now also live here, re-exported by ``runtime``.
Work-ready scheduling (``work_readiness._has_ready_work`` and
``work_readiness._ready_work_changed``, re-exported by ``runtime``, plus
``role_admission.should_run``, re-exported by ``runtime``), admission and the
scheduler stay in ``runtime``. This module also owns the display-only ready
work projection and its age formatter; the projection preserves its
``ready_work_signature`` side effect without depending back on runtime.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import acceptance, check_jobs, prompt_sections, reviews
from .head_state_observation import ci_head_state_facts
from .ci_observation import MAX_CI_WAKE_ROWS, _valid_ci_summary_fact
from .summary_observation import ci_summary_facts
from .errors import ConfigError, Denied
from .fs import DIGEST, canonical, digest, read_json
from .protocol import _writer_continuation_contract, tool_definitions
from .report import previous as _previous_report
from .snapshot import (missing_finish_recovery_due, missing_finish_streak,
                       no_progress_recovery_due)
from .work_readiness import _has_ready_work

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


def _head_state_prompt_facts(context: Context):
    """Project the driver observation as a readable prompt fact.

    The fixed key carries the observation's rows to the role. The same opening
    also surfaces the appended observation in ``context.queues`` so the fact is
    visible without waking the role; CI admission stays kernel-side.
    """
    caps = set(context.role.capabilities)
    if "vcs_request" not in caps or "ci_head_state" not in context.config.drivers:
        return []
    rows = ci_head_state_facts(context.project, context.config)
    from . import queue_facts
    context.queues = queue_facts.opening(context.project, context.role.name)
    return rows


def _ci_summary_prompt_facts(context: Context):
    """Project the opt-in aggregate observation as a readable prompt fact.

    The driver appends a non-waking observation; the same opening surfaces it
    in ``context.queues`` so the fact is visible without waking the role. CI
    admission stays kernel-side. The projection is opt-in via the ``ci``
    section and fails closed when the driver is undeclared.
    """
    if "ci" not in prompt_sections.included(context.role):
        return []
    rows = ci_summary_facts(context.project, context.config)
    from . import queue_facts
    context.queues = queue_facts.opening(context.project, context.role.name)
    return rows


def _ci_wait_contract(context: Context) -> dict:
    if not getattr(context, "ci_wait_manifest", ()):
        return {}
    return {"ci_wait_delivery": (
        "CI wait ack_tokens are enabled. Select only handled tokens in finish.ack_events; "
        "omitted/empty/partial selections leave other rows pending oldest-first. "
        "Finishing alone acknowledges no CI rows. Current blocked revision, holder, "
        "publication and evidence are revalidated. Snapshot-bound publication CI events "
        "state snapshot_relation as exact or strict_descendant; the host validates this "
        "relation against the pinned snapshot, and strict_descendant also names "
        "observed_snapshot. Bounded/bound-hit units and finishes waiting for another check "
        "acknowledge no CI rows. Evidence is external-untrusted; handling never transitions "
        "a work item.")}


def _proposal_wait_contract(context: Context) -> dict:
    if not getattr(context, "proposal_wait_manifest", ()):
        return {}
    return {"proposal_wait_delivery": (
        "Exact change-proposal merge wait ack_tokens are enabled. Select only handled tokens "
        "in finish.ack_events; omitted/empty/partial selections leave other rows pending "
        "oldest-first. Finishing alone acknowledges no proposal rows. The current blocked "
        "revision, holder and exact merge evidence are revalidated. Bounded/bound-hit units "
        "and finishes waiting for another check acknowledge no proposal rows. Evidence is "
        "external-untrusted; observing a merge or acknowledging it never transitions a work item.")}


def _unit_begins_statement(outcomes: tuple[str, ...]) -> str:
    # Keep the opening first and stable for a given role across full and delta prompts.
    return ("A new work unit begins. This is not an acknowledgement of a previous "
            "seal: do useful work within your role and capabilities, then finish "
            "with mizu_finish and a short state. " + _writer_continuation_contract(outcomes))


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

    The projection carries the bounded ``unknown_blocked`` count,
    ``coverage_complete`` and the role's capacity facts from the ready scan,
    so an incomplete scan never reads as an exhaustive zero and a held item
    with a pending wait is explained without changing readiness authority.
    """
    if type(page.get("unknown_blocked")) is not int or page["unknown_blocked"] < 0:
        raise Denied("Invalid ready-work unknown count")
    if type(page.get("coverage_complete")) is not bool:
        raise Denied("Invalid ready-work coverage")
    for key, minimum in (("capacity_in_use", 0), ("capacity_limit", 1),
                         ("held_pending_waits", 0)):
        if type(page.get(key)) is not int or page[key] < minimum:
            raise Denied(f"Invalid ready-work {key}")
    if context is not None:
        context.ready_work_signature = page["signature"]
    current_time = time.time()
    return {"items": [{**{key: row[key] for key in
                        ("id", "title", "status", "updated_at",
                         "doing_since", "doing_age_seconds", "doing_stale")},
                       "age_seconds": _ready_work_age(row["updated_at"], current_time),
                       **({key: row[key] for key in ("rev", "holder", "wait_resolution")}
                          if "wait_resolution" in row else {}),
                       **({"wait": row["wait"], "wait_liveness": row["wait_liveness"],
                           "wait_satisfied": row["wait_liveness"]["state"] == "due"}
                          if "wait" in row and "wait_liveness" in row else {})}
                      for row in page["items"]],
            **{key: page[key] for key in ("total", "overflow_count", "truncated",
                                         "coverage_complete", "unknown_blocked",
                                         "capacity_in_use", "capacity_limit",
                                         "held_pending_waits")}}


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
        "tools": [tool["name"] for tool in tool_definitions(
            context.role.capabilities, role=context.role, config=context.config)],
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

    Legacy/malformed/unknown records cannot justify a grant claim. A prior unit's per-run bound explains a wrap-up-only
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
        required = {"snapshot", "created_at"}
        allowed = required | {"broker_grant", "row_details", "sections"}
        if (not isinstance(last, dict) or not required <= set(last) or not set(last) <= allowed
                or not isinstance(last["snapshot"], str) or not DIGEST.fullmatch(last["snapshot"])
                or ("sections" in last and not (isinstance(last["sections"], list)
                                                and all(isinstance(s, str) for s in last["sections"])))):
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
        **({"next_step": context.snapshot["next_step"]} if "next_step" in context.snapshot else {}),
        "effective_verification": proof, "effective_verification_diagnostic": diagnostic}


def _external_check_wake(context: Context) -> dict:
    """Copy dispatch-frozen aggregates, never rereading the poll cache.

    Malformed entries are dropped within the cache row envelope. The details
    route is offered once, only with vcs_read; local check jobs are unrelated.
    """
    stored = getattr(context, "external_check_wake_facts", [])
    facts = ([dict(fact) for fact in stored[:MAX_CI_WAKE_ROWS]
              if _valid_ci_summary_fact(fact)] if isinstance(stored, list) else [])
    details = ({"tool": "mizu_vcs_read", "operations": ["status", "log"]}
               if "vcs_read" in context.role.capabilities else None)
    return {"facts": facts, "details": details}


def _validate_opening(payload: dict) -> dict:
    """Fail closed if nested per-check external observations reach an opening.

    Only external row signatures and passed_on annotations are forbidden;
    local check-job records, CI-wait terminal rows, check-name selectors and
    aggregates remain valid.
    """
    def walk(value):
        if isinstance(value, dict):
            if "passed_on" in value or {"branch", "sha", "check"} <= value.keys():
                raise Denied("Per-check external observation in unit opening")
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    try:
        walk(payload)
    except RecursionError as exc:
        raise Denied("Invalid nested unit opening") from exc
    return payload


def prompt_for(context: Context) -> str:
    # The charter body belongs to stable engine system context on every
    # dispatch (including resume); full and delta unit prompts carry its pin.
    caps = set(context.role.capabilities)
    # Mechanism enforces grants, not policy choice: capability-gated context
    # is offered where appropriate, alongside the always-present, bounded
    # project-wide work-item page. `queues` states, per readable queue with
    # due unacknowledged messages, the count and the newest
    # `[limits] queue_opening_messages` messages; `recent_snapshots` follows `[limits] prompt_snapshots`. Exact snapshot
    # references are always pinned. `admission` is the exact normalized
    # dispatch label selected by the host. Publication/commit and `should_run`
    # transitions below are the fixed single-writer contract, not policy.
    from . import work_order as _plan
    ci_head_state = _head_state_prompt_facts(context)
    ci_summary = _ci_summary_prompt_facts(context)
    opening = _plan._capture_opening(context)
    open_work_items, checks = _row_detail_projection(
        context, opening["open_work_items"], check_jobs.prompt_projection(context))
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in set(context.role.capabilities):
        ci_branch = None
    return json.dumps(_validate_opening(prompt_sections.shape(context, {
                       "unit": _unit_begins_statement(context.role.finish_outcomes),
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       "admission": context.admission,
                       "external_check_wake": _external_check_wake(context),
                       "published_snapshot": _published_snapshot(context),
                       "recent_snapshots": [{k: s[k] for k in ("id", "created_at", "outcome", "summary", "code_digest")}
                                            for s in context.project.snapshots.history(
                                                context.snapshot["id"], context.config.limits.prompt_snapshots)],
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       **reviews.prompt(context),
                       "publication_state": reviews.publication_state(context),
                       "queues": list(getattr(context, "queues", [])),
                       "open_work_items": open_work_items,
                       **({"work_item_events": opening["work_item_events"]}
                          if opening["work_item_events"] else {}),
                       "wait_events": opening["wait_events"],
                       **_ci_wait_contract(context),
                       **_proposal_wait_contract(context),
                       "ready_work": opening["ready_work"],
                       **checks,
                       "acceptance_commands": acceptance,
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "ci_branch": ci_branch,
                       "ci_summary": ci_summary,
                       "ci_head_state": ci_head_state,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "work_order": _work_order_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)})),
                      ensure_ascii=False)


def prompt_delta_for(context: Context, *, grant_transition: dict | None = None,
                     prior_rows: dict | None = None) -> str:
    """Append-only delta for a resumed persistent session.

    Schema: the constant ``unit`` opening, then the names-only
    ``unit_tool_surface``, fixed ``external_check_wake`` aggregates and details
    route, the ``goal_digest`` pin, the
    pinned ``published_snapshot`` and a small ``snapshot_delta``
    (previous/current ids), plus the normalized dispatch ``admission`` label
    and an optional ``grant_transition`` when bounded same-session evidence
    proves the broker tool surface changed, then the ``queues`` facts (due
    unacknowledged counts and newest messages per readable queue, restated
    every unit), the always-present bounded ``open_work_items`` page,
    workspace markers, capability-gated acceptance commands, and a
    capability-gated ``previous_report`` preview for roles holding
    ``report``. Fixed ``ci_branch`` configuration is gated by ``vcs_read``.
    Fixed ``ci_head_state`` carries one bounded head-state fact per configured
    CI branch for roles holding ``vcs_request``, and an empty array otherwise;
    it is not a wake or request rule and is independent of the ``ci`` section.
    The opt-in ``ci`` section contains only aggregate summaries for all
    observed subjects, never per-check rows.
    The independent bounded ``ready_work`` projection follows
    this stable context in the volatile suffix, before run identity and clock.
    The charter body is supplied separately in stable system instructions;
    ``recent_snapshots`` history is not repeated in deltas. Key order is fixed (stable context
    first, volatile ready-work age, run identity and clock last) so
    repeated prompts preserve as much identical prefix as possible.
    Bounds: newest queue messages follow ``[limits] queue_opening_messages``;
    the work-item page follows ``[limits] work_items_prompt`` and includes its
    overflow count, cursor and truncation.
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
    from . import work_order as _plan
    ci_head_state = _head_state_prompt_facts(context)
    ci_summary = _ci_summary_prompt_facts(context)
    opening = _plan._capture_opening(context)
    acceptance = list(context.project.verify) if "verify" in caps else []
    ci_branch = context.config.vcs.get("ci_branch") or None
    if "vcs_read" not in set(context.role.capabilities):
        ci_branch = None
    open_work_items, checks = _row_detail_projection(
        context, opening["open_work_items"], check_jobs.prompt_projection(context), prior_rows)
    return json.dumps(_validate_opening(prompt_sections.shape(context, {
                       "unit": _unit_begins_statement(context.role.finish_outcomes),
                       "unit_tool_surface": _prompt_tool_surface(context),
                       "goal_digest": context.goal_digest,
                       **(grant_transition or {}),
                       "admission": context.admission,
                       "external_check_wake": _external_check_wake(context),
                       "published_snapshot": _published_snapshot(context),
                       "snapshot_delta": {"id": context.snapshot["id"],
                                          "code_digest": context.snapshot["code_digest"]},
                       "queues": list(getattr(context, "queues", [])),
                       "open_work_items": open_work_items,
                       **({"work_item_events": opening["work_item_events"]}
                          if opening["work_item_events"] else {}),
                       "wait_events": opening["wait_events"],
                       **_ci_wait_contract(context),
                       **_proposal_wait_contract(context),
                       "research_state": _prompt_research_state(context),
                       "capture_limit": _prompt_capture_limit(context),
                       "workspace": "/workspace", "workspace_mode": context.role.workspace,
                       **reviews.prompt(context),
                       "publication_state": reviews.publication_state(context),
                       "ready_work": opening["ready_work"],
                       **checks,
                       "acceptance_commands": acceptance,
                       "ci_branch": ci_branch,
                       "ci_summary": ci_summary,
                       "ci_head_state": ci_head_state,
                       "previous_report": (_previous_report(context.project)
                                          if "report" in caps else None),
                       "work_order": _work_order_projection(context),
                       "run": _prompt_run_identity(context),
                       **_prompt_clock(context)})),
                      ensure_ascii=False)


def session_prompt(context: Context, role, run_dir: Path) -> tuple:
    """Choose the full or delta prompt for this dispatch.

    Schema: peeks at the content-bound session record for the role profile;
    returns ``(prompt, mode, session_key, rotation, session_dir)`` where mode
    is ``"full"`` or ``"delta"`` and rotation is the ``rotation.json`` record
    (or None). Bounds: prompt-state and rotation evidence are small JSON
    files. Trust: local
    session state only. Retry/cancellation: peeking never raises (any failure
    falls back to the full prompt and empty identity), except an enforcing
    due rotation, which propagates so the unit fails before model dispatch
    instead of admitting the old conversation. No-progress recovery also
    rotates a due persistent session before dispatch, even if its session
    record is missing. Missing-finish recovery likewise rotates a persistent
    session whose consecutive-missing-finish streak reached
    ``[limits] missing_finish_recovery_after`` when the filtered ready-work
    scan still shows actionable (open/doing) work without structured waits;
    a queue with no ready work never rotates. Evidence: rotations write
    ``rotation.json``; dispatches record the mode in ``prompt_projection.json``;
    failed rotations are stored
    in the run ``error.json`` by the Engine caller. Failure: a due rotation removes
    ``session.json`` so the next dispatch mints a fresh provider session
    while the published snapshot and composed policy reload unchanged; a
    missing record otherwise takes the full prompt.
    """
    from .engine_config import (effective, read_prompt_state, rotate_session,
                                rotation_due, session_record)
    if getattr(context, "ephemeral", False):
        return prompt_for(context), "full", "", None, None
    try:
        settings = effective(context.config, role, role.profile)
    except Exception:
        return prompt_for(context), "full", "", None, None
    if settings.get("session") != "persistent":
        return prompt_for(context), "full", "", None, None
    try:
        path, saved = session_record(context, role.profile, settings)
    except Exception:
        return prompt_for(context), "full", "", None, None
    session_dir = path.parent
    key = session_dir.name
    progress = context.snapshot.get("progress")
    recovery_due = no_progress_recovery_due(
        progress, context.snapshot.get("outcome", ""), context.config, context.role.name)
    recovery_reason = (
        f"no-progress units {progress['no_progress_count']} "
        f">={context.config.limits.no_progress_recovery_after}") if recovery_due else None
    missing_recovery_due = False
    missing_reason = None
    try:
        health_record = read_json(context.project.root / "health" / f"{context.role.name}.json", {})
        if isinstance(health_record, dict) and missing_finish_recovery_due(health_record, context.config):
            streak = missing_finish_streak(health_record)
            if _has_ready_work(context.project, context.role):
                missing_recovery_due = True
                missing_reason = (
                    f"missing-finish units {streak} "
                    f">={context.config.limits.missing_finish_recovery_after}")
    except Exception:
        missing_recovery_due = False
        missing_reason = None
    if saved is None:
        if recovery_due:
            record = rotate_session(path, run_dir, recovery_reason)
            return prompt_for(context), "full", key, record, session_dir
        if missing_recovery_due:
            record = rotate_session(path, run_dir, missing_reason)
            return prompt_for(context), "full", key, record, session_dir
        return prompt_for(context), "full", key, None, session_dir
    try:
        due, reason = rotation_due(context.config.limits, path, saved,
                                   settings.get("engine", "unknown"))
    except Exception:
        due, reason = False, ""
    if due:
        # A due rotation enforces a configured bound: its failure must fail
        # the work unit before model dispatch (Engine records error.json),
        # never substitute a full prompt and admit the old conversation.
        record = rotate_session(path, run_dir, reason)
        return prompt_for(context), "full", key, record, session_dir
    if recovery_due:
        record = rotate_session(path, run_dir, recovery_reason)
        return prompt_for(context), "full", key, record, session_dir
    if missing_recovery_due:
        record = rotate_session(path, run_dir, missing_reason)
        return prompt_for(context), "full", key, record, session_dir
    try:
        from . import work_order as _plan
        _plan._capture_opening(context)
        last = read_prompt_state(session_dir)
    except Exception:
        return prompt_for(context), "full", key, None, session_dir
    try:
        prior_rows = session_row_details(context, last, saved, session_dir)
        transition = session_grant_transition(context, last, saved, session_dir)
        prompt = prompt_delta_for(context, grant_transition=transition, prior_rows=prior_rows)
        return prompt, "delta", key, None, session_dir
    except Exception:
        return prompt_for(context), "full", key, None, session_dir
