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
from .errors import Cancelled, ConfigError, LimitExceeded, ProtocolError, ModelFailure, RunDeadlineExceeded, DeadlineWrapUp, DrainYieldExceeded
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
#: Selectors allow 64 rules of 64 candidates; ordinals count fallback advances.
MAX_ATTEMPT_ORDINAL = 64 * 64


#: Compaction token bound (matches limits.session_max_tokens ceiling).
#: An unset durable_compaction_* option keeps the engine/provider default
#: (today the pinned pi-durable DEFAULT_COMPACTION_POLICY); Mizu never
#: injects its own value. Enabled stays true; disabling is not exposed.
COMPACTION_TOKEN_MAX = 1073741824
#: Profile-option to Harness-policy field names for the live-context bound.
COMPACTION_FIELDS = {
    "durable_compaction_reserve_tokens": "reserveTokens",
    "durable_compaction_keep_recent_tokens": "keepRecentTokens",
    "durable_compaction_background_tokens": "backgroundTokens",
}


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
    if "durable_max_turns" in options:
        raise ConfigError("durable_max_turns was removed; requests_per_run is the single per-unit spend bound")
    compaction: dict[str, int] = {}
    for option_key, policy_key in COMPACTION_FIELDS.items():
        if option_key not in options:
            continue
        value = options[option_key]
        if type(value) is not int or not 0 <= value <= COMPACTION_TOKEN_MAX:
            raise ConfigError(f"{option_key} must be an integer in [0, 1073741824]")
        compaction[policy_key] = value
    return {"backend": backend, "resume": resume, "retention_days": retention,
            "compaction": compaction or None}


def durable_auth_path(config) -> Path:
    """Pi login store read by the durable launcher (same source as pi).

    Schema: ``config.agent_dir("pi-durable")/auth.json`` (operator-owned
    engine directory, like the pi engine's own ``auth.json``). Pi's own
    model runtime opens the shared store file-backed and persists OAuth
    refreshes under Pi's cross-process lock, so a rotating refresh is
    never spent without write-back (observed 2026-10-11 ``invalid_grant``
    from the prior read-only seed). Bounds:
    path only, never content: credentials stay out of evidence and content
    hashes. Trust: operator-owned login state, read host-side by the
    controller, never mounted into command containers. Failure: the
    launcher treats a missing file as env-only auth (reads resolve
    absent without creating it) and refuses a malformed store loudly.
    """
    agent_dir = config.agent_dir(ENGINE)
    mkdir(agent_dir)
    return agent_dir / "auth.json"


def durable_models_path(config) -> Path:
    """Operator-defined model catalog read by the durable launcher.

    Schema: ``config.agent_dir("pi-durable")/models.json`` (the same
    engine directory the pi engine reads, so one file defines a model for
    both engines). Bounds: path only, never content: the catalog names
    providers, endpoints and prices, and the model file stays out of
    evidence and content hashes. Trust: operator-owned engine state, read
    host-side by the controller. Failure: a missing file leaves the
    bundled catalog in force; a malformed file refuses loudly.
    """
    agent_dir = config.agent_dir(ENGINE)
    mkdir(agent_dir)
    return agent_dir / "models.json"


#: Options bound into the durable resume grant. Reasoning effort is
#: excluded: like the session-identity live keys in
#: CONVERSATION_LIVE_OPTIONS, it is applied per turn to the ongoing
#: conversation, so an effort-only retune resumes instead of refusing.
#: Every other listed option still binds; unlisted keys stay out.
GRANT_BOUND_OPTIONS = frozenset((
    "cacheRetention",
    "durable_backend",
    "durable_resume",
    "durable_retention_days",
    "durable_compaction_reserve_tokens",
    "durable_compaction_keep_recent_tokens",
    "durable_compaction_background_tokens",
))


def grant_for(settings: dict, role, model: dict) -> str:
    policy = role_policy_text(role)
    options_digest = digest(canonical({k: v for k, v in settings.get("options", {}).items()
                                       if k in GRANT_BOUND_OPTIONS}))
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
        authority = getattr(context, "turn_authority_role", None) or context.role
        settings = effective(self.config, authority, profile)
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
        run_key = context.run_dir.name
        attempt_ordinal = getattr(context, "attempt_ordinal", 0)
        if type(attempt_ordinal) is not int or not 0 <= attempt_ordinal <= MAX_ATTEMPT_ORDINAL:
            raise ConfigError("Durable selector attempt ordinal is outside its bound")
        if attempt_ordinal:
            run_key = f"{run_key}-a{attempt_ordinal}"
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
        grant = grant_for(settings, authority, model)
        store_path = (context.run_dir / 'durable.sqlite' if getattr(context, 'recovery_probe', False)
                      else store_path_for(self.config.data, project_name, context.role.name, session_key))
        pruned_generations = []
        if generation and not store_path.exists():
            # First dispatch after rotation: retire the abandoned
            # generation stores so the previous conversation is gone,
            # not lingering beside the fresh one.
            pruned_generations = prune_generations(
                self.config.data, project=project_name, role=context.role.name,
                base_key=base_key, generation=generation)
        conn = open_store(store_path)
        seq = None
        # Permission is host-only and process-local. Persisted rows alone never
        # authorize a new turn, including in fresh mode. Selector ordinals still
        # identify candidates; turn request ids identify submissions within one.
        identity = (run_key, str(store_path), grant)
        continuing = (getattr(context, "turn_continuation", False)
                      and getattr(context, "_durable_dispatch", None) == identity)
        context.turn_continuation = False  # Consume the host's one-dispatch permission.
        try:
            if policy["resume"] == "fresh" and load_run(conn, run_key) is not None and not continuing:
                raise ConfigError("Durable resume mode is fresh; retry with a new run")
            started_record = begin_run(conn, run_key=run_key, session_key=session_key,
                                       project=project_name, role=context.role.name, grant=grant)
            if (started_record.get("resumed") and started_record.get("status") == "completed"
                    and not continuing):
                # Only a persisted host seal makes completed replay useful:
                # a clean unsealed return needs the explicit in-process
                # continuation, never another futile terminal response.
                result = started_record.get("result")
                if not isinstance(result, dict):
                    raise ConfigError("Durable completed run lacks a replayable host finish")
                if result.get("sealed") is not True or not isinstance(result.get("finish"), dict):
                    if result.get("sealed") is False:
                        raise ConfigError("Durable completed unsealed run requires in-process continuation")
                    raise ConfigError("Durable completed run lacks a replayable host finish")
                context.finished = dict(result["finish"])
                result = dict(result)
                result.pop("finish", None)
                result["durable"] = {"resumed": True, "duplicate": True,
                                     "projection": project_context(conn, run_key)}
                return result
            if started_record.get("resumed"):
                check_grant(conn, run_key, grant)
            record = load_run(conn, run_key)
            turns = record["turns"]
            payload = {"bytes": len(prompt_bytes), "prompt_digest": digest(prompt_bytes),
                       "turn_grant": grant_for(settings, context.role, model)}
            if continuing:
                if (record.get("result") or {}).get("sealed"):
                    raise ConfigError("Durable continuation refused after seal")
                seq = len(turns) + 1
            elif turns:
                # Recover the *same* interrupted submission, never manufacture
                # a new id for uncertain effects. The Harness's replay-unsafe
                # tools retain unknown completion rather than executing again.
                last = turns[-1]
                if (last["state"] not in ("started", "unknown")
                        or not last["payload"].get("request_id")
                        or any(last["payload"].get(k) != v for k, v in payload.items())):
                    raise ConfigError("Durable recovery refused: prompt or turn grant differs, or turn settled")
                seq = last["seq"]
                payload = last["payload"]
            else:
                seq = 1
            request_id = run_key if seq == 1 else f"{run_key}-t{seq}"
            if "request_id" in payload and payload["request_id"] != request_id:
                raise ConfigError("Durable recovery refused: submission identity differs")
            payload["request_id"] = request_id
            reaped = None
            if policy["retention_days"] is not None:
                # Reap only other terminal runs: this unit may be continuing
                # a clean unsealed return. Accounting is reported, not silent.
                candidates = retention_candidates(conn, retention_days=policy["retention_days"])
                reaped = prune(conn, [key for key in candidates if key != run_key], dry_run=False)
            if context.cancelled():
                complete_run(conn, run_key, {"cancelled": True}, status="cancelled")
                context.check_deadlines()
                raise Cancelled("Run cancelled before durable dispatch")
            record_turn(conn, run_key, seq, "prompt", payload)
            context._durable_dispatch = identity
            context._durable_request_id = request_id
            # No turn bound: every provider request inside the submission
            # passes _budget admission, so requests_per_run alone bounds the
            # unit and a refusal returns as request_bound below.
            dispatch_attempt = context.execution_attempt
            context._durable_dispatch_usage = None
            outcome = self._run_once(context, settings, model, prompt, profile, run_key, store_path)
            complete_turn(conn, run_key, seq, {"engine": ENGINE}, state="completed")
            if settings["session"] == "persistent" and not context.ephemeral:
                dispatch_usage = getattr(context, "_durable_dispatch_usage", None)
                if dispatch_usage is None:
                    runtime = _runtime_evidence(context, model, settings.get("options", {}).get("thinkingLevel"),
                                                execution_attempt=dispatch_attempt)
                    dispatch_usage = (runtime["usage"] if runtime is not None
                                      else outcome.get("usage", []))
                session_outcome = {**outcome, "usage": dispatch_usage}
                save_session(path, f"pi-durable:{session_key}",
                             {"tokens": _cumulative_tokens(saved, session_outcome)})
            # A clean unsealed return is terminal, even when no follow-up is
            # allowed. Only explicit in-process host permission may reopen it.
            run_result = {"engine": outcome.get("engine"), "requests": outcome.get("requests"),
                          "sealed": context.finished is not None}
            if context.finished is not None:
                run_result["finish"] = context.finished
            complete_run(conn, run_key, run_result, status="completed")
            outcome["durable"] = {"resumed": bool(started_record.get("resumed")), "duplicate": False,
                                  "store": str(store_path), "projection": project_context(conn, run_key),
                                  "grant": grant, "reaped": reaped,
                                  "pruned_generations": pruned_generations}
            return outcome
        except (Cancelled, ProtocolError, ModelFailure, ConfigError, LimitExceeded):
            try:
                record = load_run(conn, run_key)
                if seq is not None and record is not None and record["status"] == "active":
                    # Uncertain side-effect completion: mark unknown, never assume.
                    try:
                        complete_turn(conn, run_key, seq,
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

    def _run_once(self, context, settings, model, prompt, profile, run_key, store_path) -> dict:
        # One admitted durable submission: effective file in, one bounded
        # JSON result document out. The bridge stays open for the whole
        # submission; each provider admission has its own absolute deadline.
        # Spend is bounded only by request admission, never a turn cap.
        policy = role_policy_text(context.role)
        atomic_write(context.run_dir / "system.md", policy.encode())
        cwd = context.run_dir / "controller"
        mkdir(cwd)
        options = dict(settings.get("options", {}))
        policy_options = durable_options(settings)
        effective_config = {
            "provider": model["provider"], "model": model["model"],
            "instructions": policy, "tools": tool_definitions(context.role.capabilities),
            "cwd": str(cwd), "store": str(store_path),
            "requestId": getattr(context, "_durable_request_id", run_key), "prompt": prompt,
            "authPath": str(durable_auth_path(self.config)),
            "modelsPath": str(durable_models_path(self.config)),
        }
        # Unset keeps the engine/provider default: omit the field so the
        # Harness resolves its own built-in policy instead of a Mizu value.
        # A partial triple passes through; the Harness merges it over its
        # defaults. Retuning keeps the same store (live keys).
        if policy_options["compaction"] is not None:
            effective_config["compaction"] = dict(policy_options["compaction"])
        if options.get("thinkingLevel") is not None:
            effective_config["thinkingLevel"] = options["thinkingLevel"]
        if options.get("cacheRetention") is not None:
            effective_config["cacheRetention"] = options["cacheRetention"]
        write_json(context.run_dir / "pi-durable-effective.json", effective_config)
        started = time.monotonic()
        context.model_evidence = {"profile": profile, **model, "engine": ENGINE, "requests": 0,
                                  "request_unit": "model_request", "usage": [], "usage_known": False}
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.model_request_seconds,
                    on_close=context.cancel_operations) as bridge:
            env = environment(extra={"MIZU_BRIDGE_CONFIG": str(bridge.config_file)})
            env.update(_pi.credentials(self.config.file.parent / "credentials.env"))
            try:
                result = run(self.argv(context.role, context.run_dir, profile, context.goal_digest),
                             timeout=context.transport_deadline,
                             maximum=RESULT_BYTES, cwd=cwd, env=env, cancel=context.host_cancelled)
            finally:
                # Provider errors must reach their own result path before drain
                # polling can choose a bounded transition.
                context.request_failure_pending = False
            # Classify known transport timeouts/cancellation by their source.
            # A normal error result is not a deadline merely because its parse
            # or local cleanup finishes after the wall.
            if result.reason in ("timeout", "cancelled"):
                context.check_deadlines()
            if context.host_cancelled() or result.reason == "cancelled":
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
            context.check_deadlines()
            if getattr(context, 'recovery_probe', False):
                context.model_evidence['settled'] = True
            return {**context.model_evidence, "seconds": round(time.monotonic() - started, 3)}


def _runtime_evidence(context, model, thinking_level, *, execution_attempt=None) -> dict | None:
    """Host-recorded per-request evidence, like the pi engine.

    Schema: reads ``context.runtime_usage`` (``_model_usage`` reports also
    persisted in ``runtime-usage.json``) plus ``context.runtime_models``
    identity and the host admission count. Returns None when no
    per-request usage was reported, so callers fall back to the
    launcher-aggregated document. Trust: local host evidence only.
    Failure: never raises; malformed state reads as absent.
    """
    try:
        items = list(getattr(context, "runtime_usage", {}).items())
        if execution_attempt is not None:
            items = [(key, value) for key, value in items
                     if isinstance(key, tuple) and len(key) == 2 and key[0] == execution_attempt]
        items.sort()
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
    if execution_attempt is not None:
        observed = [models.get(seq) for seq, _ in items if models.get(seq)]
    else:
        try:
            observed = list(models.values())
        except Exception:
            observed = []
    if not observed:
        observed = [{**model, "thinkingLevel": thinking_level}]
    try:
        if execution_attempt is not None and hasattr(context, "request_sequences"):
            requests = sum(1 for attempt, _ in context.request_sequences
                           if attempt == execution_attempt)
        else:
            requests = int(getattr(context, "request_count", 0) or 0)
    except Exception:
        requests = 0
    return {"requests": requests, "usage": usages, "usage_known": True,
            "observed_models": observed, "usage_observations": observations}


def _dispatch_usage(reported, context, model, thinking_level) -> list[dict]:
    """Usage for this one Harness dispatch, with its own launcher delta fallback."""
    runtime = _runtime_evidence(context, model, thinking_level,
                                execution_attempt=getattr(context, "execution_attempt", None))
    if runtime is not None:
        return runtime["usage"]
    usage = reported.get("usage") if isinstance(reported, dict) else None
    return [dict(usage)] if isinstance(usage, dict) else []


def _record_launcher_evidence(reported, context, model, thinking_level) -> None:
    """Record this dispatch's host-preferred evidence in the run total.

    Schema: each execution attempt contributes its host per-request usage,
    when present, otherwise its launcher per-dispatch delta (not session totals). Attempt-keyed replacement
    prevents a retry from counting the same usage or requests twice. Raw
    provider shapes are retained. Bounds: evidence stays local. Failure:
    never invents token usage.
    """
    attempt = getattr(context, "execution_attempt", None)
    runtime = _runtime_evidence(context, model, thinking_level,
                                execution_attempt=attempt)
    usage = None
    if runtime is not None:
        usage = list(runtime["usage"])
        requests = runtime["requests"]
        if not requests:
            try:
                requests = int((reported.get("requests", 0)
                                if isinstance(reported, dict) else 0) or 0)
            except Exception:
                requests = 0
        observed_models = runtime["observed_models"]
        observations = runtime["usage_observations"]
        usage_known = True
    else:
        reported_usage = reported.get("usage") if isinstance(reported, dict) else None
        usage = [dict(reported_usage)] if isinstance(reported_usage, dict) else []
        try:
            requests = int((reported.get("requests", 0)
                            if isinstance(reported, dict) else 0) or 0)
        except Exception:
            requests = 0
        observed_models = ([{**model, "thinkingLevel": thinking_level}]
                           if usage else [])
        observations = ([{"usage": usage[0], **model}] if usage else [])
        usage_known = bool(usage)

    attempts = getattr(context, "_durable_attempt_evidence", None)
    if not isinstance(attempts, dict):
        attempts = {}
        context._durable_attempt_evidence = attempts
    # Execution attempts are unique across turns and selector candidates;
    # reassigning the current key also makes retries idempotent.
    key = attempt if isinstance(attempt, int) else len(attempts)
    attempts[key] = {"requests": requests, "usage": usage,
                     "usage_known": usage_known,
                     "observed_models": observed_models,
                     "usage_observations": observations}

    ordered = [attempts[index] for index in sorted(attempts)]
    total_requests = sum(item["requests"] for item in ordered)
    all_usage = [entry for item in ordered for entry in item["usage"]]
    known = any(item["usage_known"] for item in ordered)
    all_models = [entry for item in ordered for entry in item["observed_models"]]
    all_observations = [entry for item in ordered for entry in item["usage_observations"]]
    context.model_evidence.update(requests=total_requests, usage=all_usage,
                                  usage_known=known, observed_models=all_models,
                                  usage_observations=all_observations)


def interpret_result(reported, context, model, thinking_level) -> None:
    """Validate the launcher result document and record usage evidence.

    Schema: ``reported`` is the parsed one-shot result (``durable_result``
    true, ``status`` done/unanswered/request_bound). ``request_bound`` maps
    to ``LimitExceeded`` so the spent budget surfaces as a bound, never a
    silent stop; only the known terminal ``unanswered`` status maps to
    ``ModelFailure``, while unknown statuses are ``ProtocolError``. A done
    a normal ``done`` result may lack the host seal; Engine._execute applies
    the configured bounded follow-up. A reported finish still must match the
    host seal, and at least one request must have been admitted. Launcher/runtime requests and
    usage are recorded before raising on ``request_bound`` or terminal
    ``unanswered`` so failed submissions keep their accounting. Bounds:
    usage counters pass
    through as-is; evidence stays local. Failure: ``ProtocolError``,
    ``ModelFailure`` or ``LimitExceeded``.
    """
    if not isinstance(reported, dict) or reported.get("durable_result") is not True:
        raise ProtocolError("Durable launcher returned an invalid result")
    context._durable_dispatch_usage = _dispatch_usage(reported, context, model, thinking_level)
    if reported.get("status") == "request_bound":
        _record_launcher_evidence(reported, context, model, thinking_level)
        context.bound_hit = context.bound_hit or "requests"
        requests_used, requests_limit = context._wrap_up_request_used_limit()
        if context.bound_hit == "drain":
            if not context.wrap_up_attempted:
                raise DeadlineWrapUp(context._bound_message())
            if requests_used >= requests_limit:
                raise DrainYieldExceeded("Drain wrap-up request allowance exhausted")
        if context.bound_hit == "deadline" and requests_used >= requests_limit:
            raise RunDeadlineExceeded("Deadline wrap-up request allowance exhausted")
        raise LimitExceeded("Per-run provider request budget exhausted")
    if reported.get("status") == "unanswered":
        _record_launcher_evidence(reported, context, model, thinking_level)
        reason = str(reported.get("reason") or "submission unanswered")
        detail = reported.get("detail")
        message = f"{reason}: {detail}" if isinstance(detail, str) and detail else reason
        raise ModelFailure("pi-durable", kind="unanswered", message=message)
    if reported.get("status") != "done":
        raise ProtocolError("Durable launcher returned an unknown settlement status")
    if type(reported.get("finish_called")) is not bool:
        raise ProtocolError("Durable launcher returned an invalid finish flag")
    if context.request_count == 0:
        raise ProtocolError("No runtime request admission observed")
    if not reported.get("finish_called") and context.finished is None:
        # Only a positive normal `done` settlement reaches this branch.
        # `unanswered` remains ModelFailure above; malformed/inconsistent
        # finish reports still fail as protocol errors below.
        _record_launcher_evidence(reported, context, model, thinking_level)
        return
    if not reported.get("finish_called") or context.finished is None:
        raise ProtocolError("Durable finish report disagrees with host seal")
    _record_launcher_evidence(reported, context, model, thinking_level)


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
