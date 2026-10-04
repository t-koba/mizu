"""Single OS abstraction for Mizu. All platform differences live here.

Operation (isolated work, scheduled services) runs on Linux, macOS and
Windows through one portable mechanism each: isolated commands execute in a
Linux OCI container via the configured runtime (`podman` or `docker`), and
schedules render to the platform's own service manager (systemd user units,
launchd plists, Task Scheduler XML). The container interior is always Linux,
so in-container probes stay identical; only host-side spellings differ.

Callers query capabilities and directories here instead of branching on
sys.platform themselves. This keeps per-environment divergence to one module
plus thin per-platform text emitters in services.py.
"""
from __future__ import annotations

import contextlib
import os
import socketserver as _socketserver
import subprocess
import sys
from pathlib import Path

SYSTEM = {"linux": "linux", "darwin": "macos"}.get(sys.platform, "windows" if os.name == "nt" else sys.platform)

IS_LINUX = SYSTEM == "linux"
IS_MACOS = SYSTEM == "macos"
IS_WINDOWS = SYSTEM == "windows" or os.name == "nt"
IS_POSIX = os.name == "posix"

try:
    import fcntl as _fcntl
except ImportError:  # Windows has no fcntl; msvcrt is used instead.
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX has no msvcrt.
    _msvcrt = None

HAS_FLOCK = _fcntl is not None
HAS_MSVCRT_LOCK = _msvcrt is not None

#: Unix-socket servers exist where the interpreter serves them (POSIX).
#: Runners without them (Windows) get a loopback TCP bridge instead, with
#: the same per-run token, framing and bounds (see mizu.bridge).
HAS_UNIX_SOCKET_SERVER = hasattr(_socketserver, "UnixStreamServer")

# Strict openat-style reads need O_NOFOLLOW + O_DIRECTORY + dir_fd, which only
# POSIX provides. Windows uses a best-effort resolve-and-check fallback that
# preserves the Denied-on-symlink contract without claiming TOCTOU pinning.
HAS_OPENAT = (
    IS_POSIX
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "open")  # always true; keeps the capability explicit
)

try:
    _HAS_DIR_FD = os.supports_dir_fd is not None and len(os.supports_dir_fd) > 0  # type: ignore[attr-defined]
except AttributeError:
    _HAS_DIR_FD = IS_POSIX

HAS_DIR_FD = bool(_HAS_DIR_FD) and IS_POSIX

#: Platforms with a service renderer in services.py.
SERVICE_SYSTEMS = ("linux", "macos", "windows")


def is_root() -> bool:
    """Portable privilege check. Windows has no geteuid; treat as non-root."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None:
        with contextlib.suppress(OSError, AttributeError):
            return geteuid() == 0
    return False


def uid_gid() -> tuple[int, int] | None:
    """Current (uid, gid) on POSIX, None where the concept does not exist."""
    getuid, getgid = getattr(os, "getuid", None), getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return None
    with contextlib.suppress(OSError, AttributeError):
        return (getuid(), getgid())
    return None


def config_home() -> Path:
    """Portable config base: XDG_CONFIG_HOME, then APPDATA on Windows, else ~/.config."""
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override)
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
    return Path.home() / ".config"


def default_config_file() -> Path:
    return config_home() / "mizu" / "config.toml"


def container_user() -> str:
    """`uid:gid` for container `--user`. Windows has no uids; use a fixed
    non-root pair so the non-root probe holds on every host."""
    ids = uid_gid()
    if ids is not None:
        return f"{ids[0]}:{ids[1]}"
    return "1000:1000"


def service_dir(system: str | None = None) -> Path:
    """Default service definition directory for a platform."""
    name = system or SYSTEM
    if name == "macos":
        return Path.home() / "Library" / "LaunchAgents"
    if name == "windows":
        return config_home() / "mizu" / "tasks"
    if name != "linux":
        raise ValueError(f"Unsupported service platform: {name}")
    return config_home() / "systemd" / "user"


def set_umask() -> None:
    """Restrictive creation mask where supported; no-op on Windows."""
    umask = getattr(os, "umask", None)
    if umask is not None:
        with contextlib.suppress(OSError, AttributeError, NotImplementedError):
            umask(0o077)


def secure_chmod(path: Path, mode: int) -> None:
    """chmod on POSIX; best-effort on Windows where ACLs differ.

    Production permission guarantees (0600 credentials, 0700 dirs, 0600
    sockets) hold on Linux/macOS. On Windows the call is attempted and
    OSError is suppressed: callers must not treat Windows ACLs as equivalent.
    """
    if IS_WINDOWS:
        with contextlib.suppress(OSError, AttributeError, NotImplementedError):
            os.chmod(path, mode)
        return
    os.chmod(path, mode)


#: Windows blocking-lock retry bound (fixed liveness bound per ADR-026,
#: not a knob): mirrors fs._LOCAL_BLOCKING_TIMEOUT so a contended
#: blocking acquire fails as Busy instead of hanging the daemon thread.
WINDOWS_LOCK_TIMEOUT = 30.0


def lock_fd(fd: int, *, blocking: bool = True) -> bool:
    """Exclusive advisory lock on an open fd. Returns False on contention.

    Uses fcntl.flock on POSIX, msvcrt.locking on Windows. Raises Busy-style
    BlockingIOError on contention so callers map it once.
    """
    if _fcntl is not None:
        flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
        _fcntl.flock(fd, flags)
        return True
    if _msvcrt is not None:
        # LK_NBLCK is non-blocking; emulate blocking with a short retry loop
        # so the blocking=True contract holds for local development.
        import time

        mode = _msvcrt.LK_NBLCK  # type: ignore[attr-defined]
        if blocking:
            deadline = time.monotonic() + WINDOWS_LOCK_TIMEOUT
            while True:
                try:
                    _msvcrt.locking(fd, mode, 1)  # type: ignore[attr-defined]
                    return True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise BlockingIOError("Lock contention")
                    time.sleep(0.05)
        try:
            _msvcrt.locking(fd, mode, 1)  # type: ignore[attr-defined]
            return True
        except OSError as exc:
            raise BlockingIOError("Already running") from exc
    # No advisory locking available: fail loudly rather than pretend.
    raise OSError("No file-locking primitive on this platform")


def unlock_fd(fd: int) -> None:
    if _fcntl is not None:
        with contextlib.suppress(OSError):
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        with contextlib.suppress(OSError):
            try:
                os.lseek(fd, 0, os.SEEK_SET)
            except OSError:
                pass
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


def popen_kwargs() -> dict:
    """Portable detached-spawn arguments for subprocess.Popen.

    POSIX uses start_new_session (setsid) so terminate() can signal the whole
    group. Windows has no setsid; CREATE_NEW_PROCESS_GROUP detaches similarly.
    """
    if IS_POSIX:
        return {"start_new_session": True, "close_fds": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    kwargs: dict = {"close_fds": True}
    if flags:
        kwargs["creationflags"] = flags
    return kwargs



def spawn(argv, **kwargs):
    """Spawn a bounded process tree. Windows starts suspended until assigned
    to a kill-on-close Job Object; failure terminates before model code runs.
    POSIX uses the process session selected by popen_kwargs().
    """
    if not IS_WINDOWS:
        return subprocess.Popen(argv, **kwargs)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    class BASIC(ctypes.Structure):
        _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                    ("Flags", wintypes.DWORD), ("Min", ctypes.c_size_t), ("Max", ctypes.c_size_t),
                    ("Active", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                    ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
    try:
        info = EXTENDED()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
        process = subprocess.Popen(argv, **kwargs)
        if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the primary thread handle. Enumerate the suspended
        # process's threads and resume with the documented Win32 API.
        class THREADENTRY(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("tid", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("priority", wintypes.LONG), ("delta", wintypes.LONG),
                        ("flags", wintypes.DWORD)]
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY))
        kernel.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(THREADENTRY))
        kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = False
        try:
            entry = THREADENTRY()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == process.pid:
                    thread = kernel.OpenThread(0x2, False, entry.tid)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xffffffff:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed = True
                    finally:
                        kernel.CloseHandle(thread)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if not resumed:
                raise OSError("No suspended process thread found")
        finally:
            kernel.CloseHandle(snapshot)
        process._mizu_job = job
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait(timeout=.2 if grace == 0 else 5)
        kernel.CloseHandle(job)
        raise

def terminate_process(process: "subprocess.Popen", grace: float = 1.0) -> None:
    """Signal a spawned group on POSIX, terminate/kill on Windows.

    Schema/bounds: SIGTERM, wait `grace`, SIGKILL, wait up to 5s.
    Trust: local process only. Retry: none. Evidence: none.
    Failure: suppresses lookup/permission errors; never raises for dead PIDs.
    """
    import signal as _signal

    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            killpg(process.pid, _signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            killpg(process.pid, _signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=.2 if grace == 0 else 5)
        return
    job = getattr(process, "_mizu_job", None)
    if job is not None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        if not kernel.CloseHandle(job):
            raise ctypes.WinError(ctypes.get_last_error())
        process._mizu_job = None
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=.2 if grace == 0 else 5)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        process.terminate()
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=.2 if grace == 0 else 5)


def credentials_owner_ok(path: Path) -> bool:
    """Strict owner/mode check on POSIX; symlink refusal plus best-effort on Windows."""
    if path.is_symlink():
        return False
    if IS_WINDOWS:
        # Windows ACLs have no uid/mode bits; refuse symlinks and accept the
        # file itself. Production secrecy still requires a Linux host.
        return True
    try:
        info = path.stat()
    except OSError:
        return False
    if info.st_mode & 0o077:
        return False
    ids = uid_gid()
    if ids is not None and info.st_uid != ids[0]:
        return False
    return True


def prepare_pipe(stream) -> None:
    """POSIX polling pipes; Windows read readiness is queried explicitly."""
    if IS_POSIX:
        os.set_blocking(stream.fileno(), False)


def read_pipe(stream, maximum: int) -> bytes:
    """Read available raw pipe bytes, b'' on EOF, BlockingIOError if idle."""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes
        available = wintypes.DWORD()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.PeekNamedPipe.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                        wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
        handle = _msvcrt.get_osfhandle(stream.fileno())
        if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):
                return b""
            raise ctypes.WinError(error)
        if not available.value:
            raise BlockingIOError("Pipe has no available data")
        maximum = min(maximum, available.value)
    return os.read(stream.fileno(), maximum)


def cancel_pipe_io(thread) -> None:
    """Cancel an owned Windows synchronous pipe operation before fd close."""
    if not IS_WINDOWS or not thread.is_alive():
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenThread(0x0001, False, thread.native_id)  # THREAD_TERMINATE
    if not handle:
        # The thread can exit between is_alive and OpenThread.
        if not thread.is_alive():
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.CancelSynchronousIo(handle) and ctypes.get_last_error() != 1168:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
