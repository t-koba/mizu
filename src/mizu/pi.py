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
from .engine_config import effective, connected_servers, session_record, save_session, session_token_total, MAX_SESSION_TOKENS
from .errors import RequestYieldExceeded
from .errors import ConfigError, DeadlineWrapUp, ProtocolError, ModelFailure
from .fs import atomic_write, mkdir, write_json
from .process import environment
from .protocol import tool_definitions
from .startup import RUNTIME_ENDPOINT, record_startup, startup_bound

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
        session_file = None
        observed = []
        started = time.monotonic()
        context.model_evidence = {"profile": profile, **model, "engine": "pi", "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.model_request_seconds,
                    on_close=context.cancel_operations) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            env.update(credentials(self.config.file.parent / "credentials.env"))
            bound = startup_bound(self.config, 'pi')
            spawned = time.monotonic()
            channel = Channel(context, self.argv(context.role, context.run_dir, profile, context.goal_digest), env, cwd, "pi")
            try:
                # The runtime handshake uses this host's per-engine bound;
                # the timer starts before child launch and ends at its first
                # state response. Probe process-exit samples use a different
                # endpoint, and the shared record keeps the larger interval.
                # SDK emits its startup events after binding all extension handlers.
                channel.send({"id": "state", "type": "get_state"})
                while True:
                    event = channel.receive(until=spawned+bound, bound=bound)
                    if event.get("id") != "state":
                        continue
                    # Runtime startup is measured through this first state
                    # response, including refusals. Probe paths measure
                    # through process exit; the shared record keeps the
                    # larger interval.
                    record_startup(self.config, 'pi', time.monotonic() - spawned, RUNTIME_ENDPOINT)
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
                                if getattr(context, "request_bound_exhausted", False):
                                    raise RequestYieldExceeded("Request wrap-up allowance exhausted")
                                if (getattr(context, "request_admission_refused", False)
                                        and context.bound_hit == "requests" and not context.wrap_up_attempted
                                        and not context.request_deadlines):
                                    raise DeadlineWrapUp(context._bound_message())
                                # A drain that refused the request is the
                                # drain bound, not a provider failure: the unit
                                # gets its wrap-up and records its finish, like
                                # the durable engine. A request deadline ends
                                # the request as "aborted" and takes the other
                                # branch, so it is never reported as a drain.
                                if (getattr(context, "drain_admission_refused", False)
                                        and context.bound_hit == "drain" and not context.wrap_up_attempted):
                                    if getattr(context, "delegated_child", False):
                                        # A delegated child has no session to
                                        # wrap up: it stops here and reports
                                        # the interruption to its caller.
                                        context.drain_refusal(context._bound_message())
                                    raise DeadlineWrapUp(context._bound_message())
                                raise ModelFailure("pi", kind="error", message=message.get("errorMessage", "error"))
                            if message.get("stopReason") == "aborted":
                                raise ProtocolError("Pi request aborted")
                            context.request_failure_pending = False
                            observed.append({key: message.get(key) for key in ("provider", "api", "model", "thinkingLevel")})
                    elif kind == "agent_settled":
                        if event.get("aborted") or (isinstance(event.get("data"), dict) and event["data"].get("aborted")):
                            raise ProtocolError("Pi run aborted before settlement")
                        context.request_failure_pending = False
                        if context.request_count == 0:
                            raise ProtocolError("No runtime request admission observed")
                        # The session record is persistent only when settings
                        # select that policy; otherwise session_record() places
                        # it under this run, where a missing-finish continuation
                        # can resume within its bounded allowance.
                        save_session(path, session_file, {"tokens": _cumulative_tokens(saved, context)})
                        # A normal settled response without a seal is returned
                        # to Engine._execute for the bounded missing-finish policy.
                        break
            except DeadlineWrapUp:
                # The Pi runtime admits individual provider requests behind a
                # bridge, so the host can cross the drain boundary from either
                # that admission or transport polling. Preserve the active
                # conversation before Engine starts its reduced-grant turn.
                if (context.bound_hit in ("drain", "requests") and not context.wrap_up_attempted
                        and isinstance(session_file, str) and session_file):
                    save_session(path, session_file, {"tokens": _cumulative_tokens(saved, context)})
                raise
            finally:
                context.request_failure_pending = False
                context.model_evidence.update(requests=context.request_count, usage=list(context.runtime_usage.values()),
                                              usage_known=bool(context.runtime_usage), observed_models=observed+list(context.runtime_models.values()),
                                              usage_observations=[{"usage": value, "provider": context.runtime_models.get(seq, {}).get("provider"),
                                                                   "model": context.runtime_models.get(seq, {}).get("model")}
                                                                  for seq, value in context.runtime_usage.items()])
                with contextlib.suppress(Exception):
                    channel.send({"type": "abort"})
                channel.close()
        if getattr(context, 'recovery_probe', False):
            context.model_evidence['settled'] = True
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


def _cumulative_tokens(saved, context):
    """Cumulative Pi session tokens: saved total plus this run's usage.

    Schema: reads the bounded saved record plus ``context.runtime_usage``
    normalized with the Pi key map. Bounds: capped at 2**63-1. Trust:
    local evidence only. Failure: unknown shapes add nothing, so rotation
    stays age-driven when the provider reports no recognized counters.
    """
    from .usage import normalize
    run_total = 0
    try:
        items = list(context.runtime_usage.values())
    except AttributeError:
        items = []
    for value in items[:4096]:
        try:
            part = normalize(value if isinstance(value, dict) else {}, "pi")
        except Exception:
            continue
        if part.get("unknown_shape"):
            continue
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "other_tokens"):
            piece = part.get(field, 0)
            if type(piece) is int and piece > 0:
                run_total = min(run_total + piece, MAX_SESSION_TOKENS)
    try:
        previous = session_token_total(saved, "pi")
    except Exception:
        previous = 0
    return min(previous + run_total, MAX_SESSION_TOKENS)
