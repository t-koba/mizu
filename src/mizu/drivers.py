"""Shared routing, bounded evidence and admissions.

Pi reserves logical model requests; Codex turns and Claude queries reserve
one work unit. None of these counts represents HTTP requests or money.
"""
from __future__ import annotations

import json

from .budget import Budget
from .errors import Cancelled, ConfigError, InfraExceeded, LimitExceeded, ProtocolError
from .fs import write_json, canonical, now

#: Driver stderr tail retained per run (bounded failure evidence).
DIAGNOSTICS_TAIL_BYTES = 256 * 1024
#: Raw CLI event-stream bound per invocation (evidence, not a bill).
EVENT_STREAM_BYTES = 4 * 1024 * 1024
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
    double-charging (see Budget.take). Enforces `requests_per_run` uniformly.
    """
    if context.finished is not None:
        raise ProtocolError("No inference after the work unit is sealed")
    if context.cancelled():
        raise Cancelled("Run cancelled before admission")
    if getattr(context, "invocation_admitted", False):
        return {"admitted": True}
    if context.request_count >= context.config.limits.requests_per_run:
        raise LimitExceeded("Per-run provider request budget exhausted")
    project_name = getattr(getattr(context, "project", None), "name", "")
    try:
        Budget(context.config.data / "budget", context.config.limits.daily_requests,
               context.config.limits.retention_days,
               context.config.limits.shared_daily_requests).take(
                   f"{context.run_dir.name}:invocation", project=project_name)
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
    context.request_count += 1
    context.invocation_admitted = True
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
