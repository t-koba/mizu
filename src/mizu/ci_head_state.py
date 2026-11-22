"""Bounded per-CI-branch head state facts for ``vcs_request`` holders.

This is a read-only projection of local evidence: injected upstream refs,
publication receipts, the shared poll cache and recorded CI requests. It never
wakes a role, dispatches a request or writes state. It exists so a current head
that was never sent to CI is visible as a fact instead of an empty ``ci_summary``.

Schema: one row per configured CI branch, sorted by subject, with exactly
``{subject, revision, state, covering_revision, may_be_incomplete}``. States are
``no_head``, ``not_requested``, ``requested``, ``passing``, ``failing`` and
``covered_by_later``. ``covering_revision`` is set only for the last state.
Bounds: the configured branch set is already deduplicated and bounded; each
evidence source is read once per branch within its own reader bound. Trust:
local records and adapter-supplied cache rows are untrusted evidence. Failure:
an unreadable or truncated source raises ``may_be_incomplete`` and never turns
missing evidence into a success claim.
"""
from __future__ import annotations

from . import ci_observation
from .ci_requests import CiRequests
from .fs import read_json
from .publication import Publication
from .vcs import REF_STORE_NAME, read_ref


def _row(subject, revision, state, covering_revision="", incomplete=False):
    return {"subject": subject, "revision": revision, "state": state,
            "covering_revision": covering_revision,
            "may_be_incomplete": bool(incomplete)}


def _receipts(project_root, branch, limit):
    """Return ``(shas, truncated, scan_stopped)``; shas are newest first."""
    try:
        page = Publication.list_receipts(project_root, branch=branch, limit=limit)
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
        rows.append(sha)
    return (rows, bool(page.get("truncated")), bool(page.get("scan_stopped")))


def _ref_head(project, branch):
    """Injected ref sha, plus whether an existing ref could not be read."""
    try:
        return read_ref(project.root, branch), False
    except Exception:
        path = project.root / REF_STORE_NAME / branch
        return "", path.exists() or path.is_symlink()


def _poll_cache(project, config, branches):
    """Validated success/failure SHAs per branch, plus incompleteness flags.

    The cache is read once. Raw rows are checked with the same bounds the
    writer uses; dropped or malformed rows, and a summary row that already
    admits dropped checks, make the observation conservatively incomplete.
    """
    global_incomplete = False
    branch_incomplete = {branch: False for branch in branches}
    try:
        raw = read_json(project.root / ci_observation.POLL_STATE_FILE, {})
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {}, {}, True, branch_incomplete
    shas = {}
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
        per_branch = {}
        for branch in branches:
            rows = value.get(branch, [])
            if not isinstance(rows, list):
                global_incomplete = True
                continue
            if len(kept.get(branch, [])) < len(rows):
                branch_incomplete[branch] = True
            per_branch[branch] = {row["sha"] for row in kept.get(branch, [])
                                  if isinstance(row, dict) and isinstance(row.get("sha"), str)}
        shas[name] = per_branch
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
    return shas.get("failures", {}), shas.get("successes", {}), global_incomplete, branch_incomplete


def _request_rows(project, config):
    """Requested SHAs from the bounded daily page."""
    try:
        daily = CiRequests(project.root, getattr(config, "ci_requests", None)).daily(limit=100)
    except Exception:
        return [], True
    if not isinstance(daily, dict) or not isinstance(daily.get("days"), list):
        return [], True
    incomplete = bool(daily.get("truncated") or daily.get("scan_stopped"))
    rows = []
    for day in daily["days"]:
        items = day.get("items") if isinstance(day, dict) else None
        if not isinstance(items, list):
            incomplete = True
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("sha"), str) or not item["sha"]:
                incomplete = True
                continue
            rows.append(item["sha"])
    return rows, incomplete


def _later_revision(head, receipts, requested_shas, success_shas):
    """Newest receipt strictly newer than ``head`` with CI evidence.

    Only SHAs present in the branch's receipt page can be ordered, and the
    page is already newest-first. An injected ref absent from that page has no
    established relation to it, so it cannot claim later coverage.
    """
    if head not in receipts:
        return ""
    for sha in receipts[:receipts.index(head)]:
        if sha in requested_shas or sha in success_shas:
            return sha
    return ""


def ci_head_state_facts(project, config=None) -> list:
    """One bounded head-state fact per configured CI branch (never raises)."""
    if config is None:
        config = getattr(project, "config", None)
    branches = ci_observation.effective_ci_wake_branches(config)
    if not branches:
        return []
    failures, successes, cache_incomplete, cache_branch_incomplete = _poll_cache(
        project, config, branches)
    requests, requests_incomplete = _request_rows(project, config)

    pages = {}
    for branch in branches:
        head, ref_incomplete = _ref_head(project, branch)
        incomplete = bool(ref_incomplete)
        if not head:
            receipt_rows, _, head_scan_stopped = _receipts(project.root, branch, 1)
            incomplete = bool(incomplete or head_scan_stopped)
            if receipt_rows:
                head = receipt_rows[0]
        receipts = []
        if head:
            receipts, truncated, scan_stopped = _receipts(project.root, branch, 100)
            incomplete = bool(incomplete or truncated or scan_stopped)
        pages[branch] = (head, receipts, incomplete)

    facts = []
    for branch in sorted(branches):
        head, receipts, incomplete = pages[branch]
        incomplete = bool(incomplete or cache_incomplete or requests_incomplete
                          or cache_branch_incomplete[branch])
        if not head:
            facts.append(_row(branch, "", "no_head", incomplete=incomplete))
            continue
        if head in failures.get(branch, set()):
            facts.append(_row(branch, head, "failing", incomplete=incomplete))
            continue
        if head in successes.get(branch, set()):
            facts.append(_row(branch, head, "passing", incomplete=incomplete))
            continue
        if head in requests:
            facts.append(_row(branch, head, "requested", incomplete=incomplete))
            continue
        covering = _later_revision(head, receipts, set(requests),
                                   successes.get(branch, set()))
        if covering:
            facts.append(_row(branch, head, "covered_by_later", covering, incomplete))
        else:
            facts.append(_row(branch, head, "not_requested", incomplete=incomplete))
    return facts
