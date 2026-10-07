"""SQLite durable execution store: persisted turns/tasks and recovery.

Mechanism: a small local SQLite file isolated by project, role and
compatible session identity under data_dir. Every policy choice
(backend, location, retention, recovery) is explicit configuration;
this module only enforces the configured bounds and refuses to revive
obsolete authority.

Trust: local operator state only, never model input. Failure: refuses
(resume mismatch, unknown completion) rather than guessing.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from .errors import ConfigError
from .fs import canonical, digest, mkdir

BACKENDS = ("sqlite",)
RESUME_MODES = ("compatible", "fresh")
MAX_TURNS = 1024
MAX_PAYLOAD_BYTES = 262144

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _safe_name(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConfigError(f"Durable store {field} must be a bounded identifier")
    return value


def store_path_for(data_dir: Path, project: str, role: str, session_key: str) -> Path:
    """Isolated store path under data_dir/durable/<project>/<role>/<key>."""
    return (Path(data_dir) / "durable" / _safe_name(project, "project")
            / _safe_name(role, "role") / _safe_name(session_key, "session")
            / "store.sqlite")


#: Suffix matching an older conversation generation of one session key.
_GENERATION = re.compile(r"-g(\d{1,10})\Z")


#: Run/turn states that still carry unresolved recovery or side-effect
#: evidence; a generation holding any of these is never pruned.
_UNRESOLVED_RUN = ("active",)
_UNRESOLVED_TURN = ("started", "unknown")


def generation_terminal(directory: Path) -> bool:
    """True only when a generation directory holds verified terminal state.

    Schema: ``directory`` is one ``durable/<project>/<role>/<key>`` store
    directory. Bounds: read-only inspection, no writes, no WAL creation.
    Trust: local operator state only. Failure: never raises; anything that
    cannot be verified (missing/unreadable store, corrupt database,
    unexpected shape, an active run, or a started/unknown turn) reads as
    not terminal so the caller retains the directory for the operator.
    """
    try:
        if directory.is_symlink() or not directory.is_dir():
            return False
        store = directory / "store.sqlite"
        if store.is_symlink() or not store.is_file():
            return False
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            runs = conn.execute("SELECT status FROM durable_runs").fetchall()
            for (status,) in runs:
                if not isinstance(status, str) or status in _UNRESOLVED_RUN:
                    return False
            states = ",".join(f"'{name}'" for name in _UNRESOLVED_TURN)
            pending = conn.execute(
                f"SELECT COUNT(*) FROM durable_turns WHERE state IN ({states})").fetchone()
            if pending is None or pending[0] != 0:
                return False
            return True
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    except (OSError, ValueError, sqlite3.Error):
        return False


def prune_generations(data_dir: Path, *, project: str, role: str,
                      base_key: str, generation: int) -> list[str]:
    """Remove verified-terminal older-generation stores of one session key.

    Schema: under ``durable/<project>/<role>/``, directories named
    exactly ``base_key`` (generation zero) or ``base_key-g<N>`` with
    ``N`` below ``generation`` hold conversations a rotation already
    abandoned. Rotation only freshens context: a directory is removed
    solely when its store verifies as terminal (every run settled, no
    started/unknown turn). Active, unknown-completion, corrupt, or
    otherwise uninspectable generations are retained for the operator;
    time-based reaping stays the separate per-store
    ``retention_candidates``/``prune`` policy, never this path.
    Bounds: one session key, older generations only, whole directories;
    symlinks and non-directories are never touched. Trust: local
    operator state only. Failure: a missing role directory is nothing
    to prune; removal I/O errors propagate; unverifiable candidates are
    retained, not forced. Returns the removed names.
    """
    if type(generation) is not int or generation < 1:
        raise ConfigError("Durable prune generation must be a positive integer")
    base = _safe_name(base_key, "session")
    role_dir = (Path(data_dir) / "durable" / _safe_name(project, "project")
                / _safe_name(role, "role"))
    removed = []
    try:
        children = list(role_dir.iterdir())
    except FileNotFoundError:
        return []
    for child in children:
        name = child.name
        if name == base:
            old = 0
        else:
            if not name.startswith(base + "-g"):
                continue
            match = _GENERATION.fullmatch(name[len(base):])
            if match is None:
                continue
            old = int(match.group(1))
        if old >= generation:
            continue
        if child.is_symlink() or not child.is_dir():
            continue
        if not generation_terminal(child):
            continue
        try:
            shutil.rmtree(child)
        except FileNotFoundError:
            continue
        removed.append(name)
    return sorted(removed)


def grant_digest(*, policy_text: str, capabilities: tuple, model: dict,
                 adapter_digest_value: str, options_digest: str) -> str:
    """Authority fingerprint that resume binds to.

    A store path alone never authorizes resuming: the saved grant must
    equal the current grant or resume is refused (fresh start required).
    """
    return digest(canonical({
        "policy": digest(policy_text.encode()),
        "capabilities": sorted(capabilities),
        "provider": model.get("provider", ""),
        "model": model.get("model", ""),
        "adapter": adapter_digest_value,
        "options": options_digest,
    }))


def open_store(path: Path) -> sqlite3.Connection:
    mkdir(path.parent)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS durable_runs (
        run_key TEXT PRIMARY KEY, session_key TEXT NOT NULL,
        project TEXT NOT NULL, role TEXT NOT NULL,
        grant_digest TEXT NOT NULL, status TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        result_json TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS durable_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL,
        seq INTEGER NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, result_json TEXT,
        state TEXT NOT NULL,
        UNIQUE(run_key, seq))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_run ON durable_turns(run_key)")
    conn.commit()
    return conn


def _bounded_json(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        raise ConfigError("Durable turn payload exceeds byte bound")
    return raw


def begin_run(conn: sqlite3.Connection, *, run_key: str, session_key: str,
              project: str, role: str, grant: str) -> dict:
    """Idempotent run creation: duplicate submission returns the saved row."""
    now = time.time()
    row = conn.execute("SELECT run_key, status, grant_digest, result_json FROM durable_runs WHERE run_key=?",
                       (run_key,)).fetchone()
    if row is not None:
        if row[2] != grant:
            raise ConfigError("Durable resume refused: current authority differs from persisted grant")
        return {"run_key": row[0], "status": row[1], "resumed": True,
                "result": json.loads(row[3]) if row[3] else None}
    conn.execute("INSERT INTO durable_runs(run_key, session_key, project, role, grant_digest, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                 (run_key, session_key, project, role, grant, "active", now, now))
    conn.commit()
    return {"run_key": run_key, "status": "active", "resumed": False, "result": None}


def check_grant(conn: sqlite3.Connection, run_key: str, grant: str) -> None:
    row = conn.execute("SELECT grant_digest FROM durable_runs WHERE run_key=?", (run_key,)).fetchone()
    if row is None:
        raise ConfigError("Unknown durable run; resume cannot invent history")
    if row[0] != grant:
        raise ConfigError("Durable resume refused: current authority differs from persisted grant")


def record_turn(conn: sqlite3.Connection, run_key: str, seq: int, kind: str, payload: dict) -> dict:
    """Record one turn/task; duplicate seq returns the saved entry (no double count)."""
    if kind not in ("prompt", "tool", "model", "finish"):
        raise ConfigError("Unknown durable turn kind")
    if type(seq) is not int or seq < 1:
        raise ConfigError("Durable sequence must be a positive integer")
    existing = conn.execute("SELECT payload_json, result_json, state FROM durable_turns WHERE run_key=? AND seq=?",
                            (run_key, seq)).fetchone()
    if existing is not None:
        return {"duplicate": True, "payload": json.loads(existing[0]),
                "result": json.loads(existing[1]) if existing[1] else None,
                "state": existing[2]}
    conn.execute("INSERT INTO durable_turns(run_key, seq, kind, payload_json, state) VALUES(?,?,?,?,?)",
                 (run_key, seq, kind, _bounded_json(payload), "started"))
    conn.execute("UPDATE durable_runs SET updated_at=?, status='active' WHERE run_key=?", (time.time(), run_key))
    conn.commit()
    return {"duplicate": False, "state": "started"}


def complete_turn(conn: sqlite3.Connection, run_key: str, seq: int, result: dict, state: str = "completed") -> None:
    if state not in ("completed", "unknown", "cancelled"):
        raise ConfigError("Unknown durable turn state")
    # Interrupted side effects are marked unknown, never assumed exactly-once.
    conn.execute("UPDATE durable_turns SET result_json=?, state=? WHERE run_key=? AND seq=?",
                 (_bounded_json(result), state, run_key, seq))
    conn.execute("UPDATE durable_runs SET updated_at=? WHERE run_key=?", (time.time(), run_key))
    conn.commit()


def complete_run(conn: sqlite3.Connection, run_key: str, result: dict, status: str = "completed") -> None:
    if status not in ("completed", "interrupted", "cancelled"):
        raise ConfigError("Unknown durable run status")
    conn.execute("UPDATE durable_runs SET status=?, updated_at=?, result_json=? WHERE run_key=?",
                 (status, time.time(), _bounded_json(result), run_key))
    conn.commit()


def load_run(conn: sqlite3.Connection, run_key: str) -> dict | None:
    row = conn.execute("SELECT run_key, session_key, project, role, grant_digest, status, result_json FROM durable_runs WHERE run_key=?",
                       (run_key,)).fetchone()
    if row is None:
        return None
    turns = [{"seq": r[0], "kind": r[1], "state": r[2],
              "payload": json.loads(r[3]), "result": json.loads(r[4]) if r[4] else None}
             for r in conn.execute("SELECT seq, kind, state, payload_json, result_json FROM durable_turns WHERE run_key=? ORDER BY seq",
                                   (run_key,)).fetchall()]
    return {"run_key": row[0], "session_key": row[1], "project": row[2], "role": row[3],
            "grant_digest": row[4], "status": row[5],
            "result": json.loads(row[6]) if row[6] else None, "turns": turns}


def project_context(conn: sqlite3.Connection, run_key: str, max_bytes: int = 65536) -> dict:
    """Bounded projection for dispatch: counts and terminal states only.

    Model-visible context stays a separate bounded projection; the full
    persisted payload is never inlined.
    """
    record = load_run(conn, run_key)
    if record is None:
        return {"run_key": run_key, "turns": 0}
    summary = {"run_key": run_key, "status": record["status"],
               "turns": len(record["turns"]),
               "states": {s: sum(1 for t in record["turns"] if t["state"] == s)
                          for s in ("started", "completed", "unknown", "cancelled")}}
    raw = json.dumps(summary).encode()
    if len(raw) > max_bytes:
        summary["truncated"] = True
    return summary


def retention_candidates(conn: sqlite3.Connection, *, retention_days: int, now: float | None = None) -> list[str]:
    """Terminal-store candidates older than retention_days.

    Unfinished (active) and unknown-completion runs are always retained;
    only terminal completed/cancelled/interrupted runs with a recorded
    result are candidates. 0 disables time-based reaping.
    """
    if not retention_days:
        return []
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    rows = conn.execute("SELECT run_key FROM durable_runs WHERE status IN ('completed','cancelled','interrupted') AND updated_at < ?",
                        (cutoff,)).fetchall()
    # Reference protection: runs still carrying unfinished turns stay.
    kept = []
    for (key,) in rows:
        pending = conn.execute("SELECT COUNT(*) FROM durable_turns WHERE run_key=? AND state IN ('started','unknown')",
                               (key,)).fetchone()[0]
        if pending == 0:
            kept.append(key)
    return sorted(kept)


def prune(conn: sqlite3.Connection, run_keys: list[str], *, dry_run: bool = True) -> dict:
    """Explicit terminal-store cleanup with dry-run/accounting.

    Dry-run reports candidates and bytes without deleting. Apply deletes
    only the named terminal candidates.
    """
    total_bytes = 0
    for (payload,) in conn.execute("SELECT result_json FROM durable_runs").fetchall():
        total_bytes += len((payload or "").encode())
    if dry_run:
        return {"dry_run": True, "candidates": sorted(run_keys), "store_bytes": total_bytes}
    for key in run_keys:
        conn.execute("DELETE FROM durable_turns WHERE run_key=?", (key,))
        conn.execute("DELETE FROM durable_runs WHERE run_key=?", (key,))
    conn.commit()
    return {"dry_run": False, "removed": sorted(run_keys), "store_bytes": total_bytes}
