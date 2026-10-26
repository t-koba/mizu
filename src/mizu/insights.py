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

Current transport: the operator path exposes revise/history/withdraw
(``mizu insight revise/history/withdraw`` with source ``operator``); each
runtime role with the ``revise_insight``/``withdraw_insight`` capability may
revise/withdraw its own insights via the runtime, which fixes source to the
role name and binds to the read rev. A current accept/modify blocks withdrawal
unless the role also has the explicit ``withdraw_decided`` capability; the
operator path remains deny-by-default. The source-match check enforces the owner
boundary on every path. History stays operator/reader-side.
``run`` is revision provenance recorded only on meaningful change and never
wakes: an identical-content retry with a different ``run`` returns current
unchanged (stored ``run`` preserved) without bumping rev.

Attribution contract: ``source`` is the submitting authority channel
(operator CLI, ``vcs`` CI helper, ``editor`` outbox, or a role name fixed by
the runtime); ``origin`` is an opaque submitter-supplied label for the actual
origin (for example which trusted caller shared the operator transport).
``origin`` is ``None`` when unspecified; historical records without it read
as unknown, never guessed from titles. ``origin`` is set once at submit by
trusted host callers, preserved across revisions, never routed on, and never
grants operator approval. Model ``submit_insight`` exposes no origin field
and is stored with ``origin`` None; the Editor outbox accepts only
``title``/``body``/``base_snapshot`` so capsule content cannot forge origin.
"""
from __future__ import annotations

import json
import re
import base64
import binascii
import time
import contextlib
import datetime as dt
import heapq
import uuid
import os
from pathlib import Path

from .smoke_refs import record_lock, validate_refs, require_pairs
from . import insight_archive, wait_events
from .work_item_waits import WAIT_KINDS, normalize_wait
from .errors import Denied, LimitExceeded
from .work_item_seed import (make_seed, validate_scope, validate_seed, validate_tasks,
                             task_id, task_seeds)
from .fs import PREVIEW_BYTES, canonical, digest, identifier, lock, now, read_json, safe_read, write_json, mkdir, DIGEST

MAX_DECISION_EVENT_BYTES = 1024 * 1024
#: Bound for one per-token decision receipt file (1 KiB). Receipts are small
#: fixed-shape records; the bound keeps the acknowledgement check bounded.
DECISION_RECEIPT_BYTES = 1024
#: Bound for the number of per-token receipt diagnostics returned alongside
#: decision events. Diagnostics carry only safe identifiers and reason codes.
DECISION_RECEIPT_DIAGNOSTIC_LIMIT = 10
#: Reason codes for per-token receipt content invalidity. Only these bounded
#: codes ever leave the receipt check; receipt bytes are never surfaced.
DECISION_RECEIPT_REASONS = frozenset({"malformed_json", "invalid_receipt", "invalid_timestamp"})


class CorruptDecisionReceipt(Exception):
    """One safely-read per-token receipt has invalid content/semantics.

    Raised only after :func:`mizu.fs.safe_read` has returned the receipt
    bytes from a safely identified regular per-token file, so unsafe
    directories, symlinks, hardlinks, size-bound refusals and systemic I/O
    still raise their own errors and fail closed. Carries only safe
    identifiers (event id, token) and a bounded reason code, never receipt
    bytes or untrusted content. Callers isolate this per event; acknowledgement
    paths translate it back to a refusal.
    """

    def __init__(self, event: str, ack_token: str, reason: str):
        super().__init__(f"Corrupt decision receipt: {reason}")
        self.event = event
        self.ack_token = ack_token
        self.reason = reason


def _rev_of(record: dict) -> int:
    try:
        rev = int(record.get("rev", 1))
    except (TypeError, ValueError):
        return 1
    return rev if rev >= 1 else 1


def _decision_key(insight_id: str, rev: int) -> str:
    """Stable 128-bit event name, independent of action/intent and writer run."""
    return digest(canonical(["insight-substantive-decision", insight_id, rev]))[:32]


def _validate_keyed_substantive_event(event, insight_id: str, rev: int, key: str,
                                       title: str) -> dict:
    """Validate an immutable keyed event before an idempotent pointer repair."""
    if not isinstance(event, dict):
        raise Denied("Malformed keyed decision event")
    if (event.get("id") != insight_id or type(event.get("rev")) is not int
            or event.get("rev") != rev or event.get("decision_key") != key):
        raise Denied("Decision event ID collision")
    action = event.get("action")
    fields = {"id", "action", "reason", "revisit", "run", "created_at", "rev", "decision_key"}
    if action in ("accept", "modify"):
        fields.add("work_item_intent")
        intent = event.get("work_item_intent")
        if intent not in ("create", "none"):
            raise Denied("Malformed keyed work-item intent")
        if intent == "create":
            if "work_items" in event or "work_item_specs" in event:
                fields.update({"work_items", "work_item_specs"})
                if "work_item_waits" in event:
                    fields.add("work_item_waits")
            else:
                fields.add("work_item")
    elif action != "reject":
        raise Denied("Decision event ID collision")
    # A decision written before the accepted scope existed keeps the older
    # field set; a decision that states one carries the extra key.
    if "smoke_refs" in event:
        validate_refs(event["smoke_refs"])
        fields.add("smoke_refs")
    present = set(event)
    shape_ok = present == fields or (action in ("accept", "modify")
                                      and present == fields | {"scope"})
    if shape_ok and "scope" in event:
        validate_scope(event["scope"])
    stamp = event.get("created_at")
    try:
        parsed_stamp = dt.datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        parsed_stamp = None
    if (not shape_ok or event.get("id") != insight_id or type(event.get("rev")) is not int
            or event["rev"] != rev or event.get("decision_key") != key
            or not isinstance(event.get("reason"), str) or not event["reason"].strip()
            or not isinstance(event.get("revisit"), str)
            or not isinstance(event.get("run"), str) or not event["run"]
            or parsed_stamp is None or parsed_stamp.tzinfo is None
            or len(canonical(event)) > PREVIEW_BYTES):
        raise Denied("Malformed keyed decision event")
    if action in ("accept", "modify") and event["work_item_intent"] == "create":
        if "work_items" in event or "work_item_specs" in event:
            task_seeds(event)
            return event
        seed = event.get("work_item")
        validate_seed(seed)
        if seed != make_seed(f"wi_{key}", title, {"insight_id": insight_id, "rev": rev}, stamp,
                             **({"smoke_refs": event["smoke_refs"]} if "smoke_refs" in event else {}),
                             **({"description": event["reason"]} if "description" in seed else {}),
                             **({"scope": event.get("scope")} if "scope" in seed else {})):
            raise Denied("Malformed keyed work-item seed")
    return event


def _content_sha(record: dict) -> str:
    payload = {k: record.get(k) for k in ("id", "source", "title", "body", "base_snapshot")}
    payload["rev"] = _rev_of(record)
    return digest(canonical(payload))


def _evidence_sha(evidence) -> str:
    """Digest of one insight's evidence list (empty list reads as no evidence)."""
    try:
        items = list(evidence) if isinstance(evidence, list) else []
    except TypeError:
        items = []
    return digest(canonical(items))


def _check_origin(origin: str | None) -> str | None:
    """Validate an opaque origin label (None means unknown, never guessed)."""
    if origin is None:
        return None
    if (not isinstance(origin, str) or not 1 <= len(origin) <= 256
            or "\x00" in origin or "\n" in origin or not origin.strip()):
        raise Denied("Insight origin must be 1-256 chars without NUL/newlines")
    return origin


def _normalize(record: dict) -> dict:
    out = dict(record)
    out["rev"] = _rev_of(record)
    if "origin" not in out:
        out["origin"] = None
    if not isinstance(out.get("evidence"), list):
        out["evidence"] = []
    for entry in out["evidence"]:
        if isinstance(entry, dict) and "smoke_refs" in entry:
            validate_refs(entry["smoke_refs"])
            if type(entry.get("rev")) is not int or entry["rev"] != out["rev"]:
                raise Denied("Smoke evidence must bind its exact Insight revision")
    if not out.get("updated_at"):
        out["updated_at"] = out.get("created_at")
    return out


def _effective_decision(item_rev: int, decision) -> dict | None:
    """Current disposition for one inbox revision (None when undecided here).

    Every action binds to its reviewed rev, including defer: a meaningful
    revision reopens the topic as pending with no current decision, so a
    stale defer is never shown as the current disposition. The superseded
    record stays in decisions/history audit; readers needing the old gap
    use the explicit history path.
    """
    if not decision:
        return None
    try:
        decision_rev = int(decision.get("rev", 1))
    except (TypeError, ValueError):
        decision_rev = 1
    if decision_rev != item_rev:
        return None
    return decision


def _archive_summary(item: dict) -> dict:
    """Bounded archive page summary matching the live list shape.

    ``item`` is one archived row already merged with its archival
    ``decision``. Only identifiers, timestamps, the decision and evidence
    counts/timestamps are projected; ``body`` and full ``evidence`` text
    stay behind ``read``.
    """
    if item.get("body_unavailable") is True:
        return {k: item[k] for k in ("id", "rev", "created_at", "body_unavailable") if k in item}
    record = {k: item[k] for k in ("id", "source", "origin", "title", "created_at", "base_snapshot") if k in item}
    if "origin" not in record:
        record["origin"] = None
    record["rev"] = _rev_of(item)
    if item.get("updated_at"):
        record["updated_at"] = item["updated_at"]
    record["decision"] = item.get("decision")
    try:
        entries = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        record["evidence_count"] = len(entries)
        stamps = [e.get("created_at") for e in entries
                  if isinstance(e, dict) and isinstance(e.get("created_at"), str)]
        if stamps:
            record["evidence_updated_at"] = max(stamps)
    except (AttributeError, TypeError, ValueError):
        record["evidence_count"] = 0
    return record


def _wait_registration_id(stored: dict, fallback_owner: str = "") -> str | None:
    """Strict new UUID identity; bounded legacy digest only for absent IDs.

    Malformed explicit IDs are not legacy records. The legacy namespace hashes
    the exact stored registration, so neither unbounded timestamps nor unsafe
    path text become identities. Reading legacy state never rewrites it.
    """
    if not isinstance(stored, dict):
        return None
    if "registration_id" in stored:
        rid = stored["registration_id"]
        return rid if isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{32}", rid) else None
    owner = stored.get("owner", fallback_owner)
    try:
        identifier(owner)
    except Denied:
        return None
    return f"legacy:{owner}:{digest(canonical(stored))}"


#: Evidence-entry bounds (fixed mechanism, not knobs): at most this many
#: append-only entries per insight, each at most this many chars of text.
#: Entries carry small text plus receipt references; bulk belongs in the
#: referenced run evidence, never pasted into the entry.
MAX_EVIDENCE_ENTRIES = 16
MAX_EVIDENCE_CHARS = 8192


class Insights:
    def __init__(self, root: Path, maximum: int = PREVIEW_BYTES, retention_days: int = 31, *,
                 work_item_intent_default: str = "create", work_item_intent_by_source: dict | None = None,
                 cursor_ttl_seconds: int = 86400, open_work_items: int = 100,
                 producer_inputs_per_source: int = 20):
        self.root, self.maximum = root, maximum
        self.retention_days = retention_days
        self.open_work_items = open_work_items
        self.producer_inputs_per_source = producer_inputs_per_source
        self.cursor_ttl_seconds = cursor_ttl_seconds
        self.work_item_intent_default = work_item_intent_default
        self.work_item_intent_by_source = dict(work_item_intent_by_source or {})
        #: Snapshot store for wait baselines; wired by Project, else None.
        self.snapshots = None
        #: Shared seed preflight/storage service, wired by Project.
        self.work_items = None

    def _work_item_store(self):
        # Standalone Insights readers need no ledger; mutation gets the same
        # service without a module-level import cycle or a second lock.
        if self.work_items is None:
            from .work_items import WorkItems
            self.work_items = WorkItems(self.root, self.snapshots, self)
        return self.work_items

    def submit(self, *, source: str, title: str, body: str, base_snapshot: str | None,
               run: str | None = None, insight_id: str | None = None,
               origin: str | None = None) -> dict:
        """Submit a proposal with authority ``source`` and opaque ``origin``.

        ``origin`` labels the actual caller sharing the transport and never
        confers authority: approval, revise, withdraw and event routing read
        ``source``/``run`` only. ``None`` reads as unknown. ``run``/``origin``
        are provenance preserved from the first store: a live ID repeat with
        the same content (``id``/``source``/``title``/``body``/``base_snapshot``)
        but a different ``run``/``origin`` returns the stored record instead
        of raising, so stable IDs (for example CI dedup) never break on
        provenance alone.

        Auto-generated IDs (``insight_id`` omitted) also deduplicate by open
        content: an identical ``source``/``title``/``body`` with a live inbox
        record that is still open (undecided or deferred) returns the oldest
        stored record with ``replayed`` set to ``True`` on the response only.
        ``base_snapshot``/``run``/``origin`` are context, not identity, and
        never update the stored record. Closed (accepted/modified/rejected/
        withdrawn) or archive-only matches stay distinct, explicit IDs keep
        their identity semantics, and no ``replayed`` marker is persisted.
        """
        identifier(source)
        origin = _check_origin(origin)
        explicit = bool(insight_id)
        if explicit:
            insight_id = identifier(insight_id)
        if not isinstance(title, str) or not 1 <= len(title) <= 200 or not isinstance(body, str) or not body.strip():
            raise Denied("Insight requires a title (1–200 characters) and nonempty body")
        if base_snapshot is not None and (not isinstance(base_snapshot, str) or not DIGEST.fullmatch(base_snapshot)):
            raise Denied("base_snapshot must be a SHA-256 ID or null")
        probe_id = insight_id if explicit else "0" * 32
        probe = {"id": probe_id, "source": source, "title": title, "body": body,
                 "base_snapshot": base_snapshot, "run": run, "origin": origin}
        if len(canonical(probe)) > self.maximum:
            raise Denied("Insight exceeds byte limit")
        with record_lock(self.root):
            if not explicit:
                best = None
                best_key = None
                for key, _, item in self._live_entries(pending=True):
                    if (not isinstance(key[0], str) or item.get("source") != source
                            or item.get("title") != title or item.get("body") != body):
                        continue
                    if best_key is None or key < best_key:
                        best, best_key = item, key
                if best is not None:
                    out = _normalize(best)
                    out["replayed"] = True
                    return out
                insight_id = identifier(uuid.uuid4().hex)
            payload = {"id": insight_id, "source": source, "title": title, "body": body,
                       "base_snapshot": base_snapshot, "run": run, "origin": origin}
            if len(canonical(payload)) > self.maximum:
                raise Denied("Insight exceeds byte limit")
            path = self.root / "inbox" / f"{insight_id}.json"
            identity_path = self.root / "insight-ids" / f"{insight_id}.json"
            identity = read_json(identity_path)
            sha = digest(canonical(payload))
            old = read_json(path)
            if old is None:
                old = insight_archive.read(self.root, insight_id)
            if identity and identity.get("sha256") != sha:
                # A revised identity exists; direct submit with different
                # content is refused (use revise with expected_rev).
                # Idempotent retry with current content returns current below;
                # provenance alone (run/origin) never breaks the dedup.
                if old is not None:
                    current_payload = {k: old.get(k) if k != "origin" else old.get("origin") for k in payload}
                    if current_payload == payload:
                        return _normalize(old)
                    core = ("id", "source", "title", "body", "base_snapshot")
                    if all(old.get(k) == payload.get(k) for k in core):
                        return _normalize(old)
                raise Denied("Insight ID was reused with different content")
            if old is None and identity:
                return {**payload, "created_at": identity["created_at"], "rev": 1,
                        "updated_at": identity["created_at"], "retained": False}
            if old is None and (self.root / "decisions" / f"{insight_id}.json").exists():
                raise Denied("Decided ID has no retained identity; reuse refused")
            if old:
                core = ("id", "source", "title", "body", "base_snapshot")
                if any(old.get(k) != payload.get(k) for k in core):
                    raise Denied("Insight ID was reused with different content")
                if {k: old.get(k) if k != "origin" else old.get("origin") for k in payload} != payload:
                    # Same content, different provenance: preserve the first
                    # store instead of raising, keeping the identity bound to
                    # the stored record.
                    return _normalize(old)
                write_json(identity_path, {"id": insight_id, "sha256": sha, "created_at": old["created_at"]}, exclusive=True)
                return _normalize(old)
            record = {**payload, "created_at": now(), "rev": 1, "evidence": []}
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
        with record_lock(self.root):
            path = self.root / "inbox" / f"{insight_id}.json"
            old = read_json(path)
            if old is None:
                old = insight_archive.read(self.root, insight_id)
            _origin = _normalize(old).get("origin") if old else None
            payload = {"id": insight_id, "source": source, "title": title, "body": body,
                       "base_snapshot": base_snapshot, "run": run, "origin": _origin}
            if len(canonical(payload)) > self.maximum:
                raise Denied("Insight exceeds byte limit")
            if not old:
                raise Denied("Insight not found")
            if old.get("source") != source:
                raise Denied("Only the original submitter may revise this insight")
            current_rev = _rev_of(old)
            if expected_rev is not None and expected_rev != current_rev:
                raise Denied("Insight revision conflict; reread the current revision")
            if (old.get("title") == title and old.get("body") == body
                    and old.get("base_snapshot") == base_snapshot):
                stored_payload = {k: old.get(k) for k in ("id", "source", "title", "body", "base_snapshot", "run", "origin")}
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
                               for k in ("source", "title", "body", "base_snapshot", "origin"))):
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
                      "origin": _origin, "evidence": [],
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

    def attach_evidence(self, insight_id: str, *, source: str, text: str,
                        run: str | None = None,
                        expected_rev: int | None = None, smoke_refs=None) -> dict:
        """Append an attributed evidence entry to an open insight.

        Schema: ``text`` is 1-8192 chars of evidence prose plus receipt or
        run references (bulk belongs in the referenced run evidence, never
        pasted here); ``expected_rev`` must equal the current rev
        (compare-and-swap, like revise); at most ``MAX_EVIDENCE_ENTRIES``
        entries per insight. Bounds: entries are append-only and bounded;
        a full insight refuses further attaches until decided. Trust:
        ``source`` is the attaching authority channel (fixed by the runtime
        for model calls); the title/body/rev never change, so ownership and
        the revise/withdraw rights stay with the submitter only — this is
        not a revision. Retry: identical concurrent attaches each append
        under the lock; a stale ``expected_rev`` is Denied, never merged.
        Evidence: the entry ``{author, run, created_at, text}`` is stored on
        the inbox record (preserved with the rev on revise and in the permanent
        decided archive); the inbox
        generation advances so the decider wakes once. Failure: Denied on
        unknown ID, revision conflict, bounds, a full entry list, or a
        revision-current substantive decision or withdrawal (revise the topic
        to reopen it).
        """
        identifier(source)
        identifier(insight_id)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_EVIDENCE_CHARS:
            raise Denied(f"Evidence text must be 1-{MAX_EVIDENCE_CHARS} chars")
        if expected_rev is None:
            raise Denied("Evidence attach requires the revision read (expected_rev)")
        if isinstance(expected_rev, bool) or not isinstance(expected_rev, int) or expected_rev < 1:
            raise Denied("Evidence revision must be a positive revision number")
        with record_lock(self.root):
            current = self.read(insight_id)
            rev = _rev_of(current)
            if expected_rev != rev:
                raise Denied(f"Insight revised since read (now rev {rev}); re-read before attaching")
            raw = self.root / "decisions" / f"{insight_id}.json"
            effective = _effective_decision(rev, None if raw.is_symlink() else read_json(raw, {}))
            if effective is not None and effective.get("action") in ("accept", "modify", "reject"):
                raise Denied("Insight already decided; revise the topic to reopen it")
            if effective is not None and effective.get("action") == "withdraw":
                raise Denied("Insight withdrawn; revise the topic to reopen it")
            entries = list(current.get("evidence") or [])
            if len(entries) >= MAX_EVIDENCE_ENTRIES:
                raise Denied("Insight evidence is full; decide the topic to make room")
            entry = {"author": source, "run": run, "created_at": now(), "text": text}
            if smoke_refs is not None:
                require_pairs(self.root, smoke_refs)
                entry.update(smoke_refs=smoke_refs, rev=rev)
            entries.append(entry)
            record = {**current, "evidence": entries, "updated_at": entry["created_at"]}
            write_json(self.root / "inbox" / f"{insight_id}.json", record)
            return _normalize(record)

    def _live_entries(self, *, pending: bool = False, action: str | None = None):
        """Shared projection; keep the raw body for full-generation hashing."""
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
                if action is not None and (not decision or decision.get("action") != action):
                    continue
                record = {k: item[k] for k in ("id", "source", "origin", "title", "created_at", "base_snapshot") if k in item}
                if "origin" not in record:
                    record["origin"] = None
                record["rev"] = item_rev
                if item.get("updated_at"):
                    record["updated_at"] = item["updated_at"]
                record["decision"] = decision
                try:
                    entries = item.get("evidence") if isinstance(item.get("evidence"), list) else []
                    record["evidence_count"] = len(entries)
                    stamps = [e.get("created_at") for e in entries
                              if isinstance(e, dict) and isinstance(e.get("created_at"), str)]
                    if stamps:
                        record["evidence_updated_at"] = max(stamps)
                except (AttributeError, TypeError, ValueError):
                    record["evidence_count"] = 0
                key = (record["created_at"], record["id"], path.name)
                yield key, record, item
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue

    def projection(self, *, pending: bool = True, limit: int = 30, action: str | None = None) -> dict:
        heap = []
        total = 0
        for key, record, _ in self._live_entries(pending=pending, action=action):
            total += 1
            if limit > 0:
                entry = (*key, record)
                if len(heap) < limit:
                    heapq.heappush(heap, entry)
                elif key > heap[0][:3]:
                    heapq.heapreplace(heap, entry)
        return {"items": [entry[3] for entry in sorted(heap)], "total": total, "truncated": total > len(heap)}

    def list(self, *, pending: bool = True, limit: int = 30) -> list[dict]:
        return self.projection(pending=pending, limit=limit)["items"]

    def list_page(self, *, scope: str = "live", limit: int = 30, cursor: str | None = None) -> dict:
        """Explicit complete enumeration; cursors are opaque, replayable bookmarks.

        Live summaries match list(pending=False), ordered oldest first, with a
        generation binding every projected row's complete body and effective
        decision. Archive items use the same bounded summary shape (no body or
        evidence text), plus their original archival decision, ordered by
        monotonically increasing seq. All archive revisions are enumerated,
        not just each ID's latest row, plus migrated identity-only tombstones
        marked body_unavailable (no invented title/decision/revision).
        Use read for an available exact full body.
        No cursor is authorization; project access is the caller's responsibility.
        """
        if scope not in ("live", "archive"):
            raise Denied("Insight page scope must be live or archive")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise Denied("Insight page limit must be 1-1000")
        project = digest(str(self.root.resolve()).encode())
        stamp = int(time.time())
        state = self._page_cursor(cursor, scope, project, stamp) if cursor is not None else None
        expires = state["expires"] if state else stamp + self.cursor_ttl_seconds
        with lock(self.root / "locks" / "insights.lock"):
            if scope == "live":
                entries = sorted(self._live_entries(), key=lambda entry: entry[0])
                generation = digest(canonical([[key, record, raw] for key, record, raw in entries]))
                if state and state["generation"] != generation:
                    raise Denied("Stale insight live cursor; restart pagination")
                position = state["position"] if state else 0
                if position > len(entries):
                    raise Denied("Malformed insight page cursor")
                items = [record for _, record, _ in entries[position:position + limit]]
                position += len(items)
                more = position < len(entries)
                high_water = None
            else:
                full, high_water, position, more = insight_archive.page(
                    self.root, limit=limit, after=state["position"] if state else 0,
                    high_water=state["high_water"] if state else None)
                items = [_archive_summary(item) for item in full]
                generation = digest(canonical([project, scope, high_water]))
                if state and state["generation"] != generation:
                    raise Denied("Malformed insight page cursor")
        next_cursor = None
        if more:
            payload = {"v": 1, "scope": scope, "project": project, "generation": generation,
                       "expires": expires, "position": position, "high_water": high_water}
            body = canonical(payload)
            next_cursor = base64.urlsafe_b64encode(body).decode() + "." + digest(body)
        return {"items": items, "generation": generation, "next_cursor": next_cursor,
                "has_more": more,
                "cursor_expires_at": dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat()}

    @staticmethod
    def _page_cursor(cursor: str, scope: str, project: str, stamp: int) -> dict:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            encoded, checksum = cursor.split(".")
            body = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if digest(body) != checksum:
                raise ValueError
            state = json.loads(body)
            if canonical(state) != body:
                raise ValueError
            if (not isinstance(state, dict) or set(state) != {
                    "v", "scope", "project", "generation", "expires", "position", "high_water"}
                    or type(state["v"]) is not int or state["v"] != 1
                    or state["scope"] not in ("live", "archive")
                    or not isinstance(state["project"], str) or not DIGEST.fullmatch(state["project"])
                    or not isinstance(state["generation"], str) or not DIGEST.fullmatch(state["generation"])
                    or type(state["expires"]) is not int or not 0 <= state["expires"] <= 253402300799
                    or type(state["position"]) is not int or state["position"] < 0
                    or (state["scope"] == "live" and state["high_water"] is not None)
                    or (state["scope"] == "archive" and (type(state["high_water"]) is not int
                        or not 0 <= state["position"] <= state["high_water"] <= 9223372036854775807))):
                raise ValueError
        except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
            raise Denied("Malformed insight page cursor") from exc
        if state["scope"] != scope or state["project"] != project:
            raise Denied("Foreign scope or project insight page cursor")
        if stamp >= state["expires"]:
            raise Denied("Expired insight page cursor; restart pagination")
        return state

    def _read_exact(self, insight_id: str) -> dict | None:
        path = self.root / "inbox" / f"{insight_id}.json"
        if not path.is_symlink():
            record = read_json(path)
            if record:
                return _normalize(record)
        return insight_archive.read(self.root, insight_id)

    def _legacy_identity(self, insight_id: str) -> bool:
        """Validate a retained identity whose body was removed by old GC."""
        return insight_archive.identity(self.root, insight_id) is not None

    def read(self, insight_id: str) -> dict:
        """Read current content, including archive; hex prefixes must be unique.

        Exact IDs win over prefixes. Prefix ambiguity spans live and archived
        IDs (deduplicating crash leftovers). A retained identity without a body
        is reported explicitly; it is not confused with an unknown ID. Archive
        pages include migrated legacy tombstones; default lists stay live-only.
        """
        key = identifier(insight_id)
        record = self._read_exact(key)
        if record is not None:
            return record
        prefix = key.lower()
        matches = set()
        if len(prefix) < 32 and all(c in "0123456789abcdef" for c in prefix):
            matches.update(insight_archive.prefixes(self.root, prefix))
            for directory in (self.root / "inbox", self.root / "insight-ids"):
                for candidate in directory.glob("*.json"):
                    if (not candidate.is_symlink() and candidate.is_file()
                            and candidate.stem.lower().startswith(prefix)):
                        matches.add(candidate.stem)
                        if len(matches) > 1:
                            break
        if not matches:
            if self._legacy_identity(key):
                raise Denied("Insight body unavailable; legacy retention removed it before archival")
            raise Denied("Insight not found")
        if len(matches) > 1:
            raise Denied("Ambiguous insight prefix; use the full id")
        matched = matches.pop()
        record = self._read_exact(matched)
        if record is not None:
            return record
        if self._legacy_identity(matched):
            raise Denied("Insight body unavailable; legacy retention removed it before archival")
        raise Denied("Insight not found")

    def scan_source(self, source: str) -> list[dict]:
        """Return every inbox record carrying the given source.

        Normalized records, malformed files skipped: the same tolerant
        enumeration as ``generation``. Callers filter further by id
        shape; a source alone never implies a record kind.
        """
        out = []
        for path in (self.root / "inbox").glob("*.json"):
            if path.is_symlink():
                continue
            try:
                item = read_json(path)
                identifier(item["id"])
                if item.get("source") == source:
                    out.append(_normalize(item))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue
        return out

    def unresolved_counts(self, sources) -> dict:
        """Count live unresolved inputs for each named source, fail-closed.

        One bounded inbox scan under the Insights record lock for a
        consistent snapshot; undecided plus deferred revisions count,
        resolved and archive-only history do not. Each inbox row is safely
        read within ``self.maximum`` and each decision within the decision
        event bound. Unsafe, unreadable, vanished or malformed live entries
        raise rather than undercount; a missing decision only leaves its
        input unresolved. Unknown or malformed requested sources raise.
        Tolerant prompt/list paths (``_live_entries``, ``generation``,
        ``list``) are unchanged.
        """
        names = list(sources)
        for name in names:
            identifier(name)
        wanted = set(names)
        counts = {name: 0 for name in wanted}
        with record_lock(self.root):
            inbox = self.root / "inbox"
            if not insight_archive._directory(inbox):
                return counts
            # iterdir propagates listing failures rather than glob's tolerant
            # handling. Do not skip a vanished row: its source is unknown.
            for path in sorted(inbox.iterdir()):
                if path.suffix != ".json":
                    continue
                item = json.loads(safe_read(self.root, f"inbox/{path.name}", self.maximum))
                if not isinstance(item, dict):
                    raise Denied("Malformed live insight record")
                identifier(item.get("id"))
                if item["id"] != path.stem:
                    raise Denied("Live insight filename does not match ID")
                # Every live record carries a valid source and revision;
                # otherwise admission must hold rather than guess.
                identifier(item.get("source"))
                item_rev = item.get("rev", 1)
                if type(item_rev) is not int or item_rev < 1:
                    raise Denied("Malformed live insight revision")
                decision_path = self.root / "decisions" / path.name
                try:
                    decision_path.lstat()
                except FileNotFoundError:
                    # No decision cannot resolve (or hide) this input.
                    decision = None
                else:
                    decision = json.loads(safe_read(
                        self.root, f"decisions/{path.name}", MAX_DECISION_EVENT_BYTES))
                    if (not isinstance(decision, dict) or decision.get("id") != item["id"]
                            or decision.get("action") not in ("accept", "modify", "reject", "defer", "withdraw")):
                        raise Denied("Malformed live insight decision")
                    decision_rev = decision.get("rev", 1)
                    if type(decision_rev) is not int or decision_rev < 1:
                        raise Denied("Malformed live insight decision revision")
                    if decision_rev != item_rev:
                        decision = None
                if decision is not None and decision["action"] != "defer":
                    continue
                source = item["source"]
                if source in wanted:
                    counts[source] += 1
        return counts

    def unresolved_count(self, source: str) -> int:
        """Single-source wrapper over :meth:`unresolved_counts`.

        Same fail-closed single scan; kept for single-producer callers.
        """
        return self.unresolved_counts([source])[source]

    def generation(self) -> str:
        parts = []
        for path in (self.root / "inbox").glob("*.json"):
            if path.is_symlink():
                continue
            try:
                item = read_json(path)
                identifier(item["id"])
                parts.append(f"{item['id']}:{_rev_of(item)}:{_content_sha(item)}:{_evidence_sha(item.get('evidence'))}")
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue
        return digest(canonical(sorted(parts)))

    def decide(self, insight_id: str, action: str, reason: str, revisit: str, run: str, wait=None,
               expected_rev: int | None = None, work_item_intent: str | None = None,
               scope: str | None = None, work_items: list | None = None) -> dict:
        """Record one decision, optionally stating the accepted scope.

        An accepted scope is the work the decision authorizes. It is recorded
        at decision time, inherited by every later revision of the work item
        and bound by its closure; a change of it is its own recorded event.
        A decision that states no scope leaves the item without one, the
        legacy shape that stays readable history. Explicit tasks are stored
        together with their seeds in one atomic manifest, never separate
        commits; no accepted subset can become visible.
        """
        if work_items is not None:
            validate_tasks(work_items)
            if action not in ("accept", "modify") or work_item_intent == "none":
                raise Denied("Tasks require an accept/modify create decision")
        if work_item_intent is not None and (work_item_intent not in ("create", "none")
                                               or action not in ("accept", "modify")):
            raise Denied("work_item_intent must be create or none, only for accept/modify")
        if scope is not None:
            if action not in ("accept", "modify"):
                # Defer and reject authorize no work, so they store no scope.
                scope = None
            else:
                validate_scope(scope)
        intent_override = work_item_intent
        if action not in ("accept", "modify", "defer", "reject") or not reason.strip():
            raise Denied("Decision requires a supported action and a reason")
        if action == "defer" and not revisit.strip():
            raise Denied("Deferred proposals require a revisit condition")
        if expected_rev is None:
            raise Denied("Decision requires the revision read (expected_rev)")
        if isinstance(expected_rev, bool) or not isinstance(expected_rev, int) or expected_rev < 1:
            raise Denied("Decision revision must be a positive revision number")
        registered = self._normalize_wait(wait, action)
        with record_lock(self.root):
            store = self._work_item_store()
            store.prepare_decision_write(need_wait=False)
            current = self.read(insight_id)
            rev = _rev_of(current)
            if expected_rev is not None and expected_rev != rev:
                raise Denied(f"Insight revised since read (now rev {rev}); re-read before deciding")
            raw = self.root / "decisions" / f"{insight_id}.json"
            current_decision = None if raw.is_symlink() else read_json(raw, {})
            prior = _effective_decision(rev, current_decision)
            if prior is not None and prior.get("action") == "withdraw":
                raise Denied("Insight withdrawn; revise the topic to reopen it")
            key = _decision_key(insight_id, rev)
            event_path = self.root / "decision-history" / f"{key}.json"
            if event_path.exists() or event_path.is_symlink():
                existing = json.loads(safe_read(self.root, f"decision-history/{key}.json", PREVIEW_BYTES))
                existing = _validate_keyed_substantive_event(existing, insight_id, rev, key,
                                                              current["title"])
                if (existing["action"] != action or existing["reason"] != reason
                        or existing["revisit"] != revisit
                        or existing.get("work_item_specs") != work_items
                        or (scope is not None and existing.get("scope") != scope)
                        or (intent_override is not None and action in ("accept", "modify")
                            and existing["work_item_intent"] != intent_override)):
                    raise Denied("Insight already decided at this revision; revise the topic to reopen it")
                if (prior is not None and prior.get("action") in ("accept", "modify", "reject")
                        and prior != existing):
                    raise Denied("Conflicting substantive decision at this revision")
                seeds = (existing["work_items"] if "work_items" in existing else
                         [existing["work_item"]] if "work_item" in existing else [])
                for seed in seeds:
                    store.validate_seed_replay(seed, seed_event=existing)
                # History committed but its current pointer may not have. Return the
                # original event (including its run) and repair only a stale pointer.
                if current_decision != existing:
                    store.prepare_decision_write(need_wait=False)
                    with lock(self.root / "locks" / "decisions.lock"):
                        write_json(raw, existing)
                return existing
            if prior is not None and prior.get("action") in ("accept", "modify", "reject"):
                raise Denied("Insight already decided at this revision; revise the topic to reopen it")
            if action in ("accept", "modify"):
                work_item_intent = (intent_override if intent_override is not None else
                                    self.work_item_intent_by_source.get(current["source"],
                                                                      self.work_item_intent_default))
                if work_items is not None and work_item_intent != "create":
                    raise Denied("Tasks conflict with configured no-work intent; specify create explicitly")
                if work_item_intent not in ("create", "none"):
                    raise Denied("Invalid configured work-item intent")
            # Only pre-key writers could hide a random-ID substantive event behind
            # a defer. Scan this legacy edge, not fresh decisions or keyed defers.
            if prior is not None and prior.get("action") == "defer" and "decision_key" not in prior:
                for path in (self.root / "decision-history").glob("*.json"):
                    event = json.loads(safe_read(self.root, f"decision-history/{path.name}", PREVIEW_BYTES))
                    if not isinstance(event, dict):
                        raise Denied("Malformed decision history")
                    if (event.get("id") == insight_id and _effective_decision(rev, event) is not None
                            and event.get("action") in ("accept", "modify", "reject")):
                        raise Denied("Insight already decided at this revision; revise the topic to reopen it")
            event_id = key if action in ("accept", "modify", "reject") else uuid.uuid4().hex
            stamped = now()
            inherited_refs = sorted({ref for entry in current.get("evidence", [])
                                     for ref in validate_refs(entry.get("smoke_refs", []))})
            validate_refs(inherited_refs)
            record = {"id": insight_id, "action": action, "reason": reason, "revisit": revisit,
                      "run": run, "created_at": stamped, "rev": rev, "decision_key": key}
            if action in ("accept", "modify"):
                record["work_item_intent"] = work_item_intent
                if scope is not None:
                    record["scope"] = scope
            if action in ("accept", "modify") and len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Decision event with work-item intent exceeds byte bound")
            if registered is not None:
                require_pairs(self.root, registered.get("smoke_refs", []))
            if inherited_refs and action in ("accept", "modify"):
                record["smoke_refs"] = inherited_refs
            if work_items is not None:
                normalized_waits = [self._normalize_wait(task.get("wait"), "defer")
                                    for task in work_items]
                record["work_item_specs"] = work_items
                if any(wait is not None for wait in normalized_waits):
                    # Persist a separate normalized manifest so generated
                    # baselines (notably code_change) are bound to each seed.
                    record["work_item_waits"] = normalized_waits
                record["work_items"] = [store.create_seed(
                    task_id(key, work_items, index), task["title"],
                    {"insight_id": insight_id, "rev": rev}, stamped, decision_key=key,
                    task_specs=work_items, task_index=index, description=task["description"],
                    wait=normalized_waits[index], smoke_refs=inherited_refs or None,
                    **({"scope": scope} if scope is not None else {}))
                    for index, task in enumerate(work_items)]
            elif work_item_intent == "create":
                record["work_item"] = store.create_seed(
                    f"wi_{event_id}", current["title"], {"insight_id": insight_id, "rev": rev},
                    stamped, decision_key=key, description=reason, smoke_refs=inherited_refs or None,
                    **({"scope": scope} if scope is not None else {}))
            if action in ("accept", "modify") and len(canonical(record)) > PREVIEW_BYTES:
                raise LimitExceeded("Decision event with work-item intent exceeds byte bound")
            seeds = record.get("work_items", [record["work_item"]] if "work_item" in record else [])
            store.require_open_capacity(len(seeds))
            if work_items is not None:
                _validate_keyed_substantive_event(record, insight_id, rev, key, current["title"])
            store.prepare_decision_write(need_wait=(registered is not None))
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{event_id}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
                if registered is not None:
                    registered = {**registered, "owner": insight_id, "rev": rev,
                                  "decision": record, "registered_at": now(), "run": run,
                                  "registration_id": uuid.uuid4().hex}
                    mkdir(self.root / "waits")
                    write_json(self.root / "waits" / f"{insight_id}.json", registered)
        return record

    def _normalize_wait(self, wait, action: str) -> dict | None:
        return normalize_wait(wait, action, self.project_snapshots_digest)

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
        title, body, kind, reason, revisit, registered_at, registration_id and
        a role/source/payload-bound ack_token (plus ``at`` for deadlines,
        ``target`` for insight_decided), at most ``limit``.
        Bounds: ``limit`` in [1, 100]; registration reads and token-bearing
        rows at most 1 MiB. Trust: recorded decisions, inbox,
        and snapshots only; routing is ``insight.source == role_name``.
        Retry: read-only except best-effort pruning of waits unbound by
        revision, a new decision, or withdrawal. Evidence: delivery is
        logged under the run directory; consumed waits are removed on
        explicit acknowledgement after durable result/publication. Failure:
        Denied on bad role name or limit; unbound, unmet, and moot (target
        withdrawn) waits never emit; unsafe/oversized reads fail closed.
        """
        identifier(role_name)
        if type(limit) is not int or limit < 1 or limit > 100:
            raise Denied("Wait limit must be an integer in [1, 100]")
        directory = self.root / "waits"
        if not directory.is_dir() or directory.is_symlink():
            return []

        def _eligible():
            for path in directory.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    stored = json.loads(safe_read(self.root, path.relative_to(self.root).as_posix(),
                                                  wait_events.MAX_ROW_BYTES))
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
                registered_at = stored.get("registered_at", "")
                if not isinstance(registered_at, str):
                    registered_at = ""
                rid = _wait_registration_id(stored, path.stem)
                if rid is None:
                    continue
                yield ((registered_at, rid), stored, current)

        selected = heapq.nsmallest(limit, _eligible(), key=lambda item: item[0])
        due = []
        for _, stored, current in selected:
            defer = stored.get("decision", {})
            if not isinstance(defer, dict):
                defer = {}
            payload = {"insight": current["id"], "rev": _rev_of(current),
                       "title": current.get("title", ""), "body": current.get("body", ""),
                       "kind": stored.get("kind"), "reason": defer.get("reason", ""),
                       "revisit": defer.get("revisit", ""),
                       "registered_at": stored.get("registered_at", ""),
                       "registration_id": _wait_registration_id(stored, current.get("id", ""))}
            if stored.get("kind") == "deadline":
                payload["at"] = stored.get("at")
            if stored.get("kind") == "insight_decided":
                payload["target"] = stored.get("target")
            if stored.get("kind") == "dependency":
                payload["recipient"] = stored.get("recipient")
                payload["requires"] = stored.get("requires")
                evidence = self._satisfaction_evidence(stored)
                if evidence is None:
                    # A satisfaction claim without current result
                    # evidence never dispatches; retry at next dispatch.
                    continue
                payload["result"] = evidence
            due.append(wait_events.delivery(role_name, wait_events.WAIT, payload))
        return due

    def _satisfaction_evidence(self, stored: dict) -> dict | None:
        """Current result evidence for a satisfied dependency (None when unreadable)."""
        try:
            requires = stored.get("requires")
            if not isinstance(requires, dict):
                return None
            result = self.read(requires.get("insight", ""))
            raw = self.root / "decisions" / f"{result['id']}.json"
            if raw.is_symlink():
                return None
            record = read_json(raw, {})
            if not isinstance(record, dict):
                return None
            if record.get("rev") != _rev_of(result):
                return None
            decided_at = record.get("created_at")
            if not isinstance(decided_at, str) or not decided_at:
                return None
            return {"insight": result["id"], "rev": _rev_of(result),
                    "action": record.get("action"), "decided_at": decided_at,
                    "title": result.get("title", "")}
        except (Denied, OSError, ValueError, TypeError, AttributeError):
            return None

    def pending_obligations(self, role_name: str, *, limit: int = 10) -> list:
        """List bound-but-unsatisfied dependency waits addressed to one role.

        Schema: oldest-registered-first payloads with insight, rev, title,
        body, recipient, requires, reason, revisit, registered_at and
        registration_id and ack_token (at most ``limit``). These make an explicit
        recipient's obligation visible when the recipient runs for other
        reasons; they never admit a run on their own, so no new wake
        source or scheduler is implied: scheduling the fulfiller stays
        operator configuration.
        Bounds: ``limit`` in [1, 100]. Trust: recorded decisions and inbox
        only. Retry: read-only except best-effort pruning of unbound
        waits, mirroring ``due_waits`` so restart recovery keeps the same
        pending set. Exact per-role receipts filter before the bounded heap;
        dispatch logs a delivered copy under the run directory. Failure:
        corrupt/unsafe receipts fail closed without repair; bad roles/limits
        are Denied; satisfied, unbound, malformed and receipted waits never list.
        Registration reads and token-bearing rows are bounded to 1 MiB.
        """
        identifier(role_name)
        if type(limit) is not int or limit < 1 or limit > 100:
            raise Denied("Obligation limit must be an integer in [1, 100]")
        directory = self.root / "waits"
        if not directory.is_dir() or directory.is_symlink():
            return []

        def _eligible():
            for path in directory.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    stored = json.loads(safe_read(self.root, path.relative_to(self.root).as_posix(),
                                                  wait_events.MAX_ROW_BYTES))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
                if not isinstance(stored, dict) or stored.get("kind") != "dependency":
                    continue
                if stored.get("recipient") != role_name:
                    continue
                try:
                    current = self.read(stored.get("owner", ""))
                except (Denied, OSError, ValueError, TypeError, AttributeError):
                    continue
                if not self._wait_bound(stored, current):
                    with contextlib.suppress(OSError):
                        path.unlink()
                    continue
                if not isinstance(stored.get("requires"), dict):
                    continue
                if self._wait_due(stored, None):
                    # Satisfied obligations belong to the requester's
                    # completion wake, not the recipient's pending list.
                    continue
                registered_at = stored.get("registered_at", "")
                if not isinstance(registered_at, str):
                    registered_at = ""
                rid = _wait_registration_id(stored, path.stem)
                if rid is None:
                    continue
                row = self._obligation_payload(stored, current)
                row = wait_events.delivery(role_name, wait_events.OBLIGATION, row)
                if not wait_events.received(self.root, role_name, row):
                    yield ((registered_at, rid), row)

        selected = heapq.nsmallest(limit, _eligible(), key=lambda item: item[0])
        return [row for _, row in selected]

    def _obligation_payload(self, stored, current):
        """Frozen projection shared by delivery and exact registration receipts."""
        defer = stored.get("decision", {})
        if not isinstance(defer, dict):
            defer = {}
        return {"insight": current["id"], "rev": _rev_of(current),
                "title": current.get("title", ""), "body": current.get("body", ""),
                "recipient": stored.get("recipient"), "requires": stored.get("requires"),
                "reason": defer.get("reason", ""), "revisit": defer.get("revisit", ""),
                "registered_at": stored.get("registered_at", ""),
                "registration_id": _wait_registration_id(stored, current.get("id", ""))}

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
            if kind == "dependency":
                requires = stored.get("requires")
                if not isinstance(requires, dict):
                    return False
                try:
                    result = self.read(requires.get("insight", ""))
                except (Denied, OSError, ValueError, TypeError, AttributeError):
                    return False
                if _rev_of(result) != requires.get("rev"):
                    # A moved result is stale against the assessed
                    # requirement: never satisfy, wait for reassessment.
                    return False
                raw = self.root / "decisions" / f"{result['id']}.json"
                if raw.is_symlink():
                    return False
                record = read_json(raw, {})
                if not isinstance(record, dict):
                    return False
                return (record.get("rev") == _rev_of(result)
                        and record.get("action") == requires.get("action"))
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
        """Release consumed wait pins under global-before-record coordination."""
        with record_lock(self.root):
            return self._consume_waits(role_name, events)

    def _consume_waits(self, role_name: str, events) -> int:
        """Remove acknowledged waits only if untouched since delivery.

        Schema: ``events`` are payloads from ``due_waits``. Trust: called
        only after successful processing. Retry: idempotent; a wait
        re-registered mid-run (different ``registration_id``) is never removed.
        Evidence: returns the consumed count. Failure: Denied on bad role
        name; I/O failures remove nothing silently per wait.
        """
        identifier(role_name)
        for event in (events or []):
            wait_events.validate(role_name, wait_events.WAIT, event)
        consumed = 0
        for event in (events or []):
            if not isinstance(event, dict) or not isinstance(event.get("insight"), str):
                continue
            try:
                identifier(event["insight"])
                path = self.root / "waits" / f"{event['insight']}.json"
                if path.is_symlink():
                    continue
                path.lstat()  # Missing after a prior acknowledgement is an idempotent replay.
                stored = json.loads(safe_read(self.root, path.relative_to(self.root).as_posix(),
                                              wait_events.MAX_ROW_BYTES))
                current = self.read(event["insight"])
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if (isinstance(stored, dict) and current.get("source") == role_name
                    and event.get("registration_id") is not None
                    and _wait_registration_id(stored, event["insight"]) == event["registration_id"]):
                with contextlib.suppress(OSError):
                    path.unlink()
                    consumed += 1
        return consumed

    def withdraw(self, insight_id: str, *, source: str, reason: str,
                 expected_rev: int | None = None, run: str | None = None,
                 allow_decided: bool = False) -> dict:
        """Close an insight by author withdrawal, not substantive rejection.

        Schema: nonempty reason; ``expected_rev`` (when given) must equal
        the current rev (compare-and-swap). Trust: ``source`` must equal
        the original submitter or be ``operator``; anything else is Denied,
        so the worker never withdraws others' submissions. Retry: a
        repeated withdrawal at the same rev returns the stored record
        unchanged. ``allow_decided`` is an explicit supplemental permission
        supplied by runtime callers, never inferred from ``source``; its
        default is false. Evidence: rev-bound record with the withdrawing
        actor in ``decisions/`` plus an entry in ``decision-history/``;
        ordinary lists expose only the current disposition. Failure: Denied
        on unknown ID, unauthorized source, revision conflict, empty reason,
        invalid permission type, or a current accept/modify unless
        ``allow_decided`` is true. Pending, deferred, rejected, and insights
        whose prior decision belongs to a superseded revision remain
        withdrawable. A permitted withdrawal appends history
        without deleting earlier decisions; a later revision reopens the topic
        as pending.
        """
        identifier(source)
        identifier(insight_id)
        if not isinstance(reason, str) or not reason.strip():
            raise Denied("Withdrawal requires a reason")
        if expected_rev is not None and (type(expected_rev) is not int or expected_rev < 1):
            raise Denied("expected_rev must be a positive integer")
        if type(allow_decided) is not bool:
            raise Denied("allow_decided must be a boolean permission")
        with record_lock(self.root):
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
            if (effective is not None and effective.get("action") in {"accept", "modify"}
                    and not allow_decided):
                action = effective["action"]
                raise Denied(f"Cannot withdraw insight: current {action} decision blocks withdrawal")
            record = {"id": insight_id, "action": "withdraw", "actor": source,
                      "reason": reason, "revisit": "", "run": run,
                      "created_at": now(), "rev": rev}
            self._work_item_store().prepare_decision_write(need_wait=False)
            with lock(self.root / "locks" / "decisions.lock"):
                write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
                write_json(self.root / "decisions" / f"{insight_id}.json", record)
        return record

    def _decision_token(self, role_name: str, payload: dict) -> str:
        """Bind a host token to the role, immutable decision and frozen payload."""
        identifier(role_name)
        event = identifier(payload["event"])
        record = json.loads(safe_read(self.root, f"decision-history/{event}.json", MAX_DECISION_EVENT_BYTES))
        return digest(canonical({"role": role_name, "decision": record,
                                 "payload": {k: v for k, v in payload.items() if k != "ack_token"}}))

    def _decision_received(self, role_name: str, event: dict) -> bool:
        """True once a valid per-token receipt exists; False when absent.

        A safely read per-token file whose content or semantics are invalid
        (malformed JSON, wrong shape/binding, bad timestamp) raises
        :class:`CorruptDecisionReceipt` so the caller can isolate that one
        event without suppressing unrelated valid events. Unsafe paths,
        symlinks, hardlinks, size-bound refusals and systemic I/O propagate
        unchanged and still fail closed. Corrupt bytes are never altered.
        """
        token = event["ack_token"]
        parent = self.root / "decision-receipts" / role_name
        if not insight_archive._directory(parent.parent) or not insight_archive._directory(parent):
            return False
        path = parent / f"{token}.json"
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        # Fail closed on unsafe/syscall failures; only content errors below
        # become an isolatable per-event diagnostic.
        raw = safe_read(self.root, path.relative_to(self.root).as_posix(), DECISION_RECEIPT_BYTES)
        try:
            receipt = json.loads(raw)
        except ValueError as exc:
            raise CorruptDecisionReceipt(event["event"], token, "malformed_json") from exc
        if not isinstance(receipt, dict):
            raise CorruptDecisionReceipt(event["event"], token, "invalid_receipt")
        try:
            shape_ok = (set(receipt) == {"role", "event", "ack_token", "acknowledged_at"}
                        and receipt["role"] == role_name and receipt["event"] == event["event"]
                        and receipt["ack_token"] == token)
            stamp = receipt.get("acknowledged_at")
            try:
                parsed = dt.datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
            except ValueError as exc:
                raise CorruptDecisionReceipt(event["event"], token, "invalid_timestamp") from exc
        except CorruptDecisionReceipt:
            raise
        except (TypeError, KeyError, AttributeError) as exc:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_receipt") from exc
        if not shape_ok:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_receipt")
        try:
            stamp_ok = parsed is not None and parsed.tzinfo is not None
        except (TypeError, AttributeError) as exc:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_timestamp") from exc
        if parsed is None:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_timestamp")
        if parsed.tzinfo is None:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_timestamp")
        if not stamp_ok:
            raise CorruptDecisionReceipt(event["event"], token, "invalid_timestamp")
        return True

    def decision_events(self, role_name: str, actions, *, limit: int = 10, sources: str = "own") -> list:
        """Return globally oldest eligible, unreceived decisions (timestamp/id).

        Legacy completion cursors prove no handling and are ignored. Exact
        receipts are role/payload-bound, never watermarks. A bounded heap keeps
        at most limit rows (1..100); each history/payload read is at most 1 MiB.
        Own-source deferrals still only ride along; no rotation acknowledges
        them. Superseded, withdrawn and revision-stale decisions never emit.
        Malformed history records are skipped individually. One safely read
        per-token receipt with invalid content/semantics (malformed JSON,
        wrong shape/binding, bad timestamp) isolates that event: it stays
        undelivered and unacknowledged, its bytes are left unchanged, and a
        bounded diagnostic is returned by
        :meth:`decision_events_with_diagnostics` while unrelated valid events
        still deliver. Unsafe shared stores, symlinks, hardlinks, size-bound
        refusals and systemic I/O still fail closed. History and corrupt
        receipts are never rewritten; repair is explicit operator action.
        """
        events, _ = self._scan_decision_delivery(role_name, actions, limit=limit, sources=sources)
        return events

    def _scan_decision_delivery(self, role_name: str, actions, *, limit: int,
                                sources: str) -> tuple[list, list]:
        """One bounded scan returning (deliverable events, receipt diagnostics).

        Diagnostics carry only safe identifiers (event id, token, action) and
        a bounded reason code; receipt bytes never leave the store. Unsafe or
        systemic receipt failures still raise and fail closed.
        """
        identifier(role_name)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Denied("Decision event limit must be an integer in [1, 100]")
        if sources not in ("own", "any"):
            raise Denied("Decision event sources must be own or any")
        wanted = {a for a in (actions or ()) if isinstance(a, str)} - {"withdraw"}
        history = self.root / "decision-history"
        if not wanted or not insight_archive._directory(history):
            return [], []
        diagnostics: list = []

        def eligible():
            for path in history.glob("*.json"):
                try:
                    identifier(path.stem)
                    record = json.loads(safe_read(self.root, f"decision-history/{path.name}",
                                                   MAX_DECISION_EVENT_BYTES))
                    if not isinstance(record, dict) or record.get("action") not in wanted:
                        continue
                    created = dt.datetime.fromisoformat(record["created_at"])
                    if created.tzinfo is None:
                        continue
                    current = self.read(record.get("id", ""))
                    if sources == "own" and current.get("source") != role_name:
                        continue
                    rev = _rev_of(current)
                    if type(record.get("rev")) is not int or record["rev"] != rev:
                        continue
                    raw = self.root / "decisions" / f"{current['id']}.json"
                    effective = _effective_decision(rev, json.loads(safe_read(
                        self.root, raw.relative_to(self.root).as_posix(), MAX_DECISION_EVENT_BYTES)))
                    if effective != record:
                        continue
                    payload = {"event": path.stem, "insight": current["id"], "rev": rev,
                               "title": current.get("title", ""), "body": current.get("body", ""),
                               "action": record["action"], "reason": record.get("reason", ""),
                               "revisit": record.get("revisit", ""),
                               "decided_at": record["created_at"], "source": current.get("source", ""),
                               "evidence": current.get("evidence", [])}
                    if len(canonical(payload)) > MAX_DECISION_EVENT_BYTES:
                        continue
                    payload["ack_token"] = self._decision_token(role_name, payload)
                except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
                try:
                    received = self._decision_received(role_name, payload)
                except CorruptDecisionReceipt as corrupt:
                    if len(diagnostics) < DECISION_RECEIPT_DIAGNOSTIC_LIMIT:
                        diagnostics.append({"event": payload["event"],
                                            "ack_token": payload["ack_token"],
                                            "action": payload["action"],
                                            "reason": corrupt.reason})
                    continue
                if not received:
                    yield created, path.stem, payload

        events = [row for _, _, row in heapq.nsmallest(limit, eligible(), key=lambda e: (e[0], e[1]))]
        return events, list(diagnostics)

    def decision_events_with_diagnostics(self, role_name: str, actions, *, limit: int = 10,
                                         sources: str = "own") -> tuple[list, list]:
        """Deliverable events plus bounded per-token receipt diagnostics.

        The events list matches :meth:`decision_events`. Each diagnostic is
        ``{"event", "ack_token", "action", "reason"}`` with ``reason`` in
        ``DECISION_RECEIPT_REASONS``. Corrupt receipts stay undelivered and
        unchanged; diagnostics let the runtime surface them instead of
        silently treating the queue as drained.
        """
        return self._scan_decision_delivery(role_name, actions, limit=limit, sources=sources)

    def acknowledge_decisions(self, role_name: str, events, *, sources: str = "own") -> None:
        """Host-only exact receipts after durable successful finish/publication.

        Runtime selects only tokens delivered in this unit. Validate the entire
        frozen batch against immutable history before writing; topic movement
        during handling does not invalidate the delivered decision. Atomic
        per-token files bound each read/write to 1 KiB, with no lossy tie window
        or unbounded aggregate. I/O failure causes replay; exact retries no-op.
        A suspect (corrupt-content) receipt is never overwritten or silently
        repaired: acknowledgement of its token is refused and its bytes are
        left for explicit operator-controlled recovery.
        """
        identifier(role_name)
        if sources not in ("own", "any"):
            raise Denied("Decision event sources must be own or any")
        if not isinstance(events, list) or len(events) > 512:
            raise Denied("Invalid decision receipt batch")
        tokens = set()
        for event in events:
            if (not isinstance(event, dict) or not isinstance(event.get("ack_token"), str)
                    or not DIGEST.fullmatch(event["ack_token"])
                    or event["ack_token"] in tokens
                    or (sources == "own" and event.get("source") != role_name)
                    or self._decision_token(role_name, event) != event["ack_token"]):
                raise Denied("Invalid decision acknowledgement")
            tokens.add(event["ack_token"])
        for event in events:
            try:
                received = self._decision_received(role_name, event)
            except CorruptDecisionReceipt as exc:
                raise Denied("Invalid decision acknowledgement") from exc
            if received:
                continue
            path = self.root / "decision-receipts" / role_name / f"{event['ack_token']}.json"
            mkdir(path.parent)
            receipt = {"role": role_name, "event": event["event"],
                       "ack_token": event["ack_token"], "acknowledged_at": now()}
            try:
                write_json(path, receipt, exclusive=True)
            except (FileExistsError, Denied):
                try:
                    if self._decision_received(role_name, event):
                        continue
                except CorruptDecisionReceipt as exc:
                    raise Denied("Invalid decision acknowledgement") from exc
                raise

    def decision_cursor(self, role_name: str) -> dict:
        """Read legacy cursor for diagnostics only; never used to suppress delivery."""
        try:
            identifier(role_name)
            cursor = read_json(self.root / "decision-cursors" / f"{role_name}.json", {})
        except (Denied, OSError, ValueError, TypeError, AttributeError):
            return {"role": role_name, "acknowledged_at": "", "acknowledged": []}
        if not isinstance(cursor, dict):
            return {"role": role_name, "acknowledged_at": "", "acknowledged": []}
        ack_at = cursor.get("acknowledged_at", "")
        acked = cursor.get("acknowledged", [])
        try:
            offset = cursor.get("defer_offset", 0)
            if type(offset) is not int or offset < 0:
                offset = 0
        except (TypeError, ValueError, AttributeError):
            offset = 0
        return {"role": role_name,
                "acknowledged_at": ack_at if isinstance(ack_at, str) else "",
                "acknowledged": [i for i in acked if isinstance(i, str)][:100],
                "defer_offset": offset}

    @staticmethod
    def _archivable(item, decision, insight_id: str) -> bool:
        """Conservative validation; malformed legacy state is not an expiry hint."""
        if not isinstance(item, dict) or not isinstance(decision, dict):
            return False
        if (item.get("id") != insight_id or decision.get("id") != insight_id
                or decision.get("action") not in ("accept", "modify", "reject", "withdraw")):
            return False
        if "base_snapshot" not in item:
            return False
        if item.get("run") is not None and not isinstance(item["run"], str):
            return False
        if item.get("updated_at") is not None and dt.datetime.fromisoformat(item["updated_at"]).tzinfo is None:
            return False
        if (not isinstance(decision.get("revisit"), str)
                or (decision.get("run") is not None and not isinstance(decision["run"], str))
                or (decision["action"] == "withdraw" and not isinstance(decision.get("actor"), str))):
            return False
        for record in (item, decision):
            rev = record.get("rev", 1)
            if type(rev) is not int or rev < 1:
                return False
            stamp = dt.datetime.fromisoformat(record["created_at"])
            if stamp.tzinfo is None:
                return False
        if item.get("rev", 1) != decision.get("rev", 1):
            return False
        identifier(item["source"])
        _check_origin(item.get("origin"))
        if (not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 200
                or not isinstance(item.get("body"), str) or not item["body"].strip()
                or (item.get("base_snapshot") is not None and
                    (not isinstance(item["base_snapshot"], str) or not DIGEST.fullmatch(item["base_snapshot"])))
                or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()
                or not isinstance(item.get("evidence", []), list)
                or len(item.get("evidence", [])) > MAX_EVIDENCE_ENTRIES):
            return False
        for entry in item.get("evidence", []):
            if (not isinstance(entry, dict) or not isinstance(entry.get("text"), str)
                    or not entry["text"].strip() or len(entry["text"]) > MAX_EVIDENCE_CHARS):
                return False
            if entry.get("run") is not None and not isinstance(entry["run"], str):
                return False
            identifier(entry["author"])
            if dt.datetime.fromisoformat(entry["created_at"]).tzinfo is None:
                return False
        return True

    def gc_decided(self, *, keep_days: int | None = None) -> int:
        """Archive long-decided proposals, then remove their live inbox copies.

        ``keep_days`` defaults to ``[limits] retention_days`` (31; 0 disables).
        Only well-formed current-revision terminal decisions qualify. Pending,
        deferred, stale and malformed records stay live. Under insights.lock,
        a FULL SQLite commit precedes unlink; retries reconcile duplicate copies.
        Returns the removed live count (ingest's historical ``reaped`` field).
        Per-file I/O/archive failures retain the inbox; archives are never deleted.
        Prior-revision history remains exclusively in insight-revisions/.
        """
        if keep_days is None:
            keep_days = self.retention_days
        if keep_days <= 0:
            return 0
        removed = 0
        cutoff = time.time() - keep_days * 86400
        with record_lock(self.root):
            for path in sorted((self.root / "inbox").glob("*.json")):
                if path.is_symlink():
                    continue
                try:
                    decision_path = self.root / "decisions" / f"{path.stem}.json"
                    if decision_path.is_symlink():
                        continue
                    decision = read_json(decision_path)
                    item = read_json(path)
                    if not self._archivable(item, decision, path.stem):
                        continue
                    decided_at = dt.datetime.fromisoformat(decision["created_at"]).timestamp()
                    if decided_at > cutoff:
                        continue
                    record = _normalize(item)
                    payload = {k: record.get(k) for k in
                               ("id", "source", "title", "body", "base_snapshot", "run", "origin")}
                    identity_path = self.root / "insight-ids" / path.name
                    write_json(identity_path,
                               {"id": item["id"], "sha256": digest(canonical(payload)),
                                "created_at": item["created_at"]}, exclusive=True)
                    insight_archive.store(self.root, record, decision, read_json(identity_path))
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
