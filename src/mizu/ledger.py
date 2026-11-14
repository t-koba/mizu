"""Transactional current-state index of one project's records.

The files under ``work-items/``, ``decision-history/``, ``inbox/`` and
``decisions/`` stay the durable, append-only history; this SQLite database
(``ledger.sqlite3`` in the project root, WAL journal) is derived from them and
is the only source of current-state reads. It is split into *domains* (work
items, insights with their current decisions), each with its own tables,
schema number and build state. Every write runs inside :meth:`Ledger.write`
while the caller holds the project record lock:

1. an intent row naming each affected key is committed (``synchronous=FULL``,
   so it is durable before the file exists);
2. the caller commits its record file(s);
3. each named key is replayed once from its files with full validation and its
   rows, together with the intent's removal, are committed in one transaction.

Invariant: a key without an intent row has index rows equal to the replay of
its committed files. A crash between steps leaves the intent; the next locked
use replays exactly those keys, and a reader that sees one replays it into a
private in-memory copy. A missing database, an older schema or an interrupted
build is (re)built from the files by the next process holding the record
lock; readers meanwhile build a private in-memory index. Connections live for
one operation, so nothing keeps the file open between calls on any platform.

A domain supplies its tables and row operations (:class:`Domain`); its source
replays files into entries (``ledger_entries`` for the whole domain,
``ledger_item`` for one key) and validates every chain it reads.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import stat
from pathlib import Path

from .errors import Denied
from .fs import lock_timeout

NAME = "ledger.sqlite3"
_BASE = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value);
CREATE TABLE intents(domain TEXT NOT NULL, key TEXT NOT NULL, hint TEXT, PRIMARY KEY(domain, key));
"""


class Domain:
    """Tables and row operations of one index domain.

    ``schema`` is bumped when the derived rows change shape; the next locked
    use rebuilds them. ``resumable`` domains commit entry by entry and resume
    an interrupted build, skipping the keys ``done`` returns; others build in
    one transaction.
    """
    resumable = False

    def __init__(self, name: str, schema: int, tables: str):
        self.name, self.schema, self.tables = name, schema, tables
        self.table_names = re.findall(r"CREATE TABLE (\w+)", tables)

    def done(self, db) -> set:
        return set()

    def put(self, db, entry) -> None:
        raise NotImplementedError

    def replace(self, db, key: str, entries) -> None:
        raise NotImplementedError

    def finish(self, db) -> None:
        """Called inside the transaction that completes a build."""

    def snapshot(self, db):
        raise NotImplementedError


class Ledger:
    """Index owner of one project root; domains attach their sources."""

    def __init__(self, root: Path):
        self.path = Path(root) / NAME
        self.domains: dict[str, tuple[Domain, object]] = {}

    def attach(self, domain: Domain, source) -> None:
        self.domains[domain.name] = (domain, source)

    @contextlib.contextmanager
    def _connect(self, *, create: bool):
        # SQLite opens by pathname, so reject unsafe existing entries before
        # connecting; they are not a missing/unusable derived-index fallback.
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            if not create:
                yield None
                return
        except OSError as exc:
            raise Denied("Ledger index path unavailable") from exc
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise Denied("Ledger index must be a regular, non-linked file")
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                                 uri=True, timeout=lock_timeout(), isolation_level=None)
        except sqlite3.Error:
            if create:
                raise
            yield None
            return
        try:
            # FULL: the intent must be durable before the record file it
            # announces, or a power loss could leave the file ahead unnoticed.
            db.execute("PRAGMA synchronous=FULL")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _state(db, domain: Domain):
        """Return (schema, complete) of one domain, or None when absent."""
        try:
            values = dict(db.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError:
            return None
        schema = values.get(f"{domain.name}.schema")
        return None if schema is None else (schema, values.get(f"{domain.name}.complete"))

    def _usable(self, db, domain: Domain) -> bool:
        return self._state(db, domain) == (domain.schema, 1)

    def _pending(self, db, names):
        marks = ", ".join("?" * len(names))
        return db.execute(f"SELECT domain, key, hint FROM intents WHERE domain IN ({marks})",
                          tuple(names)).fetchall()

    def _replay(self, db, domain, source, key, hint, *, recover):
        """Replace one key's derived rows by a replay of its files."""
        domain.replace(db, key, source.ledger_item(key, hint, recover=recover))
        db.execute("DELETE FROM intents WHERE domain=? AND key=?", (domain.name, key))

    def _repair(self, db, names, *, recover):
        for name, key, hint in self._pending(db, names):
            domain, source = self.domains[name]
            db.execute("BEGIN IMMEDIATE")
            try:
                self._replay(db, domain, source, key, hint, recover=recover)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _build(self, db, name, *, recover):
        """Create or resume one domain's index; restartable when resumable."""
        domain, source = self.domains[name]
        state = self._state(db, domain)
        if state == (domain.schema, 1):
            return
        db.execute("BEGIN IMMEDIATE")
        if "domain" not in [row[1] for row in db.execute("PRAGMA table_info(intents)")] and \
                db.execute("SELECT 1 FROM sqlite_master WHERE name='intents'").fetchone():
            # A single-domain index of an older release: discard it whole.
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                db.execute(f'DROP TABLE "{table}"')
            state = None
        if self._state(db, domain) is None and not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            for statement in filter(str.strip, _BASE.split(";")):
                db.execute(statement)
        if state is None or state[0] != domain.schema:
            for table in domain.table_names:
                db.execute(f'DROP TABLE IF EXISTS "{table}"')
            for statement in filter(str.strip, domain.tables.split(";")):
                db.execute(statement)
            db.execute("DELETE FROM intents WHERE domain=?", (name,))
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?), (?, 0)",
                       (f"{name}.schema", domain.schema, f"{name}.complete"))
        if domain.resumable:
            db.execute("COMMIT")
            skip = domain.done(db)
            db.execute("PRAGMA synchronous=NORMAL")  # The final FULL commit flushes these.
            for entry in source.ledger_entries(recover=recover, skip=skip):
                db.execute("BEGIN IMMEDIATE")
                domain.put(db, entry)
                db.execute("COMMIT")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
        else:
            try:
                for entry in source.ledger_entries(recover=recover, skip=frozenset()):
                    domain.put(db, entry)
            except BaseException:
                db.execute("ROLLBACK")
                raise
        domain.finish(db)
        db.execute("UPDATE meta SET value=1 WHERE key=?", (f"{name}.complete",))
        db.execute("COMMIT")

    def _prepare(self, db, names):
        """Caller holds the record lock: build, then repair interrupted writes."""
        for name in names:
            self._build(db, name, recover=True)
        self._repair(db, names, recover=True)

    def rebuild(self, name: str) -> None:
        """Discard and rebuild one domain; caller holds the record lock."""
        with self._connect(create=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
                db.execute("DELETE FROM meta WHERE key=?", (f"{name}.schema",))
            db.execute("COMMIT")
            self._prepare(db, [name])

    def snapshot(self, name: str):
        """Raw index contents for an audit: None, or (state label, contents)."""
        domain, _ = self.domains[name]
        with self._connect(create=False) as db:
            if db is None:
                return None
            state = self._state(db, domain)
            if state is None or state[0] != domain.schema:
                return "outdated", domain.snapshot(None)
            contents = domain.snapshot(db)
            pending = self._pending(db, [name])
            label = "partial" if state[1] != 1 else "interrupted" if pending else "complete"
            return label, contents

    @contextlib.contextmanager
    def read(self, *names: str, locked: bool):
        """Yield a connection inside one read transaction of valid domains.

        ``locked`` callers hold the record lock and persist any build or
        repair. Others never write the index: a missing, older or partial one
        is built in memory from the files, and pending intents are replayed
        into an in-memory copy, so no read returns an uncommitted state.
        """
        domains = [self.domains[name][0] for name in names]
        with self._connect(create=locked) as db:
            if locked:
                self._prepare(db, names)
                db.execute("BEGIN")
                try:
                    yield db
                finally:
                    db.execute("COMMIT")
                return
            usable = ()
            if db is not None:
                db.execute("BEGIN")
                try:
                    usable = [d.name for d in domains if self._usable(db, d)]
                    if len(usable) == len(names) and not self._pending(db, names):
                        yield db
                        return
                finally:
                    db.execute("COMMIT")
            with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as memory:
                if db is not None:
                    db.backup(memory)
                for name in names:
                    if name not in usable:
                        self._build(memory, name, recover=False)
                self._repair(memory, names, recover=False)
                memory.execute("BEGIN")
                try:
                    yield memory
                finally:
                    memory.execute("COMMIT")

    @contextlib.contextmanager
    def write(self, name: str, keys, *, hint=None):
        """Bracket the caller's record-file commit for ``keys`` of one domain.

        The caller holds the record lock. On exit, normal or not, the named
        keys are replayed from their files; a replay failure leaves the
        intents for the next locked use and propagates.
        """
        keys = list(dict.fromkeys(keys))
        if not keys:
            yield
            return
        with self._connect(create=True) as db:
            self._prepare(db, [name])
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT OR REPLACE INTO intents(domain, key, hint) VALUES (?, ?, ?)",
                           [(name, key, hint) for key in keys])
            db.execute("COMMIT")
            try:
                yield
            finally:
                self._repair(db, [name], recover=True)


def rows(db, sql, params=()):
    """Run one bounded index query; malformed state surfaces as Denied."""
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        raise Denied("Ledger index unreadable") from exc
