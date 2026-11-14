"""Durable CI request attempts at exact published commits.

A CI request asks the provider for one run per workflow at an exact
published commit. Create-only dispatch markers precede adapter calls; immutable
outcomes follow them. An exact-SHA status row cannot resolve a marker. A typed
adapter result may attest ``not_dispatched`` (no spend) or bind provider runs.
On an explicit request, an absent outcome may become terminal ``unknown`` after
the configured grace period. Unknown retains uncertainty and all evidence; it
permits an explicitly numbered later attempt marked as possibly duplicate spend.
Malformed evidence fails closed, and no path automatically retries dispatches.

Records live in the project's evidence tree, outside the workspace.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from .config import CiRequestEvidence, TOML_INTEGER_MAX
from .errors import Denied, LimitExceeded
from .fs import canonical, digest, loads_finite, mkdir, safe_read, sync_dir, write_json
from .vcs import MAX_CI_REQUEST_ATTEMPT, validate_ci_request, validate_ci_runs

#: Bounded record size (8 KiB), like the other evidence records.
RECORD_BYTES = 8192
REQUEST_FILE = re.compile(r"([0-9a-f]{64})\.request\.json\Z")
UNKNOWN_REASON = "dispatch_outcome_timeout"
DISPATCH_FILE = re.compile(r"([0-9a-f]{64})\.dispatch\.json\Z")


def request_id(publication_id: str, sha: str, workflows: list[str], attempt: int = 1) -> str:
    """Identify one idempotent attempt for a receipt/SHA/workflow set.

    Attempt one preserves the original identity. Later attempts have distinct
    immutable records; the runtime permits them only after the preceding
    terminal no-spend or unknown outcome. Workflow order does not change identity.
    """
    if type(attempt) is not int or not 1 <= attempt <= MAX_CI_REQUEST_ATTEMPT:
        raise Denied("Invalid CI request attempt")
    base = digest(canonical({"publication_id": publication_id, "sha": sha,
                             "workflows": sorted(workflows)}))
    return base if attempt == 1 else digest(canonical({"request_id": base, "attempt": attempt}))


class CiRequests:
    """The project's durable CI request records."""

    def __init__(self, root: Path, policy: CiRequestEvidence | None = None):
        self.root = Path(root) / "evidence" / "ci-requests"
        self.policy = policy if policy is not None else CiRequestEvidence()

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
        """The recorded outcome, after validating it against its dispatch."""
        record = self._read(identity, "request")
        if record is None:
            return None
        marker = self._read(identity, "dispatch")
        if marker is None or _validated_outcome(record, identity, marker) is None:
            raise Denied("Invalid or unbound CI request outcome")
        return record

    def settle_unknown(self, identity: str, observed_at: str) -> dict | None:
        """Lazily settle an expired, valid marker on an explicit request only.

        Existing outcomes (including corrupt ones) are never replaced. The
        local policy outcome records its bound and exact marker digest, not a
        provider result. Read-side listings never invoke this method.
        """
        recorded = self.outcome(identity)
        if recorded is not None:
            return recorded
        marker = self.dispatched(identity)
        if marker is None:
            return None
        if _unresolved_summary(marker, identity) is None:
            raise Denied("Invalid CI dispatch marker")
        observed = dt.datetime.fromisoformat(observed_at)
        if observed.tzinfo is None:
            raise Denied("CI settlement requires an aware timestamp")
        age = ((observed - dt.datetime.fromisoformat(marker["dispatched_at"]))
               // dt.timedelta(seconds=1))
        if age < self.policy.unknown_after_seconds:
            return None
        record = {key: value for key, value in marker.items() if key != "dispatched_at"}
        record.update(outcome="unknown", reason_code=UNKNOWN_REASON, runs=[],
                      requested_at=observed_at, dispatch_digest=digest(canonical(marker)),
                      unknown_after_seconds=self.policy.unknown_after_seconds)
        self.record_outcome(identity, record)
        return record

    def dispatched(self, identity: str) -> dict | None:
        """The marker written before the adapter call, if an attempt began."""
        return self._read(identity, "dispatch")

    def mark_dispatch(self, identity: str, record: dict) -> None:
        """Write the point of no return before the provider is called."""
        self._write(identity, "dispatch", record)

    def record_outcome(self, identity: str, record: dict) -> None:
        """Write an immutable adapter or local policy outcome for a marked attempt."""
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
        themselves. A request is dated by its dispatch: the day it was made
        is the day the adapter call began, and a later answer never moves it,
        so the day comes from the marker in every state. Each item
        names the request, its workflows, the provider runs it produced and
        whether the request is ``resolved``, ``not_dispatched``, ``unknown``
        or ``unresolved``. Terminal unknown may have spent a CI run; a later
        explicit attempt is flagged ``possibly_duplicate_spend``. No-spend
        and unknown both permit a later explicit attempt. An unreadable result
        remains unresolved, not a reason to hide the request. The rows
        are chronological and the page holds at most ``limit`` requests.
        Bounds: at most 100 attempts per page; policy bounds post-listing
        name processing and candidate-record validation (defaults 10000/1000).
        os.listdir still materializes and sorts all names before these bounds.
        Trust: locally recorded evidence; an unreadable, oversized or
        invalid outcome record is never returned unvalidated and it never
        denies the valid records beside it, and a dispatch whose marker
        cannot be validated is not listed at all, because it cannot establish
        dispatch identity. Outcome identity and provider runs are checked
        against the marker and the VCS request schema. Failure: ``Denied`` on an invalid limit; a missing
        directory lists nothing.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Denied("Invalid CI request listing limit")
        rows, scan_stopped = self._rows()
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
                                  "runs", "covered", "state", "attempt", "reason_code",
                                  "possibly_duplicate_spend")})
        return {"days": days, "total": len(rows), "truncated": len(rows) > len(page),
                "scan_stopped": scan_stopped}

    def page(self, *, limit: int = 8, offset: int = 0) -> dict:
        """Read at most eight validated attempts for the derived cost view.

        Same scan, validation and chronological order as daily(); pagination
        is positional, not a snapshot cursor. No provider calls or writes.
        """
        if (type(limit) is not int or not 1 <= limit <= 8
                or type(offset) is not int or not 0 <= offset < self.policy.record_limit):
            raise Denied("Invalid CI cost page")
        rows, stopped = self._rows()
        page = rows[offset:offset + limit]
        end = offset + len(page)
        return {"items": page, "total": len(rows), "offset": offset, "limit": limit,
                "next_offset": end if end < len(rows) else None,
                "truncated": offset > 0 or end < len(rows), "scan_stopped": stopped}

    def _rows(self) -> tuple[list, bool]:
        """One bounded validated scan shared by the count and cost views."""
        try:
            names = sorted(os.listdir(self.root))
        except (FileNotFoundError, NotADirectoryError):
            return [], False
        except OSError as exc:
            raise Denied("CI request evidence is unavailable") from exc
        rows = []
        seen = set()
        scan_stopped = len(names) > self.policy.name_processing_limit
        for name in names[:self.policy.name_processing_limit]:
            match = REQUEST_FILE.fullmatch(name) or DISPATCH_FILE.fullmatch(name)
            if match is None or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            if len(seen) > self.policy.record_limit:
                scan_stopped = True
                break
            identity = match.group(1)
            try:
                record = self._read(identity, "request")
            except (Denied, LimitExceeded, OSError):
                # An unreadable, oversized or invalid outcome is never listed
                # unvalidated, and it never denies the records beside it. It
                # is also no reason to hide the request: the dispatch marker
                # below still proves an adapter attempt began and may have
                # reached the provider.
                record = None
            try:
                marker = self._read(identity, "dispatch")
            except (Denied, LimitExceeded, OSError):
                marker = None
            marker_row = None if marker is None else _unresolved_summary(marker, identity)
            if marker_row is None:
                # Without a valid dispatch marker there is no evidence that a
                # CI run may have been spent, and no instant to date the
                # request by, so the request is not listed at all.
                continue
            if record is None:
                # No usable outcome: the adapter attempt began and its result
                # is unknown, so it may have spent a CI run. The marker names
                # the attempt but does not prove provider contact.
                rows.append(marker_row)
                continue
            row = _validated_outcome(record, identity, marker, marker_row)
            if row is None:
                # A readable outcome that cannot be validated is an unknown
                # outcome, not an absent request: the dispatch marker already
                # in hand proves the adapter attempt began and may have spent
                # a run, so it is listed as unresolved rather than hidden.
                rows.append(marker_row)
                continue
            rows.append(row)
        rows.sort(key=lambda row: (row["date"], row["request_id"]))
        return rows, scan_stopped


def _validated_outcome(record: dict, identity: str, marker: dict,
                        marker_row: dict | None = None) -> dict | None:
    """Validate an outcome once against the identity and its dispatch marker."""
    if marker_row is None:
        marker_row = _unresolved_summary(marker, identity)
    if (marker_row is None or record.get("covered", []) != marker.get("covered", [])
            or record.get("attempt", 1) != marker_row["attempt"]
            or record.get("possibly_duplicate_spend", False) != marker_row["possibly_duplicate_spend"]):
        return None
    if record.get("outcome") == "unknown":
        bound = record.get("unknown_after_seconds")
        if (type(bound) is not int or not 1 <= bound <= TOML_INTEGER_MAX
                or record.get("dispatch_digest") != digest(canonical(marker))):
            return None
        try:
            observed = dt.datetime.fromisoformat(record["requested_at"])
            dispatched = dt.datetime.fromisoformat(marker["dispatched_at"])
            if (observed - dispatched) // dt.timedelta(seconds=1) < bound:
                return None
        except (KeyError, TypeError, ValueError):
            return None
    return _summary(record, identity, marker_row)


def _summary(record: dict, identity: str, marker: dict) -> dict | None:
    """Validate a run-bound, no-spend or local unknown outcome."""
    common = {"request_id", "publication_id", "sha", "workflows", "requested_at"}
    outcome = record.get("outcome")
    not_dispatched = outcome == "not_dispatched"
    unknown = outcome == "unknown"
    fields = common | {"attempt", "covered", "outcome", "possibly_duplicate_spend"}
    if unknown:
        fields |= {"reason_code", "runs", "dispatch_digest", "unknown_after_seconds"}
        if record.get("reason_code") != UNKNOWN_REASON or record.get("runs") != []:
            return None
        runs = []
    elif not_dispatched:
        fields |= {"reason_code", "runs"}
        if (record.get("reason_code") not in ("pre_dispatch_refusal", "provider_rejected")
                or record.get("runs") != []):
            return None
        runs = []
    else:
        fields |= {"runs"}
        if outcome not in (None, "dispatched"):
            return None
        try:
            runs = validate_ci_runs(record.get("runs"), marker["sha"], marker["workflows"])
        except Denied:
            return None
    if (not common <= set(record) or set(record) - fields
            or record["request_id"] != identity
            or type(record.get("attempt", 1)) is not int
            or record.get("attempt", 1) != marker.get("attempt", 1)
            or type(record.get("possibly_duplicate_spend", False)) is not bool):
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
    workflows = record["workflows"]
    if (record["publication_id"] != marker["publication_id"]
            or record["sha"] != marker["sha"]):
        return None
    try:
        validate_ci_request(record["sha"], workflows)
    except Denied:
        return None
    if sorted(workflows) != sorted(marker["workflows"]):
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
    return {"date": marker["date"], "request_id": identity,
            "publication_id": record["publication_id"], "sha": record["sha"],
            "workflows": list(workflows), "runs": runs, "covered": items,
            "attempt": marker.get("attempt", 1),
            "state": "unknown" if unknown else "not_dispatched" if not_dispatched else "resolved",
            "possibly_duplicate_spend": marker.get("possibly_duplicate_spend", False),
            "reason_code": record.get("reason_code")}


def _unresolved_summary(record: dict, identity: str) -> dict | None:
    """One dispatch marker without an outcome, as a day row, or None."""
    fields = {"request_id", "publication_id", "sha", "workflows", "dispatched_at"}
    if (not fields <= set(record)
            or set(record) - (fields | {"covered", "attempt", "possibly_duplicate_spend"})):
        return None
    if record["request_id"] != identity:
        return None
    attempt = record.get("attempt", 1)
    if type(attempt) is not int or not 1 <= attempt <= MAX_CI_REQUEST_ATTEMPT:
        return None
    duplicate = record.get("possibly_duplicate_spend", False)
    if type(duplicate) is not bool or (duplicate and attempt == 1):
        return None
    dispatched = record["dispatched_at"]
    if not isinstance(dispatched, str) or len(dispatched) > 64:
        return None
    try:
        moment = dt.datetime.fromisoformat(dispatched)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    if (not isinstance(record["publication_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["publication_id"])):
        return None
    workflows = record["workflows"]
    try:
        validate_ci_request(record["sha"], workflows)
        expected = request_id(record["publication_id"], record["sha"], workflows, attempt)
    except Denied:
        return None
    if expected != identity:
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
            "sha": record["sha"], "workflows": list(workflows), "runs": [],
            "covered": items, "attempt": attempt, "state": "unresolved",
            "reason_code": None, "possibly_duplicate_spend": duplicate}
