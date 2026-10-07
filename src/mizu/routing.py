"""Worker-directed next-unit routing without an extra inference call.

A finishing unit may recommend the profile the next unit should use. The
recommendation is recorded under the run lock with a task/input binding
(goal digest plus published snapshot id); the next unit's selection
exposes it as predicate facts only while the binding still matches.
Operator selector rules own the recommendation-to-profile mapping, so the
product never names profiles or difficulty criteria. Stale, absent, and
corrupt records fall back to ordinary selection and are reported in the
decision record, never fatal. Recommendations stay valid while the task
is unchanged; a unit countermands by recommending again, including back
to the default profile.
"""
from __future__ import annotations

from .errors import Denied
from .fs import DIGEST, ID, canonical, mkdir, now, read_json, write_json

MAX_PROFILE_CHARS = 63
MAX_REASON_CHARS = 256
MAX_RECORD_BYTES = 1024

STATUSES = ("fresh", "stale", "absent", "invalid")


def check_recommendation(profile, reason):
    """Validate finish-time routing fields; ``Denied`` on malformed input."""
    if profile is not None and not (isinstance(profile, str) and ID.fullmatch(profile)):
        raise Denied("next_profile must be a model-profile identifier")
    if isinstance(reason, str):
        reason = reason.strip() or None
    if reason is not None and profile is None:
        raise Denied("next_reason requires next_profile")
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS):
        raise Denied("next_reason must be bounded text")
    return profile, reason


def record(project, role_name, finished, snapshot, goal_digest, run_id):
    """Persist a finish-time recommendation under the caller's run lock.

    Binds to the published snapshot id and goal digest the unit finished
    against. No ``next_profile`` means no record and no error.
    """
    profile, reason = check_recommendation(finished.get("next_profile"), finished.get("next_reason"))
    if profile is None:
        return None
    if not isinstance(snapshot, dict) or not DIGEST.fullmatch(snapshot.get("id") or ""):
        raise Denied("Routing binds to a published snapshot")
    if not isinstance(goal_digest, str) or not DIGEST.fullmatch(goal_digest):
        raise Denied("Routing binds to a goal digest")
    entry = {"version": 1, "profile": profile, "goal_digest": goal_digest,
             "snapshot": snapshot["id"], "run": run_id, "created_at": now()}
    if reason:
        entry["reason"] = reason
    if len(canonical(entry)) > MAX_RECORD_BYTES:
        raise Denied("Routing recommendation exceeds its bound")
    mkdir(project.root / "routing")
    write_json(project.root / "routing" / f"{role_name}.json", entry)
    return entry


def current(project, role_name, goal_digest, snapshot_id):
    """Read this role's recommendation: freshness-checked, never fatal.

    Returns ``{"status": one of fresh/stale/absent/invalid,
    "recommendation": {"profile", "reason"?, "run", "created_at"} | None}``.
    Only ``fresh`` entries are safe to expose as selection facts.
    """
    path = project.root / "routing" / f"{role_name}.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("invalid routing file")
        entry = read_json(path)
    except (OSError, ValueError):
        if not path.exists():
            return {"status": "absent", "recommendation": None}
        return {"status": "invalid", "recommendation": None}
    if (not isinstance(entry, dict) or entry.get("version") != 1
            or not isinstance(entry.get("profile"), str) or not ID.fullmatch(entry["profile"])
            or not isinstance(entry.get("goal_digest"), str)
            or not DIGEST.fullmatch(entry["goal_digest"])
            or not isinstance(entry.get("snapshot"), str)
            or not DIGEST.fullmatch(entry["snapshot"])
            or not isinstance(entry.get("run"), str) or not entry["run"]
            or not isinstance(entry.get("created_at"), str) or not entry["created_at"]
            or ("reason" in entry and (not isinstance(entry["reason"], str)
                                       or not entry["reason"].strip()
                                       or len(entry["reason"]) > MAX_REASON_CHARS))
            or len(canonical(entry)) > MAX_RECORD_BYTES):
        return {"status": "invalid", "recommendation": None}
    if entry["goal_digest"] != goal_digest or entry["snapshot"] != snapshot_id:
        return {"status": "stale", "recommendation": None}
    recommendation = {"profile": entry["profile"], "run": entry["run"],
                      "created_at": entry["created_at"]}
    if entry.get("reason"):
        recommendation["reason"] = entry["reason"]
    return {"status": "fresh", "recommendation": recommendation}
