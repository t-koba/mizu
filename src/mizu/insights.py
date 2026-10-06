"""Immutable proposals, separate decisions, and an untrusted Editor outbox.

Revision contract (operator EDITOR c51c): stable insight identity exposes
only the improved current content, its current decision/evidence gap and a
revision identity (``rev``) in ordinary prompts, lists and reads. Obsolete
claims are replaced, never appended. Prior revisions live under
``insight-revisions/`` and are returned only by the explicit ``history``
path, never injected into routine context. Updates are authorized to the
original submitter (source match) with compare-and-swap on ``expected_rev``;
decisions bind to the reviewed ``rev`` so a prior approval never authorizes
changed content, while a meaningful revision becomes pending again and
advances the inbox generation. Identical retries are no-ops that do not wake.
Revisions are durable audit like decisions (backed up, never pruned).

Current transport: only the operator path exposes revise/history
(``mizu insight revise/history`` with source ``operator``). Other sources
supersede obsolete claims via a new insight until a reviewed release adds an
owner revise transport; the source-match check already enforces that future.
``run`` is revision provenance recorded only on meaningful change and never
wakes: an identical-content retry with a different ``run`` returns current
unchanged (stored ``run`` preserved) without bumping rev.
"""
from __future__ import annotations

import json
import time
import datetime as dt
import heapq
import uuid
import os
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import PREVIEW_BYTES, canonical, digest, identifier, lock, now, read_json, safe_read, write_json, mkdir, DIGEST


def _rev_of(record: dict) -> int:
    try:
        rev = int(record.get("rev", 1))
    except (TypeError, ValueError):
        return 1
    return rev if rev >= 1 else 1


def _content_sha(record: dict) -> str:
    payload = {k: record.get(k) for k in ("id", "source", "title", "body", "base_snapshot")}
    payload["rev"] = _rev_of(record)
    return digest(canonical(payload))


def _normalize(record: dict) -> dict:
    out = dict(record)
    out["rev"] = _rev_of(record)
    if not out.get("updated_at"):
        out["updated_at"] = out.get("created_at")
    return out


def _effective_decision(item_rev: int, decision) -> dict | None:
    if not decision:
        return None
    if decision.get("action") == "defer":
        return decision
    try:
        decision_rev = int(decision.get("rev", 1))
    except (TypeError, ValueError):
        decision_rev = 1
    if decision_rev != item_rev:
        return None
    return decision


class Insights:
    def __init__(self, root: Path, maximum: int = PREVIEW_BYTES, retention_days: int = 31):
        self.root, self.maximum = root, maximum
        self.retention_days = retention_days

    def submit(self, *, source: str, title: str, body: str, base_snapshot: str | None,
               run: str | None = None, insight_id: str | None = None) -> dict:
        identifier(source)
        insight_id = identifier(insight_id or uuid.uuid4().hex)
        if not isinstance(title, str) or not 1 <= len(title) <= 200 or not isinstance(body, str) or not body.strip():
            raise Denied("Insight requires a title (1–200 characters) and nonempty body")
        if base_snapshot is not None and (not isinstance(base_snapshot, str) or not DIGEST.fullmatch(base_snapshot)):
            raise Denied("base_snapshot must be a SHA-256 ID or null")
        payload = {"id": insight_id, "source": source, "title": title, "body": body,
                   "base_snapshot": base_snapshot, "run": run}
        if len(canonical(payload)) > self.maximum:
            raise Denied("Insight exceeds byte limit")
        with lock(self.root / "locks" / "insights.lock"):
            path = self.root / "inbox" / f"{insight_id}.json"
            identity_path = self.root / "insight-ids" / f"{insight_id}.json"
            identity = read_json(identity_path)
            sha = digest(canonical(payload))
            old = read_json(path)
            if identity and identity.get("sha256") != sha:
                # A revised identity exists; direct submit with different
                # content is refused (use revise with expected_rev).
                # Idempotent retry with current content returns current below.
                if old is not None:
                    current_payload = {k: old[k] for k in payload}
                    if current_payload == payload:
                        return _normalize(old)
                raise Denied("Insight ID was reused with different content")
            if old is None and identity:
                return {**payload, "created_at": identity["created_at"], "rev": 1,
                        "updated_at": identity["created_at"], "retained": False}
            if old is None and (self.root / "decisions" / f"{insight_id}.json").exists():
                raise Denied("Decided ID has no retained identity; reuse refused")
            if old:
                if {k: old[k] for k in payload} != payload:
                    raise Denied("Insight ID was reused with different content")
                write_json(identity_path, {"id": insight_id, "sha256": sha, "created_at": old["created_at"]}, exclusive=True)
                return _normalize(old)
            record = {**payload, "created_at": now(), "rev": 1}
            record["updated_at"] = record["created_at"]
            write_json(path, record, exclusive=True)
            write_json(identity_path, {"id": insight_id, "sha256": sha, "created_at": record["created_at"]}, exclusive=True)
            return record

    def revise(self, insight_id: str, *, source: str, title: str, body: str,
               base_snapshot: str | None, run: str | None = None,
               expected_rev: int | None = None) -> dict:
        """Replace obsolete claims under a stable ID; prior version goes to audit.

        Schema: title 1–200 chars, nonempty body, base_snapshot SHA-256 or
        null. Bounds: same PREVIEW_BYTES bound as submit. Trust: ``source``
        must equal the original submitter; ``expected_rev`` (when given)
        must equal the current rev (compare-and-swap). Retry: identical
        content (title/body/base_snapshot) is a no-op returning current
        without bumping rev, archiving, or waking; ``run`` is provenance
        recorded only on meaningful change, so a same-content retry with a
        different ``run`` preserves the stored ``run`` and repairs identity.
Withdrawal contract (operator EDITOR 6fa4163d): author withdrawal is distinct
from substantive rejection. ``withdraw`` closes a pending proposal with an
actor-labelled (submitter source or operator) rev-bound record in the same
decision store, so ordinary lists read only the current disposition while
``decision-history/`` keeps the audit. Only the original submitter or the
operator may withdraw; the worker never withdraws others' submissions.
Withdrawal is refused over a revision-current substantive decision (accept,
modify, reject) and over an unseen revision; a deferral stays pending so it
may still be withdrawn. A repeated withdrawal at the same rev is a no-op.
``reject`` keeps meaning substantive rejection; a reason may reference a
replacement insight without implying rejected substance.
        Evidence: prior record archived to
        ``insight-revisions/<id>.r<rev>.json`` with superseded markers;
        inbox generation advances only on meaningful change. Crash recovery:
        if a prior attempt archived r<rev> but crashed before the inbox
        write, a retry over the still-current inbox overwrites that orphan
        archive and converges. Failure: Denied on unknown ID, source
        mismatch, revision conflict, bounds, or a conflicting archive.
        """
        identifier(source)
        identifier(insight_id)
        if not isinstance(title, str) or not 1 <= len(title) <= 200 or not isinstance(body, str) or not body.strip():
            raise Denied("Insight revision requires a title (1–200 characters) and nonempty body")
        if base_snapshot is not None and (not isinstance(base_snapshot, str) or not DIGEST.fullmatch(base_snapshot)):
            raise Denied("base_snapshot must be a SHA-256 ID or null")
        if expected_rev is not None and (type(expected_rev) is not int or expected_rev < 1):
            raise Denied("expected_rev must be a positive integer")
        payload = {"id": insight_id, "source": source, "title": title, "body": body,
                   "base_snapshot": base_snapshot, "run": run}
        if len(canonical(payload)) > self.maximum:
            raise Denied("Insight exceeds byte limit")
        with lock(self.root / "locks" / "insights.lock"):
            path = self.root / "inbox" / f"{insight_id}.json"
            old = read_json(path)
            if not old:
                raise Denied("Insight not found")
            if old.get("source") != source:
                raise Denied("Only the original submitter may revise this insight")
            current_rev = _rev_of(old)
            if expected_rev is not None and expected_rev != current_rev:
                raise Denied("Insight revision conflict; reread the current revision")
            if (old.get("title") == title and old.get("body") == body
                    and old.get("base_snapshot") == base_snapshot):
                stored_payload = {k: old.get(k) for k in ("id", "source", "title", "body", "base_snapshot", "run")}
                try:
                    write_json(self.root / "insight-ids" / f"{insight_id}.json",
                               {"id": insight_id, "sha256": digest(canonical(stored_payload)),
                                "created_at": old.get("created_at")})
                except (OSError, ValueError, TypeError, AttributeError, Denied):
                    pass
                return _normalize(old)
            stamped = now()
            archive = {**old, "rev": current_rev,
                       "superseded_at": stamped, "superseded_by_rev": current_rev + 1}
            mkdir(self.root / "insight-revisions")
            archive_path = self.root / "insight-revisions" / f"{insight_id}.r{current_rev}.json"
            try:
                existing = None if archive_path.is_symlink() else read_json(archive_path)
            except (OSError, ValueError, TypeError, AttributeError, Denied):
                raise Denied("Insight revision archive conflict; reread the current revision")
            if existing is not None:
                # A completed revise advances the inbox, so an archive for
                # the still-current rev is an orphan from a crash between
                # the archive and inbox writes. Overwrite it only when it
                # archives this same inbox content; otherwise conflict.
                if (not isinstance(existing, dict) or existing.get("id") != insight_id
                        or _rev_of(existing) != current_rev
                        or any(existing.get(k) != old.get(k)
                               for k in ("source", "title", "body", "base_snapshot"))):
                    raise Denied("Insight revision archive conflict; reread the current revision")
                write_json(archive_path, archive)
            else:
                try:
                    write_json(archive_path, archive, exclusive=True)
                except Denied:
                    # Lost a same-rev race under the lock; reread to converge.
                    raise Denied("Insight revision conflict; reread the current revision")
            record = {"id": insight_id, "source": source, "title": title, "body": body,
                      "base_snapshot": base_snapshot, "run": run,
                      "created_at": old.get("created_at"), "rev": current_rev + 1,
                      "updated_at": stamped}
            write_json(path, record)
            sha = digest(canonical(payload))
            write_json(self.root / "insight-ids" / f"{insight_id}.json",
                       {"id": insight_id, "sha256": sha, "created_at": record["created_at"]})
            return record

    def history(self, insight_id: str) -> list[dict]:
        """Explicit audit retrieval for prior revisions; never in routine context.

        Schema: list of archived prior records ascending by rev, each with
        superseded markers. Bounds: at most 1000 entries, byte-bounded.
        Trust: local recorded state. Failure: Denied on unknown ID shape;
        empty list when no revisions exist (current via read()).
        """
        identifier(insight_id)
        out = []
        for path in sorted((self.root / "insight-revisions").glob(f"{insight_id}.r*.json")):
            if path.is_symlink():
                continue
            try:
                record = read_json(path)
                if not isinstance(record, dict) or record.get("id") != insight_id:
                    continue
                out.append(record)
            except (OSError, ValueError, TypeError, AttributeError, Denied):
                continue
            if len(out) >= 1000:
                break
        out.sort(key=lambda r: _rev_of(r))
        return out

    def projection(self, *, pending: bool = True, limit: int = 30) -> dict:
        heap = []
        total = 0
        for path in (self.root / "inbox").glob("*.json"):
            if path.is_symlink():
                continue
            try:
                item = read_json(path)
                identifier(item["id"])
                item_rev = _rev_of(item)
                decision_path = self.root / "decisions" / f"{item['id']}.json"
                raw_decision = None if decision_path.is_symlink() else read_json(decision_path)
                decision = _effective_decision(item_rev, raw_decision)
                if pending and decision and decision["action"] != "defer":
                    continue
                record = {k: item[k] for k in ("id", "source", "title", "created_at", "base_snapshot") if k in item}
                record["rev"] = item_rev
                if item.get("updated_at"):
                    record["updated_at"] = item["updated_at"]
                record["decision"] = decision
                key = (record["created_at"], record["id"], path.name)
                total += 1
                if limit > 0:
                    entry = (*key, record)
                    if len(heap) < limit:
                        heapq.heappush(heap, entry)
                    elif key > heap[0][:3]:
                        heapq.heapreplace(heap, entry)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue
        return {"items": [entry[3] for entry in sorted(heap)], "total": total, "truncated": total > len(heap)}

    def list(self, *, pending: bool = True, limit: int = 30) -> list[dict]:
        return self.projection(pending=pending, limit=limit)["items"]

    def read(self, insight_id: str) -> dict:
        path = self.root / "inbox" / f"{identifier(insight_id)}.json"
        if path.is_symlink():
            raise Denied("Insight not found")
        record = read_json(path)
        if not record:
            raise Denied("Insight not found")
        return _normalize(record)

    def generation(self) -> str:
        parts = []
        for path in (self.root / "inbox").glob("*.json"):
            if path.is_symlink():
                continue
            try:
                item = read_json(path)
                identifier(item["id"])
                parts.append(f"{item['id']}:{_rev_of(item)}:{_content_sha(item)}")
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue
        return digest(canonical(sorted(parts)))

    def decide(self, insight_id: str, action: str, reason: str, revisit: str, run: str) -> dict:
        if action not in ("accept", "modify", "defer", "reject") or not reason.strip():
            raise Denied("Decision requires a supported action and a reason")
        if action == "defer" and not revisit.strip():
            raise Denied("Deferred proposals require a revisit condition")
        with lock(self.root / "locks" / "insights.lock"):
            current = self.read(insight_id)
            rev = _rev_of(current)
            record = {"id": insight_id, "action": action, "reason": reason, "revisit": revisit,
                      "run": run, "created_at": now(), "rev": rev}
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
        return record

    def withdraw(self, insight_id: str, *, source: str, reason: str,
                 expected_rev: int | None = None, run: str | None = None) -> dict:
        """Close a pending proposal by author withdrawal, not rejection.

        Schema: nonempty reason; ``expected_rev`` (when given) must equal
        the current rev (compare-and-swap). Trust: ``source`` must equal
        the original submitter or be ``operator``; anything else is Denied,
        so the worker never withdraws others' submissions. Retry: a
        repeated withdrawal at the same rev returns the stored record
        unchanged. Evidence: rev-bound record with the withdrawing actor in
        ``decisions/`` plus an entry in ``decision-history/``; ordinary
        lists expose only the current disposition. Failure: Denied on
        unknown ID, unauthorized source, revision conflict, a
        revision-current substantive decision (withdrawal never replaces
        accept/modify/reject history), or empty reason. A deferral stays
        pending and may still be withdrawn; a later revision reopens the
        topic as pending.
        """
        identifier(source)
        identifier(insight_id)
        if not isinstance(reason, str) or not reason.strip():
            raise Denied("Withdrawal requires a reason")
        if expected_rev is not None and (type(expected_rev) is not int or expected_rev < 1):
            raise Denied("expected_rev must be a positive integer")
        with lock(self.root / "locks" / "insights.lock"):
            current = self.read(insight_id)
            if current.get("source") != source and source != "operator":
                raise Denied("Only the submitter or operator may withdraw this insight")
            rev = _rev_of(current)
            if expected_rev is not None and expected_rev != rev:
                raise Denied("Insight revision conflict; reread the current revision")
            raw = self.root / "decisions" / f"{insight_id}.json"
            existing = None if raw.is_symlink() else read_json(raw)
            effective = _effective_decision(rev, existing)
            if effective is not None and effective.get("action") != "defer":
                if effective.get("action") == "withdraw":
                    return effective
                raise Denied("Insight already decided; withdrawal cannot replace a substantive decision")
            record = {"id": insight_id, "action": "withdraw", "actor": source,
                      "reason": reason, "revisit": "", "run": run,
                      "created_at": now(), "rev": rev}
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
        return record

    def gc_decided(self, *, keep_days: int | None = None) -> int:
        """Remove long-decided proposals. Decisions persist separately in
        decisions/ and decision-history/; deferred and undecided stay.

        Schema/bounds: ``keep_days`` defaults to the operator-selected
        ``retention_days`` (``[limits] retention_days``, default 31, 0 disables).
        Trust: local operator state only. Retry: best-effort per file.
        Evidence: returns the reaped count. Failure: never raises for I/O.
        Stale approvals (decided rev differs from current rev) are pending
        re-evaluation and are never reaped. Revision audit in
        insight-revisions/ is durable like decisions and is never reaped here.
        """
        if keep_days is None:
            keep_days = self.retention_days
        if keep_days <= 0:
            return 0
        removed = 0
        cutoff = time.time() - keep_days * 86400
        with lock(self.root / "locks" / "insights.lock"):
            for path in sorted((self.root / "inbox").glob("*.json")):
                if path.is_symlink():
                    continue
                try:
                    decision = read_json(self.root / "decisions" / f"{path.stem}.json")
                    if not decision or decision.get("action") == "defer":
                        continue
                    item = read_json(path)
                    if not item:
                        continue
                    try:
                        decision_rev = int(decision.get("rev", 1))
                    except (TypeError, ValueError):
                        decision_rev = 1
                    if decision_rev != _rev_of(item):
                        continue
                    decided_at = dt.datetime.fromisoformat(decision["created_at"]).timestamp()
                    if decided_at > cutoff:
                        continue
                    payload = {k: item[k] for k in ("id", "source", "title", "body", "base_snapshot", "run")}
                    write_json(self.root / "insight-ids" / path.name,
                               {"id": item["id"], "sha256": digest(canonical(payload)), "created_at": item["created_at"]},
                               exclusive=True)
                except (OSError, ValueError, AttributeError, KeyError, TypeError, Denied):
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def ingest_editor(self, *, keep_days: int | None = None) -> dict:
        accepted = rejected = 0
        with lock(self.root / "locks" / "editor-ingest.lock"):
            spool = self.root / "spool" / "editor"
            claimed = self.root / ".ingest"
            mkdir(claimed)
            # Recover a claim interrupted before durable submission or quarantine.
            for path in sorted(spool.glob("*.json"))[:100]:
                target = claimed / path.name
                if not target.exists() and not target.is_symlink():
                    try:
                        os.rename(path, target)
                    except FileNotFoundError:
                        pass
            for path in sorted(claimed.glob("*.json"))[:100]:
                try:
                    identifier(path.stem)
                    record = json.loads(safe_read(claimed, path.name, self.maximum))
                    if set(record) != {"title", "body", "base_snapshot"}:
                        raise Denied("Invalid outbox fields")
                    self.submit(source="editor", insight_id=path.stem, **record)
                    path.unlink()
                    accepted += 1
                except (ValueError, TypeError, KeyError, Denied, LimitExceeded, OSError) as exc:
                    quarantine_id = uuid.uuid4().hex
                    mkdir(self.root / "rejected")
                    os.rename(path, self.root / "rejected" / (quarantine_id + ".item"))
                    write_json(self.root / "rejected" / f"{quarantine_id}.json",
                               {"name": path.name, "error": str(exc), "created_at": now()})
                    rejected += 1
            reaped = self.gc_decided(keep_days=keep_days)
        return {"accepted": accepted, "rejected": rejected, "reaped": reaped}
