"""Explicit, bounded relocation of pre-partition SharedRecords evidence."""
from __future__ import annotations

import os
from itertools import islice

from . import ci_waits, host_records, operator_records, proposal_waits
from .errors import Denied, MizuError
from .fs import atomic_write, digest, loads_finite, lock, sync_dir
from .insights import INSIGHT_DELIVERY_POLICIES
from .operator_record_index import DIRECTORY as OPERATOR_INDEX
from .shared_records import RECORD_BYTES, SharedRecords
from .storage import quiescent

SCAN_ENTRIES = 1000
_DIRECTORY = "shared-records"


def _validators(project):
    stores = (operator_records.store(project),
              SharedRecords(project.root, INSIGHT_DELIVERY_POLICIES),
              ci_waits.shared_store(project.work_items),
              proposal_waits.shared_store(project.work_items),
              host_records.store(project.root, project.snapshots, project.config.record_kinds))
    # Configured kinds can share a name with a built-in kind, but not its writer.
    return {(kind, writer): store for store in stores for kind, policy in store.policies.items()
            for writer in policy.writers}


def _move(shared, name, target, raw, expected, existing):
    dest = shared.root / target
    source = shared.root / name
    if existing is None:
        shared._parents(target, create=True)
        atomic_write(dest, raw, exclusive=True)
    else:
        # An identical copy may be the result of an interrupted write.
        sync_dir(dest.parent)
    written = shared._read(target, RECORD_BYTES)
    if written is None or digest(written) != expected:
        raise Denied("Canonical shared record failed read-back verification")
    if shared._read(name, RECORD_BYTES) != raw:
        raise Denied("Flat shared record changed before removal")
    # Persist the partition's directory entry before removing the flat one.
    sync_dir(source.parent)
    if dest.parent.name != "operator":
        # The operator catalog indexes foreign flat records but doesn't scan
        # foreign partitions. Invalidate this disposable ref before its last
        # catalog-visible source goes away, under operator-records.lock.
        ref = f"{OPERATOR_INDEX}/{source.name}"
        if shared._parents(ref):
            if shared._read(ref, RECORD_BYTES) is not None:
                (shared.root / ref).unlink()
            sync_dir(shared.root / OPERATOR_INDEX)
    source.unlink()


def _flat_names(shared):
    if not shared._parents(f"{_DIRECTORY}/probe"):
        return []
    # Freeze only bounded names, before apply can mutate the directory stream.
    with os.scandir(shared.root / _DIRECTORY) as entries:
        return [entry.name for entry in islice(entries, SCAN_ENTRIES)]


def _validate_flat(shared, name, validators):
    raw = shared._read(name, RECORD_BYTES)
    if raw is None:
        raise Denied("Flat shared record is unavailable")
    envelope = loads_finite(raw)
    store = shared
    if (type(envelope) is dict and isinstance(envelope.get("kind"), str)
            and isinstance(envelope.get("writer"), str)):
        store = validators.get((envelope["kind"], envelope["writer"]), shared)
    if store._validate(envelope) != raw:
        raise Denied("Noncanonical shared record bytes")
    record_id = envelope["id"]
    if name != shared._legacy_record_name(record_id):
        raise Denied("Flat shared record filename identity mismatch")
    return raw, shared.record_name(record_id)


def _scan(project, *, apply):
    shared = SharedRecords(project.root)
    report = dict(applied=apply, entries_examined=0, candidates=0, converted=0,
                  records=[], conflicts=[], errors=[], truncated=False)
    try:
        entries = _flat_names(shared)
    except (MizuError, OSError) as exc:
        report["errors"].append(dict(path=_DIRECTORY, error=_error(exc)))
        return dict(report, ok=False)
    report["entries_examined"] = len(entries)
    report["truncated"] = len(entries) == SCAN_ENTRIES
    validators = _validators(project) if entries else {}
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        name = f"{_DIRECTORY}/{entry}"
        try:
            raw, target = _validate_flat(shared, name, validators)
            existing = shared._read(target, RECORD_BYTES)
            if existing is not None and existing != raw:
                report["conflicts"].append(dict(flat=name, canonical=target))
                continue
            expected = digest(raw)
            report["candidates"] += 1
            report["records"].append(dict(flat=name, canonical=target, digest=expected))
            if apply:
                _move(shared, name, target, raw, expected, existing)
                report["converted"] += 1
                sync_dir(shared.root / _DIRECTORY)
        except (MizuError, OSError, ValueError, TypeError) as exc:
            report["errors"].append(dict(path=name, error=_error(exc)))
    return dict(report, ok=not (report["conflicts"] or report["errors"] or report["truncated"]))


def _error(exc):
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    if isinstance(exc, (ValueError, TypeError)):
        return "Invalid shared record JSON or fields"
    return str(exc)


def plan(project) -> dict:
    """Preview eligible records without writes or a project lock."""
    return _scan(project, apply=False)


def apply(project) -> dict:
    """Recompute under exclusion and convert each eligible record independently."""
    with (quiescent(project, snapshot_gc=True),
          lock(project.root / "locks/operator-records.lock", blocking=False)):
        return _scan(project, apply=True)
