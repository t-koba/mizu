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


def _text(raw: bytearray) -> str:
    """Decode captured output with CRLF folded to LF.

    Windows translates a child's LF to CRLF on pipes, so without folding the
    same command records different bytes per host. Byte bounds are accounted
    pre-decode in pump(); this only normalizes the recorded text.
    """
    return raw.decode("utf-8", "replace").replace("\r\n", "\n")


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
    """Terminate a spawned process group with an explicit grace period."""
    _platform.terminate_process(process, grace=grace)



def _input_thread(stream, data: bytes, stop: threading.Event, errors: list,
                  *, close: bool = False) -> threading.Thread:
    """Raw, interruptible writes. A partial frame is never retried."""
    _platform.prepare_pipe(stream)
    def write():
        try:
            view = memoryview(data)
            while view and not stop.is_set():
                try:
                    count = os.write(stream.fileno(), view[:4096])
                    if not count:
                        raise OSError("Pipe write made no progress")
                    view = view[count:]
                except BlockingIOError:
                    stop.wait(.01)
        except (OSError, ValueError) as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            if close:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
    thread = threading.Thread(target=write, daemon=True, name="mizu-input")
    thread.start()
    return thread


def _stop_io(stop: threading.Event, threads) -> None:
    stop.set()
    for thread in threads:
        _platform.cancel_pipe_io(thread)
    # Polling reads/nonblocking writes exit promptly; Windows synchronous
    # writes are cancelled before closing descriptors. No buffered pipe locks.
    for thread in threads:
        thread.join(timeout=.2)
        if thread.is_alive():
            raise OSError("Pipe operation did not stop after cancellation")


def send_bounded(process, data: bytes, *, deadline: float, cancel: Callable[[], bool]) -> None:
    """Send one RPC frame under the caller's deadline; never replay it.

    Failed/partial sends stop the process group. Delivery is unknown, not
    retryable. Cancellation is checked before starting and after completion.
    """
    from .errors import Cancelled, LimitExceeded, ProtocolError
    if cancel():
        raise Cancelled("RPC send cancelled")
    if time.monotonic() >= deadline:
        raise LimitExceeded("RPC input deadline exceeded")
    stop, errors = threading.Event(), []
    thread = _input_thread(process.stdin, data, stop, errors)
    try:
        while thread.is_alive():
            if cancel():
                raise Cancelled("RPC send cancelled")
            if time.monotonic() >= deadline:
                raise LimitExceeded("RPC input deadline exceeded")
            thread.join(timeout=min(.01, max(0, deadline-time.monotonic())))
        if cancel():
            raise Cancelled("RPC send cancelled")
        if errors:
            raise ProtocolError("RPC input closed; delivery unknown") from errors[0]
    except Exception:
        terminate(process, grace=0)
        raise
    finally:
        _stop_io(stop, (thread,))


def run(argv: Sequence[str], *, timeout: float, maximum: int,
        cwd: Path | None = None, env: dict[str, str] | None = None,
        cancel: Callable[[], bool] = lambda: False, input_data: bytes = b"") -> Result:
    """Bound input/output/exit by one deadline, then stop and reap locally.

    Raw polling pipes avoid buffered read locks, including when detached
    descendants retain pipe handles. POSIX signals the original process group;
    containment of escaped descendants requires the configured OCI boundary.
    Windows uses the kill-on-close Job and cancellable synchronous writes.
    """
    started = time.monotonic()
    if cancel():
        return Result(None, "", "", "cancelled", 0.0)
    if timeout <= 0:
        return Result(None, "", "", "timeout", 0.0)
    deadline = started + timeout
    output = {"stdout": bytearray(), "stderr": bytearray()}
    process = _platform.spawn(list(argv), cwd=cwd, env=environment() if env is None else env,
                             stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                             **_platform.popen_kwargs())
    stop, truncated = threading.Event(), threading.Event()
    output_lock = threading.Lock()
    errors = []
    def pump(stream, key):
        _platform.prepare_pipe(stream)
        try:
            while not stop.is_set():
                try:
                    chunk = _platform.read_pipe(stream, 65536)
                except BlockingIOError:
                    stop.wait(.01)
                    continue
                if not chunk:
                    return
                with output_lock:
                    used = len(output["stdout"]) + len(output["stderr"])
                    output[key].extend(chunk[:max(0, maximum-used)])
                    if used+len(chunk) > maximum:
                        truncated.set()
                        return
        except (OSError, ValueError) as exc:
            if not stop.is_set():
                errors.append(exc)
    readers = [threading.Thread(target=pump, args=(process.stdout, "stdout"), daemon=True, name="mizu-output"),
               threading.Thread(target=pump, args=(process.stderr, "stderr"), daemon=True, name="mizu-output")]
    for reader in readers:
        reader.start()
    input_errors = []
    writer = _input_thread(process.stdin, input_data, stop, input_errors, close=True) if input_data else None
    threads = (*readers, *((writer,) if writer else ()))
    reason = "exited"
    try:
        while True:
            if cancel():
                reason = "cancelled"
                break
            if truncated.is_set():
                reason = "output_limit"
                break
            if input_errors or errors:
                reason = "input_error" if input_errors else "output_error"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            if process.poll() is not None and not any(t.is_alive() for t in threads):
                # A pump may finish between the checks above and this exit check.
                # Preserve its failure instead of racing it into a successful exit.
                if truncated.is_set():
                    reason = "output_limit"
                elif input_errors or errors:
                    reason = "input_error" if input_errors else "output_error"
                break
            stop.wait(min(.01, max(0, deadline-time.monotonic())))
    finally:
        stop.set()
        terminate(process, grace=0)
        try:
            _stop_io(stop, threads)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
    return Result(process.returncode, _text(output["stdout"]), _text(output["stderr"]), reason,
                  round(time.monotonic()-started, 3))
