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
        if not isinstance(health, dict):
            health = {}
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
            pause_cause = {"kind": "failure_brake", "error_class": type(exc).__name__,
                           "roles": [role_name]}
            project.set_control(paused=True, reason=reason, pause_cause=pause_cause)


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


# Status reads at most 512 run entries, 64 KiB per record and 8 MiB total.
MAX_RATE_RUNS = 512
MAX_RATE_FILE_BYTES = 64 * 1024
MAX_RATE_BYTES = 8 * 1024 * 1024


def finish_only_rates(project, *, as_of=None) -> dict:
    """Prior-24h rates from retained normal-unit results, without text inspection.

    Counts are observed lower bounds when status is unknown/incomplete; only
    complete coverage with a nonempty denominator yields a numeric rate.
    """
    import datetime as dt
    import json
    import os
    import stat
    from pathlib import Path

    from .errors import Denied, LimitExceeded
    from .fs import ID, loads_finite, safe_read

    end = as_of if as_of is not None else dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    end = end.astimezone(dt.timezone.utc)
    start = end - dt.timedelta(hours=24)
    roles = {role: {"finish_only_units": 0, "completed_units": 0, "reasons": {}}
             for role in project.config.roles}
    scanned = used_bytes = 0

    def issue(code, role=None):
        for row in ([roles[role]] if role in roles else roles.values()):
            row["reasons"][code] = row["reasons"].get(code, 0) + 1

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def real_dir(path):
        try:
            info = path.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        try:
            if path.is_symlink():
                return False
        except OSError:
            return False
        if getattr(info, "st_file_attributes", 0) & 0x400:
            return False
        if getattr(info, "st_reparse_tag", 0):
            return False
        return True

    def read(name):
        nonlocal used_bytes
        allowance = min(MAX_RATE_FILE_BYTES, MAX_RATE_BYTES - used_bytes)
        if allowance <= 0:
            raise LimitExceeded("scan byte bound")
        # Reserve before IO; refused/unstable reads consume their allowance.
        used_bytes += allowance
        raw = safe_read(project.root, name, allowance)
        used_bytes -= allowance - len(raw)
        loads_finite(raw)
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value

    def activity_valid(activity):
        if not isinstance(activity, dict) or type(activity.get("schema")) is not int or activity["schema"] != 1:
            return False
        if any(activity.get(key) is not False for key in
               ("truncated", "counts_truncated", "incomplete") if key in activity):
            return False
        if type(activity.get("open_model_requests")) is not int or activity["open_model_requests"] != 0:
            return False
        counts = activity.get("counts")
        return (isinstance(counts, dict) and 0 < len(counts) <= 128
                and all(isinstance(key, str) and 0 < len(key) <= 128 and key.isprintable()
                        and type(value) is int and 0 <= value <= 2**63 - 1
                        for key, value in counts.items())
                and counts.get("tool_call:finish", 0) > 0)

    try:
        runs = project.root / "runs"
        if not real_dir(runs):
            raise OSError("unsafe runs directory")
        with os.scandir(runs) as entries:
            for entry in entries:
                if scanned >= MAX_RATE_RUNS or used_bytes >= MAX_RATE_BYTES:
                    issue("scan_limit")
                    break
                scanned += 1
                if not real_dir(Path(entry.path)):
                    issue("unsafe_run_entry")
                    continue
                name = entry.name
                if not ID.fullmatch(name):
                    issue("invalid_identity")
                    continue
                try:
                    record = read(f"runs/{name}/result.json")
                except (OSError, ValueError, Denied, LimitExceeded, RecursionError) as exc:
                    # Child records live in consultation/delegation.json, not
                    # result.json. Only a genuinely absent result may be
                    # excluded when the stored started identity is internally
                    # consistent with a child; malformed/unsafe results remain
                    # incomplete. Local records are not authenticated.
                    missing_result = (isinstance(exc, FileNotFoundError)
                                      or isinstance(exc, Denied)
                                      and isinstance(exc.__cause__, FileNotFoundError))
                    try:
                        started = read(f"runs/{name}/started.json")
                    except (OSError, ValueError, Denied, LimitExceeded, RecursionError):
                        issue("unavailable_record")
                        continue
                    if started.get("run") != name:
                        issue("invalid_identity")
                        continue
                    child_role = started.get("role")
                    if (not isinstance(child_role, str) or not ID.fullmatch(child_role)
                            or child_role not in roles):
                        issue("invalid_identity")
                        continue
                    parent = started.get("parent_run")
                    if parent is None:
                        issue("unavailable_result", child_role)
                        continue
                    if (not isinstance(parent, str) or not ID.fullmatch(parent)
                            or parent == name):
                        issue("invalid_identity", child_role)
                        continue
                    if not real_dir(runs / parent):
                        issue("invalid_identity", child_role)
                        continue
                    if missing_result:
                        continue
                    issue("unavailable_result", child_role)
                    continue
                role = record.get("role")
                if not isinstance(role, str) or not ID.fullmatch(role):
                    issue("invalid_identity")
                    continue
                if role not in roles:
                    continue
                if record.get("parent_run") is not None:
                    issue("invalid_identity", role)
                    continue
                if record.get("run") != name:
                    # A mismatched run id cannot be attributed to this role
                    # from the untrusted record alone.
                    issue("invalid_identity")
                    continue
                status = record.get("status")
                if status in ("prepared", "bounded", "interrupted"):
                    continue
                if status not in ("completed", "waiting"):
                    issue("invalid_status", role)
                    continue
                try:
                    started = read(f"runs/{name}/started.json")
                except (OSError, ValueError, Denied, LimitExceeded, RecursionError):
                    issue("unavailable_record", role)
                    continue
                if started.get("run") != name or started.get("role") != role:
                    # Conflicting role/run identities are unattributable.
                    issue("invalid_identity")
                    continue
                if started.get("parent_run") is not None:
                    issue("invalid_identity", role)
                    continue
                try:
                    stamp = record["finished_at"]
                    if not isinstance(stamp, str) or len(stamp) > 64:
                        raise ValueError("invalid time")
                    finished = dt.datetime.fromisoformat(stamp)
                    if finished.tzinfo is None or finished.utcoffset() is None:
                        raise ValueError("naive time")
                except (KeyError, ValueError, TypeError):
                    issue("invalid_finished_at", role)
                    continue
                if not start <= finished < end:
                    continue
                row = roles[role]
                row["completed_units"] += 1
                if (any(record.get(key) is not False for key in ("truncated", "incomplete") if key in record)
                        or not activity_valid(record.get("activity"))):
                    issue("incomplete_activity", role)
                    continue
                counts = record["activity"]["counts"]
                if not any(key.startswith("tool_call:") and key != "tool_call:finish" and value > 0
                           for key, value in counts.items()):
                    row["finish_only_units"] += 1
    except OSError:
        issue("unavailable_runs")

    for row in roles.values():
        denominator = row["completed_units"]
        row["status"] = (("incomplete" if denominator else "unknown") if row["reasons"] else
                         "known" if denominator else "no_completed_units")
        row["rate"] = row["finish_only_units"] / denominator if row["status"] == "known" else None
    return {"window": {"start": start.isoformat(), "end": end.isoformat(),
                       "seconds": 86400, "bounds": "[start,end)"},
            "target": 0, "scanned_runs": scanned, "roles": roles}
