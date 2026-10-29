"""Host-only, read-only aggregate of bounded dispatch evidence; no policy input.

Only fixed categories, counts, UTC timestamps and numeric distributions leave
this module. Internal identities are never serialized, even as anonymous rows.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import heapq
import os
import re
import stat
import time
from collections import Counter, defaultdict

from . import platform
from .dispatch_evidence import MAX_ATTEMPTS, MAX_BYTES
from .errors import LimitExceeded, MizuError
from .fs import loads_finite, safe_read
from .usage import (MAX_CACHE_AFFINITY_RUNS, MAX_RUNS_SCANNED,
                    cache_read_share, prompt_input_tokens)

MAX_SECONDS = 2.0
SPARSE_BELOW = 5
_KEY = re.compile(r"[0-9a-f]{64}\Z")
REASONS = ("invalid_record", "unknown_session", "invalid_interval",
           "unfinished", "unknown_model", "unknown_denominator")
PAIR_REASONS = ("unsequenced", "overlap", "ineligible", "different_model_or_engine", "file_boundary")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def _attempt(raw, file_index):
    row = dict(file=file_index, sequence=None, group=None, engine=None,
               start=None, end=None, share=None, reason="invalid_record")
    if not isinstance(raw, dict):
        return row
    row.update(start=_timestamp(raw.get("started_at")), end=_timestamp(raw.get("ended_at")))
    session = raw.get("session")
    if isinstance(session, dict):
        key, generation = session.get("key"), session.get("generation")
        if isinstance(key, str) and _KEY.fullmatch(key) and type(generation) is int and generation >= 0:
            row["sequence"] = (key, generation)
    models = raw.get("models")
    if (raw.get("models_unknown") is False and raw.get("models_truncated") is False
            and isinstance(models, list) and len(models) == 1 and isinstance(models[0], dict)):
        provider, model = models[0].get("provider"), models[0].get("model")
        if row["sequence"] and _text(provider) and _text(model):
            row["group"] = (*row["sequence"], provider, model)
    engine = raw.get("engine")
    row["engine"] = engine if isinstance(engine, str) else None
    usage = raw.get("usage")
    if isinstance(usage, dict) and usage.get("status") in ("known", "partial"):
        denominator = prompt_input_tokens(row["engine"], usage.get("input_tokens"),
                                         usage.get("cache_read_tokens"), usage.get("cache_write_tokens"))
        row["share"] = cache_read_share(denominator, usage.get("cache_read_tokens"))
    row["reason"] = (
        "unknown_session" if row["sequence"] is None else
        "invalid_interval" if row["start"] is None or row["end"] is None or row["end"] <= row["start"] else
        "unfinished" if raw.get("status") not in ("returned", "raised") else
        "unknown_model" if row["group"] is None else
        "unknown_denominator" if row["share"] is None else None)
    return row


def _distribution(values):
    """Nearest-rank observed quantiles, including singleton bins; no smoothing."""
    values = sorted(values)
    count = len(values)
    return {"count": count, "sparse": count < SPARSE_BELOW,
            "quantiles": {name: values[max(0, (count * percent + 99) // 100 - 1)] if count else None
                          for name, percent in (("p0", 0), ("p25", 25), ("p50", 50), ("p75", 75), ("p100", 100))}}


@contextlib.contextmanager
def _runs(root):
    # Pin the enumeration directory on POSIX. Match safe_read's portable
    # reparse-point refusal otherwise; no portable TOCTOU-pinning claim.
    if platform.HAS_OPENAT and platform.HAS_DIR_FD:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            child = os.open("runs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                with os.scandir(child) as entries:
                    yield entries
            finally:
                os.close(child)
        finally:
            os.close(fd)
    else:
        for path in (root, root / "runs"):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Unsafe directory")
        with os.scandir(root / "runs") as entries:
            yield entries


def summarize(project):
    """One authenticated project's private source, never rows or persisted output.

    Deadline checks surround each bounded operation; a stalled OS read cannot
    be preempted. Reaching a limit conservatively marks coverage unknown.
    """
    deadline = time.monotonic() + MAX_SECONDS
    coverage = dict(candidate_entries=0, run_directories=0, files_attempted=0, files_read=0,
                    unavailable_files=0, malformed_files=0, oversized_files=0,
                    unsafe_entries=0, dropped_attempts=0, attempt_limit_files=0,
                    candidate_limit=False, file_limit=False, timeout=False, scan_unavailable=False)

    def expired():
        if time.monotonic() >= deadline:
            coverage["timeout"] = True
        return coverage["timeout"]

    candidates = []
    try:
        with _runs(project.root) as entries:
            for entry in entries:
                if expired():
                    break
                coverage["candidate_entries"] += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
                        coverage["run_directories"] += 1
                        candidates.append((info.st_mtime_ns, entry.name))
                    else:
                        coverage["unsafe_entries"] += 1
                except OSError:
                    coverage["unsafe_entries"] += 1
                if coverage["candidate_entries"] >= MAX_RUNS_SCANNED:
                    coverage["candidate_limit"] = True
                    break
    except (OSError, ValueError):
        coverage["scan_unavailable"] = True
    coverage["file_limit"] = len(candidates) > MAX_CACHE_AFFINITY_RUNS
    rows = []
    unsafe_files = set()
    for index, (_, name) in enumerate(heapq.nlargest(MAX_CACHE_AFFINITY_RUNS, candidates)):
        if expired():
            break
        coverage["files_attempted"] += 1
        try:
            raw = safe_read(project.root, f"runs/{name}/dispatch-attempts.json", MAX_BYTES)
            if expired():
                break
            record = loads_finite(raw, object_pairs_hook=_unique_object)
            if (not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 1
                    or not isinstance(record.get("attempts"), list)
                    or type(record.get("dropped_attempts")) is not int
                    or not 0 <= record["dropped_attempts"] <= 2**63 - 1):
                raise ValueError("Invalid record")
            coverage["files_read"] += 1
            coverage["dropped_attempts"] += record["dropped_attempts"]
            attempts = record["attempts"]
            if len(attempts) > MAX_ATTEMPTS:
                coverage["attempt_limit_files"] += 1
            if record["dropped_attempts"] or len(attempts) > MAX_ATTEMPTS:
                unsafe_files.add(index)
            for raw_attempt in attempts[:MAX_ATTEMPTS]:
                if expired():
                    unsafe_files.add(index)
                    break
                row = _attempt(raw_attempt, index)
                rows.append(row)
                if row["sequence"] is None:
                    unsafe_files.add(index)
        except (OSError, MizuError) as exc:
            coverage["oversized_files" if isinstance(exc, LimitExceeded) else "unavailable_files"] += 1
        except (ValueError, TypeError, RecursionError):
            coverage["malformed_files"] += 1
    # No incomplete file may supply an adjacency claim. No cross-file pairs:
    # missing files or retained tails could otherwise hide intervening calls.
    sequences, groups = defaultdict(list), defaultdict(list)
    excluded = Counter({reason: 0 for reason in REASONS})
    for row in rows:
        if row["reason"]:
            excluded[row["reason"]] += 1
        if row["sequence"]:
            sequences[row["sequence"]].append(row)
        if row["group"]:
            groups[row["group"]].append(row)
    changes, group_changes = [], defaultdict(list)
    pair_excluded = Counter({reason: 0 for reason in PAIR_REASONS})
    pair_candidates = unpaired = 0
    unknown_sessions = any(r["sequence"] is None for r in rows)
    for sequence in sequences.values():
        if expired():
            break
        pair_candidates += len(sequence) // 2
        unpaired += len(sequence) % 2
        if (unknown_sessions
                or any(r["start"] is None or r["end"] is None or r["end"] <= r["start"] for r in sequence)):
            pair_excluded["unsequenced"] += len(sequence) // 2
            continue
        sequence.sort(key=lambda r: r["start"])
        overlapping = set()
        furthest = 0
        for i in range(1, len(sequence)):
            if sequence[i]["start"] < sequence[furthest]["end"]:
                overlapping.update((furthest, i))
            if sequence[i]["end"] > sequence[furthest]["end"]:
                furthest = i
        for i in range(0, len(sequence) - 1, 2):
            a, b = sequence[i:i + 2]
            reason = ("unsequenced" if a["file"] in unsafe_files or b["file"] in unsafe_files else
                      "overlap" if i in overlapping or i + 1 in overlapping else
                      "file_boundary" if a["file"] != b["file"] else
                      "ineligible" if a["reason"] or b["reason"] else
                      "different_model_or_engine" if (a["group"], a["engine"]) != (b["group"], b["engine"]) else None)
            if reason:
                pair_excluded[reason] += 1
            else:
                change = b["share"] - a["share"]
                changes.append(change)
                group_changes[a["group"]].append(change)
    expired()
    shares = [r["share"] for r in rows if r["reason"] is None]
    bins = [sum(low <= share < high or (high == 1 and share == 1) for share in shares)
            for low, high in ((0, .25), (.25, .5), (.5, .75), (.75, 1))]
    timestamps = [r[k] for r in rows for k in ("start", "end") if r[k] is not None]
    faults = ("unavailable_files", "malformed_files", "oversized_files", "unsafe_entries", "dropped_attempts",
              "attempt_limit_files", "candidate_limit", "file_limit", "timeout", "scan_unavailable")
    coverage["incomplete_attempts"] = sum(excluded.values())
    coverage["history"] = "unknown"
    coverage["status"] = ("incomplete_unknown" if any(coverage[k] for k in faults) or coverage["incomplete_attempts"]
                          else "observed_window_only" if rows else "empty_unknown")
    result = {"schema": 1, "coverage": coverage,
            "limits": {"candidate_entries": MAX_RUNS_SCANNED, "files": MAX_CACHE_AFFINITY_RUNS,
                       "bytes_per_file": MAX_BYTES, "attempts_per_file": MAX_ATTEMPTS, "seconds": MAX_SECONDS},
            "period_utc": {"first": min(timestamps).isoformat() if timestamps else None,
                           "last": max(timestamps).isoformat() if timestamps else None},
            "attempts": {"observed": len(rows), "eligible": len(shares), "excluded": dict(excluded)},
            "pairs": {"candidates": pair_candidates, "valid": len(changes), "excluded": dict(pair_excluded),
                      "unpaired": unpaired, "change_quantiles": _distribution(changes)},
            "cache_read_share": {**_distribution(shares), "bins": [
                {"lower": low, "upper": high, "count": count, "sparse": count < SPARSE_BELOW}
                for (low, high), count in zip(((0, .25), (.25, .5), (.5, .75), (.75, 1)), bins)]},
            "groups": {"distinct": len(groups), "with_pairs": len(group_changes),
                       "attempt_counts": _distribution([len(rs) for rs in groups.values()]),
                       "pair_counts": _distribution([len(group_changes.get(g, [])) for g in groups]),
                       "median_shares": _distribution([_distribution([r["share"] for r in rs if not r["reason"]])["quantiles"]["p50"]
                                                       for rs in groups.values() if any(not r["reason"] for r in rs)]),
                       "median_changes": _distribution([_distribution(cs)["quantiles"]["p50"] for cs in group_changes.values()])},
            "pairing": "Within each session-generation, sort all observed attempts by start; disjoint consecutive pairs (1,2), (3,4). Never skip invalid attempts. Invalid intervals block the sequence; an unknown session blocks all pairs. Dropped/truncated files cannot supply pairs. Reject either endpoint overlapping any interval in its sequence, ineligible endpoints, or different exact provider/model/engine. Never pair across files.",
            "note": "Observed counts only; empty, missing and bounded history is unknown, never zero history. Nearest-rank quantiles; sparse means fewer than 5 observations. Bins are left-closed, right-open except 1. Groups use exact session key, generation, provider and model; only distributions across groups leave this view. No causal inference or scheduler authorization."}
    if expired():
        coverage["status"] = "incomplete_unknown"
    return result
