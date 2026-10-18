"""Permanent decided insight storage; callers serialize writes with insights.lock.

DELETE journals and FULL synchronous commits keep the database self-contained at
quiescence (including backups). No deletion, migration, or listing API. Rows bind
complete normalized content to the decision and identity observed at archival.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import stat
from pathlib import Path

from .errors import Denied
from .fs import canonical, sync_dir

NAME = "insight-archive.sqlite3"


@contextlib.contextmanager
def _connect(root: Path, *, write: bool = False):
    path = root.resolve() / NAME
    # SQLite also opens its journal by name. Refuse unsafe pre-existing files.
    for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied("Unsafe insight archive path")
    if not path.exists() and not write:
        yield None
        return
    try:
        # mode=rw avoids creating a missing DB during reads; permits hot-journal
        # recovery after a killed writer. Connections never outlive an operation.
        with contextlib.closing(sqlite3.connect(path.as_uri() + ("?mode=rwc" if write else "?mode=rw"),
                                               uri=True)) as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise Denied("Insight archive requires DELETE journal mode")
            if write:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS insights ("
                           "id TEXT NOT NULL, rev INTEGER NOT NULL, record TEXT NOT NULL, "
                           "decision TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY (id, rev))")
                db.execute("CREATE INDEX IF NOT EXISTS insight_prefix ON insights (id COLLATE NOCASE)")
            yield db
    except sqlite3.Error as exc:
        raise Denied("Insight archive unavailable") from exc


def read(root: Path, insight_id: str) -> dict | None:
    with _connect(root) as db:
        if db is None:
            return None
        row = db.execute("SELECT record FROM insights WHERE id=? ORDER BY rev DESC LIMIT 1",
                         (insight_id,)).fetchone()
        return json.loads(row[0]) if row else None


def prefixes(root: Path, prefix: str) -> list[str]:
    """At most two distinct matches: enough to refuse ambiguous reads."""
    with _connect(root) as db:
        if db is None:
            return []
        return [row[0] for row in db.execute(
            "SELECT DISTINCT id FROM insights WHERE id LIKE ? LIMIT 2", (prefix + "%",))]


def store(root: Path, record: dict, decision: dict, identity: dict) -> None:
    """Commit before caller unlinks. Identical crash leftovers converge.

    A row is immutable. Later decisions remain in the decision audit; the row's
    binding is the terminal decision first observed, not a mutable projection.
    A conflicting body fails closed, retaining the inbox copy.
    """
    body = canonical(record).decode()
    with _connect(root, write=True) as db:
        with db:
            old = db.execute("SELECT record FROM insights WHERE id=? AND rev=?",
                             (record["id"], record["rev"])).fetchone()
            if old is not None and old[0] != body:
                raise Denied("Insight archive content conflict")
            db.execute("INSERT OR IGNORE INTO insights VALUES (?, ?, ?, ?, ?)",
                       (record["id"], record["rev"], body,
                        canonical(decision).decode(), canonical(identity).decode()))
    sync_dir(root)


def recover(root: Path) -> None:
    """Resolve any hot rollback journal before a quiescent single-file backup."""
    with _connect(root) as db:
        if db is not None:
            db.execute("SELECT name FROM sqlite_master").fetchall()


def snapshot_references(root: Path):
    """Internal maintenance scan, not an insight listing/pagination interface."""
    with _connect(root) as db:
        if db is not None:
            for row in db.execute("SELECT record FROM insights"):
                record = json.loads(row[0])
                yield record.get("base_snapshot")
