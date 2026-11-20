"""Bounded ready-work observation and writer external-wait liveness.

This module owns the fail-closed ready-set probes and the source inventory that
tells a parked writer whether an external wait still has live evidence.
Runtime re-exports these helpers to preserve its established call seam.
"""
from __future__ import annotations

from . import ci_waits
from .ci_requests import CiRequests
from .config import wait_writer_roles
from .errors import Busy, Denied, LimitExceeded
from .fs import DIGEST
from .snapshot import validate_progress


def _has_ready_work(project, role=None) -> bool:
    """Ready-work existence from a filtered bounded ledger scan; errors close."""
    if role is not None and "work_items_transition" not in role.capabilities:
        return False
    try:
        return bool(project.work_items.page_ready(
            limit=1, **({"role_name": role.name} if role is not None else {}))["items"])
    except Exception:
        return False


def _ready_work_observation(project, record, role=None, *, work_items=None) -> tuple[bool, bool, bool]:
    """Return ready-set change, presence and a resolution-qualified change.

    Current store/read or malformed progress errors close all three facts. The
    full signature reports ready-set change; schema-4–6 writer progress separately
    qualifies resolution wakes by exact item/revision tokens actually shown in
    the opening pages. Legacy progress retains its conservative changed-set
    fallback because it has no delivery tokens. A sole-holder deadline stays
    ordinary open work in the holder's ready set, so its instant passing grants
    no wake. The progress role and fingerprint scope prevent an observer from
    consuming another role's ready-work signal.
    """
    if role is not None and "work_items_transition" not in role.capabilities:
        return False, False, False
    try:
        previous = record.get("progress")
        if previous is not None:
            validate_progress(previous)
        store = project.work_items if work_items is None else work_items
        delivered_tokens = None
        if (role is not None and isinstance(previous, dict)
                and "wait_resolution_tokens" in previous):
            delivered_tokens = (previous["wait_resolution_tokens"]
                                if previous["role"] == role.name else [])
        ready_args = {"limit": 1, **({"role_name": role.name} if role is not None else {})}
        if delivered_tokens is not None:
            ready_args["_delivered_wait_resolution_tokens"] = delivered_tokens
            if ("wait_resolution_high_water" in previous
                    and previous["role"] == role.name):
                ready_args["_wait_resolution_high_water"] = previous["wait_resolution_high_water"]
        page = store.page_ready(**ready_args)
        signature = page.get("signature")
        total = page.get("total")
        if (not isinstance(signature, str) or not DIGEST.fullmatch(signature)
                or type(total) is not int or total < 0):
            return False, False, False
        present = total > 0
        pending = page.get("_wait_resolution_pending", False)
        if type(pending) is not bool:
            return False, False, False
        if (previous is None or previous.get("ready_work_signature") is None
                or role is not None and previous["role"] != role.name):
            changed = present
        else:
            changed = signature != previous["ready_work_signature"]
        if delivered_tokens is not None:
            resolution_changed = page.get("_wait_resolution_undelivered")
            if type(resolution_changed) is not bool:
                return False, False, False
        else:
            # Older progress records do not identify which resolution proofs
            # their prompt actually delivered; retain their conservative wake.
            resolution_changed = changed and pending
        return changed, present, resolution_changed
    except Exception:
        return False, False, False


def _ready_work_changed(project, record, role=None, *, work_items=None) -> bool:
    """Compare full role-eligible membership to the input actually delivered."""
    return _ready_work_observation(project, record, role, work_items=work_items)[0]


def _writer_wait_liveness(project, role, *, releases=()) -> bool | None:
    """True for external liveness, False when none remains, None when unknown."""
    return _writer_wait_sources(project, role, releases=releases)


def _writer_wait_sources(project, role, *, releases=()) -> bool | None:
    """Return external liveness from checks and pending waits.

    Holder-only waits are ordinary ready work through the ready set, never an
    external source here: the blocked-item status projection already reclassifies
    them as open, so only genuinely external pending waits can report liveness.
    No adapter calls or inferred remote completion. Incomplete inventories can
    prove positive liveness, never absence.
    """
    try:
        unknown = False
        external = False
        if "check_job" in role.capabilities:
            from .acceptance import discover
            jobs, invalid = discover(project)
            if any(job["role"] == role.name and job["id"] not in releases
                   and job["status"] in ("queued", "running") for job in jobs.values()):
                external = True
                return True
            unknown = bool(invalid)
        store = project.work_items
        writers = wait_writer_roles(project.config, project.roles)
        status = store.open_state_readonly(
            limit=project.config.limits.work_items_prompt, writer_roles=writers)
        blocked = status.get("blocked_work") if isinstance(status, dict) else None
        if not isinstance(blocked, dict) or not isinstance(blocked.get("items"), list):
            return None
        page_complete = (blocked.get("coverage_complete") is True
                         and blocked.get("truncated") is False
                         and blocked.get("diagnostic") is None)
        unknown |= not page_complete
        requests = None
        for row in blocked["items"]:
            if row.get("holder") != role.name:
                continue
            kind = row.get("wait_kind")
            if row.get("state") == "unknown":
                unknown = True
                continue
            if kind in ("ci", "publication_ci"):
                if row.get("state") not in ("pending", "receipt_bound"):
                    continue
                item = store.get(row["id"])
                if (item["rev"] != row["rev"] or item["status"] != "blocked"
                        or item["holder"] != role.name or item["wait"]["kind"] != kind):
                    unknown = True
                    continue
                if ci_waits.terminal_evidence(store, item) is not None:
                    continue
                wait = ci_waits._resolved(store, item["wait"])
                if wait is None:
                    unknown = True
                    continue
                if requests is None:
                    requests = CiRequests(project.root, project.config.ci_requests).daily()
                if any(request["publication_id"] == wait["publication_id"]
                       and request["sha"] == wait["sha"]
                       and request["state"] != "not_dispatched"
                       for day in requests["days"] for request in day["items"]):
                    external = True
                else:
                    # Missing dispatch evidence is not evidence of remote inactivity.
                    unknown = True
                if external:
                    return True
                continue
            if row.get("state") in ("pending", "receipt_bound"):
                external = True
                return True
        live = True if external else None if unknown else False
        return live
    except (Busy, Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
