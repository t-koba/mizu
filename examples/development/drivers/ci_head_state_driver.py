"""Operator-owned projection of raw CI head evidence into bounded prompt facts.

The trusted host caller supplies only evidence inputs; this driver owns the
branch row projection. Its observation is external-untrusted evidence: no
verdict, request state, wake or CI request is derived from it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# Mirrors config.MAX_CI_WAKE_EXTRA_BRANCHES + 1; this stdlib-only
# driver cannot import the kernel configuration module.
MAX_BRANCHES = 65
MAX_IDENTITIES = 8
MAX_TEXT = 128
MAX_RECEIPTS = 100
SUMMARY_MAX = 4096
# 65 branches x 16 identities x 128 characters plus JSON overhead, rounded
# up to a power of two; the kernel call envelope is 512 KiB by default.
DETAIL_MAX = 262144
_BRANCH_KEYS = frozenset({
    "subject", "head", "head_unreadable", "head_page_scan_stopped", "exact_page",
    "recordings", "runs", "cache_incomplete", "branch_incomplete", "requests"})
_PAGE_KEYS = frozenset({"pairs", "truncated", "scan_stopped"})
_REQUEST_KEYS = frozenset({"items", "incomplete"})


def _text(value, *, allow_empty=False):
    if (not isinstance(value, str) or len(value) > MAX_TEXT
            or any(character.isspace() or character == "\x00" for character in value)):
        raise ValueError("Invalid head evidence text")
    if not value and not allow_empty:
        raise ValueError("Empty head evidence identity")
    return value


def _flag(value):
    if type(value) is not bool:
        raise ValueError("Invalid head evidence flag")
    return value


# Same terminal-control rule as the host's ci_observation._valid_ci_run;
# ordinary spaces are valid run identities and must survive the projection.
_BIDI_CONTROLS = frozenset(
    "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069\ufeff")


def _run_identity(value):
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ValueError("Invalid CI run identity")
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F or char in _BIDI_CONTROLS:
            raise ValueError("Invalid CI run identity")
    return value


def _page(value):
    if type(value) is not dict or set(value) != _PAGE_KEYS:
        raise ValueError("Invalid exact-head receipt page")
    pairs = value["pairs"]
    if (not isinstance(pairs, list) or len(pairs) > MAX_RECEIPTS
            or any(type(pair) is not list or len(pair) != 2 for pair in pairs)):
        raise ValueError("Invalid exact-head receipt pairs")
    for identity, sha in pairs:
        _text(identity)
        _text(sha)
    return pairs, _flag(value["truncated"]), _flag(value["scan_stopped"])


def _recordings(value, publication_ids):
    if (not isinstance(value, list) or len(value) > MAX_RECEIPTS
            or any(type(pair) is not list or len(pair) != 2 for pair in value)):
        raise ValueError("Invalid publication recordings")
    stamps = {}
    for identity, stamp in value:
        _text(identity)
        if identity in stamps or identity not in publication_ids:
            raise ValueError("Unbound or duplicate publication recording")
        if stamp is not None:
            try:
                parsed = datetime.fromisoformat(_text(stamp))
            except (TypeError, ValueError):
                raise ValueError("Invalid recorded_at instant") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Recorded_at needs an offset")
        stamps[identity] = stamp
    if set(stamps) != set(publication_ids):
        raise ValueError("Missing publication recording")
    return stamps


def _request_page(value):
    if value is None:
        return [], True
    if type(value) is not dict or set(value) != _REQUEST_KEYS:
        raise ValueError("Invalid head request page")
    items = value["items"]
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise ValueError("Invalid head request identities")
    for item in items:
        _text(item)
    return items, _flag(value["incomplete"])


def _identity_ids(values, cap=MAX_IDENTITIES):
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


def _row(subject, revision, runs=(), requests=(), *, no_head=False,
         may_be_incomplete=False):
    runs, requests = list(runs), list(requests)
    return {"subject": subject, "revision": revision, "runs": runs,
            "requests": requests, "no_head": bool(no_head),
            "no_evidence": bool(revision) and not runs and not requests,
            "may_be_incomplete": bool(may_be_incomplete)}


def _project(branch):
    if type(branch) is not dict or set(branch) != _BRANCH_KEYS:
        raise ValueError("Invalid head-state branch evidence")
    subject = _text(branch["subject"])
    head = _text(branch["head"], allow_empty=True)
    incomplete = any(_flag(branch[key]) for key in
                     ("head_unreadable", "head_page_scan_stopped",
                      "cache_incomplete", "branch_incomplete"))
    pairs, page_truncated, page_stopped = _page(branch["exact_page"])
    incomplete = incomplete or page_truncated or page_stopped
    publication_ids = [identity for identity, sha in pairs if head and sha == head]
    stamps = _recordings(branch["recordings"], publication_ids)
    instants = [datetime.fromisoformat(stamps[identity]) for identity in publication_ids
                if stamps[identity] is not None]
    incomplete = incomplete or len(instants) < len(publication_ids)
    page = branch["requests"]
    if publication_ids and instants:
        if page is None:
            raise ValueError("Head requests missing for an established window")
        request_values, request_incomplete = _request_page(page)
        incomplete = incomplete or request_incomplete
    elif publication_ids:
        if page is not None:
            raise ValueError("Head requests supplied without a publication window")
        request_values = []
    else:
        if page is not None:
            raise ValueError("Head requests supplied without exact-head publications")
        request_values = []
    if not head:
        return _row(subject, "", no_head=True, may_be_incomplete=incomplete)
    run_values = branch["runs"]
    if not isinstance(run_values, list):
        raise ValueError("Invalid cleaned run identities")
    for value in run_values:
        _run_identity(value)
    runs, runs_capped = _identity_ids(run_values)
    requests, requests_capped = _identity_ids(request_values)
    return _row(subject, head, runs, requests,
                may_be_incomplete=incomplete or runs_capped or requests_capped)


def observed_head_state(request) -> dict:
    """Project one validated raw-evidence request into sorted bounded rows."""
    if type(request) is not dict or set(request) != {"branches"}:
        raise ValueError("Head-state observation needs one branches object")
    branches = request["branches"]
    if (not isinstance(branches, list) or not branches
            or len(branches) > MAX_BRANCHES):
        raise ValueError("Invalid head-state branch set")
    rows = [_project(branch) for branch in branches]
    subjects = [row["subject"] for row in rows]
    if len(set(subjects)) != len(subjects):
        raise ValueError("Duplicate head-state subject")
    return {"branches": sorted(rows, key=lambda row: row["subject"])}


def main() -> None:
    """Read one request and report one bounded observation on stdout."""
    request = json.load(sys.stdin)
    report = observed_head_state(request)
    detail = json.dumps(report, separators=(",", ":"), allow_nan=False)
    if len(detail) > DETAIL_MAX:
        raise SystemExit("head-state report exceeds the observation detail bound")
    identities = sum(len(row["runs"]) + len(row["requests"]) for row in report["branches"])
    summary = (f"Observed {identities} head evidence identities "
               f"over {len(report['branches'])} branch(es)")[:SUMMARY_MAX]
    observation = {
        "driver": "ci_head_state",
        "kind": "ci_head_state",
        "summary": summary,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    json.dump({"observations": [observation]}, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
