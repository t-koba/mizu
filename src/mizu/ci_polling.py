"""Bounded CI branch polling and per-branch success-cache merging.

This leaf owns periodic CI status polling into the dedicated CI
notification cache plus its bounded cache merge/prune helpers. Poll coordination
(``poll_project_vcs``), shared poll-state persistence and daemon orchestration
stay in runtime; CI cache projection stays in ``ci_observation`` and per-role
wake records/due policy stay in ``ci_wake``. This module never imports runtime.
"""
from __future__ import annotations

from . import vcs as _vcs
from .ci_observation import (ci_cache_limits, _clean_ci_failures,
                             _clean_ci_successes, _valid_ci_run)
from .config import MAX_CI_CACHE_ROWS
from .ci_wake import effective_ci_wake_branches
from .errors import Denied
from .fs import now as now_iso
from .vcs_poll_policy import (capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_max_branches, vcs_poll_status_timeout)


def _merge_ci_success_rows(existing, incoming, config=None) -> list:
    """Keep the newest same-branch evidence first, unique by sha/check."""
    _, max_checks = ci_cache_limits(config)
    out = []
    seen = set()
    for rows in (incoming, existing):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = (row.get("sha"), row.get("check"))
            if identity in seen:
                continue
            seen.add(identity)
            out.append({"sha": identity[0], "check": identity[1]})
            if len(out) >= max_checks:
                return out
    return out


def _merge_ci_success_maps(existing, incoming, config=None, *, live_branches=None,
                             defer_branch_cap=False) -> dict:
    """Merge bounded success evidence by branch, favoring newer rows."""
    merged = _clean_ci_successes(
        existing, config, live_branches=live_branches, defer_branch_cap=defer_branch_cap)
    for branch, rows in _clean_ci_successes(
            incoming, config, live_branches=live_branches,
            defer_branch_cap=defer_branch_cap).items():
        combined = _merge_ci_success_rows(merged.get(branch), rows, config)
        if combined:
            merged[branch] = combined
        else:
            merged.pop(branch, None)
    for branch in [name for name, rows in merged.items() if not rows]:
        merged.pop(branch, None)
    _prune_ci_branch_map(merged, effective_ci_wake_branches(config), config,
                         defer_branch_cap=defer_branch_cap)
    return merged


def _prune_ci_branch_map(mapping: dict, protected_branches, config=None, *,
                           defer_branch_cap=False) -> bool:
    """Trim to the effective branch cap without dropping configured wake branches."""
    max_branches, _ = ci_cache_limits(config)
    if defer_branch_cap:
        # The configured branch cap waits for a complete live-ref inventory;
        # the fixed cache envelope still bounds both branches and actual rows.
        max_branches = MAX_CI_CACHE_ROWS
    protected = set(protected_branches)
    pruned = False
    while len(mapping) > max_branches:
        removable = sorted(branch for branch in mapping if branch not in protected)
        if not removable:
            break
        mapping.pop(removable[0], None)
        pruned = True
    if defer_branch_cap:
        remaining_rows = MAX_CI_CACHE_ROWS
        branches = list(mapping)
        for index, branch in enumerate(branches):
            rows = mapping.get(branch)
            if not isinstance(rows, list):
                mapping.pop(branch, None)
                pruned = True
                continue
            if len(rows) > remaining_rows:
                mapping[branch] = rows[:remaining_rows]
                remaining_rows = 0
                pruned = True
            else:
                remaining_rows -= len(rows)
            if remaining_rows == 0:
                # No further nonempty row can fit; drop later branches instead
                # of retaining an over-envelope success cache.
                for candidate in branches[index + 1:]:
                    mapping.pop(candidate, None)
                    pruned = True
                break
    return pruned




def _live_ci_branches(config, project):
    """Complete local ref inventory with the CI main fallback; read errors propagate."""
    live = set(_vcs.list_refs(project.root))
    # Preserve the fresh-workspace/main fallback used by CI target selection.
    if len(live) <= vcs_poll_max_branches(config):
        live.add("main")
    return live


def poll_ci(config, project, state: dict, *, now: float, interval: float,
            branches=None, reader=None) -> dict | None:
    """Best-effort periodic CI polling into the dedicated CI notification cache.

    Schema: ``branches=None`` derives from injected upstream refs
    (``vcs.list_refs``): all of them when they fit ``poll_max_branches``,
    else a rotating window tracked in ``state["ci_offset"]`` so every branch
    is covered within ceil(refs/max) ticks instead of pinning the sorted
    tail; explicit lists are validated as branch names. Returns a ``ci_poll``
    event dict or ``None`` when skipped (disabled or not due). Bounds: one
    adapter call per branch per due poll under the adapter timeout; status
    checks capped at 1024 rows each. Trust: host side only; adapter facts
    stay external-untrusted. Bounded failure and success facts are cached for
    dedicated CI wake selection and prompts; neither creates insights.
    Receipt-pinned CI wait delivery is independent of this branch-head cache.
    Retry/cancellation: no retry; ``last_poll`` and the rotation cursor
    advance even on failure so one bad adapter cannot busy-loop or pin the
    window. Evidence: the event names ok/branches/counts/total or
    error; this helper never publishes a snapshot. Failure: ``Denied``,
    ``OSError`` and ``ValueError`` become ``ok=False`` events, never raised.
    """
    if not config.vcs.get("command"):
        return None
    if not upstream_fetch_due(state.get("last_poll"), now, interval):
        return None
    state["last_poll"] = now
    max_branches = vcs_poll_max_branches(config)
    wake_branches = set(effective_ci_wake_branches(config))
    cache_branches, max_checks = ci_cache_limits(config)
    def _default_reader(settings, op, params):
        return _vcs.read_via(capped_vcs_settings(settings, vcs_poll_status_timeout(config)),
                             op, params)
    do_read = reader if reader is not None else _default_reader
    try:
        if branches is not None and len(list(branches)) > max_branches:
            raise Denied("Too many CI branches per poll")
        if branches is None:
            try:
                names = sorted(_vcs.list_refs(project.root))
            except OSError as exc:
                raise Denied(f"CI polling is unavailable: {exc}") from exc
            branches_total = len(names)
            if len(names) <= max_branches:
                # Fall back to main so a fresh workspace still polls once configured.
                targets = names or ["main"]
            else:
                offset = state.get("ci_offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                start = offset % len(names)
                targets = [names[(start + i) % len(names)] for i in range(max_branches)]
                state["ci_offset"] = (start + max_branches) % len(names)
        else:
            targets = [_vcs.check_branch(b) for b in branches]
            branches_total = len(targets)
        failures = 0
        check_rows_dropped = False
        success_tick: dict = {}
        failure_branches_pruned = (
            isinstance(state.get("failures"), dict)
            and len(state["failures"]) > cache_branches)
        tick: dict = {}
        for branch in targets:
            data = do_read(config.vcs, "status", {"branch": branch})
            checks = data.get("checks")
            if not isinstance(checks, list):
                raise Denied("VCS status must carry a checks array")
            rows = []
            passed_rows = []
            for entry in checks:
                if entry.get("state") == "success":
                    # The nondeterminism identity is exactly this SHA/check;
                    # run IDs add no evidence and are not retained for passes.
                    passed_rows.append({"sha": entry["sha"], "check": entry["check"]})
                    continue
                if entry.get("state") != "failure":
                    continue
                failures += 1
                tick_row = {"sha": entry["sha"], "check": entry["check"]}
                tick_run = _valid_ci_run(entry.get("run"))
                if tick_run is not None:
                    tick_row["run"] = tick_run
                rows.append(tick_row)
            if len(rows) > max_checks:
                check_rows_dropped = True
            tick[branch] = rows[:max_checks]
            success_tick[branch] = passed_rows[:max_checks]
        live = None
        if branches is None:
            try:
                live = _live_ci_branches(config, project)
            except OSError:
                pass
        previous_failures = state.get("failures")
        failures_map = _clean_ci_failures(previous_failures, config, live_branches=live)
        for branch, rows in tick.items():
            if live is None or branch in live:
                failures_map[branch] = rows
        for empty in [b for b, rows in failures_map.items() if not rows]:
            failures_map.pop(empty, None)
        # Old rows can only hide current failures while an unrefreshed branch
        # remains in the cache after live-ref pruning.
        if isinstance(previous_failures, dict):
            check_rows_dropped = check_rows_dropped or any(
                branch not in tick
                and isinstance(previous_failures.get(branch), list)
                and len(previous_failures[branch]) > max_checks
                for branch in failures_map)
        failure_branches_pruned = (
            _prune_ci_branch_map(failures_map, wake_branches, config)
            or failure_branches_pruned)
        state["failures"] = failures_map
        successes_map = _merge_ci_success_maps(
            state.get("successes"), {}, config, live_branches=live)
        for branch, rows in success_tick.items():
            combined = _merge_ci_success_rows(successes_map.get(branch), rows, config)
            if combined:
                successes_map[branch] = combined
            else:
                successes_map.pop(branch, None)
        if branches is None and live is not None:
            for stale in [name for name in successes_map if name not in live]:
                successes_map.pop(stale, None)
        _prune_ci_branch_map(successes_map, wake_branches, config)
        state["successes"] = successes_map
        state["last_error"] = ""
        state["last_ok"] = now
        return {"event": "ci_poll", "ok": True, "branches": list(targets),
                "branches_total": branches_total,
                "failures": failures,
                "ci_failures_may_be_truncated": bool(
                    check_rows_dropped or failure_branches_pruned),
                "trust": "external-untrusted", "time": now_iso()}
    except (Denied, OSError, ValueError) as exc:
        state["last_error"] = str(exc)
        return {"event": "ci_poll", "ok": False,
                "error": str(exc), "ci_failures_may_be_truncated": False,
                "time": now_iso()}
