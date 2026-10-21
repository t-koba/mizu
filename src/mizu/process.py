"""Bounded subprocesses with portable cancellation and no host shell."""
from __future__ import annotations

import contextlib
import dataclasses
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import platform as _platform

#: Configurable per-adapter default for model-visible failure diagnostics.
ADAPTER_DIAGNOSTIC_BYTES_DEFAULT = 2048
#: Fixed mechanism ceiling for a single model-visible adapter diagnostic.
ADAPTER_DIAGNOSTIC_BYTES_MAX = 8192
#: Ceiling for the whole I/O stop: one bound for every stopped thread.
#: Fixed in code, never a knob and never the caller's remaining deadline:
#: that deadline is exhausted exactly on the timeout path, where the stop
#: still has to hold.
STOP_IO_TIMEOUT_SECONDS = 5.0


@dataclasses.dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    reason: str
    seconds: float
    #: The threads that outlived the stop bound, named; empty when the
    #: stop was clean. It is the last field so existing positional
    #: construction keeps working.
    survivors: tuple[str, ...] = ()


def adapter_failure_diagnostic(result: Result, *, source: str, maximum_bytes: int) -> str:
    """Return a bounded, labelled stderr tail only for a normal nonzero exit.

    The excerpt is model-visible and external-untrusted. Other failure reasons
    keep their existing generic handling; a disabled/invalid byte allowance
    returns no excerpt. The hard ceiling protects callers even before config
    validation.
    """
    if (result.reason != "exited" or type(result.exit_code) is not int or result.exit_code == 0
            or type(maximum_bytes) is not int or maximum_bytes <= 0):
        return ""
    limit = min(maximum_bytes, ADAPTER_DIAGNOSTIC_BYTES_MAX)
    raw = result.stderr.encode("utf-8")
    if not raw:
        return ""
    truncated = len(raw) > limit
    excerpt = raw[-limit:].decode("utf-8", "ignore").rstrip()
    if not excerpt.strip():
        return ""
    tail = f"; tail capped at {limit} bytes" if truncated else ""
    return f"\nAdapter stderr (external-untrusted; {source}{tail}):\n{excerpt}"


def stop_note(result: Result) -> str:
    """Return a bounded note naming the threads that outlived the stop bound.

    It is empty for a run whose stop was clean, so a caller can use it as
    the detail of a failure without changing the detail it reports for
    any other reason.
    """
    if not result.survivors:
        return ""
    named = ", ".join(result.survivors[:8])
    return f"pipe threads outlived the stop bound: {named}"


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


class PipeStopFailure(OSError):
    """A pipe thread outlived the stop bound: a host fault, reported.

    It carries the operation it interrupted and the threads that outlived
    the bound. When an exception is already propagating, the fault is
    attached to it as a note instead of replacing it, so the reason the
    operation failed survives the host fault.
    """

    def __init__(self, message: str, *, reason: str, survivors: tuple[str, ...]):
        super().__init__(message)
        self.reason = reason
        self.survivors = survivors


def _stop_io(stop: threading.Event, threads, *, ceiling: float | None = None) -> list[str]:
    """Stop owned I/O threads and name the ones that survive the bound.

    The wait is one ceiling for the whole stop, not a bound per thread and
    not the caller's remaining deadline: the deadline is exhausted exactly
    on the timeout path, where the stop still has to hold, and a per-thread
    wait would stack into a delay of the whole operation. It is a bound, not
    a delay: it ends as soon as every thread has stopped. Polling reads and
    nonblocking writes exit promptly once the stop is set, but a loaded host
    may schedule them late.
    """
    limit = STOP_IO_TIMEOUT_SECONDS if ceiling is None else ceiling
    stop.set()
    for thread in threads:
        _platform.cancel_pipe_io(thread)
    # Polling reads/nonblocking writes exit promptly; Windows synchronous
    # writes are cancelled before closing descriptors. No buffered pipe locks.
    deadline = time.monotonic() + limit
    survivors = []
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # A thread that had already finished is not a survivor: only one
        # that is still running when the shared bound expires is named.
        if thread.is_alive():
            survivors.append(thread.name)
    return survivors


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
        survivors = _stop_io(stop, (thread,))
        if survivors:
            fault = PipeStopFailure(
                "Pipe operation did not stop after cancellation (input send): "
                + ", ".join(survivors),
                reason="input send", survivors=tuple(survivors))
            pending = sys.exc_info()[1]
            if pending is None:
                raise fault
            # An exception is already propagating: the stop fault is
            # attached to it, never raised in its place, so the reason the
            # send failed survives the host fault.
            pending.add_note(str(fault))


def run(argv: Sequence[str], *, timeout: float | Callable[[], float | None], maximum: int,
        cwd: Path | None = None, env: dict[str, str] | None = None,
        cancel: Callable[[], bool] = lambda: False, input_data: bytes = b"") -> Result:
    """Bound input/output/exit by one deadline, then stop and reap locally.

    timeout is seconds, or a host callable returning an absolute monotonic
    deadline. The callable may return None while an admitted operation/join
    runs under its own bounds; ordinary command callers stay numeric.

    Raw polling pipes avoid buffered read locks, including when detached
    descendants retain pipe handles. POSIX signals the original process group;
    containment of escaped descendants requires the configured OCI boundary.
    Windows uses the kill-on-close Job and cancellable synchronous writes.

    The decided outcome is returned, never raised away. A run reports
    "exited" only when the child has exited and every pipe thread has
    already stopped, so its output is complete and a caller may read it
    as a finished run. A decided failure keeps its own reason and records
    the threads that outlived the stop bound in Result.survivors, so a
    host fault is visible beside the outcome it interrupted. Only a run
    that decided no outcome at all (a launch that failed) raises.
    """
    started = time.monotonic()
    if cancel():
        return Result(None, "", "", "cancelled", 0.0)
    if not callable(timeout) and timeout <= 0:
        return Result(None, "", "", "timeout", 0.0)
    deadline_at = timeout if callable(timeout) else lambda: started + timeout
    output = {"stdout": bytearray(), "stderr": bytearray()}
    process = _platform.spawn(list(argv), cwd=cwd, env=environment() if env is None else env,
                             stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                             **_platform.popen_kwargs())
    stop, truncated = threading.Event(), threading.Event()
    changed = threading.Event()
    output_lock = threading.Lock()
    errors = []
    def pump(stream, key):
        _platform.prepare_pipe(stream)
        try:
            while not stop.is_set():
                if not _platform.wait_pipe(stream, stop):
                    continue
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
        finally:
            changed.set()
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
            deadline = deadline_at()
            if deadline is not None and time.monotonic() >= deadline:
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
            # I/O completion wakes immediately; idle authority/deadline checks
            # remain bounded at 50 ms without 100 control-file reads per second.
            changed.wait(.05 if deadline is None else min(.05, max(0, deadline-time.monotonic())))
            changed.clear()
    finally:
        stop.set()
        terminate(process, grace=0)
        try:
            survivors = _stop_io(stop, threads)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
    # The reason the run decided is kept, and the threads that outlived the
    # stop bound are recorded beside it. A run that reports "exited" reached
    # that decision only after every pipe thread had stopped, so the stop
    # finds no survivor on that path and the output is complete.
    return Result(process.returncode, _text(output["stdout"]), _text(output["stderr"]), reason,
                  round(time.monotonic()-started, 3), tuple(survivors))
