"""Content-addressed, coherent views; never expose a moving worktree to readers."""
from __future__ import annotations

import difflib
import fnmatch
import os
import stat
from pathlib import Path

from . import platform as _platform
from .errors import Denied, LimitExceeded
from .fs import DIGEST, PREVIEW_BYTES, atomic_write, canonical, digest, mkdir, now, publish_pointer, read_json, safe_read, write_json, relative_parts


def open_store(root: Path, config) -> "Snapshots":
    """Build a Snapshots view with the operator's limits. Single construction site."""
    limits = config.limits
    return Snapshots(root, excludes=config.exclude, max_file=limits.file_bytes,
                     max_bytes=limits.snapshot_bytes, max_files=limits.snapshot_files,
                     history_index=limits.history_index)


class Snapshots:
    def __init__(self, root: Path, *, excludes: tuple[str, ...], max_file: int,
                 max_bytes: int, max_files: int, history_index: int = 128):
        self.root = root
        self.excludes = excludes
        self.max_file, self.max_bytes, self.max_files = max_file, max_bytes, max_files
        self.history_index = history_index
        mkdir(root / "objects")
        mkdir(root / "snapshots")

    def excluded(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(part, pattern)
                   for part in name.split("/") for pattern in self.excludes)

    def capture_files(self, workspace: Path) -> dict:
        files: dict[str, dict] = {}
        skipped: list[str] = []
        total = 0
        if _platform.HAS_OPENAT and _platform.HAS_DIR_FD:
            return self._capture_openat(workspace, files, skipped, total)
        return self._capture_portable(workspace, files, skipped, total)

    def _capture_openat(self, workspace: Path, files: dict, skipped: list, total: int) -> dict:
        # fwalk does not traverse symlink directories; safe_read pins each component.
        for directory, dirs, names, directory_fd in os.fwalk(workspace, follow_symlinks=False):
            relative = Path(directory).relative_to(workspace)
            visible_dirs = []
            for d in sorted(dirs):
                rel = (relative / d).as_posix()
                if self.excluded(rel):
                    continue
                if stat.S_ISLNK(os.stat(d, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                    skipped.append(rel)
                else:
                    visible_dirs.append(d)
            dirs[:] = visible_dirs
            for name in sorted(names):
                path = (relative / name).as_posix()
                if self.excluded(path):
                    continue
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    skipped.append(path)
                    continue
                if len(files) >= self.max_files:
                    raise LimitExceeded("Snapshot file count exceeded")
                data = safe_read(workspace, path, self.max_file)
                total += len(data)
                if total > self.max_bytes:
                    raise LimitExceeded("Snapshot byte limit exceeded")
                sha = digest(data)
                object_path = self.root / "objects" / sha
                if not object_path.exists():
                    atomic_write(object_path, data, exclusive=True)
                files[path] = {"sha256": sha, "bytes": len(data), "executable": bool(info.st_mode & 0o111)}
        return {"files": files, "code_digest": digest(canonical(files)), "skipped": skipped,
                "bytes": total}

    def _capture_portable(self, workspace: Path, files: dict, skipped: list, total: int) -> dict:
        # Fallback where openat/dir_fd is unavailable: walk without following
        # links, refuse any symlink, and read through the same safe_read path.
        for directory, dirs, names in os.walk(workspace, followlinks=False):
            relative = Path(directory).relative_to(workspace)
            visible_dirs = []
            for d in sorted(dirs):
                rel = (relative / d).as_posix() if str(relative) != "." else d
                full = Path(directory) / d
                if self.excluded(rel):
                    continue
                if full.is_symlink():
                    skipped.append(rel)
                else:
                    visible_dirs.append(d)
            dirs[:] = visible_dirs
            for name in sorted(names):
                rel = (relative / name).as_posix() if str(relative) != "." else name
                if self.excluded(rel):
                    continue
                full = Path(directory) / name
                try:
                    if full.is_symlink():
                        skipped.append(rel)
                        continue
                    info = full.stat()
                except OSError:
                    skipped.append(rel)
                    continue
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                    skipped.append(rel)
                    continue
                if len(files) >= self.max_files:
                    raise LimitExceeded("Snapshot file count exceeded")
                data = safe_read(workspace, rel, self.max_file)
                total += len(data)
                if total > self.max_bytes:
                    raise LimitExceeded("Snapshot byte limit exceeded")
                sha = digest(data)
                object_path = self.root / "objects" / sha
                if not object_path.exists():
                    atomic_write(object_path, data, exclusive=True)
                files[rel] = {"sha256": sha, "bytes": len(data), "executable": bool(info.st_mode & 0o111)}
        return {"files": files, "code_digest": digest(canonical(files)), "skipped": skipped,
                "bytes": total}

    def create(self, captured: dict, *, goal: str, state: str, run: str | None,
               outcome: str, summary: str, verification: dict | None = None,
               wake_at: float | None = None, inbox_seen: str = "", wake_generation: str = "") -> dict:
        record = {"schema": 1, **captured, "created_at": now(), "goal": goal,
                  "goal_digest": digest(goal.encode()), "state": state, "run": run,
                  "outcome": outcome, "summary": summary, "verification": verification,
                  "wake_at": wake_at, "inbox_seen": inbox_seen, "wake_generation": wake_generation}
        record["id"] = digest(canonical(record))
        write_json(self.root / "snapshots" / f"{record['id']}.json", record, exclusive=True)
        return record

    def publish(self, snapshot: dict) -> None:
        # This pointer is the commit point. A manifest exists before it can be visible.
        history = read_json(self.root / "history.json", [])
        if snapshot["id"] not in history:
            history = [*history, snapshot["id"]][-self.history_index:]
            write_json(self.root / "history.json", history)
        publish_pointer(self.root, {"snapshot": snapshot["id"]}, name="current.json")

    def get(self, snapshot_id: str | None = None) -> dict:
        if snapshot_id is None:
            pointer = read_json(self.root / "current.json")
            if pointer is None:
                raise Denied("No published snapshot")
            snapshot_id = pointer["snapshot"]
        if not isinstance(snapshot_id, str) or not DIGEST.fullmatch(snapshot_id):
            raise Denied("Invalid snapshot ID")
        record = read_json(self.root / "snapshots" / f"{snapshot_id}.json")
        if record is None:
            raise Denied("Snapshot not found")
        body = {k: v for k, v in record.items() if k != "id"}
        if record.get("id") != snapshot_id or digest(canonical(body)) != snapshot_id:
            raise Denied("Snapshot integrity failure")
        return record

    def read(self, snapshot: dict, name: str) -> bytes:
        entry = snapshot["files"].get(name)
        if entry is None or not DIGEST.fullmatch(entry["sha256"]):
            raise Denied("File is not part of this snapshot")
        data = safe_read(self.root / "objects", entry["sha256"], self.max_file)
        if digest(data) != entry["sha256"]:
            raise Denied("Object integrity failure")
        return data

    def materialize(self, snapshot: dict, destination: Path) -> None:
        if destination.exists():
            raise Denied("Snapshot destination already exists")
        mkdir(destination)
        for name, entry in snapshot["files"].items():
            relative_parts(name)
            atomic_write(destination / name, self.read(snapshot, name),
                         mode=0o700 if entry["executable"] else 0o600)

    def history(self, anchor: str, limit: int = 8) -> list[dict]:
        """Only entries at/before the caller's snapshot; never a moving mixed view."""
        history = read_json(self.root / "history.json", [])
        if anchor not in history:
            return [self.get(anchor)]
        end = history.index(anchor) + 1
        return [self.get(sid) for sid in history[max(0, end - limit):end]]

    def changes(self, anchor: str, maximum: int = PREVIEW_BYTES) -> dict:
        current = self.get(anchor)
        earlier = self.history(anchor, self.history_index)[:-1]
        base = next((s for s in reversed(earlier) if s["code_digest"] != current["code_digest"]), None)
        if base is None:
            return {"base_snapshot": None, "target_snapshot": anchor, "diff": "", "note": "No earlier distinct code snapshot in the recent window"}
        chunks, count, skipped = [], 0, []
        for name in sorted(set(base["files"]) | set(current["files"])):
            if base["files"].get(name) == current["files"].get(name):
                continue
            if max(base["files"].get(name, {}).get("bytes", 0), current["files"].get(name, {}).get("bytes", 0)) > PREVIEW_BYTES:
                skipped.append(name)
                continue
            before = self.read(base, name) if name in base["files"] else b""
            after = self.read(current, name) if name in current["files"] else b""
            if b"\x00" in before or b"\x00" in after:
                skipped.append(name)
                continue
            for line in difflib.unified_diff(before.decode("utf-8", "replace").splitlines(True),
                                             after.decode("utf-8", "replace").splitlines(True),
                                             fromfile="before/" + name, tofile="after/" + name):
                encoded = line.encode()
                if count + len(encoded) > maximum:
                    return {"base_snapshot": base["id"], "target_snapshot": anchor, "diff": "".join(chunks), "truncated": True, "skipped": skipped}
                chunks.append(line)
                count += len(encoded)
        return {"base_snapshot": base["id"], "target_snapshot": anchor, "diff": "".join(chunks), "truncated": bool(skipped), "skipped": skipped}
