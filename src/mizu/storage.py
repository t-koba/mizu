"""Conservative maintenance: backups at a quiescent boundary and safe pruning.

Evidence, snapshot manifests, objects and persistent sessions are never silently
removed. Pruning removes only reproducible run inputs, old artifact documents,
bulky per-run engine logs, disposable dashboard generations, and expired
web-cache entries under operator-set retention; result, error,
consultation, started, selection, and usage records are retained.
Sessions are durable audit evidence: they are accounted, never reclaimed.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from .config import keys, strings
from .errors import Denied
from .fs import DIGEST, atomic_write, identifier, lock, mkdir, now, read_json, sync_dir, write_json, relative_parts
from .project import Project, check_verify, init_managed_repo, read_goal
from .snapshot import open_store

#: Project members covered by backup/restore (plus backup.json on restore).
MEMBERS = ("project.toml", "PROJECT.md", "control.json", "current.json", "snapshots", "objects",
           "inbox", "insight-ids", "histories", ".ingest", "decisions", "decision-history", "runs", "sessions", "artifacts", "health", "observed",
           "maintenance", "spool", "selection")


@contextlib.contextmanager
def quiescent(project: Project):
    if not project.control().get("paused"):
        raise Denied("Pause the project before storage maintenance")
    with contextlib.ExitStack() as stack:
        stack.enter_context(lock(project.root / "locks" / "workspace.lock", blocking=False))
        for role in sorted(project.roles):
            stack.enter_context(lock(project.root / "locks" / f"run-{role}.lock", blocking=False))
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
            with temporary.open("w+b") as raw:
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
                with tarfile.open(destination, "r|gz") as archive:
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

#: Bulky per-run engine logs eligible for retention (raw names). Compressed
#: ``.gz`` siblings are eligible for the drop stage only. Every other run
#: file (result/error/consultation/started/selection/admission/usage records,
#: prompts, sessions, sources) is never a candidate.
_EVENT_LOG_RAW = ("diagnostics.txt",)


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


#: Default dashboard generation retention ([limits] dashboard_keep fallback).
#: The live ``latest.json`` target is always protected; 0 keeps all.
DEFAULT_KEEP_DASHBOARD = 30


def _dashboard_keep(project: Project) -> int:
    """Operator-selected dashboard generation keep-count (0 keeps all)."""
    limits = getattr(project.config, "limits", None)
    keep = getattr(limits, "dashboard_keep", DEFAULT_KEEP_DASHBOARD)
    try:
        keep = int(keep)
    except (TypeError, ValueError) as exc:
        raise Denied(f"Invalid dashboard retention: {exc}") from exc
    if keep < 0:
        raise Denied("Dashboard retention must not be negative")
    return keep


def dashboard_candidates(project: Project, keep: int) -> list[Path]:
    """Oldest dashboard generations beyond the kept count.

    The live ``latest.json`` target is never a candidate; only
    content-addressed ``<sha256>.json`` regular files are considered.
    ``keep`` counts the live target when it exists (0 keeps all).
    Symlinks, ``latest.json``, ``index.html``, locks, and non-digest names
    are never candidates.
    """
    if keep <= 0:
        return []
    root = project.root / "dashboard"
    try:
        pointer = read_json(root / "latest.json", {})
    except (OSError, ValueError):
        pointer = {}
    live = pointer.get("dashboard") if isinstance(pointer, dict) else None
    if not (isinstance(live, str) and DIGEST.fullmatch(live)):
        live = None
    dated: list[tuple[float, Path]] = []
    try:
        children = sorted(root.glob("*.json")) if root.is_dir() else []
    except OSError:
        return []
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                continue
            if child.name == "latest.json" or not DIGEST.fullmatch(child.stem):
                continue
            if live is not None and child.name == f"{live}.json":
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            dated.append((mtime, child))
        except OSError:
            continue
    dated.sort(key=lambda item: (item[0], item[1].name))
    live_entry = root / f"{live}.json" if live is not None else None
    reserve = 0
    try:
        if live_entry is not None and live_entry.is_file() and not live_entry.is_symlink():
            reserve = 1
    except OSError:
        reserve = 0
    drop = max(0, len(dated) - keep + reserve)
    return [child for _, child in dated[:drop]]


def _dashboard_accounting(project: Project) -> dict:
    """Capacity accounting for disposable dashboard generations."""
    root = project.root / "dashboard"
    try:
        keep = _dashboard_keep(project)
    except Denied:
        keep = DEFAULT_KEEP_DASHBOARD
    try:
        pointer = read_json(root / "latest.json", {})
    except (OSError, ValueError):
        pointer = {}
    live = pointer.get("dashboard") if isinstance(pointer, dict) else None
    if not (isinstance(live, str) and DIGEST.fullmatch(live)):
        live = None
    documents = 0
    total_bytes = 0
    skipped = 0
    try:
        children = sorted(root.glob("*.json")) if root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                skipped += 1
                continue
            if child.name == "latest.json" or not DIGEST.fullmatch(child.stem):
                skipped += 1
                continue
            try:
                total_bytes += child.stat().st_size
            except OSError:
                skipped += 1
                continue
            documents += 1
        except OSError:
            skipped += 1
    try:
        victims = dashboard_candidates(project, keep)
    except Denied:
        victims = []
    victim_names = sorted(f"dashboard/{p.name}" for p in victims)
    victim_bytes = 0
    for path in victims:
        try:
            victim_bytes += path.stat().st_size
        except OSError:
            continue
    return {
        "documents": documents,
        "bytes": total_bytes,
        "live": live,
        "keep": keep,
        "candidates": len(victims),
        "candidate_bytes": victim_bytes,
        "candidate_sample": victim_names[:_AUDIT_SAMPLE],
        "candidate_truncated": len(victim_names) > _AUDIT_SAMPLE,
        "skipped": skipped,
    }


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
    followed.
    """
    root = project.root / "sessions"
    records = 0
    files = 0
    total_bytes = 0
    skipped = 0
    try:
        paths = sorted(root.rglob("*")) if root.is_dir() else []
    except OSError:
        paths = []
    # Bound the walk so one huge session tree cannot exhaust the audit.
    for child in paths[:100000]:
        try:
            if child.is_symlink():
                skipped += 1
                continue
            if not child.is_file():
                continue
            try:
                total_bytes += child.stat().st_size
            except OSError:
                skipped += 1
                continue
            files += 1
            if child.name == "session.json":
                records += 1
        except OSError:
            skipped += 1
    truncated = len(paths) > 100000
    if truncated:
        skipped += len(paths) - 100000
    return {"records": records, "files": files, "bytes": total_bytes, "skipped": skipped}


def prune(project: Project, *, apply: bool = False, keep_artifacts: int = DEFAULT_KEEP_ARTIFACTS) -> dict:
    """List (or apply) removal of reproducible inputs, old artifacts, bulky logs, dashboard generations, and expired web cache.

    Schema/bounds: ``keep_artifacts`` counts the live pointer when it exists.
    ``[limits] event_log_compress_days`` (0 disables, 0-3650) gzips raw
    ``*-events.jsonl``/``diagnostics.txt``/``*-events-truncated.json`` at or
    beyond N days old;
    ``[limits] event_log_retention_days`` (0 disables, 0-3650) drops raw and
    ``.gz`` logs at or beyond M days old. ``[limits] dashboard_keep``
    (0 keeps all, 0-1000) keeps the newest N dashboard generations plus the
    live ``latest.json`` target; older content-addressed generations are
    disposable candidates. ``[web] cache_seconds`` decides web-cache expiry
    from ``retrieved_epoch`` age (entries without a usable epoch fall back to mtime).
    Age is file mtime vs now unless noted; the compressed copy keeps the raw
    mtime so the drop clock does not restart.
    Only top-level run-dir logs, non-live dashboard generations, and expired
    web-cache entries are candidates; result/error/consultation/
    started/selection/admission/usage records, snapshots, objects, sessions,
    decisions, and proposals are never candidates.
    Trust: operator-invoked maintenance under quiescence (paused + locks);
    paths are local recorded state, never model input. The web cache is
    shared under ``data_dir``; the invoking project's TTL decides expiry and
    removal is best-effort (a concurrent fetch recreates the entry).
    Retry: dry-run by default; apply writes a maintenance audit record.
    Evidence: returns candidates + audit path. Failure: Denied for negative
    retention, symlinks, or non-quiescent state.
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
    dashboard_keep = _dashboard_keep(project)
    cache_seconds = _web_cache_seconds(project)
    with quiescent(project):
        for run_dir in sorted((project.root / "runs").glob("*")):
            if not run_dir.is_dir() or run_dir.is_symlink():
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
        dashboard_victims = dashboard_candidates(project, dashboard_keep)
        web_victims, _web_accounting = _web_cache_candidates(project)
        data_base = project.config.data
        removed = {"reproducible_inputs": [p.relative_to(project.root).as_posix() for p in candidates],
                   "artifacts": [p.relative_to(project.root).as_posix() for p in victims],
                   "event_logs_compressed": [p.relative_to(project.root).as_posix() for p in to_compress],
                   "event_logs_removed": [p.relative_to(project.root).as_posix() for p in to_drop],
                   "dashboard_generations": [p.relative_to(project.root).as_posix() for p in dashboard_victims],
                   "web_cache": [p.relative_to(data_base).as_posix() for p in web_victims]}
        if apply:
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
            for candidate in dashboard_victims:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                candidate.unlink()
            for candidate in web_victims:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                candidate.unlink()
            for parent in {c.parent for c in (*candidates, *victims, *to_compress, *to_drop, *dashboard_victims, *web_victims)}:
                try:
                    sync_dir(parent)
                except OSError:
                    pass
            removed["event_logs_compressed"] = compressed_names
            removed["event_logs_removed"] = dropped_names
            audit = project.root / "maintenance" / f"prune-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
            write_json(audit, {"applied_at": now(), "keep_artifacts": keep_artifacts,
                               "dashboard_keep": dashboard_keep,
                               "event_log_compress_days": compress_days,
                               "event_log_retention_days": retention_days,
                               "cache_seconds": cache_seconds, **removed})
            sync_dir(audit.parent)
            removed["audit"] = audit.relative_to(project.root).as_posix()
    return {"applied": apply, **removed,
            "retained": "All result/error/consultation/started/selection/usage records, snapshots, "
                        "content objects, sessions, decisions, and proposals. "
                        "Artifact documents beyond the kept count, dashboard generations beyond "
                        "dashboard_keep, bulky engine logs beyond operator retention, and "
                        "web-cache entries expired past cache_seconds are disposable projections; "
                        "their evidence remains in snapshots and run records. "
                        "Sessions are durable audit evidence and are accounted, never reclaimed."}


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


def _audit_collect(project: Project) -> dict:
    """Full reference-aware collection; internal helper for audit/reclaim.

    Returns exact full sorted ID lists (no truncation) plus sizes; callers
    truncate for display. Same roots, bounds, and symlink/corrupt skipping
    as audit(); see audit() for the contract.
    """
    root = project.root
    manifests: dict[str, dict] = {}
    manifest_bytes = 0
    manifest_skipped = 0
    manifest_objects: dict[str, set[str]] = {}
    snapshots_dir = root / "snapshots"
    try:
        snapshot_paths = sorted(snapshots_dir.glob("*.json")) if snapshots_dir.is_dir() else []
    except OSError:
        snapshot_paths = []
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
            record = _audit_json(child)
            if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
                manifest_skipped += 1
                continue
            manifests[sid] = record
            shapes: set[str] = set()
            for entry in record["files"].values():
                if not isinstance(entry, dict):
                    continue
                sha = _audit_digest(entry.get("sha256"))
                if sha is not None:
                    shapes.add(sha)
            manifest_objects[sid] = shapes
        except OSError:
            manifest_skipped += 1
    history_ids: set[str] = set()
    generations = 0
    histories_bytes = 0
    histories_dir = root / "histories"
    try:
        history_paths = sorted(histories_dir.glob("*.json")) if histories_dir.is_dir() else []
    except OSError:
        history_paths = []
    for child in history_paths:
        try:
            if child.is_symlink() or not DIGEST.fullmatch(child.stem):
                continue
            try:
                histories_bytes += child.stat().st_size
            except OSError:
                continue
            generations += 1
            try:
                if child.stat().st_size <= _AUDIT_JSON_BYTES:
                    record = read_json(child, None)
                else:
                    record = None
            except (OSError, ValueError):
                record = None
            if isinstance(record, list):
                for sid in record:
                    digest_id = _audit_digest(sid)
                    if digest_id is not None:
                        history_ids.add(digest_id)
        except OSError:
            continue
    live_ids: set[str] = set()
    try:
        live_ids = set(project.snapshots._history_ids())
    except Exception:
        try:
            pointer = read_json(root / "current.json", {})
            if isinstance(pointer, dict):
                digest_id = _audit_digest(pointer.get("snapshot"))
                if digest_id is not None:
                    live_ids = {digest_id}
        except (OSError, ValueError):
            live_ids = set()
    history_ids |= live_ids
    run_ids: set[str] = set()
    try:
        run_dirs = sorted((root / "runs").glob("*")) if (root / "runs").is_dir() else []
    except OSError:
        run_dirs = []
    for run_dir in run_dirs:
        try:
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
        except OSError:
            continue
        for name in ("started.json", "result.json", "consultation.json", "error.json", "prompt_projection.json"):
            record = _audit_json(run_dir / name)
            if record is None:
                continue
            digest_id = _audit_digest(record.get("snapshot"))
            if digest_id is not None:
                run_ids.add(digest_id)
    artifact_ids: set[str] = set()
    try:
        pointer = read_json(root / "artifacts" / "latest.json", {})
        if isinstance(pointer, dict):
            digest_id = _audit_digest(pointer.get("snapshot"))
            if digest_id is not None:
                artifact_ids.add(digest_id)
    except (OSError, ValueError):
        pass
    try:
        artifact_dirs = sorted((root / "artifacts").glob("*")) if (root / "artifacts").is_dir() else []
    except OSError:
        artifact_dirs = []
    for child in artifact_dirs:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
            record = _audit_json(child / "evidence.json")
            if record is None:
                continue
            digest_id = _audit_digest(record.get("snapshot"))
            if digest_id is not None:
                artifact_ids.add(digest_id)
        except OSError:
            continue
    insight_ids: set[str] = set()
    try:
        inbox_paths = sorted((root / "inbox").glob("*.json")) if (root / "inbox").is_dir() else []
    except OSError:
        inbox_paths = []
    for child in inbox_paths:
        try:
            if child.is_symlink():
                continue
            record = _audit_json(child)
            if record is None:
                continue
            digest_id = _audit_digest(record.get("base_snapshot"))
            if digest_id is not None:
                insight_ids.add(digest_id)
        except OSError:
            continue
    referenced_ids = set(history_ids) | set(run_ids) | set(artifact_ids) | set(insight_ids)
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
    try:
        object_paths = sorted(objects_dir.glob("*")) if objects_dir.is_dir() else []
    except OSError:
        object_paths = []
    object_sizes: dict[str, int] = {}
    objects_skipped = 0
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
        "insight_ids": insight_ids,
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

    Schema: ``{dashboard: {documents, bytes, live, keep, candidates,
    candidate_bytes, candidate_sample, candidate_truncated, skipped},
    sessions: {records, files, bytes, skipped},
    web_cache: {entries, bytes, expired, expired_bytes, expired_sample,
    expired_truncated, skipped, cache_seconds},
    snapshots: {manifests, bytes, live, referenced,
    unreferenced, unreferenced_bytes, unreferenced_sample,
    unreferenced_truncated, skipped}, histories: {generations, bytes},
    objects: {count, bytes, referenced_count, referenced_bytes,
    orphan_count, orphan_bytes, orphan_sample, orphan_truncated, skipped},
    references: {history_snapshots, run_snapshots, artifact_snapshots,
    insight_snapshots}, preview_only: True}``. Counts and byte totals are
    exact; sample lists hold at most ``_AUDIT_SAMPLE`` sorted IDs with a
    truncation flag.
    Bounds: reference JSON files above ``_AUDIT_JSON_BYTES`` are skipped
    and counted; symlinks are never followed; corrupt entries are skipped,
    never raised. Manifest ``files`` entries with invalid digests are
    skipped. Trust: local recorded state only (data, not proof); insight
    prose is never read, only ``base_snapshot`` IDs. Retry/cancellation:
    read-only, lock-free, idempotent; safe to retry while writers run, with
    no quiescence requirement because nothing is removed.
    Evidence: exact counts/bytes plus bounded samples; full evidence stays
    on disk under ``snapshots/``, ``histories/`` and ``objects/``.
    Failure: raises only when the project or its store is unreadable;
    per-file faults are counted as skipped. Nothing is deleted, moved, or
    rewritten by this preview.
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
        dashboard = _dashboard_accounting(project)
    except (OSError, ValueError):
        dashboard = {"documents": 0, "bytes": 0, "live": None, "keep": DEFAULT_KEEP_DASHBOARD,
                     "candidates": 0, "candidate_bytes": 0, "candidate_sample": [],
                     "candidate_truncated": False, "skipped": 0}
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
        "dashboard": dashboard,
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
        },
        "preview_only": True,
        "retained": "All manifests, history generations, objects, runs, artifacts, sessions, decisions, and proposals are retained; this preview removes nothing. "
                    "Dashboard generations beyond dashboard_keep and web-cache entries expired past cache_seconds are disposable (see dashboard/sessions/web_cache); "
                    "sessions are durable audit evidence and are accounted, never reclaimed.",
    }


def reclaim(project: Project, *, apply: bool = False) -> dict:
    """List (or apply) removal of unreferenced manifests and orphan objects.

    Schema: ``{applied, snapshots: [relative manifest paths], objects:
    [relative object paths], unreferenced_bytes, orphan_bytes, audit?}``.
    The candidate sets are exactly what ``audit()`` reports in full: manifests
    outside every durable reference (live/history/run/artifact/insight) and
    objects referenced by no manifest on disk. Full ID lists are returned
    (sorted); use ``audit()`` for the bounded display samples.
    Bounds: same bounded reads, digest checks, and symlink skipping as
    ``audit()``; history generations are never candidates, and neither are
    runs, artifacts, sessions, decisions, or proposals.
    Trust: operator-invoked maintenance; the explicit ``--apply`` invocation
    behind quiescence (paused + locks) is the grant. Candidates are
    recomputed inside the quiescent section, so a stale preview can never
    authorize a removal.
    Retry/cancellation: dry-run (default) is read-only, lock-free, and
    idempotent. Apply writes a ``maintenance/storage-*.json`` audit record;
    a second reclaim converges on objects orphaned by manifest removal.
    Evidence: returns removed/retained paths plus the maintenance audit path.
    Failure: Denied for symlinks at removal time or a non-quiescent project
    when applying; per-file read faults are skipped, never raised.
    """
    if not apply:
        collected = _audit_collect(project)
        return {
            "applied": False,
            "snapshots": [f"snapshots/{sid}.json" for sid in collected["unreferenced"]],
            "objects": [f"objects/{name}" for name in collected["orphan"]],
            "unreferenced_bytes": collected["unreferenced_bytes"],
            "orphan_bytes": sum(collected["object_sizes"][name] for name in collected["orphan"]),
            "retained": "All live/history/run/artifact/insight-referenced manifests, history generations, "
                        "runs, artifacts, sessions, decisions, and proposals. "
                        "Repeat audit/reclaim converges on newly orphaned objects.",
        }
    with quiescent(project):
        collected = _audit_collect(project)
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
                            "orphan_bytes": sum(collected["object_sizes"][name] for name in collected["orphan"])})
        sync_dir(record.parent)
        removed["audit"] = record.relative_to(project.root).as_posix()
    return {"applied": True, **removed,
            "retained": "All live/history/run/artifact/insight-referenced manifests, history generations, "
                        "runs, artifacts, sessions, decisions, and proposals. "
                        "Repeat audit/reclaim converges on newly orphaned objects."}


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
            with tarfile.open(archive_path, "r|gz") as archive:
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
            init_managed_repo(stage / "workspace")
            write_json(stage / "control.json", {"armed": False, "paused": True,
                       "wake_generation": uuid.uuid4().hex, "reason": "Restored checkpoint; operator review required"})
            atomic_write(stage / "spool/editor/.mizu-outbox", b"Mizu Editor proposal outbox\n")
            store.publish(checkpoint)
            os.rename(stage, destination)
            sync_dir(parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"project": name, "checkpoint": checkpoint["id"], "armed": False, "paused": True,
            "note": "Review restored goals, acceptance commands, policies and evidence before arming. Git history is not included."}
