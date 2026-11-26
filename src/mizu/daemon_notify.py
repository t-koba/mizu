"""Kernel-backed change notification for the resident daemon's idle wait.

A settled daemon must add no periodic work: no stat calls and no wakeups on a
timer between a real change and its exact next deadline. This module registers
the fixed fingerprint set with each supported platform's own change notification
once, then exposes a blocking wait over that notification, an exact monotonic
deadline and a stop signal. The kernel reports a change; the caller recomputes
the fingerprint exactly once after a wake, so a notification is only a reason to
look, never a poll loop.

The project root is watched only for top-level entry events, and where the
platform reports event names those names are filtered to the fingerprint's
fixed set. Content writes to unwatched root children (ledger journals, WAL
files) therefore cannot wake the daemon or make it recompute the fingerprint.
A path that cannot be watched is returned in ``Watcher.unwatchable`` as a fact;
it is never a reason to fall back to polling. This coverage is established for
local filesystems. A successful registration on a network-backed path does not
prove that remote changes are observable; there is no reliable cross-platform
predicate for that, so such coverage is unverified rather than assumed, and a
registration failure is still reported as a fact.

Backends: inotify on Linux, kqueue EVFILT_VNODE on macOS, and
FindFirstChangeNotification/WaitForMultipleObjects on Windows, all via the
standard library. One stop bridge is owned per stop event for the daemon's
lifetime and released deterministically when that event is collected.
"""
from __future__ import annotations

import _thread
import contextlib
import ctypes
import math
import os
import select
import struct
import sys
import time
import weakref
from pathlib import Path

# inotify event: watch descriptor, mask, cookie, name length.
_IN_EVENT = struct.Struct("iIII")

# Entry events on the project root: an atomic writer's temp-file create and
# final rename are visible, but content writes to unwatched children are not.
_IN_ENTRY = (
    0x00000040  # IN_MOVED_FROM
    | 0x00000080  # IN_MOVED_TO
    | 0x00000100  # IN_CREATE
    | 0x00000200  # IN_DELETE
)
# Filtered directories carry names, so content events are safe to include: an
# unwatched child's event is discarded before the caller ever sees a wake.
_IN_FILTERED = _IN_ENTRY | (
    0x00000002  # IN_MODIFY
    | 0x00000004  # IN_ATTRIB  (fs.touch_directory bumps a watched dir's mtime)
    | 0x00000008  # IN_CLOSE_WRITE
)
# Unfiltered watched directories and files report every relevant event.
_IN_ALL = _IN_FILTERED | (
    0x00000400  # IN_DELETE_SELF
    | 0x00000800  # IN_MOVE_SELF
)


def available() -> bool:
    """True on every supported platform: each has a native backend."""
    return sys.platform.startswith(("linux", "darwin", "win32"))


def config_ancestors(config_file: Path | None, config_files=()) -> dict[str, frozenset]:
    """Existing ancestors of config inputs whose next component is missing.

    A config parent that does not exist at settle time cannot itself be
    watched. The nearest existing ancestor can: its first missing component is
    the name that, once created, extends watch coverage into the restored
    subtree. Genuinely unwatchable ancestors are reported by the backend.
    """
    out: dict[str, set[str]] = {}
    for path in (config_file, *config_files):
        if path is None:
            continue
        current = Path(path).parent
        missing: list[str] = []
        while not current.is_dir():
            missing.append(current.name)
            parent = current.parent
            if parent == current:
                break
            current = parent
        if missing and current.is_dir():
            out.setdefault(os.fspath(current), set()).add(missing[-1])
    return {key: frozenset(names) for key, names in out.items()}


def watch_dirs(root: Path | None, config_file: Path | None,
               config_files=()) -> tuple[Path, ...]:
    """Directories whose entry changes can perturb the fingerprint."""
    from .daemon_wake import WATCHED_DIRS
    seen: set[str] = set()
    out: list[Path] = []
    for path in (config_file, *config_files):
        if path is None:
            continue
        parent = Path(path).parent
        key = os.fspath(parent)
        if key not in seen:
            seen.add(key)
            out.append(parent)
    for key in config_ancestors(config_file, config_files):
        if key not in seen:
            seen.add(key)
            out.append(Path(key))
    if root is not None:
        for path in (root, *(root / name for name in WATCHED_DIRS)):
            key = os.fspath(path)
            if key not in seen:
                seen.add(key)
                out.append(path)
    return tuple(p for p in out if p.is_dir())


def watch_files(root: Path | None, config_file: Path | None,
                config_files=()) -> tuple[Path, ...]:
    """Existing individual paths whose in-place writes must wake the daemon.

    inotify and Windows directory watches already report child writes; kqueue's
    EVFILT_VNODE on a directory reports only entry changes, so existing files
    are registered there directly as well.
    """
    from .daemon_wake import WATCHED_FILES
    out: list[Path] = []
    for path in (config_file, *config_files):
        if path is not None and path.is_file():
            out.append(path)
    if root is not None:
        for name in WATCHED_FILES:
            path = root / name
            if path.is_file():
                out.append(path)
    return tuple(out)


def _interesting_names(root, config_file, config_files) -> dict[str, frozenset]:
    """Directory -> child names that can change the fingerprint.

    Only the project root and config parents are filtered; every watched
    directory is itself in the fingerprint, so any of its events is relevant.
    """
    from .daemon_wake import WATCHED_DIRS, WATCHED_FILES
    names: dict[str, frozenset] = {}
    if root is not None:
        names[os.fspath(root)] = frozenset((*WATCHED_FILES, *WATCHED_DIRS))
    for path in (config_file, *config_files):
        if path is None:
            continue
        key = os.fspath(Path(path).parent)
        name = os.fspath(Path(path).name)
        names[key] = names.get(key, frozenset()) | {name}
    for key, missing in config_ancestors(config_file, config_files).items():
        names[key] = names.get(key, frozenset()) | missing
    return names


# One stop bridge per stop event, owned for the daemon's lifetime. A raw fd or
# Windows HANDLE has no destructor: caching it in a weak map alone leaks it (and
# its monitor thread) when the event is collected. The bridge owns both and a
# weakref.finalize closes them at that boundary.
_STOP_BRIDGES = weakref.WeakKeyDictionary()


class _StopBridge:
    def __init__(self, stop):
        self._closed = False
        if sys.platform == "win32":
            from ctypes import wintypes
            self._kind = "win32"
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                         wintypes.BOOL, wintypes.LPCWSTR)
            k32.CreateEventW.restype = wintypes.HANDLE
            k32.SetEvent.argtypes = (wintypes.HANDLE,)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            self._k32 = k32
            self._event = k32.CreateEventW(None, False, False, None)
            if not self._event:
                raise OSError(ctypes.get_last_error(), "CreateEventW")
            event = self._event

            def monitor():
                stop.wait()
                k32.SetEvent(event)
        else:
            self._kind = "posix"
            self._read_fd, self._write_fd = os.pipe()
            os.set_blocking(self._read_fd, False)
            os.set_blocking(self._write_fd, False)
            write_fd = self._write_fd

            def monitor():
                stop.wait()
                with contextlib.suppress(OSError):
                    os.write(write_fd, b"\0")

        # A raw thread avoids Thread.__init__'s own Event() allocation: some
        # callers replace threading.Event wholesale, and a shared "started"
        # Event would make Thread.start() raise spuriously. Ownership starts at
        # the first acquisition: a failed start must release the pipe or event
        # rather than leaving it to a context manager that was never entered.
        try:
            self._thread = _thread.start_new_thread(monitor, ())
        except BaseException:
            self.close()
            raise

    def fileno(self) -> int:
        return self._read_fd

    def event(self):
        return self._event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._kind == "posix":
            for fd in (self._read_fd, self._write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)
        elif self._event:
            self._k32.CloseHandle(self._event)
            self._event = None


def stop_bridge(stop) -> _StopBridge:
    """The single, owned stop bridge for ``stop`` (created on first use)."""
    bridge = _STOP_BRIDGES.get(stop)
    if bridge is None:
        bridge = _StopBridge(stop)
        _STOP_BRIDGES[stop] = bridge
        weakref.finalize(stop, bridge.close)
    return bridge


class _Inotify:
    def __init__(self, directories, files, stop, interesting):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = (ctypes.c_int,)
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.inotify_rm_watch.argtypes = (ctypes.c_int, ctypes.c_int)
        self._libc = libc
        self._fd = -1
        self._wds: set[int] = set()
        self._filtered: dict[int, frozenset] = {}
        self._stop_fd = stop.fileno()
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self._fd = fd
        try:
            failed: list[Path] = []
            for directory in directories:
                names = interesting.get(os.fspath(directory))
                mask = _IN_FILTERED if names is not None else _IN_ALL
                wd = libc.inotify_add_watch(self._fd, os.fsencode(directory), mask)
                if wd < 0:
                    failed.append(directory)
                else:
                    self._wds.add(wd)
                    if names is not None:
                        self._filtered[wd] = names
            for path in files:
                wd = libc.inotify_add_watch(self._fd, os.fsencode(path), _IN_ALL)
                if wd < 0:
                    failed.append(path)
                else:
                    self._wds.add(wd)
            self.unwatchable = tuple(failed)
        except BaseException:
            self.close()
            raise

    def _drain(self) -> bool:
        """Consume all pending events; report only a relevant one."""
        relevant = False
        size = _IN_EVENT.size
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            offset = 0
            while offset + size <= len(chunk):
                wd, _mask, _cookie, length = _IN_EVENT.unpack_from(chunk, offset)
                offset += size
                name = b""
                if length:
                    name = chunk[offset:offset + length].split(b"\0", 1)[0]
                offset += length
                names = self._filtered.get(wd)
                if names is not None:
                    try:
                        decoded = os.fsdecode(name)
                    except (UnicodeDecodeError, ValueError):
                        decoded = ""
                    if decoded not in names:
                        continue
                relevant = True
        return relevant

    def wait(self, deadline):
        while True:
            now = time.monotonic()
            timeout = None if deadline is None else max(0.0, deadline - now)
            if timeout == 0.0:
                return
            ready, _, _ = select.select([self._fd, self._stop_fd], [], [], timeout)
            if self._stop_fd in ready:
                with contextlib.suppress(OSError):
                    os.read(self._stop_fd, 4096)
                return
            if self._fd in ready and self._drain():
                return

    def close(self):
        if self._fd >= 0:
            for wd in tuple(self._wds):
                self._libc.inotify_rm_watch(self._fd, wd)
            self._wds.clear()
            os.close(self._fd)
            self._fd = -1


_KQ_NOTE_NAMES = (
    "KQ_NOTE_DELETE", "KQ_NOTE_WRITE", "KQ_NOTE_EXTEND", "KQ_NOTE_ATTRIB",
    "KQ_NOTE_LINK", "KQ_NOTE_RENAME", "KQ_NOTE_REVOKE",
)


def kqueue_vnode_flags(select_module=select) -> tuple[int, int]:
    """Return (flags, fflags) for a kqueue EVFILT_VNODE registration.

    Python exposes vnode notes as KQ_NOTE_*; the KQ_VNODE_* spelling has no
    attributes, which silently registers no change events. The select namespace
    is an argument so the mapping can be tested without a macOS kqueue.
    """
    flags = (select_module.KQ_EV_ADD | select_module.KQ_EV_ENABLE
             | select_module.KQ_EV_CLEAR)
    fflags = 0
    for name in _KQ_NOTE_NAMES:
        fflags |= getattr(select_module, name, 0)
    return flags, fflags


class _Kqueue:
    def __init__(self, directories, files, stop):
        self._kq = select.kqueue()
        self._fds: list[int] = []
        self._stop_fd = stop.fileno()
        failed: list[Path] = []
        flags, fflags = kqueue_vnode_flags(select)
        try:
            for path in (*directories, *files):
                fd = None
                try:
                    open_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                                  | getattr(os, "O_NONBLOCK", 0))
                    if path in directories:
                        open_flags |= getattr(os, "O_DIRECTORY", 0)
                    fd = os.open(os.fspath(path), open_flags)
                    event = select.kevent(fd, select.KQ_FILTER_VNODE, flags, fflags, 0, 0)
                    self._kq.control([event], 0, 0)
                except OSError:
                    if fd is not None:
                        os.close(fd)
                    failed.append(path)
                    continue
                self._fds.append(fd)
            self.unwatchable = tuple(failed)
            self._kq.control([select.kevent(self._stop_fd, select.KQ_FILTER_READ,
                                            flags, 0, 0, 0)], 0, 0)
        except BaseException:
            self.close()
            raise

    def wait(self, deadline):
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        events = self._kq.control([], 32, timeout)
        for event in events:
            if event.filter == select.KQ_FILTER_READ:
                with contextlib.suppress(OSError):
                    os.read(self._stop_fd, 4096)

    def close(self):
        if self._kq is not None:
            self._kq.close()
            self._kq = None
        for fd in self._fds:
            os.close(fd)
        self._fds = []


class _WinChange:
    """FindFirstChangeNotification + WaitForMultipleObjects, via ctypes."""

    _FILE_NAME = 0x001
    _DIR_NAME = 0x002
    _ATTRIBUTES = 0x004
    _SIZE = 0x008
    _LAST_WRITE = 0x010
    _FILTER = _FILE_NAME | _DIR_NAME | _ATTRIBUTES | _SIZE | _LAST_WRITE
    # The project root has no name filter, so it sees only entry events.
    _ENTRY_FILTER = _FILE_NAME | _DIR_NAME
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF
    _INFINITE = 0xFFFFFFFF
    _INVALID = ctypes.c_void_p(-1).value

    def __init__(self, directories, files, stop, narrow):
        from ctypes import wintypes
        self._wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.FindFirstChangeNotificationW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD)
        k32.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        k32.FindCloseChangeNotification.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.WaitForMultipleObjects.argtypes = (
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD)
        k32.WaitForMultipleObjects.restype = wintypes.DWORD
        self._k32 = k32
        self._handles: list[int] = []
        self._stop_event = stop.event()
        try:
            failed: list[Path] = []
            # MAXIMUM_WAIT_OBJECTS is 64; reserve one slot for the stop event.
            for directory in directories[:62]:
                is_narrow = os.fspath(directory) in narrow
                notify_filter = self._ENTRY_FILTER if is_narrow else self._FILTER
                subtree = False if is_narrow else True
                # FindFirstChangeNotificationW requires a fully qualified path;
                # a relative configured directory would fail every registration.
                # This is an intrinsic Win32 precondition, not watch policy.
                absolute = os.path.abspath(os.fspath(directory))
                handle = k32.FindFirstChangeNotificationW(
                    absolute, subtree, notify_filter)
                if handle in (None, self._INVALID):
                    failed.append(directory)
                else:
                    self._handles.append(handle)
            failed.extend(directories[62:])
            self.unwatchable = tuple(failed)
            self._wait_handles = (wintypes.HANDLE * (len(self._handles) + 1))(
                *self._handles, self._stop_event)
        except BaseException:
            self.close()
            raise

    def wait(self, deadline):
        if deadline is None:
            millis = self._INFINITE
        else:
            millis = max(0, math.ceil((deadline - time.monotonic()) * 1000))
        result = self._k32.WaitForMultipleObjects(
            len(self._wait_handles), self._wait_handles, False, millis)
        if result == self._WAIT_FAILED:
            raise OSError(ctypes.get_last_error(), "WaitForMultipleObjects")
        # One wait per watcher: daemon_wake closes this watcher and registers
        # fresh handles before the next fingerprint reconciliation, so the
        # signalled handle is never waited on again. FindFirstChangeNotification
        # registrations remain one-shot by design; re-arming here would target
        # a discarded handle and turn a healthy watch into a false degraded
        # sleep.

    def close(self):
        # The stop event belongs to the shared bridge, not to this watcher.
        for handle in self._handles:
            self._k32.FindCloseChangeNotification(handle)
        self._handles = []


class Watcher:
    """Platform-neutral watch over the fixed fingerprint path set."""

    def __init__(self, root, config_file, config_files, stop):
        directories = watch_dirs(root, config_file, config_files)
        files = watch_files(root, config_file, config_files)
        interesting = _interesting_names(root, config_file, config_files)
        ancestors = config_ancestors(config_file, config_files)
        narrow = (frozenset({os.fspath(root)}) if root is not None else frozenset())
        narrow = narrow | frozenset(ancestors)
        # The bridge is the daemon's shared lifetime resource: acquire it before
        # any backend descriptor so a failure at either boundary can be rolled
        # back by its own owner without leaking a HANDLE or fd.
        bridge = stop_bridge(stop)
        if sys.platform.startswith("linux"):
            self._backend = _Inotify(directories, files, bridge, interesting)
        elif sys.platform == "darwin":
            self._backend = _Kqueue(directories, files, bridge)
        elif sys.platform == "win32":
            self._backend = _WinChange(directories, files, bridge, narrow)
        else:
            raise OSError(f"no native change-notification backend for {sys.platform}")
        self.unwatchable = self._backend.unwatchable

    def wait(self, deadline):
        self._backend.wait(deadline)

    def close(self):
        self._backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
