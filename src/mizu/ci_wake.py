"""Per-role CI wake record and due calculation (bounded, never raises).

This module owns the per-role CI wake record/due helpers and their
wake-version/default constants. CI status polling (``poll_ci``) lives in
``ci_polling``; proposal polling lives in ``vcs_proposal_poll``. Shared
coordination (``poll_project_vcs``) and state persistence stay in
runtime; admission lives in ``role_admission`` and Engine dispatch in
``engine_dispatch``. The helpers read the shared cached-CI projection via ``ci_observation``, the operator ``vcs``
mapping shape via config, and the per-role record via ``fs``; this module
never imports runtime.
"""
from __future__ import annotations

import json
import time

from .ci_observation import (MAX_CI_WAKE_BRANCHES, _valid_ci_run, _valid_ci_summary_fact,
                             effective_ci_wake_branches, wake_ci_observation)
from .fs import read_json, write_json


#: Default re-wake interval for an unchanged red (seconds). An unchanged
#: red must still re-wake eventually so it can never park a writer forever.
DEFAULT_CI_REWAKE_SECONDS = 1800
#: Version of the per-role CI wake record.
CI_WAKE_VERSION = 2


def wake_facts_cover_due(offered_facts, frozen_facts) -> bool:
    """Match every frozen aggregate fact to a valid offered fact (never raises).

    Both inputs are lists of the exact CI summary schema. Empty or malformed
    frozen facts fail closed; additional offered facts do not block settlement.
    This pure check reads no cache and compares all fields, not just revisions.
    """
    try:
        return (isinstance(offered_facts, list) and isinstance(frozen_facts, list)
                and bool(frozen_facts)
                and all(_valid_ci_summary_fact(fact)
                        and any(_valid_ci_summary_fact(offered) and offered == fact
                                for offered in offered_facts)
                        for fact in frozen_facts))
    except Exception:
        return False


def _ci_rewake_seconds(project_config) -> float:
    """Configured ``[vcs] ci_rewake_seconds`` (never raises).

    Schema: operator config. Trust: configuration. Failure: never raises;
    missing or garbage reads as the DEFAULT_CI_REWAKE_SECONDS default since
    tests build vcs dicts by hand.
    """
    try:
        settings = getattr(project_config, "vcs", None)
        if settings is None and isinstance(project_config, dict):
            settings = project_config
        raw = settings.get("ci_rewake_seconds", DEFAULT_CI_REWAKE_SECONDS) if isinstance(settings, dict) else DEFAULT_CI_REWAKE_SECONDS
    except Exception:
        return float(DEFAULT_CI_REWAKE_SECONDS)
    try:
        if type(raw) is bool:
            return float(DEFAULT_CI_REWAKE_SECONDS)
        value = float(raw) if isinstance(raw, (int, float)) else None
        if value is None:
            return float(DEFAULT_CI_REWAKE_SECONDS)
        import math as _math
        if not _math.isfinite(value) or not 60 <= value <= 86400:
            return float(DEFAULT_CI_REWAKE_SECONDS)
        return value
    except Exception:
        return float(DEFAULT_CI_REWAKE_SECONDS)


def _ci_wake_path(project, role_name):
    """Per-role CI wake record path (None when unusable, never raises)."""
    try:
        name = str(role_name)
        if not name or "/" in name or "\\" in name or "\x00" in name or ".." in name:
            return None
        return project.root / f"vcs-ci-wake-{name}.json"
    except Exception:
        return None


def read_ci_wake(project, role_name):
    """Last bounded CI wake record for one role (never raises).

    Schema: version 2 stores selected ``branches``, a SHA-256 digest of all
    branch-specific failure facts, published ``snapshot`` and timestamp
    ``at``. Version-1 records deliberately read as-is and cause one due wake.
    """
    try:
        path = _ci_wake_path(project, role_name)
        if path is None:
            return None
        raw = read_json(path, None)
        if raw is None or not isinstance(raw, dict):
            return None
        return raw
    except Exception:
        return None


def _normalize_ci_wake_branches(branches) -> tuple[str, ...]:
    """Validate and sort the bounded configured wake-branch set."""
    try:
        if (not isinstance(branches, (tuple, list)) or not branches
                or len(branches) > MAX_CI_WAKE_BRANCHES):
            return ()
        from .vcs import check_branch as _check_branch
        result = set()
        for value in branches:
            branch = _check_branch(value)
            if any(character.isspace() for character in branch):
                return ()
            result.add(branch)
        return tuple(sorted(result))
    except Exception:
        return ()


def _ci_wake_fact_digest(present, branches) -> str | None:
    """Hash complete bounded branch/SHA/check/run facts without storing a list."""
    try:
        import hashlib
        allowed = set(branches)
        facts = []
        if not isinstance(present, list):
            return None
        for entry in present:
            if not isinstance(entry, dict) or entry.get("branch") not in allowed:
                continue
            sha, check = entry.get("sha"), entry.get("check")
            if not isinstance(sha, str) or not isinstance(check, str):
                return None
            fact = [entry["branch"], sha, check, _valid_ci_run(entry.get("run")) or ""]
            facts.append(fact)
        facts.sort()
        payload = json.dumps([list(branches), facts], ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        return None


def write_ci_wake(project, role_name, branches, present, snapshot_id, now=None) -> None:
    """Record a role's whole selected-set red using a bounded digest.

    Schema: branches are the selected wake set, ``present`` carries its
    bounded ``[{branch, sha, check, run?}]`` rows, ``snapshot_id`` is the
    published head and ``now`` epoch seconds. Malformed inputs write nothing;
    failures in wake suppression never break dispatch.
    """
    try:
        path = _ci_wake_path(project, role_name)
        if path is None or not isinstance(snapshot_id, str) or not snapshot_id:
            return
        selected = _normalize_ci_wake_branches(branches)
        if not selected:
            return
        try:
            at = time.time() if now is None else float(now)
        except Exception:
            return
        import math as _math
        if not _math.isfinite(at):
            return
        fact_digest = _ci_wake_fact_digest(present, selected)
        if fact_digest is None:
            return
        write_json(path, {"version": CI_WAKE_VERSION, "branches": list(selected),
                          "fact_sha256": fact_digest, "snapshot": snapshot_id, "at": at})
    except Exception:
        return


def _ci_due(present, record, snapshot_id, now, rewake_seconds, branches) -> bool:
    """True when selected CI facts are due to wake (fail open, never raises).

    Due on first sighting, any selected-set/fact digest change, a moved
    published head, or an elapsed interval. Old-version/malformed records
    are due once; an empty presence is never due.
    """
    try:
        if not present:
            return False
        if not isinstance(record, dict):
            return True
        selected = _normalize_ci_wake_branches(branches)
        if not selected:
            return True
        if record.get("version") != CI_WAKE_VERSION:
            return True
        if record.get("branches") != list(selected):
            return True
        current_digest = _ci_wake_fact_digest(present, selected)
        if not isinstance(current_digest, str) or record.get("fact_sha256") != current_digest:
            return True
        record_snapshot = record.get("snapshot")
        if not isinstance(record_snapshot, str) or not isinstance(snapshot_id, str):
            return True
        if record_snapshot != snapshot_id:
            return True
        try:
            at = record.get("at")
            if type(at) is bool:
                return True
            at_value = float(at)
            now_value = float(now)
            rewake_value = float(rewake_seconds)
            import math as _math
            if not _math.isfinite(at_value) or not _math.isfinite(now_value):
                return True
            if not _math.isfinite(rewake_value):
                return True
        except Exception:
            return True
        if at_value > now_value:
            return True
        return (now_value - at_value) >= rewake_value
    except Exception:
        return True


def _ci_due_for_with_facts(project, project_config, role, snapshot_id=None, now=None) -> tuple[list, list]:
    """Coherent due rows and frozen external-check facts for one role.

    Reads the poll cache once. Facts are returned only alongside due rows;
    admission labels and caller-owned event-wake masks do not enter selection.
    The digest still uses branch/SHA/check/run rows, never aggregate counts.

    A red on the configured CI wake set wakes only roles with ``ci_wake``
    (writable roles by default): event-triggered or unchanged-code dispatch
    for any other role
    exits cheaply instead of spending a run on CI it never subscribed to.
    The red stays visible via ``current_ci_failures`` prompts, but it wakes
    only on change: a new failing sha/check, a moved published head, or an
    elapsed ``ci_rewake_seconds`` interval. Without ``snapshot_id``/``now``
    this falls back to presence (as before) so project-level checks stay
    conservative.
    """
    observation = None
    try:
        if isinstance(role, str):
            role_name = role
            if not _ci_wake_enabled(project_config, role):
                return [], []
        else:
            try:
                role_name = getattr(role, "name", None)
            except Exception:
                role_name = None
            try:
                if not bool(getattr(role, "ci_wake", False)):
                    return [], []
            except Exception:
                return [], []
        branches = effective_ci_wake_branches(project_config)
        if not branches:
            return [], []
        try:
            observation = wake_ci_observation(project, project_config)
            present, facts = observation
        except Exception:
            return [], []
        if not present:
            return [], []
        if snapshot_id is None or now is None:
            return present, facts
        if not isinstance(role_name, str) or not role_name:
            return present, facts
        try:
            rewake = _ci_rewake_seconds(project_config)
        except Exception:
            rewake = float(DEFAULT_CI_REWAKE_SECONDS)
        try:
            record = read_ci_wake(project, role_name)
        except Exception:
            return present, facts
        try:
            if _ci_due(present, record, snapshot_id, now, rewake, branches):
                return present, facts
            return [], []
        except Exception:
            return present, facts
    except Exception:
        try:
            return (observation if observation is not None
                    else wake_ci_observation(project, project_config))
        except Exception:
            return [], []


def _ci_due_for(project, project_config, role, snapshot_id=None, now=None) -> list:
    """Compatibility rows-only selection; check/run wake suppression is unchanged."""
    return _ci_due_for_with_facts(project, project_config, role, snapshot_id, now)[0]


def _ci_wake_enabled(project_config, role_name) -> bool:
    """True when the named role opts into the CI-red wake (never raises)."""
    try:
        roles = getattr(project_config, "roles", None) or {}
        role = roles.get(role_name) if isinstance(roles, dict) else None
        return bool(getattr(role, "ci_wake", False))
    except Exception:
        return False
