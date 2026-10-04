"""Managed Pi SDK driver, using the shared bounded duplex channel."""
from __future__ import annotations
import contextlib
import re
import time
from pathlib import Path

from . import platform as _platform
from .bridge import Bridge
from .config import Config, Role, role_policy_text
from .engine_channel import Channel
from .engine_config import effective, connected_servers, session_record, save_session
from .errors import ConfigError, ProtocolError, ModelFailure
from .fs import atomic_write, mkdir, write_json
from .process import environment
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]


class PiDriver:
    requires_sandbox = True

    def __init__(self, config: Config):
        self.config = config

    def argv(self, role: Role, run_dir: Path, profile: str, goal_digest: str,
             *, policy_text: str | None = None) -> list[str]:
        return [*self.config.command("pi"), str(ROOT / "adapters/pi/launcher.mjs"), str(run_dir / "pi-effective.json")]

    def execute(self, context, prompt: str, *, profile: str | None = None) -> dict:
        profile = profile or context.role.profile
        settings = effective(self.config, context.role, profile)
        path, saved = session_record(context, profile, settings)
        model = self.config.model(profile)
        policy = role_policy_text(context.role)
        atomic_write(context.run_dir / "system.md", policy.encode())
        cwd = context.run_dir / "controller"
        mkdir(cwd)
        agent_dir = self.config.agent_dir("pi")
        mkdir(agent_dir)
        write_json(context.run_dir / "pi-effective.json", {**settings, "mcp_servers": connected_servers(context, settings),
                   "agentDir": str(agent_dir), "cwd": str(cwd), "sessionDir": str(path.parent),
                   "resume": saved["id"] if saved else None, "systemPrompt": policy})
        started = time.monotonic()
        session_file = None
        observed = []
        context.model_evidence = {"profile": profile, **model, "engine": "pi", "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.run_seconds,
                    on_close=context.cancel_operations) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            env.update(credentials(self.config.file.parent / "credentials.env"))
            channel = Channel(context, self.argv(context.role, context.run_dir, profile, context.goal_digest), env, cwd, "pi")
            try:
                # SDK emits its startup events after binding all extension handlers.
                channel.send({"id": "state", "type": "get_state"})
                while True:
                    event = channel.receive(until=min(context.deadline, started+30))
                    if event.get("id") != "state":
                        continue
                    if not event.get("success") or not context.hello.is_set():
                        raise ProtocolError("Pi managed SDK handshake failed")
                    actual = event.get("data", {}).get("model") or {}
                    if actual.get("id") != model["model"] or actual.get("provider") != model["provider"]:
                        raise ProtocolError("Pi selected a different model/provider")
                    session_file = event.get("data", {}).get("sessionFile")
                    break
                channel.send({"id": "work", "type": "prompt", "message": prompt})
                while True:
                    event = channel.receive()
                    kind = event.get("type")
                    if kind == "response" and event.get("id") == "work":
                        if not event.get("success") or event.get("data", {}).get("disposition") == "handled":
                            raise ProtocolError("Pi prompt did not start an agent run")
                    elif kind == "message_end":
                        message = event.get("message", {})
                        if message.get("role") == "assistant":
                            if message.get("stopReason") == "error":
                                raise ModelFailure("pi", kind="error", message=message.get("errorMessage", "error"))
                            if message.get("stopReason") == "aborted":
                                raise ProtocolError("Pi request aborted")
                            observed.append({key: message.get(key) for key in ("provider", "api", "model", "thinkingLevel")})
                    elif kind == "agent_settled":
                        if context.request_count == 0:
                            raise ProtocolError("No runtime request admission observed")
                        if context.finished is None:
                            raise ProtocolError("Pi settled without mizu_finish")
                        if settings["session"] == "persistent" and not context.ephemeral:
                            save_session(path, session_file, {})
                        break
            finally:
                context.model_evidence.update(requests=context.request_count, usage=list(context.runtime_usage.values()),
                                              usage_known=bool(context.runtime_usage), observed_models=observed+list(context.runtime_models.values()),
                                              usage_observations=[{"usage": value, "provider": context.runtime_models.get(seq, {}).get("provider"),
                                                                   "model": context.runtime_models.get(seq, {}).get("model")}
                                                                  for seq, value in context.runtime_usage.items()])
                with contextlib.suppress(Exception):
                    channel.send({"type": "abort"})
                channel.close()
        return {**context.model_evidence, "seconds": round(time.monotonic()-started, 3)}


def credentials(path: Path) -> dict[str, str]:
    """Literal KEY=value, optional matching quotes. Never source/eval a shell file."""
    if not path.exists():
        return {}
    if not _platform.credentials_owner_ok(path):
        raise ConfigError("credentials.env must be owned by the current user, mode 0600, and not a symlink")
    result = {}
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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
