"""Permanent decided insight storage; callers serialize writes with insights.lock.

DELETE journals and FULL synchronous commits keep the database self-contained at
quiescence (including backups). No deletion API. Body rows bind
complete normalized content to the decision and identity observed at archival.
Revision 0 rows preserve validated identities whose bodies predate archival.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import DIGEST, MAX_FRAME, canonical, identifier, safe_read, sync_dir

NAME = "insight-archive.sqlite3"
# SQLite INTEGER ordering keys are signed 64-bit values.
MAX_SQLITE_INTEGER = 9223372036854775807


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
    except (sqlite3.Error, OSError, LimitExceeded) as exc:
        raise Denied("Insight archive unavailable") from exc


@contextlib.contextmanager
def _immutable_connect(root: Path):
    """No-recovery, no-lock connection for read-only projections.

    The writable ``_connect`` reader uses ``mode=rw`` so a hot rollback
    journal left by a killed writer is recovered on open. Read-only CLI
    projections must never trigger that recovery, create the database, or
    take SQLite locks. This path therefore refuses any ``-journal``,
    ``-wal`` or ``-shm`` sidecar before opening and uses a ``mode=ro``
    immutable handle; any sidecar fails closed without touching files.
    """
    path = root.resolve() / NAME
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Denied("Insight archive unavailable") from exc
        raise Denied("Insight archive has pending recovery; refusing read-only access")
    try:
        info = path.lstat()
    except FileNotFoundError:
        yield None
        return
    except OSError as exc:
        raise Denied("Insight archive unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Denied("Unsafe insight archive path")
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1",
                                               uri=True)) as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise Denied("Insight archive requires DELETE journal mode")
            yield db
    except (sqlite3.Error, OSError, LimitExceeded) as exc:
        raise Denied("Insight archive unavailable") from exc


def read_immutable(root: Path, insight_id: str, *, include_unavailable: bool = False) -> dict | None:
    """No-recovery, no-lock archive read for read-only projections.

    Returns the newest row for ``insight_id`` or None when the archive is
    absent/unknown, without recovering journals, creating files, or taking
    locks. Any pending ``-journal``/``-wal``/``-shm`` sidecar fails closed.
    """
    with _immutable_connect(root) as db:
        if db is None:
            return None
        row = db.execute("SELECT record FROM insights WHERE id=? ORDER BY rev DESC LIMIT 1",
                         (insight_id,)).fetchone()
        record = json.loads(row[0]) if row else None
        if record and record.get("body_unavailable") is True and not include_unavailable:
            raise Denied("Insight body unavailable; legacy retention removed it before archival")
        return record


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


def _json(raw: bytes | str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        # Also refuse non-finite values, including in unprojected fields.
        canonical(value)
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied("Corrupt retained insight history") from exc


def identity(root: Path, insight_id: str) -> dict | None:
    """Bounded, pinned validation shared by exact legacy reads and migration."""
    identifier(insight_id)
    path = root / "insight-ids" / f"{insight_id}.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Denied("Unsafe retained insight identity") from exc
    try:
        value = _json(safe_read(root, f"insight-ids/{insight_id}.json", 4096))
        if (value.get("id") != insight_id or not isinstance(value.get("sha256"), str)
                or not DIGEST.fullmatch(value["sha256"])):
            raise ValueError
        created = value.get("created_at")
        stamp = dt.datetime.fromisoformat(created) if isinstance(created, str) else None
        if stamp is None or stamp.tzinfo is None:
            raise ValueError
        if "rev" in value and (type(value["rev"]) is not int
                                or not 1 <= value["rev"] <= MAX_SQLITE_INTEGER):
            raise ValueError
        return value
    except (OSError, LimitExceeded, ValueError, TypeError) as exc:
        raise Denied("Unsafe or corrupt retained insight identity") from exc


def _directory(path: Path) -> bool:
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise Denied("Unsafe insight history directory")
        return True
    except FileNotFoundError:
        return False


def _body(raw: bytes | str, key: str) -> None:
    record = _json(raw)
    if (record.get("id") != key or not isinstance(record.get("body"), str)
            or not record["body"].strip() or not isinstance(record.get("title"), str)
            or not record["title"]):
        raise Denied("Corrupt insight body during history migration")
    identifier(record.get("source"))


_MIGRATION = "legacy-identities-v1"


def _tombstone(value: dict) -> bytes:
    record = {"id": value["id"], "body_unavailable": True, "created_at": value["created_at"]}
    if "rev" in value:
        record["rev"] = value["rev"]
    return canonical(record)


def _live_identity_body(root: Path, key: str, has_inbox: bool) -> bool:
    """Validate an existing inbox body before treating it as a live candidate."""
    if not has_inbox:
        return False
    live = root / "inbox" / f"{key}.json"
    try:
        live.lstat()
    except FileNotFoundError:
        return False
    _body(safe_read(root, f"inbox/{key}.json", MAX_FRAME), key)
    return True


def _identity_entries(root: Path) -> list[tuple[str, dict, Path]]:
    """Validate every pinned retained identity; refuse unexpected entries."""
    directory = root / "insight-ids"
    if not _directory(directory):
        return []
    entries = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if not entry.name.endswith(".json"):
                    raise Denied("Unexpected insight identity entry; repair before retry")
                key = identifier(entry.name[:-5])
                value = identity(root, key)
                if value is None:
                    raise Denied("Insight identity disappeared during migration")
                entries.append((key, value, Path(entry.path)))
    except OSError as exc:
        raise Denied("Unsafe insight identity directory") from exc
    return entries


def _migrate_identities(root: Path, db) -> None:
    """One scan, one commit under insights.lock; cancellation rolls back all rows.

    Revision 0 is reserved for identity-only rows, never an actual body revision.
    Do not infer a revision, decision, title or reference from other audit files.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS insight_migrations (name TEXT PRIMARY KEY)")
        if not db.execute("SELECT 1 FROM insight_migrations WHERE name=?", (_MIGRATION,)).fetchone():
            has_inbox = _directory(root / "inbox")
            if _directory(root / "insight-ids"):
                # Stream entries and propagate errors instead of dropping candidates.
                with os.scandir(root / "insight-ids") as entries:
                    for entry in entries:
                        _import_identity(root, db, Path(entry.path), has_inbox)
            db.execute("INSERT INTO insight_migrations (name) VALUES (?)", (_MIGRATION,))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _import_identity(root: Path, db, path: Path, has_inbox: bool) -> None:
    if path.suffix != ".json":
        raise Denied("Unexpected insight identity entry; repair before retry")
    key = identifier(path.stem)
    value = identity(root, key)
    if value is None:
        raise Denied("Insight identity disappeared during migration")
    if _live_identity_body(root, key, has_inbox):
        return
    archived = db.execute("SELECT rev, record FROM insights WHERE id=? ORDER BY rev DESC LIMIT 1",
                          (key,)).fetchone()
    if archived:
        if archived[0] != 0:
            _body(archived[1], key)
        return
    db.execute("INSERT INTO insights (id, rev, record, decision, identity) VALUES (?, 0, ?, 'null', ?)",
               (key, _tombstone(value).decode(), canonical(value).decode()))


def identity_conversion_state(root: Path) -> dict:
    """Read-only, no-recovery inventory of identities still needing conversion.

    A live inbox body or any archived row is already represented; only an
    identity whose body was removed before archival needs a tombstone row.
    """
    entries = _identity_entries(root)
    has_inbox = _directory(root / "inbox")
    pending = []
    with _immutable_connect(root) as db:
        for key, _, _ in entries:
            if _live_identity_body(root, key, has_inbox):
                continue
            row = db.execute("SELECT 1 FROM insights WHERE id=? LIMIT 1", (key,)).fetchone() if db else None
            if row is None:
                pending.append(key)
    return {"entries_examined": len(entries), "pending": pending}


def convert_identities(root: Path) -> dict:
    """Import retained identities into verified archive tombstones.

    The caller holds insights.lock on a paused, quiescent project. Every import
    is idempotent, so a retry after a crash adds no duplicate rows. Read-back
    verification and the completion marker share the same transaction as the
    imports; any refusal rolls the whole conversion back.
    """
    entries = _identity_entries(root)
    has_inbox = _directory(root / "inbox")
    report = dict(entries_examined=len(entries), candidates=0, converted=0,
                  conflicts=[], errors=[], ok=True, scan_complete=True, truncated=False)
    with _connect(root, write=True) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS insight_migrations (name TEXT PRIMARY KEY)")
            for key, _, path in entries:
                if (not _live_identity_body(root, key, has_inbox)
                        and db.execute("SELECT 1 FROM insights WHERE id=? LIMIT 1",
                                       (key,)).fetchone() is None):
                    report["candidates"] += 1
                before = db.total_changes
                _import_identity(root, db, path, has_inbox)
                if db.total_changes > before:
                    report["converted"] += 1
            db.execute("INSERT OR IGNORE INTO insight_migrations (name) VALUES (?)", (_MIGRATION,))
            for key, value, _ in entries:
                if _live_identity_body(root, key, has_inbox):
                    continue
                archived = db.execute("SELECT rev, record, identity FROM insights "
                                      "WHERE id=? ORDER BY rev DESC LIMIT 1", (key,)).fetchone()
                if archived is None:
                    raise Denied("Converted insight identity is missing from the archive")
                if archived[0] != 0:
                    _body(archived[1], key)
                    continue
                if (archived[1] != _tombstone(value).decode()
                        or archived[2] != canonical(value).decode()):
                    raise Denied("Converted insight identity failed read-back verification")
    sync_dir(root)
    return report


def page(root: Path, *, limit: int, after: int = 0, high_water: int | None = None) -> tuple:
    """Bounded indexed keyset read of immutable rows, including all revisions.

    Caller holds insights.lock. Capture MAX(seq) once; later appends are outside
    the traversal. Only limit+1 bodies are read, independent of archive size.
    Body rows stay complete; identity-only rows carry an explicit tombstone.
    Insights.list_page projects bounded summaries.
    """
    with _connect(root, write=True) as db:
        _migrate_identities(root, db)
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


def read(root: Path, insight_id: str, *, include_unavailable: bool = False) -> dict | None:
    with _connect(root) as db:
        if db is None:
            return None
        row = db.execute("SELECT record FROM insights WHERE id=? ORDER BY rev DESC LIMIT 1",
                         (insight_id,)).fetchone()
        record = json.loads(row[0]) if row else None
        if record and record.get("body_unavailable") is True and not include_unavailable:
            raise Denied("Insight body unavailable; legacy retention removed it before archival")
        return record


def latest(root: Path, *, recover: bool) -> list:
    """Newest row of every archived insight as (id, record JSON) pairs.

    ``recover`` callers hold insights.lock and may recover a hot journal; the
    others use the no-recovery read-only path.
    """
    with (_connect(root) if recover else _immutable_connect(root)) as db:
        if db is None:
            return []
        return db.execute("SELECT id, record FROM insights i WHERE rev="
                          "(SELECT MAX(rev) FROM insights WHERE id=i.id)").fetchall()


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
            if db.execute("SELECT 1 FROM insights WHERE id=? AND rev=0", (record["id"],)).fetchone():
                raise Denied("Insight body unavailable; cannot replace a legacy tombstone")
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
                if not record.get("body_unavailable"):
                    yield record.get("base_snapshot")
