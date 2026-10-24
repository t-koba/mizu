"""Conservative storage maintenance with explicit policy and nonblocking locks.

Prune expires completed, unreferenced run components and disposable projections.
Permanent ledgers and sessions remain. Backup and snapshot/object reclamation
add a pause requirement; prune needs exclusions, not a paused project.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from .admission import role_exclusions
from .config import keys, strings
from .errors import Busy, Denied, LimitExceeded
from .fs import (DIGEST, PREVIEW_BYTES, atomic_write, canonical, digest,
                 identifier, lock, mkdir, now, read_json, safe_read,
                 sync_dir, write_json, relative_parts)
from . import acceptance, insight_archive
from .project import Project, check_verify, read_goal
from .snapshot import open_store

#: Project members covered by backup/restore (plus backup.json on restore).
MEMBERS = (
    "project.toml", "PROJECT.md", "control.json", "current.json", "snapshots", "objects",
    "inbox", "insight-ids", "insight-revisions", "insight-archive.sqlite3", "histories", ".ingest", "decisions",
    "decision-history", "decision-cursors", "decision-receipts", "work-items", "work-item-receipts", "waits", "runs", "sessions",
    "evidence", "artifacts", "health", "observed", "maintenance", "spool", "selection",
)


@contextlib.contextmanager
def quiescent(project: Project, *, require_pause: bool = True):
    if require_pause and not project.control().get("paused"):
        raise Denied("Pause the project before storage maintenance")
    with contextlib.ExitStack() as stack:
        stack.enter_context(lock(project.root / "locks" / "workspace.lock", blocking=False))
        for path in role_exclusions(project.root, tuple(project.config.roles)):
            stack.enter_context(lock(path, blocking=False))
        stack.enter_context(lock(project.root / "locks" / "editor-ingest.lock", blocking=False))
        stack.enter_context(lock(project.root / "locks" / "insights.lock", blocking=False))
        yield


def backup(project: Project, destination: Path, *, verify: bool = False) -> dict:
    destination = destination.resolve()
    if destination.exists() or destination == project.root or project.root in destination.parents:
        raise Denied("Backup must be a new file outside the project tree")
    mkdir(destination.parent)
    if shutil.disk_usage(destination.parent).free < project.config.limits.free_disk_mb * 1048576:
        raise Denied("Free disk space is below the configured reserve")
    # Deliberately exclude project .git/hooks, active sockets/locks, caches and the
    # mutable worktree. A separate sealed snapshot captures uncommitted work.
    with quiescent(project):
        insight_archive.recover(project.root)
        invalid = acceptance.recover_producers(project, include_runs=True)
        if invalid:
            raise Denied('Backup refused: unresolved invalid acceptance producers: ' +
                         json.dumps(acceptance.producer_diagnostics(invalid), sort_keys=True))
        acceptance.audit(project.root)
        captured = project.snapshots.capture_files(project.workspace)
        checkpoint = project.snapshots.create(captured, goal=project.goal, state=project.snapshots.get()["state"],
                                              run=None, outcome="wait", summary="Operator backup checkpoint; not published")
        fd, name = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            # fsync the writable handle before linking: fsync on a
            # read-only descriptor fails on Windows, and durability must
            # precede the link so the new name never points at unwritten data.
            with lock(project.root / "locks/acceptance.lock"), temporary.open("w+b") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                    # All members are regular files or directories; no symlink dereferencing.
                    # Regenerable render output (index.html) is not evidence: back it up never.
                    # Reproducible run inputs live under runs/ only; source trees may
                    # legitimately contain input/ or controller/ directories.
                    include = MEMBERS
                    def safe(info):
                        parts = Path(info.name).parts
                        if not (info.isfile() or info.isdir()):
                            return None
                        if len(parts) >= 3 and parts[0] == "runs" and parts[2] in ("input", "controller"):
                            return None
                        if Path(info.name).name == "index.html":
                            return None
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        return info
                    for child in include:
                        path = project.root / child
                        if path.exists():
                            archive.add(path, arcname=child, recursive=True, filter=safe)
                    metadata = json.dumps({"checkpoint": checkpoint["id"],
                                           "project": project.name, "credentials_included": False}).encode()
                    info = tarfile.TarInfo("backup.json")
                    info.size, info.mode = len(metadata), 0o600
                    archive.addfile(info, io.BytesIO(metadata))
                raw.flush()
                os.fsync(raw.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise Denied("Backup must be a new file outside the project tree") from None
            except (AttributeError, NotImplementedError, OSError):
                # Exclusive creation still refuses a concurrent destination.
                try:
                    with temporary.open("rb") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                except FileExistsError:
                    raise Denied("Backup must be a new file outside the project tree") from None
            sync_dir(destination.parent)
            if verify:
                with tarfile.open(destination, "r:gz") as archive:
                    members = [m.name for m in archive]
        finally:
            temporary.unlink(missing_ok=True)
    result = {"archive": str(destination), "checkpoint": checkpoint["id"],
              "note": "Contains private code and model conversations. Credentials/configuration excluded. Protect this archive."}
    if verify:
        result["members"] = len(members)
    return result


#: Default artifact retention for `prune --keep-artifacts` (operator-overridable
#: via CLI; explicit 0 keeps all). The live pointer target is never a candidate.
DEFAULT_KEEP_ARTIFACTS = 30


#: Day length for run-evidence retention age (file mtime vs now).
_DAY_SECONDS = 86400

#: Per-run diagnostics and bounded dispatch measurements eligible for
#: retention (raw names). Compressed ``.gz`` siblings are eligible for the
#: drop stage only. Other run records remain durable.
_EVENT_LOG_RAW = ("diagnostics.txt", "dispatch-attempts.json")


def _event_log_paths(run_dir: Path) -> tuple[list[Path], list[Path]]:
    """Raw and compressed bulky log paths directly under one run dir.

    The ``*-events-truncated.json`` marker rides with its event stream:
    it compresses and drops on the same clocks, so a capped stream never
    leaves an orphaned marker or an unmarked gap.
    """
    raw: list[Path] = []
    compressed: list[Path] = []
    for child in sorted(run_dir.glob("*-events.jsonl")):
        if child.is_file() and not child.is_symlink():
            raw.append(child)
    for child in sorted(run_dir.glob("*-events-truncated.json")):
        if child.is_file() and not child.is_symlink():
            raw.append(child)
    for name in _EVENT_LOG_RAW:
        candidate = run_dir / name
        try:
            if candidate.is_file() and not candidate.is_symlink():
                raw.append(candidate)
        except OSError:
            continue
    for child in sorted(run_dir.glob("*-events.jsonl.gz")):
        if child.is_file() and not child.is_symlink():
            compressed.append(child)
    for child in sorted(run_dir.glob("*-events-truncated.json.gz")):
        if child.is_file() and not child.is_symlink():
            compressed.append(child)
    for name in _EVENT_LOG_RAW:
        candidate = run_dir / f"{name}.gz"
        try:
            if candidate.is_file() and not candidate.is_symlink():
                compressed.append(candidate)
        except OSError:
            continue
    return raw, compressed


def _file_age_days(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / _DAY_SECONDS
    except OSError:
        return -1.0


def _compress_event_log(raw: Path) -> Path:
    """Gzip one raw log, keep the original mtime on the ``.gz``, unlink raw."""
    target = raw.with_name(raw.name + ".gz")
    if target.is_symlink():
        raise Denied("Refusing a symlink during prune")
    try:
        mtime = raw.stat().st_mtime
    except OSError as exc:
        raise Denied(f"Cannot read event log: {exc}") from exc
    temporary = raw.with_name(raw.name + f".tmp-{uuid.uuid4().hex[:8]}.gz")
    try:
        with raw.open("rb") as source, gzip.GzipFile(temporary, "wb", mtime=int(mtime)) as output:
            shutil.copyfileobj(source, output, length=1048576)
            output.flush()
        try:
            os.utime(temporary, (mtime, mtime))
        except OSError as exc:
            raise Denied(f"Cannot stamp compressed event log: {exc}") from exc
        os.rename(temporary, target)
        try:
            os.utime(target, (mtime, mtime))
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    raw.unlink()
    try:
        sync_dir(raw.parent)
    except OSError:
        pass
    return target


def _web_cache_seconds(project: Project) -> int:
    """Operator-selected web-cache reuse TTL ([web] cache_seconds)."""
    web = getattr(project.config, "web", None)
    seconds = web.get("cache_seconds", 1800) if isinstance(web, dict) else 1800
    try:
        seconds = int(seconds)
    except (TypeError, ValueError) as exc:
        raise Denied(f"Invalid web-cache retention: {exc}") from exc
    if seconds < 1:
        raise Denied("Web-cache retention must be positive")
    return seconds


def _web_cache_candidates(project: Project) -> tuple[list[Path], dict]:
    """Expired global web-cache entries under the invoking config TTL.

    Expiry is ``retrieved_epoch`` age versus ``[web] cache_seconds``; entries
    without a usable epoch (corrupt, unreadable, or valid JSON without one)
    fall back to file mtime so poison entries still converge.
    Symlinks are skipped, never candidates. The cache is
    shared across projects under ``data_dir``; the invoking project's TTL
    decides expiry and removal is best-effort (a concurrent fetch recreates
    the entry, so apply is safe to retry).
    """
    seconds = _web_cache_seconds(project)
    base = project.config.data
    cache_dir = base / "web-cache"
    candidates: list[Path] = []
    entries = 0
    total_bytes = 0
    skipped = 0
    expired_bytes = 0
    try:
        children = sorted(cache_dir.glob("*.json")) if cache_dir.is_dir() else []
    except OSError:
        children = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_file() or not DIGEST.fullmatch(child.stem):
                skipped += 1
                continue
            try:
                size = child.stat().st_size
            except OSError:
                skipped += 1
                continue
            entries += 1
            total_bytes += size
            record = _audit_json(child)
            epoch = None
            if isinstance(record, dict):
                raw = record.get("retrieved_epoch")
                if type(raw) in (int, float) and raw == raw and raw not in (float("inf"), float("-inf")) and raw >= 0:
                    epoch = float(raw)
            if epoch is None:
                try:
                    mtime = float(child.stat().st_mtime)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                if (time.time() - mtime) >= seconds:
                    candidates.append(child)
                    expired_bytes += size
                continue
            if (time.time() - epoch) >= seconds:
                candidates.append(child)
                expired_bytes += size
        except OSError:
            skipped += 1
    candidates.sort()
    accounting = {
        "entries": entries,
        "bytes": total_bytes,
        "expired": len(candidates),
        "expired_bytes": expired_bytes,
        "expired_sample": sorted(f"web-cache/{p.name}" for p in candidates)[:_AUDIT_SAMPLE],
        "expired_truncated": len(candidates) > _AUDIT_SAMPLE,
        "skipped": skipped,
        "cache_seconds": seconds,
    }
    return candidates, accounting


def _session_accounting(project: Project) -> dict:
    """Capacity accounting for durable persistent-session evidence.

    Sessions are never reclamation candidates: content-bound session keys
    (``sessions/<role>/<key>/``) stay for audit after rotation or policy
    changes. Only exact counts/bytes are reported; symlinks are never
    followed. The depth-first walk retains one open ``os.scandir`` iterator
    per ancestor (O(tree depth) state, no visit limit or truncation): every
    accessible regular file and its size is counted, ``session.json`` files also count
    as records, per-entry faults and symlinks count as skipped, and a
    missing sessions root reports all zeros.
    """
    root = project.root / "sessions"
    records = 0
    files = 0
    total_bytes = 0
    skipped = 0
    try:
        if root.is_symlink():
            return {"records": 0, "files": 0, "bytes": 0, "skipped": 1}
    except OSError:
        return {"records": 0, "files": 0, "bytes": 0, "skipped": 1}
    try:
        if not root.is_dir():
            return {"records": 0, "files": 0, "bytes": 0, "skipped": 0}
    except OSError:
        return {"records": 0, "files": 0, "bytes": 0, "skipped": 1}
    iterators = []
    try:
        try:
            iterators.append(os.scandir(root))
        except OSError:
            skipped += 1
        while iterators:
            try:
                entry = next(iterators[-1])
            except StopIteration:
                iterators.pop().close()
                continue
            except OSError:
                skipped += 1
                iterators.pop().close()
                continue
            try:
                if entry.is_symlink():
                    skipped += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    # Descend before advancing the parent: never retain siblings.
                    iterators.append(os.scandir(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                total_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                skipped += 1
                continue
            files += 1
            if entry.name == "session.json":
                records += 1
    finally:
        # Also release ancestors on unexpected exceptions. ExitStack attempts
        # every close even if another close fails; callbacks exist only here.
        with contextlib.ExitStack() as cleanup:
            for iterator in iterators:
                cleanup.callback(iterator.close)
    return {"records": records, "files": files, "bytes": total_bytes, "skipped": skipped}


def prune(project: Project, *, apply: bool = False, keep_artifacts: int = DEFAULT_KEEP_ARTIFACTS) -> dict:
    """Preview/apply run retention and existing projection cleanup.

    ``run_retention_days`` (default 31; 0 disables; 0–3650) expires only
    complete, terminal, unreferenced connected components. Lifecycle age is
    validated started_at versus now, never mtime. Typed references, bounds and
    interruption recovery live in storage_runs and docs/configuration.md.
    Existing log clocks use mtime (gzip preserves it); artifact count and web
    TTL policies are unchanged. Snapshots, objects, permanent ledgers and
    sessions are not candidates. No shared smoke artifacts are handled here.

    Every invocation recomputes under workspace, sorted role, editor-ingest,
    insights exclusions, nonblocking and without pause. Apply refuses any
    incomplete reference scan before deletion; dry-run changes no evidence.
    Apply records full bounded intent and completion audits; response lists
    are previews (100 names, 256 serialized bytes each, counts/digests). I/O errors
    propagate, leaving recoverable quarantine intent, never false success.
    """
    if keep_artifacts < 0:
        raise Denied("Artifact retention must not be negative")
    limits = getattr(project.config, "limits", None)
    compress_days = getattr(limits, "event_log_compress_days", 7)
    retention_days = getattr(limits, "event_log_retention_days", 31)
    try:
        compress_days = int(compress_days)
        retention_days = int(retention_days)
    except (TypeError, ValueError) as exc:
        raise Denied(f"Invalid event-log retention: {exc}") from exc
    if compress_days < 0 or retention_days < 0:
        raise Denied("Event-log retention must not be negative")
    candidates = []
    victims: list[Path] = []
    to_compress: list[Path] = []
    to_drop: list[Path] = []
    cache_seconds = _web_cache_seconds(project)
    from . import storage_runs
    with quiescent(project, require_pause=False):
        leftovers = storage_runs.quarantines(project)
        if apply:
            storage_runs.recover_staging(project, leftovers)
            leftovers = storage_runs.quarantines(project)
        run_plan = storage_runs.plan(project)
        run_preview = storage_runs.preview(run_plan)
        run_preview["quarantine_count"] = len(leftovers)
        run_preview["quarantines"] = [p.name for p, _ in leftovers[:storage_runs.PREVIEW_ROWS]]
        run_preview["quarantines_truncated"] = len(leftovers) > storage_runs.PREVIEW_ROWS
        run_preview["restored_component_count"] = sum(p.name.startswith("staging-") for p, _ in leftovers) if apply else 0
        if not apply and any(p.name.startswith("staging-") for p, _ in leftovers):
            run_preview["complete"] = False
            run_preview["reason"] = "Uncommitted quarantine; next apply restores it before rescanning"
        if apply and not run_plan["complete"]:
            raise Denied("Incomplete run-reference scan; prune refused: " + run_plan.get("reason", ""))
        retiring = {run for component in run_plan["components"] for run in component}
        for run_dir in sorted((project.root / "runs").glob("*")):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            if run_dir.name in retiring:
                continue
            finished = (run_dir / "result.json").exists() or (run_dir / "error.json").exists() or (run_dir / "consultation.json").exists()
            if finished and (run_dir / "input").is_dir():
                candidates.append(run_dir / "input")
            raw, compressed = _event_log_paths(run_dir)
            for path in raw:
                if path.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                age = _file_age_days(path)
                if age < 0:
                    continue
                if retention_days and age >= retention_days:
                    to_drop.append(path)
                elif compress_days and age >= compress_days:
                    to_compress.append(path)
            for path in compressed:
                if path.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                age = _file_age_days(path)
                if age < 0:
                    continue
                if retention_days and age >= retention_days:
                    to_drop.append(path)
        # A path selected for the drop stage is never also compressed.
        drop_set = {str(path) for path in to_drop}
        to_compress = [path for path in to_compress if str(path) not in drop_set]
        to_compress.sort()
        to_drop.sort()
        if keep_artifacts:
            victims = artifact_candidates(project, keep_artifacts)
        web_victims, _web_accounting = _web_cache_candidates(project)
        data_base = project.config.data
        removed = {"reproducible_inputs": [p.relative_to(project.root).as_posix() for p in candidates],
                   "artifacts": [p.relative_to(project.root).as_posix() for p in victims],
                   "event_logs_compressed": [p.relative_to(project.root).as_posix() for p in to_compress],
                   "event_logs_removed": [p.relative_to(project.root).as_posix() for p in to_drop],
                   "web_cache": [p.relative_to(data_base).as_posix() for p in web_victims]}
        if apply and len(canonical(removed)) > storage_runs.MAX_BYTES:
            raise Denied("Prune audit exceeds byte bound")
        if apply:
            # Persist the bounded complete plan before any destructive action.
            intent = project.root / "maintenance" / f"prune-runs-{uuid.uuid4().hex}.json"
            write_json(intent, run_plan)
            run_preview["intent"] = intent.relative_to(project.root).as_posix()
            storage_runs.retire(project, run_plan, leftovers)
            for candidate in candidates:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                shutil.rmtree(candidate)
            for candidate in victims:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                shutil.rmtree(candidate)
            compressed_names: list[str] = []
            for candidate in to_compress:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                target = _compress_event_log(candidate)
                compressed_names.append(target.relative_to(project.root).as_posix())
            dropped_names: list[str] = []
            for candidate in to_drop:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                candidate.unlink()
                dropped_names.append(candidate.relative_to(project.root).as_posix())
            for candidate in web_victims:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                candidate.unlink()
            for parent in {c.parent for c in (*candidates, *victims, *to_compress, *to_drop, *web_victims)}:
                sync_dir(parent)
            removed["event_logs_compressed"] = compressed_names
            removed["event_logs_removed"] = dropped_names
            audit = project.root / "maintenance" / f"prune-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
            write_json(audit, {"applied_at": now(), "keep_artifacts": keep_artifacts,
                               "event_log_compress_days": compress_days,
                               "event_log_retention_days": retention_days,
                               "cache_seconds": cache_seconds, "run_retention": run_preview, **removed})
            sync_dir(audit.parent)
            removed["audit"] = audit.relative_to(project.root).as_posix()
    def path_preview(path):
        short = path[:256]
        while len(canonical(short)) > 256:
            short = short[:-1]
        return short

    path_previews = {}
    for key, paths in list(removed.items()):
        if isinstance(paths, list):
            names = [path_preview(p) for p in paths[:storage_runs.PREVIEW_ROWS]]
            path_previews[key] = {"count": len(paths), "sha256": digest(canonical(paths)),
                                  "truncated": len(paths) > len(names) or names != paths}
            removed[key] = names
    return {"applied": apply, "run_retention": run_preview, "path_previews": path_previews, **removed,
            "retained": "Run evidence outside expired, unreferenced complete components; snapshots, "
                        "content objects, sessions, decisions, proposals, insight revisions, and work-item histories. "
                        "Artifact documents beyond the kept count, "
                        "bulky engine logs beyond operator retention, and "
                        "web-cache entries expired past cache_seconds are disposable projections; "
                        "their evidence remains in snapshots and retained run records where available. "
                        "Sessions are durable audit evidence and are accounted, never reclaimed."}


#: Default automatic run-retention cadence (seconds) when configuration
#: omits the policy; the validated `[limits]` value normally applies.
AUTOMATIC_PRUNE_INTERVAL_DEFAULT = 86400
#: Maximum automatic run-retention cadence (31 days, mirroring workspace-cache).
AUTOMATIC_PRUNE_INTERVAL_MAX = 2678400
#: Bound on the automatic-prune due-state file; larger state is unavailable.
_AUTOMATIC_PRUNE_STATE_BYTES = 4096


def _automatic_prune_due(path: Path, instant: int, interval: int) -> bool:
    """True when automatic prune is due; malformed state refuses the check."""
    missing: object = object()
    record = read_json(path, missing, max_bytes=_AUTOMATIC_PRUNE_STATE_BYTES)
    if record is missing:
        return True
    if (not isinstance(record, dict) or type(record.get("checked_at")) is not int
            or not 0 <= record["checked_at"] <= instant):
        raise Denied("Invalid automatic prune maintenance record")
    return instant - record["checked_at"] >= interval


def maintain_automatic_prune(project: Project) -> dict:
    """Attempt one due automatic run-retention sweep, or skip. Never fail the caller.

    Storage-owned counterpart to manual ``prune``: on due, applies the existing
    validated ``prune(project, apply=True, keep_artifacts=0)`` under its
    nonblocking no-pause exclusions, so count-based artifact deletion is never
    enabled implicitly and snapshots/objects/shared smoke bundles are untouched.
    The latest bounded result is retained in
    ``maintenance/automatic-prune.json``. Busy acquisition does not consume the
    cadence. Incomplete scans, malformed references and I/O failures are
    recorded as ``refused``/``unavailable`` without deleting uncertain state
    and without failing the requested run. Call only before the caller acquires
    its own admission/execution locks.
    """
    try:
        limits = getattr(project.config, "limits", None)
        interval = getattr(limits, "automatic_prune_interval_seconds",
                           AUTOMATIC_PRUNE_INTERVAL_DEFAULT)
    except Exception:
        return {"status": "unavailable"}
    if type(interval) is not int or not 0 <= interval <= AUTOMATIC_PRUNE_INTERVAL_MAX:
        return {"status": "unavailable"}
    if interval == 0:
        return {"status": "disabled"}
    state = project.root / "maintenance/automatic-prune.json"
    instant = int(time.time())
    try:
        if not _automatic_prune_due(state, instant, interval):
            return {"status": "not-due"}
        with lock(project.root / "locks/automatic-prune.lock", blocking=False):
            if not _automatic_prune_due(state, instant, interval):
                return {"status": "not-due"}
            result: dict = {"checked_at": instant, "status": "refused"}
            try:
                prune(project, apply=True, keep_artifacts=0)
                result["status"] = "pruned"
            except Busy:
                return {"status": "busy"}
            except (Denied, LimitExceeded, OSError, ValueError) as exc:
                result["status"] = "refused"
                result["error"] = type(exc).__name__
            write_json(state, result)
            return result
    except Busy:
        return {"status": "busy"}
    except (Denied, LimitExceeded, OSError, ValueError):
        return {"status": "unavailable"}


#: Reference-aware storage accounting bounds (fixed mechanism, not knobs):
#: reference JSON files larger than this are skipped and counted, never
#: loaded fully, so one huge run record cannot exhaust the audit.
_AUDIT_JSON_BYTES = 1048576
#: Sample lists stay small; full counts/bytes are always exact.
_AUDIT_SAMPLE = 100


def _audit_digest(value) -> str | None:
    if isinstance(value, str) and DIGEST.fullmatch(value):
        return value
    return None


def _audit_json(path: Path):
    """Bounded JSON read for audit references; None when skipped/unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > _AUDIT_JSON_BYTES:
                return None
        except OSError:
            return None
        record = read_json(path, None)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def _audit_ref_json(root: Path, path: Path, shape: type) -> tuple[dict | list | None, bool]:
    """Bounded, no-follow read; absent optional files are not skipped.

    JSON null is a present wrong-shape record, not an absent file. Pin path
    components through safe_read so a parent swap cannot bypass no-follow.
    """
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            return None, False
        record = json.loads(
            safe_read(root, path.relative_to(root).as_posix(), _AUDIT_JSON_BYTES),
            parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
        if not isinstance(record, shape):
            return None, True
        return record, False
    except Denied as exc:
        # safe_read wraps OS errors; a raced deletion is still absence.
        if isinstance(exc.__cause__, FileNotFoundError):
            return None, False
        return None, True
    except (OSError, ValueError, RecursionError, LimitExceeded):
        return None, True


def _audit_ref_dict(root: Path, path: Path) -> tuple[dict | None, bool]:
    """Read an optional reference dictionary, counting invalid records."""
    return _audit_ref_json(root, path, dict)


def _audit_ref_list(root: Path, path: Path) -> tuple[list | None, bool]:
    """Read a history generation with the same absent/skipped rule."""
    return _audit_ref_json(root, path, list)


def _audit_list_dir(directory: Path, *, required: bool = False) -> tuple[list[Path], bool]:
    """List immediate children without following a symlinked directory.

    Returns ``(paths, scan_skipped)``. A missing optional directory is normal;
    a missing required directory, symlink, non-directory, or failed listing
    hides possible references and reports ``scan_skipped=True``.
    """
    try:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return [], required
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return [], True
        try:
            # glob can suppress listing errors and falsely look like no refs.
            with os.scandir(directory) as entries:
                return sorted(Path(entry.path) for entry in entries), False
        except OSError:
            return [], True
    except OSError:
        return [], True


def _audit_collect(project: Project) -> dict:
    """Full reference-aware collection; internal helper for audit/reclaim.

    Returns exact full sorted ID lists (no truncation) plus sizes; callers
    truncate for display. Same roots, bounds, and symlink/corrupt skipping
    as audit(); see audit() for the contract.
    """
    root = project.root
    acceptance_receipts = acceptance.audit(root)
    manifests: dict[str, dict] = {}
    manifest_bytes = 0
    manifest_skipped = 0
    manifest_objects: dict[str, set[str]] = {}
    snapshots_dir = root / "snapshots"
    snapshot_paths, snapshots_scan_skipped = _audit_list_dir(snapshots_dir, required=True)
    if snapshots_scan_skipped:
        manifest_skipped += 1
        snapshot_paths = []
    else:
        snapshot_paths = [child for child in snapshot_paths if child.suffix == ".json"]
    for child in snapshot_paths:
        try:
            if child.is_symlink():
                manifest_skipped += 1
                continue
            sid = child.stem
            if not DIGEST.fullmatch(sid):
                manifest_skipped += 1
                continue
            try:
                manifest_bytes += child.stat().st_size
            except OSError:
                manifest_skipped += 1
                continue
            record, skipped = _audit_ref_dict(root, child)
            # These files were enumerated as manifests, so even a raced
            # disappearance makes object reachability incomplete.
            if skipped or record is None:
                manifest_skipped += 1
                continue
            project.snapshots.validate_manifest(sid, record)
            manifests[sid] = record
            manifest_objects[sid] = {entry["sha256"] for entry in record["files"].values()}
        except (OSError, Denied, LimitExceeded, ValueError, TypeError, RecursionError):
            manifest_skipped += 1
    # Count unique record/scan paths, including a generation checked both as
    # a durable reference source and as the active publication history.
    skipped_references: set[Path] = set()

    def reference_field(path: Path, record: dict, field: str, ids: set[str], *, required=False):
        value = record.get(field)
        sid = _audit_digest(value)
        if sid is not None:
            ids.add(sid)
        elif value is not None or required:
            skipped_references.add(path)

    def reference_dict(path: Path, field: str, ids: set[str], *, required=False,
                       must_exist=False):
        record, skipped = _audit_ref_dict(root, path)
        if skipped or (must_exist and record is None):
            skipped_references.add(path)
        if record is not None:
            reference_field(path, record, field, ids, required=required)
        return record

    def reference_children(directory: Path):
        paths, skipped = _audit_list_dir(directory)
        if skipped:
            skipped_references.add(directory)
        return paths

    live_ids: set[str] = set()
    current_path = root / "current.json"
    pointer = reference_dict(current_path, "snapshot", live_ids, required=True)
    generation = _audit_digest(pointer.get("history")) if pointer is not None else None
    pointer_snapshot = _audit_digest(pointer.get("snapshot")) if pointer is not None else None
    if pointer is not None and (generation is None or pointer_snapshot is None):
        skipped_references.add(current_path)

    # Continuation seals remain live even after leaving the history window.
    from .continuations import entries as continuation_entries
    if pointer is not None:
        try:
            live_ids.update(row["snapshot"] for row in continuation_entries(pointer))
        except Denied:
            skipped_references.add(current_path)

    history_ids: set[str] = set()
    active_history: list | None = None
    generations = 0
    histories_bytes = 0
    histories_dir = root / "histories"
    for child in reference_children(histories_dir):
        if child.suffix != ".json":
            continue
        try:
            info = child.lstat()
            histories_bytes += info.st_size
        except FileNotFoundError:
            continue
        except OSError:
            skipped_references.add(child)
            continue
        valid_name = _audit_digest(child.stem) is not None
        if valid_name:
            generations += 1
        else:
            skipped_references.add(child)
        record, skipped = _audit_ref_list(root, child)
        if child.stem == generation:
            active_history = record
        if skipped:
            skipped_references.add(child)
        if record is None:
            continue
        for sid in record:
            digest_id = _audit_digest(sid)
            if digest_id is None:
                skipped_references.add(child)
            else:
                history_ids.add(digest_id)
        if child not in skipped_references and (not record or digest(canonical(record)) != child.stem):
            skipped_references.add(child)

    if generation is not None and histories_dir not in skipped_references:
        history_path = histories_dir / (generation + ".json")
        # Missing required generations are incomplete, unlike missing optional
        # run/artifact records. Reuse the bounded read, never _history_ids().
        if active_history is None:
            skipped_references.add(history_path)
        elif pointer_snapshot is not None:
            if not active_history or active_history[-1] != pointer_snapshot:
                skipped_references.add(history_path)
            elif history_path not in skipped_references:
                live_ids |= set(active_history)
    history_ids |= live_ids
    run_ids: set[str] = set()
    for run_dir in reference_children(root / "runs"):
        try:
            info = run_dir.lstat()
            if stat.S_ISLNK(info.st_mode):
                skipped_references.add(run_dir)
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError:
            skipped_references.add(run_dir)
            continue
        for name in ("started.json", "result.json", "consultation.json", "error.json", "prompt_projection.json"):
            reference_dict(run_dir / name, "snapshot", run_ids)
    artifact_ids: set[str] = set()
    artifacts_dir = root / "artifacts"
    artifact_children = reference_children(artifacts_dir)
    # Validate the root before even attempting latest.json: an unsafe parent
    # is one skipped scan, never an opportunity to read outside the project.
    if artifacts_dir not in skipped_references:
        reference_dict(artifacts_dir / "latest.json", "snapshot", artifact_ids, required=True)
    for child in artifact_children:
        try:
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                skipped_references.add(child)
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError:
            skipped_references.add(child)
            continue
        reference_dict(child / "evidence.json", "snapshot", artifact_ids,
                       required=True, must_exist=True)
    insight_ids: set[str] = set()
    for child in reference_children(root / "inbox"):
        if child.suffix == ".json":
            reference_dict(child, "base_snapshot", insight_ids)
    # Archive bodies remain durable references after leaving the live inbox.
    # An unreadable archive refuses maintenance rather than hiding references.
    for reference in insight_archive.snapshot_references(root):
        digest_id = _audit_digest(reference)
        if digest_id is not None:
            insight_ids.add(digest_id)
        elif reference is not None:
            skipped_references.add(root / "insight-archive.sqlite3")
    work_item_ids: set[str] = set()
    work_item_events_skipped = 0
    work_items_dir = root / "work-items"
    try:
        try:
            work_items_info = work_items_dir.lstat()
        except FileNotFoundError:
            work_items_info = None
        if work_items_info is not None:
            if (stat.S_ISLNK(work_items_info.st_mode)
                    or not stat.S_ISDIR(work_items_info.st_mode)):
                work_item_events_skipped += 1
            else:
                with os.scandir(work_items_dir) as item_directories:
                    for item_entry in item_directories:
                        if (item_entry.is_symlink()
                                or not item_entry.is_dir(follow_symlinks=False)):
                            work_item_events_skipped += 1
                            continue
                        try:
                            # A drop-only legacy sidecar is covered evidence,
                            # not a seeded item. Validate it read-only; open
                            # sidecars continue through normal reconstruction.
                            if project.work_items._audit_drop_only(item_entry.name):
                                continue
                            # Immutable append events make the internal read
                            # stable without taking the writer lock; reclaim
                            # calls this again under project quiescence.
                            item_references = set()
                            for record in project.work_items._iter_chain(item_entry.name):
                                # Every historical binding stays replayable, even
                                # after re-binding, dropping or history eviction.
                                delivery = record.get("delivery")
                                if delivery is not None:
                                    item_references.add(delivery["snapshot_id"])
                                if record.get("status") == "done":
                                    digest_id = _audit_digest(record.get("snapshot_id"))
                                    if digest_id is None:
                                        raise Denied("Invalid completed snapshot reference")
                                    item_references.add(digest_id)
                        except (Denied, LimitExceeded, OSError, ValueError):
                            work_item_events_skipped += 1
                            continue
                        work_item_ids.update(item_references)
    except OSError:
        work_item_events_skipped += 1
    referenced_ids = (set(history_ids) | set(run_ids) | set(artifact_ids)
                      | set(insight_ids) | set(work_item_ids))
    unreferenced = sorted(sid for sid in manifests if sid not in referenced_ids)
    unreferenced_bytes = 0
    for sid in unreferenced:
        try:
            unreferenced_bytes += (snapshots_dir / f"{sid}.json").stat().st_size
        except OSError:
            continue
    # Objects referenced by any manifest on disk (including unreferenced
    # manifests). Apply removes exactly this preview set, so a second
    # audit/reclaim converges on objects orphaned by manifest removal.
    live_objects: set[str] = set()
    for shapes in manifest_objects.values():
        live_objects |= shapes
    objects_dir = root / "objects"
    object_paths, objects_scan_skipped = _audit_list_dir(objects_dir)
    object_sizes: dict[str, int] = {}
    objects_skipped = int(objects_scan_skipped)
    for child in object_paths:
        try:
            if child.is_symlink() or not child.is_file() or not DIGEST.fullmatch(child.name):
                objects_skipped += 1
                continue
            try:
                object_sizes[child.name] = child.stat().st_size
            except OSError:
                objects_skipped += 1
        except OSError:
            objects_skipped += 1
    orphan = sorted(name for name in object_sizes if name not in live_objects)
    return {
        "manifests": manifests,
        "manifest_bytes": manifest_bytes,
        "manifest_skipped": manifest_skipped,
        "manifest_objects": manifest_objects,
        "live_ids": live_ids,
        "referenced_ids": referenced_ids,
        "history_ids": history_ids,
        "run_ids": run_ids,
        "artifact_ids": artifact_ids,
        "acceptance_receipts": acceptance_receipts,
        "insight_ids": insight_ids,
        "work_item_ids": work_item_ids,
        "work_item_events_skipped": work_item_events_skipped,
        "reference_records_skipped": len(skipped_references),
        "generations": generations,
        "histories_bytes": histories_bytes,
        "unreferenced": unreferenced,
        "unreferenced_bytes": unreferenced_bytes,
        "live_objects": live_objects,
        "object_sizes": object_sizes,
        "objects_skipped": objects_skipped,
        "orphan": orphan,
    }


def audit(project: Project) -> dict:
    """Report snapshot/object retention accounting; read-only preview only.

    Schema: ``{sessions: {records, files, bytes, skipped},
    web_cache: {entries, bytes, expired, expired_bytes, expired_sample,
    expired_truncated, skipped, cache_seconds},
    snapshots: {manifests, bytes, live, referenced,
    unreferenced, unreferenced_bytes, unreferenced_sample,
    unreferenced_truncated, skipped}, histories: {generations, bytes},
    objects: {count, bytes, referenced_count, referenced_bytes,
    orphan_count, orphan_bytes, orphan_sample, orphan_truncated, skipped},
    references: {history_snapshots, run_snapshots, artifact_snapshots,
    insight_snapshots, work_item_snapshots}, work_item_events_skipped,
    reference_records_skipped, preview_only: True}``. Counts and byte totals
    are exact; sample lists hold at most ``_AUDIT_SAMPLE`` sorted IDs with a
    truncation flag.
    Bounds: reference JSON files above ``_AUDIT_JSON_BYTES`` are skipped
    and counted in ``reference_records_skipped``; symlinks are never
    followed; corrupt entries are skipped, never raised. Missing optional
    run/artifact files are normal and not counted; present-but-unreadable,
    malformed, over-bound, wrong-shape, or failed directory scans are counted
    as skipped. Missing optional run records and an absent latest-artifact
    pointer are normal; an artifact directory without its evidence record is
    incomplete. The count is unique record/scan paths, not failed checks;
    null records, invalid non-null reference IDs, and invalid history names,
    entries, digests, or active endpoints are incomplete. Usable valid IDs
    are still reported, but an incomplete scan
    is never proof that references are absent. Manifest ``files`` entries
    with invalid digests are skipped. An unreadable insight archive raises
    rather than hiding references. Trust: local recorded state only (data,
    not proof); insight prose is never read, only ``base_snapshot`` IDs.
    Retry/cancellation: read-only, lock-free, idempotent; safe to retry
    while writers run, with no quiescence requirement because nothing is
    removed.
    Evidence: exact counts/bytes plus bounded samples; full evidence stays
    on disk under ``snapshots/``, ``histories/`` and ``objects/``.
    Failure: raises only when the project or its store is unreadable (or the
    insight archive is unreadable); per-file faults are counted as skipped.
    Nothing is deleted, moved, or rewritten by this preview.
    """
    collected = _audit_collect(project)
    manifests = collected["manifests"]
    live_ids = collected["live_ids"]
    referenced_ids = collected["referenced_ids"]
    unreferenced = collected["unreferenced"]
    live_objects = collected["live_objects"]
    object_sizes = collected["object_sizes"]
    orphan = collected["orphan"]
    try:
        sessions = _session_accounting(project)
    except (OSError, ValueError):
        sessions = {"records": 0, "files": 0, "bytes": 0, "skipped": 0}
    try:
        _web_victims, web_cache = _web_cache_candidates(project)
    except (OSError, ValueError):
        web_cache = {"entries": 0, "bytes": 0, "expired": 0, "expired_bytes": 0,
                     "expired_sample": [], "expired_truncated": False, "skipped": 0,
                     "cache_seconds": 1800}
    return {
        "acceptance_receipts": collected["acceptance_receipts"],
        "sessions": sessions,
        "web_cache": web_cache,
        "snapshots": {
            "manifests": len(manifests),
            "bytes": collected["manifest_bytes"],
            "live": len([sid for sid in manifests if sid in live_ids]),
            "referenced": len([sid for sid in manifests if sid in referenced_ids]),
            "unreferenced": len(unreferenced),
            "unreferenced_bytes": collected["unreferenced_bytes"],
            "unreferenced_sample": unreferenced[:_AUDIT_SAMPLE],
            "unreferenced_truncated": len(unreferenced) > _AUDIT_SAMPLE,
            "skipped": collected["manifest_skipped"],
        },
        "histories": {"generations": collected["generations"], "bytes": collected["histories_bytes"]},
        "objects": {
            "count": len(object_sizes),
            "bytes": sum(object_sizes.values()),
            "referenced_count": len([name for name in object_sizes if name in live_objects]),
            "referenced_bytes": sum(size for name, size in object_sizes.items() if name in live_objects),
            "orphan_count": len(orphan),
            "orphan_bytes": sum(object_sizes[name] for name in orphan),
            "orphan_sample": orphan[:_AUDIT_SAMPLE],
            "orphan_truncated": len(orphan) > _AUDIT_SAMPLE,
            "skipped": collected["objects_skipped"],
        },
        "references": {
            "history_snapshots": len(collected["history_ids"]),
            "run_snapshots": len(collected["run_ids"]),
            "artifact_snapshots": len(collected["artifact_ids"]),
            "insight_snapshots": len(collected["insight_ids"]),
            "work_item_snapshots": len(collected["work_item_ids"]),
        },
        "work_item_events_skipped": collected["work_item_events_skipped"],
        "reference_records_skipped": collected["reference_records_skipped"],
        "preview_only": True,
        "retained": "All manifests, history generations, objects, runs, artifacts, sessions, decisions, proposals, insight revisions, and work-item histories are retained; this preview removes nothing. "
                    "Web-cache entries expired past cache_seconds are disposable (see sessions/web_cache); "
                    "sessions are durable audit evidence and are accounted, never reclaimed.",
    }


def reclaim(project: Project, *, apply: bool = False) -> dict:
    """List (or apply) removal of unreferenced manifests and orphan objects.

    Schema: ``{applied, snapshots: [relative manifest paths], objects:
    [relative object paths], unreferenced_bytes, orphan_bytes,
    manifest_skipped, work_item_events_skipped, reference_records_skipped,
    audit?}``.
    The candidate sets are exactly what ``audit()`` reports in full: manifests
    outside every durable reference (live/history/run/artifact/insight/work-item) and
    objects referenced by no manifest on disk. Full ID lists are returned
    (sorted); use ``audit()`` for the bounded display samples.
    Bounds: same bounded reads, digest checks, and symlink skipping as
    ``audit()``; dry-run lists known candidates and reports skipped manifests
    and skipped reference records. Apply refuses if any manifest or any
    reference record scan was skipped because manifest or object reachability
    is unknown. Missing optional run/artifact files are normal and not
    counted; present-but-unreadable, malformed, over-bound, wrong-shape, or
    failed directory scans set ``reference_records_skipped`` and refuse
    apply. History generations are never candidates, nor are runs, artifacts,
    sessions, decisions, proposals, insight revisions, or work-item histories.
    Trust: operator-invoked maintenance; the explicit ``--apply`` invocation
    behind quiescence (paused + locks) is the grant. Candidates are
    recomputed inside the quiescent section, so a stale preview can never
    authorize a removal. If work-item history was unreadable, apply refuses
    to reclaim because completion references may be hidden.
    Retry/cancellation: dry-run (default) is read-only, lock-free, and
    idempotent. Apply writes a ``maintenance/storage-*.json`` audit record;
    a second reclaim converges on objects orphaned by manifest removal.
    Evidence: returns removed/retained paths plus the maintenance audit path.
    Failure: Denied for skipped snapshot manifests, skipped reference
    records, unsafe work-item history or storage roots, symlinks at removal time, or a
    non-quiescent project when applying; an unreadable insight archive
    raises rather than hiding references.
    """
    if not apply:
        collected = _audit_collect(project)
        return {
            "applied": False,
            "snapshots": [f"snapshots/{sid}.json" for sid in collected["unreferenced"]],
            "objects": [f"objects/{name}" for name in collected["orphan"]],
            "unreferenced_bytes": collected["unreferenced_bytes"],
            "orphan_bytes": sum(collected["object_sizes"][name] for name in collected["orphan"]),
            "manifest_skipped": collected["manifest_skipped"],
            "retained": "All live/history/run/artifact/insight/work-item-referenced manifests, history generations, "
                        "runs, artifacts, sessions, decisions, proposals, insight revisions, and work-item histories. "
                        "Repeat audit/reclaim converges on newly orphaned objects.",
            "work_item_events_skipped": collected["work_item_events_skipped"],
            "reference_records_skipped": collected["reference_records_skipped"],
        }
    with quiescent(project):
        collected = _audit_collect(project)
        if collected["manifest_skipped"]:
            raise Denied("Skipped snapshot manifest; cannot safely determine object references")
        if collected["reference_records_skipped"]:
            raise Denied("Skipped reference records; cannot safely reclaim snapshot references")
        if collected["work_item_events_skipped"]:
            raise Denied("Unreadable work-item history; cannot safely reclaim snapshot references")
        for directory in (project.root / "snapshots", project.root / "objects"):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Denied("Cannot safely inspect storage directory before reclamation") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise Denied("Refusing a symlink or non-directory storage root during reclamation")
        manifest_paths = [project.root / "snapshots" / f"{sid}.json" for sid in collected["unreferenced"]]
        object_paths = [project.root / "objects" / name for name in collected["orphan"]]
        for path in (*manifest_paths, *object_paths):
            if path.is_symlink():
                raise Denied("Refusing a symlink during storage reclamation")
        removed = {"snapshots": [p.relative_to(project.root).as_posix() for p in manifest_paths],
                   "objects": [p.relative_to(project.root).as_posix() for p in object_paths]}
        for path in manifest_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path in object_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for parent in {p.parent for p in (*manifest_paths, *object_paths)}:
            try:
                sync_dir(parent)
            except OSError:
                pass
        record = project.root / "maintenance" / f"storage-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
        write_json(record, {"applied_at": now(), **removed,
                            "unreferenced_bytes": collected["unreferenced_bytes"],
                            "orphan_bytes": sum(collected["object_sizes"][name] for name in collected["orphan"]),
                            "work_item_snapshots": len(collected["work_item_ids"]),
                            "manifest_skipped": 0,
                            "work_item_events_skipped": 0,
                            "reference_records_skipped": 0})
        sync_dir(record.parent)
        removed["audit"] = record.relative_to(project.root).as_posix()
    return {"applied": True, **removed,
            "retained": "All live/history/run/artifact/insight/work-item-referenced manifests, history generations, "
                        "runs, artifacts, sessions, decisions, proposals, insight revisions, and work-item histories. "
                        "Repeat audit/reclaim converges on newly orphaned objects.",
            "manifest_skipped": 0,
            "work_item_events_skipped": 0,
            "reference_records_skipped": 0}


def artifact_candidates(project: Project, keep: int) -> list[Path]:
    """Oldest published artifacts beyond the kept count. The live pointer target is never a candidate."""
    root = project.root / "artifacts"
    latest = read_json(root / "latest.json", {})
    live = latest.get("artifact") if isinstance(latest, dict) else None
    dated = []
    for child in sorted(root.glob("*")):
        if not child.is_dir() or child.is_symlink() or child.name == live:
            continue
        evidence = read_json(child / "evidence.json", {})
        published = evidence.get("published_at") if isinstance(evidence, dict) else None
        if not published:
            continue
        dated.append((published, child))
    dated.sort(key=lambda item: item[0])
    # The keep-count includes the live target; reserve its slot only when the
    # pointer aims at an existing entry (a missing pointer reserves nothing).
    live_entry = root / live if isinstance(live, str) else None
    reserve = 1 if live_entry is not None and live_entry.is_dir() and not live_entry.is_symlink() else 0
    drop = max(0, len(dated) - keep + reserve)
    return [child for _, child in dated[:drop]]


def restore(config, name: str, archive_path: Path, *, max_bytes: int = 1073741824) -> dict:
    """Restore a checkpoint into a NEW, unarmed project without extracting links.

    Backups carry code and evidence, not Git history, credentials, global policy,
    or the shared budget. Resource limits also apply to malicious archives.
    """
    identifier(name)
    if max_bytes < 1:
        raise Denied("Restore byte limit must be positive")
    parent = config.data / "projects"
    mkdir(parent)
    destination = parent / name
    allowed = set(MEMBERS) | {"backup.json"}
    with lock(config.data / "locks" / f"init-{name}.lock", blocking=False):
        if destination.exists():
            raise Denied("Restore never overwrites an existing project")
        stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=parent))
        try:
            total = count = 0
            seen = set()
            with tarfile.open(archive_path, "r:gz") as archive:
                for member in archive:
                    relative = Path(member.name)
                    parts = relative_parts(member.name.rstrip("/") if member.isdir() else member.name)
                    if (relative.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts)
                            or "\\" in member.name or parts[0] not in allowed
                            or not (member.isfile() or member.isdir()) or member.name in seen):
                        raise Denied("Unsafe or duplicate backup member")
                    seen.add(member.name)
                    count += 1
                    total += member.size
                    if count > 100000 or member.size < 0 or total > max_bytes:
                        raise Denied("Backup exceeds restore limits")
                    target = stage / relative
                    if member.isdir():
                        mkdir(target)
                        continue
                    mkdir(target.parent)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise Denied("Backup file has no content")
                    with stream, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            block = stream.read(min(1048576, remaining))
                            if not block:
                                raise Denied("Truncated backup member")
                            output.write(block)
                            remaining -= len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    with contextlib.suppress(OSError, AttributeError, NotImplementedError):
                        target.chmod(0o600)
            acceptance.audit(stage)
            metadata = read_json(stage / "backup.json")
            if not isinstance(metadata, dict):
                raise Denied("Invalid backup metadata")
            store = open_store(stage, config)
            checkpoint = store.get(metadata["checkpoint"])
            # get() verifies the manifest; materialize() verifies every code object.
            store.materialize(checkpoint, stage / "workspace")
            if checkpoint.get("skipped"):
                raise Denied("Checkpoint omitted unsupported filesystem objects")
            # Parse operator-owned fields before publishing a usable project.
            settings = tomllib.loads((stage / "project.toml").read_text(encoding="utf-8"))
            keys(settings, {"roles", "verify", "attributes"}, "project")
            from .selection import attributes
            attributes(settings.get("attributes", {}))
            roles = strings(settings.get("roles", []), "project.roles")
            check_verify(settings.get("verify", []))
            if not roles or any(r not in config.roles for r in roles):
                raise Denied("Backup roles are not configured on this installation")
            read_goal(stage / "PROJECT.md", "Restored goal must be nonempty and at most 64 KiB")
            # Validate every published history reference before exposing the project.
            for sid in store._history_ids():
                historical = store.get(sid)
                for path in historical["files"]:
                    store.read(historical, path)
            write_json(stage / "control.json", {"armed": False, "paused": True,
                       "draining": False,
                       "wake_generation": uuid.uuid4().hex, "reason": "Restored checkpoint; operator review required"})
            atomic_write(stage / "spool/editor/.mizu-outbox", b"Mizu Editor proposal outbox\n")
            store.publish(checkpoint)
            from .smoke_refs import shared_lock
            from .smoke_retention import validate_restored_refs
            with shared_lock(config.data, blocking=True):
                validate_restored_refs(config.data, stage)
                os.rename(stage, destination)
                sync_dir(parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"project": name, "checkpoint": checkpoint["id"], "armed": False, "paused": True,
            "note": "Review restored goals, acceptance commands, policies and evidence before arming. Git history is not included."}
