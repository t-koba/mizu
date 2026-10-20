"""Durable CI request records; one request per published commit.

A CI request asks the provider for one run per workflow at an exact
published commit. The record is create-only: a dispatch marker is written
before the provider is called and the outcome follows it. An unresolved
dispatch is never retried blindly, because a second request would spend a
second run for the same commit; the caller re-observes the CI status
instead.

Records live in the project's evidence tree, outside the workspace.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import canonical, digest, loads_finite, mkdir, safe_read, sync_dir, write_json

#: Bounded record size (8 KiB), like the other evidence records.
RECORD_BYTES = 8192
#: Bounded scan of the recorded CI requests.
REQUEST_SCAN_LIMIT = 10000
REQUEST_RECORD_LIMIT = 1000
REQUEST_FILE = re.compile(r"([0-9a-f]{64})\.request\.json\Z")


def request_id(publication_id: str, sha: str, workflows: list[str]) -> str:
    """The stable identity of one request: the receipt, the commit, the workflows.

    The same request always names the same record, so a retry replays the
    stored outcome instead of spending a second run. The workflows are sorted:
    the same set of workflows in another order is the same request, not a
    second run.
    """
    return digest(canonical({"publication_id": publication_id, "sha": sha,
                             "workflows": sorted(workflows)}))


class CiRequests:
    """The project's durable CI request records."""

    def __init__(self, root: Path):
        self.root = Path(root) / "evidence" / "ci-requests"

    def _path(self, identity: str, kind: str) -> Path:
        return self.root / f"{identity}.{kind}.json"

    def _read(self, identity: str, kind: str) -> dict | None:
        path = self._path(identity, kind)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raw = safe_read(self.root, path.name, RECORD_BYTES)
        try:
            record = loads_finite(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise Denied(f"Invalid CI request record: {identity}") from exc
        if not isinstance(record, dict):
            raise Denied(f"Invalid CI request record: {identity}")
        return record

    def outcome(self, identity: str) -> dict | None:
        """The recorded outcome of a request, when it is known."""
        return self._read(identity, "request")

    def dispatched(self, identity: str) -> dict | None:
        """The dispatch marker of a request, when it was sent to the provider."""
        return self._read(identity, "dispatch")

    def mark_dispatch(self, identity: str, record: dict) -> None:
        """Write the point of no return before the provider is called."""
        self._write(identity, "dispatch", record)

    def record_outcome(self, identity: str, record: dict) -> None:
        """Write the outcome of a dispatched request."""
        self._write(identity, "request", record)

    def _write(self, identity: str, kind: str, record: dict) -> None:
        mkdir(self.root)
        write_json(self._path(identity, kind), record, exclusive=True)
        sync_dir(self.root)

    def daily(self, *, limit: int = 100) -> dict:
        """The recorded CI requests of the project, grouped by UTC day.

        Schema: ``{"days": [{"date": "YYYY-MM-DD", "requests": int,
        "covered_items": int, "items": [...]}], "total": int,
        "truncated": bool, "scan_stopped": bool}``. A day row counts the
        requests made on that UTC day and names the work items they covered,
        so the CI a unit spent is visible per day next to the requests
        themselves. The rows are chronological and the page holds at most
        ``limit`` requests.
        Bounds: at most 100 requests per page, at most 10000 directory
        entries read and at most 1000 candidate records validated.
        Trust: locally recorded evidence; an unreadable, oversized or
        invalid record is skipped rather than returned unvalidated, and it
        never denies the valid records beside it. Failure: ``Denied`` on an
        invalid limit; a missing directory lists nothing.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Denied("Invalid CI request listing limit")
        try:
            names = sorted(os.listdir(self.root))
        except (FileNotFoundError, NotADirectoryError):
            return {"days": [], "total": 0, "truncated": False, "scan_stopped": False}
        except OSError as exc:
            raise Denied("CI request evidence is unavailable") from exc
        rows = []
        seen = set()
        scan_stopped = len(names) > REQUEST_SCAN_LIMIT
        for name in names[:REQUEST_SCAN_LIMIT]:
            match = REQUEST_FILE.fullmatch(name)
            if match is None or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            if len(seen) > REQUEST_RECORD_LIMIT:
                scan_stopped = True
                break
            try:
                record = self._read(match.group(1), "request")
            except (Denied, LimitExceeded, OSError):
                # An unreadable, oversized or invalid record is never listed
                # unvalidated, and it never denies the records beside it.
                continue
            if record is None:
                continue
            row = _summary(record, match.group(1))
            if row is None:
                continue
            rows.append(row)
        rows.sort(key=lambda row: (row["date"], row["request_id"]))
        page = rows[:limit]
        days = []
        for row in page:
            if days and days[-1]["date"] == row["date"]:
                day = days[-1]
            else:
                day = {"date": row["date"], "requests": 0, "covered_items": 0, "items": []}
                days.append(day)
            day["requests"] += 1
            day["covered_items"] += len(row["covered"])
            day["items"].append({key: row[key] for key in
                                 ("request_id", "publication_id", "sha", "workflows",
                                  "runs", "covered")})
        return {"days": days, "total": len(rows), "truncated": len(rows) > len(page),
                "scan_stopped": scan_stopped}


def _summary(record: dict, identity: str) -> dict | None:
    """One request record as a day row, or None when it cannot be trusted."""
    fields = {"request_id", "publication_id", "sha", "workflows", "runs", "requested_at"}
    if not fields <= set(record) or set(record) - (fields | {"covered"}):
        return None
    if record["request_id"] != identity:
        return None
    requested = record["requested_at"]
    if not isinstance(requested, str) or len(requested) > 64:
        return None
    try:
        moment = dt.datetime.fromisoformat(requested)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    if not isinstance(record["publication_id"], str) or len(record["publication_id"]) != 64:
        return None
    if not isinstance(record["sha"], str) or not 40 <= len(record["sha"]) <= 64:
        return None
    workflows = record["workflows"]
    if (not isinstance(workflows, list) or len(workflows) > 8
            or any(not isinstance(name, str) or not name or len(name) > 128
                   for name in workflows)):
        return None
    runs = record["runs"]
    if not isinstance(runs, list) or len(runs) > 8:
        return None
    covered = record.get("covered", [])
    if not isinstance(covered, list) or len(covered) > 16:
        return None
    items = []
    for entry in covered:
        if (not isinstance(entry, dict)
                or set(entry) - {"id", "rev", "kind", "publication_id", "branch",
                                 "sha", "checks"}
                or not isinstance(entry.get("id"), str) or not entry["id"]
                or type(entry.get("rev")) is not int or entry["rev"] < 1):
            return None
        items.append({"id": entry["id"], "rev": entry["rev"]})
    return {"date": moment.astimezone(dt.timezone.utc).date().isoformat(),
            "request_id": identity, "publication_id": record["publication_id"],
            "sha": record["sha"], "workflows": list(workflows), "runs": runs,
            "covered": items}
