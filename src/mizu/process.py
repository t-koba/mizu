"""Bounded subprocesses with portable cancellation and no host shell."""
from __future__ import annotations

import contextlib
import dataclasses
import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import platform as _platform


@dataclasses.dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    reason: str
    seconds: float


def environment(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_RUNTIME_DIR",
               "XDG_CONFIG_HOME", "XDG_DATA_HOME", "DBUS_SESSION_BUS_ADDRESS"}
    if _platform.IS_WINDOWS:
        # Minimal Windows system variables needed to spawn processes at all.
        allowed |= {"SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"}
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    return {**env, **(extra or {})}


def terminate(process: subprocess.Popen, grace: float = 1.0) -> None:
    if _platform.IS_POSIX:
        import contextlib
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
                process.wait(timeout=5)
            return
    _platform.terminate_process(process)


def run(argv: Sequence[str], *, timeout: float, maximum: int,
        cwd: Path | None = None, env: dict[str, str] | None = None,
        cancel: Callable[[], bool] = lambda: False, input_data: bytes = b"") -> Result:
    """Run argv with timeout/output/cancel bounds. Portable thread-pump design.

    The previous selector + non-blocking-fd loop only works on POSIX (Windows
    selectors do not support pipes). Threads with blocking reads behave the
    same on all three OSes with no performance loss for bounded outputs.
    """
    started = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    reason = "exited"
    process = subprocess.Popen(list(argv), cwd=cwd, env=environment() if env is None else env,
                               stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **_platform.popen_kwargs())
    assert process.stdout and process.stderr
    truncated = threading.Event()

    def pump(stream, key: str) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                used = len(output["stdout"]) + len(output["stderr"])
                room = max(0, maximum - used)
                output[key].extend(chunk[:room])
                if used + len(chunk) > maximum:
                    truncated.set()
                    # Drain without storing so a flooding child cannot block.
                    while stream.read(65536):
                        pass
                    return
        except (OSError, ValueError):
            return

    readers = [threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True)]
    for reader in readers:
        reader.start()
    if input_data:
        assert process.stdin
        try:
            try:
                process.stdin.write(input_data)
            except (BrokenPipeError, OSError):
                pass
        finally:
            with contextlib.suppress(Exception):
                process.stdin.close()
    try:
        # Wait for process exit AND pipe EOF (orphans holding pipes keep the
        # run bounded until timeout, then the whole group is signalled).
        while True:
            if cancel():
                reason = "cancelled"
                break
            if truncated.is_set():
                reason = "output_limit"
                break
            if time.monotonic() - started >= timeout:
                reason = "timeout"
                break
            exited = process.poll() is not None
            drained = not any(r.is_alive() for r in readers)
            if exited and drained:
                break
            if exited and not drained:
                # Parent is gone but an orphan still holds a pipe: keep the
                # bound, do not return "exited" while output is still open.
                time.sleep(0.02)
                continue
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        if reason != "exited":
            terminate(process)
        else:
            if truncated.is_set():
                reason = "output_limit"
                terminate(process)
            elif any(r.is_alive() for r in readers):
                # Raced: pipes still open after exit (orphan). Enforce the bound.
                remaining = max(0.1, timeout - (time.monotonic() - started))
                deadline = time.monotonic() + remaining
                while any(r.is_alive() for r in readers) and time.monotonic() < deadline:
                    if cancel() or truncated.is_set():
                        break
                    time.sleep(0.02)
                if any(r.is_alive() for r in readers):
                    reason = "timeout" if not truncated.is_set() else "output_limit"
                    terminate(process)
        return Result(process.returncode, output["stdout"].decode("utf-8", "replace"),
                      output["stderr"].decode("utf-8", "replace"), reason,
                      round(time.monotonic() - started, 3))
    finally:
        terminate(process)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                with contextlib.suppress(Exception):
                    stream.close()
