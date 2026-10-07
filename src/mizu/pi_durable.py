"""Managed pi-durable driver: distinct engine with persisted turns/tasks.

Preserves every Pi authority boundary (sandbox, admission/budget
accounting, cancellation, single-writer, verification/publication) by
reusing the Pi channel protocol; persistence only adds idempotent
recovery around it. Resume binds to valid current authority; persisted
state never revives obsolete grants or bypasses pause/stop. Interrupted
side effects are marked unknown, never blindly repeated.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import pi as _pi
from .bridge import Bridge
from .config import role_policy_text
from .engine_channel import Channel
from .engine_config import (adapter_digest, connected_servers, effective,
                            session_record, save_session)
from .errors import Cancelled, ConfigError, ProtocolError, ModelFailure
from .fs import atomic_write, canonical, digest, mkdir, write_json
from .pi_durable_store import (BACKENDS, RESUME_MODES, complete_run, complete_turn,
                               begin_run, check_grant, grant_digest, load_run,
                               open_store, project_context, record_turn, store_path_for)
from .process import environment
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
ENGINE = "pi-durable"


def durable_options(settings: dict) -> dict:
    """Explicit durable policy with documented defaults (no silent fallback)."""
    options = dict(settings.get("options", {}))
    backend = options.get("durable_backend", "sqlite")
    if backend not in BACKENDS:
        raise ConfigError("Unknown durable backend (supported: sqlite)")
    resume = options.get("durable_resume", "compatible")
    if resume not in RESUME_MODES:
        raise ConfigError("durable_resume must be compatible or fresh")
    retention = options.get("durable_retention_days", None)
    if retention is not None and (type(retention) is not int or not 0 <= retention <= 3650):
        raise ConfigError("durable_retention_days must be an integer in [0, 3650]")
    max_turns = options.get("durable_max_turns", 128)
    if type(max_turns) is not int or not 1 <= max_turns <= 1024:
        raise ConfigError("durable_max_turns must be an integer in [1, 1024]")
    return {"backend": backend, "resume": resume, "retention_days": retention, "max_turns": max_turns}


def grant_for(settings: dict, role, model: dict) -> str:
    policy = role_policy_text(role)
    options_digest = digest(canonical({k: v for k, v in settings.get("options", {}).items()
                                       if k in ("thinkingLevel", "settings", "codemode", "toolSearch",
                                                "excludeTools", "scopedModels")}))
    return grant_digest(policy_text=policy, capabilities=tuple(role.capabilities),
                        model=model, adapter_digest_value=adapter_digest(ENGINE),
                        options_digest=options_digest)


class PiDurableDriver:
    requires_sandbox = True

    def __init__(self, config):
        self.config = config

    def argv(self, role, run_dir: Path, profile: str, goal_digest: str) -> list[str]:
        return [*self.config.command(ENGINE), str(ROOT / "adapters/pi-durable/launcher.mjs"),
                str(run_dir / "pi-durable-effective.json")]

    def execute(self, context, prompt: str, *, profile: str | None = None) -> dict:
        profile = profile or context.role.profile
        settings = effective(self.config, context.role, profile)
        if settings.get("engine") != ENGINE:
            raise ConfigError("pi-durable driver requires a pi-durable profile")
        policy_text = durable_options(settings)
        # Capability/extension incompatibility is explicit: durable refuses
        # native host tools that bypass the bridge (same floor as Pi).
        for tool in context.role.engine_tools:
            if tool in ("bash", "powershell", "edit", "write", "read", "Bash", "Read", "Edit",
                        "Write", "NotebookEdit", "Computer", "Glob", "Grep"):
                raise ConfigError("Native host tools bypass the OCI bridge; grant mizu operations instead")
        path, saved = session_record(context, profile, settings)
        session_key = path.parent.name
        project_name = getattr(getattr(context, "project", None), "name", "unknown")
        model = self.config.model(profile)
        grant = grant_for(settings, context.role, model)
        store_path = store_path_for(self.config.data, project_name, context.role.name, session_key)
        conn = open_store(store_path)
        try:
            run_key = context.run_dir.name
            if policy_text["resume"] == "fresh" and load_run(conn, run_key) is not None:
                raise ConfigError("Durable resume mode is fresh; retry with a new run")
            started_record = begin_run(conn, run_key=run_key, session_key=session_key,
                                       project=project_name, role=context.role.name, grant=grant)
            if started_record.get("resumed") and started_record.get("status") == "completed":
                # Duplicate submission: return the saved result without new admission.
                result = dict(started_record["result"] or {})
                result["durable"] = {"resumed": True, "duplicate": True,
                                     "projection": project_context(conn, run_key)}
                return result
            if started_record.get("resumed"):
                check_grant(conn, run_key, grant)
            if context.cancelled():
                complete_run(conn, run_key, {"cancelled": True}, status="cancelled")
                raise Cancelled("Run cancelled before durable dispatch")
            record_turn(conn, run_key, 1, "prompt", {"bytes": len(prompt.encode())})
            if len(prompt.encode()) == 0:
                raise ConfigError("Durable prompt must be nonempty")
            outcome = self._run_channel(context, settings, saved, path, model, prompt, profile, conn, run_key)
            complete_turn(conn, run_key, 1, {"engine": ENGINE}, state="completed")
            complete_run(conn, run_key, {"engine": outcome.get("engine"), "requests": outcome.get("requests")}, status="completed")
            outcome["durable"] = {"resumed": bool(started_record.get("resumed")), "duplicate": False,
                                  "store": str(store_path), "projection": project_context(conn, run_key)}
            return outcome
        except (Cancelled, ProtocolError, ModelFailure, ConfigError):
            try:
                record = load_run(conn, run_key)
                if record is not None and record["status"] == "active":
                    # Uncertain side-effect completion: mark unknown, never assume.
                    try:
                        complete_turn(conn, run_key, 1, {"uncertain": True}, state="unknown")
                    except Exception:
                        pass
                    try:
                        complete_run(conn, run_key, {"interrupted": True}, status="interrupted")
                    except Exception:
                        pass
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run_channel(self, context, settings, saved, path, model, prompt, profile, conn, run_key) -> dict:
        # Shared Pi protocol: handshake, exact-model pin, settlement seal.
        # Durable turns persist the prompt; per-message persistence stays in
        # the store's turn record (started/unknown/completed) while the live
        # event stream remains the channel's bounded evidence.
        policy = role_policy_text(context.role)
        atomic_write(context.run_dir / "system.md", policy.encode())
        cwd = context.run_dir / "controller"
        mkdir(cwd)
        agent_dir = self.config.agent_dir(ENGINE)
        mkdir(agent_dir)
        write_json(context.run_dir / "pi-durable-effective.json",
                   {**settings, "mcp_servers": connected_servers(context, settings),
                    "agentDir": str(agent_dir), "cwd": str(cwd), "sessionDir": str(path.parent),
                    "resume": saved["id"] if saved else None, "systemPrompt": policy,
                    "durable": {"run_key": run_key, "store": str(store_path_for(
                        self.config.data, getattr(getattr(context, "project", None), "name", "unknown"),
                        context.role.name, path.parent.name))}})
        started = time.monotonic()
        session_file = None
        observed = []
        context.model_evidence = {"profile": profile, **model, "engine": ENGINE, "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        import contextlib
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.run_seconds,
                    on_close=context.cancel_operations) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            env.update(_pi.credentials(self.config.file.parent / "credentials.env"))
            channel = Channel(context, self.argv(context.role, context.run_dir, profile, context.goal_digest), env, cwd, "pi-durable")
            try:
                channel.send({"id": "state", "type": "get_state"})
                while True:
                    event = channel.receive(until=min(context.deadline, started+30))
                    if event.get("id") != "state":
                        continue
                    if not event.get("success") or not context.hello.is_set():
                        raise ProtocolError("Pi-durable managed SDK handshake failed")
                    actual = event.get("data", {}).get("model") or {}
                    if actual.get("id") != model["model"] or actual.get("provider") != model["provider"]:
                        raise ProtocolError("Pi-durable selected a different model/provider")
                    session_file = event.get("data", {}).get("sessionFile")
                    break
                channel.send({"id": "work", "type": "prompt", "message": prompt})
                while True:
                    event = channel.receive()
                    kind = event.get("type")
                    if kind == "response" and event.get("id") == "work":
                        if not event.get("success") or event.get("data", {}).get("disposition") == "handled":
                            raise ProtocolError("Pi-durable prompt did not start an agent run")
                    elif kind == "message_end":
                        message = event.get("message", {})
                        if message.get("role") == "assistant":
                            if message.get("stopReason") == "error":
                                raise ModelFailure("pi-durable", kind="error", message=message.get("errorMessage", "error"))
                            if message.get("stopReason") == "aborted":
                                raise ProtocolError("Pi-durable request aborted")
                            observed.append({key: message.get(key) for key in ("provider", "api", "model", "thinkingLevel")})
                    elif kind == "agent_settled":
                        if context.request_count == 0:
                            raise ProtocolError("No runtime request admission observed")
                        if context.finished is None:
                            raise ProtocolError("Pi-durable settled without mizu_finish")
                        if settings["session"] == "persistent" and not context.ephemeral:
                            save_session(path, session_file, {"tokens": _pi._cumulative_tokens(saved, context)})
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
