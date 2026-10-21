"""Shared routing, bounded evidence and admissions.

Pi reserves logical model requests; Codex turns and Claude queries reserve
one work unit. None of these counts represents HTTP requests or money.
"""
from __future__ import annotations

import json

from .budget import Budget
from .config import role_requests_per_run
from .errors import (Cancelled, ConfigError, DeadlineWrapUp, DrainYieldExceeded, InfraExceeded,
                     LimitExceeded, ProtocolError, RunDeadlineExceeded)
from .fs import write_json, canonical, now

#: Driver stderr tail retained per run (bounded failure evidence).
DIAGNOSTICS_TAIL_BYTES = 256 * 1024
#: Default aggregate engine event-stream retention per invocation (evidence,
#: not a bill). Operator-overridable via `[limits] event_stream_bytes`; the
#: policy default always equals this mechanism default.
EVENT_STREAM_BYTES = 4 * 1024 * 1024
#: Fixed transport safety: one engine JSONL record beyond this is refused
#: outright (fail closed), so a lineless flood cannot exhaust memory while
#: aggregate retention stays a separate operator policy. Not a knob.
EVENT_RECORD_BYTES = 1024 * 1024
#: Failure-evidence tail attached to driver errors.
ERROR_TAIL_CHARS = 4000


def tool_names(capabilities: tuple[str, ...]) -> list[str]:
    """MCP tool names exposed for a capability set (matches the Pi convention)."""
    return ["mizu_" + name for name in capabilities]


def engine_of(config, profile: str) -> str:
    """Return the validated engine for a configured profile (explicit engine)."""
    return config.engine(profile)


def command_for(config, engine: str) -> tuple[str, ...]:
    """Return the operator-owned trusted argv for an engine."""
    return config.command(engine)


def driver_for(config, profile: str, cache: dict):
    """Return the cached driver for a profile's engine. No model defaults."""
    # Lazy: codex/claude import this registry, so a top-level import would cycle.
    engine = engine_of(config, profile)
    if engine not in cache:
        if engine == "pi":
            from .pi import PiDriver
            cache[engine] = PiDriver(config)
        elif engine == "pi-durable":
            from .pi_durable import PiDurableDriver
            cache[engine] = PiDurableDriver(config)
        elif engine == "codex":
            from .codex import CodexDriver
            cache[engine] = CodexDriver(config)
        elif engine == "claude":
            from .claude import ClaudeDriver
            cache[engine] = ClaudeDriver(config)
        else:
            raise ConfigError(f"Unknown inference engine: {engine}")
    return cache[engine]


def admit_invocation(context, *, unit: str) -> dict:
    """Admit one non-Pi driver invocation against the shared daily budget.

    Idempotent per run directory: a retry records the same admission without
    double-charging (see Budget.take). Enforces the role-effective
    `requests_per_run` (per-role override or project default).
    """
    if context.finished is not None:
        raise ProtocolError("No inference after the work unit is sealed")
    context.check_deadlines()
    if getattr(context, "invocation_admitted_attempt", None) == context.execution_attempt:
        return {"admitted": True}
    if getattr(context, 'recovery_probe', False):
        context.revalidate_admission()
    context.check_deadlines()
    context.enter_deadline_wrap_up()
    context.enter_drain_wrap_up()
    if context.bound_hit == "drain" and not context.wrap_up_attempted:
        context.drain_refusal(context._bound_message())
    over = context.bound_hit in ("deadline", "drain") or context.request_count >= role_requests_per_run(context.config, context.spend_role or context.role)
    if over and getattr(context, 'recovery_probe', False):
        context.bound_hit = 'requests'
        context.admission_error = 'request_ceiling'
        raise LimitExceeded('Recovery request ceiling reached')
    if over:
        requests_used, requests_limit = context._wrap_up_request_used_limit()
        if requests_used >= requests_limit:
            if context.bound_hit == "drain":
                raise DrainYieldExceeded("Drain wrap-up request allowance exhausted")
            if context.bound_hit == "deadline":
                raise RunDeadlineExceeded("Deadline wrap-up request allowance exhausted")
            raise LimitExceeded("Per-run provider request budget exhausted")
    project_name = getattr(getattr(context, "project", None), "name", "")
    try:
        Budget(context.config.data / "budget", context.config.limits.daily_requests,
               context.config.limits.retention_days,
               context.config.limits.shared_daily_requests, context.config.timezone).take(
                   f"{context.run_dir.name}:{context.execution_attempt}:invocation", project=project_name)
    except InfraExceeded:
        if hasattr(context,'model_evidence'):
            context.model_evidence['admission_status']='rejected'
        try:
            context.admission_wait = True
        except Exception:
            pass
        raise
    except LimitExceeded:
        if hasattr(context,'model_evidence'):
            context.model_evidence['admission_status']='rejected'
        raise
    except Exception:
        if hasattr(context,'model_evidence'):
            context.model_evidence.update(requests_known=False,admission_status='unconfirmed')
        raise
    if over:
        # Wrap state records only after daily admission succeeds. Delegated
        # children spend their clamped share (checked above); anything else
        # would bypass the share and be lost on join, which copies only
        # delegated_* back to the caller.
        context.bound_hit = context.bound_hit or "requests"
        if getattr(context, "delegated_child", False):
            context.delegated_wrap_up_requests = getattr(context, "delegated_wrap_up_requests", 0) + 1
        else:
            context.wrap_up_requests = getattr(context, "wrap_up_requests", 0) + 1
    context.request_count += 1
    context.request_sequences.add((context.execution_attempt, 0))
    context.start_request(0)  # One enclosing Claude query / Codex turn.
    context.invocation_admitted_attempt = context.execution_attempt
    if getattr(context, 'recovery_probe', False):
        context.recovery_progress()
    if hasattr(context,"model_evidence"):
        context.model_evidence.update(requests=context.request_count,requests_known=True,admission_status="accepted")
    write_json(context.run_dir / "admission.json", {"run":context.run_dir.name,
               "requests":context.request_count,"request_unit":unit,"admitted_at":now()})
    return {"admitted": True}


def parse_event(line):
    """Finite UTF-8 JSON event; malformed values never enter saved evidence."""
    value=json.loads(line,parse_constant=lambda v:(_ for _ in ()).throw(ValueError('Non-finite event')))
    canonical(value)
    return value
