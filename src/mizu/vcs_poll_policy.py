"""Pure policy helpers for bounded VCS polling.

This leaf owns poll settings defaults, bounds, cadence and due calculations;
it does not orchestrate polls or call adapters. Runtime re-exports these names
for compatibility with existing callers.
"""
from __future__ import annotations


def upstream_fetch_due(last_fetch: float | None, now: float, interval: float) -> bool:
    """Return True when a periodic upstream fetch is due (fake-clock testable).

    Schema: wall-clock seconds; ``last_fetch=None`` means never fetched.
    Bounds: ``interval <= 0`` always fetches (config floors keep it positive).
    Trust: pure time arithmetic, no I/O. Failure: never raises.
    """
    if last_fetch is None:
        return True
    try:
        last = float(last_fetch)
        current = float(now)
    except (TypeError, ValueError):
        return True
    if last > current:
        return True
    try:
        return (current - last) >= float(interval)
    except (TypeError, ValueError):
        return True


#: Fail-closed defaults for daemon polling bounds (operator policy in
#: `[vcs]` carries the same values; see ``vcs_poll_*`` below). The branch
#: cap keeps one tick short so a poll cannot block the loop for minutes
#: even when every adapter call uses its full per-call timeout. The
#: per-call caps bound daemon ticks via `min(timeout_seconds, cap)` so a
#: large on-demand timeout cannot stall the loop.
MAX_CI_BRANCHES = 4
POLL_CALL_TIMEOUT_CAP = 15
POLL_FETCH_TIMEOUT_CAP = 30


def _vcs_poll_number(settings: dict, key: str, default: int) -> int:
    """Configured `[vcs]` poll bound or its fail-closed default."""
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def vcs_poll_max_branches(config) -> int:
    """Daemon CI branches per tick from `vcs.poll_max_branches` (default 4)."""
    try:
        settings = config.vcs
    except AttributeError:
        return MAX_CI_BRANCHES
    return _vcs_poll_number(settings, "poll_max_branches", MAX_CI_BRANCHES)


def vcs_poll_fetch_timeout(config) -> int:
    """Daemon fetch cap from `vcs.poll_fetch_timeout_seconds` (default 30)."""
    try:
        settings = config.vcs
    except AttributeError:
        return POLL_FETCH_TIMEOUT_CAP
    return _vcs_poll_number(settings, "poll_fetch_timeout_seconds",
                            POLL_FETCH_TIMEOUT_CAP)


def vcs_poll_status_timeout(config) -> int:
    """Daemon per-branch status cap from `vcs.poll_status_timeout_seconds` (default 15)."""
    try:
        settings = config.vcs
    except AttributeError:
        return POLL_CALL_TIMEOUT_CAP
    return _vcs_poll_number(settings, "poll_status_timeout_seconds",
                            POLL_CALL_TIMEOUT_CAP)


def vcs_poll_proposals_enabled(config) -> bool:
    """True only when global VCS polling and proposal polling are opted in."""
    try:
        return (vcs_poll_enabled(config)
                and config.vcs.get("poll_proposals_enabled") is True)
    except (AttributeError, TypeError):
        return False


def vcs_poll_enabled(config) -> bool:
    """True only when daemon VCS polling is explicitly opted in.

    Schema: operator config. Trust: config, never model input.
    Failure: never raises; unconfigured/disabled reads as False.
    """
    try:
        return bool(config.vcs.get("poll_enabled")) and bool(config.vcs.get("command"))
    except (AttributeError, TypeError):
        return False


def vcs_poll_interval(config) -> float:
    """Daemon VCS poll cadence (seconds) from `vcs.poll_interval_seconds`."""
    try:
        value = float(config.vcs.get("poll_interval_seconds", 300))
    except (TypeError, ValueError):
        return 300.0
    return value if value > 0 else 300.0


def capped_vcs_settings(settings: dict, cap: int) -> dict:
    """Copy adapter settings with the timeout capped for daemon polling."""
    try:
        timeout = int(settings.get("timeout_seconds", 20))
    except (TypeError, ValueError):
        timeout = 20
    return {**settings, "timeout_seconds": min(timeout, cap)}
