"""Usage facts: aggregate provider-reported token records into queryable sums.

The mechanism collects only facts the harness already recorded in completed
run records (`runs/*/result.json`, consultation answers in
`runs/*/consultation.json`): provider, model, request counts, token amounts
and timestamps. It never prices anything and never calls money a limit:
whether a dashboard shows estimated cost, and at which operator-pinned rate,
is presentation policy (see `docs/dashboard.md`). Provider `usage` shapes
differ, so anything not recognized is surfaced as `other_tokens` /
`unrecognized_keys` / `unknown_shapes` instead of being silently dropped or
folded into the input/output sums.

Nothing here is destructive: this module is read-only, and the raw per-run
records stay on disk (prune never removes them). Grouped sums assume one
rate per (day, provider, model); anything finer — for example time-of-day
off-peak rates — uses `recent_entries`, which keep full per-run timestamps,
or the raw `runs/*/result.json` records themselves.
"""
from __future__ import annotations

import datetime as dt

from .fs import read_json

SCHEMA = 1
#: Most recent run records scanned per summary; older ones are flagged, not hidden.
MAX_RUNS_SCANNED = 5000
#: Per-run entries retained with full timestamps (time-of-day rates need these).
MAX_RECENT_ENTRIES = 200
#: Distinct unrecognized usage keys retained per group for transparency.
MAX_UNKNOWN_KEYS = 20

_INPUT_KEYS = {"input", "inputtokens", "prompttokens"}
_OUTPUT_KEYS = {"output", "outputtokens", "completiontokens"}


def _flat(name: str) -> str:
    return name.lower().replace("_", "")


def normalize(entry) -> dict:
    """Split one provider usage record into input/output/other token counts."""
    if not isinstance(entry, dict):
        return {"input_tokens": 0, "output_tokens": 0, "other_tokens": 0,
                "unrecognized_keys": [], "unknown_shape": True}
    found = {"input_tokens": 0, "output_tokens": 0, "other_tokens": 0}
    unknown = []
    for key, value in entry.items():
        if type(value) is bool or not isinstance(value, (int, float)):
            continue
        amount = int(value)
        flat = _flat(str(key))
        if flat in _INPUT_KEYS:
            found["input_tokens"] += amount
        elif flat in _OUTPUT_KEYS:
            found["output_tokens"] += amount
        else:
            found["other_tokens"] += amount
            if key not in unknown:
                unknown.append(str(key))
    return {**found, "unrecognized_keys": sorted(unknown)[:MAX_UNKNOWN_KEYS],
            "unknown_shape": False}


def _day(record: dict, path) -> str:
    finished = record.get("finished_at")
    if isinstance(finished, str) and len(finished) >= 10:
        return finished[:10]
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).date().isoformat()
    except OSError:
        return "unknown"


def summarize(project) -> dict:
    """Sum recorded usage grouped by (day, provider, model). Read-only.

    Also retains per-run entries with full timestamps so presentation policy
    can apply finer rate structures (e.g. time-of-day off-peak pricing)
    without returning to raw files. Scans the most recent run records up to
    `MAX_RUNS_SCANNED` and keeps the newest `MAX_RECENT_ENTRIES` entries;
    when more exist the summary is partial and `truncated` is true. Never
    raises for a malformed single record: it is counted in
    `skipped_records` instead.
    """
    def mtime(path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    candidates = sorted((project.root / "runs").glob("*"), key=mtime)
    groups: dict[tuple, dict] = {}
    entries: list[dict] = []
    scanned = skipped = unknown_shapes = 0
    truncated = len(candidates) > MAX_RUNS_SCANNED
    for run_dir in candidates[-MAX_RUNS_SCANNED:]:
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        for name in ("result.json", "consultation.json"):
            target = run_dir / name
            try:
                if target.is_file() and target.stat().st_size > 1048576:
                    skipped += 1  # Harness records are KBs; never parse megabytes.
                    continue
                record = read_json(target, None)
                if not isinstance(record, dict):
                    continue
                if name == "result.json":
                    if record.get("status") != "completed" or not isinstance(record.get("model"), dict):
                        continue
                    jobs = [(run_dir.name, record.get("role", "unknown"),
                             _day(record, run_dir / name), record["model"])]
                else:
                    answers = record.get("answers")
                    if not isinstance(answers, list):
                        continue
                    jobs = [(f"{run_dir.name}/consult-{i}", "consult",
                             _day(record, run_dir / name), a["model"])
                            for i, a in enumerate(answers)
                            if isinstance(a, dict) and isinstance(a.get("model"), dict)]
                for run_id, role, day, model in jobs:
                    provider = str(model.get("provider", "unknown"))
                    model_id = str(model.get("model", "unknown"))
                    engine = str(model.get("engine", "unknown"))
                    key = (day, provider, model_id)
                    group = groups.setdefault(key, {"day": day, "provider": provider, "model": model_id,
                                                    "engines": [],
                                                    "runs": 0, "requests": 0, "input_tokens": 0,
                                                    "output_tokens": 0, "other_tokens": 0,
                                                    "unrecognized_keys": [], "unknown_shapes": 0})
                    if engine not in group["engines"]:
                        group["engines"].append(engine)
                        group["engines"].sort()
                    entry = {"run": run_id, "role": role, "day": day,
                             "finished_at": record.get("finished_at"),
                             "engine": str(model.get("engine", "unknown")),
                             "started_at": None,  # Filled below, only for retained entries.
                             "provider": provider, "model": model_id,
                             "requests": 0, "input_tokens": 0, "output_tokens": 0,
                             "other_tokens": 0, "unknown_shapes": 0}
                    group["runs"] += 1
                    requests = model.get("requests")
                    if isinstance(requests, int) and requests > 0:
                        group["requests"] += requests
                        entry["requests"] = requests
                    usages = model.get("usage")
                    if not isinstance(usages, list):
                        usages = []
                    for item in usages:
                        part = normalize(item)
                        for field in ("input_tokens", "output_tokens", "other_tokens"):
                            group[field] += part[field]
                            entry[field] += part[field]
                        for key_name in part["unrecognized_keys"]:
                            if key_name not in group["unrecognized_keys"] and \
                                    len(group["unrecognized_keys"]) < MAX_UNKNOWN_KEYS:
                                group["unrecognized_keys"].append(key_name)
                        if part["unknown_shape"]:
                            group["unknown_shapes"] += 1
                            entry["unknown_shapes"] += 1
                            unknown_shapes += 1
                    entries.append(entry)
                scanned += 1
            except (ValueError, TypeError, AttributeError):
                skipped += 1
    entries.sort(key=lambda e: (str(e.get("finished_at") or ""), str(e.get("run") or "")))
    recent = entries[-MAX_RECENT_ENTRIES:]
    for entry in recent:
        # Consult entries have no started record; result entries read one file each.
        if entry["started_at"] is None and "/" not in entry["run"]:
            entry["started_at"] = read_json(
                project.root / "runs" / entry["run"] / "started.json", {}).get("started_at")
    ordered = sorted(groups.values(), key=lambda g: (g["day"], g["provider"], g["model"]))
    totals = {"runs": sum(g["runs"] for g in ordered), "requests": sum(g["requests"] for g in ordered),
              "input_tokens": sum(g["input_tokens"] for g in ordered),
              "output_tokens": sum(g["output_tokens"] for g in ordered),
              "other_tokens": sum(g["other_tokens"] for g in ordered)}
    return {"schema": SCHEMA, "project": project.name, "groups": ordered, "totals": totals,
            "recent_entries": recent,
            "entries_truncated": len(entries) > MAX_RECENT_ENTRIES,
            "scanned_records": scanned, "skipped_records": skipped,
            "unknown_shapes": unknown_shapes, "truncated": truncated,
            "note": "Provider-reported amounts grouped for presentation policy. Not a bill."}
