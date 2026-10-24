"""Host-only durable writer continuation admission; no yield trigger.

Publication proves the intent; started.json is its single-use execution fence.
Recovery reports interruption rather than replaying a provider session.
"""
from __future__ import annotations

import contextlib
import re
import stat
import time
from datetime import datetime

from .errors import Denied, Busy
from .fs import DIGEST, ID, canonical, digest, lock, publish_pointer, read_json, write_json, now, mkdir, sync_dir

MAX_PENDING = 64
POINTER_BYTES = 64 * 1024
RUN_BYTES = 1024 * 1024
_RUN = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = {"role", "source_run", "snapshot", "successor", "state"}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _successor(role, source_run, snapshot):
    return digest(canonical(["writer-continuation-v1", role, source_run, snapshot]))[:32]


def entries(pointer):
    """Validate optional version-1 pointer metadata (64 rows, fixed fields).

    Local coordination data, never model input. Unknown shapes fail closed;
    absent metadata is the old, empty registry. Returned rows are copies.
    """
    if not isinstance(pointer, dict):
        raise Denied("Invalid continuation publication pointer")
    if "continuations" not in pointer:
        return []
    value = pointer["continuations"]
    if (not isinstance(value, dict) or set(value) != {"version", "entries"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["entries"], list) or len(value["entries"]) > MAX_PENDING):
        raise Denied("Invalid continuation registry")
    roles, sources, successors = set(), set(), set()
    result = []
    for row in value["entries"]:
        if (not isinstance(row, dict) or set(row) != _FIELDS
                or not _matches(ID, row["role"])
                or not _matches(_RUN, row["source_run"])
                or not _matches(DIGEST, row["snapshot"])
                or not _matches(_RUN, row["successor"])
                or row["successor"] != _successor(row["role"], row["source_run"], row["snapshot"])
                or row["state"] not in ("prepared", "committed", "reserved")
                or row["role"] in roles or row["source_run"] in sources
                or row["successor"] in successors):
            raise Denied("Invalid continuation entry")
        roles.add(row["role"])
        sources.add(row["source_run"])
        successors.add(row["successor"])
        result.append(dict(row))
    return result


def pointer_at(root):
    """Bounded, link-safe read; the publication lock serializes mutations."""
    pointer = read_json(root / "current.json", {}, max_bytes=POINTER_BYTES)
    entries(pointer)
    return pointer


def _with_entries(pointer, rows):
    return {**pointer, "continuations": {"version": 1, "entries": rows}}


def publication_metadata(pointer, snapshot):
    """Called only inside Snapshots.publish's publication transaction.

    Commit proof travels in the SAME atomic pointer replacement as the seal,
    not in a post-publication sidecar. Carry it across every later publication,
    even after the bounded history index drops the source snapshot.
    """
    rows = entries(pointer)
    for row in rows:
        if row["snapshot"] == snapshot["id"]:
            if snapshot.get("run") != row["source_run"] or snapshot.get("outcome") != "continue":
                raise Denied("Continuation seal binding mismatch")
            if row["state"] == "prepared":
                row["state"] = "committed"
    return {"continuations": {"version": 1, "entries": rows}} if rows else {}


def prepare(store, *, role, source_run, snapshot):
    """Prepare one host-requested yield, before the writer publishes its seal.

    Caller must own the writer's role/workspace exclusions (the future yield
    integration point). Lock order is execution exclusions -> publication.
    The exact immutable snapshot must be a new `continue` seal of that run,
    based on the current head and on its started.json input. Failed/CAS-losing
    publication leaves a prepared intent, never an admissible continuation.
    Repeated preparation of the same intent is a no-op, including after commit.
    One pending entry per role and at most 64 per project; overflow is Busy.
    """
    if not _matches(ID, role) or not _matches(_RUN, source_run) or not _matches(DIGEST, snapshot):
        raise Denied("Invalid continuation identity")
    with lock(store.root / "locks" / "publication.lock"):
        pointer = pointer_at(store.root)
        rows = entries(pointer)
        wanted = {"role": role, "source_run": source_run, "snapshot": snapshot,
                  "successor": _successor(role, source_run, snapshot), "state": "prepared"}
        for row in rows:
            if row["role"] == role:
                if all(row[key] == wanted[key] for key in _FIELDS - {"state"}):
                    return row
                raise Busy("Writer already has a pending continuation")
        if len(rows) >= MAX_PENDING:
            raise Busy("Continuation registry is full")
        record = store.get(snapshot)
        started = read_json(store.root / "runs" / source_run / "started.json", {}, max_bytes=RUN_BYTES)
        history = store._history_ids()
        current = history[-1] if history else None
        if (not current or snapshot == current or record.get("parent") != current
                or record.get("run") != source_run or record.get("outcome") != "continue"
                or not isinstance(started, dict) or started.get("run") != source_run
                or started.get("role") != role or started.get("snapshot") != current):
            raise Denied("Continuation requires a new writer continue seal")
        publish_pointer(store.root, _with_entries(pointer, [*rows, wanted]), name="current.json")
        return wanted


def pending(store, role):
    """Read one prepared/committed/reserved intent without mutating it."""
    return next((row for row in entries(pointer_at(store.root)) if row["role"] == role), None)


@contextlib.contextmanager
def reservation(engine, project, role_name, *, wait=False, enqueue=False,
                observation_sink=None, expected=None, owner_stack=None):
    """Reserve the same successor through normal FIFO, under execution locks.

    Fresh stop/authority/budget/disk checks and ordinary queue capacity apply
    through Engine._admit. A refusal changes no intent. Never hold publication
    while waiting for admission. Re-read after admission so racing wakes all
    observe one identity and the then-current tree, not the yield's old tree.
    Yield None for no matching committed intent (a stale wake). Otherwise
    hold all execution exclusions until Engine acceptance/settlement completes.
    Retries reserve the same ID. No registry mutation precedes admission.
    """
    role = engine.config.roles.get(role_name)
    if role_name not in project.roles or role is None or role.workspace != "write":
        raise Denied("Continuation requires an enabled writer")
    row = pending(project.snapshots, role_name)
    if (row is None or row["state"] == "prepared"
            or (expected is not None and identity(row) != identity(expected))):
        yield None
        return
    with contextlib.ExitStack() as stack:
        # Selector waits may release admission locks inside Engine.run. Keep
        # identity ownership for that whole invocation, not just its slot
        # lease, so another wake cannot mistake a live start for a crash.
        owner = owner_stack if owner_stack is not None else stack
        owner.enter_context(lock(project.root / "locks" / f"continuation-{role_name}.lock",
                                 blocking=False))
        engine._check_admission(project)
        elapsed = engine._admit(project, role_name, role, stack, wait=wait,
                                enqueue=enqueue, observation_sink=observation_sink)
        store = project.snapshots
        with lock(store.root / "locks" / "publication.lock"):
            pointer = pointer_at(store.root)
            rows = entries(pointer)
            row = next((r for r in rows if r["role"] == role_name), None)
            if (row is None or row["state"] == "prepared"
                    or (expected is not None and identity(row) != identity(expected))):
                descriptor = None
            else:
                seal = store.get(row["snapshot"])
                if seal.get("run") != row["source_run"] or seal.get("outcome") != "continue":
                    raise Denied("Continuation seal binding mismatch")
                current = store.get()
                # Validate the current history generation as well as manifest.
                if store._history_ids()[-1] != current["id"]:
                    raise Denied("Continuation publication pointer mismatch")
                if row["state"] != "reserved":
                    row["state"] = "reserved"
                    publish_pointer(store.root, _with_entries(pointer, rows), name="current.json")
                descriptor = {**row, "current_snapshot": current["id"],
                              "admission_elapsed_seconds": elapsed}
        yield descriptor


def identity(row):
    """Stable binding copied into started.json; reservation state is not identity."""
    return {key: row[key] for key in ("role", "source_run", "snapshot", "successor")}


def ready(store, role_name):
    row = pending(store, role_name)
    return row if row is not None and row["state"] in ("committed", "reserved") else None


def _started(store, row):
    directory = store.root / "runs" / row["successor"]
    if directory.exists() or directory.is_symlink():
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise Denied("Invalid continuation run directory")
    started = read_json(directory / "started.json", None, max_bytes=RUN_BYTES)
    if started is not None:
        if (not isinstance(started, dict) or started.get("run") != row["successor"]
                or started.get("role") != row["role"]
                or started.get("continuation") != identity(row)
                or not _matches(DIGEST, started.get("snapshot"))
                or not _aware_time(started.get("started_at"))):
            raise Denied("Continuation start binding mismatch")
        store.get(started["snapshot"])
    return directory, started


def _aware_time(value):
    if not isinstance(value, str) or len(value) > 32:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _result_snapshot(store, result, started, row):
    """Bind result snapshot evidence to this successor's exact input tree."""
    snapshot_id = result.get("snapshot")
    if snapshot_id is None:
        raise Denied("Continuation result has no snapshot binding")
    if not _matches(DIGEST, snapshot_id):
        raise Denied("Invalid continuation result snapshot")
    if snapshot_id == started["snapshot"]:
        if result.get("finish") is not None:
            raise Denied("Continuation output snapshot is missing")
        return
    snapshot = store.get(snapshot_id)
    if (snapshot.get("run") != row["successor"]
            or snapshot.get("parent") != started["snapshot"]):
        raise Denied("Continuation output snapshot binding mismatch")
    finish = result.get("finish")
    if isinstance(finish, dict) and snapshot.get("outcome") != finish.get("outcome"):
        raise Denied("Continuation output outcome mismatch")


def _terminal(store, directory, row, started):
    result = read_json(directory / "result.json", None, max_bytes=RUN_BYTES)
    error = read_json(directory / "error.json", None, max_bytes=RUN_BYTES)
    if result is not None:
        if (not isinstance(result, dict) or result.get("run") != row["successor"]
                or result.get("role") != row["role"]):
            raise Denied("Continuation result binding mismatch")
        if ("continuation" in result and result["continuation"] != identity(row)):
            raise Denied("Continuation result identity mismatch")
        if result.get("status") == "prepared":
            _result_snapshot(store, result, started, row)
            if error is not None:
                _validate_lifecycle(row, started, None, error)
            return None
        if result.get("status") not in ("completed", "waiting", "bounded", "interrupted"):
            raise Denied("Invalid continuation result state")
        _result_snapshot(store, result, started, row)
    if result is None and error is None:
        return None
    _validate_lifecycle(row, started, result, error)
    return result


def _validate_lifecycle(row, started, result, error):
    """Reuse the existing bounded run-envelope validator before settlement."""
    from .storage_runs import _terminal as valid_run_terminal
    records = {"result.json": result, "error.json": error}
    if not valid_run_terminal(row["successor"], started, records, time.time()):
        raise Denied("Invalid continuation terminal evidence")


def _unstarted_directory(directory):
    # atomic_write may leave an uncommitted temporary on process death. It is
    # not a start marker; leave it untouched. Never reuse other unbound files.
    if directory.exists():
        for child in directory.iterdir():
            info = child.lstat()
            if (not child.name.startswith(".new-") or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1):
                raise Denied("Continuation run has files without a durable start")


def recover(store, row):
    """Under execution exclusions, reconcile a start, never dispatch it twice.

    Empty directories left before start may be reused; other unbound files
    fail closed. A prepared result is not terminal/publication proof. Recovery
    records interruption without attempting publication or provider replay.
    The registry remains pinned if any evidence write or settlement fails.
    """
    directory, started = _started(store, row)
    if started is None:
        _unstarted_directory(directory)
        return None
    result = _terminal(store, directory, row, started)
    if result is None:
        previous = read_json(directory / "result.json", None, max_bytes=RUN_BYTES)
        error = read_json(directory / "error.json", None, max_bytes=RUN_BYTES)
        if error is not None:
            # Preserve any runtime error envelope exactly; result.json is the
            # lifecycle index used by readers and must contain every error field.
            result = {**(previous if isinstance(previous, dict) else {}), **error,
                      "snapshot": (previous.get("snapshot") if isinstance(previous, dict)
                                   and previous.get("snapshot") is not None else started["snapshot"]),
                      "continuation": identity(row)}
        else:
            result = {**(previous if isinstance(previous, dict) else {}),
                      "run": row["successor"], "role": row["role"],
                      "status": "interrupted", "finished_at": now(),
                      "snapshot": (previous.get("snapshot") if isinstance(previous, dict)
                                   and previous.get("snapshot") is not None else started["snapshot"]),
                      "continuation": identity(row),
                      "error": "Continuation started without a terminal result; execution not replayed"}
        _result_snapshot(store, result, started, row)
        _validate_lifecycle(row, started, result, error)
        write_json(directory / "result.json", result)
    active = store.root / "active" / (row["role"] + ".json")
    active_record = read_json(active, {}, max_bytes=RUN_BYTES)
    if isinstance(active_record, dict) and active_record.get("run") == row["successor"]:
        active.unlink()
        sync_dir(active.parent)
    settle(store, row)
    return result


def start(store, row, started):
    """Durable create-only acceptance before active pointer/model execution."""
    directory, previous = _started(store, row)
    if previous is not None:
        raise Denied("Continuation already started")
    _unstarted_directory(directory)
    if (not isinstance(started, dict) or started.get("run") != row["successor"]
            or started.get("role") != row["role"]
            or started.get("snapshot") != row["current_snapshot"]
            or not _aware_time(started.get("started_at"))):
        raise Denied("Continuation input binding mismatch")
    mkdir(directory)
    sync_dir(directory.parent)
    write_json(directory / "started.json", {**started, "continuation": identity(row)}, exclusive=True)


def settle(store, row):
    """Retire only an exact durably started terminal admission, idempotently.

    Caller holds execution exclusions. Keep the row throughout execution and
    until this atomic replacement, including across errors and later seals.
    """
    directory, started = _started(store, row)
    if started is None or _terminal(store, directory, row, started) is None:
        return False
    with lock(store.root / "locks" / "publication.lock"):
        pointer = pointer_at(store.root)
        rows = entries(pointer)
        current = next((r for r in rows if r["role"] == row["role"]), None)
        if current is None:
            return True
        if identity(current) != identity(row) or current["state"] == "prepared":
            raise Denied("Continuation settlement binding mismatch")
        publish_pointer(store.root, _with_entries(pointer, [r for r in rows if r != current]),
                        name="current.json")
    return True
