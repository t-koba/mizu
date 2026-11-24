"""Read-only, page-local observed runner wall time; never billing or a total.

Operator-owned observation driver: it joins a validated dispatch page to
status results already verified by the trusted host caller. Its report is
external-untrusted evidence, not an authoritative cost account.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

IDENTITY = ("namespace", "run", "group", "attempt")
RUNNING_STATES = frozenset({"running", "in_progress", "queued", "pending"})
SUMMARY_MAX = 4096
DETAIL_MAX = 16384
_READ_REFUSAL_CODES = frozenset({69, 77})


def _status(statuses, publication):
    """Resolve one status; a refused read is unavailable evidence, not a retry."""
    try:
        return statuses.get(publication) if isinstance(statuses, dict) else statuses(publication)
    except OSError:
        return None
    except Exception as exc:
        # This driver is stdlib-only and cannot import the kernel's
        # Denied/LimitExceeded classes; their stable shell exit codes are the
        # cross-process contract. A callable shim raises those same classes.
        # Every other failure propagates instead of being hidden as absent.
        if getattr(type(exc), "code", None) in _READ_REFUSAL_CODES:
            return None
        raise


def observed_costs(page: dict, statuses) -> dict:
    """Join validated dispatch rows to exact receipt-bound status evidence.

    statuses maps publication IDs to status results (None for a failed read),
    or resolves those IDs through a callable shim. One observation per distinct
    receipt, including failed reads; no retry, branch-head fallback or store.
    The adapter attests a fixed namespace, not enumeration completeness.
    Conflicting namespace attestations fail attribution closed. All
    sums/deduplication are page-local; pages are not additive totals.
    """
    resolved = {}
    for request in page["items"]:
        if request["state"] == "not_dispatched":
            continue
        publication = request["publication_id"]
        if publication not in resolved:
            resolved[publication] = _status(statuses, publication)
    statuses = resolved
    namespaces = {row["namespace"] for status in statuses.values() if status is not None
                  for row in status["result"]["checks"] if "namespace" in row}
    namespace_conflict = len(namespaces) > 1
    days = {}
    executions = {}
    for request in page["items"]:
        day = days.setdefault(request["date"], {"date": request["date"], "requests": []})
        not_dispatched = request["state"] == "not_dispatched"
        status = None if not_dispatched else statuses.get(request["publication_id"])
        report = {**request, "status_evidence": None,
                  "status": "not_dispatched" if not_dispatched else "unavailable",
                  "identityless_rows": 0, "unmatched_rows": 0, "matched_rows": 0,
                  "running_rows": 0, "missing_duration_rows": 0,
                  "state_timing_conflicts": 0,
                  "unobserved_run_ids": [run["run_id"] for run in request["runs"]]}
        day["requests"].append(report)
        if status is None:
            continue
        report["status_evidence"] = {key: status[key] for key in ("evidence_id", "evidence")}
        data = status["result"]
        # The caller validates the receipt and retains this binding. The
        # dispatch must name exactly that receipt and SHA too.
        publication = data["publication"]
        if (publication["publication_id"] != request["publication_id"]
                or publication["published_sha"] != request["sha"]):
            report["status"] = "publication_mismatch"
            continue
        report["status"] = "observed" if data["checks"] else "missing_checks"
        run_ids = {run["run_id"] for run in request["runs"]}
        observed = set()
        for row in data["checks"]:
            if not all(key in row for key in IDENTITY):
                report["identityless_rows"] += 1
                continue
            if (namespace_conflict or row["sha"] != request["sha"]
                    or row["run"] not in run_ids):
                report["unmatched_rows"] += 1
                continue
            observed.add(row["run"])
            report["matched_rows"] += 1
            running = row["state"].casefold() in RUNNING_STATES
            if running:
                report["running_rows"] += 1
            if "job_seconds" not in row:
                report["missing_duration_rows"] += 1
            identity = tuple(row[key] for key in IDENTITY)
            execution = executions.setdefault(identity, {"days": set(), "seconds": set(), "shas": set()})
            execution["days"].add(request["date"])
            execution["shas"].add(row["sha"])
            if running:
                # A nonterminal state and a complete interval contradict; do
                # not count its time as a completed execution.
                if "job_seconds" in row:
                    report["state_timing_conflicts"] += 1
                continue
            if "job_seconds" in row:
                execution["seconds"].add(row["job_seconds"])
        report["unobserved_run_ids"] = sorted(run_ids - observed)
    # Same execution with incompatible durations, SHAs or dispatch days is ambiguous,
    # not two executions or an arbitrary latest/earliest duration/day.
    conflicts = 0
    for day in days.values():
        seconds = []
        for execution in executions.values():
            if day["date"] not in execution["days"]:
                continue
            if (len(execution["days"]) != 1 or len(execution["seconds"]) > 1
                    or len(execution["shas"]) > 1):
                conflicts += 1
            elif execution["seconds"]:
                seconds.append(next(iter(execution["seconds"])))
        day.update(observed_wall_seconds=sum(seconds) if seconds else None,
                   timed_executions=len(seconds), coverage="partial")
    timed = [day["observed_wall_seconds"] for day in days.values()
             if day["observed_wall_seconds"] is not None]
    return {**{key: value for key, value in page.items() if key != "items"},
            "days": list(days.values()), "observed_wall_seconds": sum(timed) if timed else None,
            "measure": "runner_wall_seconds", "coverage": "partial",
            "deduplication": "page_local", "namespace_conflict": namespace_conflict,
            "namespace": next(iter(namespaces)) if len(namespaces) == 1 else None,
            "conflicting_execution_day_observations": conflicts}


def main() -> None:
    """Read one request and report one bounded observation on stdout."""
    request = json.load(sys.stdin)
    if (not isinstance(request, dict) or "page" not in request
            or "statuses" not in request):
        raise SystemExit("ci_costs driver needs one object with page and statuses")
    report = observed_costs(request["page"], request["statuses"])
    detail = json.dumps(report, separators=(",", ":"), allow_nan=False)
    if len(detail) > DETAIL_MAX:
        raise SystemExit("ci cost report exceeds the observation detail bound")
    summary = (f"Observed {report['observed_wall_seconds']} runner_wall_seconds "
               f"over {len(report['days'])} day(s)")[:SUMMARY_MAX]
    observation = {
        "driver": "ci_costs",
        "kind": "ci_costs",
        "summary": summary,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    json.dump({"observations": [observation]}, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
