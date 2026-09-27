"""Durable local-file primitives. No database, stale PID files, or implicit shell."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from . import platform as _platform
from .errors import Busy, Denied, LimitExceeded

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
#: Fixed mechanism preview bound (128 KiB): read/diff/editor/insight framing.
#: Not a TOML knob; file_bytes/snapshot_bytes stay operator policy.
PREVIEW_BYTES = 131072
#: MCP/socket LF framing bound shared by the bridge, the MCP proxy and the
#: Editor capsule server (each rejects anything above it).
MAX_FRAME = 1024 * 1024
#: Shell-input bound shared by the tool contract and the sandbox executor.
SCRIPT_MAX = 65536


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Denied("Invalid identifier; use lowercase letters, digits, '_' or '-'")
    return value


def mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def sync_dir(path: Path) -> None:
    # Directory fsync is POSIX-only (needs O_DIRECTORY); best-effort elsewhere.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (AttributeError, OSError, NotImplementedError, ValueError):
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, exclusive: bool = False,
                 mode: int = 0o600) -> None:
    """Publish only complete files; fsync data and containing directory.

    exclusive=True never replaces existing content. Identical retries are safe.
    """
    mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with contextlib.suppress(AttributeError, OSError, NotImplementedError):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            with contextlib.suppress(OSError):
                os.fsync(stream.fileno())
        if exclusive:
            try:
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except (TypeError, NotImplementedError):
                    os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise Denied(f"Immutable object conflict: {path.name}") from None
        else:
            os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(temporary)


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    atomic_write(path, canonical(value), exclusive=exclusive)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Kernel releases the lock on death. Never unlink a lock inode."""
    mkdir(path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            _platform.lock_fd(fd, blocking=blocking)
        except BlockingIOError:
            raise Busy(f"Already running: {path.stem}") from None
        yield
    finally:
        with contextlib.suppress(Exception):
            _platform.unlock_fd(fd)
        os.close(fd)


def publish_pointer(root: Path, pointer: dict, *, name: str = "latest.json") -> None:
    """Move a latest pointer only after the entry is complete.

    A failed run must never partially replace the previous published pointer.
    """
    write_json(root / name, pointer)


def publish_entry(root: Path, name: str, payload: bytes, pointer: dict) -> None:
    """Complete an entry file first, then atomically move the latest pointer."""
    mkdir(root)
    atomic_write(root / name, payload)
    publish_pointer(root, pointer)


def relative_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise Denied("Invalid relative path")
    parts = tuple(name.split("/"))
    if PurePosixPath(name).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise Denied("Path must be relative, without empty, '.' or '..' components")
    return parts


def safe_read(root: Path, name: str, maximum: int) -> bytes:
    """Read a pinned regular file; refuse symlinks, specials and hardlinks.

    On POSIX this pins EVERY component with openat + O_NOFOLLOW, so a swap of
    a directory for a symlink between validation and open still fails. Where
    the OS lacks openat/dir_fd (Windows), a resolve-and-check fallback keeps
    the same Denied contract without claiming TOCTOU pinning.
    """
    parts = relative_parts(name)
    if _platform.HAS_OPENAT and _platform.HAS_DIR_FD:
        return _safe_read_openat(root, name, parts, maximum)
    return _safe_read_portable(root, name, parts, maximum)


def _safe_read_openat(root: Path, name: str, parts: tuple[str, ...], maximum: int) -> bytes:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                            os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW |
                          os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Denied("Only regular, non-hardlinked files are readable")
            if before.st_size > maximum:
                raise LimitExceeded("File exceeds configured byte limit")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if len(data) > maximum:
                raise LimitExceeded("File grew past configured byte limit")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise Denied("File changed while reading; retry at a stable boundary")
            return data
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc
    finally:
        os.close(fd)


def _safe_read_portable(root: Path, name: str, parts: tuple[str, ...], maximum: int) -> bytes:
    target = root.joinpath(*parts)
    try:
        # Refuse any symlink along the path, including the final component.
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise Denied(f"Unsafe or unavailable path: {name}")
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise Denied("Only regular, non-hardlinked files are readable")
        if getattr(info, "st_nlink", 1) != 1:
            raise Denied("Only regular, non-hardlinked files are readable")
        if info.st_size > maximum:
            raise LimitExceeded("File exceeds configured byte limit")
        data = target.read_bytes()[:maximum + 1]
        if len(data) > maximum:
            raise LimitExceeded("File grew past configured byte limit")
        return data
    except (Denied, LimitExceeded):
        raise
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc
