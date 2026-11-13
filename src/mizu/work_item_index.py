"""Index domain of work items: one current row per item, plus unseeded decisions."""
from __future__ import annotations

from .ledger import Domain

COLUMNS = ("id", "rev", "status", "holder", "created_key", "updated_key", "origin_insight",
           "origin_rev", "wait_kind", "doing_since", "blocked_since", "source_event", "item")
_INSERT = f"INSERT OR REPLACE INTO items({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
_TABLES = """
CREATE TABLE items(
    id TEXT PRIMARY KEY, rev INTEGER NOT NULL, status TEXT NOT NULL, holder TEXT,
    created_key INTEGER NOT NULL, updated_key INTEGER NOT NULL,
    origin_insight TEXT, origin_rev INTEGER, wait_kind TEXT,
    doing_since TEXT, blocked_since TEXT, source_event TEXT, item TEXT NOT NULL);
CREATE INDEX items_open ON items(status, holder);
CREATE INDEX items_closed ON items(status, updated_key DESC, id);
CREATE INDEX items_origin ON items(origin_insight, origin_rev);
CREATE TABLE unseeded(event_id TEXT PRIMARY KEY);
"""


class _Items(Domain):
    """Entries are ``("item", row)`` and ``("unseeded", event_id)``; keys are item IDs."""
    resumable = True

    def done(self, db) -> set:
        return {row[0] for row in db.execute("SELECT id FROM items")}

    def put(self, db, entry) -> None:
        kind, value = entry
        if kind == "item":
            db.execute(_INSERT, value)
        else:
            db.execute("INSERT OR IGNORE INTO unseeded(event_id) VALUES (?)", (value,))

    def replace(self, db, key, entries) -> None:
        db.execute("DELETE FROM items WHERE id=?", (key,))
        db.execute("DELETE FROM unseeded WHERE event_id=?", (key[3:],))
        for entry in entries:
            self.put(db, entry)

    def snapshot(self, db):
        """Raw contents for an audit: ({id: row}, unseeded set)."""
        if db is None:
            return {}, set()
        return ({row[0]: tuple(row) for row in db.execute(f"SELECT {', '.join(COLUMNS)} FROM items")},
                {row[0] for row in db.execute("SELECT event_id FROM unseeded")})


#: Bump when the derived rows change shape; the next locked use rebuilds them.
ITEMS = _Items("items", 1, _TABLES)
