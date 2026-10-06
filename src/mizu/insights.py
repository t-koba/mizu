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
import contextlib
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


#: Observable wait kinds a deferral may register. All are checkable at
#: dispatch from local recorded state, so waits survive restarts without
#: polling or model calls. Anything else is refused at registration with
#: this list, exposing the missing wake source instead of implying it.
WAIT_KINDS = ("deadline", "code_change", "insight_decided")


class Insights:
    def __init__(self, root: Path, maximum: int = PREVIEW_BYTES, retention_days: int = 31):
        self.root, self.maximum = root, maximum
        self.retention_days = retention_days
        #: Snapshot store for wait baselines; wired by Project, else None.
        self.snapshots = None

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
Withdrawal overlays any current decision without erasing it from the
audit and is refused only over an unseen revision; a repeated withdrawal at
the same rev is a no-op. Conversely no substantive decision may land on a
revision-current withdrawal: revise the topic to reopen it.
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

    def decide(self, insight_id: str, action: str, reason: str, revisit: str, run: str, wait=None) -> dict:
        if action not in ("accept", "modify", "defer", "reject") or not reason.strip():
            raise Denied("Decision requires a supported action and a reason")
        if action == "defer" and not revisit.strip():
            raise Denied("Deferred proposals require a revisit condition")
        registered = self._normalize_wait(wait, action)
        with lock(self.root / "locks" / "insights.lock"):
            current = self.read(insight_id)
            rev = _rev_of(current)
            raw = self.root / "decisions" / f"{insight_id}.json"
            prior = _effective_decision(rev, None if raw.is_symlink() else read_json(raw, {}))
            if prior is not None and prior.get("action") == "withdraw":
                raise Denied("Insight withdrawn; revise the topic to reopen it")
            record = {"id": insight_id, "action": action, "reason": reason, "revisit": revisit,
                      "run": run, "created_at": now(), "rev": rev}
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
                if registered is not None:
                    registered = {**registered, "owner": insight_id, "rev": rev,
                                  "decision": record, "registered_at": now(), "run": run}
                    mkdir(self.root / "waits")
                    write_json(self.root / "waits" / f"{insight_id}.json", registered)
        return record

    def _normalize_wait(self, wait, action: str) -> dict | None:
        """Validate a structured defer wait (fail closed, no prose parsing).

        Schema: None, or ``{"kind": ...}`` with kind-specific fields:
        ``deadline`` needs tz-aware ISO ``at``; ``code_change`` needs no
        fields (the current published digest is the baseline);
        ``insight_decided`` needs an ``insight`` id. Bounds: at most one
        wait per insight; unknown kinds, bad params, and waits on
        non-defer actions are Denied naming the supported set. Trust:
        recorded state only. Failure: Denied, never a silent downgrade
        to prose.
        """
        if wait is None:
            return None
        if action != "defer":
            raise Denied("Structured waits may only be registered with a deferral")
        if not isinstance(wait, dict):
            raise Denied(f"Unsupported wait; supported kinds: {', '.join(WAIT_KINDS)}")
        kind = wait.get("kind")
        if kind not in WAIT_KINDS:
            raise Denied(f"Unsupported wait; supported kinds: {', '.join(WAIT_KINDS)}")
        if kind == "deadline":
            at = wait.get("at")
            try:
                moment = dt.datetime.fromisoformat(at) if isinstance(at, str) else None
            except (ValueError, TypeError):
                moment = None
            if moment is None or moment.tzinfo is None:
                raise Denied("A deadline wait needs a timezone-aware ISO timestamp in 'at'")
            return {"kind": kind, "at": moment.isoformat(timespec="seconds")}
        if kind == "insight_decided":
            target = wait.get("insight")
            if not isinstance(target, str) or not target:
                raise Denied("An insight_decided wait needs an 'insight' id")
            identifier(target)
            return {"kind": kind, "target": target}
        if set(wait) - {"kind"}:
            raise Denied("A code_change wait takes no fields")
        try:
            baseline = self.project_snapshots_digest()
        except (OSError, ValueError, TypeError, AttributeError):
            raise Denied("A code_change wait needs a published snapshot baseline")
        return {"kind": kind, "digest": baseline}

    def project_snapshots_digest(self) -> str:
        """Current published code digest (wait baseline hook, never raises silently)."""
        store = self.snapshots
        if store is None:
            raise ValueError("No snapshot store")
        current = store.get()
        digest_value = current.get("code_digest") if isinstance(current, dict) else None
        if not isinstance(digest_value, str) or not digest_value:
            raise ValueError("No published code digest")
        return digest_value

    def wait_for(self, insight_id: str) -> dict | None:
        """Return the valid registered wait for one insight, else None (never raises)."""
        try:
            identifier(insight_id)
            stored = read_json(self.root / "waits" / f"{insight_id}.json", {})
        except (Denied, OSError, ValueError, TypeError, AttributeError):
            return None
        if not isinstance(stored, dict):
            return None
        try:
            current = self.read(insight_id)
        except (Denied, OSError, ValueError, TypeError, AttributeError):
            return None
        if not self._wait_bound(stored, current):
            return None
        return stored

    def _wait_bound(self, stored: dict, current: dict) -> bool:
        """True while a wait stays bound to its topic revision and decision."""
        try:
            if stored.get("rev") != _rev_of(current):
                return False
            raw = self.root / "decisions" / f"{current['id']}.json"
            if raw.is_symlink():
                return False
            return read_json(raw) == stored.get("decision")
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def due_waits(self, role_name: str, snapshot: dict, *, limit: int = 10) -> list:
        """Collect registered waits due for one role (one-shot, dispatch-lazy).

        Schema: returns oldest-registered-first payloads with insight, rev,
        title, body, kind, reason, revisit, and registered_at (plus ``at``
        for deadlines, ``target`` for insight_decided), at most ``limit``.
        Bounds: ``limit`` in [1, 100]. Trust: recorded decisions, inbox,
        and snapshots only; routing is ``insight.source == role_name``.
        Retry: read-only except best-effort pruning of waits unbound by
        revision, a new decision, or withdrawal. Evidence: delivery is
        logged under the run directory; consumed waits are removed on
        acknowledge. Failure: Denied on bad role name or limit; unbound,
        unmet, and moot (target withdrawn) waits never emit.
        """
        identifier(role_name)
        if type(limit) is not int or limit < 1 or limit > 100:
            raise Denied("Wait limit must be an integer in [1, 100]")
        directory = self.root / "waits"
        if not directory.is_dir() or directory.is_symlink():
            return []
        candidates = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                continue
            try:
                stored = read_json(path, {})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if not isinstance(stored, dict) or stored.get("kind") not in WAIT_KINDS:
                continue
            try:
                current = self.read(stored.get("owner", ""))
            except (Denied, OSError, ValueError, TypeError, AttributeError):
                continue
            if not self._wait_bound(stored, current):
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if current.get("source") != role_name or not self._wait_due(stored, snapshot):
                continue
            candidates.append((stored.get("registered_at", ""), stored, current))
            if len(candidates) >= limit:
                break
        due = []
        for _, stored, current in sorted(candidates, key=lambda item: item[0]):
            defer = stored.get("decision", {})
            if not isinstance(defer, dict):
                defer = {}
            payload = {"insight": current["id"], "rev": _rev_of(current),
                       "title": current.get("title", ""), "body": current.get("body", ""),
                       "kind": stored.get("kind"), "reason": defer.get("reason", ""),
                       "revisit": defer.get("revisit", ""),
                       "registered_at": stored.get("registered_at", "")}
            if stored.get("kind") == "deadline":
                payload["at"] = stored.get("at")
            if stored.get("kind") == "insight_decided":
                payload["target"] = stored.get("target")
            due.append(payload)
        return due

    def _wait_due(self, stored: dict, snapshot: dict) -> bool:
        """True when a bound wait's observable condition is met (never raises)."""
        try:
            kind = stored.get("kind")
            if kind == "deadline":
                moment = dt.datetime.fromisoformat(stored.get("at", ""))
                return dt.datetime.now(dt.timezone.utc) >= moment
            if kind == "code_change":
                current = snapshot.get("code_digest") if isinstance(snapshot, dict) else None
                return isinstance(current, str) and bool(current) and current != stored.get("digest")
            if kind == "insight_decided":
                target = self.root / "decisions" / f"{stored.get('target')}.json"
                if target.is_symlink():
                    return False
                record = read_json(target, {})
                if not isinstance(record, dict):
                    return False
                try:
                    inbox = self.read(record.get("id", ""))
                except (Denied, OSError, ValueError, TypeError, AttributeError):
                    return False
                if record.get("rev") != _rev_of(inbox):
                    return False
                return record.get("action") in ("accept", "modify", "reject")
            return False
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def consume_waits(self, role_name: str, events) -> int:
        """Remove acknowledged waits only if untouched since delivery.

        Schema: ``events`` are payloads from ``due_waits``. Trust: called
        only after successful processing. Retry: idempotent; a wait
        re-registered mid-run (newer ``registered_at``) is never removed.
        Evidence: returns the consumed count. Failure: Denied on bad role
        name; I/O failures remove nothing silently per wait.
        """
        identifier(role_name)
        consumed = 0
        for event in (events or []):
            if not isinstance(event, dict) or not isinstance(event.get("insight"), str):
                continue
            try:
                identifier(event["insight"])
                path = self.root / "waits" / f"{event['insight']}.json"
                if path.is_symlink():
                    continue
                stored = read_json(path, {})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if (isinstance(stored, dict)
                    and stored.get("registered_at") == event.get("registered_at")
                    and stored.get("kind") == event.get("kind")):
                with contextlib.suppress(OSError):
                    path.unlink()
                    consumed += 1
        return consumed

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
        unknown ID, unauthorized source, revision conflict, or empty
        reason. Withdrawal overlays any current decision without erasing
        it from ``decision-history/``; a later revision reopens the topic
        as pending.
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
            if effective is not None and effective.get("action") == "withdraw":
                return effective
            record = {"id": insight_id, "action": "withdraw", "actor": source,
                      "reason": reason, "revisit": "", "run": run,
                      "created_at": now(), "rev": rev}
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
        return record

    def decision_events(self, role_name: str, actions, *, limit: int = 10) -> list:
        """Collect unacknowledged decision events routed to one role.

        Schema: ``actions`` is the role's configured trigger list;
        returns oldest-first focused payloads
        ``{event, insight, rev, title, body, action, reason, decided_at}``
        (at most ``limit``). Bounds: ``limit`` is a positive int capped at
        100; cursors keep at most 100 tie-window ids. Trust: only
        recorded decisions and inbox state, never model input; routing is
        ``insight.source == role_name`` so one role's rejection never
        schedules another. Retry: read-only and idempotent; corrupt
        records are skipped, never invented. Evidence: the acknowledged
        cursor lives in ``decision-cursors/<role>.json``; dispatch logs
        delivered events under the run directory. Failure: Denied on bad
        role name or limit; missing inbox, superseded decisions,
        revision-stale records, and withdrawals never emit (a
        revision-current withdraw over a rejection means do not
        reanimate; a later revision reopens the topic through the normal
        pending list instead).
        """
        identifier(role_name)
        if type(limit) is not int or limit < 1 or limit > 100:
            raise Denied("Decision event limit must be an integer in [1, 100]")
        wanted = {a for a in (actions or ()) if isinstance(a, str)} - {"withdraw"}
        if not wanted:
            return []
        cursor_path = self.root / "decision-cursors" / f"{role_name}.json"
        try:
            cursor = read_json(cursor_path, {})
        except (OSError, ValueError, TypeError, AttributeError):
            cursor = {}
        ack_at = cursor.get("acknowledged_at") if isinstance(cursor, dict) else ""
        if not isinstance(ack_at, str):
            ack_at = ""
        acked = cursor.get("acknowledged") if isinstance(cursor, dict) else []
        acked = set(acked) if isinstance(acked, list) else set()
        history = self.root / "decision-history"
        candidates = []
        if history.is_dir() and not history.is_symlink():
            for path in history.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    record = read_json(path, {})
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("action") not in wanted:
                    continue
                created = record.get("created_at")
                if not isinstance(created, str) or not created:
                    continue
                if created < ack_at or (created == ack_at and path.stem in acked):
                    continue
                candidates.append((created, path.stem, record))
        candidates.sort()
        pending = []
        for created, stem, record in candidates:
            try:
                current = self.read(record.get("id", ""))
            except (Denied, OSError, ValueError, TypeError, AttributeError):
                continue
            if current.get("source") != role_name:
                continue
            rev = _rev_of(current)
            if not isinstance(record.get("rev"), int) or record["rev"] != rev:
                continue
            raw = self.root / "decisions" / f"{current['id']}.json"
            try:
                effective = _effective_decision(rev, None if raw.is_symlink() else read_json(raw))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if effective != record:
                continue
            pending.append({"event": stem, "insight": current["id"], "rev": rev,
                            "title": current.get("title", ""), "body": current.get("body", ""),
                            "action": record.get("action"), "reason": record.get("reason", ""),
                            "decided_at": created})
            if len(pending) >= limit:
                break
        return pending

    def acknowledge_decisions(self, role_name: str, events) -> dict:
        """Advance one role's decision-event cursor over delivered events.

        Schema: ``events`` are payloads from ``decision_events``. Trust:
        called only after successful processing, so busy/budget/restart
        redelivers instead of losing work. Retry: idempotent and forward
        only; an empty delivery leaves the cursor untouched. Evidence:
        the cursor file records ``acknowledged_at`` plus the tie-window
        ids at that timestamp. Failure: Denied on bad role name.
        """
        identifier(role_name)
        delivered = [e for e in (events or []) if isinstance(e, dict)
                     and isinstance(e.get("decided_at"), str) and isinstance(e.get("event"), str)]
        if not delivered:
            return self.decision_cursor(role_name)
        cursor = self.decision_cursor(role_name)
        ack_at = max([cursor.get("acknowledged_at") or ""] + [e["decided_at"] for e in delivered])
        acked = {e["event"] for e in delivered if e["decided_at"] == ack_at}
        if cursor.get("acknowledged_at") == ack_at and isinstance(cursor.get("acknowledged"), list):
            acked |= set(cursor["acknowledged"])
        if len(acked) > 100:
            acked = set(sorted(acked)[-100:])
        record = {"role": role_name, "acknowledged_at": ack_at,
                  "acknowledged": sorted(acked), "updated_at": now()}
        mkdir(self.root / "decision-cursors")
        write_json(self.root / "decision-cursors" / f"{role_name}.json", record)
        return record

    def decision_cursor(self, role_name: str) -> dict:
        """Return one role's acknowledged decision-event cursor (never raises)."""
        try:
            identifier(role_name)
            cursor = read_json(self.root / "decision-cursors" / f"{role_name}.json", {})
        except (Denied, OSError, ValueError, TypeError, AttributeError):
            return {"role": role_name, "acknowledged_at": "", "acknowledged": []}
        if not isinstance(cursor, dict):
            return {"role": role_name, "acknowledged_at": "", "acknowledged": []}
        ack_at = cursor.get("acknowledged_at", "")
        acked = cursor.get("acknowledged", [])
        return {"role": role_name,
                "acknowledged_at": ack_at if isinstance(ack_at, str) else "",
                "acknowledged": [i for i in acked if isinstance(i, str)][:100]}

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
