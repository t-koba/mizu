"""Best-effort local dispatch measurements and grant observations, never policy."""
from __future__ import annotations

import contextlib
import os
import stat
import time

from . import platform
from .fs import _same_file, canonical, loads_finite, now, safe_read, write_json
from .usage import attempt_usage

#: Fixed safety ceiling for persisted text labels; not a retention budget.
MAX_TEXT = 256

# Fixed on-disk integer envelope, not a retention policy.
MAX_ORDINAL = 2**63 - 1


def _regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400)


def _optional_read(directory, name, maximum):
    # safe_read intentionally folds missing paths into Denied. Only an absent
    # leaf before the read is optional; every refused/unstable read stays unsafe.
    try:
        (directory / name).lstat()
    except FileNotFoundError:
        return None
    return safe_read(directory, name, maximum)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _generation(directory):
    # Observation must not turn an unsafe/special generation file into a
    # blocking read. Match session_generation's schema, not its physical path.
    raw = _optional_read(directory, "generation.json", 128)
    if raw is None:
        return 0
    value = loads_finite(raw, object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or set(value) != {"generation"}
            or type(value["generation"]) is not int or not 0 <= value["generation"] <= 2**31
            or canonical(value) != raw):
        raise ValueError("Invalid observed session generation")
    return value["generation"]


def _dispatch_ordinal(directory, generation, *, reserve=True):
    """Best-effort reservation, never a wait/retry or an execution lock.

    One current-generation JSON counter and one stable two-byte lock witness
    live with the session. A poisoned witness is never repaired automatically:
    an unnumbered call must not disappear and let later counters bridge it.
    Rotation overwrites healthy counters; explicit session reset removes both.
    Portable path checks refuse links/reparse points, without a TOCTOU claim.
    """
    fd = None
    locked = False
    try:
        # Trust the configured project/data root, not managed session components.
        for parent in (directory, directory.parent, directory.parent.parent):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None
        path = directory / "dispatch-order.lock"
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            if os.write(fd, b"00") != 2:
                raise OSError("Could not initialize dispatch-order witness")
        except FileExistsError:
            if not _regular(path.lstat()):
                return None
            fd = os.open(path, flags)
        info = os.fstat(fd)
        if not _regular(info) or not _same_file(info, path.lstat()):
            return None
        if not reserve:
            raise ValueError("Unsequenced selection")
        os.lseek(fd, 0, os.SEEK_SET)
        locked = platform.lock_fd(fd, blocking=False)
        os.lseek(fd, 0, os.SEEK_SET)
        if not locked or info.st_size != 2 or os.read(fd, 2) != b"00":
            raise ValueError("Unsafe dispatch order witness")
        if _generation(directory) != generation:
            raise ValueError("Session generation changed")
        raw = _optional_read(directory, "dispatch-order.json", 256)
        if raw is None:
            if not created:
                raise ValueError("Missing dispatch counter") from None
            previous = {"generation": generation, "ordinal": 0}
        else:
            if created:
                raise ValueError("Missing dispatch lock witness")
            previous = loads_finite(raw)
            if (not isinstance(previous, dict) or set(previous) != {"generation", "ordinal"}
                    or type(previous["generation"]) is not int
                    or not 0 <= previous["generation"] <= generation
                    or type(previous["ordinal"]) is not int
                    or not 1 <= previous["ordinal"] <= MAX_ORDINAL
                    or canonical(previous) != raw):
                raise ValueError("Invalid dispatch counter")
        ordinal = previous["ordinal"] + 1 if previous["generation"] == generation else 1
        if ordinal > MAX_ORDINAL:
            raise ValueError("Exhausted dispatch counter")
        write_json(directory / "dispatch-order.json", {"generation": generation, "ordinal": ordinal})
        os.lseek(fd, 0, os.SEEK_SET)
        final_info = os.fstat(fd)
        if (os.read(fd, 2) != b"00" or _generation(directory) != generation
                or not _regular(final_info) or not _same_file(final_info, path.lstat())):
            raise ValueError("Dispatch order became uncertain")
        return ordinal
    except Exception:
        # Also on nonblocking contention: a future reader must not bridge this
        # unnumbered call if its run evidence is later missing. Never clear it.
        if fd is not None:
            with contextlib.suppress(Exception):
                # Byte zero is the Windows lock range; the poison byte must
                # remain writable by a contending process on every platform.
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, b"!")
                os.fsync(fd)
        return None
    finally:
        if fd is not None:
            if locked:
                with contextlib.suppress(Exception):
                    platform.unlock_fd(fd)
            with contextlib.suppress(Exception):
                os.close(fd)


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= MAX_TEXT else None


def _unknown(reason):
    return {"key": None, "generation": None, "prompt_mode": None,
            "rotation": None, "unknown_reason": reason}


def _save(context):
    state = context._dispatch_evidence
    # Strict JSON validation before the atomic, fsynced replacement. Retain a
    # tail, not an unbounded journal; an interrupted write leaves the old file.
    while len(canonical(state)) > context.config.dispatch_evidence.max_bytes:
        state["attempts"].pop(0)
        state["dropped_attempts"] += 1
    write_json(context.run_dir / "dispatch-attempts.json", state)


def capture_projection(context, profile, mode, key, rotation, directory):
    """Remember a peek only; it is not evidence of driver session selection."""
    try:
        context._dispatch_projection = {
            "profile": profile, "mode": mode, "key": key,
            "generation": _generation(directory) if directory else None,
            "rotation": _text((rotation or {}).get("reason"))}
    except Exception:
        context._dispatch_projection = None


def selected_session(context, profile, settings, path):
    """Observe session_record during a physical execute, not during a peek.

    This attests only the local content-bound selection, not provider state.
    Any mismatch keeps all session/prompt fields unknown, including selector
    fallback reusing a prompt projected for a different profile.
    """
    persistent = False
    try:
        persistent = (not getattr(context, "ephemeral", False)
                      and settings.get("session") == "persistent")
        dispatching = bool(getattr(context, "_dispatch_in_progress", False))
        if not dispatching:
            return
        record = getattr(context, "_active_dispatch_evidence", None)
        if record is None:
            if dispatching and persistent:
                try:
                    _dispatch_ordinal(path.parent, _generation(path.parent))
                except Exception:
                    _dispatch_ordinal(path.parent, None, reserve=False)
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
            generation = _generation(path.parent)
            if (projection["key"] != path.parent.name
                    or projection["generation"] != generation):
                reason = "prompt_session_mismatch"
            else:
                previous = record["session"]
                if previous["key"] is not None and (previous["key"], previous["generation"]) != (path.parent.name, generation):
                    reason = "selected_session_changed"
                from .unit_prompts import broker_grant
                record["broker_grant"] = broker_grant(context)
                record["session"] = {"key": path.parent.name, "generation": generation,
                                     "prompt_mode": ("continuation" if context.turn_continuation else projection["mode"]),
                                     "rotation": (None if context.turn_continuation else projection["rotation"]), "unknown_reason": None}
        if reason:
            record["session"] = _unknown(reason)
            if dispatching and persistent:
                try:
                    # Consume a real sequence position even though attribution
                    # is rejected; if this run file disappears, the later gap
                    # prevents it from being bridged.
                    record["dispatch_ordinal"] = _dispatch_ordinal(path.parent, _generation(path.parent))
                except Exception:
                    record["dispatch_ordinal"] = _dispatch_ordinal(path.parent, None, reserve=False)
        elif dispatching and persistent and record["session"]["key"] is not None:
            if record.get("dispatch_ordinal") is None:
                record["dispatch_ordinal"] = _dispatch_ordinal(path.parent, generation)
        _save(context)
    except Exception:
        # Even an unavailable observation must not leave future ordinals free
        # to bridge this call once its unknown/missing run record is evicted.
        if (getattr(context, "_dispatch_in_progress", False) and persistent):
            try:
                _dispatch_ordinal(path.parent, _generation(path.parent))
            except Exception:
                _dispatch_ordinal(path.parent, None, reserve=False)
        # Measurement must never make a session selection fail.


def _facts(context, record):
    policy = context.config.dispatch_evidence
    attempt = record["execution_attempt"]
    # Context.handle records usage/models under this same lock. Snapshot both
    # maps together so a late bridge event cannot mutate a dict during projection.
    with context.mutex:
        usages = [value for (index, _), value in context.runtime_usage.items() if index == attempt]
        models_for_attempt = [(dict(value) if isinstance(value, dict) else {})
                              for (index, _), value in context.runtime_models.items()
                              if index == attempt]
    record["usage"] = attempt_usage(usages, record["engine"], unknown_keys=policy.unknown_keys)
    models = set()
    unknown = False
    model_observations = len(models_for_attempt)
    for value in models_for_attempt:
        provider, model = _text(value.get("provider")), _text(value.get("model"))
        unknown |= provider is None or model is None
        models.add((provider, model))
    ordered = sorted(models, key=lambda pair: (pair[0] or "", pair[1] or ""))
    record["models"] = [{"provider": p, "model": m} for p, m in ordered[:policy.max_models]]
    record["models_truncated"] = len(ordered) > policy.max_models
    record["models_unknown"] = unknown or not models or model_observations < len(usages)


def execute(context, driver, prompt, **kwargs):
    """One start/final record per physical call; original returns/errors prevail.

    The start is persisted before the driver, and local session selection is
    persisted when observed. A crash may leave status=started and no end;
    runtime-usage.json can still contain later observations. Only a nonblocking
    counter lock is attempted after session attestation; no retries, execution locks,
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
                  "status": "started", "session": _unknown(reason), "dispatch_ordinal": None}
        _facts(context, record)
        if not hasattr(context, "_dispatch_evidence"):
            context._dispatch_evidence = {"schema": 1, "dropped_attempts": 0, "attempts": []}
        state = context._dispatch_evidence
        state["attempts"].append(record)
        while len(state["attempts"]) > context.config.dispatch_evidence.max_attempts:
            state["attempts"].pop(0)
            state["dropped_attempts"] += 1
        context._active_dispatch_evidence = record
        _save(context)
    except Exception:
        pass
    status = "raised"
    context._dispatch_in_progress = True
    try:
        result = driver.execute(context, prompt, **kwargs)
        status = "returned"
        return result
    finally:
        try:
            context._dispatch_in_progress = False
            context._active_dispatch_evidence = None
            if record is not None:
                record.update(ended_at=now(), elapsed_seconds=round(time.monotonic() - origin, 6), status=status)
                _facts(context, record)
                _save(context)
        except Exception:
            pass
