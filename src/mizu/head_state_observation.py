"""Trusted host gathering of raw CI head evidence for an observation driver.

The host caller assembles only evidence inputs: exact branch heads, the exact
publication pairs and page flags at each head, receipt instants, cleaned run
identities and validated CI request pages. A configured operator driver projects
those inputs into bounded prompt facts through the generic host seam; this
module never recomputes or repairs the returned rows.

Schema: ``ci_head_state_facts(project, config=None)`` returns a list of at most
one plain row object per configured branch with exactly
``{subject, revision, runs, requests, no_head, no_evidence, may_be_incomplete}``;
each identity array holds at most eight unique strings. Bounds: the request
carries at most ``MAX_CI_WAKE_BRANCHES`` configured branches, one newest-first receipt page, one
exact-head receipt page, one shared CI request scan for all exact heads and one
cleaned poll-cache read. Trust: refs, receipts, cache rows and driver output are
untrusted evidence; the kernel-owned queue records the observation as
external-untrusted. Retry/cancellation: one driver call, no replay. Evidence:
the driver observation is appended to its declared queue by the host seam.
Failure: an undeclared/refused/failing driver, an invalid observation or a
branch set beyond the bound returns ``[]``; no projected fact is invented in
the kernel.
"""
from __future__ import annotations

import datetime as dt

from . import ci_observation, host_drivers
from .ci_observation import MAX_CI_WAKE_BRANCHES, _valid_ci_run
from .ci_requests import CiRequests
from .context_vcs_read_tools import _RECORD_ENVELOPE_KEYS
from .fs import loads_finite, read_json
from .publication import Publication
from .vcs import REF_STORE_NAME, read_ref

#: Matches the example driver's projection and response bounds.
MAX_BRANCHES = MAX_CI_WAKE_BRANCHES
MAX_IDENTITIES = 8
_ROW_KEYS = frozenset({"subject", "revision", "runs", "requests", "no_head",
                       "no_evidence", "may_be_incomplete"})


def _identity(value):
    return isinstance(value, str) and value and len(value) <= 128 and not any(
        character.isspace() or character == "\x00" for character in value)


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
        if not _identity(identity) or not _identity(sha):
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


def _recordings(project, publication_ids):
    """Exact publication instants as UTC strings, paired with unreadable IDs."""
    rows, incomplete = [], False
    for identity in publication_ids:
        try:
            record = Publication.read_receipt(project.root, identity)
            instant = dt.datetime.fromisoformat(
                record["recorded_at"]).astimezone(dt.timezone.utc)
            rows.append([identity, instant.isoformat()])
        except Exception:
            rows.append([identity, None])
            incomplete = True
    return rows, incomplete


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


def _request_page(result):
    """Validate one shared-scan page: request identities plus incompleteness."""
    if not isinstance(result, dict):
        return {"items": [], "incomplete": True}
    items, incomplete = result.get("items"), result.get("incomplete")
    if (not isinstance(items, list)
            or any(not _identity(value) for value in items)
            or not isinstance(incomplete, bool)):
        return {"items": [], "incomplete": True}
    return {"items": items, "incomplete": incomplete}


def head_state_request(project, config=None):
    """Assemble one bounded plain JSON request from local raw evidence."""
    if config is None:
        config = getattr(project, "config", None)
    branches = ci_observation.effective_ci_wake_branches(config)
    if not branches or len(branches) > MAX_BRANCHES:
        return {}
    runs, cache_incomplete, cache_branch_incomplete = _poll_cache(project, config, branches)
    entries, by_branch, queries = [], {}, {}
    for branch in branches:
        head, ref_incomplete = _ref_head(project, branch)
        head_page_scan_stopped = False
        if not head:
            pairs, _, head_page_scan_stopped = _receipts(project.root, branch, 1)
            if pairs:
                head = pairs[0][1]
        receipt_pairs, truncated, exact_stopped = [], False, False
        if head:
            receipt_pairs, truncated, exact_stopped = _receipts(
                project.root, branch, 100, published_sha=head)
        publication_ids = [identity for identity, sha in receipt_pairs if sha == head]
        recordings = []
        if head and publication_ids:
            recordings, _ = _recordings(project, publication_ids)
            instants = [dt.datetime.fromisoformat(stamp) for _, stamp in recordings
                        if stamp is not None]
            if instants:
                queries[branch] = (publication_ids, min(instants))
        entry = {
            "subject": branch,
            "head": head,
            "head_unreadable": bool(ref_incomplete),
            "head_page_scan_stopped": bool(head_page_scan_stopped),
            "exact_page": {
                "pairs": [list(pair) for pair in receipt_pairs],
                "truncated": bool(truncated),
                "scan_stopped": bool(exact_stopped),
            },
            "recordings": recordings,
            "runs": list(runs[branch].get(head, ())) if head else [],
            "cache_incomplete": bool(cache_incomplete),
            "branch_incomplete": bool(cache_branch_incomplete[branch]),
            "requests": None,
        }
        entries.append(entry)
        by_branch[branch] = entry

    if queries:
        try:
            shared_pages = CiRequests(
                project.root, getattr(config, "ci_requests", None)).matching_many(queries)
        except Exception:
            shared_pages = {}
        if not isinstance(shared_pages, dict):
            shared_pages = {}
        for branch in queries:
            by_branch[branch]["requests"] = _request_page(shared_pages.get(branch))
    return {"branches": entries}



def ci_head_state_facts(project, config=None) -> list:
    """Return the driver's bounded head-state rows, or ``[]`` when refused."""
    if config is None:
        config = getattr(project, "config", None)
    try:
        request = head_state_request(project, config)
        if not request:
            return []
        response = host_drivers.observe(config, project, "ci_head_state", request)
        observations = response.get("observations") if isinstance(response, dict) else None
        if (not isinstance(observations, list) or len(observations) != 1
                or type(observations[0]) is not dict
                or not isinstance(observations[0].get("detail"), str)):
            return []
        detail = loads_finite(observations[0]["detail"].encode("utf-8"))
        if (not isinstance(detail, dict) or set(detail) != {"branches"}
                or _RECORD_ENVELOPE_KEYS.intersection(detail)):
            return []
        rows = detail["branches"]
        if (not isinstance(rows, list) or not rows or len(rows) > MAX_BRANCHES
                or any(type(row) is not dict or set(row) != _ROW_KEYS for row in rows)):
            return []
        subjects = [row["subject"] for row in rows]
        configured = {entry["subject"] for entry in request["branches"]}
        if (len(set(subjects)) != len(subjects)
                or not set(subjects) <= configured
                or subjects != sorted(subjects)):
            return []
        for row in rows:
            if (not _identity(row["subject"]) or not isinstance(row["revision"], str)
                    or (row["revision"] and not _identity(row["revision"]))
                    or any(type(row[key]) is not bool for key in
                           ("no_head", "no_evidence", "may_be_incomplete"))):
                return []
            runs = row["runs"]
            if (not isinstance(runs, list) or len(runs) > MAX_IDENTITIES
                    or any(_valid_ci_run(value) is None for value in runs)
                    or len(set(runs)) != len(runs)):
                return []
            requests = row["requests"]
            if (not isinstance(requests, list) or len(requests) > MAX_IDENTITIES
                    or any(not _identity(value) for value in requests)
                    or len(set(requests)) != len(requests)):
                return []
        return rows
    except Exception:
        return []
