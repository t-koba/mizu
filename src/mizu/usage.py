"""Bounded usage facts from run evidence, never pricing or a monetary limit.

Logical model requests, turns and queries are distinct units.
Completed/interrupted normal and child consultation records are canonical;
parent consultation records refer to child runs. Unknown usage is explicit.
Records are never rewritten. Corrupt individual records increment skipped.
"""
from __future__ import annotations

import datetime as dt
import heapq

from .fs import read_json

MAX_RUNS_SCANNED = 5000
MAX_RECENT_ENTRIES = 200
MAX_UNKNOWN_KEYS = 20
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "other_tokens")
_KEYS = {
    "pi": {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens"},
    "codex": {"input_tokens": "input_tokens", "output_tokens": "output_tokens", "cached_input_tokens": "cache_read_tokens"},
    "claude": {"inputTokens": "input_tokens", "outputTokens": "output_tokens", "cacheReadInputTokens": "cache_read_tokens", "cacheCreationInputTokens": "cache_write_tokens"},
}
_CANONICAL = {name:name for name in _FIELDS}
_METADATA = {"model", "cost", "cost_estimate_usd", "reasoning", "reasoning_output_tokens", "cacheWrite1h", "totalTokens"}


def normalize(entry, engine: str = "unknown") -> dict:
    found = dict.fromkeys(_FIELDS, 0)
    if not isinstance(entry, dict):
        return {**found, "unrecognized_keys": [], "unknown_shape": True}
    fields = _KEYS.get(engine, _CANONICAL)
    recognized = invalid = False
    unknown = []
    for key, value in entry.items():
        field = fields.get(key)
        if field:
            if type(value) is not int or not 0 <= value <= 2**63-1:
                invalid = True
                unknown.append(str(key))
            else:
                found[field] = value
                recognized = True
        elif key not in _METADATA:
            unknown.append(str(key))
    return {**found, "unrecognized_keys": sorted(set(unknown))[:MAX_UNKNOWN_KEYS],
            "unknown_shape": invalid or not recognized}


def record_time(value) -> float:
    """Aware ISO-8601 evidence time; malformed/naive values sort oldest."""
    try:
        if not isinstance(value, str) or len(value) > 64:
            return 0.0
        parsed = dt.datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else 0.0
    except (ValueError, OverflowError, OSError):
        return 0.0


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _zone(project) -> dt.tzinfo:
    """Configured day-boundary zone for usage buckets (UTC default unchanged)."""
    try:
        from .config import resolve_timezone
        name = getattr(getattr(project, "config", None), "timezone", "UTC") or "UTC"
        return resolve_timezone(name)
    except Exception:
        return dt.timezone.utc


def _day(record, path, tz: dt.tzinfo | None = None):
    tz = tz if tz is not None else dt.timezone.utc
    try:
        return dt.datetime.fromisoformat(record["finished_at"]).astimezone(tz).date().isoformat()
    except (KeyError, ValueError, TypeError):
        return dt.datetime.fromtimestamp(_mtime(path), tz).date().isoformat()


def usage_views(model):
    """Split explicitly attributed observations without assigning invocation counts to models.

    Legacy evidence keeps its recorded grouping. New adapters mark unknown
    attribution explicitly. Admissions occupy the unknown/unknown bucket;
    token-only views never multiply admission totals.
    """
    observations = model.get("usage_observations")
    if not isinstance(observations, list):
        return [model]
    views = {}
    for observation in observations:
        if not isinstance(observation, dict):
            observation = {"usage": observation}
        identity = tuple(observation.get(k) if isinstance(observation.get(k), str) and observation[k] else "unknown"
                         for k in ("provider", "model"))
        view = views.setdefault(identity, {**model, "provider": identity[0], "model": identity[1],
                                          "usage": [], "requests": 0, "requests_known": False,
                                          "admission_only": False})
        view["usage"].append(observation.get("usage"))
    admission = views.setdefault(("unknown", "unknown"),
        {**model, "provider": "unknown", "model": "unknown", "usage": [],
         "admission_only": bool(observations)})
    admission.update(requests=model.get("requests", 0), requests_known=model.get("requests_known", True))
    return list(views.values())


def summarize(project) -> dict:
    tz = _zone(project)
    candidates = heapq.nlargest(MAX_RUNS_SCANNED + 1, (p for p in (project.root / "runs").glob("*")
                                if p.is_dir() and not p.is_symlink()), key=lambda p: (_mtime(p), p.name))
    truncated = len(candidates) > MAX_RUNS_SCANNED
    groups, entries = {}, []
    scanned = skipped = unknown_shapes = entry_count = 0
    run_count = unknown_usage_runs = unknown_request_runs = 0
    for run_dir in candidates[:MAX_RUNS_SCANNED]:
        jobs = []
        canonical_found = False
        for name in ("result.json", "consultation.json", "error.json"):
            target = run_dir / name
            try:
                if target.is_symlink():
                    skipped += 1
                    continue
                if not target.is_file():
                    continue
                if target.stat().st_size > 1048576:
                    skipped += 1
                    continue
                record = read_json(target)
                if not isinstance(record, dict):
                    skipped += 1
                    continue
                if isinstance(record.get("model"), dict):
                    if canonical_found:
                        continue
                    canonical_found = True
                    run_count += 1
                    source = record["model"]
                    unknown_request_runs += source.get("requests_known", True) is False
                    observations = source.get("usage_observations")
                    usages = ([item.get("usage") if isinstance(item, dict) else item for item in observations]
                              if isinstance(observations, list) else source.get("usage"))
                    known = isinstance(usages, list) and bool(usages) and source.get("usage_known", True)
                    unknown_usage_runs += not (known and all(not normalize(item, str(source.get("engine", "unknown")))["unknown_shape"] for item in usages))
                    jobs.extend((run_dir.name, record, view) for view in usage_views(record["model"]))
                else:
                    continue
                scanned += 1
            except (OSError, ValueError, TypeError, AttributeError):
                skipped += 1
        for run_id, record, model in jobs:
            day = _day(record, run_dir, tz)
            provider, model_id, engine = (str(model.get(k, "unknown")) for k in ("provider", "model", "engine"))
            key = (day, provider, model_id)
            group = groups.setdefault(key, {"day": day, "provider": provider, "model": model_id,
                                          "engines": [], "runs": 0, "requests": 0,
                                          "request_units": {}, "unknown_request_runs":0, **dict.fromkeys(_FIELDS, 0),
                                          "unrecognized_keys": [], "unknown_shapes": 0, "unknown_usage_runs": 0})
            if engine not in group["engines"]:
                group["engines"].append(engine)
                group["engines"].sort()
            unit = model.get("request_unit", "unknown")
            requests = model.get("requests", 0)
            requests = requests if type(requests) is int and requests >= 0 else 0
            entry = {"run": run_id, "role": record.get("role", "consult" if "consult" in run_id else "unknown"),
                     "day": day, "finished_at": record.get("finished_at"), "started_at": None,
                     "status": record.get("status", "unknown"), "engine": engine, "provider": provider,
                     "model": model_id, "requests": requests, "request_unit": unit,
                     **dict.fromkeys(_FIELDS, 0), "unknown_shapes": 0}
            group["runs"] += 1
            entry["requests_known"] = model.get("requests_known",True) is not False
            if not entry["requests_known"]:group["unknown_request_runs"] += 1
            group["requests"] += requests
            group["request_units"][str(unit)] = group["request_units"].get(str(unit), 0) + requests
            usages = model.get("usage")
            parts = [normalize(item, engine) for item in usages] if isinstance(usages, list) else []
            known = model.get("admission_only", False) or (bool(parts) and model.get("usage_known", True) and not any(p["unknown_shape"] for p in parts))
            entry["usage_known"] = bool(known)
            if not known:
                group["unknown_usage_runs"] += 1
            for part in parts:
                for field in _FIELDS:
                    group[field] += part[field]
                    entry[field] += part[field]
                group["unrecognized_keys"] = sorted(set(group["unrecognized_keys"] + part["unrecognized_keys"]))[:MAX_UNKNOWN_KEYS]
                if part["unknown_shape"]:
                    group["unknown_shapes"] += 1
                    entry["unknown_shapes"] += 1
                    unknown_shapes += 1
            try:
                if "/" not in run_id:
                    started = read_json(run_dir / "started.json", {})
                    entry["started_at"] = started.get("started_at") if isinstance(started, dict) else None
            except (OSError, ValueError, TypeError, AttributeError):
                skipped += 1
            entry_count += 1
            key = (record_time(entry.get("finished_at")), entry["run"])
            value = (*key, entry_count, entry)
            if len(entries) < MAX_RECENT_ENTRIES:
                heapq.heappush(entries, value)
            elif key > entries[0][:2]:
                heapq.heapreplace(entries, value)
    recent = [e[3] for e in sorted(entries)]
    ordered = sorted(groups.values(), key=lambda g: (g["day"], g["provider"], g["model"]))
    totals = {field: sum(g[field] for g in ordered) for field in ("runs", "requests", *_FIELDS, "unknown_usage_runs", "unknown_request_runs")}
    totals.update(runs=run_count, unknown_usage_runs=unknown_usage_runs, unknown_request_runs=unknown_request_runs)
    return {"project": project.name, "groups": ordered, "totals": totals,
            "recent_entries": recent, "entries_truncated": entry_count > MAX_RECENT_ENTRIES,
            "scanned_records": scanned, "skipped_records": skipped, "unknown_shapes": unknown_shapes,
            "truncated": truncated, "note": "Provider-reported amounts; cached fields may be subsets of input. Not a bill."}
