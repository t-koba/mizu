"""Operator-owned projection of raw CI aggregate evidence into prompt facts.

The trusted host caller supplies only evidence inputs; this driver owns the
aggregate projection. Its observation is external-untrusted evidence: no
verdict, request state, wake or CI request is derived from it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# Mirrors ci_observation.MAX_CI_WAKE_ROWS and vcs.MAX_CHECKS; this stdlib-only
# driver cannot import the kernel configuration modules.
MAX_ROWS = 2048
MAX_TEXT = 256
MAX_COUNT = 1024
SUMMARY_MAX = 4096
# 2048 aggregate facts with 256-char subjects and 64-char revisions in UTF-8
# plus JSON overhead; serialized as UTF-8, not escaped ASCII. Matches the
# ci_head_state detail bound; the 4 MiB envelope covers the larger request.
DETAIL_MAX = 1048576
_HEX = frozenset("0123456789abcdef")
_REQUEST_KEYS = frozenset({"rows", "summaries", "partial"})


def _text(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_TEXT
            or any(character.isspace() or character == "\x00" for character in value)):
        raise ValueError("Invalid CI summary evidence text")
    return value


def _sha(value):
    if (not isinstance(value, str) or len(value) not in (40, 64)
            or any(character not in _HEX for character in value)):
        raise ValueError("Invalid CI summary revision")
    return value


def _count(value):
    if type(value) is not int or not 1 <= value <= MAX_COUNT:
        raise ValueError("Invalid CI summary check count")
    return value


def _flag(value):
    if type(value) is not bool:
        raise ValueError("Invalid CI summary flag")
    return value


def _rows(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError("Invalid CI summary failure rows")
    for row in value:
        if type(row) is not list or len(row) != 3:
            raise ValueError("Invalid CI summary failure row")
        _text(row[0])
        _sha(row[1])
        _text(row[2])
    return value


def _summaries(value):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError("Invalid CI check summaries")
    exact = {}
    for row in value:
        if type(row) is not list or len(row) != 4:
            raise ValueError("Invalid CI check summary row")
        _text(row[0])
        _sha(row[1])
        _count(row[2])
        _flag(row[3])
        key = (row[0], row[1])
        if key in exact:
            raise ValueError("Duplicate CI check summary")
        exact[key] = {"sha": row[1], "non_passing_checks": row[2],
                      "may_be_incomplete": row[3]}
    return exact


def observed_summary(request) -> dict:
    """Project one validated raw-evidence request into sorted bounded facts."""
    if type(request) is not dict or set(request) != _REQUEST_KEYS:
        raise ValueError("CI summary observation needs one evidence object")
    rows = _rows(request["rows"])
    exact = _summaries(request["summaries"])
    partial = request["partial"]
    if partial is not None:
        _flag(partial)

    counts = {}
    for branch, sha, _check in rows:
        key = (branch, sha)
        counts[key] = counts.get(key, 0) + 1
    facts = []
    for (branch, sha), lower_bound in sorted(counts.items()):
        summary = exact.get((branch, sha))
        if summary is not None and summary["non_passing_checks"] < lower_bound:
            summary = None
        facts.append({
            "subject": branch,
            "revision": sha,
            "outcome": "failure",
            "non_passing_checks": (summary["non_passing_checks"]
                                   if summary else lower_bound),
            "may_be_incomplete": bool(partial is not False or not summary
                                      or summary["may_be_incomplete"]),
        })
        if len(facts) == MAX_ROWS:
            break
    return {"facts": facts}


def main() -> None:
    """Read one request and report one bounded observation on stdout."""
    request = json.load(sys.stdin)
    report = observed_summary(request)
    detail = json.dumps(report, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False)
    if len(detail.encode("utf-8")) > DETAIL_MAX:
        raise SystemExit("CI summary report exceeds the observation detail bound")
    summary = (f"Observed {len(report['facts'])} CI summary facts")[:SUMMARY_MAX]
    observation = {
        "driver": "ci_summary",
        "kind": "ci_summary",
        "summary": summary,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    json.dump({"observations": [observation]}, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
