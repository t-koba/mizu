"""Bounded usage facts from run evidence, never pricing or a monetary limit.

Logical model requests, turns and queries are distinct units.
Completed/interrupted normal and child consultation records are canonical;
parent consultation records refer to child runs. Unknown usage is explicit.
Records are never rewritten. Corrupt individual records increment skipped.
"""
from __future__ import annotations

import datetime as dt
import heapq
import re

from .errors import MizuError
from .fs import read_json

MAX_RUNS_SCANNED = 5000
MAX_RECENT_ENTRIES = 200
MAX_UNKNOWN_KEYS = 20
MAX_CACHE_AFFINITY_ROWS = 200
MAX_CACHE_AFFINITY_RUNS = 200
_SESSION_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_CACHE_STATUSES = ("known", "partial", "unknown")
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "other_tokens")
_KEYS = {
    "pi": {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens"},
    "pi-durable": {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens"},
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


def attempt_usage(observations: list, engine: str | None) -> dict:
    """Strict attempt-only totals: omitted/invalid fields stay null, not zero.

    Unlike historical dashboard normalization, a total is known only when
    every reported observation supplies that field. This cannot establish
    completeness of provider reporting; it describes the observations only.
    """
    fields = _KEYS.get(engine, _CANONICAL)
    totals = dict.fromkeys(_FIELDS, None)
    unknown = set()
    for field in _FIELDS:
        values = []
        for entry in observations:
            value = None
            if isinstance(entry, dict):
                for key, mapped in fields.items():
                    if mapped == field and key in entry:
                        candidate = entry[key]
                        if type(candidate) is int and 0 <= candidate <= 2**63 - 1:
                            value = candidate
            values.append(value)
        if values and all(value is not None for value in values):
            total = sum(values)
            if total <= 2**63 - 1:
                totals[field] = total
    for entry in observations:
        if isinstance(entry, dict):
            unknown.update(key[:256] for key in entry if key not in fields and key not in _METADATA)
            unknown = set(sorted(unknown)[:MAX_UNKNOWN_KEYS])
    known = sum(value is not None for value in totals.values())
    return {**totals, "observations": len(observations),
            "status": "known" if known == len(_FIELDS) else "partial" if known else "unknown",
            "unknown_reason": ("no_runtime_usage" if not observations else
                               "missing_or_unrecognized_fields" if known < len(_FIELDS) else None),
            "unrecognized_keys": sorted(unknown)[:MAX_UNKNOWN_KEYS]}


def prompt_input_tokens(engine, input_tokens, cache_read_tokens, cache_write_tokens):
    """Return the documented total prompt-token denominator for one engine.

    Pi's Usage and Anthropic Messages require mutually exclusive uncached,
    cache-read, and cache-write buckets. Codex's input total alone determines
    its denominator; cache-read remains necessary for the share. Unknown engines
    and incomplete denominator components stay unknown.
    """
    if engine == "codex":
        if type(input_tokens) is not int or not 0 <= input_tokens <= 2**63 - 1:
            return None
        total = input_tokens
    elif engine in ("pi", "pi-durable", "claude"):
        values = (input_tokens, cache_read_tokens, cache_write_tokens)
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in values):
            return None
        total = input_tokens + cache_read_tokens + cache_write_tokens
    else:
        return None
    return total if 0 < total <= 2**63 - 1 else None


def cache_read_share(prompt_input, cache_read_tokens):
    """Observed cache-read share of prompt input; null unless exactly computable."""
    if type(prompt_input) is not int or type(cache_read_tokens) is not int:
        return None
    if prompt_input <= 0 or cache_read_tokens < 0 or cache_read_tokens > prompt_input:
        return None
    return cache_read_tokens / prompt_input


def _dispatch_max_bytes() -> int:
    try:
        from .dispatch_evidence import MAX_BYTES as cap
        if type(cap) is int and cap > 0:
            return cap
    except Exception:
        pass
    return 256 * 1024


def _affinity_row(run_id: str, attempt: dict):
    """Whitelisted shadow row; never copies native session IDs, arbitrary model lists, or payloads."""
    role = attempt.get("role")
    role = role if isinstance(role, str) and 0 < len(role) <= 256 else "unknown"
    engine = attempt.get("engine")
    engine = engine if isinstance(engine, str) and 0 < len(engine) <= 256 else None
    started = attempt.get("started_at")
    started = started if isinstance(started, str) and 0 < len(started) <= 64 else None
    ended = attempt.get("ended_at")
    ended = ended if isinstance(ended, str) and 0 < len(ended) <= 64 else None
    session_key = session_generation = prompt_mode = None
    session = attempt.get("session")
    if isinstance(session, dict):
        key, generation, mode = session.get("key"), session.get("generation"), session.get("prompt_mode")
        if (isinstance(key, str) and _SESSION_KEY_RE.fullmatch(key)
                and type(generation) is int and generation >= 0):
            session_key, session_generation = key, generation
            prompt_mode = mode if isinstance(mode, str) and 0 < len(mode) <= 256 else None
    profile_raw = attempt.get("profile")
    profile = profile_raw if isinstance(profile_raw, str) and 0 < len(profile_raw) <= 256 else None
    provider = model = None
    model_attribution_unknown = True
    models = attempt.get("models")
    if attempt.get("models_unknown") is False and attempt.get("models_truncated") is False:
        if isinstance(models, list) and len(models) == 1 and isinstance(models[0], dict):
            raw_provider, raw_model = models[0].get("provider"), models[0].get("model")
            if (isinstance(raw_provider, str) and 0 < len(raw_provider) <= 256
                    and isinstance(raw_model, str) and 0 < len(raw_model) <= 256):
                provider, model = raw_provider, raw_model
                model_attribution_unknown = False
    usage = attempt.get("usage")
    if isinstance(usage, dict):
        status = usage.get("status")
        usage_status = status if status in _CACHE_STATUSES else "unknown"
        raw_input, raw_cache, raw_write = (usage.get("input_tokens"),
                                           usage.get("cache_read_tokens"),
                                           usage.get("cache_write_tokens"))
        input_tokens = raw_input if type(raw_input) is int and 0 <= raw_input <= 2**63 - 1 else None
        cache_read = raw_cache if type(raw_cache) is int and 0 <= raw_cache <= 2**63 - 1 else None
        cache_write = raw_write if type(raw_write) is int and 0 <= raw_write <= 2**63 - 1 else None
    else:
        usage_status, input_tokens, cache_read, cache_write = "unknown", None, None, None
    prompt_input = prompt_input_tokens(engine, input_tokens, cache_read, cache_write)
    return {"run": run_id, "role": role,
            "execution_attempt": attempt.get("execution_attempt"),
            "started_at": started, "ended_at": ended, "engine": engine,
            "profile": profile, "provider": provider, "model": model,
            "model_attribution_unknown": model_attribution_unknown,
            "session_key": session_key, "session_generation": session_generation,
            "prompt_mode": prompt_mode, "usage_status": usage_status,
            "input_tokens": input_tokens, "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write, "prompt_input_tokens": prompt_input,
            "cache_read_share_of_input": cache_read_share(prompt_input, cache_read)}


def _collect_cache_affinity(candidates) -> dict:
    """Recent shadow view over dispatch-attempts.json; read-only, bounded."""
    cap = _dispatch_max_bytes()
    rows: list = []
    skipped = 0
    run_window = candidates[:min(MAX_RUNS_SCANNED, MAX_CACHE_AFFINITY_RUNS)]
    for run_dir in run_window:
        target = run_dir / "dispatch-attempts.json"
        try:
            if target.is_symlink():
                skipped += 1
                continue
            if not target.is_file():
                continue
            record = read_json(target, max_bytes=cap)
            if not isinstance(record, dict):
                skipped += 1
                continue
            attempts = record.get("attempts")
            if not isinstance(attempts, list):
                skipped += 1
                continue
            # File order is oldest-first; newest attempt first preserves
            # most-recent-first ordering without extra timestamp reads.
            for attempt in reversed(attempts):
                if not isinstance(attempt, dict):
                    continue
                index = attempt.get("execution_attempt")
                if type(index) is not int or index < 0:
                    continue
                rows.append(_affinity_row(run_dir.name, attempt))
        except (OSError, ValueError, TypeError, AttributeError, MizuError):
            skipped += 1
    total = len(rows)
    return {"attempts": rows[:MAX_CACHE_AFFINITY_ROWS], "total": total,
            "truncated": total > MAX_CACHE_AFFINITY_ROWS, "skipped_records": skipped,
            "runs_scanned": len(run_window), "runs_truncated": len(candidates) > len(run_window)}

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


def _day(record, path, tz: dt.tzinfo):
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
    tz = project.config.tzinfo
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
            entry = {"run": run_id, "role": record.get("role", "unknown"),
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
    affinity = _collect_cache_affinity(candidates)
    return {"project": project.name, "groups": ordered, "totals": totals,
            "recent_entries": recent, "entries_truncated": entry_count > MAX_RECENT_ENTRIES,
            "scanned_records": scanned, "skipped_records": skipped, "unknown_shapes": unknown_shapes,
            "truncated": truncated, "cache_affinity": affinity,
            "note": "Provider-reported amounts; cached fields may be subsets of input. Not a bill."}
