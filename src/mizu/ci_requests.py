"""Durable CI request attempts at exact published commits.

A CI request asks the provider for one run per workflow at an exact
published commit. Create-only dispatch markers precede adapter calls; immutable
outcomes follow them. An exact-SHA status row cannot resolve a marker. A typed
adapter result may attest ``not_dispatched`` (no spend) or bind provider runs.
An explicit query may reconcile an unresolved dispatch without dispatching again.
Legacy timeout-unknown evidence remains immutable and unresolved until a validated
provider answer is recorded in a digest-bound resolution sidecar.
Malformed evidence fails closed, and no path automatically retries dispatches.

Records live in the project's evidence tree, outside the workspace.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import stat
import uuid
from itertools import islice
from pathlib import Path

from .config import CiRequestEvidence, TOML_INTEGER_MAX
from .errors import Denied, LimitExceeded, MizuError
from .fs import canonical, digest, loads_finite, lock, mkdir, safe_read, write_json
from .process import ADAPTER_DIAGNOSTIC_BYTES_MAX
from .vcs import MAX_CI_REQUEST_ATTEMPT, validate_ci_request, validate_ci_runs

#: Ordinary CI evidence-record read bound; markers and outcomes keep this cap.
RECORD_BYTES = 8192
#: 64 KiB covers the 8 KiB envelope, sixfold JSON escaping, and resolution wrapper.
CI_DIAGNOSTIC_RECORD_BYTES = 65536
#: The diagnostic tail plus its fixed external-untrusted label.
MAX_CI_DIAGNOSTIC_BYTES = ADAPTER_DIAGNOSTIC_BYTES_MAX + 128
UNKNOWN_REASON = "dispatch_outcome_timeout"
ATTEMPT_FILE = re.compile(r"([0-9a-f]{64})\.(?:dispatch|request|resolution)\.json\Z")
ACTIVITY = "evidence/ci-request-activity.json"
#: Single-key sentinel used when ``matching()`` delegates to the shared scan.
_MATCHING_KEY = object()


def request_id(publication_id: str, sha: str, workflows: list[str], attempt: int = 1) -> str:
    """Identify one idempotent attempt for a receipt/SHA/workflow set.

    Attempt one preserves the original identity. Later attempts have distinct
    immutable records; the runtime permits them only after the preceding
    durable no-spend outcome. Workflow order does not change identity.
    """
    if type(attempt) is not int or not 1 <= attempt <= MAX_CI_REQUEST_ATTEMPT:
        raise Denied("Invalid CI request attempt")
    base = digest(canonical({"publication_id": publication_id, "sha": sha,
                             "workflows": sorted(workflows)}))
    return base if attempt == 1 else digest(canonical({"request_id": base, "attempt": attempt}))


class CiRequests:
    """The project's durable CI request records."""

    def __init__(self, root: Path, policy: CiRequestEvidence | None = None):
        self.project_root = Path(root)
        self.root = self.project_root / "evidence" / "ci-requests"
        self.lock = self.project_root / "locks" / "ci-requests.lock"
        self.policy = policy if policy is not None else CiRequestEvidence()

    def _path(self, identity: str, kind: str) -> Path:
        return self.root / f"{identity}.{kind}.json"

    @staticmethod
    def _has_diagnostic_outcome(record: dict, kind: str) -> bool:
        if kind == "request":
            outcome = record
        elif kind == "resolution":
            outcome = record.get("outcome")
        else:
            outcome = None
        return (isinstance(outcome, dict) and outcome.get("outcome") == "not_dispatched"
                and "diagnostic" in outcome)

    def _read(self, identity: str, kind: str) -> dict | None:
        path = self._path(identity, kind)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        maximum = RECORD_BYTES
        if (kind in {"request", "resolution"} and info.st_size > RECORD_BYTES):
            maximum = CI_DIAGNOSTIC_RECORD_BYTES
        raw = safe_read(self.root, path.name, maximum)
        try:
            record = loads_finite(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise Denied(f"Invalid CI request record: {identity}") from exc
        if not isinstance(record, dict):
            raise Denied(f"Invalid CI request record: {identity}")
        if len(raw) > RECORD_BYTES and not self._has_diagnostic_outcome(record, kind):
            raise LimitExceeded(f"CI {kind} record exceeds {RECORD_BYTES} bytes")
        return record

    def outcome(self, identity: str) -> dict | None:
        """Validated effective result; legacy timeout-unknown remains unresolved."""
        marker = self.dispatched(identity)
        if marker is not None and _unresolved_summary(marker, identity) is None:
            raise Denied("Invalid CI dispatch marker")
        record = self._read(identity, "request")
        resolution = self._read(identity, "resolution")
        if record is None:
            if resolution is not None:
                raise Denied("Unbound CI request resolution")
            return None
        if marker is None or _validated_outcome(record, identity, marker) is None:
            raise Denied("Invalid or unbound CI request outcome")
        if record.get("outcome") != "unknown":
            if resolution is not None:
                raise Denied("Unexpected CI request resolution")
            return record
        if resolution is None:
            return None
        if (set(resolution) != {"dispatch_digest", "legacy_outcome_digest", "outcome"}
                or resolution["dispatch_digest"] != digest(canonical(marker))
                or resolution["legacy_outcome_digest"] != digest(canonical(record))
                or not isinstance(resolution["outcome"], dict)):
            raise Denied("Invalid or unbound CI request resolution")
        effective = resolution["outcome"]
        if (effective.get("outcome") == "unknown"
                or _validated_outcome(effective, identity, marker) is None):
            raise Denied("Invalid CI request resolution outcome")
        return effective

    def prior_spend_possible(self, publication_id: str, sha: str,
                             workflows: list[str], attempt: int) -> bool:
        """Conservative risk at dispatch time, not permission to retry.

        Only validated no-spend outcomes for *every* earlier exact attempt
        establish no prior spend. Ignore their historical risk flags: a later
        exact reconciliation can now prove that the whole chain spent nothing.
        Missing, unreadable or over-budget history cannot prove that. Read at
        most policy.record_limit attempts, without scanning unrelated evidence.
        """
        if attempt - 1 > self.policy.record_limit:
            return True
        for prior in range(1, attempt):
            identity = request_id(publication_id, sha, workflows, prior)
            try:
                outcome = self.outcome(identity)
            except (Denied, LimitExceeded, OSError):
                return True
            if outcome is None or outcome.get("outcome") != "not_dispatched":
                return True
        return False

    def resolve(self, identity: str, record: dict) -> None:
        """Retain a provider answer without replacing legacy timeout evidence."""
        if self.outcome(identity) is not None:
            raise Denied("CI request is already resolved")
        marker = self.dispatched(identity)
        if (marker is None or record.get("outcome") == "unknown"
                or _validated_outcome(record, identity, marker) is None):
            raise Denied("Invalid CI request resolution outcome")
        legacy = self._read(identity, "request")
        if legacy is None:
            self.record_outcome(identity, record)
        else:
            self._write(identity, "resolution", {
                "dispatch_digest": digest(canonical(marker)),
                "legacy_outcome_digest": digest(canonical(legacy)), "outcome": record})

    def dispatched(self, identity: str) -> dict | None:
        """The marker written before the adapter call, if an attempt began."""
        return self._read(identity, "dispatch")

    def mark_dispatch(self, identity: str, record: dict) -> None:
        """Write the point of no return before the provider is called."""
        self._write(identity, "dispatch", record)

    def record_outcome(self, identity: str, record: dict) -> None:
        """Write an immutable adapter outcome for a marked attempt."""
        self._write(identity, "request", record)

    def _write(self, identity: str, kind: str, record: dict) -> None:
        maximum = (CI_DIAGNOSTIC_RECORD_BYTES
                   if self._has_diagnostic_outcome(record, kind) else RECORD_BYTES)
        if len(canonical(record)) > maximum:
            raise LimitExceeded(f"CI {kind} record exceeds {maximum} bytes")
        with lock(self.lock):
            mkdir(self.root)
            # Revoke prior quiet observations durably before any evidence
            # change (in particular before a dispatch reaches the adapter).
            write_json(self.project_root / ACTIVITY, uuid.uuid4().hex)
            write_json(self._path(identity, kind), record, exclusive=True)

    def activity(self) -> str:
        """Cheap local identity, not proof the ledger was read or is quiet."""
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            return digest(b"")
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Denied("Invalid CI request evidence directory")
        try:
            generation = loads_finite(safe_read(self.project_root, ACTIVITY, 64))
        except FileNotFoundError:
            generation = None  # Legacy records predate the invalidation token.
        else:
            if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise Denied("Invalid CI request activity token")
        return digest(canonical([generation, info.st_dev, info.st_ino,
                                 info.st_mtime_ns, info.st_ctime_ns]))

    def revoke_quiet_activity(self) -> bool:
        """Rotate the durable quiet-observation token without recording a request."""
        try:
            before = self.activity()
            with lock(self.lock, blocking=False):
                mkdir(self.root)
                write_json(self.project_root / ACTIVITY, uuid.uuid4().hex)
                return self.activity() != before
        except (MizuError, OSError, ValueError, TypeError):
            return False

    def quiet_activity(self) -> str | None:
        """Certify one complete, quiescent ledger generation under the writer lock."""
        with lock(self.lock, blocking=False):
            activity = self.activity()
            rows, incomplete = self._rows(strict=True)
            if (not incomplete and all(row["state"] in ("resolved", "not_dispatched") for row in rows)
                    and activity == self.activity()):
                return activity
        return None

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
        whether the request is ``resolved``, ``not_dispatched`` or
        ``unresolved``. Legacy timeout-unknown is unresolved until a validated
        resolution exists. Only no-spend permits a later explicit attempt.
        ``possibly_duplicate_spend`` records whether earlier exact attempts
        may have spent runs when this attempt began; later reconciliation
        never rewrites that historical flag. An unreadable result
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

    def matching_many(self, queries) -> dict:
        """Validated request identities for several publication windows in one scan.

        Schema: ``queries`` maps opaque keys to ``(publication_ids, not_before)``
        pairs; the result maps the same keys to
        ``{"items": [request_id, ...], "incomplete": bool}`` pages, newest first.
        Each page holds the identities of requests whose ``publication_id`` is
        in that query's set and whose dispatch day is on or after its UTC
        ``not_before`` date. Bounds: one shared newest-first scan of the
        validated rows stops at the earliest window across all queries, so a
        long older history is read once and cannot hide any query's request;
        the shared reader's configured name and candidate limits apply.
        Trust: locally recorded evidence; a dispatch that cannot be validated
        is not an identity and is not returned. Failure: unreadable evidence,
        an unreadable dispatch marker, a stopped scan or dropped records make
        every page incomplete; invalid dates or filters raise ``Denied``.
        """
        prepared = {}
        for key, query in queries.items():
            try:
                publication_ids, not_before = query
            except (TypeError, ValueError) as exc:
                raise Denied("Invalid CI request query") from exc
            if isinstance(not_before, dt.datetime):
                if not_before.tzinfo is None:
                    raise Denied("Invalid CI request publication window")
                cutoff = not_before.astimezone(dt.timezone.utc).date()
            elif isinstance(not_before, dt.date):
                cutoff = not_before
            else:
                raise Denied("Invalid CI request publication window")
            try:
                wanted = frozenset(publication_ids)
            except TypeError as exc:
                raise Denied("Invalid CI request publication filter") from exc
            if any(not isinstance(value, str) or not value for value in wanted):
                raise Denied("Invalid CI request publication filter")
            prepared[key] = (wanted, cutoff.isoformat())
        if not prepared:
            return {}
        earliest = min(cutoff for _, cutoff in prepared.values())
        dropped = []
        try:
            rows, stopped = self._rows(dropped=dropped)
        except Exception:
            return {key: {"items": [], "incomplete": True} for key in prepared}
        items = {key: [] for key in prepared}
        for row in reversed(rows):
            date = row["date"]
            if date < earliest:
                break
            for key, (wanted, cutoff) in prepared.items():
                if date >= cutoff and row["publication_id"] in wanted:
                    items[key].append(row["request_id"])
        incomplete = bool(stopped or dropped)
        return {key: {"items": page, "incomplete": incomplete}
                for key, page in items.items()}

    def matching(self, publication_ids, *, not_before: dt.date) -> dict:
        """Validated request identities for exact publication IDs, newest first.

        Schema: ``{"items": [request_id, ...], "incomplete": bool}``. Items are
        the identities of requests whose ``publication_id`` is in
        ``publication_ids`` and whose dispatch day is on or after the UTC
        ``not_before`` date. This reads the same validated rows as ``daily()``,
        walks them newest first and stops at the first row older than the
        publication window, so a long history of unrelated earlier requests
        cannot hide or be mistaken for a head's request. Bounds: the shared
        reader's configured name and candidate limits apply; no state, writes
        or new policy are added, and ``daily()`` keeps its own page and order.
        Trust: locally recorded evidence; a dispatch that cannot be validated
        is not an identity and is not returned. Failure: unreadable evidence,
        an unreadable dispatch marker, or a stopped scan returns no items with
        ``incomplete`` true, because any of them could hide a match; invalid
        arguments raise ``Denied``.
        """
        return self.matching_many({_MATCHING_KEY: (publication_ids, not_before)})[_MATCHING_KEY]

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

    def _rows(self, *, strict=False, dropped=None) -> tuple[list, bool]:
        """Bounded validation; strict consumers cannot skip ambiguous records.

        A caller that must fail closed can pass ``dropped``; identities of
        records whose dispatch marker cannot be read are appended to it.
        """
        maximum = self.policy.name_processing_limit
        try:
            if strict:
                with os.scandir(self.root) as entries:
                    names = [entry.name for entry in islice(entries, maximum + 1)]
                if len(names) > maximum:
                    return [], True
            else:
                names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            return [], False
        except NotADirectoryError:
            # The evidence root exists but is not a directory: this is an
            # unreadable source, not proof that no request was ever made.
            return [], True
        except OSError as exc:
            raise Denied("CI request evidence is unavailable") from exc
        rows = []
        seen = set()
        scan_stopped = len(names) > maximum
        for name in names[:maximum]:
            match = ATTEMPT_FILE.fullmatch(name)
            if match is None:
                if strict:
                    return [], True
                continue
            if match.group(1) in seen or (not strict and name.endswith(".resolution.json")):
                continue
            seen.add(match.group(1))
            if len(seen) > self.policy.record_limit:
                scan_stopped = True
                break
            identity = match.group(1)
            try:
                record = self.outcome(identity)
            except (Denied, LimitExceeded, OSError):
                # Invalid outcomes and resolutions never hide valid markers.
                record = None
            try:
                marker = self._read(identity, "dispatch")
            except (Denied, LimitExceeded, OSError):
                marker = None
            marker_row = None if marker is None else _unresolved_summary(marker, identity)
            if marker_row is None:
                if strict:
                    return [], True
                # Without a valid dispatch marker there is no evidence that a
                # CI run may have been spent, and no instant to date the
                # request by, so the request is not listed at all. A caller
                # answering an exact-head question must still know that an
                # attempt may exist here.
                if dropped is not None:
                    dropped.append(identity)
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
        fields |= {"reason_code", "runs", "diagnostic"}
        if (record.get("reason_code") not in ("pre_dispatch_refusal", "provider_rejected")
                or record.get("runs") != []):
            return None
        if "diagnostic" in record:
            try:
                diagnostic_bytes = len(record["diagnostic"].encode("utf-8"))
            except (AttributeError, UnicodeEncodeError):
                return None
            if (not isinstance(record["diagnostic"], str)
                    or diagnostic_bytes > MAX_CI_DIAGNOSTIC_BYTES):
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
    try:
        date = moment.astimezone(dt.timezone.utc).date().isoformat()
    except OverflowError:
        # A timezone-aware instant at the edge of the representable range can
        # parse yet have no representable UTC date; an undatable marker is
        # invalid and must not deny the valid records beside it.
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
    return {"date": date,
            "request_id": identity, "publication_id": record["publication_id"],
            "sha": record["sha"], "workflows": list(workflows), "runs": [],
            "covered": items, "attempt": attempt, "state": "unresolved",
            "reason_code": None, "possibly_duplicate_spend": duplicate}
