"""Writer's explicit work order: bounded atomic CAS over actionable IDs.

The plan records only the writer's ordered unique actionable work-item IDs
plus a bounded short rationale and the exact semantic input generation
reviewed. WorkItems stays authoritative for status, scope and evidence; no
field here duplicates them. The generation binds semantic inputs captured
for the unit opening: goal digest, exact bounded open-work page, bounded
ready-work display (without volatile ages) plus the full actionable inventory
signature and a full open-inventory accepted-scope/redecision fingerprint,
the full bounded pending-insight inventory plus open-insight backlog (when
granted), the bounded CI-failure projection (when granted), and the
dispatch-frozen configured actionable inputs (decision events and their
visible page summary, corrupt-receipt diagnostics, work-item terminal events,
structured insight waits including due waits, and obligations). Full prompts
display the pending inventory; resumed-session deltas may omit already-offered
bodies while reusing the same frozen inventory for generation and backlog.
It is captured once per unit so arrivals after opening are not retroactively
treated as reviewed.
A change in the configured actionable-input set -- including an event
disappearing after acknowledgement -- is a semantic input-set change and
may legitimately make the next unit's order review_due; there is no
requirement to preserve current status across acknowledgements and no
ack-stable historical scan. A stored generation that differs from the
opening one reads ``review_due``; a missing file reads ``absent``; an
unreadable or malformed store reads ``unavailable`` (fail closed, never
empty/current). A valid plan only changes what the writer sees;
ready-work, admission and selection are untouched.
"""
from __future__ import annotations

import re
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import DIGEST, canonical, digest, identifier, now, safe_read, write_json
from .work_item_seed import ITEM_ID

#: Ordered plan IDs are bounded by the fixed maximum work-item page size.
MAX_WORK_ORDER_ITEMS = 100
#: Separate bounded rationale cap (short, operator-readable).
MAX_RATIONALE_CHARS = 1024
#: Stored record byte ceiling (envelope plus rationale, no item bodies).
MAX_WORK_ORDER_BYTES = 8192
#: Read statuses exposed in prompts and operator status.
STATUSES = frozenset({"absent", "current", "review_due", "unavailable"})

_SCHEMA = 2


def _directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise Denied("Unsafe work-order directory")
    return True


def _root(project_root: Path) -> Path:
    return project_root / "work-order"


def _current_path(project_root: Path) -> Path:
    return _root(project_root) / "current.json"


def _validate_items(items) -> list:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_WORK_ORDER_ITEMS:
        raise Denied("Work order must list 1-100 work-item IDs")
    seen = set()
    for entry in items:
        if not isinstance(entry, str) or ITEM_ID.fullmatch(entry) is None:
            raise Denied("Invalid work-order item ID")
        if entry in seen:
            raise Denied("Duplicate work-order item ID")
        seen.add(entry)
    return list(items)


def _validate_rationale(value) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_RATIONALE_CHARS
            or not value.strip() or "\x00" in value):
        raise Denied("Work-order rationale must be 1-1024 nonblank characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Denied("Work-order rationale must be valid UTF-8") from exc
    return value


def _validate_generation(value) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise Denied("Invalid work-order generation")
    return value


def _validate_rev(value) -> int:
    if type(value) is not int or value < 0:
        raise Denied("Work-order revision must be a non-negative integer")
    return value


def _validate_run(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise Denied("Invalid work-order run ID")
    return value


def _read_record(project_root: Path, name: str) -> dict:
    import datetime
    import json
    raw = safe_read(project_root, name, MAX_WORK_ORDER_BYTES)
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict) or canonical(record) != raw:
        raise Denied("Noncanonical work-order record")
    fields = {"schema", "rev", "generation", "items", "rationale", "actor", "updated_at"}
    schema = record.get("schema")
    if (type(schema) is not int or schema not in (1, _SCHEMA)
            or set(record) != (fields if schema == 1 else fields | {"run"})
            or type(record["rev"]) is not int or record["rev"] < 1):
        raise Denied("Invalid work-order record")
    if schema == _SCHEMA:
        _validate_run(record["run"])
    identifier(record["actor"])
    _validate_items(record["items"])
    _validate_rationale(record["rationale"])
    _validate_generation(record["generation"])
    if datetime.datetime.fromisoformat(record["updated_at"]).tzinfo is None:
        raise Denied("Invalid work-order timestamp")
    if len(canonical(record)) > MAX_WORK_ORDER_BYTES:
        raise Denied("Work-order record exceeds bound")
    return record


def _read_stored(project_root: Path) -> dict:
    """Read only the canonical current record; legacy audit files are inert."""
    absent = {"status": "absent", "rev": 0, "record": None}
    try:
        if not _directory(_root(project_root)):
            return absent
        try:
            _current_path(project_root).lstat()
        except FileNotFoundError:
            return absent
        record = _read_record(project_root, "work-order/current.json")
        return {"status": "current", "rev": record["rev"], "record": record}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError,
            Denied, LimitExceeded):
        return {"status": "unavailable", "rev": None, "record": None}


def _stable_ready_for_hash(ready_work) -> dict:
    """Stable ready display without volatile age/review indicators."""
    if not isinstance(ready_work, dict) or not isinstance(ready_work.get("items"), list):
        raise Denied("Invalid ready-work projection")
    items = ready_work.get("items")
    stable_items = []
    for row in items:
        if not isinstance(row, dict):
            raise Denied("Invalid ready-work row")
        stable_items.append({k: v for k, v in row.items()
                             if k not in {"age_seconds", "doing_age_seconds", "doing_stale"}})
    stable = dict(ready_work)
    stable["items"] = stable_items
    # Signature lives separately; display never carries it.
    stable.pop("signature", None)
    if not isinstance(stable.get("total"), int):
        raise Denied("Invalid ready-work projection")
    if type(stable.get("unknown_blocked")) is not int or stable["unknown_blocked"] < 0:
        raise Denied("Invalid ready-work unknown count")
    if type(stable.get("coverage_complete")) is not bool:
        raise Denied("Invalid ready-work coverage")
    return stable


def _stable_open_for_hash(open_work_items) -> dict:
    """Keep hold age/review visibility out of the durable work generation."""
    if not isinstance(open_work_items, dict) or not isinstance(open_work_items.get("items"), list):
        raise Denied("Invalid open-work page")
    stable = dict(open_work_items)
    stable["items"] = []
    for row in open_work_items["items"]:
        if not isinstance(row, dict):
            raise Denied("Invalid open-work row")
        stable["items"].append({k: v for k, v in row.items()
                                if k not in {"doing_age_seconds", "doing_stale"}})
    return stable


def generation_from_captured(*, goal_digest, open_work_items, ready_work,
                             ready_signature, order_inventory, pending_insights, open_backlog,
                             ci_failures, ci_truncated, decision_events, decision_page,
                             diagnostics, work_item_events, wait_events,
                             obligations) -> str:
    """Hash the captured prompt projections and full scope/redecision fingerprint.

    The prompt projections are captured for this unit (goal pin, bounded
    open page, ready display and inventory signature, pending list and
    backlog, CI failures, and dispatch-frozen event arrays including
    structured waits). The separate constant-size scope/redecision
    fingerprint covers the full open inventory, not just the visible page. Volatile ready ages are stripped so wall time alone never
    moves the generation. Any malformed input raises Denied for the
    caller to map to ``unavailable``. Age/review fields are prompt visibility,
    not work identity, and never move the generation as time passes.
    """
    _validate_generation(goal_digest)
    if (not isinstance(open_work_items, dict)
            or not isinstance(open_work_items.get("items"), list)
            or not isinstance(open_work_items.get("total"), int)):
        raise Denied("Invalid open-work page")
    if not isinstance(ready_signature, str) or DIGEST.fullmatch(ready_signature) is None:
        raise Denied("Invalid ready signature")
    _validate_generation(order_inventory)
    stable_open = _stable_open_for_hash(open_work_items)
    stable_ready = _stable_ready_for_hash(ready_work)
    if not isinstance(pending_insights, list):
        raise Denied("Invalid pending insights")
    if (not isinstance(open_backlog, dict)
            or not isinstance(open_backlog.get("ids"), list)
            or not isinstance(open_backlog.get("total"), int)
            or not isinstance(open_backlog.get("truncated"), bool)):
        raise Denied("Invalid open-insight backlog")
    if ci_failures is None:
        if ci_truncated is not None:
            raise Denied("Invalid CI projection")
    elif (not isinstance(ci_failures, list)
          or (ci_truncated is not None and not isinstance(ci_truncated, bool))):
        raise Denied("Invalid CI projection")
    if decision_page is not None and (not isinstance(decision_page, dict) or "events" in decision_page):
        raise Denied("Invalid decision page summary")
    for name, value in (("decision_events", decision_events),
                        ("diagnostics", diagnostics),
                        ("work_item_events", work_item_events),
                        ("wait_events", wait_events),
                        ("obligations", obligations)):
        if not isinstance(value, list):
            raise Denied(f"Invalid {name}")
    return digest(canonical(["work-order-generation-v5", goal_digest,
                             stable_open, stable_ready, ready_signature, order_inventory,
                             pending_insights, open_backlog,
                             [ci_failures, ci_truncated],
                             decision_events, decision_page, diagnostics,
                             work_item_events, wait_events, obligations]))


def _decision_page_summary(page):
    return {key: value for key, value in page.items() if key != "events"} if page is not None else None


def _capture_opening(context) -> dict:
    """Capture the unit-opening projections once; reuse for prompt and review.

    Reads each projection a single time and reuses the same objects for
    both the full prompt and the frozen generation, so an arrival between
    the two cannot enter the generation. A delta displays its offered subset,
    but hashes this same full semantic pending inventory. Later calls on the same context
    return the first capture; a prior failure is not retried mid-unit.
    Display-critical store failures propagate (fail-closed prompt);
    generation-hash failures are cached as None for unavailable.
    """
    cached = getattr(context, "_work_order_inputs", None)
    if isinstance(cached, dict) and cached.get("_captured") is True:
        return cached
    if getattr(context, "_work_order_generation", False) is None:
        raise Denied("Work-order review is unavailable")
    try:
        goal_digest = getattr(context, "goal_digest", None)
        _validate_generation(goal_digest)
        caps = set(getattr(context.role, "capabilities", ()))
        project = context.project
        config = context.config
        limit_open = config.limits.work_items_prompt
        if "work_order" in caps:
            open_work_items, order_inventory = project.work_items.page_open_for_order(limit=limit_open)
        else:
            open_work_items = project.work_items.page_open(limit=limit_open)
            order_inventory = None
        if (not isinstance(open_work_items, dict)
                or not isinstance(open_work_items.get("items"), list)
                or not isinstance(open_work_items.get("total"), int)):
            raise Denied("Invalid open-work page")
        raw_ready = project.work_items.page_ready(
            limit=limit_open, role_name=context.role.name)
        if (not isinstance(raw_ready, dict)
                or not isinstance(raw_ready.get("signature"), str)
                or DIGEST.fullmatch(raw_ready["signature"]) is None):
            raise Denied("Invalid ready signature")
        ready_signature = raw_ready["signature"]
        from .unit_prompts import _ready_work_projection
        ready_work = _ready_work_projection(raw_ready, context=context)
        limit_pending = config.limits.pending_insights
        if "insights" in caps or "decide" in caps:
            proj = project.insights.projection(limit=limit_pending)
            if (not isinstance(proj, dict)
                    or not isinstance(proj.get("items"), list)
                    or not isinstance(proj.get("total"), int)):
                raise Denied("Invalid pending-insight projection")
            pending_insights = list(proj["items"])
            open_backlog = {"ids": [item["id"] for item in pending_insights],
                            "total": proj["total"],
                            "truncated": bool(proj.get("truncated"))}
        else:
            pending_insights = []
            open_backlog = {"ids": [], "total": 0, "truncated": False}
        if "vcs_read" in caps:
            from .ci_observation import ci_failures_projection as _proj
            rows, truncated = _proj(project)
            if (not isinstance(rows, list)
                    or (truncated is not None and not isinstance(truncated, bool))):
                raise Denied("Invalid CI failure projection")
            ci_failures, ci_truncated = rows, truncated
        else:
            ci_failures, ci_truncated = None, None
        decision_events = list(getattr(context, "decision_events", []) or [])
        decision_page = _decision_page_summary(getattr(context, "decision_page", None))
        diagnostics = list(getattr(context, "decision_receipt_diagnostics", []) or [])
        work_item_events = list(getattr(context, "work_item_events", []) or [])
        wait_events = list(getattr(context, "wait_events", []) or [])
        obligations = list(getattr(context, "obligations", []) or [])
        inputs = {"_captured": True, "goal_digest": goal_digest,
                  "open_work_items": open_work_items,
                  "ready_work": ready_work, "ready_signature": ready_signature,
                  "_order_inventory": order_inventory,
                  "pending_insights": pending_insights,
                  "open_backlog": open_backlog,
                  "ci_failures": ci_failures, "ci_truncated": ci_truncated,
                  "decision_events": decision_events, "decision_page": decision_page,
                  "diagnostics": diagnostics,
                  "work_item_events": work_item_events,
                  "wait_events": wait_events, "obligations": obligations}
        if "work_order" not in caps:
            context._work_order_inputs = inputs
            return inputs
        try:
            generation = generation_from_captured(
                goal_digest=goal_digest, open_work_items=open_work_items,
                ready_work=ready_work, ready_signature=ready_signature, order_inventory=order_inventory,
                pending_insights=pending_insights, open_backlog=open_backlog,
                ci_failures=ci_failures, ci_truncated=ci_truncated,
                decision_events=decision_events, decision_page=decision_page, diagnostics=diagnostics,
                work_item_events=work_item_events, wait_events=wait_events,
                obligations=obligations)
        except Exception:
            # Hash failure is review-unavailable, not a prompt failure:
            # keep the display inputs so the prompt still builds.
            inputs["_generation"] = None
            context._work_order_inputs = inputs
            context._work_order_generation = None
            return inputs
        inputs["_generation"] = generation
        context._work_order_inputs = inputs
        context._work_order_generation = generation
        return inputs
    except (Denied, LimitExceeded):
        # Display-critical store failures fail the prompt closed without
        # caching: the delta-to-full fallback must observe the same
        # fail-closed cause rather than a cached generic.
        raise


def _frozen_generation(context) -> str | None:
    """Capture the unit-opening generation once; arrivals stay unreviewed."""
    cached = getattr(context, "_work_order_generation", False)
    if isinstance(cached, str) and DIGEST.fullmatch(cached) is not None:
        return cached
    if cached is None:
        return None
    if cached is not False:
        return None
    # If prompt capture already ran, reuse its generation without rereading.
    inputs = getattr(context, "_work_order_inputs", None)
    if isinstance(inputs, dict) and isinstance(inputs.get("_generation"), str):
        if DIGEST.fullmatch(inputs["_generation"]) is not None:
            context._work_order_generation = inputs["_generation"]
            return inputs["_generation"]
    try:
        captured = _capture_opening(context)
    except (Denied, LimitExceeded, OSError, ValueError, TypeError, AttributeError):
        # Distinguish display inputs captured but hash failed (unavailable)
        # from a store failure that already failed the prompt: both read
        # as unavailable here without retrying mid-unit.
        existing = getattr(context, "_work_order_inputs", None)
        if isinstance(existing, dict) and "_generation" in existing:
            context._work_order_generation = existing["_generation"]
            return existing["_generation"]
        context._work_order_generation = None
        return None
    gen = captured.get("_generation")
    if isinstance(gen, str) and DIGEST.fullmatch(gen) is not None:
        return gen
    context._work_order_generation = None
    return None


def _fresh_generation_for_role(project, role) -> str:
    """Fresh current generation from the same captured sources.

    Does not write or refresh the plan. Source projections use their existing
    locks and recognized recovery/pruning behavior; no scheduling occurs.
    It hashes the next-unit projections and full open-inventory fingerprint,
    including live dispatch-equivalent event sets.
    """
    try:
        goal = project.goal
    except Exception as exc:
        raise Denied("Work-order goal is unavailable") from exc
    if not isinstance(goal, str) or not goal.strip():
        raise Denied("Work-order goal is unavailable")
    from .fs import digest as _digest
    goal_digest = _digest(goal.encode("utf-8"))
    limit_open = project.config.limits.work_items_prompt
    open_work_items, order_inventory = project.work_items.page_open_for_order(limit=limit_open)
    raw_ready = project.work_items.page_ready(limit=limit_open, role_name=role.name)
    ready_signature = raw_ready["signature"]
    from .unit_prompts import _ready_work_projection
    ready_work = _ready_work_projection(raw_ready)
    caps = set(getattr(role, "capabilities", ()))
    limit_pending = project.config.limits.pending_insights
    if "insights" in caps or "decide" in caps:
        proj = project.insights.projection(limit=limit_pending)
        pending_insights = list(proj["items"])
        open_backlog = {"ids": [item["id"] for item in proj["items"]],
                        "total": proj["total"],
                        "truncated": bool(proj.get("truncated"))}
    else:
        pending_insights = []
        open_backlog = {"ids": [], "total": 0, "truncated": False}
    if "vcs_read" in caps:
        from .ci_observation import ci_failures_projection as _proj
        rows, truncated = _proj(project)
        if (not isinstance(rows, list)
                or (truncated is not None and not isinstance(truncated, bool))):
            raise Denied("Invalid CI failure projection")
        ci_failures, ci_truncated = rows, truncated
    else:
        ci_failures, ci_truncated = None, None
    from . import decision_delivery as _delivery
    from . import work_item_events as _tev
    from . import ci_waits as _ciw
    decision_state = _delivery.pending(project, role)
    decision_events = list(decision_state.get("events", []) or [])
    decision_page = _decision_page_summary(decision_state.get("page"))
    diagnostics = list(decision_state.get("diagnostics", []) or [])
    terminal_page = _tev.pending(
        project.work_items, role.name, getattr(role, "work_item_events", ()),
        sources=getattr(role, "work_item_sources", "own") or "own",
        limit=__import__("mizu.config", fromlist=["role_work_item_events_limit"]).role_work_item_events_limit(project.config, role))
    terminal_page = _tev.delivery(role.name, terminal_page)
    work_item_events = list(terminal_page.get("events", []) or [])
    try:
        snapshot = project.snapshots.get()
    except Exception as exc:
        raise Denied("Work-order snapshot is unavailable") from exc
    wait_events = list(project.insights.due_waits(role.name, snapshot)
                       + _ciw.pending(project.work_items, role.name, config=project.config))
    obligations = list(project.insights.pending_obligations(role.name))
    return generation_from_captured(
        goal_digest=goal_digest, open_work_items=open_work_items,
        ready_work=ready_work, ready_signature=ready_signature, order_inventory=order_inventory,
        pending_insights=pending_insights, open_backlog=open_backlog,
        ci_failures=ci_failures, ci_truncated=ci_truncated,
        decision_events=decision_events, decision_page=decision_page, diagnostics=diagnostics,
        work_item_events=work_item_events, wait_events=wait_events,
        obligations=obligations)


def projection_for(context) -> dict | None:
    """Prompt projection for the owning role; None when the grant is absent.

    Returns ``{"status", "rev", "generation", "items", "rationale", "actor",
    "updated_at", "opening_generation"}`` on ``current``/``review_due``
    (bounded), and ``opening_generation`` on ``absent``. Corrupt state reads
    ``unavailable``, never empty/current. Never raises for prompt use.
    """
    try:
        caps = set(getattr(context.role, "capabilities", ()))
    except Exception:
        return {"status": "unavailable", "rev": None, "generation": None}
    if "work_order" not in caps:
        return None
    try:
        opening = _frozen_generation(context)
    except Exception:
        opening = None
    try:
        stored = _read_stored(context.project.root)
    except Exception:
        return {"status": "unavailable", "rev": None, "generation": None}
    if opening is None:
        return {"status": "unavailable", "rev": None, "generation": None}
    if stored["status"] == "absent":
        return {"status": "absent", "rev": 0, "generation": None,
                "opening_generation": opening}
    if stored["status"] != "current":
        return {"status": "unavailable", "rev": None, "generation": None}
    record = stored["record"]
    if record["generation"] == opening:
        return {"status": "current", "rev": record["rev"], "generation": record["generation"],
                "items": list(record["items"]), "rationale": record["rationale"],
                "actor": record["actor"], "updated_at": record["updated_at"],
                "opening_generation": opening}
    return {"status": "review_due", "rev": record["rev"], "generation": record["generation"],
            "items": list(record["items"]), "rationale": record["rationale"],
            "actor": record["actor"], "updated_at": record["updated_at"],
            "opening_generation": opening}


def status_for_project(project, *, role_name: str | None = None) -> dict:
    """Operator-readable freshness; never writes or refreshes the plan."""
    try:
        stored = _read_stored(project.root)
    except Exception:
        return {"status": "unavailable", "rev": None, "generation": None}
    if stored["status"] == "absent":
        return {"status": "absent", "rev": 0, "generation": None}
    if stored["status"] == "unavailable":
        return {"status": "unavailable", "rev": None, "generation": None}
    record = stored["record"]
    owner = record
    candidate = None
    try:
        roles = getattr(project, "roles", [])
        config_roles = getattr(getattr(project, "config", None), "roles", {})
        if isinstance(owner.get("actor"), str) and owner["actor"] in roles \
                and "work_order" in tuple(getattr(config_roles.get(owner["actor"]), "capabilities", ())):
            candidate = owner["actor"]
        else:
            for name in roles:
                role = config_roles.get(name)
                if role is not None and "work_order" in tuple(getattr(role, "capabilities", ())):
                    candidate = name
                    break
    except Exception:
        candidate = None
    if candidate is None:
        return {"status": "unavailable", "rev": None, "generation": None}
    try:
        role = project.config.roles[candidate]
        current = _fresh_generation_for_role(project, role)
    except Exception:
        return {"status": "unavailable", "rev": None, "generation": None}
    if record["generation"] == current:
        return {"status": "current", "rev": record["rev"], "generation": record["generation"],
                "items": list(record["items"]), "rationale": record["rationale"],
                "actor": record["actor"], "updated_at": record["updated_at"]}
    return {"status": "review_due", "rev": record["rev"], "generation": record["generation"],
            "items": list(record["items"]), "rationale": record["rationale"],
            "actor": record["actor"], "updated_at": record["updated_at"],
            "current_generation": current}


def _is_actionable_locked(project, role_name: str, item_id: str, published_snapshot) -> bool:
    """Locked ready check mirroring page_ready holder/wait policy."""
    store = project.work_items
    revisions = store._recover_temporary_writes(item_id)
    if revisions is None:
        raise Denied("Unknown work-order item")
    item = store._get(item_id, history_revisions=revisions)
    status = item.get("status")
    if status not in ("open", "doing", "blocked"):
        return False
    holder = item.get("holder")
    if status == "open":
        if holder is not None:
            return False
    elif status == "doing":
        if holder != role_name:
            return False
    else:  # blocked
        if holder is not None and holder != role_name:
            return False
        wait = item.get("wait")
        if not wait or not isinstance(wait, dict) or wait.get("kind") == "ci":
            return False
        try:
            if not project.insights._wait_due(wait, published_snapshot):
                return False
        except Exception as exc:
            raise Denied("Work-order wait is unavailable") from exc
    if item.get("wait") is not None and status != "blocked":
        return False
    return True


def write_plan(project, *, actor: str, run: str, items, rationale: str,
               expected_rev: int, expected_generation: str,
               opening_generation: str | None) -> dict:
    """Replace one complete current record under the revision/generation CAS.

    ``actor`` and ``run`` are runtime-supplied, never caller-supplied.
    ``expected_rev`` is the stored rev (0 when absent); the expected generation
    must equal the frozen unit-opening generation. No recovery or retry occurs.
    Return the complete committed record for the authoring run's audit receipt.
    """
    identifier(actor)
    _validate_run(run)
    items = _validate_items(items)
    rationale = _validate_rationale(rationale)
    _validate_rev(expected_rev)
    _validate_generation(expected_generation)
    if opening_generation is None:
        raise Denied("Stale work-order review; re-read before recording")
    _validate_generation(opening_generation)
    from .fs import lock as _lock
    with _lock(project.root / "locks" / "insights.lock"):
        stored = _read_stored(project.root)
        if stored["status"] == "unavailable":
            raise Denied("Work order is unavailable")
        current_rev = stored["rev"]
        if expected_rev != current_rev:
            raise Denied("Stale work order; re-read before recording")
        if expected_generation != opening_generation:
            raise Denied("Stale work-order review; re-read before recording")
        # Actionability is judged against the live ledger under the same lock.
        try:
            published = project.snapshots.get()
        except Exception as exc:
            raise Denied("Work-order snapshot is unavailable") from exc
        for item_id in items:
            try:
                if not _is_actionable_locked(project, actor, item_id, published):
                    raise Denied(f"Work-order item is not actionable: {item_id}")
            except Denied:
                raise
            except Exception as exc:
                raise Denied("Work-order item is unavailable") from exc
        new_rev = current_rev + 1
        record = {"schema": _SCHEMA, "rev": new_rev, "generation": expected_generation,
                  "items": list(items), "rationale": rationale, "actor": actor,
                  "updated_at": now(), "run": run}
        if len(canonical(record)) > MAX_WORK_ORDER_BYTES:
            raise LimitExceeded("Work order exceeds byte bound")
        root = _root(project.root)
        try:
            if not _directory(root):
                root.mkdir(parents=True, exist_ok=True)
            write_json(_current_path(project.root), record)
        except OSError as exc:
            raise Denied("Work order is unavailable") from exc
        return record
