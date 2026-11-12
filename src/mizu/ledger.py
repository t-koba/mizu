"""Transactional current-state index of one project's work items.

The revision files under ``work-items/`` and ``decision-history/`` stay the
durable, append-only history; this SQLite database (``ledger.sqlite3`` in the
project root, WAL journal) is derived from them and is the only source of
current-state reads. Every work-item write runs inside :meth:`Ledger.write`
while the caller holds the project record lock:

1. an intent row naming each affected item is committed (``synchronous=FULL``,
   so it is durable before the file exists);
2. the caller commits its revision file(s);
3. each named item is replayed once from its files with full chain validation
   and its row, together with the intent's removal, is committed in one
   transaction.

Invariant: an item without an intent row has an index row equal to the replay
of its committed files. A crash between steps leaves the intent; the next
locked use replays exactly those items, and a reader that sees one replays it
into a private in-memory copy. A missing database, an older schema or an
interrupted build is (re)built from the files by the next process holding the
record lock, resuming item by item; readers meanwhile build a private
in-memory index. Connections live for one operation, so nothing keeps the file
open between calls on any platform.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from .errors import Denied
from .fs import lock_timeout

NAME = "ledger.sqlite3"
#: Bump when the derived rows change shape; the next locked use rebuilds them.
SCHEMA = 1
_TABLES = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE items(
    id TEXT PRIMARY KEY, rev INTEGER NOT NULL, status TEXT NOT NULL, holder TEXT,
    created_key INTEGER NOT NULL, updated_key INTEGER NOT NULL,
    origin_insight TEXT, origin_rev INTEGER, wait_kind TEXT,
    doing_since TEXT, blocked_since TEXT, source_event TEXT, item TEXT NOT NULL);
CREATE INDEX items_open ON items(status, holder);
CREATE INDEX items_closed ON items(status, updated_key DESC, id);
CREATE INDEX items_origin ON items(origin_insight, origin_rev);
CREATE TABLE unseeded(event_id TEXT PRIMARY KEY);
CREATE TABLE intents(item_id TEXT PRIMARY KEY, source_event TEXT);
"""
COLUMNS = ("id", "rev", "status", "holder", "created_key", "updated_key", "origin_insight",
           "origin_rev", "wait_kind", "doing_since", "blocked_since", "source_event", "item")
_INSERT = f"INSERT OR REPLACE INTO items({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"


class Ledger:
    """Index owner; ``source`` replays entries from the revision files.

    ``source.ledger_entries(recover=, skip=)`` yields ``("item", row)`` and
    ``("unseeded", event_id)`` for the whole ledger; ``source.ledger_item(id,
    source_event, recover=)`` returns the entries of one item. Both validate
    the complete chains they read and raise on corrupt history.
    """

    def __init__(self, root: Path, source):
        self.path, self.source = Path(root) / NAME, source

    @contextlib.contextmanager
    def _connect(self, *, create: bool):
        if not create and not self.path.exists():
            yield None
            return
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                                 uri=True, timeout=lock_timeout(), isolation_level=None)
        except sqlite3.Error:
            if create:
                raise
            yield None
            return
        try:
            # FULL: the intent must be durable before the revision file it
            # announces, or a power loss could leave the file ahead unnoticed.
            db.execute("PRAGMA synchronous=FULL")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _state(db):
        """Return (schema, complete) or None when the tables are absent."""
        try:
            values = dict(db.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError:
            return None
        return values.get("schema"), values.get("complete")

    @staticmethod
    def _apply(db, entries):
        for kind, value in entries:
            if kind == "item":
                db.execute(_INSERT, value)
            else:
                db.execute("INSERT OR IGNORE INTO unseeded(event_id) VALUES (?)", (value,))

    def _replay(self, db, item_id, source_event, *, recover):
        """Replace one item's derived rows by a replay of its files."""
        entries = self.source.ledger_item(item_id, source_event, recover=recover)
        db.execute("DELETE FROM items WHERE id=?", (item_id,))
        db.execute("DELETE FROM unseeded WHERE event_id=?", (item_id[3:],))
        self._apply(db, entries)
        db.execute("DELETE FROM intents WHERE item_id=?", (item_id,))

    def _repair(self, db, *, recover):
        for item_id, source_event in db.execute("SELECT item_id, source_event FROM intents").fetchall():
            db.execute("BEGIN IMMEDIATE")
            try:
                self._replay(db, item_id, source_event, recover=recover)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _build(self, db, *, recover):
        """Create or resume the full index; committed per entry, so restartable."""
        state = self._state(db)
        if state is not None and state[0] == SCHEMA and state[1] == 1:
            return
        if state is None or state[0] != SCHEMA:
            db.execute("BEGIN IMMEDIATE")
            for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                db.execute(f'DROP TABLE "{name}"')
            for statement in filter(str.strip, _TABLES.split(";")):
                db.execute(statement)
            db.execute("INSERT INTO meta VALUES ('schema', ?), ('complete', 0)", (SCHEMA,))
            db.execute("COMMIT")
        skip = {row[0] for row in db.execute("SELECT id FROM items")}
        db.execute("PRAGMA synchronous=NORMAL")  # The final FULL commit flushes these.
        for entry in self.source.ledger_entries(recover=recover, skip=skip):
            db.execute("BEGIN IMMEDIATE")
            self._apply(db, [entry])
            db.execute("COMMIT")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE meta SET value=1 WHERE key='complete'")
        db.execute("COMMIT")

    def _prepare(self, db):
        """Caller holds the record lock: build, then repair interrupted writes."""
        self._build(db, recover=True)
        self._repair(db, recover=True)

    def rebuild(self) -> None:
        """Discard and rebuild the whole index; caller holds the record lock."""
        with self._connect(create=True) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DROP TABLE IF EXISTS meta")
            db.execute("COMMIT")
            self._prepare(db)

    def snapshot(self):
        """Raw index contents for an audit: None, or (state, {id: row}, unseeded)."""
        with self._connect(create=False) as db:
            if db is None:
                return None
            state = self._state(db)
            if state is None or state[0] != SCHEMA:
                return "outdated", {}, set()
            items = {row[0]: tuple(row) for row in db.execute(
                f"SELECT {', '.join(COLUMNS)} FROM items")}
            unseeded = {row[0] for row in db.execute("SELECT event_id FROM unseeded")}
            pending = db.execute("SELECT 1 FROM intents LIMIT 1").fetchone()
            label = "partial" if state[1] != 1 else "interrupted" if pending else "complete"
            return label, items, unseeded

    @contextlib.contextmanager
    def read(self, *, locked: bool):
        """Yield a connection inside one read transaction of a valid index.

        ``locked`` callers hold the record lock and persist any build or
        repair. Others never write the index: a missing, older or partial one
        is built in memory from the files, and pending intents are replayed
        into an in-memory copy, so no read returns an uncommitted state.
        """
        with self._connect(create=locked) as db:
            if locked:
                self._prepare(db)
                db.execute("BEGIN")
                try:
                    yield db
                finally:
                    db.execute("COMMIT")
                return
            usable = False
            if db is not None:
                db.execute("BEGIN")
                try:
                    state = self._state(db)
                    usable = state == (SCHEMA, 1)
                    pending = usable and db.execute("SELECT 1 FROM intents LIMIT 1").fetchone()
                    if usable and not pending:
                        yield db
                        return
                finally:
                    db.execute("COMMIT")
            with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as memory:
                if usable:
                    db.backup(memory)
                    self._repair(memory, recover=False)
                else:
                    self._build(memory, recover=False)
                memory.execute("BEGIN")
                try:
                    yield memory
                finally:
                    memory.execute("COMMIT")

    @contextlib.contextmanager
    def write(self, item_ids, *, source_event=None):
        """Bracket the caller's revision-file commit for ``item_ids``.

        The caller holds the record lock. On exit, normal or not, the named
        items are replayed from their files; a replay failure leaves the
        intents for the next locked use and propagates.
        """
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            yield
            return
        with self._connect(create=True) as db:
            self._prepare(db)
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT OR REPLACE INTO intents(item_id, source_event) VALUES (?, ?)",
                           [(item_id, source_event) for item_id in ids])
            db.execute("COMMIT")
            try:
                yield
            finally:
                self._repair(db, recover=True)


def rows(db, sql, params=()):
    """Run one bounded index query; malformed state surfaces as Denied."""
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        raise Denied("Work-item ledger index unreadable") from exc
