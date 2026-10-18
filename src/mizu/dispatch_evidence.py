"""Best-effort local dispatch measurements, never admission or session policy."""
from __future__ import annotations

import time

from .fs import canonical, now, write_json
from .usage import attempt_usage

MAX_ATTEMPTS = 64
MAX_BYTES = 256 * 1024
MAX_MODELS = 16
MAX_TEXT = 256


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= MAX_TEXT else None


def _unknown(reason):
    return {"key": None, "generation": None, "prompt_mode": None,
            "rotation": None, "unknown_reason": reason}


def _save(context):
    state = context._dispatch_evidence
    # Strict JSON validation before the atomic, fsynced replacement. Retain a
    # tail, not an unbounded journal; an interrupted write leaves the old file.
    while len(canonical(state)) > MAX_BYTES:
        state["attempts"].pop(0)
        state["dropped_attempts"] += 1
    write_json(context.run_dir / "dispatch-attempts.json", state)


def capture_projection(context, profile, mode, key, rotation, directory):
    """Remember a peek only; it is not evidence of driver session selection."""
    try:
        from .engine_config import session_generation
        context._dispatch_projection = {
            "profile": profile, "mode": mode, "key": key,
            "generation": session_generation(directory) if directory else None,
            "rotation": _text((rotation or {}).get("reason"))}
    except Exception:
        context._dispatch_projection = None


def selected_session(context, profile, settings, path):
    """Observe session_record during a physical execute, not during a peek.

    This attests only the local content-bound selection, not provider state.
    Any mismatch keeps all session/prompt fields unknown, including selector
    fallback reusing a prompt projected for a different profile.
    """
    try:
        record = getattr(context, "_active_dispatch_evidence", None)
        if record is None:
            return
        projection = getattr(context, "_dispatch_projection", None)
        reason = None
        if record["profile"] != profile:
            reason = "selected_profile_mismatch"
        elif projection and projection["profile"] != profile:
            reason = "prompt_profile_mismatch"
        elif getattr(context, "ephemeral", False) or settings.get("session") != "persistent":
            reason = "nonpersistent_session"
        elif not projection:
            reason = "prompt_projection_unavailable"
        else:
            from .engine_config import session_generation
            generation = session_generation(path.parent)
            if (projection["key"] != path.parent.name
                    or projection["generation"] != generation):
                reason = "prompt_session_mismatch"
            else:
                record["session"] = {"key": path.parent.name, "generation": generation,
                                     "prompt_mode": ("continuation" if context.turn_continuation else projection["mode"]),
                                     "rotation": (None if context.turn_continuation else projection["rotation"]), "unknown_reason": None}
        if reason:
            record["session"] = _unknown(reason)
        _save(context)
    except Exception:
        # Measurement must never make a session selection fail.
        pass


def _facts(context, record):
    attempt = record["execution_attempt"]
    # Context.handle records usage/models under this same lock. Snapshot both
    # maps together so a late bridge event cannot mutate a dict during projection.
    with context.mutex:
        usages = [value for (index, _), value in context.runtime_usage.items() if index == attempt]
        models_for_attempt = [(dict(value) if isinstance(value, dict) else {})
                              for (index, _), value in context.runtime_models.items()
                              if index == attempt]
    record["usage"] = attempt_usage(usages, record["engine"])
    models = set()
    unknown = False
    model_observations = len(models_for_attempt)
    for value in models_for_attempt:
        provider, model = _text(value.get("provider")), _text(value.get("model"))
        unknown |= provider is None or model is None
        models.add((provider, model))
    ordered = sorted(models, key=lambda pair: (pair[0] or "", pair[1] or ""))
    record["models"] = [{"provider": p, "model": m} for p, m in ordered[:MAX_MODELS]]
    record["models_truncated"] = len(ordered) > MAX_MODELS
    record["models_unknown"] = unknown or not models or model_observations < len(usages)


def execute(context, driver, prompt, **kwargs):
    """One start/final record per physical call; original returns/errors prevail.

    The start is persisted before the driver, and local session selection is
    persisted when observed. A crash may leave status=started and no end;
    runtime-usage.json can still contain later observations. No retries, locks,
    prompt mutations or provider calls are introduced by evidence writes.
    """
    record = None
    origin = time.monotonic()
    try:
        profile = _text(kwargs.get("profile") or context.role.profile)
        try:
            engine = context.config.engine(profile) if profile else None
        except Exception:
            engine = None
        projection = getattr(context, "_dispatch_projection", None)
        reason = ("prompt_profile_mismatch" if projection and projection["profile"] != profile
                  else "driver_session_unobserved")
        record = {"execution_attempt": context.execution_attempt,
                  "run": context.run_dir.name, "parent_run": getattr(context, "parent_run", None),
                  "role": context.role.name, "profile": profile,
                  "profile_unknown_reason": None if profile else "selected_profile_unavailable",
                  "engine": engine, "engine_unknown_reason": None if engine else "selected_engine_unavailable",
                  "class": getattr(context, "dispatch_class", None) or (
                      "ephemeral" if getattr(context, "ephemeral", False) else "normal"),
                  "phase": "continuation" if context.turn_continuation else "initial",
                  "started_at": now(), "ended_at": None, "elapsed_seconds": None,
                  "status": "started", "session": _unknown(reason)}
        _facts(context, record)
        if not hasattr(context, "_dispatch_evidence"):
            context._dispatch_evidence = {"schema": 1, "dropped_attempts": 0, "attempts": []}
        state = context._dispatch_evidence
        state["attempts"].append(record)
        if len(state["attempts"]) > MAX_ATTEMPTS:
            state["attempts"].pop(0)
            state["dropped_attempts"] += 1
        context._active_dispatch_evidence = record
        _save(context)
    except Exception:
        pass
    status = "raised"
    try:
        result = driver.execute(context, prompt, **kwargs)
        status = "returned"
        return result
    finally:
        try:
            context._active_dispatch_evidence = None
            if record is not None:
                record.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - origin, 6), status=status)
                _facts(context, record)
                _save(context)
        except Exception:
            pass
