"""Content-addressed, coherent views; never expose a moving worktree to readers."""
from __future__ import annotations

import datetime as dt
import difflib
import fnmatch
import itertools
import os
import re
import stat
import time
from pathlib import Path

from . import platform as _platform
from .config import role_no_progress_backoff_seconds
from .errors import Denied, LimitExceeded, SnapshotConflict
from .fs import DIGEST, ID, PREVIEW_BYTES, atomic_write, canonical, digest, lock, mkdir, now, publish_pointer, read_json, safe_read, safe_read_info, write_json, relative_parts
from .vcs import REF_PREFIX


_CURRENT_PARENT = object()

PROGRESS_SIGNALS = ("tool", "code", "state", "decision", "insight", "input")
_PROGRESS_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def validate_progress(value: dict) -> dict:
    """Validate the compact, immutable progress record; legacy absence is valid."""
    fields = {"schema", "signals", "selected", "role", "has_progress",
              "no_progress_count", "obligations_digest", "recorded_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise Denied("Invalid snapshot progress record")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise Denied("Invalid snapshot progress schema")
    signals = value["signals"]
    if (not isinstance(signals, dict) or set(signals) != set(PROGRESS_SIGNALS)
            or any(type(signals[name]) is not bool for name in PROGRESS_SIGNALS)):
        raise Denied("Invalid snapshot progress signals")
    selected = value["selected"]
    if (not isinstance(selected, list) or len(selected) > len(PROGRESS_SIGNALS)
            or any(not isinstance(name, str) or name not in PROGRESS_SIGNALS for name in selected)
            or len(selected) != len(set(selected))):
        raise Denied("Invalid snapshot progress policy")
    if not isinstance(value["role"], str) or not ID.fullmatch(value["role"]):
        raise Denied("Invalid snapshot progress role")
    has_progress = any(signals[name] for name in selected)
    if type(value["has_progress"]) is not bool or value["has_progress"] != has_progress:
        raise Denied("Invalid snapshot progress result")
    count = value["no_progress_count"]
    if (type(count) is not int or not 0 <= count <= 101
            or has_progress and count != 0):
        raise Denied("Invalid snapshot no-progress count")
    obligations_digest = value["obligations_digest"]
    if not isinstance(obligations_digest, str) or not DIGEST.fullmatch(obligations_digest):
        raise Denied("Invalid snapshot progress obligation digest")
    stamp = value["recorded_at"]
    if not isinstance(stamp, str) or len(stamp) > 64 or not _PROGRESS_TIMESTAMP.fullmatch(stamp):
        raise Denied("Invalid snapshot progress timestamp")
    try:
        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise Denied("Invalid snapshot progress timestamp") from exc
    return value


def progress_timestamp(value: dict) -> float | None:
    """Return a validated progress timestamp, or None for malformed evidence."""
    try:
        validate_progress(value)
        return dt.datetime.fromisoformat(value["recorded_at"].replace("Z", "+00:00")).timestamp()
    except (Denied, OverflowError, OSError, ValueError, TypeError):
        return None


def progress_projection(value: dict | None) -> dict | None:
    """Return only schema-bounded progress fields suitable for status output."""
    if value is None:
        return None
    validate_progress(value)
    return {"schema": 1, "signals": {name: value["signals"][name] for name in PROGRESS_SIGNALS},
            "selected": list(value["selected"]), "role": value["role"],
            "has_progress": value["has_progress"],
            "no_progress_count": value["no_progress_count"],
            "recorded_at": value["recorded_at"]}


def progress_disposition(value: dict | None, outcome: str, config=None,
                         role_name: str | None = None, current_time: float | None = None) -> str:
    """Bounded explanation of whether this record self-admits continue/bounded."""
    if value is None:
        return "legacy_behavior"
    try:
        validate_progress(value)
        limits = config.limits
        selected = tuple(limits.progress_signals)
        allowance = limits.no_progress_continuations
        role = value["role"] if role_name is None else role_name
        # Effective per-role timer: roles.<name>.no_progress_backoff_seconds
        # when set, else the project limit. Keep resolution centralized with
        # the role configuration helpers; legacy config-like callers lacking
        # a roles mapping still inherit the project value.
        roles = getattr(config, "roles", None)
        role_config = roles.get(role) if isinstance(roles, dict) else None
        backoff = role_no_progress_backoff_seconds(config, role_config)
        if set(value["selected"]) != set(selected) or value["role"] != role:
            return "reevaluate"
        if outcome not in ("continue", "bounded"):
            return "not_applicable"
        if value["has_progress"]:
            return "self_admit_progress"
        if value["no_progress_count"] <= allowance:
            return "self_admit_allowance"
        if backoff > 0:
            timestamp = progress_timestamp(value)
            current_time = time.time() if current_time is None else current_time
            if timestamp is not None and current_time >= timestamp + backoff:
                return "self_admit_backoff"
        return "parked_no_progress"
    except (AttributeError, TypeError, ValueError, Denied):
        return "reevaluate"


def no_progress_recovery_due(value: dict | None, outcome: str, config,
                             role_name: str | None = None) -> bool:
    """Whether a matching empty continuation requires a fresh session next time.

    This is an admission-independent signal: it never schedules a run. A
    persistent session rotates only when another trigger already admits a
    unit, so recovery cannot create a no-progress spin.
    """
    if value is None or outcome not in ("continue", "bounded"):
        return False
    try:
        validate_progress(value)
        limits = config.limits
        role = value["role"] if role_name is None else role_name
        return (value["role"] == role
                and set(value["selected"]) == set(limits.progress_signals)
                and not value["has_progress"]
                and value["no_progress_count"] >= limits.no_progress_recovery_after)
    except (AttributeError, TypeError, ValueError, Denied):
        return False


#: Bounds for the persisted consecutive-missing-finish streak (fail-closed read).
MAX_MISSING_FINISH_STREAK = 1073741824


def missing_finish_streak(health: dict | None) -> int:
    """Validated consecutive-missing-finish streak, or 0 for absent/invalid."""
    try:
        if not isinstance(health, dict):
            return 0
        streak = health.get("consecutive_missing_finish", 0)
        if type(streak) is not int or not 0 <= streak <= MAX_MISSING_FINISH_STREAK:
            return 0
        return streak
    except Exception:
        return 0


def missing_finish_recovery_due(health: dict | None, config) -> bool:
    """Whether consecutive missing-finish failures require a fresh session.

    This is an admission-independent signal: it never schedules a run. A
    persistent session rotates only when another trigger already admits a
    unit with unblocked open work, so recovery cannot create a
    missing-finish spin. A fully blocked/empty queue never rotates (checked
    at dispatch, not here).
    """
    if not isinstance(health, dict):
        return False
    try:
        streak = health.get("consecutive_missing_finish", 0)
        if type(streak) is not int or not 0 <= streak <= MAX_MISSING_FINISH_STREAK:
            return False
        threshold = config.limits.missing_finish_recovery_after
        if type(threshold) is not int:
            return False
        return streak >= threshold
    except (AttributeError, TypeError, ValueError):
        return False


def open_store(root: Path, config, *, create_dirs: bool = True) -> "Snapshots":
    """Build a Snapshots view with the operator's limits. Single construction site."""
    limits = config.limits
    return Snapshots(root, excludes=config.exclude, max_file=limits.file_bytes,
                     max_bytes=limits.snapshot_bytes, max_files=limits.snapshot_files,
                     history_index=limits.history_index, create_dirs=create_dirs)


class Snapshots:
    def __init__(self, root: Path, *, excludes: tuple[str, ...], max_file: int,
                 max_bytes: int, max_files: int, history_index: int = 128,
                 create_dirs: bool = True):
        self.root = root
        self.excludes = excludes
        self.max_file, self.max_bytes, self.max_files = max_file, max_bytes, max_files
        self.history_index = history_index
        if create_dirs:
            mkdir(root / "objects")
            mkdir(root / "snapshots")

    @staticmethod
    def _reserved_prefix(name: str) -> bool:
        """Whether a workspace path falls under the reserved virtual-ref prefix.

        The ``refs/remotes/upstream/**`` subtree is reserved for virtual
        upstream refs served from the harness store, plus the legacy
        in-workspace subtree that the migration still excludes. Unlike
        operator excludes (chosen policy, silent by design), a reservation
        hit is reported in the capture record, refused at plain-directory
        import, and refused on colliding reads.
        """
        parts = name.split("/")
        return len(parts) >= len(REF_PREFIX) and tuple(parts[:len(REF_PREFIX)]) == REF_PREFIX

    def excluded(self, name: str) -> bool:
        # Legacy in-workspace upstream-ref subtree (pre-move): the store now
        # lives outside the working tree, so this only guards workspaces
        # that have not yet refreshed. It stays excluded so the migration
        # never moves code_digest. Operators must not keep project source
        # here.
        parts = name.split("/")
        if len(parts) >= len(REF_PREFIX) and tuple(parts[:len(REF_PREFIX)]) == REF_PREFIX:
            return True
        return any(fnmatch.fnmatchcase(part, pattern)
                   for part in name.split("/") for pattern in self.excludes)

    def capture_files(self, workspace: Path) -> dict:
        files: dict[str, dict] = {}
        skipped: list[str] = []
        reserved: list[str] = []
        total = 0
        if _platform.HAS_OPENAT and _platform.HAS_DIR_FD:
            return self._capture_openat(workspace, files, skipped, reserved, total)
        return self._capture_portable(workspace, files, skipped, reserved, total)

    def _capture_openat(self, workspace: Path, files: dict, skipped: list, reserved: list, total: int) -> dict:
        # fwalk does not traverse symlink directories; safe_read pins each component.
        for directory, dirs, names, directory_fd in os.fwalk(workspace, follow_symlinks=False):
            relative = Path(directory).relative_to(workspace)
            visible_dirs = []
            for d in sorted(dirs):
                rel = (relative / d).as_posix()
                if self._reserved_prefix(rel):
                    if stat.S_ISLNK(os.stat(d, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                        reserved.append(rel)
                    else:
                        visible_dirs.append(d)
                    continue
                if self.excluded(rel):
                    continue
                if stat.S_ISLNK(os.stat(d, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                    skipped.append(rel)
                else:
                    visible_dirs.append(d)
            dirs[:] = visible_dirs
            for name in sorted(names):
                path = (relative / name).as_posix()
                if self._reserved_prefix(path):
                    reserved.append(path)
                    continue
                if self.excluded(path):
                    continue
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    skipped.append(path)
                    continue
                total = self._capture_file(workspace, path, files, total)
        return {"files": files, "code_digest": digest(canonical(files)), "skipped": skipped,
                "reserved": reserved, "bytes": total}

    def _capture_portable(self, workspace: Path, files: dict, skipped: list, reserved: list, total: int) -> dict:
        # Fallback where openat/dir_fd is unavailable: walk without following
        # links, refuse any symlink, and read through the same safe_read path.
        for directory, dirs, names in os.walk(workspace, followlinks=False):
            relative = Path(directory).relative_to(workspace)
            visible_dirs = []
            for d in sorted(dirs):
                rel = (relative / d).as_posix()
                full = Path(directory) / d
                if self._reserved_prefix(rel):
                    if full.is_symlink() or bool(getattr(full.lstat(), "st_file_attributes", 0) & 0x400):
                        reserved.append(rel)
                    else:
                        visible_dirs.append(d)
                    continue
                if self.excluded(rel):
                    continue
                if full.is_symlink() or bool(getattr(full.lstat(), "st_file_attributes", 0) & 0x400):
                    skipped.append(rel)
                else:
                    visible_dirs.append(d)
            dirs[:] = visible_dirs
            for name in sorted(names):
                rel = (relative / name).as_posix()
                if self._reserved_prefix(rel):
                    reserved.append(rel)
                    continue
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
                "reserved": reserved, "bytes": total}

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
               wake_at: float | None = None, inbox_seen: str = "", wake_generation: str = "",
               progress: dict | None = None,
               parent: str | None | object = _CURRENT_PARENT) -> dict:
        """Persist an immutable capture; publication is a separate CAS operation.

        Omitted parent captures the current published ID. Derived callers must
        pass the exact snapshot they started from. None is an initial root only
        (no current pointer); explicit IDs must already exist in this store.
        No pointer moves on creation or failure.
        """
        if parent is _CURRENT_PARENT or parent is None:
            history = self._history_ids()
            if parent is None and history:
                raise Denied("A root snapshot requires an empty publication store")
            parent = history[-1] if history else None
        if parent is not None:
            if not isinstance(parent, str) or not DIGEST.fullmatch(parent):
                raise Denied("Invalid parent snapshot ID")
            self.get(parent)
        if progress is not None:
            validate_progress(progress)
        record = {**captured, "created_at": now(), "goal": goal,
                  "goal_digest": digest(goal.encode()), "state": state, "run": run,
                  "outcome": outcome, "summary": summary, "verification": verification,
                  "wake_at": wake_at, "inbox_seen": inbox_seen, "wake_generation": wake_generation,
                  "parent": parent}
        if progress is not None:
            record["progress"] = progress
        record["id"] = digest(canonical(record))
        write_json(self.root / "snapshots" / f"{record['id']}.json", record, exclusive=True)
        return record

    def publish(self, snapshot: dict) -> None:
        """Publish under one store-scoped fs.lock, after validating stored data.

        New records compare their parent (including an initial None) with the
        current ID. SnapshotConflict leaves history/current untouched; retrying
        the already-current ID is a no-op. Legacy records without parent remain
        readable; parentless legacy records may initialize an empty store but
        cannot replace an existing head. Caller-supplied fields never override
        a stored record's CAS. Immutable history precedes the durable pointer commit;
        I/O failure can leave an unreferenced generation, as before. No external
        I/O or admission changes; fs.lock releases on exit/process death.
        """
        with lock(self.root / "locks" / "publication.lock"):
            record = self.get(snapshot["id"])
            history = self._history_ids()
            current = history[-1] if history else None
            if current == record["id"]:
                return
            if "parent" in record:
                if record["parent"] != current:
                    raise SnapshotConflict("Snapshot parent is no longer current")
                if record["parent"] is not None:
                    self.get(record["parent"])
            elif current is not None:
                # Old records remain readable, but without a bound base they
                # cannot replace a head whose lineage is already advancing.
                raise SnapshotConflict("Legacy snapshot has no compare-and-swap parent")
            # This pointer is the commit point. The manifest and history exist
            # before they can be visible; keep the existing durability order.
            if record["id"] not in history:
                history = [*history, record["id"]][-self.history_index:]
            generation = digest(canonical(history))
            write_json(self.root / "histories" / (generation + ".json"), history, exclusive=True)
            publish_pointer(self.root, {"snapshot": record["id"], "history": generation}, name="current.json")

    def merge(self, base: str, head: str, target: str) -> tuple[dict | None, list[str]]:
        """Three-way path merge of exact stored IDs, without reading object bytes.

        head must directly name base as parent; target must descend from base
        within history_index. Inputs are untrusted and checked by get, paths
        and aggregate bounds.
        Complete entries (digest/size/mode) merge atomically, never as text.
        Return (capture, []) on success, suitable for create(parent=target), or
        (None, sorted conflict paths) for divergent edits or host/path-prefix
        collisions. No partial capture is publishable. Invalid inputs raise
        Denied; input/output count or byte overflow raises LimitExceeded.
        This is read-only, deterministic and safe to retry/cancel between calls;
        returned code_digest binds the merged manifest, not publication authority.
        """
        records = []
        for sid in (base, head, target):
            # None must not invoke get's moving-current convenience interface.
            if not isinstance(sid, str) or not DIGEST.fullmatch(sid):
                raise Denied("Merge requires exact snapshot IDs")
            record = self.get(sid)
            self._manifest_bounds(record["files"])
            records.append(record)
        before, ours, theirs = records
        if ours.get("parent") != base:
            raise Denied("Merge head must be directly based on base")
        ancestor = theirs
        for _ in range(self.history_index):
            if ancestor["id"] == base:
                break
            parent = ancestor.get("parent")
            if parent is None:
                raise Denied("Merge target must descend from base")
            ancestor = self.get(parent)
        else:
            raise Denied("Merge target ancestry exceeds retained history bound")
        files, conflicts = {}, set()
        for name in sorted(set(before["files"]) | set(ours["files"]) | set(theirs["files"])):
            old, left, right = (r["files"].get(name) for r in records)
            if left == right or right == old:
                entry = left
            elif left == old:
                entry = right
            else:
                conflicts.add(name)
                continue
            if entry is not None:
                files[name] = dict(entry)
        # Each input's paths are valid, but their union may not be. Report both
        # sides of a collision rather than selecting an arbitrary survivor.
        normalized = {}
        for name in files:
            key = name.casefold() if _platform.IS_WINDOWS else name
            normalized.setdefault(key, []).append(name)
        for key, names in normalized.items():
            if len(names) > 1:
                conflicts.update(names)
            parts = key.split("/")
            for i in range(1, len(parts)):
                prefix = "/".join(parts[:i])
                if prefix in normalized:
                    conflicts.update(names)
                    conflicts.update(normalized[prefix])
        if conflicts:
            return None, sorted(conflicts)
        self.validate_paths(files)
        total = self._manifest_bounds(files)
        return {"files": files, "code_digest": digest(canonical(files)),
                "skipped": [], "reserved": [], "bytes": total}, []

    def _manifest_bounds(self, files: dict) -> int:
        if len(files) > self.max_files:
            raise LimitExceeded("Snapshot file count exceeded")
        total = sum(entry["bytes"] for entry in files.values())
        if total > self.max_bytes:
            raise LimitExceeded("Snapshot byte limit exceeded")
        return total

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
        if not isinstance(record, dict):
            raise Denied("Invalid snapshot record")
        body = {k: v for k, v in record.items() if k != "id"}
        if record.get("id") != snapshot_id or digest(canonical(body)) != snapshot_id:
            raise Denied("Snapshot integrity failure")
        if "parent" in record and record["parent"] is not None and (not isinstance(record["parent"], str) or not DIGEST.fullmatch(record["parent"])):
            raise Denied("Invalid parent snapshot ID")
        if not isinstance(record.get("files"), dict):
            raise Denied("Invalid snapshot files")
        if "progress" in record:
            validate_progress(record["progress"])
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
