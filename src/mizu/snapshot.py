"""Content-addressed, coherent views; never expose a moving worktree to readers."""
from __future__ import annotations

import difflib
import itertools
import fnmatch
import os
import stat
from pathlib import Path

from . import platform as _platform
from .errors import Denied, LimitExceeded
from .fs import DIGEST, PREVIEW_BYTES, atomic_write, canonical, digest, mkdir, now, publish_pointer, read_json, safe_read, safe_read_info, write_json, relative_parts
from .vcs import REF_PREFIX


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
        # Reserved upstream-ref subtree (M1): injected refs are read-only
        # workspace views served from the live tree, never snapshot content,
        # so they cannot affect code_digest even under an emptied operator
        # exclude list. Operators must not keep project source here.
        parts = name.split("/")
        if len(parts) >= len(REF_PREFIX) and tuple(parts[:len(REF_PREFIX)]) == REF_PREFIX:
            return True
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
                total = self._capture_file(workspace, path, files, total)
        return {"files": files, "code_digest": digest(canonical(files)), "skipped": skipped,
                "bytes": total}

    def _capture_portable(self, workspace: Path, files: dict, skipped: list, total: int) -> dict:
        # Fallback where openat/dir_fd is unavailable: walk without following
        # links, refuse any symlink, and read through the same safe_read path.
        for directory, dirs, names in os.walk(workspace, followlinks=False):
            relative = Path(directory).relative_to(workspace)
            visible_dirs = []
            for d in sorted(dirs):
                rel = (relative / d).as_posix()
                full = Path(directory) / d
                if self.excluded(rel):
                    continue
                if full.is_symlink() or bool(getattr(full.lstat(), "st_file_attributes", 0) & 0x400):
                    skipped.append(rel)
                else:
                    visible_dirs.append(d)
            dirs[:] = visible_dirs
            for name in sorted(names):
                rel = (relative / name).as_posix()
                if self.excluded(rel):
                    continue
                full = Path(directory) / name
                try:
                    if full.is_symlink() or bool(getattr(full.lstat(), "st_file_attributes", 0) & 0x400):
                        skipped.append(rel)
                        continue
                    info = full.stat()
                except OSError:
                    skipped.append(rel)
                    continue
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                    skipped.append(rel)
                    continue
                total = self._capture_file(workspace, rel, files, total)
        return {"files": files, "code_digest": digest(canonical(files)), "skipped": skipped,
                "bytes": total}

    def _capture_file(self, workspace: Path, name: str, files: dict, total: int) -> int:
        if len(files) >= self.max_files:
            raise LimitExceeded("Snapshot file count exceeded")
        data, info = safe_read_info(workspace, name, self.max_file)
        total += len(data)
        if total > self.max_bytes:
            raise LimitExceeded("Snapshot byte limit exceeded")
        sha = digest(data)
        atomic_write(self.root / "objects" / sha, data, exclusive=True)
        files[name] = {"sha256": sha, "bytes": len(data), "executable": bool(info.st_mode & 0o111)}
        return total

    def _history_ids(self) -> list[str]:
        pointer = read_json(self.root / "current.json")
        if pointer is None:
            return []
        if not isinstance(pointer, dict):
            raise Denied("Invalid publication pointer")
        generation = pointer.get("history")
        if not isinstance(generation, str) or not DIGEST.fullmatch(generation):
            raise Denied("Publication pointer requires a history generation")
        history = read_json(self.root / "histories" / (generation + ".json"))
        if digest(canonical(history)) != generation:
            raise Denied("History integrity failure")
        if not isinstance(history, list) or any(not isinstance(sid, str) or not DIGEST.fullmatch(sid) for sid in history):
            raise Denied("Invalid history entries")
        if not history or history[-1] != pointer.get("snapshot"):
            raise Denied("History generation does not end at the published snapshot")
        return history

    def create(self, captured: dict, *, goal: str, state: str, run: str | None,
               outcome: str, summary: str, verification: dict | None = None,
               wake_at: float | None = None, inbox_seen: str = "", wake_generation: str = "") -> dict:
        record = {**captured, "created_at": now(), "goal": goal,
                  "goal_digest": digest(goal.encode()), "state": state, "run": run,
                  "outcome": outcome, "summary": summary, "verification": verification,
                  "wake_at": wake_at, "inbox_seen": inbox_seen, "wake_generation": wake_generation}
        record["id"] = digest(canonical(record))
        write_json(self.root / "snapshots" / f"{record['id']}.json", record, exclusive=True)
        return record

    def publish(self, snapshot: dict) -> None:
        # This pointer is the commit point. A manifest exists before it can be visible.
        self.get(snapshot["id"])
        history = self._history_ids()
        if snapshot["id"] not in history:
            history = [*history, snapshot["id"]][-self.history_index:]
        generation = digest(canonical(history))
        write_json(self.root / "histories" / (generation + ".json"), history, exclusive=True)
        publish_pointer(self.root, {"snapshot": snapshot["id"], "history": generation}, name="current.json")

    def get(self, snapshot_id: str | None = None) -> dict:
        if snapshot_id is None:
            pointer = read_json(self.root / "current.json")
            if pointer is None:
                raise Denied("No published snapshot")
            if not isinstance(pointer, dict) or not isinstance(pointer.get("history"), str) or not DIGEST.fullmatch(pointer["history"]):
                raise Denied("Publication pointer requires a history generation")
            snapshot_id = pointer.get("snapshot")
        if not isinstance(snapshot_id, str) or not DIGEST.fullmatch(snapshot_id):
            raise Denied("Invalid snapshot ID")
        record = read_json(self.root / "snapshots" / f"{snapshot_id}.json")
        if record is None:
            raise Denied("Snapshot not found")
        body = {k: v for k, v in record.items() if k != "id"}
        if record.get("id") != snapshot_id or digest(canonical(body)) != snapshot_id:
            raise Denied("Snapshot integrity failure")
        if not isinstance(record.get("files"), dict):
            raise Denied("Invalid snapshot files")
        if digest(canonical(record["files"])) != record.get("code_digest"):
            raise Denied("Snapshot code digest mismatch")
        if not isinstance(record.get("goal"), str) or digest(record["goal"].encode()) != record.get("goal_digest"):
            raise Denied("Snapshot goal digest mismatch")
        self.validate_paths(record["files"])
        for entry in record["files"].values():
            if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) or not DIGEST.fullmatch(entry["sha256"]) or type(entry.get("bytes")) is not int or not 0 <= entry["bytes"] <= self.max_file or type(entry.get("executable")) is not bool:
                raise Denied("Invalid snapshot file metadata")
        return record

    def validate_paths(self, files):
        seen = set()
        for name in files:
            relative_parts(name)
            if self.excluded(name):
                raise Denied("Snapshot contains an excluded path")
            key = name.casefold() if _platform.IS_WINDOWS else name
            if key in seen:
                raise Denied("Snapshot paths collide on this host")
            seen.add(key)
        if any("/".join(name.split("/")[:i]) in seen for name in seen for i in range(1, len(name.split("/")))):
            raise Denied("Snapshot file and directory paths collide")

    def read(self, snapshot: dict, name: str) -> bytes:
        entry = snapshot["files"].get(name)
        if entry is None or not DIGEST.fullmatch(entry["sha256"]):
            raise Denied("File is not part of this snapshot")
        data = safe_read(self.root / "objects", entry["sha256"], self.max_file)
        if digest(data) != entry["sha256"] or len(data) != entry["bytes"]:
            raise Denied("Object integrity failure")
        return data

    def materialize(self, snapshot: dict, destination: Path) -> None:
        if destination.exists():
            raise Denied("Snapshot destination already exists")
        self.validate_paths(snapshot["files"])
        mkdir(destination)
        for name, entry in snapshot["files"].items():
            relative_parts(name)
            atomic_write(destination / name, self.read(snapshot, name),
                         mode=0o700 if entry["executable"] else 0o600)

    def history(self, anchor: str, limit: int = 8) -> list[dict]:
        """Only entries at/before the caller's snapshot; never a moving mixed view."""
        history = self._history_ids()
        if anchor not in history:
            return [self.get(anchor)]
        end = history.index(anchor) + 1
        return [self.get(sid) for sid in history[max(0, end - limit):end]]

    def changes(self, anchor: str, maximum: int = PREVIEW_BYTES) -> dict:
        current = self.get(anchor)
        ids = self._history_ids()
        earlier = ids[:ids.index(anchor)] if anchor in ids else []
        base = None
        for sid in reversed(earlier):
            candidate = self.get(sid)
            if candidate["code_digest"] != current["code_digest"]:
                base = candidate
                break
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
            old_mode = base["files"].get(name, {}).get("executable")
            new_mode = current["files"].get(name, {}).get("executable")
            mode_lines = [] if old_mode == new_mode else [f"mode {name}: {old_mode} -> {new_mode}\n"]
            for line in itertools.chain(mode_lines, difflib.unified_diff(before.decode("utf-8", "replace").splitlines(True),
                                             after.decode("utf-8", "replace").splitlines(True),
                                             fromfile="before/" + name, tofile="after/" + name)):
                encoded = line.encode()
                if count + len(encoded) > maximum:
                    return {"base_snapshot": base["id"], "target_snapshot": anchor, "diff": "".join(chunks), "truncated": True, "skipped": skipped}
                chunks.append(line)
                count += len(encoded)
        return {"base_snapshot": base["id"], "target_snapshot": anchor, "diff": "".join(chunks), "truncated": bool(skipped), "skipped": skipped}
