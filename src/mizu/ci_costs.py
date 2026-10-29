"""Read-only, page-local observed runner wall time; never billing or a total."""
from __future__ import annotations

from .errors import Denied, LimitExceeded

IDENTITY = ("repository", "sha", "workflow_run_id", "job_id", "run_attempt")
RUNNING_STATES = frozenset({"running", "in_progress", "queued", "pending"})


def observed_costs(page: dict, status_read) -> dict:
    """Join validated dispatch rows to exact receipt-bound status evidence.

    status_read is the ordinary status(publication_id) path, including its
    receipt validation and evidence write. One observation per distinct
    receipt, including failed reads; no retry, branch-head fallback or store.
    The adapter attests a fixed repository namespace, not enumeration
    completeness. Conflicting repository attestations fail attribution closed.
    All sums/deduplication are page-local; pages are not additive totals.
    """
    statuses = {}
    for request in page["items"]:
        publication = request["publication_id"]
        if publication not in statuses:
            try:
                statuses[publication] = status_read(publication)
            except (Denied, LimitExceeded, OSError):
                statuses[publication] = None
    repositories = {row["repository"] for status in statuses.values() if status is not None
                    for row in status["result"]["checks"] if "repository" in row}
    repository_conflict = len(repositories) > 1
    days = {}
    executions = {}
    for request in page["items"]:
        day = days.setdefault(request["date"], {"date": request["date"], "requests": []})
        status = statuses[request["publication_id"]]
        report = {**request, "status_evidence": None, "status": "unavailable",
                  "identityless_rows": 0, "unmatched_rows": 0, "matched_rows": 0,
                  "running_rows": 0, "missing_duration_rows": 0,
                  "state_timing_conflicts": 0,
                  "unobserved_workflow_run_ids": [run["run_id"] for run in request["runs"]]}
        day["requests"].append(report)
        if status is None:
            continue
        report["status_evidence"] = {key: status[key] for key in ("evidence_id", "evidence")}
        data = status["result"]
        # The ordinary status read validates the receipt and retains this
        # binding. The dispatch must name exactly that receipt and SHA too.
        publication = data["publication"]
        if (publication["publication_id"] != request["publication_id"]
                or publication["published_sha"] != request["sha"]):
            report["status"] = "publication_mismatch"
            continue
        report["status"] = "observed" if data["checks"] else "missing_checks"
        run_ids = {run["run_id"] for run in request["runs"]}
        observed = set()
        for row in data["checks"]:
            if "workflow_run_id" not in row:
                report["identityless_rows"] += 1
                continue
            if (repository_conflict or row["sha"] != request["sha"]
                    or row["workflow_run_id"] not in run_ids):
                report["unmatched_rows"] += 1
                continue
            observed.add(row["workflow_run_id"])
            report["matched_rows"] += 1
            running = row["state"].casefold() in RUNNING_STATES
            if running:
                report["running_rows"] += 1
            if "job_seconds" not in row:
                report["missing_duration_rows"] += 1
            identity = tuple(row[key] for key in IDENTITY)
            execution = executions.setdefault(identity, {"days": set(), "seconds": set()})
            execution["days"].add(request["date"])
            if running:
                # A nonterminal state and a complete interval contradict; do
                # not count its time as a completed execution.
                if "job_seconds" in row:
                    report["state_timing_conflicts"] += 1
                continue
            if "job_seconds" in row:
                execution["seconds"].add(row["job_seconds"])
        report["unobserved_workflow_run_ids"] = sorted(run_ids - observed)
    # Same execution with incompatible durations or dispatch days is ambiguous,
    # not two executions or an arbitrary latest/earliest duration/day.
    conflicts = 0
    for day in days.values():
        seconds = []
        for execution in executions.values():
            if day["date"] not in execution["days"]:
                continue
            if len(execution["days"]) != 1 or len(execution["seconds"]) > 1:
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
            "deduplication": "page_local", "repository_conflict": repository_conflict,
            "repository": next(iter(repositories)) if len(repositories) == 1 else None,
            "conflicting_execution_day_observations": conflicts}
