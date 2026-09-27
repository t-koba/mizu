"""Generic inference-engine registry. Mechanism owns routing; policy owns selection.

Profiles name an `engine` (`pi`, `codex` or `claude`; default `pi`). The Engine
resolves one driver per profile through this registry, so every engine honors
the same work-unit contract: empty control directory, policy-verbatim system
prompt, capability-filtered `mizu_*` tools only, shared budget/slot/deadline
admission, sealed finish, usage records and policy-bound sessions.

Counting units differ by engine and are reported honestly, not normalized:
Pi counts provider requests via its admission hook; `codex`/`claude` count
driver invocations (one per work unit). The shared `daily_requests` budget
still gates every invocation; it is not currency (see ADR-005).
"""
from __future__ import annotations

from .budget import Budget
from .errors import Cancelled, ConfigError, LimitExceeded, ProtocolError
from .fs import write_json

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
    """Return the validated engine for a configured profile (default `pi`)."""
    return config.engine(profile)


def command_for(config, engine: str) -> tuple[str, ...]:
    """Return the operator-owned trusted argv for an engine."""
    commands = {"pi": config.pi_command, "codex": config.codex_command,
                "claude": config.claude_command}
    try:
        return commands[engine]
    except KeyError:
        raise ConfigError(f"Unknown inference engine: {engine}") from None


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


def admit_invocation(context) -> dict:
    """Admit one non-Pi driver invocation against the shared daily budget.

    Idempotent per run directory: a retry records the same admission without
    double-charging (see Budget.take). Enforces `requests_per_run` uniformly.
    """
    if getattr(context, "invocation_admitted", False):
        return {"admitted": True}
    if context.request_count >= context.config.limits.requests_per_run:
        raise LimitExceeded("Per-run provider request budget exhausted")
    Budget(context.config.data / "budget", context.config.limits.daily_requests).take(
        f"{context.run_dir.name}:invocation")
    context.request_count += 1
    context.invocation_admitted = True
    write_json(context.run_dir / "admission.json", {"requests": context.request_count})
    return {"admitted": True}


def settle_invocation(context, result, parsed, *, engine: str) -> None:
    """Map a CLI runner outcome to Cancelled/LimitExceeded/ProtocolError.

    Shared by the codex/claude drivers so every engine honors the same
    completion contract: cancellation, deadline, event bound, clean exit,
    parsed errors, then a sealed finish.
    """
    if context.cancelled() or result.reason == "cancelled":
        raise Cancelled("Run cancelled")
    if result.reason == "timeout":
        raise LimitExceeded(f"{engine} run deadline exceeded")
    if result.reason == "output_limit":
        raise LimitExceeded(f"{engine} event stream exceeded its bound")
    if result.exit_code != 0:
        raise ProtocolError(f"{engine} exited unsuccessfully: " + result.stderr[-ERROR_TAIL_CHARS:])
    if parsed["errors"]:
        raise ProtocolError("; ".join(parsed["errors"])[-ERROR_TAIL_CHARS:])
    if context.finished is None:
        raise ProtocolError("Agent settled without calling mizu_finish")
