"""Best-effort local dispatch measurements and grant observations, never policy."""
from __future__ import annotations

import contextlib
import os
import secrets
import stat
import time
from types import SimpleNamespace

from . import platform
from .fs import _same_file, canonical, digest, loads_finite, now, safe_read, write_json
from .usage import attempt_usage

#: Fixed safety ceiling for persisted text labels; not a retention budget.
MAX_TEXT = 256

# Fixed on-disk integer envelope, not a retention policy.
MAX_ORDINAL = 2**63 - 1

# Fixed lowercase-hex encoding of one 128-bit dispatch-sequence identity.
SEQUENCE_ID_HEX = 32


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


class AdmissionSource:
    """Bounded config binding and read-only local session facts, never selection.

    Queue tickets carry only the binding hash. Peers must reproduce it from
    their own current configuration; no ticket supplies an identity or path.
    This source can attest only its Engine's project. Cross-project identity,
    selectors and Pi family indirection remain explicitly unknown.
    """

    def __init__(self, config, project, role, *, ephemeral=False):
        self.config, self.project, self.role = config, project, role
        self.ephemeral = ephemeral
        self.prepared = {}
        self.metadata = {"binding": self._prepare(role)["binding"]}

    def _prepare(self, role, *, refresh=False):
        if not refresh and role.name in self.prepared:
            return self.prepared[role.name]
        from .engine_config import conversation_identity, effective
        value = {"profile": None, "engine": None, "key": None,
                 "unknown_reason": "selector_not_resolved_at_admission"}
        # Fixed-schema hash only; never persist the config it binds.
        binding = {"role": role.name, "profile": role.profile, "selector": role.selector,
                   "workspace": role.workspace, "ephemeral": self.ephemeral}
        if not role.selector:
            settings = effective(self.config, role, role.profile)
            binding["settings"] = settings
            value.update(profile=role.profile, engine=settings["engine"])
            if self.ephemeral or settings["session"] != "persistent":
                value["unknown_reason"] = "nonpersistent_session"
            elif settings["engine"] not in ("pi", "pi-durable"):
                # Native identities bind the dispatch's pinned goal/policy.
                # Admission has no Context yet; do not perform unbounded
                # snapshot/policy reads or guess what dispatch will pin.
                value["unknown_reason"] = "session_identity_unavailable_at_admission"
            else:
                context = SimpleNamespace(config=self.config, role=role)
                identity = conversation_identity(context, settings)
                value.update(key=digest(canonical(identity)), unknown_reason=None)
                binding["identity"] = identity
        value["binding"] = digest(canonical(binding))
        self.prepared[role.name] = value
        return value

    def __call__(self, ticket, *, held):
        session = {"key": None, "generation": None, "unknown_reason": "resources_not_held"}
        result = {"profile": None, "engine": None, "session": session,
                  "unknown_reason": "unsafe_or_stale_identity"}
        resources = ticket["resources"]
        prefix = self.project.name + "/locks/"
        roles = [p[len(prefix) + 4:-5] for p in resources
                 if p.startswith(prefix + "run-") and p.endswith(".lock")]
        if len(roles) != 1 or any(not p.startswith(prefix) for p in resources):
            result["unknown_reason"] = "project_or_role_identity_unavailable"
            return result
        role = self.role if roles[0] == self.role.name else self.config.roles.get(roles[0])
        if role is None:
            return result
        expected = [prefix + f"run-{role.name}.lock"]
        if role.workspace == "write":
            expected.append(prefix + "workspace.lock")
        if resources != expected:
            return result
        value = self._prepare(role, refresh=not held)
        if ticket["evidence"] != {"binding": value["binding"]}:
            return result
        result.update(profile=value["profile"], engine=value["engine"], unknown_reason=value["unknown_reason"])
        if value["unknown_reason"] is not None:
            session["unknown_reason"] = value["unknown_reason"]
        elif held:
            directory = self.project.root / "sessions" / role.name / value["key"]
            # Refuse links, special directories and Pi family state. Resolving
            # a family can write/take a blocking lock; admission never does so.
            for parent in (directory.parent.parent, directory.parent, directory):
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Unsafe admission session path")
            if value["engine"] == "pi":
                for marker in (directory / "family-owner.json", directory.parent / "families-v2"):
                    try:
                        marker.lstat()
                    except FileNotFoundError:
                        continue
                    raise ValueError("Pi family identity unresolved at admission")
            session.update(key=value["key"], generation=_generation(directory), unknown_reason=None)
        return result


def _sequence_id(value):
    return (isinstance(value, str) and len(value) == SEQUENCE_ID_HEX
            and all(char in "0123456789abcdef" for char in value))


def _dispatch_reservation(directory, generation, *, reserve=True):
    """Best-effort ordinal and reset-distinct identity; never a wait/retry.

    One current-generation JSON counter and one stable two-byte lock witness
    live with the session. The counter identity survives rotation but is
    replaced when explicit reset removes the session state. A poisoned witness
    is never repaired automatically: an unnumbered call must not disappear and
    let later counters bridge it. Portable path checks refuse links/reparse
    points, without a TOCTOU claim.
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
            sequence_id = secrets.token_hex(SEQUENCE_ID_HEX // 2)
        else:
            if created:
                raise ValueError("Missing dispatch lock witness")
            previous = loads_finite(raw)
            if (not isinstance(previous, dict)
                    or set(previous) not in ({"generation", "ordinal"},
                                            {"generation", "ordinal", "sequence_id"})
                    or type(previous["generation"]) is not int
                    or not 0 <= previous["generation"] <= generation
                    or type(previous["ordinal"]) is not int
                    or not 1 <= previous["ordinal"] <= MAX_ORDINAL
                    or canonical(previous) != raw):
                raise ValueError("Invalid dispatch counter")
            if "sequence_id" in previous:
                sequence_id = previous["sequence_id"]
                if not _sequence_id(sequence_id):
                    raise ValueError("Invalid dispatch sequence identity")
            else:
                # Upgrade the old bounded counter in place. Historical attempts
                # lack this identity and therefore remain unsequenced.
                sequence_id = secrets.token_hex(SEQUENCE_ID_HEX // 2)
        ordinal = previous["ordinal"] + 1 if previous["generation"] == generation else 1
        if ordinal > MAX_ORDINAL:
            raise ValueError("Exhausted dispatch counter")
        write_json(directory / "dispatch-order.json",
                   {"generation": generation, "ordinal": ordinal, "sequence_id": sequence_id})
        os.lseek(fd, 0, os.SEEK_SET)
        final_info = os.fstat(fd)
        if (os.read(fd, 2) != b"00" or _generation(directory) != generation
                or not _regular(final_info) or not _same_file(final_info, path.lstat())):
            raise ValueError("Dispatch order became uncertain")
        return ordinal, sequence_id
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


def _dispatch_ordinal(directory, generation, *, reserve=True):
    reservation = _dispatch_reservation(directory, generation, reserve=reserve)
    return reservation[0] if reservation is not None else None


def _record_dispatch_sequence(record, directory, generation, *, reserve=True):
    reservation = _dispatch_reservation(directory, generation, reserve=reserve)
    record["dispatch_ordinal"], record["dispatch_sequence_id"] = reservation or (None, None)


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
                    _record_dispatch_sequence(record, path.parent, _generation(path.parent))
                except Exception:
                    _record_dispatch_sequence(record, path.parent, None, reserve=False)
        elif dispatching and persistent and record["session"]["key"] is not None:
            if record.get("dispatch_ordinal") is None:
                _record_dispatch_sequence(record, path.parent, generation)
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
        admission_id = getattr(context, "_admission_id", None)
        record = {"execution_attempt": context.execution_attempt,
                  "admission_id": admission_id if _sequence_id(admission_id) else None,
                  "run": context.run_dir.name, "parent_run": getattr(context, "parent_run", None),
                  "role": context.role.name, "profile": profile,
                  "profile_unknown_reason": None if profile else "selected_profile_unavailable",
                  "engine": engine, "engine_unknown_reason": None if engine else "selected_engine_unavailable",
                  "class": getattr(context, "dispatch_class", None) or (
                      "ephemeral" if getattr(context, "ephemeral", False) else "normal"),
                  "phase": "continuation" if context.turn_continuation else "initial",
                  "started_at": now(), "ended_at": None, "elapsed_seconds": None,
                  "status": "started", "session": _unknown(reason), "dispatch_ordinal": None,
                  "dispatch_sequence_id": None}
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
    # Credit the opening's queue content at the actual handoff boundary: the
    # prompt is about to cross into the driver. A failure before this point
    # (prompt construction, projection capture, admission, container setup)
    # leaves the wake intact for a later unit; a driver that received the
    # prompt and then fails has still been handed the content. Idempotent
    # across retries, and a measurement failure here never fails dispatch.
    handoff = getattr(context, "_opening_handoff", None)
    if handoff is not None:
        try:
            handoff()
        except Exception:
            pass
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
