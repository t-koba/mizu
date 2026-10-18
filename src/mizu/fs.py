"""Durable local-file primitives. No database, stale PID files, or implicit shell."""
from __future__ import annotations

import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator

from . import platform as _platform
from .errors import Busy, Denied, LimitExceeded

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
#: Fixed mechanism preview bound (128 KiB): diff/editor/insight previews and
#: canonical list framing. Not a TOML knob; file_bytes/snapshot_bytes stay policy.
PREVIEW_BYTES = 131072
#: Maximum decoded characters in one file-read page. This keeps escaped tool
#: responses well below the protocol frame bound even for control-heavy text.
READ_PAGE_CHARS = 8192
#: MCP/socket LF framing bound shared by the bridge, the MCP proxy and the
#: Editor capsule server (each rejects anything above it).
MAX_FRAME = 1024 * 1024


def _reject_nonfinite_constant(value: str) -> object:
    raise ValueError("Non-finite JSON value")


def _reject_nonfinite_number(text: str) -> float:
    value = float(text)
    if not value == value or value in (float("inf"), float("-inf")):
        raise ValueError("Non-finite JSON number")
    return value


def loads_finite(raw: bytes | str) -> Any:
    """Parse one JSON document, refusing non-finite numbers.

    ``parse_constant`` rejects NaN/Infinity literals; ``parse_float``
    rejects valid-syntax numbers (for example ``1e9999``) that overflow
    to infinity. Finite numbers, including large integers within the
    decoder bound, keep working.
    """
    return json.loads(raw, parse_constant=_reject_nonfinite_constant,
                      parse_float=_reject_nonfinite_number)
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
    # Unsupported directory sync is distinct from a real durability failure.
    if not _platform.IS_POSIX:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                raise
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
            os.fsync(stream.fileno())
        if exclusive:
            try:
                if _platform.IS_WINDOWS:
                    # Windows rename is atomic and fails if the destination
                    # exists; unlike a hard link, it leaves no second name if
                    # cleanup of the temporary path is delayed by the host.
                    _move_with_retry(temporary, path, exclusive=True)
                else:
                    try:
                        os.link(temporary, path, follow_symlinks=False)
                    except (TypeError, NotImplementedError):
                        os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != data:
                    raise Denied(f"Immutable object conflict: {path.name}") from None
        else:
            _move_with_retry(temporary, path, exclusive=False)
        sync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(temporary)


def _move_with_retry(temporary: Path, path: Path, *, exclusive: bool) -> None:
    """Atomically publish a staged file, tolerating transient locks.

    Concurrent readers (Windows mandatory locking, antivirus scanners) can
    briefly hold the destination; retry a bounded window before failing.
    FileExistsError still fails fast for the immutable-conflict path above,
    as do the create-only link attempts.
    """
    move = os.rename if exclusive and _platform.IS_WINDOWS else os.replace
    deadline = time.monotonic() + 0.5
    while True:
        try:
            move(temporary, path)
            return
        except PermissionError:
            # Transient sharing violation (a concurrent reader holds the
            # destination); fail loud once the window passes.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    atomic_write(path, canonical(value), exclusive=exclusive)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_bytes(), parse_constant=lambda v: (_ for _ in ()).throw(ValueError("Non-finite JSON value")))
    except FileNotFoundError:
        return default


#: In-process exclusion for lock files. OS advisory locks (msvcrt on
#: Windows) are per-process, so a second open from this process would
#: otherwise succeed and break single-writer and budget admission. The
#: per-path threading lock provides the missing within-process mutual
#: exclusion; the OS lock still provides cross-process exclusion.
#: Fixed liveness bound per ADR-026 (not a knob): no operator would
#: plausibly tune an intra-process deadlock guard.
_LOCAL_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
#: Blocking acquire bound so a re-entrant blocking request fails as Busy
#: instead of hanging the daemon thread forever. Fixed liveness bound
#: (same 30 s as platform.lock_fd on Windows); see ADR-026.
_LOCAL_BLOCKING_TIMEOUT = 30.0


def _local_key(path: Path) -> str:
    # Canonicalize through the filesystem so aliased paths (symlinked
    # parent, `.`/`..`, case variants on Windows) share one guard entry.
    # realpath resolves existing components; the lock file itself may not
    # exist yet, but its parent does (mkdir before acquire).
    return os.path.normcase(os.path.realpath(str(path)))


def _acquire_local(key: str, *, blocking: bool) -> threading.Lock:
    with _LOCAL_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    if blocking:
        held = local.acquire(True, _LOCAL_BLOCKING_TIMEOUT)
    else:
        held = local.acquire(False)
    if not held:
        raise BlockingIOError("Already running")
    return local


@contextlib.contextmanager
def lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Kernel releases the lock on death. Never unlink a lock inode."""
    mkdir(path.parent)
    try:
        local = _acquire_local(_local_key(path), blocking=blocking)
    except BlockingIOError:
        raise Busy(f"Already running: {path.stem}") from None
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        local.release()
        raise
    try:
        try:
            _platform.lock_fd(fd, blocking=blocking)
        except BlockingIOError:
            raise Busy(f"Already running: {path.stem}") from None
        yield
    except Busy:
        raise
    except BlockingIOError:
        raise Busy(f"Already running: {path.stem}") from None
    finally:
        with contextlib.suppress(Exception):
            _platform.unlock_fd(fd)
        with contextlib.suppress(Exception):
            os.close(fd)
        local.release()


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
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive or any(p in ("", ".", "..") for p in parts):
        raise Denied("Path must be relative, without empty, '.' or '..' components")
    if _platform.IS_WINDOWS and any(':' in p or p.endswith((' ', '.')) or
            PureWindowsPath(p).is_reserved() for p in parts):
        raise Denied("Path is not representable on Windows")
    return parts


def safe_read(root: Path, name: str, maximum: int) -> bytes:
    """Read a pinned regular file; refuse symlinks, specials and hardlinks.

    On POSIX this pins EVERY component with openat + O_NOFOLLOW, so a swap of
    a directory for a symlink between validation and open still fails. Where
    the OS lacks openat/dir_fd (Windows), a resolve-and-check fallback keeps
    the same Denied contract without claiming TOCTOU pinning.
    """
    return safe_read_info(root, name, maximum)[0]


def safe_read_info(root: Path, name: str, maximum: int) -> tuple[bytes, os.stat_result]:
    """Return bounded bytes and metadata from the same opened regular file.

    No retries; unstable reads raise Denied. POSIX pins path components;
    portable reads check reparse points and identity without claiming pinning.
    """
    parts = relative_parts(name)
    if _platform.HAS_OPENAT and _platform.HAS_DIR_FD:
        return _safe_read_openat(root, name, parts, maximum)
    return _safe_read_portable(root, name, parts, maximum)


def _safe_read_openat(root: Path, name: str, parts: tuple[str, ...], maximum: int) -> tuple[bytes, os.stat_result]:
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
            if _file_identity(before) != _file_identity(after):
                raise Denied("File changed while reading; retry at a stable boundary")
            return data, after
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc
    finally:
        os.close(fd)


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _same_file(first, second):
    """Cross-API identity check: path stat and handle stat source device,
    index and creation fields differently on Windows (host-dependent
    mismatches observed for freshly written files), so across APIs only
    mode/nlink/size/mtime bind. Same-handle pairs stay exact."""
    if os.name == "nt":
        key = lambda info: (info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)
        return key(first) == key(second)
    return _file_identity(first) == _file_identity(second)


def _safe_read_portable(root: Path, name: str, parts: tuple[str, ...], maximum: int) -> tuple[bytes, os.stat_result]:
    target = root.joinpath(*parts)
    try:
        # Refuse any symlink along the path, including the final component.
        def check_components():
            current = root
            for part in ('', *parts):
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise Denied(f"Unsafe or unavailable path: {name}")
            if not target.resolve().is_relative_to(root.resolve()):
                raise Denied(f"Unsafe or unavailable path: {name}")
        check_components()
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise Denied("Only regular, non-hardlinked files are readable")
        if getattr(info, "st_nlink", 1) != 1:
            raise Denied("Only regular, non-hardlinked files are readable")
        if info.st_size > maximum:
            raise LimitExceeded("File exceeds configured byte limit")
        with target.open('rb') as stream:
            before = os.fstat(stream.fileno())
            # The handle is the truth for type/link count; path stat already
            # screened these, but the binding check must use handle values.
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Denied("Only regular, non-hardlinked files are readable")
            if not _same_file(info, before):
                raise Denied("File changed before reading")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(data) > maximum:
            raise LimitExceeded("File grew past configured byte limit")
        check_components()
        if _file_identity(before) != _file_identity(after) or not _same_file(after, target.stat()):
            raise Denied("File changed while reading; retry at a stable boundary")
        return data, after
    except (Denied, LimitExceeded):
        raise
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc


def safe_write(root: Path, name: str, data: bytes) -> None:
    """Atomically create-or-replace one regular file without creating parents.

    The destination must be an exact relative path under ``root`` (see
    :func:`relative_parts`): absolute paths, traversal, backslashes and
    ``.``/``..`` components are refused. Parent directories must already
    exist and no parent is created. Symlinked parents or targets,
    hardlinked or special existing destinations, and hardlinked results
    are refused. The write stages a temporary sibling and renames it, so
    a failed validation or write never partially replaces the destination;
    only complete bytes are published, followed by a directory fsync.

    On POSIX the parent chain is pinned with ``openat``/``O_NOFOLLOW`` so
    a swapped component fails instead of following it. Where the OS lacks
    ``openat``/``dir_fd`` (Windows) a resolve-and-check fallback keeps the
    same Denied contract without claiming TOCTOU pinning.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise Denied("Only bytes are writable")
    parts = relative_parts(name)
    if _platform.HAS_OPENAT and _platform.HAS_DIR_FD:
        return _safe_write_openat(root, name, parts, bytes(data))
    return _safe_write_portable(root, name, parts, bytes(data))


def _safe_write_openat(root: Path, name: str, parts: tuple[str, ...], data: bytes) -> None:
    import uuid as _uuid
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                                os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise Denied(f"Unsafe or unavailable path: {name}") from exc
            os.close(fd)
            fd = child
        try:
            existing = os.lstat(parts[-1], dir_fd=fd)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise Denied(f"Unsafe or unavailable path: {name}") from exc
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise Denied(f"Unsafe or unavailable path: {name}")
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise Denied("Only regular, non-hardlinked files are writable")
        temporary = None
        temp_fd = None
        try:
            for _ in range(8):
                candidate = f".new-{_uuid.uuid4().hex}"
                try:
                    temp_fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise Denied(f"Unsafe or unavailable path: {name}") from exc
                temporary = candidate
                break
            if temporary is None or temp_fd is None:
                raise Denied(f"Unsafe or unavailable path: {name}")
            try:
                view = memoryview(data)
                while view:
                    written = os.write(temp_fd, view)
                    if written == 0:
                        raise Denied("Unable to write complete file")
                    view = view[written:]
                os.fsync(temp_fd)
                info = os.fstat(temp_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Denied("Only regular, non-hardlinked files are writable")
            finally:
                os.close(temp_fd)
                temp_fd = None
            try:
                os.rename(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            except OSError as exc:
                raise Denied(f"Unsafe or unavailable path: {name}") from exc
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
                    raise
            # The rename consumed the temporary name; do not unlink it.
            temporary = None
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=fd)
    except (Denied, LimitExceeded):
        raise
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _safe_write_portable(root: Path, name: str, parts: tuple[str, ...], data: bytes) -> None:
    target = root.joinpath(*parts)
    parent = target.parent
    try:
        def check_components() -> None:
            current = root
            for part in ("", *parts):
                current = current / part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    # Only the final destination may be absent; every parent
                    # must already exist (no parent creation).
                    if current != target:
                        raise Denied(f"Unsafe or unavailable path: {name}")
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise Denied(f"Unsafe or unavailable path: {name}")
            try:
                if not target.resolve().is_relative_to(root.resolve()):
                    raise Denied(f"Unsafe or unavailable path: {name}")
            except OSError as exc:
                raise Denied(f"Unsafe or unavailable path: {name}") from exc
        check_components()
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise Denied(f"Unsafe or unavailable path: {name}")
            if not stat.S_ISREG(existing.st_mode):
                raise Denied("Only regular, non-hardlinked files are writable")
            if getattr(existing, "st_nlink", 1) != 1:
                raise Denied("Only regular, non-hardlinked files are writable")
        if not parent.is_dir() or parent.is_symlink():
            raise Denied(f"Unsafe or unavailable path: {name}")
        fd, temporary = tempfile.mkstemp(prefix=".new-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            check_components()
            os.replace(temporary, target)
            temporary = None
            sync_dir(parent)
        finally:
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError, OSError):
                    os.unlink(temporary)
    except (Denied, LimitExceeded):
        raise
    except OSError as exc:
        raise Denied(f"Unsafe or unavailable path: {name}") from exc


def page(items, offset: int = 0, limit: int = 1000, *, maximum: int = PREVIEW_BYTES) -> dict:
    """Deterministic bounded list projection, offset is an index in sorted input.

    Schema: items/offset/next_offset/truncated. Byte bound applies to canonical
    item JSON. No retry or implicit continuation; caller explicitly pages.
    """
    selected = []
    used = 0
    for item in items[offset:offset + limit]:
        size = len(canonical(item))
        if used + size > maximum:
            break
        selected.append(item)
        used += size
    end = offset + len(selected)
    return {"items": selected, "offset": offset,
            "next_offset": end if end < len(items) else None, "truncated": end < len(items)}


def text_preview(value: str, maximum: int = 8192) -> tuple[str, bool]:
    """UTF-8 byte preview with explicit truncation; full evidence stays on disk."""
    raw = value.encode('utf-8')
    return raw[:maximum].decode('utf-8', 'ignore'), len(raw) > maximum
