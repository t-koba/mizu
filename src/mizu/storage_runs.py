"""Bounded, typed live-run references and interruptible run retirement.

Internal to storage.prune; callers hold the project maintenance exclusions.
No prose, provider CI IDs, historical audit run fields or snapshot references
are roots. Unknown run envelopes are retained, never interpreted as completion.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

from .errors import Denied, LimitExceeded
from .usage import MAX_RUNS_SCANNED
from .fs import DIGEST, canonical, digest, identifier, loads_finite, safe_read, sync_dir, write_json

MAX_ENTRIES = 100_000
MAX_BYTES = 64 * 1024 * 1024
RECORD_BYTES = 1024 * 1024
PREVIEW_ROWS = 100
MAX_DEPTH = 8
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
# Exact CLI sentinel for operator decisions (src/mizu/cli.py): not a Mizu run.
OPERATOR_RUN = "operator"
OUTCOMES = {"done", "continue", "wait", "blocked", "bounded"}


def _directory(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    value = dt.datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("naive timestamp")
    return value.timestamp()


class Scan:
    def __init__(self, root):
        self.root = root
        self.entries = self.bytes = 0

    def children(self, directory, limit=MAX_ENTRIES):
        try:
            directory.lstat()
        except FileNotFoundError:
            return []
        if not _directory(directory):
            raise Denied("Unsafe run-reference directory")
        result = []
        with os.scandir(directory) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_ENTRIES or len(result) >= limit:
                    raise LimitExceeded("Run-reference entry bound exceeded")
                result.append(Path(entry.path))
        return sorted(result)

    def record(self, path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise Denied("Unsafe run-reference record")
        raw = safe_read(self.root, path.relative_to(self.root).as_posix(), RECORD_BYTES)
        self.bytes += len(raw)
        if self.bytes > MAX_BYTES:
            raise LimitExceeded("Run-reference byte bound exceeded")
        value = loads_finite(raw)
        if not isinstance(value, dict):
            raise Denied("Malformed run-reference record")
        return value

    def optional(self, path):
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        return self.record(path)

    def preflight(self, directory, depth=0, *, file_limit=RECORD_BYTES):
        """lstat every member before ledger reads or recursive retirement.

        Opaque payloads (file_limit=None) need no parsing or byte budget;
        entries and depth still bound their traversal. Never descend through a link or
        Windows reparse point, including ones hidden under otherwise safe dirs.
        """
        if depth > MAX_DEPTH:
            raise LimitExceeded("Run-reference subtree depth bound exceeded")
        for path in self.children(directory):
            info = path.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise Denied("Unsafe run-reference subtree entry")
            if stat.S_ISDIR(info.st_mode):
                self.preflight(path, depth + 1, file_limit=file_limit)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if file_limit is not None:
                    self.bytes += info.st_size
                    if self.bytes > MAX_BYTES or info.st_size > file_limit:
                        raise LimitExceeded("Run-reference subtree byte bound exceeded")
            else:
                raise Denied("Unsafe run-reference subtree entry")


def _run(value):
    if not isinstance(value, str) or RUN_ID.fullmatch(value) is None:
        raise Denied("Invalid local run reference")
    return value


def _evidence(record, pin):
    if record.get("run") is not None:
        pin(record["run"])
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list):
        raise Denied("Malformed insight evidence")
    for entry in evidence:
        if not isinstance(entry, dict):
            raise Denied("Malformed insight evidence entry")
        if entry.get("run") is not None:
            pin(entry["run"])


def _terminal(run, started, records, clock):
    """Validate known lifecycle envelopes, not opaque model/log payloads."""
    try:
        if (not started or started.get("run") != run or not RUN_ID.fullmatch(run)
                or not isinstance(started.get("snapshot"), str)
                or not DIGEST.fullmatch(started["snapshot"])):
            return False
        identifier(started["role"])
        stamp = _stamp(started["started_at"])
        if stamp > clock:
            return False
        envelopes = {name: record for name, record in records.items() if record is not None}
        if len(envelopes) > 1:
            # Runtime may record an error then rewrite its result by merging
            # that same error. No other pair of terminal envelopes is emitted
            # by a consistent successful or interrupted lifecycle.
            if set(envelopes) != {"result.json", "error.json"} or "parent_run" in started:
                return False
            error, result = envelopes["error.json"], envelopes["result.json"]
            if (error.get("status") != "interrupted" or result.get("status") != "interrupted"
                    or any(key not in result or result[key] != value for key, value in error.items())):
                return False
        found = False
        for name, record in records.items():
            if record is None:
                continue
            if (record.get("run") != run or record.get("role") != started["role"]
                    or not stamp <= _stamp(record.get("finished_at")) <= clock):
                return False
            status = record.get("status")
            if name == "error.json" or (name == "result.json" and status in ("interrupted", "bounded")):
                if status not in ("interrupted", "bounded") or not isinstance(record.get("error"), str):
                    return False
            elif name in ("consultation.json", "delegation.json"):
                if (status != "completed" or record.get("parent_run") != started.get("parent_run")
                        or not isinstance(record.get("answer"), str)
                        or not isinstance(record.get("snapshot"), str)
                        or not DIGEST.fullmatch(record["snapshot"])):
                    return False
                _run(record.get("parent_run"))
            elif name == "result.json" and status in ("completed", "waiting"):
                # Prepared is recoverable intermediate evidence: publication
                # and post-processing have not yet completed.
                finish = record.get("finish")
                if finish is not None:
                    if (not isinstance(record.get("snapshot"), str)
                            or not DIGEST.fullmatch(record["snapshot"])):
                        return False
                    if (not isinstance(finish, dict) or finish.get("outcome") not in OUTCOMES
                            or not isinstance(finish.get("summary"), str)
                            or not isinstance(finish.get("state"), str)):
                        return False
                elif not (status == "waiting" and isinstance(record.get("selection"), dict)
                          and "profile" in record["selection"]
                          and record["selection"]["profile"] in (None, "")):
                    return False
            else:
                return False
            found = True
        return found
    except (ValueError, KeyError, TypeError, Denied, OverflowError):
        return False


def plan(project):
    """Recomputed under locks on every preview/apply. Bounds close globally."""
    root = project.root
    scan = Scan(root)
    days = project.config.limits.run_retention_days
    if type(days) is not int or not 0 <= days <= 3650:
        raise Denied("Invalid run_retention_days (expected 0–3650)")
    clock = time.time()
    if not days:
        # No new expiry needs live references. The caller still independently
        # validates and recovers committed/uncommitted quarantine under locks.
        return {"complete": True, "days": days, "scanned_at": clock,
                "components": [], "pinned_runs": 0, "runs_seen": 0}
    graph, eligible, pins = {}, set(), set()

    def existing(value):
        run = _run(value)
        if run not in graph:
            raise Denied("Missing referenced run member")
        return run

    def pin(value):
        pins.add(existing(value))
        if len(pins) > MAX_ENTRIES:
            raise LimitExceeded("Run-reference pin bound exceeded")

    def edge(a, b):
        b = existing(b)
        graph[a].add(b)
        graph[b].add(a)

    try:
        # Physical run count is the usage.py bound, independent of the larger
        # entry/byte budgets needed for nested evidence and permanent ledgers.
        directories = scan.children(root / "runs", limit=MAX_RUNS_SCANNED)
        for directory in directories:
            if not _directory(directory):
                raise Denied("Unsafe run directory")
            graph[directory.name] = set()
        for directory in directories:
            run = directory.name
            started = scan.optional(directory / "started.json")
            records = {name: scan.optional(directory / name) for name in
                       ("result.json", "error.json", "consultation.json", "delegation.json")}
            for record in (started, *records.values()):
                if record and "parent_run" in record:
                    edge(run, record["parent_run"])
            for name in ("consultations", "delegations"):
                for path in scan.children(directory / name):
                    record = scan.record(path)
                    if name == "delegations":
                        edge(run, record.get("run"))
                    else:
                        answers = record.get("answers")
                        if not isinstance(answers, list):
                            raise Denied("Malformed consultation links")
                        for answer in answers:
                            if not isinstance(answer, dict):
                                raise Denied("Malformed consultation link")
                            edge(run, answer.get("run"))
            if days and _terminal(run, started, records, clock):
                if clock - _stamp(started["started_at"]) > days * 86400:
                    eligible.add(run)

        # Pending scheduler continuations own both their source evidence and
        # any already-created successor. The reserved ID can precede its run
        # directory; source evidence, by contrast, must exist. Pointer metadata
        # is bounded and validated by its single owner.
        from .continuations import entries as continuation_entries, pointer_at
        for intent in continuation_entries(pointer_at(root)):
            pin(intent["source_run"])
            if intent["successor"] in graph:
                pin(intent["successor"])

        from . import safe_point
        fence = safe_point.read(project.snapshots)
        if fence is not None:
            pin(fence["source_run"])
            pin(fence["run"])

        for path in scan.children(root / "inbox"):
            _evidence(scan.record(path), pin)
        for path in scan.children(root / "active"):
            pin(scan.record(path).get("run"))
        for path in scan.children(root / "selection"):
            # Other classification caches contain no live run pointer.
            if path.name.endswith("-wait.json"):
                record = scan.record(path)
                if (record.get("version") == 1 and isinstance(record.get("fingerprint"), str)
                        and DIGEST.fullmatch(record["fingerprint"])):
                    run = record.get("run")
                    if run == "":  # documented invalidation sentinel
                        continue
                    existing(run)
                    saved = scan.optional(root / "runs" / run / "result.json")
                    selection = scan.optional(root / "runs" / run / "selection.json")
                    if saved and saved.get("run") == run and saved.get("status") == "waiting" and selection is not None:
                        pin(run)
                else:
                    raise Denied("Malformed selection wait")

        # Bound the physical ledger before invoking its existing schema/chain
        # validation. No recovery, locks, projections or scheduling here.
        for name in ("work-items", "decision-history", "decisions", "insight-ids", "insight-revisions"):
            scan.preflight(root / name)
        store = project.work_items
        waits = []
        for event_id, event in store._iter_item_events(recover=False):
            kind, seed = store._seed_for_event(event_id, event, readonly=True)
            if kind == "unseeded":
                raise Denied("Unresolved work-item seed")
            if kind != "seed":
                continue
            revisions = store._history_revisions(seed["id"], recover=False)
            item = store._get(seed["id"], seed_event=event, history_revisions=revisions)
            if item["status"] == "blocked" and item.get("wait"):
                waits.append(item["wait"])
        required_keys = set()
        for wait in waits:
            if wait["kind"] not in ("dependency", "insight_decided"):
                continue
            required = wait.get("requires")
            target = required["insight"] if required else wait["target"]
            identifier(target)
            # Current target decision is the exact evidence used by wait_due;
            # do not pin unrelated decisions or arbitrary run fields in prose.
            decision = scan.optional(root / "decisions" / (target + ".json"))
            if decision is not None:
                store._validate_current_decision_shape(decision, target)
                if not required or (decision.get("rev", 1) == required["rev"]
                                    and decision.get("action") == required["action"]):
                    run_value = decision.get("run")
                    # The operator CLI records substantive decisions with the
                    # exact "operator" sentinel, which names no local run.
                    # Only that sentinel (and an absent/null run, e.g.
                    # withdrawal) pins nothing; any other non-hex value
                    # still fails closed inside pin().
                    if run_value is not None and run_value != OPERATOR_RUN:
                        pin(run_value)
            # Dependency waits remain bound to the exact historical revision
            # even if the current result moved. Pin that decision, not a newer
            # head. History is bounded above and validated by the ledger.
            if required:
                required_keys.add((target, required["rev"], required["action"]))
        if required_keys:
            matched = set()
            for _, event in store._iter_decision_events(recover=False):
                key = (event.get("id"), event.get("rev", 1), event.get("action"))
                if key in required_keys:
                    if key in matched:
                        raise Denied("Duplicate exact dependency decision evidence")
                    matched.add(key)
                    historical = event["run"]
                    if historical is not None and historical != OPERATOR_RUN:
                        pin(historical)
            if matched != required_keys:
                raise Denied("Missing exact dependency decision evidence")
        components = []
        unseen = set(graph)
        for first in sorted(graph):
            if first not in unseen:
                continue
            component, pending = set(), [first]
            unseen.remove(first)
            while pending:
                run = pending.pop()
                component.add(run)
                for linked in graph[run] & unseen:
                    unseen.remove(linked)
                    pending.append(linked)
            if component <= eligible and not component & pins:
                components.append(sorted(component))
        components.sort()
        # Check the entire payload, not only the lifecycle/link JSON that made
        # it eligible. A bounded incomplete preflight refuses the whole apply.
        for component in components:
            for run in component:
                scan.preflight(root / "runs" / run, file_limit=None)
        if any(len(canonical(component)) + 1024 > RECORD_BYTES for component in components):
            raise LimitExceeded("Run component exceeds quarantine manifest bound")
        return {"complete": True, "days": days, "scanned_at": clock,
                "components": components, "pinned_runs": len(pins), "runs_seen": len(graph)}
    except (Denied, LimitExceeded, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        return {"complete": False, "days": days, "components": [], "reason": str(exc)[:256]}


def preview(plan):
    components = plan["components"]
    runs = [run for component in components for run in component]
    return {**{k: v for k, v in plan.items() if k != "components"},
            "component_count": len(components), "run_count": len(runs),
            "runs": runs[:PREVIEW_ROWS], "truncated": len(runs) > PREVIEW_ROWS,
            "plan_sha256": digest(canonical(components))}


def quarantines(project):
    """Validate all leftovers before recovery or deletion; never valid runs."""
    scan = Scan(project.root)
    directory = project.root / "maintenance" / "run-quarantine"
    # safe_read checks parents; children must check the maintenance parent too.
    if directory.parent.exists() and not _directory(directory.parent):
        raise Denied("Unsafe maintenance directory")
    if directory.parent.is_symlink():
        raise Denied("Unsafe maintenance directory")
    result = []
    for path in scan.children(directory):
        if re.fullmatch(r"(?:staging|retired)-[0-9a-f]{32}", path.name) is None:
            raise Denied("Unknown run quarantine entry")
        if not _directory(path):
            raise Denied("Unsafe run quarantine")
        # Validate the entire remaining payload before any rollback/deletion,
        # including empty staging and partially removed committed components.
        scan.preflight(path, file_limit=None)
        manifest_path = path / "manifest.json"
        manifest = scan.optional(manifest_path)
        if manifest is None:
            completion = scan.optional(directory.parent / (path.name + ".json"))
            if path.name.startswith("retired-") and completion:
                manifest = completion.get("intent")
            elif path.name.startswith("staging-") and completion and completion.get("intent"):
                manifest = completion["intent"]
            elif path.name.startswith("staging-"):
                children = scan.children(path)
                if any(p.name != "runs" for p in children) or scan.children(path / "runs"):
                    raise Denied("Missing run quarantine manifest")
                result.append((path, []))
                continue
            else:
                raise Denied("Missing run quarantine manifest")
        if not isinstance(manifest, dict):
            raise Denied("Malformed run quarantine manifest")
        runs = manifest.get("runs")
        if (manifest.get("version") != 1 or not isinstance(runs, list) or not runs
                or any(not isinstance(r, str) for r in runs)
                or len(runs) != len(set(runs))):
            raise Denied("Malformed run quarantine manifest")
        if (type(manifest.get("days")) is not int or not 1 <= manifest["days"] <= 3650
                or type(manifest.get("scanned_at")) not in (int, float)
                or not 0 <= manifest["scanned_at"] <= time.time()
                or not isinstance(manifest.get("plan_sha256"), str)
                or not DIGEST.fullmatch(manifest["plan_sha256"])):
            raise Denied("Malformed run quarantine intent")
        for run in runs:
            _run(run)
        for child in scan.children(path):
            if child.name not in ("manifest.json", "runs"):
                raise Denied("Unknown run quarantine member")
        for child in scan.children(path / "runs"):
            if child.name not in runs or not _directory(child):
                raise Denied("Unsafe quarantined run")
        result.append((path, runs))
    return result


def recover_staging(project, leftovers):
    """Rollback uncommitted staging; never delete a possibly live run."""
    if any(p.name.startswith("staging-") and r for p, r in leftovers) and not _directory(project.root / "runs"):
        raise Denied("Unsafe run recovery destination")
    for path, runs in leftovers:
        if not path.name.startswith("staging-"):
            continue
        for run in runs:
            source = path / "runs" / run
            target = project.root / "runs" / run
            if source.exists() and (target.exists() or target.is_symlink()):
                raise Denied("Run quarantine recovery collision")
            if not source.exists() and not target.is_dir():
                raise Denied("Missing run during quarantine recovery")
        for run in runs:
            source = path / "runs" / run
            if source.exists():
                source.rename(project.root / "runs" / run)
                sync_dir(project.root / "runs")
                sync_dir(path / "runs")
        if (path / "manifest.json").exists():
            write_json(path.parent.parent / (path.name + ".json"),
                       {"restored_at": time.time(), "intent": Scan(project.root).record(path / "manifest.json")})
        # Leave even empty staging/intent in place until the new reference
        # scan succeeds. An incomplete scan must not delete anything.



def retire(project, plan, leftovers):
    """Durable intent, staged renames, atomic component commit, then deletion.

    A crash before commit restores the entire staged component on next apply.
    A crash after commit resumes deletion, including partially removed payloads.
    I/O errors propagate; the intent and quarantine remain as failure evidence.
    """
    base = project.root / "maintenance" / "run-quarantine"
    base.mkdir(parents=True, exist_ok=True)
    sync_dir(base.parent)
    plan_digest = preview(plan)["plan_sha256"]
    for component in plan["components"]:
        key = uuid.uuid4().hex
        staging = base / ("staging-" + key)
        (staging / "runs").mkdir(parents=True)
        write_json(staging / "manifest.json", {"version": 1, "runs": component,
                   "days": plan["days"], "scanned_at": plan["scanned_at"],
                   "plan_sha256": plan_digest})
        sync_dir(staging)
        sync_dir(base)
        for run in component:
            (project.root / "runs" / run).rename(staging / "runs" / run)
            sync_dir(project.root / "runs")
            sync_dir(staging / "runs")
        retired = base / ("retired-" + key)
        staging.rename(retired)  # atomic per-component deletion commit
        sync_dir(base)
        leftovers.append((retired, component))
    for path, _ in leftovers:
        if path.name.startswith("staging-"):
            if (path / "runs").exists():
                (path / "runs").rmdir()  # recovery must have restored everything
            (path / "manifest.json").unlink(missing_ok=True)
            path.rmdir()
            sync_dir(base)
            continue
        payload = path / "runs"
        if payload.exists():
            shutil.rmtree(payload)  # never suppress a partial failure
        # Keep an independent permanent completion record before removing intent.
        manifest = Scan(project.root).optional(path / "manifest.json")
        if manifest is not None:
            write_json(base.parent / (path.name + ".json"),
                       {"completed_at": time.time(), "intent": manifest})
        (path / "manifest.json").unlink(missing_ok=True)
        path.rmdir()
        sync_dir(base)
