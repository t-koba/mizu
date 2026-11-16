"""Index domain of work items: one current row per item, plus unseeded decisions."""
from __future__ import annotations

import base64
import hmac
import struct

from .errors import Denied
from .ledger import Domain, rows

# Source-derived columns used by replay/audit; change_seq is index-local.
COLUMNS = ("id", "rev", "status", "holder", "created_key", "updated_key", "origin_insight",
           "origin_rev", "wait_kind", "doing_since", "blocked_since", "source_event", "item")
_INSERT = (f"INSERT OR REPLACE INTO items({', '.join(COLUMNS)}, change_seq) "
           f"VALUES ({', '.join('?' * len(COLUMNS))}, (SELECT seq FROM item_changes))")
_TABLES = """
CREATE TABLE items(
    id TEXT PRIMARY KEY, rev INTEGER NOT NULL, status TEXT NOT NULL, holder TEXT,
    created_key INTEGER NOT NULL, updated_key INTEGER NOT NULL,
    origin_insight TEXT, origin_rev INTEGER, wait_kind TEXT,
    doing_since TEXT, blocked_since TEXT, source_event TEXT, item TEXT NOT NULL,
    change_seq INTEGER NOT NULL);
CREATE UNIQUE INDEX items_changes ON items(change_seq);
CREATE INDEX items_open ON items(status, holder);
CREATE INDEX items_closed ON items(status, updated_key DESC, id);
CREATE INDEX items_origin ON items(origin_insight, origin_rev);
CREATE TABLE unseeded(event_id TEXT PRIMARY KEY);
CREATE TABLE item_changes(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    token_key BLOB NOT NULL, seq INTEGER NOT NULL CHECK(typeof(seq)='integer' AND seq>=0));
INSERT INTO item_changes VALUES (1, randomblob(32), 0);
"""


class _Items(Domain):
    """Entries are ``("item", row)`` and ``("unseeded", event_id)``; keys are item IDs."""
    resumable = True

    def done(self, db) -> set:
        return {row[0] for row in db.execute("SELECT id FROM items")}

    def put(self, db, entry) -> None:
        kind, value = entry
        if kind == "item":
            previous = db.execute(
                f"SELECT {', '.join(COLUMNS)} FROM items WHERE id=?", (value[0],)).fetchone()
            if previous != tuple(value):
                db.execute("UPDATE item_changes SET seq=seq+1")
                db.execute(_INSERT, value)
        else:
            db.execute("INSERT OR IGNORE INTO unseeded(event_id) VALUES (?)", (value,))

    def replace(self, db, key, entries) -> None:
        entries = list(entries)
        if not any(kind == "item" for kind, _ in entries):
            removed = db.execute("DELETE FROM items WHERE id=?", (key,)).rowcount
            if removed:
                # Retained source rows do not normally disappear. If repair
                # removes one, no old cursor may silently omit that deletion.
                db.execute("UPDATE item_changes SET token_key=randomblob(32)")
        db.execute("DELETE FROM unseeded WHERE event_id=?", (key[3:],))
        for entry in entries:
            self.put(db, entry)

    @staticmethod
    def change_position(db, cursor):
        """Authenticate a position against this persistent index generation.

        A zero fence is a completed checkpoint. Otherwise the fence stays
        fixed for the page sequence; rows moved past it belong to the next
        sequence. The key is replaced on rebuild, not on process restart.
        """
        state = rows(db, "SELECT token_key, seq FROM item_changes WHERE singleton=1")
        if (len(state) != 1 or not isinstance(state[0][0], bytes) or len(state[0][0]) != 32
                or type(state[0][1]) is not int or state[0][1] < 0):
            raise Denied("Work-item change index incomplete; rebuild and full resync required")
        key, high = state[0]
        after, fence = 0, 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) != 68 or not cursor.startswith("wc1."):
                    raise ValueError
                raw = base64.b64decode(cursor[4:], altchars=b"-_", validate=True)
                if (len(raw) != 48 or base64.urlsafe_b64encode(raw).decode("ascii") != cursor[4:]
                        or not hmac.compare_digest(
                            raw[16:], hmac.digest(key, b"work-items-v1" + raw[:16], "sha256"))):
                    raise ValueError
                after, fence = struct.unpack(">QQ", raw[:16])
                if after > high or (fence and not after < fence <= high):
                    raise ValueError
            except (ValueError, TypeError, struct.error) as exc:
                raise Denied("Invalid work-item change cursor; full resync required") from exc
        return key, after, fence or high

    @staticmethod
    def change_cursor(key, after, fence=0):
        payload = struct.pack(">QQ", after, fence)
        return "wc1." + base64.urlsafe_b64encode(
            payload + hmac.digest(key, b"work-items-v1" + payload, "sha256")).decode("ascii")

    def snapshot(self, db):
        """Raw contents for an audit: ({id: row}, unseeded set)."""
        if db is None:
            return {}, set()
        return ({row[0]: tuple(row) for row in db.execute(f"SELECT {', '.join(COLUMNS)} FROM items")},
                {row[0] for row in db.execute("SELECT event_id FROM unseeded")})


#: Bump when the derived rows change shape; the next locked use rebuilds them.
ITEMS = _Items("items", 2, _TABLES)
