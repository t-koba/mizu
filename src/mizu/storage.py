"""Conservative maintenance: backups at a quiescent boundary and safe pruning.

Evidence, snapshot manifests, objects and persistent sessions are never silently
removed. Pruning removes only reproducible run inputs; evidence is retained.
"""
from __future__ import annotations

import contextlib
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
from .fs import atomic_write, identifier, lock, mkdir, now, read_json, sync_dir, write_json, relative_parts
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


def prune(project: Project, *, apply: bool = False, keep_artifacts: int = DEFAULT_KEEP_ARTIFACTS) -> dict:
    """List (or apply) removal of reproducible inputs and old artifact docs.

    Schema/bounds: ``keep_artifacts`` counts the live pointer when it exists.
    Trust: operator-invoked maintenance under quiescence (paused + locks).
    Retry: dry-run by default; apply writes a maintenance audit record.
    Evidence: returns candidates + audit path. Failure: Denied for negative
    retention, symlinks, or non-quiescent state. Snapshots/objects/runs/
    decisions/proposals are never candidates.
    """
    if keep_artifacts < 0:
        raise Denied("Artifact retention must not be negative")
    candidates = []
    victims: list[Path] = []
    with quiescent(project):
        for run_dir in sorted((project.root / "runs").glob("*")):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            finished = (run_dir / "result.json").exists() or (run_dir / "error.json").exists() or (run_dir / "consultation.json").exists()
            if finished and (run_dir / "input").is_dir():
                candidates.append(run_dir / "input")
        if keep_artifacts:
            victims = artifact_candidates(project, keep_artifacts)
        removed = {"reproducible_inputs": [str(p.relative_to(project.root)) for p in candidates],
                   "artifacts": [str(p.relative_to(project.root)) for p in victims]}
        if apply:
            for candidate in candidates:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                shutil.rmtree(candidate)
            for candidate in victims:
                if candidate.is_symlink():
                    raise Denied("Refusing a symlink during prune")
                shutil.rmtree(candidate)
            for parent in {c.parent for c in (*candidates, *victims)}:
                sync_dir(parent)
            audit = project.root / "maintenance" / f"prune-{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
            write_json(audit, {"applied_at": now(), "keep_artifacts": keep_artifacts, **removed})
            sync_dir(audit.parent)
            removed["audit"] = str(audit.relative_to(project.root))
    return {"applied": apply, **removed,
            "retained": "All evidence, snapshots, content objects, conversations and proposals. "
                        "Artifact documents beyond the kept count are operator-approved disposable projections; "
                        "their evidence remains in snapshots and runs."}


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
