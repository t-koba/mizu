"""Bounded per-CI-branch evidence facts for ``vcs_request`` holders.

This is a read-only, branch-scoped projection of local evidence: injected
upstream refs, publication receipts, the shared poll cache and recorded CI
requests. It never wakes a role, dispatches a request, writes state or derives
a verdict. Every declared subject appears once at its exact published head,
with the runs and requests recorded for that revision or explicit
``no_evidence``.

Schema: one row per configured CI branch, sorted by subject, with exactly
``{subject, revision, runs, requests, no_head, no_evidence, may_be_incomplete}``.
``runs`` and ``requests`` are arrays of at most eight opaque identity strings
each, never raw records. ``no_head`` marks a branch without a ref or receipt;
``no_evidence`` marks a revision with no runs and no requests. There is no
passing/failing verdict, request state or later-revision coverage inference: a
request belongs only to the branch whose receipt carries its publication ID at
the exact head. Bounds: the configured branch set is already deduplicated and
bounded; the newest head takes one receipt row, the exact head's publication
IDs come from an exact-SHA/branch page, CI requests from one bounded daily page,
and each projected identity array is capped at eight. Trust: local records and
adapter-supplied cache rows are untrusted evidence. Failure: an unreadable ref
or source, an invalid page, a stopped scan, an exact-head page truncated so
another publication ID for that head could be hidden, a request page that stops
before (or cannot establish) the head's publication window, or a reached
identity cap sets ``may_be_incomplete``. Page fullness after that window or on
unrelated records never flags the exact head, and missing evidence is never a
passing verdict.
"""
from __future__ import annotations

import datetime as dt

from . import ci_observation
from .ci_observation import _valid_ci_run
from .ci_requests import CiRequests
from .fs import read_json
from .publication import Publication
from .vcs import REF_STORE_NAME, read_ref


def _row(subject, revision, runs=(), requests=(), *, no_head=False,
         may_be_incomplete=False):
    runs, requests = list(runs), list(requests)
    return {"subject": subject, "revision": revision, "runs": runs,
            "requests": requests, "no_head": bool(no_head),
            "no_evidence": bool(revision) and not runs and not requests,
            "may_be_incomplete": bool(may_be_incomplete)}


def _identity_ids(values, cap):
    """Unique opaque identities in first-seen order, plus whether the cap was hit."""
    kept, seen, total = [], set(), 0
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        total += 1
        if len(kept) < cap:
            kept.append(value)
    return kept, total > cap


def _receipts(project_root, branch, limit, *, published_sha=None):
    """Return ``(pairs, truncated, scan_stopped)``; pairs are newest first.

    Each pair is ``(publication_id, published_sha)``: request attribution needs
    the publication identity, not just the SHA. An exact-SHA query bounds one
    head, so its truncation can hide another publication ID for that head.
    """
    try:
        page = Publication.list_receipts(project_root, branch=branch,
                                         published_sha=published_sha, limit=limit)
    except Exception:
        return [], False, True
    if not isinstance(page, dict) or not isinstance(page.get("publications"), list):
        return [], False, True
    rows = []
    for entry in page["publications"]:
        if not isinstance(entry, dict):
            return [], False, True
        identity, sha = entry.get("publication_id"), entry.get("published_sha")
        if not isinstance(identity, str) or not identity or not isinstance(sha, str) or not sha:
            return [], False, True
        rows.append((identity, sha))
    return (rows, bool(page.get("truncated")), bool(page.get("scan_stopped")))


def _ref_head(project, branch):
    """Injected ref sha, plus whether an existing ref could not be read."""
    try:
        return read_ref(project.root, branch), False
    except Exception:
        path = project.root / REF_STORE_NAME / branch
        return "", path.exists() or path.is_symlink()


def _head_window(project, publication_ids):
    """UTC day of the earliest head publication, plus incompleteness."""
    dates, incomplete = [], False
    for identity in publication_ids:
        try:
            record = Publication.read_receipt(project.root, identity)
            dates.append(dt.datetime.fromisoformat(
                record["recorded_at"]).astimezone(dt.timezone.utc).date().isoformat())
        except Exception:
            incomplete = True
    return (min(dates) if dates else ""), incomplete


def _poll_cache(project, config, branches):
    """Runs by ``(branch, sha)`` from the validated cache, plus incompleteness.

    The cache is read once. Raw rows are checked with the same bounds the
    writer uses; dropped or malformed rows, invalid run identities, and a
    summary row that already admits dropped checks make the observation
    conservatively incomplete. No result is interpreted as a verdict.
    """
    global_incomplete = False
    branch_incomplete = {branch: False for branch in branches}
    runs = {branch: {} for branch in branches}
    try:
        raw = read_json(project.root / ci_observation.POLL_STATE_FILE, {})
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return runs, True, branch_incomplete
    for name, clean in (("failures", ci_observation._clean_ci_failures),
                        ("successes", ci_observation._clean_ci_successes)):
        value = raw.get(name, {})
        if not isinstance(value, dict):
            global_incomplete = True
            value = {}
        try:
            kept = clean(value, config)
        except Exception:
            kept = {}
            global_incomplete = True
        for branch in branches:
            rows = value.get(branch, [])
            if not isinstance(rows, list):
                global_incomplete = True
                continue
            if len(kept.get(branch, [])) < len(rows):
                branch_incomplete[branch] = True
            if any(isinstance(entry, dict) and "run" in entry
                   and _valid_ci_run(entry.get("run")) is None for entry in rows):
                branch_incomplete[branch] = True
            for entry in kept.get(branch, []):
                if not isinstance(entry, dict):
                    continue
                sha = entry.get("sha")
                run = _valid_ci_run(entry.get("run"))
                if not isinstance(sha, str) or not sha or run is None:
                    continue
                runs[branch].setdefault(sha, [])
                if run not in runs[branch][sha]:
                    runs[branch][sha].append(run)
    summaries = raw.get("ci_check_summaries", {})
    if not isinstance(summaries, dict):
        global_incomplete = True
    else:
        for branch in branches:
            rows = summaries.get(branch, [])
            if not isinstance(rows, list):
                global_incomplete = True
                continue
            if any(isinstance(row, dict) and row.get("may_be_incomplete") is True
                   for row in rows):
                branch_incomplete[branch] = True
    return runs, global_incomplete, branch_incomplete


def _request_rows(project, config):
    """``(triples, last_day, full, broken)`` from the bounded daily page.

    Triples are ``(publication_id, sha, request_id)``. ``last_day`` is the
    newest UTC day the page covers, ``full`` means it stops early, and
    ``broken`` means the page cannot be trusted at all.
    """
    try:
        daily = CiRequests(project.root, getattr(config, "ci_requests", None)).daily(limit=100)
    except Exception:
        return [], "", False, True
    if not isinstance(daily, dict) or not isinstance(daily.get("days"), list):
        return [], "", False, True
    full = bool(daily.get("truncated"))
    broken = bool(daily.get("scan_stopped"))
    rows, last_day = [], ""
    for day in daily["days"]:
        if not isinstance(day, dict):
            broken = True
            continue
        date = day.get("date")
        if not isinstance(date, str) or not date:
            broken = True
        else:
            last_day = max(last_day, date)
        items = day.get("items")
        if not isinstance(items, list):
            broken = True
            continue
        for item in items:
            if not isinstance(item, dict):
                broken = True
                continue
            identity, publication_id, sha = (item.get("request_id"),
                                             item.get("publication_id"), item.get("sha"))
            if not all(isinstance(value, str) and value for value in
                       (identity, publication_id, sha)):
                broken = True
                continue
            rows.append((publication_id, sha, identity))
    return rows, last_day, full, broken


def ci_head_state_facts(project, config=None) -> list:
    """One bounded evidence row per configured CI branch (never raises)."""
    if config is None:
        config = getattr(project, "config", None)
    branches = ci_observation.effective_ci_wake_branches(config)
    if not branches:
        return []
    pages = {}
    for branch in branches:
        head, ref_incomplete = _ref_head(project, branch)
        incomplete = bool(ref_incomplete)
        if not head:
            page, _, head_scan_stopped = _receipts(project.root, branch, 1)
            incomplete = bool(incomplete or head_scan_stopped)
            if page:
                head = page[0][1]
        publication_ids = []
        if head:
            page, truncated, scan_stopped = _receipts(
                project.root, branch, 100, published_sha=head)
            incomplete = bool(incomplete or truncated or scan_stopped)
            publication_ids = [identity for identity, sha in page if sha == head]
        pages[branch] = (head, publication_ids, incomplete)

    runs, cache_incomplete, cache_branch_incomplete = _poll_cache(project, config, branches)
    requests, last_day, request_full, request_broken = _request_rows(project, config)

    facts = []
    for branch in sorted(branches):
        head, publication_ids, incomplete = pages[branch]
        incomplete = bool(incomplete or cache_incomplete or request_broken
                          or cache_branch_incomplete[branch])
        if request_full and head:
            window, window_incomplete = _head_window(project, publication_ids)
            incomplete = bool(incomplete or window_incomplete
                              or not window or last_day < window)
        if not head:
            facts.append(_row(branch, "", no_head=True, may_be_incomplete=incomplete))
            continue
        branch_runs, runs_capped = _identity_ids(runs[branch].get(head, ()), 8)
        eligible = set(publication_ids)
        branch_requests, requests_capped = _identity_ids(
            (identity for publication_id, sha, identity in requests
             if sha == head and publication_id in eligible), 8)
        incomplete = bool(incomplete or runs_capped or requests_capped)
        facts.append(_row(branch, head, branch_runs, branch_requests,
                          may_be_incomplete=incomplete))
    return facts
