"""Read-only, bounded projection of recorded project/role evidence.

This is deliberately not a scheduler preview: it reads current pointers and
bounded state files only. It never asks what the engine would select or run.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import stat
import time
from pathlib import Path

from .budget import Budget
from .errors import ConfigError, Denied, LimitExceeded
from .fs import now, safe_read

# These records are small control-plane facts, never histories or logs.
RECORD_BYTES = 65536


def _read_record(root: Path, relative: str, maximum: int = RECORD_BYTES) -> tuple[str, dict | None]:
    """Read one named JSON record without following links or unbounded reads."""
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unavailable", None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        return "unavailable", None
    try:
        value = json.loads(safe_read(root, relative, maximum),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (Denied, LimitExceeded, OSError, ValueError, TypeError, RecursionError):
        return "unavailable", None
    if not isinstance(value, dict):
        return "unavailable", None
    return "current", value


def _timestamp(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _run_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _control(project) -> dict:
    status, record = _read_record(project.root, "control.json")
    if status != "current":
        return {"status": status, "record": None}
    selected = {key: record.get(key) for key in
                ("armed", "paused", "draining", "wake_generation", "updated_at", "reason")
                if key in record}
    if any(key not in selected or type(selected[key]) is not bool
           for key in ("armed", "paused", "draining")):
        return {"status": "unavailable", "record": None}
    wake = selected.get("wake_generation")
    if wake is not None and (not isinstance(wake, str) or len(wake) > 128 or "\n" in wake):
        return {"status": "unavailable", "record": None}
    if "updated_at" in selected and not _timestamp(selected["updated_at"]):
        return {"status": "unavailable", "record": None}
    if "reason" in selected and (not isinstance(selected["reason"], str)
                                  or len(selected["reason"]) > 512
                                  or "\x00" in selected["reason"]):
        return {"status": "unavailable", "record": None}
    # Ignore unknown keys and bound the operator-authored control reason.
    return {"status": "current", "record": selected}


def _role_record(project, role: str, directory: str, fields: tuple[str, ...]) -> dict:
    status, record = _read_record(project.root, f"{directory}/{role}.json")
    if status != "current":
        return {"status": status, "record": None}
    selected = {key: record[key] for key in fields if key in record}
    if directory == "health":
        if ("consecutive_failures" not in selected or
                type(selected["consecutive_failures"]) is not int or selected["consecutive_failures"] < 0):
            return {"status": "unavailable", "record": None}
        if "last_run" in selected and selected["last_run"] is not None and not _run_id(selected["last_run"]):
            return {"status": "unavailable", "record": None}
        if "updated_at" not in selected or not _timestamp(selected["updated_at"]):
            return {"status": "unavailable", "record": None}
    elif (selected.get("role") != role or not _run_id(selected.get("run"))
          or not _timestamp(selected.get("started_at"))):
        return {"status": "unavailable", "record": None}
    return {"status": "current", "record": selected}


def _selection(config, role) -> dict:
    configured = {"profile": role.profile}
    if not role.selector:
        return {"status": "not_configured", "selector": None,
                "configured_profile": configured,
                "fixed_blocks": [], "probe_blocks": []}
    try:
        from .selection import MAX_BYTES, State
        selection_path = config.data / "selection" / "state.json"
        try:
            info = selection_path.lstat()
        except FileNotFoundError:
            return {"status": "absent", "selector": role.selector,
                    "configured_profile": configured, "current_choice": "unknown",
                    "fixed_blocks": None, "probe_blocks": None}
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MAX_BYTES):
            return {"status": "unavailable", "selector": role.selector,
                    "configured_profile": configured, "current_choice": "unknown",
                    "fixed_blocks": None, "probe_blocks": None}
        state = State(config).read()
        spec = config.selectors[role.selector]
        relevant = set()
        for rule in spec["rules"]:
            for candidate in rule["candidates"]:
                relevant.add("profile:" + candidate["profile"])
                relevant.add("group:" + candidate.get("group", candidate["profile"]))
        at = time.time()
        fixed = []
        for key, block in state["blocks"].items():
            if key in relevant and block["until"] > at:
                fixed.append({"key": key, **{field: block[field] for field in
                              ("created_at", "until", "run", "selector", "rule",
                               "error_rule", "sponsor", "role") if field in block}})
        probes = []
        for identity, contribution in sorted(state["contributions"].items()):
            if (contribution["key"] not in relevant or contribution["status"] != "active"
                    or contribution["until"] is not None and contribution["until"] <= at):
                continue
            probe = {"id": identity, "key": contribution["key"],
                     "profile": contribution["profile"],
                     "sponsor": contribution["sponsor"],
                     "sponsor_role": contribution["role"],
                     "selector": contribution["selector"],
                     "rule": contribution["rule"],
                     "error_rule": contribution["error_rule"],
                     "status": contribution["status"],
                     "until": contribution["until"],
                     "probe_seconds": contribution["probe_seconds"],
                     "next_probe_at": contribution["next_probe_at"],
                     "probe_due": contribution["next_probe_at"] <= at,
                     "claim_pending": contribution["claim"] is not None,
                     "reason": contribution["reason"],
                     "sponsor_authorization": "not_checked"}
            event_id = contribution["claim"] or contribution["last_event"]
            event = state["events"].get(event_id) if event_id else None
            if event is not None:
                probe["last_event_status"] = event["status"]
            probes.append(probe)
        fixed.sort(key=lambda item: item["key"])
        return {"status": "current", "selector": role.selector,
                "configured_profile": configured, "current_choice": "unknown",
                "exhausted": state["exhausted"], "fixed_blocks": fixed,
                "probe_blocks": probes}
    except (ConfigError, Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
        # Malformed/unreadable selection state is unavailable, not an empty ledger.
        return {"status": "unavailable", "selector": role.selector,
                "configured_profile": configured, "current_choice": "unknown",
                "fixed_blocks": None, "probe_blocks": None}


def _snapshot(project) -> dict:
    try:
        snapshot = project.snapshots.get()
    except (Denied, OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable", "scope": "project", "record": None}
    return {"status": "current", "scope": "project", "record": {
        "id": snapshot["id"], "code_digest": snapshot["code_digest"],
        "created_at": snapshot["created_at"], "outcome": snapshot["outcome"],
        "wake_at": snapshot.get("wake_at"),
    }}


def _budget(config, project_name) -> dict:
    budget = Budget(config.data / "budget", config.limits.daily_requests,
                    config.limits.retention_days,
                    config.limits.shared_daily_requests, config.timezone)
    peek = budget.peek(project_name)
    if peek["status"] == "unavailable":
        return {"status": "unavailable", "scope": "project", "counters": None}
    return {"status": peek["status"], "scope": "project", "counters": {
        key: peek[key] for key in ("day", "zone", "used", "limit", "shared_used",
                                   "shared_limit", "bytes")
    }}


def explain(config, project, role_name: str) -> dict:
    """Return a timestamped evidence projection for one configured project role."""
    if role_name not in project.roles or role_name not in config.roles:
        raise ConfigError("Role is not enabled for this project")
    role = config.roles[role_name]
    health = _role_record(project, role_name, "health",
                          ("consecutive_failures", "last_run", "updated_at"))
    active = _role_record(project, role_name, "active", ("run", "role", "started_at"))
    triggers = {"configured_profile": role.profile, "selector": role.selector,
                "daemon": role.daemon, "interval_seconds": role.interval_seconds,
                "calendar": list(role.calendar), "on_change": role.on_change,
                "decision_events": list(role.decision_events),
                "decision_sources": role.decision_sources, "ci_wake": role.ci_wake}
    return {
        "schema_version": 1,
        "generated_at": now(),
        "observation": "Non-atomic read of recorded state; not a scheduler verdict.",
        "project": project.name,
        "role": role_name,
        "project_facts": {
            "control": _control(project),
            "snapshot": _snapshot(project),
            "budget": _budget(config, project.name),
        },
        "role_facts": {
            "health": health,
            "active": {**active, "process_liveness": "unknown", "yieldability": "unknown"},
            "selection_constraints": _selection(config, role),
            "triggers": triggers,
            "unknowns": {
                "exact_next_invocation": "unknown",
                "external_service_manager_state": "unknown",
                "last_completed_outcome": "unknown",
                "admission_queue_position": "unknown",
                "active_process_liveness": "unknown",
                "active_process_yieldability": "unknown",
                "pending_insight_state": "unknown",
                "structured_wait_state": "unknown",
            },
        },
    }
