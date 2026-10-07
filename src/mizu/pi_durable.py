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
                             save_session, session_generation, session_record,
                             session_token_total)
from .errors import Cancelled, ConfigError, LimitExceeded, ProtocolError, ModelFailure
from .fs import atomic_write, canonical, digest, mkdir, write_json
from .pi_durable_store import (BACKENDS, RESUME_MODES, complete_run, complete_turn,
                               begin_run, check_grant, grant_digest, load_run,
                               open_store, project_context, prune, prune_generations,
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
        # Conversation identity: a persistent session shares one store per
        # rotation generation (the content-bound key alone at generation
        # zero, suffixed after), so rotation starts a fresh conversation;
        # every other run gets a distinct key that is stable across retries
        # of the same run, so two runs never meet in one store while crash
        # recovery still resumes the active run.
        path, saved = session_record(context, profile, settings)
        base_key = ""
        generation = 0
        if settings.get("session") == "persistent" and not context.ephemeral:
            base_key = path.parent.name
            generation = session_generation(path.parent)
            session_key = base_key if not generation else f"{base_key}-g{generation}"
        else:
            session_key = f"run-{context.run_dir.name}"
        project_name = getattr(getattr(context, "project", None), "name", "unknown")
        model = self.config.model(profile)
        grant = grant_for(settings, context.role, model)
        store_path = store_path_for(self.config.data, project_name, context.role.name, session_key)
        pruned_generations = []
        if generation and not store_path.exists():
            # First dispatch after rotation: retire the abandoned
            # generation stores so the previous conversation is gone,
            # not lingering beside the fresh one.
            pruned_generations = prune_generations(
                self.config.data, project=project_name, role=context.role.name,
                base_key=base_key, generation=generation)
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
            # The shared budget covers persisted turns plus Harness steps: the
            # just-recorded prompt turn leaves `remaining` admissions for this
            # submission. Refuse without dispatch when nothing remains, so the
            # grant is never exceeded by one.
            remaining = policy["max_turns"] - (existing + 1)
            if remaining < 1:
                raise LimitExceeded("Durable turn bound exhausted for this run")
            outcome = self._run_once(context, settings, model, prompt, profile, run_key, store_path,
                                     remaining=remaining)
            complete_turn(conn, run_key, existing + 1, {"engine": ENGINE}, state="completed")
            if settings["session"] == "persistent" and not context.ephemeral:
                save_session(path, f"pi-durable:{session_key}",
                             {"tokens": _cumulative_tokens(saved, outcome)})
            complete_run(conn, run_key, {"engine": outcome.get("engine"), "requests": outcome.get("requests")}, status="completed")
            outcome["durable"] = {"resumed": bool(started_record.get("resumed")), "duplicate": False,
                                  "store": str(store_path), "projection": project_context(conn, run_key),
                                  "grant": grant, "reaped": reaped,
                                  "pruned_generations": pruned_generations}
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
        # run (including the just-recorded prompt turn) leave `remaining`
        # admissions for the Harness submission, so retries and
        # Harness-internal steps draw from one bound.
        if not isinstance(remaining, int) or remaining < 1:
            raise LimitExceeded("Durable turn bound exhausted for this run")
        effective_config = {
            "provider": model["provider"], "model": model["model"],
            "instructions": policy, "tools": tool_definitions(context.role.capabilities),
            "cwd": str(cwd), "store": str(store_path), "requestId": run_key, "prompt": prompt,
            "max_turns": remaining,
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


def _runtime_evidence(context, model, thinking_level) -> dict | None:
    """Host-recorded per-request evidence, like the pi engine.

    Schema: reads ``context.runtime_usage`` (``_model_usage`` reports also
    persisted in ``runtime-usage.json``) plus ``context.runtime_models``
    identity and the host admission count. Returns None when no
    per-request usage was reported, so callers fall back to the
    launcher-aggregated document. Trust: local host evidence only.
    Failure: never raises; malformed state reads as absent.
    """
    try:
        items = sorted(getattr(context, "runtime_usage", {}).items())
    except Exception:
        return None
    if not items:
        return None
    try:
        models = getattr(context, "runtime_models", {}) or {}
    except Exception:
        models = {}
    usages = [value for _, value in items]
    observations = []
    for seq, value in items:
        try:
            identity = models.get(seq) or {}
        except Exception:
            identity = {}
        observations.append({"usage": value,
                             "provider": identity.get("provider"),
                             "model": identity.get("model")})
    try:
        observed = list(models.values())
    except Exception:
        observed = []
    if not observed:
        observed = [{**model, "thinkingLevel": thinking_level}]
    try:
        requests = int(getattr(context, "request_count", 0) or 0)
    except Exception:
        requests = 0
    return {"requests": requests, "usage": usages, "usage_known": True,
            "observed_models": observed, "usage_observations": observations}


def _record_launcher_evidence(reported, context, model, thinking_level) -> None:
    """Record launcher requests/usage into run evidence (raw shapes).

    Schema: prefers host-recorded per-request ``runtime_usage`` (the same
    source the pi engine records, also persisted in
    ``runtime-usage.json``); the launcher-aggregated ``harness.usage``
    document is only a fallback when no per-request report arrived.
    Raw provider shapes are stored as-is so ``usage.normalize`` counts
    them under this engine; nothing is remapped to canonical keys here.
    Bounds: evidence stays local. Failure: never invents tokens.
    """
    runtime = _runtime_evidence(context, model, thinking_level)
    if runtime is not None:
        if not runtime["requests"]:
            try:
                reported_requests = int((reported.get("requests", 0) if isinstance(reported, dict) else 0) or 0)
            except Exception:
                reported_requests = 0
            runtime["requests"] = reported_requests
        context.model_evidence.update(runtime)
        return
    usage = reported.get("usage") if isinstance(reported, dict) else None
    if isinstance(usage, dict):
        context.model_evidence.update(requests=int(reported.get("requests", 0) or 0),
                                      usage=[dict(usage)], usage_known=True,
                                      observed_models=[{**model, "thinkingLevel": thinking_level}],
                                      usage_observations=[{"usage": dict(usage), **model}])
    else:
        context.model_evidence.update(requests=int((reported.get("requests", 0) if isinstance(reported, dict) else 0) or 0))


def interpret_result(reported, context, model, thinking_level) -> None:
    """Validate the launcher result document and record usage evidence.

    Schema: ``reported`` is the parsed one-shot result (``durable_result``
    true, ``status`` done/turn_bound/other). ``turn_bound`` maps to
    ``LimitExceeded`` so the spent budget surfaces as a bound, never a
    silent stop; other non-done statuses map to ``ModelFailure``. A done
    result still requires the ``mizu_finish`` seal and at least one
    admitted request, like the pi engine. Launcher requests/usage counters
    are recorded before raising on ``turn_bound`` so the bound-exhausted
    path keeps its accounting. Bounds: usage counters pass
    through as-is; evidence stays local. Failure: ``ProtocolError``,
    ``ModelFailure`` or ``LimitExceeded``.
    """
    if not isinstance(reported, dict) or reported.get("durable_result") is not True:
        raise ProtocolError("Durable launcher returned an invalid result")
    if reported.get("status") == "turn_bound":
        _record_launcher_evidence(reported, context, model, thinking_level)
        raise LimitExceeded("Durable turn bound exhausted")
    if reported.get("status") != "done":
        raise ModelFailure("pi-durable", kind="unanswered",
                           message=str(reported.get("reason") or "submission unanswered"))
    if not reported.get("finish_called") or context.finished is None:
        raise ProtocolError("Pi-durable settled without mizu_finish")
    if context.request_count == 0:
        raise ProtocolError("No runtime request admission observed")
    runtime = _runtime_evidence(context, model, thinking_level)
    if runtime is not None:
        context.model_evidence.update(runtime)
        return
    usage = reported.get("usage")
    if isinstance(usage, dict):
        context.model_evidence.update(requests=int(reported.get("requests", 0) or 0),
                                      usage=[dict(usage)], usage_known=True,
                                      observed_models=[{**model, "thinkingLevel": thinking_level}],
                                      usage_observations=[{"usage": dict(usage), **model}])
    else:
        context.model_evidence.update(requests=int(reported.get("requests", 0) or 0))


def _cumulative_tokens(saved, outcome) -> int:
    """Cumulative durable session tokens: saved total plus this run's usage.

    Schema: reads the bounded saved record plus this run's usage entries
    in either raw provider shape (``input``/``output``/``cacheRead``/
    ``cacheWrite``, as recorded from per-request runtime evidence) or
    canonical shape (``input_tokens``/..., as kept by older evidence).
    Bounds: capped at 2**63-1. Trust: local evidence only. Failure:
    unknown shapes add nothing, so rotation stays age-driven when the
    launcher reports no recognized counters.
    """
    from .usage import normalize as _normalize
    run_total = 0
    # The run outcome carries model_evidence usage: per-request raw entries
    # (or one fallback entry, or empty when no usage was reported).
    items = outcome.get("usage") if isinstance(outcome, dict) else None
    entries = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
    for value in entries[:4096]:
        if not isinstance(value, dict):
            continue
        try:
            part = _normalize(value, ENGINE)
        except Exception:
            part = None
        if isinstance(part, dict) and not part.get("unknown_shape"):
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_write_tokens", "other_tokens"):
                piece = part.get(field, 0)
                if type(piece) is int and piece > 0:
                    run_total = min(run_total + piece, MAX_SESSION_TOKENS)
        else:
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
