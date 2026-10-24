"""Bounded CI branch polling and per-branch success-cache merging.

This leaf owns periodic CI status polling into the dedicated CI
notification cache plus its bounded cache merge/prune helpers. Poll coordination
(``poll_project_vcs``), shared poll-state persistence and daemon orchestration
stay in runtime; CI cache projection stays in ``ci_observation`` and per-role
wake records/due policy stay in ``ci_wake``. This module never imports runtime.
"""
from __future__ import annotations

from . import vcs as _vcs
from .ci_observation import (MAX_CI_CHECKS_PER_BRANCH,
                             MAX_CI_FAILURE_BRANCHES, _clean_ci_failures,
                             _clean_ci_successes, _valid_ci_run)
from .ci_wake import effective_ci_wake_branches
from .errors import Denied
from .fs import now as now_iso
from .vcs_poll_policy import (capped_vcs_settings, upstream_fetch_due,
                              vcs_poll_max_branches, vcs_poll_status_timeout)


def _merge_ci_success_rows(existing, incoming) -> list:
    """Keep the newest same-branch evidence first, unique by sha/check."""
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
            if len(out) >= MAX_CI_CHECKS_PER_BRANCH:
                return out
    return out


def _merge_ci_success_maps(existing, incoming) -> dict:
    """Merge bounded success evidence by branch, favoring newer rows."""
    merged = _clean_ci_successes(existing)
    for branch, rows in _clean_ci_successes(incoming).items():
        combined = _merge_ci_success_rows(merged.get(branch), rows)
        if combined:
            merged[branch] = combined
        else:
            merged.pop(branch, None)
    for branch in [name for name, rows in merged.items() if not rows]:
        merged.pop(branch, None)
    while len(merged) > MAX_CI_FAILURE_BRANCHES:
        merged.pop(sorted(merged)[0], None)
    return merged


def _prune_ci_branch_map(mapping: dict, protected_branches) -> bool:
    """Trim to the fixed branch cap without dropping configured wake branches."""
    protected = set(protected_branches)
    pruned = False
    while len(mapping) > MAX_CI_FAILURE_BRANCHES:
        removable = sorted(branch for branch in mapping if branch not in protected)
        if not removable:
            break
        mapping.pop(removable[0], None)
        pruned = True
    return pruned




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
            and len(state["failures"]) > MAX_CI_FAILURE_BRANCHES)
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
            if len(rows) > MAX_CI_CHECKS_PER_BRANCH:
                check_rows_dropped = True
            tick[branch] = rows[:MAX_CI_CHECKS_PER_BRANCH]
            success_tick[branch] = passed_rows[:MAX_CI_CHECKS_PER_BRANCH]
        previous_failures = state.get("failures")
        failures_map = _clean_ci_failures(previous_failures)
        for branch, rows in tick.items():
            failures_map[branch] = rows
        if branches is None:
            try:
                live = set(sorted(_vcs.list_refs(project.root)))
            except OSError:
                live = None
            if live is not None:
                if len(live) <= max_branches:
                    live.add("main")
                stale_branches = [b for b in failures_map if b not in live]
                for stale in stale_branches:
                    failures_map.pop(stale, None)
        for empty in [b for b, rows in failures_map.items() if not rows]:
            failures_map.pop(empty, None)
        # Old rows can only hide current failures while an unrefreshed branch
        # remains in the cache after live-ref pruning.
        if isinstance(previous_failures, dict):
            check_rows_dropped = check_rows_dropped or any(
                branch not in tick
                and isinstance(previous_failures.get(branch), list)
                and len(previous_failures[branch]) > MAX_CI_CHECKS_PER_BRANCH
                for branch in failures_map)
        failure_branches_pruned = (
            _prune_ci_branch_map(failures_map, wake_branches)
            or failure_branches_pruned)
        state["failures"] = failures_map
        successes_map = _merge_ci_success_maps(state.get("successes"), {})
        for branch, rows in success_tick.items():
            combined = _merge_ci_success_rows(successes_map.get(branch), rows)
            if combined:
                successes_map[branch] = combined
            else:
                successes_map.pop(branch, None)
        if branches is None and live is not None:
            for stale in [name for name in successes_map if name not in live]:
                successes_map.pop(stale, None)
        _prune_ci_branch_map(successes_map, wake_branches)
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
