"""Unit-owned OCI shell, with cooperative (not authenticated) completion frames.

Only direct exec uses this transport. Sandbox.execute remains one-shot. Pipes
are continuously drained into bounded buffers, including between calls; an idle
background producer can exhaust the bound and invalidate the next call. No TTY,
no replay, no credentials or runtime socket are passed into the container.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
import uuid

from . import platform
from .errors import Cancelled, Denied, LimitExceeded
from .fs import SCRIPT_MAX, now, write_json
from .process import _stop_io, send_bounded, terminate, run, stop_note
from .sandbox import assert_no_checkpoint, runtime_env


class ExecSession:
    def __init__(self, sandbox, workspace, *, writable):
        self.sandbox, self.workspace, self.writable = sandbox, workspace, writable
        self.name = f"mizu-{sandbox.label}-{uuid.uuid4().hex[:12]}"
        self.process = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.overflow = threading.Event()
        self.pending = {"stdout": bytearray(), "stderr": bytearray()}
        self.pending_lock = threading.Lock()
        self.changed = threading.Event()
        self.threads = []
        self.closed = False
        self.launched = False
        self.cleanup_error = None

    def _start(self):
        sb = self.sandbox
        env = runtime_env(sb.config)
        assert_no_checkpoint(sb.config, env)
        argv = sb.argv(self.name, self.workspace, "", writable=self.writable)
        # Same configured shell and arguments as one-shot, but reading commands
        # from stdin. -i here is the OCI stdin flag, NOT an interactive shell.
        argv.remove("--rm")  # Explicit unit cleanup owns even an exited shell.
        argv.insert(len(sb.base()) + 1, "-i")
        argv[-2:] = ["-s"]
        self.launched = True
        self.process = platform.spawn(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, env=env,
                                        **platform.popen_kwargs())
        for key in ("stdout", "stderr"):
            stream = getattr(self.process, key)
            platform.prepare_pipe(stream)
            def pump(stream=stream, key=key):
                try:
                    while not self.stop.is_set():
                        if not platform.wait_pipe(stream, self.stop):
                            continue
                        try:
                            chunk = platform.read_pipe(stream, 4096)
                        except BlockingIOError:
                            self.stop.wait(.01)
                            continue
                        if not chunk:
                            break
                        with self.pending_lock:
                            remaining = sb.config.limits.output_bytes + 128 - sum(map(len, self.pending.values()))
                            self.pending[key].extend(chunk[:max(0, remaining)])
                            if len(chunk) > remaining:
                                self.overflow.set()
                        self.changed.set()
                        if self.overflow.is_set():
                            break
                except (OSError, ValueError):
                    if not self.stop.is_set():
                        self.overflow.set()
            thread = threading.Thread(target=pump, daemon=True, name="mizu-exec-" + key)
            self.threads.append(thread)
            thread.start()

    def _remove(self):
        # The launcher is already stopped. Probe then remove by exact name for
        # Docker as well as Podman; every incomplete observation fails closed.
        sb = self.sandbox
        for index, argv in enumerate(([*sb.base(), "ps", "--all", "--filter", "name=" + self.name,
                      "--format", "{{.ID}}"], [*sb.base(), "rm", "--force", self.name])):
            result = run(argv, timeout=15, maximum=8192, env=runtime_env(sb.config))
            if result.reason != "exited" or result.exit_code != 0 or result.survivors:
                raise Denied(stop_note(result) or result.stderr or "container cleanup " + result.reason)
            if index == 0 and not result.stdout.strip():
                return

    def close(self):
        """Idempotent bounded removal; a failed cleanup permanently poisons owner."""
        with self.lock:
            if self.cleanup_error:
                raise Denied(self.cleanup_error)
            if self.closed:
                return
            self.closed = True
            errors = []
            # Stop delivery first: do not race a half-started CLI into creating
            # its container after we checked for absence. Still attempt removal
            # if launcher termination itself fails.
            if self.process is not None:
                try:
                    terminate(self.process, grace=0)
                except Exception as exc:
                    errors.append(str(exc))
            if self.launched:
                try:
                    self._remove()
                except Exception as exc:
                    errors.append(str(exc))
            if self.process is not None:
                try:
                    survivors = _stop_io(self.stop, self.threads)
                    if survivors:
                        errors.append("pipe threads outlived stop bound: " + ", ".join(survivors))
                except Exception as exc:
                    errors.append(str(exc))
                finally:
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        try:
                            stream.close()
                        except OSError as exc:
                            errors.append(str(exc))
            if errors:
                self.cleanup_error = "Persistent exec cleanup failed; operator recovery required: " + "; ".join(errors)
            write_json(self.sandbox.run_dir / "exec-sessions" / (self.name + ".json"),
                       {"container": self.name, "closed_at": now(), "cleanup_error": self.cleanup_error})
            if self.cleanup_error:
                raise Denied(self.cleanup_error)

    def execute(self, script):
        if not isinstance(script, str) or not script.strip() or len(script.encode()) > SCRIPT_MAX:
            raise Denied("Command must be nonempty and at most 64 KiB")
        with self.lock:
            if self.closed:
                raise Denied(self.cleanup_error or "Persistent exec session is closed")
            sb = self.sandbox
            started = time.monotonic()
            deadline = started + sb.config.limits.command_seconds
            token = uuid.uuid4().hex
            prefix = b"\x1eMIZU:" + token.encode() + b":"
            pattern = re.compile(re.escape(prefix) + rb"([0-9]{1,3})\x1f")
            # eval is in this shell, not a subshell: cwd, functions, exported
            # variables and background processes survive. Commands can tamper
            # with this framing; it is deliberately NOT acceptance evidence.
            # Match one-shot Sandbox.execute: user commands receive EOF on
            # stdin, never the subsequent protocol frames from the shell pipe.
            frame = ("eval " + shlex.quote(script) + " </dev/null\n__mizu_status=$?\n"
                     + "printf '\\036MIZU:" + token + ":%s\\037' \"$__mizu_status\"\n"
                     + "printf '\\036MIZU:" + token + ":%s\\037' \"$__mizu_status\" >&2\n")
            operation = uuid.uuid4().hex
            record = {"id": operation, "kind": "command", "persistent_exec": True,
                      "role": sb.role, "created_at": now(), "script": script,
                      "container": self.name, "image": sb.config.sandbox.image,
                      "network": sb.config.sandbox.network, "writable": self.writable,
                      "entrypoint": sb.config.sandbox.entrypoint,
                      "entrypoint_args": list(sb.config.sandbox.entrypoint_args),
                      "mounts": [{"source": m["source"], "target": m["target"]} for m in sb.config.sandbox.mounts],
                      "env_keys": sorted(sb.config.sandbox.env)}
            write_json(sb.run_dir / "commands" / f"{operation}.started.json", record)
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            statuses = {}
            reason, exit_code = "process_error", None
            try:
                if self.process is None:
                    self._start()
                send_bounded(self.process, frame.encode(), deadline=deadline, cancel=sb.cancel)
                while True:
                    if sb.cancel():
                        reason = "cancelled"
                        break
                    if self.overflow.is_set():
                        reason = "output_limit"
                        break
                    if time.monotonic() >= deadline:
                        reason = "timeout"
                        break
                    with self.pending_lock:
                        key = next((k for k, v in self.pending.items() if v), None)
                        if key is not None:
                            chunk = bytes(self.pending[key])
                            self.pending[key].clear()
                        else:
                            self.changed.clear()
                    if key is None:
                        if self.process.poll() is not None:
                            reason = "shell_death"
                            break
                        self.changed.wait(.02)
                        continue
                    buffers[key].extend(chunk)
                    match = pattern.search(buffers[key])
                    if match and key not in statuses:
                        status = int(match[1])
                        if status > 255:
                            reason = "malformed_completion"
                            break
                        statuses[key] = status
                        del buffers[key][match.start():match.end()]
                    elif prefix in buffers[key] and len(buffers[key].split(prefix, 1)[1]) > 4 and not match:
                        reason = "malformed_completion"
                        break
                    used = sum(map(len, buffers.values()))
                    # Permit only the fixed framing overhead until both frames
                    # arrive; enforce the exact output bound on completion.
                    overhead = (2 - len(statuses)) * (len(prefix) + 4)
                    if used > sb.config.limits.output_bytes + overhead:
                        reason = "output_limit"
                        break
                    if len(statuses) == 2:
                        if statuses['stdout'] != statuses['stderr']:
                            reason = "malformed_completion"
                        else:
                            reason, exit_code = "exited", statuses['stdout']
                        break
            except Cancelled:
                reason = "cancelled"
            except LimitExceeded:
                reason = "timeout"
            except Exception as exc:
                record['error'] = str(exc)
                reason = "delivery_uncertain"
            finally:
                record.update(exit_code=exit_code, reason=reason,
                              seconds=round(time.monotonic()-started, 3), finished_at=now())
                maximum = sb.config.limits.output_bytes
                for key, data in buffers.items():
                    record[key] = bytes(data[:maximum]).decode('utf-8', 'replace')
                    maximum = max(0, maximum-len(data))
                if reason != "exited":
                    record.update(state_lost=True, execution_may_be_partial=True,
                                  status_note="Shell state lost; execution may have partially occurred; not replayed")
                    try:
                        self.close()
                    except Exception as exc:
                        record['cleanup_error'] = str(exc)
                try:
                    write_json(sb.run_dir / "commands" / f"{operation}.json", record)
                except Exception as exc:
                    # The command frame may already have run. Never return an
                    # unrecorded success or leave the shell available for a
                    # caller that might retry after this uncertain response.
                    if reason == "exited":
                        try:
                            self.close()
                        except Exception as cleanup_exc:
                            record['cleanup_error'] = str(cleanup_exc)
                    detail = ("Persistent exec evidence could not be recorded; state_lost=true; "
                              "execution_may_be_partial=true; the command may have executed, "
                              "shell state was reset, and it must not be replayed")
                    if record.get('cleanup_error'):
                        detail += "; " + record['cleanup_error']
                    raise Denied(detail) from exc
            if record.get('cleanup_error'):
                note = record.get('status_note', "")
                detail = (note + "; " if note else "") + record['cleanup_error']
                raise Denied(detail)
            return record
