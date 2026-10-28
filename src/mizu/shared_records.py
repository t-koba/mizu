"""Host-only immutable delivery records and exact per-role receipts.

Schema 1 has exactly schema/id/kind/created_at/writer/subject/payload. Names
are at most 256 ASCII characters: lowercase alphanumeric/underscore/hyphen
components separated by '.', '/' or ':' (at least two components; kinds may be single-component). Event times
are aware ISO timestamps, at most 64 characters; subject/payload are JSON
objects. Canonical envelopes are at most 1 MiB plus 4 KiB of framing, or a
smaller kind bound.
Roles use fs.identifier (1-63 characters). Receipt files are at most 2 KiB;
manifest/selection batches are at most 512 entries. Receipt schema is exactly
role/record_id/record_digest/acknowledged_at, with an aware acknowledgement time.
Tokens are SHA-256 of the canonical role/record_id/record_digest binding: they
are identity checks, not secrets or authorization independent of the manifest.

No kind is enabled by default. Trusted callers supply closed kind validators and
writer principals, freeze a manifest for the current unit, and pass only explicit
acknowledgement selections. This module grants no routing or source authority.

Canonical JSON (fs.canonical, including its final newline) is the evidence: reads
recompute its SHA-256; callers pin that digest in deliveries/retrieval. Files are
create-only, bounded, and never repaired. There is no index or timestamp cursor.
Canonical files are sharded by the record ID's first component under
``shared-records/<namespace>/``; long namespace components use a bounded digest
directory. This generic store has no index or timestamp cursor; source-specific
derived projections, when needed, are owned outside this module. Pre-partition
flat ``shared-records/*.json`` files remain readable as legacy and never gain a
second copy on exact replay.
Managed project directories must remain host-owned during writes, as required by
fs.atomic_write; safe_read additionally pins read paths on supported platforms.
A batch validates before writing, but an I/O failure may commit a prefix: replay
is safe, and callers must not interpret a failed call as a complete batch.
"""
from __future__ import annotations

import datetime as dt
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, MAX_JSON_DEPTH, READ_PAGE_CHARS, atomic_write, canonical,
                 digest, identifier, loads_finite, now, safe_read, sync_dir)

RECORD_BYTES = 1024 * 1024 + 4096
RECEIPT_BYTES = 2048
MANIFEST_ROWS = 512
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(?:[./:][a-z0-9][a-z0-9_-]*)+\Z")
_ENVELOPE = {"schema", "id", "kind", "created_at", "writer", "subject", "payload"}
_BINDING = {"role", "record_id", "record_digest", "ack_token"}


def _name(value: str, *, kind: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 256 or not _NAME.fullmatch(value if not kind else "kind/" + value):
        raise Denied("Expected a bounded record name")
    return value


_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def _namespace(record_id: str) -> str:
    """First ID component; the canonical storage partition for new files.

    The component grammar is the same single-component grammar as the name
    rule, so the directory name stays portable (no '.', '/' or ':' escapes).
    """
    text = _name(record_id)
    match = re.match(r"[a-z0-9][a-z0-9_-]*", text)
    assert match is not None
    return match.group(0)


def _check_namespace(namespace: str) -> str:
    if (not isinstance(namespace, str) or len(namespace) > 254
            or not _NAMESPACE.fullmatch(namespace)):
        raise Denied("Invalid shared record namespace")
    return namespace


def _partition(namespace: str) -> str:
    """Keep long but valid IDs within portable path-component bounds."""
    namespace = _check_namespace(namespace)
    return namespace if len(namespace) <= 64 else f"n-{digest(namespace.encode())[:32]}"


def _time(value: str) -> None:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = dt.datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise Denied("Expected an aware record timestamp") from exc


def _json(value, depth=0) -> None:
    # Refuse Python-only values/coercions, not just non-finite JSON numbers.
    if depth > MAX_JSON_DEPTH:
        raise Denied("Record JSON exceeds nesting bound")
    if type(value) is dict:
        for key, child in value.items():
            if not isinstance(key, str):
                raise Denied("Record JSON keys must be strings")
            _json(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _json(child, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise Denied("Record must contain only JSON values")


def _bytes(value, maximum: int) -> bytes:
    _json(value)
    try:
        raw = canonical(value)
        if len(raw) > maximum:
            raise LimitExceeded("Shared record exceeds byte bound")
        loads_finite(raw)
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied("Invalid shared record JSON") from exc


@dataclass(frozen=True)
class KindPolicy:
    """Trusted host declaration, not user configuration or model input.

    validate(subject, payload) must reject invalid kind-specific bindings by
    raising Denied. It must be pure: validators cannot rewrite record content.
    maximum bounds the entire canonical envelope, at most RECORD_BYTES.
    """

    writers: frozenset[str]
    validate: Callable[[dict, dict], None]
    maximum: int = RECORD_BYTES


@dataclass(frozen=True)
class Delivery:
    """One frozen host manifest entry; ack selections use these exact fields."""

    role: str
    record_id: str
    record_digest: str
    ack_token: str


def _token(role: str, record_id: str, record_digest: str) -> str:
    identifier(role)
    _name(record_id)
    if not isinstance(record_digest, str) or not DIGEST.fullmatch(record_digest):
        raise Denied("Invalid shared record digest")
    return digest(canonical({"role": role, "record_id": record_id,
                             "record_digest": record_digest}))


class SharedRecords:
    def __init__(self, root: Path, policies: Mapping[str, KindPolicy] | None = None):
        self.root = root
        self.policies = dict(policies or {})
        for kind, policy in self.policies.items():
            _name(kind, kind=True)
            if (not isinstance(policy, KindPolicy) or not callable(policy.validate)
                    or type(policy.maximum) is not int or not 1 <= policy.maximum <= RECORD_BYTES
                    or not isinstance(policy.writers, frozenset)):
                raise Denied("Invalid shared record kind policy")
            for writer in policy.writers:
                _name(writer)

    def _record_name(self, record_id: str) -> str:
        namespace = _partition(_namespace(record_id))
        return f"shared-records/{namespace}/{digest(_name(record_id).encode())}.json"

    def _legacy_record_name(self, record_id: str) -> str:
        return f"shared-records/{digest(_name(record_id).encode())}.json"

    def _record_names(self, record_id: str) -> tuple[str, str]:
        canonical = self._record_name(record_id)
        legacy = self._legacy_record_name(record_id)
        return canonical, legacy

    def _receipt_name(self, role: str, record_id: str) -> str:
        identifier(role)
        return (f"shared-record-receipts/{digest(role.encode())}/"
                f"{digest(_name(record_id).encode())}.json")

    def _parents(self, name: str, *, create: bool = False) -> bool:
        # All names here are internally constructed, never caller paths.
        current = self.root
        for part in ("", *name.split("/")[:-1]):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                sync_dir(current.parent)
                info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Denied("Unsafe shared record directory")
        return True

    def _read(self, name: str, maximum: int) -> bytes | None:
        if not self._parents(name):
            return None
        try:
            (self.root / name).lstat()
        except FileNotFoundError:
            return None
        return safe_read(self.root, name, maximum)

    def _validate(self, envelope: dict) -> bytes:
        if (type(envelope) is not dict or set(envelope) != _ENVELOPE
                or type(envelope["schema"]) is not int or envelope["schema"] != 1
                or type(envelope["subject"]) is not dict or type(envelope["payload"]) is not dict):
            raise Denied("Invalid shared record envelope")
        for field in ("id", "kind", "writer"):
            _name(envelope[field], kind=field == "kind")
        _time(envelope["created_at"])
        policy = self.policies.get(envelope["kind"])
        if policy is None or envelope["writer"] not in policy.writers:
            raise Denied("Shared record kind or writer is not authorized")
        raw = _bytes(envelope, policy.maximum)
        # The trusted validator sees a detached copy; its mutation cannot change
        # either the caller's envelope or the bytes about to be published.
        copy = loads_finite(raw)
        policy.validate(copy["subject"], copy["payload"])
        if canonical(copy) != raw:
            raise Denied("Shared record validator mutated content")
        return raw

    def put(self, envelope: dict, *, writer: str) -> str:
        """Create under an authenticated host writer; exact replay is a no-op.

        New files use the sharded canonical path; a pre-partition flat file
        with identical bytes satisfies replay without gaining a second copy.
        Conflicting duplicates across either location fail closed.
        """
        raw = self._validate(envelope)
        if writer != envelope["writer"]:
            raise Denied("Shared record authenticated writer mismatch")
        canonical, legacy = self._record_names(envelope["id"])
        self._parents(canonical, create=True)
        canonical_existing = self._read(canonical, RECORD_BYTES)
        legacy_existing = None if legacy == canonical else self._read(legacy, RECORD_BYTES)
        if canonical_existing is not None and legacy_existing is not None:
            if canonical_existing != legacy_existing:
                raise Denied("Shared record identity conflict")
        existing = canonical_existing if canonical_existing is not None else legacy_existing
        if existing is not None:
            self.get(envelope["id"])
            if existing != raw:
                raise Denied("Shared record identity conflict")
            return digest(raw)
        atomic_write(self.root / canonical, raw, exclusive=True)
        self.get(envelope["id"], expected_digest=digest(raw))
        return digest(raw)

    def get(self, record_id: str, *, expected_digest: str | None = None) -> tuple[dict, str]:
        """Return validated envelope and recomputed digest; absence is refused.

        A digest pin detects changed payloads, not just misplaced record IDs.
        Without a pin this reads the immutable object's current canonical bytes.
        Pre-partition flat files remain readable; conflicting duplicates fail.
        """
        canonical, legacy = self._record_names(record_id)
        canonical_raw = self._read(canonical, RECORD_BYTES)
        legacy_raw = None if legacy == canonical else self._read(legacy, RECORD_BYTES)
        if canonical_raw is not None and legacy_raw is not None and canonical_raw != legacy_raw:
            raise Denied("Shared record identity conflict")
        raw = canonical_raw if canonical_raw is not None else legacy_raw
        if raw is None:
            raise Denied("Shared record is unavailable")
        try:
            envelope = loads_finite(raw)
        except (ValueError, UnicodeError) as exc:
            raise Denied("Corrupt shared record JSON") from exc
        if self._validate(envelope) != raw or envelope["id"] != record_id:
            raise Denied("Shared record canonical identity mismatch")
        computed = digest(raw)
        if expected_digest is not None and computed != expected_digest:
            raise Denied("Shared record digest mismatch")
        return envelope, computed

    def records(self, *, namespace: str | None = None):
        """Scan this store's declared kinds only, without a second index/queue.

        Other source families retain their own validators and lifecycle. Unsafe
        files, malformed JSON and mismatched canonical identities fail closed.
        Each enumerated envelope is validated once from its scanned bytes; no
        second read is issued. ``namespace`` restricts the scan to a single
        ``shared-records/<namespace>/`` partition (for example ``"operator"``);
        the unfiltered scan covers both sharded partitions and pre-partition
        flat legacy files, deduplicating identical bytes and refusing
        conflicting duplicates for one ID.
        """
        if namespace is not None:
            partition = _partition(namespace)
            if not self._parents(f"shared-records/{partition}/entry.json"):
                return
            prefix = f"shared-records/{partition}/"
            for path in sorted((self.root / "shared-records" / partition).glob("*.json")):
                raw = self._read(f"{prefix}{path.name}", RECORD_BYTES)
                if raw is None:
                    raise Denied("Shared record is unavailable")
                try:
                    envelope = loads_finite(raw)
                except (ValueError, TypeError, UnicodeError) as exc:
                    raise Denied("Corrupt shared record JSON") from exc
                if type(envelope) is not dict or set(envelope) != _ENVELOPE:
                    raise Denied("Invalid shared record envelope")
                _name(envelope["kind"], kind=True)
                record_id = _name(envelope["id"])
                if _namespace(record_id) != namespace:
                    raise Denied("Shared record filename identity mismatch")
                if self._record_name(record_id) != f"{prefix}{path.name}":
                    raise Denied("Shared record filename identity mismatch")
                if envelope["kind"] not in self.policies:
                    raise Denied("Undeclared kind in shared record namespace")
                if self._validate(envelope) != raw:
                    raise Denied("Shared record canonical identity mismatch")
                yield envelope, digest(raw)
            return
        if not self._parents("shared-records/entry.json"):
            return
        seen: dict[str, bytes] = {}
        for path in sorted((self.root / "shared-records").rglob("*.json")):
            try:
                rel = path.relative_to(self.root).as_posix()
            except ValueError as exc:
                raise Denied("Shared record filename identity mismatch") from exc
            raw = self._read(rel, RECORD_BYTES)
            if raw is None:
                raise Denied("Shared record is unavailable")
            try:
                envelope = loads_finite(raw)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise Denied("Corrupt shared record JSON") from exc
            if type(envelope) is not dict or set(envelope) != _ENVELOPE:
                raise Denied("Invalid shared record envelope")
            _name(envelope["kind"], kind=True)
            record_id = _name(envelope["id"])
            canonical, legacy = self._record_names(record_id)
            if rel != canonical and rel != legacy:
                raise Denied("Shared record filename identity mismatch")
            previous = seen.get(record_id)
            if previous is not None:
                if previous != raw:
                    raise Denied("Shared record identity conflict")
                continue
            seen[record_id] = raw
            if envelope["kind"] not in self.policies:
                continue
            if self._validate(envelope) != raw:
                raise Denied("Shared record canonical identity mismatch")
            yield envelope, digest(raw)

    def freeze(self, role: str, record_ids: list[str]) -> tuple[Delivery, ...]:
        """Build the host's current-unit manifest after its eligibility/routing checks.

        The caller must retain this tuple for that unit only, never accept a
        model-supplied manifest. This method makes no source eligibility claim.
        """
        identifier(role)
        if not isinstance(record_ids, list) or len(record_ids) > MANIFEST_ROWS:
            raise Denied("Invalid shared record manifest")
        entries = []
        seen = set()
        for record_id in record_ids:
            _, computed = self.get(record_id)
            if record_id in seen:
                raise Denied("Duplicate shared record manifest identity")
            seen.add(record_id)
            entries.append(Delivery(role, record_id, computed, _token(role, record_id, computed)))
        return tuple(entries)

    def read_page(self, role: str, ack_token: str, *, manifest: tuple[Delivery, ...],
                  offset: int = 0, limit: int = READ_PAGE_CHARS) -> dict:
        """Read canonical envelope text only through a frozen host manifest.

        Offsets/limits count Unicode characters, not UTF-8 bytes. Each call
        revalidates the immutable record and its exact full-envelope digest.
        Source/routing changes do not invalidate this unit's retained manifest.
        """
        identifier(role)
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= READ_PAGE_CHARS):
            raise Denied("Invalid shared record page bounds")
        allowed = self._manifest(role, manifest)
        if not isinstance(ack_token, str) or ack_token not in allowed:
            raise Denied("Record is not in the frozen shared record manifest")
        entry = allowed[ack_token]
        envelope, _ = self.get(entry.record_id, expected_digest=entry.record_digest)
        text = canonical(envelope).decode("utf-8")
        if offset > len(text):
            raise Denied("Shared record offset exceeds text length")
        end = min(offset + limit, len(text))
        return {"record_id": entry.record_id, "record_digest": entry.record_digest,
                "offset": offset, "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "text": text[offset:end]}

    def _manifest(self, role: str, manifest: tuple[Delivery, ...]) -> dict[str, Delivery]:
        if not isinstance(manifest, tuple) or len(manifest) > MANIFEST_ROWS:
            raise Denied("Invalid frozen shared record manifest")
        allowed = {}
        for entry in manifest:
            if (not isinstance(entry, Delivery) or entry.role != role
                    or _token(role, entry.record_id, entry.record_digest) != entry.ack_token
                    or not isinstance(entry.ack_token, str) or entry.ack_token in allowed):
                raise Denied("Invalid frozen shared record manifest")
            allowed[entry.ack_token] = entry
        return allowed

    def received(self, role: str, record_id: str, record_digest: str) -> bool:
        """Check exact receipt; unsafe/corrupt data raises, never counts as ack."""
        _token(role, record_id, record_digest)
        self.get(record_id, expected_digest=record_digest)
        raw = self._read(self._receipt_name(role, record_id), RECEIPT_BYTES)
        if raw is None:
            return False
        try:
            receipt = loads_finite(raw)
        except (ValueError, UnicodeError) as exc:
            raise Denied("Corrupt shared record receipt") from exc
        if (type(receipt) is not dict
                or set(receipt) != {"role", "record_id", "record_digest", "acknowledged_at"}
                or receipt["role"] != role or receipt["record_id"] != record_id
                or receipt["record_digest"] != record_digest):
            raise Denied("Shared record receipt binding mismatch")
        _time(receipt["acknowledged_at"])
        if _bytes(receipt, RECEIPT_BYTES) != raw:
            raise Denied("Noncanonical shared record receipt")
        return True

    def acknowledge(self, role: str, selections: list[dict] | None = None, *, manifest: tuple[Delivery, ...]) -> None:
        """Accept only explicit exact selections from the frozen host manifest.

        Empty/omitted selections do nothing. Manifest entries remain valid after
        source/policy routing movement; no source is mutated or reactivated.
        """
        identifier(role)
        if selections is None:
            selections = []
        if (not isinstance(manifest, tuple) or len(manifest) > MANIFEST_ROWS
                or not isinstance(selections, list) or len(selections) > MANIFEST_ROWS):
            raise Denied("Invalid shared record acknowledgement batch")
        allowed = self._manifest(role, manifest)
        pending = []
        seen = set()
        for selected in selections:
            if type(selected) is not dict or set(selected) != _BINDING:
                raise Denied("Invalid shared record acknowledgement selection")
            entry = Delivery(**selected)
            _token(entry.role, entry.record_id, entry.record_digest)
            if (not isinstance(entry.ack_token, str) or allowed.get(entry.ack_token) != entry
                    or entry.ack_token in seen):
                raise Denied("Selection is not in the frozen shared record manifest")
            seen.add(entry.ack_token)
            if not self.received(role, entry.record_id, entry.record_digest):
                pending.append(entry)
        for entry in pending:
            name = self._receipt_name(role, entry.record_id)
            self._parents(name, create=True)
            raw = _bytes({"role": role, "record_id": entry.record_id,
                          "record_digest": entry.record_digest, "acknowledged_at": now()}, RECEIPT_BYTES)
            try:
                atomic_write(self.root / name, raw, exclusive=True)
            except Denied:
                # A concurrent exact retry may have a different timestamp.
                if not self.received(role, entry.record_id, entry.record_digest):
                    raise
            self.received(role, entry.record_id, entry.record_digest)
