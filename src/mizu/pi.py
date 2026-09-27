"""Pi JSONL process adapter. Version-specific details do not enter the runtime."""
from __future__ import annotations

import contextlib
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import platform as _platform
from .bridge import Bridge
from .config import Config, Role
from .drivers import ERROR_TAIL_CHARS
from .errors import Cancelled, ConfigError, LimitExceeded, ProtocolError
from .fs import atomic_write, canonical, digest, mkdir, write_json
from .process import environment, terminate
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
MAX_RECORD = 4 * 1024 * 1024


class PiDriver:
    #: Real drivers spawn host-adjacent processes, so Engine enforces rootless
    #: sandbox prerequisites before command-capable runs. Pure test doubles
    #: set this False; unknown drivers default to enforced (see Engine.run).
    requires_sandbox = True

    def __init__(self, config: Config):
        self.config = config

    def argv(self, role: Role, run_dir: Path, profile: str, goal_digest: str,
             *, policy_text: str | None = None) -> list[str]:
        model = self.config.model(profile)
        # thinking levels are validated in config.THINKING_LEVELS for this Pi interface.
        command = [*self.config.pi_command, "--mode", "rpc", "--provider", model["provider"],
                   "--model", model["model"], "--thinking", model["thinking"] or "off",
                   "--no-builtin-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
                   "--no-themes", "--no-context-files", "--no-approve", "--offline",
                   "--extension", str(ROOT / "adapters" / "pi" / "extension.ts"),
                   "--system-prompt", str(run_dir / "system.md"),
                   "--tools", ",".join("mizu_" + name for name in role.capabilities)]
        if role.persistent:
            session_dir = run_dir.parents[1] / "sessions" / role.name
            mkdir(session_dir)
            policy_text = policy_text if policy_text is not None else role.policy.read_text()
            key = digest(canonical({"model": model, "goal": goal_digest,
                                    "policy": policy_text, "caps": role.capabilities}))[:24]
            command.extend(["--session-dir", str(session_dir), "--session-id", role.name + "-" + key])
        else:
            command.extend(["--session-dir", str(run_dir / "session"), "--session-id", run_dir.name])
        return command

    def execute(self, context, prompt: str, *, profile: str | None = None) -> dict:
        role, run_dir = context.role, context.run_dir
        profile = profile or role.profile
        model = self.config.model(profile)
        policy_text = role.policy.read_text()
        atomic_write(run_dir / "system.md", policy_text.encode())
        cwd = run_dir / "controller"
        mkdir(cwd)
        mkdir(self.config.pi_dir)
        # The Pi directory is operator-owned and never mounted into command containers.
        settings = self.config.pi_dir / "settings.json"
        if not settings.exists():
            write_json(settings, {"retry": {"enabled": False}, "compaction": {"enabled": True}})
        records: queue.Queue = queue.Queue(maxsize=128)
        stderr = bytearray()
        stop_readers = threading.Event()
        errors: list[str] = []
        usage = []
        started = time.monotonic()
        with Bridge(context.handle, tool_definitions(role.capabilities), timeout=self.config.limits.run_seconds) as bridge:
            env = environment(extra={"PI_CODING_AGENT_DIR": str(self.config.pi_dir), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
                                     "MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            # Provider-specific environment is explicitly configured in a private file.
            env.update(credentials(self.config.file.parent / "credentials.env"))
            process = subprocess.Popen(self.argv(role, run_dir, profile, context.goal_digest,
                                               policy_text=policy_text),
                                       cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, **_platform.popen_kwargs())

            def put(value):
                while not stop_readers.is_set():
                    try:
                        records.put(value, timeout=0.1)
                        return
                    except queue.Full:
                        continue

            def read_stdout():
                try:
                    while not stop_readers.is_set():
                        raw = process.stdout.readline(MAX_RECORD + 1)
                        if not raw:
                            break
                        if len(raw) > MAX_RECORD or not raw.endswith(b"\n"):
                            put(ProtocolError("Oversized or incomplete Pi JSONL record"))
                            break
                        try:
                            put(json.loads(raw))
                        except (ValueError, UnicodeError):
                            put(ProtocolError("Pi stdout was not valid JSONL"))
                            break
                finally:
                    put(None)

            def read_stderr():
                while not stop_readers.is_set():
                    raw = process.stderr.read(4096)
                    if not raw:
                        break
                    room = max(0, self.config.limits.output_bytes - len(stderr))
                    stderr.extend(raw[:room])

            readers = [threading.Thread(target=read_stdout, daemon=True),
                       threading.Thread(target=read_stderr, daemon=True)]
            for reader in readers:
                reader.start()

            def send(value):
                process.stdin.write(canonical(value))
                process.stdin.flush()

            def receive():
                if context.cancelled():
                    raise Cancelled("Run cancelled")
                if time.monotonic() - started > self.config.limits.run_seconds:
                    raise LimitExceeded("Pi run deadline exceeded")
                try:
                    item = records.get(timeout=0.1)
                except queue.Empty:
                    return {}
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    cause = context.admission_error or "Pi exited before agent_settled"
                    raise ProtocolError(cause)
                if not isinstance(item, dict):
                    raise ProtocolError("Pi emitted a non-object record")
                return item

            try:
                # Never send a paid prompt before the trusted extension has contacted us.
                while not context.hello.is_set():
                    receive()
                    if time.monotonic() - started > 30:
                        raise ProtocolError("Pi extension did not complete the safety handshake")
                send({"id": "state", "type": "get_state"})
                while True:
                    event = receive()
                    if event.get("id") != "state":
                        continue
                    if not event.get("success"):
                        raise ProtocolError(event.get("error", "Pi get_state failed"))
                    actual = event.get("data", {}).get("model") or {}
                    if actual.get("id") != model["model"] or actual.get("provider") != model["provider"]:
                        raise ProtocolError("Pi selected a model different from the configured exact ID")
                    break
                send({"id": "work", "type": "prompt", "message": prompt})
                settled = False
                while not settled:
                    event = receive()
                    kind = event.get("type")
                    if kind == "response" and event.get("id") == "work":
                        if not event.get("success"):
                            raise ProtocolError(event.get("error", "Pi rejected prompt"))
                        if event.get("data", {}).get("disposition") == "handled":
                            raise ProtocolError("Prompt was handled without starting an agent run")
                    elif kind == "message_end":
                        message = event.get("message", {})
                        if message.get("role") == "assistant":
                            if message.get("stopReason") in ("error", "aborted"):
                                errors.append(str(message.get("errorMessage", message["stopReason"])))
                            if message.get("usage"):
                                usage.append(message["usage"])
                    elif kind == "agent_settled":
                        settled = True
                    # agent_end is deliberately NOT a completion condition.
                if errors:
                    raise ProtocolError("; ".join(errors)[-ERROR_TAIL_CHARS:])
                if context.request_count == 0:
                    raise ProtocolError("No provider-request admission was observed")
                if context.finished is None:
                    raise ProtocolError("Agent settled without calling mizu_finish")
                return {"profile": profile, "provider": model["provider"], "model": model["model"],
                        "engine": "pi", "requests": context.request_count, "usage": usage,
                        "seconds": round(time.monotonic() - started, 3)}
            finally:
                with contextlib.suppress(OSError, ValueError):
                    process.stdin.close()
                terminate(process, grace=2)
                stop_readers.set()
                for reader in readers:
                    reader.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    stream.close()
                atomic_write(run_dir / "diagnostics.txt", bytes(stderr))


def credentials(path: Path) -> dict[str, str]:
    """Literal KEY=value, optional matching quotes. Never source/eval a shell file."""
    if not path.exists():
        return {}
    if not _platform.credentials_owner_ok(path):
        raise ConfigError("credentials.env must be owned by the current user, mode 0600, and not a symlink")
    result = {}
    for index, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid credentials.env line {index}")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ConfigError(f"Invalid environment variable on line {index}")
        if name in ("PATH", "HOME", "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "MIZU_BRIDGE_CONFIG") or name.startswith("PI_"):
            raise ConfigError(f"Reserved environment variable: {name}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[name] = value
    return result
