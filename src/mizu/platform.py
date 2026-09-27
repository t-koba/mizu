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
            deadline = time.monotonic() + 30
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
    kwargs: dict = {"close_fds": False}
    if flags:
        kwargs["creationflags"] = flags
    return kwargs


def terminate_process(process: "subprocess.Popen") -> None:
    """Signal a spawned group on POSIX, terminate/kill on Windows."""
    import signal as _signal

    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            killpg(process.pid, _signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            killpg(process.pid, _signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=5)
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
        process.wait(timeout=5)


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
