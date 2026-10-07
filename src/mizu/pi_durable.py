"""Managed pi-durable driver: distinct engine with persisted turns/tasks.

Runs one admitted durable submission per call through the pinned
pi-durable Harness over file-backed SQLite (adapters/pi-durable). The
launcher speaks a one-shot protocol: effective file in, a single JSON
result document on stdout, no RPC or event stream. This preserves every
Pi authority boundary (sandbox, admission/budget accounting,
cancellation, single-writer, verification/publication) while durable
commits add idempotent recovery around it. Resume binds to valid
current authority; persisted state never revives obsolete grants or
bypasses pause/stop. Interrupted side effects are marked unknown,
never blindly repeated.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import pi as _pi
from .bridge import Bridge
from .config import role_policy_text
from .drivers import EVENT_RECORD_BYTES
from .engine_config import (MAX_SESSION_TOKENS, adapter_digest, effective,
                             save_session, session_record, session_token_total)
from .errors import Cancelled, ConfigError, LimitExceeded, ProtocolError, ModelFailure
from .fs import atomic_write, canonical, digest, mkdir, write_json
from .pi_durable_store import (BACKENDS, RESUME_MODES, complete_run, complete_turn,
                               begin_run, check_grant, grant_digest, load_run,
                               open_store, project_context, prune,
                               record_turn, retention_candidates, store_path_for)
from .process import environment, run
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
ENGINE = "pi-durable"
#: Single bounded result document from the one-shot launcher.
RESULT_BYTES = 1024 * 1024


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
                                       if k in ("thinkingLevel", "durable_backend", "durable_resume",
                                                "durable_retention_days", "durable_max_turns")}))
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
        policy = durable_options(settings)
        # Capability/extension incompatibility is explicit: durable refuses
        # native host tools that bypass the bridge (same floor as Pi), and
        # the durable launcher owns no MCP/resource surface to grant.
        for tool in context.role.engine_tools:
            if tool in ("bash", "powershell", "edit", "write", "read", "Bash", "Read", "Edit",
                        "Write", "NotebookEdit", "Computer", "Glob", "Grep"):
                raise ConfigError("Native host tools bypass the OCI bridge; grant mizu operations instead")
        if settings.get("mcp_servers"):
            raise ConfigError("MCP servers are unsupported on pi-durable")
        if settings.get("resources"):
            raise ConfigError("Resource kinds are unsupported on pi-durable")
        prompt_bytes = prompt.encode()
        if not prompt_bytes:
            raise ConfigError("Durable prompt must be nonempty")
        if len(prompt_bytes) > EVENT_RECORD_BYTES:
            raise ConfigError("Durable prompt exceeds the bounded record")
        # The durable SQLite store (keyed by session_key) owns conversation
        # resume and supersedes native session files; the session record
        # still carries cumulative usage for rotation, like the pi engine.
        path, saved = session_record(context, profile, settings)
        session_key = path.parent.name
        project_name = getattr(getattr(context, "project", None), "name", "unknown")
        model = self.config.model(profile)
        grant = grant_for(settings, context.role, model)
        store_path = store_path_for(self.config.data, project_name, context.role.name, session_key)
        conn = open_store(store_path)
        existing = 0
        try:
            run_key = context.run_dir.name
            if policy["resume"] == "fresh" and load_run(conn, run_key) is not None:
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
            existing = len((load_run(conn, run_key) or {}).get("turns", []))
            if existing >= policy["max_turns"]:
                raise LimitExceeded("Durable turn bound exhausted for this run")
            reaped = None
            if policy["retention_days"] is not None:
                # Reap only terminal runs of this session store; the active
                # run is never a candidate. Accounting is reported, not silent.
                candidates = retention_candidates(conn, retention_days=policy["retention_days"])
                reaped = prune(conn, candidates, dry_run=False)
            if context.cancelled():
                complete_run(conn, run_key, {"cancelled": True}, status="cancelled")
                raise Cancelled("Run cancelled before durable dispatch")
            record_turn(conn, run_key, existing + 1, "prompt", {"bytes": len(prompt_bytes)})
            outcome = self._run_once(context, settings, model, prompt, profile, run_key, store_path,
                                     remaining=policy["max_turns"] - existing)
            complete_turn(conn, run_key, existing + 1, {"engine": ENGINE}, state="completed")
            if settings["session"] == "persistent" and not context.ephemeral:
                save_session(path, f"pi-durable:{session_key}",
                             {"tokens": _cumulative_tokens(saved, outcome)})
            complete_run(conn, run_key, {"engine": outcome.get("engine"), "requests": outcome.get("requests")}, status="completed")
            outcome["durable"] = {"resumed": bool(started_record.get("resumed")), "duplicate": False,
                                  "store": str(store_path), "projection": project_context(conn, run_key),
                                  "grant": grant, "reaped": reaped}
            return outcome
        except (Cancelled, ProtocolError, ModelFailure, ConfigError, LimitExceeded):
            try:
                record = load_run(conn, run_key)
                if record is not None and record["status"] == "active":
                    # Uncertain side-effect completion: mark unknown, never assume.
                    try:
                        complete_turn(conn, run_key, existing + 1,
                                      {"uncertain": True}, state="unknown")
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

    def _run_once(self, context, settings, model, prompt, profile, run_key, store_path,
                   remaining: int = 128) -> dict:
        # One admitted durable submission: effective file in, one bounded
        # JSON result document out. The bridge stays open for the whole
        # submission so tool callbacks and admission share one deadline.
        policy = role_policy_text(context.role)
        atomic_write(context.run_dir / "system.md", policy.encode())
        cwd = context.run_dir / "controller"
        mkdir(cwd)
        options = dict(settings.get("options", {}))
        # The turn budget is shared: persisted turns already spent on this
        # run leave `remaining` admissions for the Harness submission, so
        # retries and Harness-internal steps draw from one bound.
        effective_config = {
            "provider": model["provider"], "model": model["model"],
            "instructions": policy, "tools": tool_definitions(context.role.capabilities),
            "cwd": str(cwd), "store": str(store_path), "requestId": run_key, "prompt": prompt,
            "max_turns": max(1, remaining),
        }
        if options.get("thinkingLevel") is not None:
            effective_config["thinkingLevel"] = options["thinkingLevel"]
        deadline_ms = max(1, int(1000 * min(context.deadline - time.monotonic(),
                                            self.config.limits.run_seconds)))
        effective_config["deadline_ms"] = deadline_ms
        write_json(context.run_dir / "pi-durable-effective.json", effective_config)
        started = time.monotonic()
        context.model_evidence = {"profile": profile, **model, "engine": ENGINE, "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.run_seconds,
                    on_close=context.cancel_operations) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            env.update(_pi.credentials(self.config.file.parent / "credentials.env"))
            result = run(self.argv(context.role, context.run_dir, profile, context.goal_digest),
                         timeout=min(max(0.0, context.deadline - time.monotonic()),
                                     self.config.limits.run_seconds),
                         maximum=RESULT_BYTES, cwd=cwd, env=env, cancel=context.cancelled)
            if context.cancelled() or result.reason == "cancelled":
                raise Cancelled("Durable run cancelled")
            if result.reason == "timeout":
                raise LimitExceeded("Durable run exceeded its deadline")
            if result.reason == "output_limit":
                raise ProtocolError("Durable result exceeded its bound")
            if result.reason == "input_error" or result.reason == "output_error":
                raise ProtocolError(f"Durable transport failed: {result.reason}")
            if result.exit_code != 0:
                tail = (result.stderr or result.stdout or "").strip()[-2000:]
                raise ModelFailure("pi-durable", kind="launcher", message=tail or "launcher failed")
            try:
                reported = json.loads(result.stdout.strip())
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError("Durable launcher returned an invalid result") from exc
            interpret_result(reported, context, model, options.get("thinkingLevel"))
            return {**context.model_evidence, "seconds": round(time.monotonic() - started, 3)}


def interpret_result(reported, context, model, thinking_level) -> None:
    """Validate the launcher result document and record usage evidence.

    Schema: ``reported`` is the parsed one-shot result (``durable_result``
    true, ``status`` done/turn_bound/other). ``turn_bound`` maps to
    ``LimitExceeded`` so the spent budget surfaces as a bound, never a
    silent stop; other non-done statuses map to ``ModelFailure``. A done
    result still requires the ``mizu_finish`` seal and at least one
    admitted request, like the pi engine. Bounds: usage counters pass
    through as-is; evidence stays local. Failure: ``ProtocolError``,
    ``ModelFailure`` or ``LimitExceeded``.
    """
    if not isinstance(reported, dict) or reported.get("durable_result") is not True:
        raise ProtocolError("Durable launcher returned an invalid result")
    if reported.get("status") == "turn_bound":
        raise LimitExceeded("Durable turn bound exhausted")
    if reported.get("status") != "done":
        raise ModelFailure("pi-durable", kind="unanswered",
                           message=str(reported.get("reason") or "submission unanswered"))
    if not reported.get("finish_called") or context.finished is None:
        raise ProtocolError("Pi-durable settled without mizu_finish")
    if context.request_count == 0:
        raise ProtocolError("No runtime request admission observed")
    usage = reported.get("usage")
    if isinstance(usage, dict):
        mapped = {"input_tokens": usage.get("input", 0), "output_tokens": usage.get("output", 0),
                  "cache_read_tokens": usage.get("cacheRead", 0),
                  "cache_write_tokens": usage.get("cacheWrite", 0)}
        context.model_evidence.update(requests=int(reported.get("requests", 0) or 0),
                                      usage=[mapped], usage_known=True,
                                      observed_models=[{**model, "thinkingLevel": thinking_level}],
                                      usage_observations=[{"usage": mapped, **model}])
    else:
        context.model_evidence.update(requests=int(reported.get("requests", 0) or 0))


def _cumulative_tokens(saved, outcome) -> int:
    """Cumulative durable session tokens: saved total plus this run's usage.

    Schema: reads the bounded saved record plus the mapped launcher usage
    (``input_tokens``/``output_tokens``/``cache_read_tokens``/
    ``cache_write_tokens``). Bounds: capped at 2**63-1. Trust: local
    evidence only. Failure: unknown shapes add nothing, so rotation stays
    age-driven when the launcher reports no recognized counters.
    """
    run_total = 0
    # The run outcome carries model_evidence usage: a list with one mapped
    # entry (or empty when the launcher reported no usage).
    items = outcome.get("usage") if isinstance(outcome, dict) else None
    entries = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
    for value in entries[:4096]:
        if not isinstance(value, dict):
            continue
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "other_tokens"):
            piece = value.get(field, 0)
            if type(piece) is int and piece > 0:
                run_total = min(run_total + piece, MAX_SESSION_TOKENS)
    try:
        previous = session_token_total(saved, ENGINE)
    except Exception:
        previous = 0
    return min(previous + run_total, MAX_SESSION_TOKENS)
