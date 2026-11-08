"""Read-only validation and projection of cached CI observations.

This module owns bounded normalization and presentation of the shared CI poll
cache. Status polling lives in ``ci_polling``; per-role wake records and due
policy live in ``ci_wake``.
"""
from __future__ import annotations

from .config import (DEFAULT_CI_CACHE_MAX_BRANCHES, DEFAULT_CI_CACHE_MAX_CHECKS_PER_BRANCH,
                     MAX_CI_CACHE_ROWS, MAX_CI_WAKE_EXTRA_BRANCHES)
from .fs import read_json

#: Shared per-project poll state filename (timestamps, cursors, bounded CI
#: facts). Runtime persists it; this module only reads it. Single source so
#: the read projection and runtime poll-state persister cannot disagree on the
#: filename; ``ci_polling`` only updates the bounded CI fields in memory.
POLL_STATE_FILE = "vcs-poll.json"

#: Prompt-only flattened failure projection cap; independent of cache retention.
#: A reached cap conservatively reports possible truncation.
MAX_CI_FAILURES = 32
#: No-config retention defaults; runtime settings may redistribute the same row envelope.
MAX_CI_CHECKS_PER_BRANCH = DEFAULT_CI_CACHE_MAX_CHECKS_PER_BRANCH
MAX_CI_FAILURE_BRANCHES = DEFAULT_CI_CACHE_MAX_BRANCHES
#: Maximum selected wake branches: ci_branch plus 64 configured extras.
MAX_CI_WAKE_BRANCHES = MAX_CI_WAKE_EXTRA_BRANCHES + 1
#: Wake projection must cover every retained selected row, not the prompt's 32 rows.
MAX_CI_WAKE_ROWS = MAX_CI_CACHE_ROWS


def ci_cache_limits(config=None) -> tuple[int, int]:
    """Effective branches/checks retention; malformed runtime settings use defaults.

    Selected wake branches always fit. The per-map row envelope is fixed, and
    checks cannot exceed the local VCS status-parser cap (not a provider limit).
    """
    settings = getattr(config, "vcs", config)
    if not isinstance(settings, dict):
        settings = {}
    branches = settings.get("ci_cache_max_branches", MAX_CI_FAILURE_BRANCHES)
    checks = settings.get("ci_cache_max_checks_per_branch", MAX_CI_CHECKS_PER_BRANCH)
    from .vcs import MAX_CHECKS
    if (type(branches) is not int or not 1 <= branches <= MAX_CI_CACHE_ROWS
            or type(checks) is not int or not 1 <= checks <= MAX_CHECKS):
        return MAX_CI_FAILURE_BRANCHES, MAX_CI_CHECKS_PER_BRANCH
    branches = max(branches, len(effective_ci_wake_branches(config)), 1)
    if branches * checks > MAX_CI_CACHE_ROWS:
        return MAX_CI_FAILURE_BRANCHES, MAX_CI_CHECKS_PER_BRANCH
    return branches, checks


def _valid_ci_run(value) -> str | None:
    """Return a valid adapter run identity or ``None`` (never raises).

    Schema: opaque string 1-128 chars without newline/NUL or terminal
    controls. Invalid reads as absent so rows without identity keep
    exactly ``{sha, check}`` and all existing behavior holds.
    """
    try:
        if not isinstance(value, str) or not value or len(value) > 128:
            return None
        if "\n" in value or "\x00" in value:
            return None
        from .vcs import _reject_terminal_controls as _reject_controls
        _reject_controls(value, "Invalid CI run identity")
        return value
    except Exception:
        return None


def _clean_ci_failures(value, config=None) -> dict:
    """Normalize a stored ``{branch: [{sha, check, run?}]}`` mapping (never raises)."""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    try:
        from .vcs import check_branch as _check_branch
        from .vcs import _SHA as _sha_re
    except Exception:
        return {}
    max_branches, max_checks = ci_cache_limits(config)
    protected = effective_ci_wake_branches(config)
    # Reserve selected branches before unrelated cache entries consume capacity.
    names = list(dict.fromkeys([b for b in protected if b in value] + list(value)))
    for branch in names[:max_branches]:
        rows = value[branch]
        try:
            _check_branch(branch)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        kept = []
        for entry in rows[:max_checks]:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha")
            check = entry.get("check")
            try:
                if not isinstance(sha, str) or not _sha_re.fullmatch(sha):
                    continue
                if (not isinstance(check, str) or not check
                        or len(check) > 256 or "\n" in check or "\x00" in check):
                    continue
            except Exception:
                continue
            row = {"sha": sha, "check": check}
            kept_run = _valid_ci_run(entry.get("run"))
            if kept_run is not None:
                row["run"] = kept_run
            kept.append(row)
        out[branch] = kept
    return out


def _clean_ci_successes(value, config=None) -> dict:
    """Normalize bounded persisted success evidence (same schema as failures)."""
    return _clean_ci_failures(value, config)


def ci_failures_projection(project, *, limit: int = MAX_CI_FAILURES,
                           branches: tuple[str, ...] | None = None, config=None) -> tuple[list, bool | None]:
    """Return bounded cached CI rows and whether a retention/projection bound may have sliced them.

    Schema: ``(rows, may_be_truncated)`` for the shared
    ``vcs-poll.json`` cache. An optional selected-branch filter is applied
    to raw cache keys before configured branch/check retention and
    flattened-row bounds; prompt projections omit it to preserve visibility
    of unrelated refs, while CI wake selection uses configured branches.
    Retained same-SHA/check success evidence annotates a failure with one
    validated ``passed_on`` branch; a missing annotation does not prove
    determinism because success history is bounded. Reaching a failure cap is
    conservatively marked because a cache may already have been sliced and
    the exact omitted count is unknowable. Trust: host-side poll cache;
    adapter facts remain external-untrusted. Failure: an unreadable or
    malformed top-level cache returns ``([], None)``; the boolean is ``None``
    when truncation cannot be determined.
    """
    try:
        raw = read_json(project.root / POLL_STATE_FILE, {})
    except Exception:
        return [], None
    try:
        if not isinstance(raw, dict):
            return [], None
        stored = raw.get("failures")
        if stored is not None and not isinstance(stored, dict):
            return [], None
        if branches is not None:
            if not isinstance(branches, tuple) or len(branches) > MAX_CI_WAKE_BRANCHES:
                return [], None
            from .vcs import check_branch as _check_branch
            try:
                wanted = {_check_branch(item) for item in branches}
            except Exception:
                return [], None
            # Probe only the configured keys; unrelated raw rows cannot consume
            # the branch or flattened-row bounds for wake selection.
            stored = ({name: stored[name] for name in wanted if name in stored}
                      if isinstance(stored, dict) else {})
        if config is None:
            config = getattr(project, "config", None)
        max_branches, max_checks = ci_cache_limits(config)
        may_be_truncated = False
        if isinstance(stored, dict):
            # At a reached retention cap the original full size cannot be
            # recovered from an old or already-cleaned cache.
            may_be_truncated = len(stored) >= max_branches
            if not may_be_truncated:
                may_be_truncated = any(
                    isinstance(rows, list) and len(rows) >= max_checks
                    for rows in stored.values())
        cleaned = _clean_ci_failures(stored, config)
        raw_successes = raw.get("successes")
        successes = _clean_ci_successes(raw_successes, config)
        passed_on = {}
        for success_branch in sorted(successes):
            # _clean_ci_successes has already checked the citation syntax;
            # revalidate at projection boundary before rendering it.
            try:
                from .vcs import check_branch as _check_branch
                citation = _check_branch(success_branch)
                if any(character.isspace() for character in citation):
                    continue
            except Exception:
                continue
            for success in successes[success_branch]:
                passed_on.setdefault((success["sha"], success["check"]), citation)
        flat = []
        for failure_branch in sorted(cleaned):
            for entry in cleaned[failure_branch]:
                row = {"branch": failure_branch, "sha": entry["sha"], "check": entry["check"]}
                kept_run = _valid_ci_run(entry.get("run"))
                if kept_run is not None:
                    row["run"] = kept_run
                citation = passed_on.get((entry["sha"], entry["check"]))
                if citation is not None:
                    row["nondeterministic"] = True
                    row["passed_on"] = citation
                flat.append(row)
        row_limit = max(0, int(limit))
        if row_limit:
            may_be_truncated = may_be_truncated or len(flat) >= row_limit
        elif flat:
            may_be_truncated = True
        return flat[:row_limit], may_be_truncated
    except Exception:
        return [], None


def current_ci_failures(project, *, limit: int = MAX_CI_FAILURES) -> list:
    """Return cached current CI failures as ``[{branch, sha, check, run?}]`` (never raises).

    Schema: best-effort read of the shared ``vcs-poll.json`` ``failures``
    mapping written by :func:`poll_ci`; flattened branch-major, sorted,
    capped at ``limit`` and optionally annotated from retained successes.
    Empty means no observed red (poll disabled,
    never polled, unreadable, or last ticks green). Trust: host-side poll
    cache, adapter facts stay external-untrusted; insight decisions
    are deliberately ignored so a deferred failure stays current until
    green or superseded. Failure: never raises (unreadable reads as none).
    """
    return ci_failures_projection(project, limit=limit)[0]


def selected_ci_failures_present(project, config=None) -> bool:
    """Return whether the selected wake-branch cache has any failure rows.

    Unlike prompt projections, this read is strict: malformed/unreadable
    selected evidence raises so an admission gate cannot mistake it for empty.
    Any nonempty selected row list is conservatively present; row-level
    validation belongs to the normal CI projection.
    """
    if config is None:
        config = getattr(project, "config", None)
    branches = effective_ci_wake_branches(config)
    if not branches:
        return False
    raw = read_json(project.root / POLL_STATE_FILE, {})
    if not isinstance(raw, dict):
        raise ValueError("Invalid CI observation cache")
    failures = raw.get("failures", {})
    if not isinstance(failures, dict):
        raise ValueError("Invalid CI failure cache")
    for branch in branches:
        if branch not in failures:
            continue
        rows = failures[branch]
        if not isinstance(rows, list):
            raise ValueError("Invalid selected CI failure rows")
        if rows:
            return True
    return False


def effective_ci_wake_branches(project_config) -> tuple[str, ...]:
    """Deduplicated configured CI wake branches, fail-closed on bad runtime values."""
    try:
        settings = getattr(project_config, "vcs", None)
        if settings is None and isinstance(project_config, dict):
            settings = project_config
        if not isinstance(settings, dict):
            return ()
        from .vcs import check_branch as _check_branch
        primary = settings.get("ci_branch", "")
        extras = settings.get("ci_wake_extra_branches", ())
        if (not isinstance(primary, str)
                or not isinstance(extras, (list, tuple))
                or len(extras) > MAX_CI_WAKE_EXTRA_BRANCHES):
            return ()
        candidates = ([primary] if primary else []) + list(extras)
        result = set()
        for candidate in candidates:
            try:
                valid = _check_branch(candidate)
                if any(character.isspace() for character in valid):
                    return ()
            except Exception:
                return ()
            result.add(valid)
        return tuple(sorted(result))
    except Exception:
        return ()


def wake_ci_failures(project, config=None) -> list:
    """Cached failures on configured wake branches (never raises).

    The deduplicated ``ci_branch`` plus up to 64 explicit extra branches are
    filtered from raw cache keys before row caps. Other branches remain in
    the unfiltered prompt projection and never wake a role. The projection
    carries every retained row on the selected set (at most 2048).
    """
    try:
        if config is None:
            config = getattr(project, "config", None)
        branches = effective_ci_wake_branches(config)
        if not branches:
            return []
        return ci_failures_projection(
            project, limit=MAX_CI_WAKE_ROWS, branches=branches, config=config)[0]
    except Exception:
        return []
