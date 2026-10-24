"""Role health and capture-cause bookkeeping for completed run outcomes."""
from __future__ import annotations

import contextlib

from .errors import Cancelled, CaptureLimitExceeded, MissingFinish
from .fs import now, read_json, write_json
from .snapshot import MAX_MISSING_FINISH_STREAK, missing_finish_streak


def _capture_cause(exc, cause=None) -> str | None:
    """The capture-limit cause this unit observed, if any.

    A capture bound names its cause in the error itself, so the authoritative
    publication capture reaches health without a separate hand-off. A cause
    recorded by an earlier observation of this unit wins.
    """
    if isinstance(cause, str) and cause:
        return cause
    if isinstance(exc, CaptureLimitExceeded):
        return str(exc)[:512]
    return None


def _record_health_failure(project, role_name, run_id, exc, maximum, cause=None,
                           resolved=False):
    """Advance the existing role failure brake once for a terminal failure.

    The consecutive-failure brake semantics are unchanged. A terminal
    missing-finish increments the dedicated consecutive-missing-finish
    streak; any other counted failure resets it to zero. The streak lives
    in the same health record (``consecutive_missing_finish``) and is
    bounded by ``MAX_MISSING_FINISH_STREAK``.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        health = read_json(project.root / "health" / f"{role_name}.json", {})
        failures = health.get("consecutive_failures", 0) + 1
        if isinstance(exc, MissingFinish):
            streak = missing_finish_streak(health) + 1
            streak = min(streak, MAX_MISSING_FINISH_STREAK)
        else:
            streak = 0
        observed = _capture_cause(exc, cause)
        previous = health.get("capture_limit") if isinstance(health, dict) else None
        # The cause that still applies: this unit's, or the recorded one while
        # nothing proved the workspace representable again.
        if observed:
            recorded = observed
        elif resolved:
            recorded = None
        else:
            recorded = previous if isinstance(previous, str) and previous else None
        write_json(project.root / "health" / f"{role_name}.json",
                   {"consecutive_failures": failures, "consecutive_missing_finish": streak,
                    "last_run": run_id, "error": str(exc), "updated_at": now(),
                    **({"capture_limit": recorded} if recorded else {})})
        if maximum > 0 and failures >= maximum and not isinstance(exc, Cancelled):
            reason = "Repeated failures; inspect health and run records"
            if recorded:
                reason = f"{reason}: {recorded}"
            project.set_control(paused=True, reason=reason)


def _record_missing_finish_only(project, role_name, run_id, exc, cause=None,
                                resolved=False):
    """Count a suppressed missing-finish toward the rotation streak only.

    A missing-finish with ``missing_finish_counts_as_failure`` disabled never
    touches the consecutive-failure brake or the pause control; it only
    advances the dedicated consecutive-missing-finish streak so a stuck
    session still rotates. Failure: never raises.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        health = read_json(project.root / "health" / f"{role_name}.json", {})
        if not isinstance(health, dict):
            health = {}
        streak = min(missing_finish_streak(health) + 1, MAX_MISSING_FINISH_STREAK)
        failures = health.get("consecutive_failures", 0)
        if type(failures) is not int or failures < 0:
            failures = 0
        observed = _capture_cause(exc, cause)
        previous = health.get("capture_limit") if isinstance(health, dict) else None
        if observed:
            recorded = observed
        elif resolved:
            recorded = None
        else:
            recorded = previous if isinstance(previous, str) and previous else None
        write_json(project.root / "health" / f"{role_name}.json",
                   {"consecutive_failures": failures, "consecutive_missing_finish": streak,
                    "last_run": run_id, "error": str(exc), "updated_at": now(),
                    **({"capture_limit": recorded} if recorded else {})})


def _clear_missing_finish_streak(project, role_name):
    """Reset the missing-finish streak after a non-missing-finish unit.

    Best-effort and byte-preserving when there is nothing to reset: a
    health record without the streak field (or already zero) is left
    untouched so bounded/deferred paths stay exactly as before.
    Failure: never raises.
    """
    with contextlib.suppress(OSError, ValueError, TypeError):
        path = project.root / "health" / f"{role_name}.json"
        health = read_json(path, {})
        if not isinstance(health, dict):
            return
        if health.get("consecutive_missing_finish", 0) == 0 and "consecutive_missing_finish" not in health:
            return
        if missing_finish_streak(health) == 0 and health.get("consecutive_missing_finish") == 0:
            return
        health = {**health, "consecutive_missing_finish": 0, "updated_at": now()}
        write_json(path, health)


