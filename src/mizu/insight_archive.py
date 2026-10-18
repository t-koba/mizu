"""Permanent decided insight storage; callers serialize writes with insights.lock.

DELETE journals and FULL synchronous commits keep the database self-contained at
quiescence (including backups). No deletion API. Rows bind
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
                _schema(db)
            yield db
    except sqlite3.Error as exc:
        raise Denied("Insight archive unavailable") from exc


def _schema(db) -> None:
    """Lazy atomic migration on archive writes/pages; preserve row bytes/order.

    Old databases used (id, rev) as their primary key. Their rowid supplies a
    stable initial sequence. AUTOINCREMENT allocation state travels with backups.
    Ordinary ID reads and reference scans also work before migration.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(insights)")}
        if columns and "seq" not in columns:
            db.execute("ALTER TABLE insights RENAME TO insights_legacy")
        db.execute("CREATE TABLE IF NOT EXISTS insights ("
                   "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "id TEXT NOT NULL, rev INTEGER NOT NULL, record TEXT NOT NULL, "
                   "decision TEXT NOT NULL, identity TEXT NOT NULL, UNIQUE (id, rev))")
        if columns and "seq" not in columns:
            db.execute("INSERT INTO insights (id, rev, record, decision, identity) "
                       "SELECT id, rev, record, decision, identity FROM insights_legacy ORDER BY rowid")
            db.execute("DROP TABLE insights_legacy")
        db.execute("CREATE INDEX IF NOT EXISTS insight_prefix ON insights (id COLLATE NOCASE)")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def page(root: Path, *, limit: int, after: int = 0, high_water: int | None = None) -> tuple:
    """Bounded indexed keyset read of immutable rows, including all revisions.

    Caller holds insights.lock. Capture MAX(seq) once; later appends are outside
    the traversal. Only limit+1 bodies are read, independent of archive size.
    Rows stay complete; Insights.list_page projects bounded summaries.
    """
    with _connect(root, write=True) as db:
        maximum = db.execute("SELECT COALESCE(MAX(seq), 0) FROM insights").fetchone()[0]
        if high_water is None:
            high_water = maximum
        if not 0 <= after <= high_water <= maximum:
            raise Denied("Stale insight archive cursor; restart pagination")
        rows = db.execute("SELECT seq, record, decision FROM insights "
                          "WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",
                          (after, high_water, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{**json.loads(record), "decision": json.loads(decision)}
                 for _, record, decision in rows]
        sync_dir(root)
        return items, high_water, rows[-1][0] if rows else after, more


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
            db.execute("INSERT OR IGNORE INTO insights (id, rev, record, decision, identity) VALUES (?, ?, ?, ?, ?)",
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
