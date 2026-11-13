"""Index domain of insights: current inbox state and current decision per insight.

Rows (derived from ``inbox/``, the insight archive and ``decisions/``):

* ``insights``: one row per known insight. ``live`` marks an inbox file, else
  the row stands for the newest archived revision (``rev`` only). ``valid``
  is false when the file cannot be read as a well-formed record; list-style
  readers skip such rows, fail-closed counts refuse to answer while any
  exists, and the audit reports them. ``sig`` is the content identity the
  inbox generation follows; ``item`` is the exact inbox record.
* ``decisions``: one row per current decision pointer. ``valid`` means a
  readable pointer with a known action and revision; ``linked`` means the
  pointer also has the full persisted shape and equals its immutable
  ``decision-history`` event (the check that used to run on every read).

``inbox.epoch``/``inbox.seq`` in ``meta`` form the inbox generation: the epoch
is a digest of the contents at build time and the sequence advances in the
transaction of every write that changes a live record's content identity.
"""
from __future__ import annotations

import json
import os

from . import insight_archive
from .errors import Denied, LimitExceeded
from .fs import canonical, digest, identifier, safe_read
from .ledger import Domain

_TABLES = """
CREATE TABLE insights(
    id TEXT PRIMARY KEY, live INTEGER NOT NULL, valid INTEGER NOT NULL, rev INTEGER,
    source TEXT, kind TEXT, addressee TEXT, created_at TEXT, sig TEXT, item TEXT);
CREATE INDEX insights_live ON insights(live, valid, created_at, id);
CREATE INDEX insights_source ON insights(source, live);
CREATE TABLE decisions(
    insight_id TEXT PRIMARY KEY, valid INTEGER NOT NULL, linked INTEGER NOT NULL,
    rev INTEGER, action TEXT, record TEXT);
CREATE INDEX decisions_action ON decisions(action, rev);
"""
_ACTIONS = ("accept", "modify", "reject", "defer", "withdraw")
_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, Denied, LimitExceeded)


def _signature(row):
    return row[8] if row[1] and row[2] else None


class _Insights(Domain):
    """Entries are ``("insight", row)`` and ``("decision", row)``; keys ``i:ID`` and ``d:ID``."""

    def put(self, db, entry) -> None:
        kind, row = entry
        marks = ", ".join("?" * len(row))
        db.execute(f"INSERT OR REPLACE INTO {'insights' if kind == 'insight' else 'decisions'} "
                   f"VALUES ({marks})", row)

    def replace(self, db, key, entries) -> None:
        kind, item_id = key.split(":", 1)
        if kind == "d":
            db.execute("DELETE FROM decisions WHERE insight_id=?", (item_id,))
        else:
            old = db.execute("SELECT * FROM insights WHERE id=?", (item_id,)).fetchone()
            db.execute("DELETE FROM insights WHERE id=?", (item_id,))
        for entry in entries:
            self.put(db, entry)
        if kind == "i":
            new = db.execute("SELECT * FROM insights WHERE id=?", (item_id,)).fetchone()
            if (old and _signature(old)) != (new and _signature(new)):
                db.execute("UPDATE meta SET value=value+1 WHERE key='inbox.seq'")

    def finish(self, db) -> None:
        sigs = sorted(row[0] for row in db.execute(
            "SELECT sig FROM insights WHERE live=1 AND valid=1"))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('inbox.epoch', ?), ('inbox.seq', 0)",
                   (digest(canonical(sigs)),))

    def snapshot(self, db):
        """Raw contents for an audit: ({id: row}, {id: row})."""
        if db is None:
            return {}, {}
        return ({row[0]: tuple(row) for row in db.execute("SELECT * FROM insights")},
                {row[0]: tuple(row) for row in db.execute("SELECT * FROM decisions")})


#: Bump when the derived rows change shape; the next locked use rebuilds them.
INSIGHTS = _Insights("insights", 1, _TABLES)


def generation(db) -> str:
    """The inbox generation as a SHA-256 hex identity of the epoch and sequence."""
    values = dict(db.execute("SELECT key, value FROM meta WHERE key LIKE 'inbox.%'"))
    return digest(canonical([values["inbox.epoch"], values["inbox.seq"]]))


class InsightSource:
    """Replays inbox, archive and decision files into index entries."""

    def __init__(self, insights):
        self.insights = insights
        self._events = None

    # Row construction -------------------------------------------------

    def _live(self, stem: str) -> tuple:
        from .insights import _content_sha, _evidence_sha, insight_metadata
        root = self.insights.root
        bad = (stem, 1, 0, None, None, None, None, None, None, None)
        try:
            if (root / "inbox" / f"{stem}.json").is_symlink():
                return bad
            item = json.loads(safe_read(root, f"inbox/{stem}.json", self.insights.maximum))
            if type(item) is not dict or identifier(item.get("id")) != stem:
                return bad
            identifier(item.get("source"))
            rev = item.get("rev", 1)
            created = item.get("created_at")
            metadata = insight_metadata(item)
            if type(rev) is not int or rev < 1 or not isinstance(created, str):
                return bad
            sig = f"{stem}:{rev}:{_content_sha(item)}:{_evidence_sha(item.get('evidence'))}"
            return (stem, 1, 1, rev, item["source"], metadata.get("kind"),
                    metadata.get("addressee"), created, sig, canonical(item).decode())
        except _ERRORS:
            return bad

    @staticmethod
    def _archived(insight_id: str, record) -> tuple:
        """Row of the newest archived revision; an identity-only tombstone may lack ``rev``."""
        if not isinstance(record, dict):
            return (insight_id, 0, 0, None, None, None, None, None, None, None)
        if record.get("body_unavailable") is True:
            rev = record.get("rev")
            rev = rev if type(rev) is int and rev >= 1 else None
        else:
            rev = record.get("rev", 1)
            if record.get("id") != insight_id or type(rev) is not int or rev < 1:
                return (insight_id, 0, 0, None, None, None, None, None, None, None)
        source = record.get("source")
        return (insight_id, 0, 1, rev, source if isinstance(source, str) else None,
                None, None, None, None, None)

    def _decision(self, stem: str, *, recover: bool) -> tuple:
        from .insights import MAX_DECISION_EVENT_BYTES
        from .work_items import WorkItems
        root = self.insights.root
        bad = (stem, 0, 0, None, None, None)
        try:
            if (root / "decisions" / f"{stem}.json").is_symlink():
                return bad
            decision = json.loads(safe_read(root, f"decisions/{stem}.json",
                                            MAX_DECISION_EVENT_BYTES))
            rev = decision.get("rev", 1)
            if (type(decision) is not dict or decision.get("id") != stem
                    or decision.get("action") not in _ACTIONS or type(rev) is not int or rev < 1):
                return bad
        except _ERRORS:
            return bad
        try:
            WorkItems._validate_current_decision_shape(decision, stem)
            self.insights._work_item_store()._resolve_current_work_item(
                decision, readonly=not recover, events=lambda: self._history(recover),
                resolve=False)
            linked = 1
        except _ERRORS:
            linked = 0
        return (stem, 1, linked, rev, decision["action"], canonical(decision).decode())

    def _history(self, recover: bool):
        """Decision-history events, scanned once per replay."""
        if self._events is None:
            self._events = list(self.insights._work_item_store()._iter_decision_events(
                recover=recover))
        return self._events

    # Source protocol --------------------------------------------------

    def _names(self, directory: str):
        path = self.insights.root / directory
        if not insight_archive._directory(path):
            return
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda value: value.name):
                if entry.name.endswith(".json"):
                    yield entry.name[:-5]

    def ledger_entries(self, *, recover: bool, skip=frozenset()):
        self._events = None
        live = set()
        for stem in self._names("inbox"):
            live.add(stem)
            yield ("insight", self._live(stem))
        for insight_id, raw in insight_archive.latest(self.insights.root, recover=recover):
            if insight_id not in live:
                yield ("insight", self._archived(insight_id, json.loads(raw)))
        for stem in self._names("decisions"):
            yield ("decision", self._decision(stem, recover=recover))

    def ledger_item(self, key: str, hint, *, recover: bool) -> list:
        self._events = None
        kind, insight_id = key.split(":", 1)
        identifier(insight_id)
        if kind == "d":
            if (self.insights.root / "decisions" / f"{insight_id}.json").exists():
                return [("decision", self._decision(insight_id, recover=recover))]
            return []
        if (self.insights.root / "inbox" / f"{insight_id}.json").exists():
            return [("insight", self._live(insight_id))]
        read = insight_archive.read if recover else insight_archive.read_immutable
        record = read(self.insights.root, insight_id, include_unavailable=True)
        return [] if record is None else [("insight", self._archived(insight_id, record))]


def audit(insights, *, repair: bool) -> dict:
    """Replay every insight and decision file strictly and compare with the index.

    The caller holds the record lock. Corruption covers unreadable inbox
    records, decision pointers that are malformed or differ from their
    immutable event, decisions ahead of their insight and unreadable prior
    revisions; divergence is any difference between the files' replay and the
    index rows. ``repair`` rebuilds a diverged index. Files never change.
    """
    from .work_items import MAX_OPEN_PAGE_ROWS
    ledger = insights.ledger
    live, decisions = {}, {}
    for kind, row in ledger.domains["insights"][1].ledger_entries(recover=False):
        (live if kind == "insight" else decisions)[row[0]] = row
    corrupt = []
    for row in live.values():
        if row[1] and not row[2]:
            corrupt.append((row[0], "Malformed inbox record"))
        elif row[1]:
            for rev in range(1, row[3]):
                path = insights.root / "insight-revisions" / f"{row[0]}.r{rev}.json"
                if path.exists() and insights.read_revision(row[0], rev) is None:
                    corrupt.append((row[0], f"Malformed prior revision {rev}"))
    for row in decisions.values():
        if not row[1]:
            corrupt.append((row[0], "Malformed decision pointer"))
        elif not row[2]:
            corrupt.append((row[0], "Decision pointer differs from its immutable event"))
        elif row[0] in live and live[row[0]][3] is not None and row[3] > live[row[0]][3]:
            corrupt.append((row[0], "Decision revision is ahead of its insight"))
    snapshot = ledger.snapshot("insights")
    state, (indexed_live, indexed_decisions) = ("missing", ({}, {})) if snapshot is None else snapshot
    divergent = sorted({key for key in set(live) | set(indexed_live)
                        if live.get(key) != indexed_live.get(key)}
                       | {key for key in set(decisions) | set(indexed_decisions)
                          if decisions.get(key) != indexed_decisions.get(key)})
    consistent = not corrupt and not divergent
    repaired = bool(repair and divergent)
    if repaired:
        ledger.rebuild("insights")
    return {"insights": len(live), "decisions": len(decisions), "index": state,
            "consistent": consistent, "repaired": repaired,
            "corrupt": [{"id": key, "error": error} for key, error in corrupt[:MAX_OPEN_PAGE_ROWS]],
            "corrupt_count": len(corrupt),
            "divergent": divergent[:MAX_OPEN_PAGE_ROWS], "divergent_count": len(divergent)}
