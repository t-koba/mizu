"""Immutable publication identity/evidence; no retries or remote reconciliation.

Records live in project evidence/publications, outside the workspace. Callers
hold the per-ID kernel lock across inspection and dispatch. Each JSON record is
create-only and read with a fixed 8 KiB no-follow bound. A dispatch marker is a
conservative point of no return: without a valid receipt it requires manual
reconciliation, even if the process died before actually starting the adapter.

Two receipt shapes share this store. The product's own publish path retains
intent, dispatch marker and receipt for one publication ID. The external
receipt is recorded after the fact by an operator-run publisher that pushed
outside the product. It is create-only, idempotent for an
identical request, and carries the same caller-visible fields, so an exact-SHA
CI wait can be pinned to either shape.

Source-identity boundary: ``code_digest`` always names the whole immutable
capture, never a filtered tree; historical digests keep that meaning. New schema-3 records bind the versioned public-source projection
(``projection_schema``/``projection_algorithm``/``projection_digest`` from
``mizu.distribution``) that the host adapter must verify against the Git
publication tree. A schema-4 external receipt may additionally carry a
versioned lineage observation; the product receipt carries the same nested
observation without changing its publication identity. A filtered projection
digest is never stored or compared as a ``code_digest``, and no exact
whole-snapshot match is claimed for a filtered tree. Legacy records stay
readable and are never rewritten; new external writes require schema 3 or 4.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import stat
import sys
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from . import lineage, process
from .config import PublicationEvidence
from .errors import Denied, LimitExceeded
from .fs import (DIGEST, canonical, digest, identifier, loads_finite,
                 lock as kernel_lock, mkdir, now, safe_read, sync_dir, write_json)

SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
RECORD_BYTES = 8192
#: Opaque cursor format of the newest-first receipt listing.
MAX_LISTING_CURSOR_CHARS = 128
_PUBLICATION_CURSOR_TIME = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")
#: Bounded process-local reuse of fully validated receipts. Each key carries
#: the SHA-256 of the exact bounded bytes that were parsed and validated, so
#: replaced or corrupt evidence cannot be served from an earlier result.
RECEIPT_VALIDATION_CACHE_LIMIT = 128
_VALIDATED_RECEIPTS = OrderedDict()
_VALIDATED_RECEIPTS_LOCK = threading.Lock()

#: Operation label of a receipt recorded by an operator-run publisher.
EXTERNAL_OP = "external"
#: Schema version of an external receipt record.
EXTERNAL_SCHEMA = 2
#: Exact fields of an external receipt record.
EXTERNAL_FIELDS = {"schema", "publication_id", "op", "branch", "published_sha",
                   "code_digest", "approval", "source_commit", "run", "recorded_at"}
#: Schema version of a projection-bound receipt (product intent/receipt trio
#: and external receipt alike). The operation still distinguishes the shapes;
#: the schema marks that the publication ID binds the projection identity.
PROJECTION_SCHEMA = 3
#: Projection keys carried by a schema-3 publication identity and receipt.
PROJECTION_KEYS = ("projection_schema", "projection_algorithm", "projection_digest")
#: Exact fields of a projection-bound external receipt record.
EXTERNAL_PROJECTION_FIELDS = EXTERNAL_FIELDS | set(PROJECTION_KEYS)
#: Schema version of an external receipt that also carries a lineage observation.
#: The publication ID still binds only the projection identity; lineage never
#: enters the identity so the same publication keeps one ID.
EXTERNAL_LINEAGE_SCHEMA = 4
#: Exact fields of an external receipt with a lineage observation.
EXTERNAL_LINEAGE_FIELDS = EXTERNAL_PROJECTION_FIELDS | {"lineage"}
#: Maximum UTF-8 bytes of a caller-supplied lineage JSON value.
LINEAGE_JSON_BYTES = 4096


def _check_projection(schema, algorithm, projection_digest: str) -> None:
    """Validate one projection identity carried in publication evidence.

    Schema: ``schema`` is the distribution projection schema (int),
    ``algorithm`` is the exact distribution projection algorithm label, and
    ``projection_digest`` is a 64-hex digest of the projection manifest -- a
    filtered-tree digest that must never be stored or compared as a
    ``code_digest``. Bounds: the algorithm label is short ASCII (at most 128
    chars). Failure: ``Denied``.
    """
    from .distribution import (PUBLIC_SOURCE_ALGORITHM,
                               PUBLIC_SOURCE_ALGORITHM_CHARS, PUBLIC_SOURCE_SCHEMA)
    if type(schema) is not int or schema != PUBLIC_SOURCE_SCHEMA:
        raise Denied("Invalid projection schema for publication")
    if (not isinstance(algorithm, str) or not algorithm
            or len(algorithm) > PUBLIC_SOURCE_ALGORITHM_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in algorithm)
            or algorithm != PUBLIC_SOURCE_ALGORITHM):
        raise Denied("Invalid projection algorithm for publication")
    if (not isinstance(projection_digest, str)
            or not DIGEST.fullmatch(projection_digest)):
        raise Denied("Invalid projection digest for publication")


def _projection_of(record: dict) -> dict | None:
    """Return the projection identity of a record, or None for legacy ones."""
    if any(key in record for key in PROJECTION_KEYS):
        return {"schema": record.get("projection_schema"),
                "algorithm": record.get("projection_algorithm"),
                "digest": record.get("projection_digest")}
    return None


def _parse_record(raw: bytes, kind: str, publication_id: str) -> dict:
    """Parse one bounded receipt byte read for validation.

    This is the single parse hook of receipt validation. Every cached result
    is derived from the same bytes that passed through this function.
    """
    try:
        record = loads_finite(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Denied(f"Invalid publication {kind}: {publication_id}") from exc
    if not isinstance(record, dict):
        raise Denied(f"Invalid publication {kind}: {publication_id}")
    return record


#: File names of recorded receipts inside the publications directory.
RECEIPT_FILE = re.compile(r"([0-9a-f]{64})\.(external|receipt)\.json\Z")
#: A name that looks like a receipt but does not parse cannot be skipped by a
#: rebuild: a rename must not hide evidence from keyed resolution.
MALFORMED_RECEIPT_NAME = re.compile(r"(?:external|receipt)\.json")

#: Writer-owned key index. It lives outside the publications directory so a
#: lookup reads one fixed file and never enumerates receipt names; the receipt
#: directory stamp detects out-of-band name changes and per-entry content
#: digests detect same-name edits before any key is resolved. A rebuild from
#: directory names tolerates unreadable individual receipts by marking the
#: index incomplete; lookups refuse to resolve such an index.
KEY_INDEX_DIR = "publication-keys"
KEY_INDEX_FILE = "receipt-key-index.json"
KEY_INDEX_LOCK = "receipt-key-index.lock"
KEY_INDEX_SCHEMA = 2
KEY_INDEX_BYTES = 1 << 20
#: Maximum characters of an operator approval or run reference.
REFERENCE_CHARS = 256


def _key_index_paths(store):
    """Return the screened sibling directory holding the writer-owned index."""
    evidence = Path(store.root).parent
    for path in (evidence, evidence / KEY_INDEX_DIR):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise Denied("Unsafe publication evidence directory")
    directory = evidence / KEY_INDEX_DIR
    return directory, directory / KEY_INDEX_FILE, directory / KEY_INDEX_LOCK


def _directory_stamp(path):
    """Observe receipt-directory changes without reading its contents."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return [info.st_mtime_ns, info.st_size]


def _receipt_file_name(publication_id, kind):
    return f"{publication_id}.{kind}.json"


def _receipt_names_differ(store, policy, index):
    """Compare receipt-like directory names with the names the index implies.

    A new writer prepares before its receipt exists, so the index describes
    exactly the names on disk until the append. The stamp is a cheap change
    detector, not proof: an out-of-band change could share its value. Only
    receipt-like names matter; other evidence is owned by other writers. A
    malformed receipt-like name is reported as a change so the rebuild
    refuses it exactly as it would on any other write.
    """
    try:
        names = os.listdir(store.root)
    except (FileNotFoundError, NotADirectoryError):
        names = []
    except OSError as exc:
        raise Denied("Publication evidence is unavailable") from exc
    if len(names) > policy.name_processing_limit:
        return True
    current = set()
    for name in names:
        if RECEIPT_FILE.fullmatch(name):
            current.add(name)
        elif MALFORMED_RECEIPT_NAME.search(name):
            return True
    expected = {_receipt_file_name(entry["publication_id"], entry["kind"])
                for entry in index["entries"]}
    return current != expected


def _reference(value, label: str) -> str:
    """Validate one operator reference (approval or run) of a receipt.

    Schema: a nonempty string of at most 256 characters with no control
    characters (code point below 32 or 127) and at least one non-whitespace
    character. Trust: the operator asserts this reference; it names evidence,
    it does not grant authority. Failure: ``Denied`` for anything else.
    """
    if (not isinstance(value, str) or not value or len(value) > REFERENCE_CHARS
            or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise Denied(f"Invalid publication {label} reference")
    return value


def _timestamp(value) -> bool:
    """Accept only a timezone-aware ISO 8601 timestamp."""
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _timestamp_order(value: str) -> int:
    """Convert a timezone-aware receipt timestamp to exact UTC microseconds."""
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        delta = parsed - dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)
    except (OverflowError, TypeError, ValueError) as exc:
        raise Denied("Invalid publication receipt timestamp") from exc
    return ((delta.days * 86400 + delta.seconds) * 1_000_000) + delta.microseconds


def _listing_cursor_key(cursor: str | None):
    """Decode and validate a ``pub1|micros|publication_id`` page cursor."""
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > MAX_LISTING_CURSOR_CHARS:
        raise Denied("Invalid publication listing cursor")
    parts = cursor.split("|")
    if (len(parts) != 3 or parts[0] != "pub1"
            or _PUBLICATION_CURSOR_TIME.fullmatch(parts[1]) is None
            or DIGEST.fullmatch(parts[2]) is None):
        raise Denied("Invalid publication listing cursor")
    return int(parts[1]), parts[2]


def _listing_cursor_token(key: tuple[int, str]) -> str:
    """Encode one ordering key as an opaque continuation cursor."""
    micros, publication_id = key
    token = f"pub1|{micros}|{publication_id}"
    if len(token) > MAX_LISTING_CURSOR_CHARS:
        raise LimitExceeded("Publication listing cursor exceeds character bound")
    return token


def _receipt_order(record: dict) -> tuple[int, str]:
    """Ordering key of one validated receipt, newest first.

    Older product receipts predate ``recorded_at`` and sort before every
    stamped receipt, with the publication ID breaking equal instants.
    """
    stamp = record.get("recorded_at")
    return (_timestamp_order(stamp) if stamp is not None else 0,
            record["publication_id"])


def _external_record(record, publication_id: str) -> dict:
    """Validate one external receipt record against its stable publication ID.

    Schema: exactly the recorded fields, schema 2 (legacy), 3, or 4 with a
    lineage observation, operation ``external``, and a publication ID equal
    to the file name and to the digest of its identity. Schema 4 carries
    exactly the schema-3 fields plus ``lineage``; schema 2/3 never carry
    lineage. The outer receipt identity is validated here; lineage validity
    is projected separately so a malformed optional observation becomes
    unknown without suppressing otherwise valid CI status.
    Bounds: 8 KiB record (read bound), 40/64-hex commit SHAs, a 64-hex code
    digest, and references of at most 256 characters. Trust: the operator
    asserts the recorded content; it is never inferred from a branch head.
    Failure: ``Denied`` for any mismatch.
    """
    if not isinstance(record, dict):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    if record.get("schema") == EXTERNAL_SCHEMA:
        if set(record) != EXTERNAL_FIELDS:
            raise Denied(f"Invalid external publication receipt: {publication_id}")
        projection = None
    elif record.get("schema") == PROJECTION_SCHEMA:
        if set(record) != EXTERNAL_PROJECTION_FIELDS:
            raise Denied(f"Invalid external publication receipt: {publication_id}")
        projection = _projection_of(record)
        assert projection is not None
        _check_projection(projection["schema"], projection["algorithm"], projection["digest"])
    elif record.get("schema") == EXTERNAL_LINEAGE_SCHEMA:
        if set(record) != EXTERNAL_LINEAGE_FIELDS:
            raise Denied(f"Invalid external publication receipt: {publication_id}")
        projection = _projection_of(record)
        assert projection is not None
        _check_projection(projection["schema"], projection["algorithm"], projection["digest"])
    else:
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    if (record.get("op") != EXTERNAL_OP
            or record.get("publication_id") != publication_id):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    identity = {"op": EXTERNAL_OP, "branch": record["branch"],
                "code_digest": record["code_digest"],
                "published_sha": record["published_sha"]}
    if projection is not None:
        identity = {**identity, "projection_schema": projection["schema"],
                    "projection_algorithm": projection["algorithm"],
                    "projection_digest": projection["digest"]}
    try:
        computed = digest(canonical(identity))
    except (TypeError, ValueError) as exc:
        raise Denied(f"Invalid external publication identity: {publication_id}") from exc
    if computed != publication_id:
        raise Denied(f"Invalid external publication identity: {publication_id}")
    from .vcs import check_branch
    check_branch(record["branch"])
    if (not isinstance(record["published_sha"], str)
            or not SHA.fullmatch(record["published_sha"])):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    if (not isinstance(record["code_digest"], str)
            or not DIGEST.fullmatch(record["code_digest"])):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    source_commit = record["source_commit"]
    if source_commit is not None and (not isinstance(source_commit, str)
                                      or not SHA.fullmatch(source_commit)):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    _reference(record["approval"], "approval")
    if record["run"] is not None:
        _reference(record["run"], "run")
    if not _timestamp(record["recorded_at"]):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    return record


def _summary(record: dict) -> dict:
    """Project one validated receipt onto its caller-visible fields."""
    summary = {"publication_id": record["publication_id"], "op": record["op"],
               "branch": record["branch"], "published_sha": record["published_sha"],
               "code_digest": record["code_digest"], "approval": record["approval"],
               "source_commit": record.get("source_commit"), "run": record.get("run")}
    projection = _projection_of(record)
    if projection is not None:
        summary = {**summary, "projection_schema": projection["schema"],
                   "projection_algorithm": projection["algorithm"],
                   "projection_digest": projection["digest"]}
    return summary


def source_commit(workspace: Path, *, timeout: int, cancel=lambda: False) -> str:
    """Read only this workspace's Git HEAD commit, never a remote/parent ref."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    result = process.run(
        ["git", "--git-dir=.git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=workspace, env=env, timeout=timeout, maximum=4096, cancel=cancel)
    sha = result.stdout.strip()
    if result.reason != "exited" or result.exit_code != 0 or not SHA.fullmatch(sha):
        raise Denied("Publication requires a valid local workspace Git HEAD commit")
    return sha


class Publication:
    def __init__(self, project_root: Path, source: str, code_digest: str,
                 branch: str, op: str, projection: dict | None = None):
        """Bind one publication to a whole capture plus its public projection.

        Schema: ``projection`` must be a ``{"schema", "algorithm", "digest"}``
        identity from ``mizu.distribution.public_source_identity`` for a
        schema-3 intent. Missing projections are refused before any write;
        schema-1/2 records are read-only legacy history.
        The publication ID digests the whole identity, so the receipt ID
        binds the projection; the projection digest is never a code digest.
        Failure: ``Denied``.
        """
        from .vcs import PUBLISH_OPS, check_branch
        check_branch(branch)
        if op not in PUBLISH_OPS:
            raise Denied("vcs_publish cannot publish this operation")
        if not isinstance(source, str) or not SHA.fullmatch(source):
            raise Denied("Invalid source commit for publication")
        if not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest):
            raise Denied("Invalid code digest for publication")
        self.identity = {"source_commit": source, "code_digest": code_digest,
                         "branch": branch, "op": op}
        if not isinstance(projection, dict) or set(projection) != {"schema", "algorithm", "digest"}:
            raise Denied("Invalid projection identity for publication")
        _check_projection(projection["schema"], projection["algorithm"], projection["digest"])
        self.identity = {**self.identity,
                         "projection_schema": projection["schema"],
                         "projection_algorithm": projection["algorithm"],
                         "projection_digest": projection["digest"]}
        self.id = digest(canonical(self.identity))
        self.root = project_root / "evidence" / "publications"

    @property
    def schema(self) -> int:
        """Record schema of all new publication intents."""
        return PROJECTION_SCHEMA

    def _check_directories(self) -> None:
        # Match the portable safe-FS screening, including Windows reparse
        # points. This rejects static unsafe components; it does not pin the
        # directories against concurrent replacement between check and use.
        for path in (self.root.parent.parent, self.root.parent, self.root):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise Denied("Unsafe publication evidence directory")

    @property
    def lock(self) -> Path:
        # fs.lock creates its parent and lock file: screen before either.
        self._check_directories()
        return self.root / (self.id + ".lock")

    def path(self, kind: str) -> Path:
        return self.root / (self.id + "." + kind + ".json")

    def _read_raw(self, kind: str, observed: dict | None = None) -> bytes | None:
        """Return one bounded no-follow byte read, or absence.

        ``observed`` carries ``(raw, sha256)`` already checked against the
        writer's key index; reusing it avoids a second read whose bytes could
        differ from the indexed witness.
        """
        if observed is not None and kind in observed:
            return observed[kind][0]
        try:
            self.path(kind).lstat()
        except FileNotFoundError:
            return None
        return safe_read(self.root, self.path(kind).name, RECORD_BYTES)

    def _read(self, kind: str) -> dict | None:
        raw = self._read_raw(kind)
        if raw is None:
            return None
        return _parse_record(raw, kind, self.id)

    def _write(self, kind: str, record: dict) -> None:
        self._check_directories()
        write_json(self.path(kind), record, exclusive=True)
        # atomic_write syncs the record's directory; also retain any newly
        # created evidence/publications ancestors before external dispatch.
        sync_dir(self.root.parent)
        sync_dir(self.root.parent.parent)

    @classmethod
    def read_receipt(cls, project_root: Path, publication_id: str) -> dict:
        """Read and validate one completed receipt by its stable ID.

        This is a bounded, no-follow read only: missing or uncertain publication
        evidence is refused, never reconstructed from a branch head. Repeated
        reads of unchanged bytes reuse the bounded validation cache of
        ``_validated_receipt``; metadata changes alone never authorize reuse.
        """
        return cls._validated_receipt(
            cls._receipt_store_for_id(project_root, publication_id), publication_id)

    @classmethod
    def _validated_receipt(cls, store, publication_id: str,
                           observed: dict | None = None) -> dict:
        """Validate one receipt from the exact bounded byte reads.

        ``observed`` maps a component kind to ``(raw, sha256)`` from a read
        already checked against the writer's key index by ``receipt_for_key``.
        It replaces that read rather than reading twice, so the index witness,
        the cache key and the validated record describe one set of bytes.

        The process-local cache is keyed by the store root, the publication ID
        and the SHA-256 of every byte read used for validation. An external
        receipt is one bounded file, so its key is exactly
        ``(root, publication_id, sha256)``. The product receipt binds three
        independent files (intent, dispatch, receipt); one digest cannot name
        them, so the key holds one digest per component in validation order.
        Neither a stat, an mtime nor a directory name is a cache input.
        Entries are bounded, never cache failures, and are returned as copies;
        any changed byte misses and is parsed and validated afresh.
        """
        store._check_directories()
        raws = {kind: store._read_raw(kind, observed)
                for kind in ("intent", "dispatch", "receipt")}
        if all(value is None for value in raws.values()):
            raws["external"] = store._read_raw("external", observed)
        key_parts = tuple(
            (kind, observed[kind][1] if observed is not None and kind in observed
             else digest(raws[kind]))
            for kind in ("intent", "dispatch", "receipt", "external")
            if raws.get(kind) is not None)
        key = (str(store.root), publication_id, key_parts)
        with _VALIDATED_RECEIPTS_LOCK:
            cached = _VALIDATED_RECEIPTS.get(key)
            if cached is not None:
                _VALIDATED_RECEIPTS.move_to_end(key)
                return deepcopy(cached)
        if raws.get("external") is not None:
            external = _parse_record(raws["external"], "external", publication_id)
            validated = _external_record(external, publication_id)
        else:
            def parse(kind):
                return (_parse_record(raws[kind], kind, publication_id)
                        if raws[kind] is not None else None)
            intent = parse("intent")
            dispatched = parse("dispatch")
            receipt = parse("receipt")
            if intent is None or dispatched is None or receipt is None:
                raise Denied(f"Publication receipt unavailable or unresolved: {publication_id}")
            from .vcs import PUBLISH_OPS, check_branch
            if intent.get("schema") == 1:
                fields = {"source_commit", "code_digest", "branch", "op", "schema",
                          "publication_id", "approval", "run"}
                identity_keys = ("source_commit", "code_digest", "branch", "op")
            elif intent.get("schema") == PROJECTION_SCHEMA:
                fields = {"source_commit", "code_digest", "branch", "op", "schema",
                          "publication_id", "approval", "run",
                          "projection_schema", "projection_algorithm", "projection_digest"}
                identity_keys = ("source_commit", "code_digest", "branch", "op",
                                 "projection_schema", "projection_algorithm", "projection_digest")
            else:
                raise Denied(f"Invalid publication intent: {publication_id}")
            if set(intent) != fields:
                raise Denied(f"Invalid publication intent: {publication_id}")
            identity = {key: intent[key] for key in identity_keys}
            if ("projection_schema" in identity):
                _check_projection(identity["projection_schema"],
                                  identity["projection_algorithm"],
                                  identity["projection_digest"])
            if (intent.get("publication_id") != publication_id
                    or not isinstance(identity["source_commit"], str)
                    or not SHA.fullmatch(identity["source_commit"])
                    or not isinstance(identity["code_digest"], str)
                    or not DIGEST.fullmatch(identity["code_digest"])
                    or not isinstance(identity["op"], str) or identity["op"] not in PUBLISH_OPS
                    or digest(canonical(identity)) != publication_id):
                raise Denied(f"Invalid publication identity: {publication_id}")
            check_branch(identity["branch"])
            identifier(intent["approval"])
            identifier(intent["run"])
            if dispatched != {"publication_id": publication_id}:
                raise Denied(f"Invalid publication dispatch marker: {publication_id}")
            published_sha = receipt.get("published_sha")
            if (not isinstance(published_sha, str) or not SHA.fullmatch(published_sha)
                    or {k: v for k, v in receipt.items() if k != "lineage"} != {**intent, "published_sha": published_sha}):
                raise Denied(f"Invalid publication receipt: {publication_id}")
            validated = receipt
        with _VALIDATED_RECEIPTS_LOCK:
            _VALIDATED_RECEIPTS[key] = deepcopy(validated)
            _VALIDATED_RECEIPTS.move_to_end(key)
            while len(_VALIDATED_RECEIPTS) > RECEIPT_VALIDATION_CACHE_LIMIT:
                _VALIDATED_RECEIPTS.popitem(last=False)
        return validated

    def prepare(self, approval: str, run: str) -> tuple[dict, dict | None]:
        """Reuse intent/receipt; refuse any uncertain prior dispatch."""
        self._check_directories()
        intent = self._read("intent")
        receipt = self._read("receipt")
        dispatched = self._read("dispatch")
        if intent is None:
            if receipt is not None or dispatched is not None:
                raise Denied(f"Missing publication intent: {self.id}")
            intent = {**self.identity, "schema": self.schema, "publication_id": self.id,
                      "approval": identifier(approval), "run": identifier(run)}
            self._write("intent", intent)
        expected = {**self.identity, "schema": self.schema, "publication_id": self.id}
        if (set(intent) != set(expected) | {"approval", "run"}
                or any(intent.get(key) != value for key, value in expected.items())):
            raise Denied(f"Publication intent mismatch: {self.id}")
        identifier(intent["approval"])
        identifier(intent["run"])
        if dispatched is not None and dispatched != {"publication_id": self.id}:
            raise Denied(f"Invalid publication dispatch marker: {self.id}")
        if receipt is not None:
            sha = receipt.get("published_sha")
            if (dispatched is None or not isinstance(sha, str) or not SHA.fullmatch(sha)
                    or {k: v for k, v in receipt.items() if k != "lineage"} != {**intent, "published_sha": sha}):
                raise Denied(f"Invalid publication receipt: {self.id}")
            # A previous caller may have failed at directory fsync after the
            # receipt rename. Re-establish durability before acknowledging it.
            sync_dir(self.root)
            sync_dir(self.root.parent)
            sync_dir(self.root.parent.parent)
            return intent, receipt
        if dispatched is not None:
            raise Denied(f"Publication outcome unresolved; already dispatched: {self.id}")
        if intent["approval"] != identifier(approval):
            raise Denied(f"Publication approval mismatch: {self.id}")
        return intent, None

    def mark_dispatch(self) -> None:
        self._write("dispatch", {"publication_id": self.id})

    @classmethod
    def record_external(cls, project_root: Path, *, branch: str, published_sha: str,
                        code_digest: str, approval: str, source_commit: str | None = None,
                        run: str | None = None, projection: dict | None = None,
                        lineage=None, lineage_target=None) -> dict:
        """Record one operator-run publication as a durable receipt.

        The publisher pushes outside the product, then records what it
        published. Schema: branch (ref name), published SHA (40 or 64 hex),
        code digest (64 hex, always the whole capture), an approval reference,
        an optional source commit and an optional run reference; the record
        carries schema 3, the operation ``external``, the publication ID and
        the recording time. ``projection`` must be a ``{"schema", "algorithm",
        "digest"}`` public-source identity. The publication ID binds that
        projection, so a filtered tree is never recorded as an exact
        whole-snapshot match. Missing projections are refused before any
        write; schema-2 receipts remain read-only legacy history.
        An optional ``lineage`` observation uses the same bounded shape as
        the product path and must bind the published SHA; when present the
        receipt carries schema 4 with exactly the schema-3 fields plus
        ``lineage``, otherwise schema 3 with no lineage. When
        ``lineage_target`` (the configured promotion descriptor) is given,
        the observation is bound to that descriptor and must match it;
        without it the observation stays unbound legacy. Lineage never
        enters the publication ID. A receipt without lineage stays unknown.
        Identity: the publication ID is the digest of the operation, branch,
        code digest, published SHA and projection, so the same
        publication keeps one ID. Bounds: 8 KiB record, 256 characters per
        reference, lineage JSON at most 4096 UTF-8 bytes. Trust: the operator
        asserts this content; the product never infers a published SHA or
        ancestry from a branch head. Retry: none; the write is create-only
        and an identical request replays the stored record, a conflicting
        one (including a differing lineage) is refused. Failure: ``Denied``
        for invalid input or a conflicting receipt.
        """
        from .vcs import check_branch
        check_branch(branch)
        if not isinstance(published_sha, str) or not SHA.fullmatch(published_sha):
            raise Denied("Invalid published SHA for external publication")
        if not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest):
            raise Denied("Invalid code digest for external publication")
        if source_commit is not None and (not isinstance(source_commit, str)
                                          or not SHA.fullmatch(source_commit)):
            raise Denied("Invalid source commit for external publication")
        approval = _reference(approval, "approval")
        if run is not None:
            run = _reference(run, "run")
        identity = {"op": EXTERNAL_OP, "branch": branch, "code_digest": code_digest,
                    "published_sha": published_sha}
        if not isinstance(projection, dict) or set(projection) != {"schema", "algorithm", "digest"}:
            raise Denied("Invalid projection identity for external publication")
        _check_projection(projection["schema"], projection["algorithm"], projection["digest"])
        extra = {"projection_schema": projection["schema"],
                 "projection_algorithm": projection["algorithm"],
                 "projection_digest": projection["digest"]}
        identity = {**identity, **extra}
        from . import lineage as lineage_module
        validated = None
        if lineage is not None:
            if lineage_target is None:
                validated = lineage_module.validate(lineage, published_sha)
                if validated is not None and validated.get("schema") != lineage_module.SCHEMA:
                    validated = None
            else:
                target = lineage_module.check_target(lineage_target)
                if target is None:
                    raise Denied("Invalid lineage target for external publication")
                # Accept an already-bound fact matching the target, or bind an
                # unbound adapter observation to the target.
                existing = lineage_module.validate(lineage, published_sha)
                if existing is not None and existing.get("schema") == lineage_module.BOUND_SCHEMA:
                    if (existing.get("expected_repository") != target["repository"]
                            or existing.get("expected_remote") != target["remote"]
                            or existing.get("expected_target_ref") != target["target_ref"]
                            or existing.get("expected_version") != target["version"]):
                        raise Denied("Invalid lineage observation for external publication")
                    validated = existing
                else:
                    validated = lineage_module.bind(lineage, published_sha, target)
            if validated is None:
                raise Denied("Invalid lineage observation for external publication")
        compared = ("branch", "published_sha", "code_digest", "approval",
                    "source_commit", "run", *PROJECTION_KEYS, "lineage")
        publication_id = digest(canonical(identity))
        store = cls.__new__(cls)
        store.id = publication_id
        store.root = Path(project_root) / "evidence" / "publications"
        store.identity = identity
        if validated is not None:
            record = {"schema": EXTERNAL_LINEAGE_SCHEMA, "publication_id": publication_id,
                      "op": EXTERNAL_OP, "branch": branch, "published_sha": published_sha,
                      "code_digest": code_digest, "approval": approval,
                      "source_commit": source_commit, "run": run, "recorded_at": now(),
                      **extra, "lineage": validated}
        else:
            record = {"schema": PROJECTION_SCHEMA, "publication_id": publication_id,
                      "op": EXTERNAL_OP, "branch": branch, "published_sha": published_sha,
                      "code_digest": code_digest, "approval": approval,
                      "source_commit": source_commit, "run": run, "recorded_at": now(),
                      **extra}
        # Observe the directory before this writer creates its lock or
        # receipt file; the index must describe exactly this state. It also
        # covers the replay path, which writes no receipt at all.
        before_stamp = _directory_stamp(store.root)
        with kernel_lock(store.lock):
            try:
                existing = store._read("external")
            except LimitExceeded as exc:
                # An unreadable or oversized record is a refusal, never a
                # silent overwrite of a receipt the product cannot read.
                raise Denied(f"Invalid external publication receipt: {publication_id}") from exc
            if existing is not None:
                stored = _external_record(existing, publication_id)
                if any(stored.get(key) != record.get(key) for key in compared):
                    raise Denied(f"Conflicting external publication receipt: {publication_id}")
                cls._update_key_index(store, stored, "external", PublicationEvidence(),
                                      before_stamp)
                return stored
            cls._write_with_key_index(store, record, "external",
                                      PublicationEvidence(), before_stamp)
        return record

    @classmethod
    def _receipt_store(cls, project_root):
        """Build the bounded, directory-checked store shared by receipt scans."""
        store = cls.__new__(cls)
        store.id = ""
        store.root = Path(project_root) / "evidence" / "publications"
        store.identity = None
        store._check_directories()
        return store

    @classmethod
    def _receipt_store_for_id(cls, project_root, publication_id):
        """Build a checked store for one receipt ID."""
        if not isinstance(publication_id, str) or not DIGEST.fullmatch(publication_id):
            raise Denied("Invalid publication ID")
        store = cls._receipt_store(project_root)
        store.id = publication_id
        return store

    @classmethod
    def _read_key_index(cls, store, policy):
        """Read and structurally validate the writer-owned key index."""
        _, path, _ = _key_index_paths(store)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        try:
            raw = safe_read(path.parent, path.name, KEY_INDEX_BYTES)
        except LimitExceeded as exc:
            raise Denied("Invalid publication key index") from exc
        try:
            value = loads_finite(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise Denied("Invalid publication key index") from exc
        if (not isinstance(value, dict)
                or set(value) != {"schema", "stamp", "entries", "keys", "incomplete"}
                or value["schema"] != KEY_INDEX_SCHEMA
                or type(value["incomplete"]) is not bool):
            raise Denied("Invalid publication key index")
        entries, keys, stamp = value["entries"], value["keys"], value["stamp"]
        if (not isinstance(entries, list) or not isinstance(keys, list)
                or len(entries) > policy.record_limit or len(keys) > policy.record_limit):
            raise Denied("Invalid publication key index")
        if stamp is not None and (not isinstance(stamp, list) or len(stamp) != 2
                                  or any(type(part) is not int for part in stamp)):
            raise Denied("Invalid publication key index")
        from .vcs import check_branch
        for entry in entries:
            if (not isinstance(entry, dict)
                    or set(entry) != {"publication_id", "kind", "sha256"}
                    or not isinstance(entry["publication_id"], str)
                    or not DIGEST.fullmatch(entry["publication_id"])
                    or entry["kind"] not in ("external", "receipt")
                    or not isinstance(entry["sha256"], str)
                    or not DIGEST.fullmatch(entry["sha256"])):
                raise Denied("Invalid publication key index")
        indexed_ids = set()
        for key in keys:
            if not isinstance(key, dict):
                raise Denied("Invalid publication key index")
            ids = key.get("publication_ids")
            if (set(key) != {"branch", "code_digest", "publication_ids"}
                    or not isinstance(key["code_digest"], str)
                    or not DIGEST.fullmatch(key["code_digest"])
                    or not isinstance(ids, list) or not ids
                    or len(ids) > policy.record_limit):
                raise Denied("Invalid publication key index")
            check_branch(key["branch"])
            for publication_id in ids:
                if (not isinstance(publication_id, str)
                        or not DIGEST.fullmatch(publication_id)
                        or publication_id in indexed_ids):
                    raise Denied("Invalid publication key index")
                indexed_ids.add(publication_id)
        if indexed_ids != {entry["publication_id"] for entry in entries}:
            raise Denied("Invalid publication key index")
        return value

    @classmethod
    def _build_key_index(cls, store, policy):
        """Rebuild the key index from directory names at write time.

        This is the one bounded scan, owned by the receipt writer. A lookup
        never calls it: without a writer-built index, keyed resolution stays
        unavailable and ordinary listing remains the only reader. An
        unreadable, oversized or invalid individual receipt is not indexed and
        marks the result incomplete, so keyed lookups fail closed without
        blocking the writer that is recording a new receipt. Duplicate
        identities, malformed receipt names and configured bounds still refuse
        the whole rebuild.
        """
        try:
            names = sorted(os.listdir(store.root))
        except (FileNotFoundError, NotADirectoryError):
            names = []
        except OSError as exc:
            raise Denied("Publication evidence is unavailable") from exc
        if len(names) > policy.name_processing_limit:
            raise Denied("Publication evidence scan incomplete")
        entries, keys, seen = [], {}, set()
        incomplete = False
        for name in names:
            match = RECEIPT_FILE.fullmatch(name)
            if match is None:
                if MALFORMED_RECEIPT_NAME.search(name):
                    raise Denied("Malformed publication receipt name")
                continue
            publication_id, kind = match.groups()
            if publication_id in seen:
                raise Denied("Multiple publication receipt files for one identity")
            seen.add(publication_id)
            if len(seen) > policy.record_limit:
                raise Denied("Publication evidence scan incomplete")
            try:
                receipt = cls.read_receipt(Path(store.root).parent.parent, publication_id)
                raw = safe_read(store.root, name, RECORD_BYTES)
            except (Denied, LimitExceeded, OSError):
                incomplete = True
                continue
            entries.append({"publication_id": publication_id, "kind": kind,
                            "sha256": digest(raw)})
            key = (receipt["branch"], receipt["code_digest"])
            keys.setdefault(key, []).append(publication_id)
        return {"schema": KEY_INDEX_SCHEMA, "stamp": _directory_stamp(store.root),
                "entries": entries, "keys": [
                    {"branch": branch, "code_digest": code_digest,
                     "publication_ids": ids}
                    for (branch, code_digest), ids in keys.items()],
                "incomplete": incomplete}

    @classmethod
    def _prepare_key_index(cls, store, record, kind, policy, before_stamp, raw):
        """Compute the prospective index while the caller holds its lock.

        ``raw`` is the exact receipt bytes: ``canonical(record)`` for a new
        write, or the bounded read of an existing receipt for a replay, so a
        new receipt never has to be read back from disk. A capacity refusal
        writes only the current index view with a refreshed stamp and commits
        no receipt. ``before_stamp`` is the directory stamp observed before
        the writer's own receipt write (or, for a replay, around its read). A
        changed stamp, a receipt-like name the index does not imply, or an
        incomplete/corrupt index forces a bounded rebuild from current
        directory names; only an unchanged view is ever appended to. An
        append never clears incompleteness -- only a fresh rebuild can.
        """
        try:
            index = cls._read_key_index(store, policy)
        except (Denied, LimitExceeded, OSError):
            # The writer owns this file: replace an unusable index with a
            # rebuilt one instead of blocking every later receipt write.
            index = None
        if (index is None or index["incomplete"]
                or index["stamp"] != before_stamp
                or _receipt_names_differ(store, policy, index)):
            index = cls._build_key_index(store, policy)
        publication_id = record["publication_id"]
        entry = {"publication_id": publication_id, "kind": kind,
                 "sha256": digest(raw)}
        entries = [item for item in index["entries"]
                   if not (item["publication_id"] == publication_id and item["kind"] == kind)]
        entries.append(entry)
        branch, code_digest = record["branch"], record["code_digest"]
        keys = []
        for key in index["keys"]:
            ids = [item for item in key["publication_ids"] if item != publication_id]
            if ids:
                keys.append({**key, "publication_ids": ids})
        for key in keys:
            if key["branch"] == branch and key["code_digest"] == code_digest:
                if publication_id not in key["publication_ids"]:
                    key["publication_ids"].append(publication_id)
                break
        else:
            keys.append({"branch": branch, "code_digest": code_digest,
                         "publication_ids": [publication_id]})
        value = {"schema": KEY_INDEX_SCHEMA, "stamp": before_stamp,
                 "entries": entries, "keys": keys,
                 "incomplete": index["incomplete"]}
        # The stamp is refreshed after a receipt commit; reserve the widest
        # native stamp representation for this pre-commit capacity check.
        reason = None
        if len(entries) > policy.record_limit:
            reason = "Publication evidence scan incomplete"
        elif len(canonical({**value, "stamp": [sys.maxsize, sys.maxsize]})) > KEY_INDEX_BYTES:
            reason = "Publication key index exceeds capacity"
        if reason is not None:
            # No receipt was committed, so refresh only the writer's stamp;
            # the current entries still describe the unchanged directory and
            # previously resolved keys stay valid.
            cls._write_key_index(store, {**value, "entries": index["entries"],
                                         "keys": index["keys"]})
            raise Denied(reason)
        return value

    @classmethod
    def _write_key_index(cls, store, value):
        """Publish a prepared index from the writer that holds its lock."""
        directory, index_path, _ = _key_index_paths(store)
        value["stamp"] = _directory_stamp(store.root)
        write_json(index_path, value)
        sync_dir(directory)

    @classmethod
    def _update_key_index(cls, store, record, kind, policy, before_stamp):
        """Refresh the index for an already-written receipt (a replay)."""
        directory, _, lock_path = _key_index_paths(store)
        mkdir(directory)
        with kernel_lock(lock_path):
            raw = safe_read(store.root,
                            _receipt_file_name(record["publication_id"], kind),
                            RECORD_BYTES)
            cls._write_key_index(
                store, cls._prepare_key_index(store, record, kind, policy,
                                              before_stamp, raw))

    @classmethod
    def _write_with_key_index(cls, store, record, kind, policy, before_stamp):
        """Commit a new receipt and its index as one lock-scoped operation."""
        directory, _, lock_path = _key_index_paths(store)
        mkdir(directory)
        with kernel_lock(lock_path):
            value = cls._prepare_key_index(store, record, kind, policy,
                                           before_stamp, canonical(record))
            store._write(kind, record)
            cls._write_key_index(store, value)

    @classmethod
    def _receipt_names(cls, project_root) -> list[str]:
        """Enumerate receipt-directory names without reading or validating files."""
        store = cls._receipt_store(project_root)
        try:
            return sorted(os.listdir(store.root))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as exc:
            raise Denied("Publication evidence is unavailable") from exc

    @classmethod
    def _scan_receipts(cls, project_root, names, *, strict: bool,
                       policy: PublicationEvidence) -> tuple[list[tuple[tuple[int, str], dict]], bool]:
        """Validate one bounded directory scan into ordering keys plus summaries.

        The name-processing and record bounds, duplicate-ID refusal, strict
        validation and ``scan_stopped`` meaning are the ones ``list_receipts``
        publishes. Every candidate is validated before filtering so corrupt
        evidence cannot be mistaken for absence or uniqueness. The ordering
        key stays beside its summary so the public row keeps its schema.
        """
        scan_stopped = len(names) > policy.name_processing_limit
        records = []
        seen = set()
        for name in names[:policy.name_processing_limit]:
            match = RECEIPT_FILE.fullmatch(name)
            if match is None:
                continue
            publication_id = match.group(1)
            if publication_id in seen:
                if strict:
                    raise Denied("Multiple publication receipt files for one identity")
                continue
            seen.add(publication_id)
            if len(seen) > policy.record_limit:
                scan_stopped = True
                break
            try:
                record = cls.read_receipt(project_root, publication_id)
                records.append((_receipt_order(record), _summary(record)))
            except (Denied, LimitExceeded, OSError):
                if strict:
                    raise
                # An unreadable, oversized or invalid record is never listed
                # unvalidated, and it never denies the records beside it.
        return records, scan_stopped

    @classmethod
    def list_receipts(cls, project_root: Path, *, code_digest: str | None = None,
                      branch: str | None = None, published_sha: str | None = None,
                      limit: int = 100, cursor: str | None = None, strict: bool = False,
                      policy: PublicationEvidence | None = None) -> dict:
        """List the recorded publication receipts of one project, newest first.

        Schema: one bounded page, ``{"publications": [...], "total": int,
        "truncated": bool, "scan_stopped": bool, "next_cursor": str | None}``.
        A summary carries the publication ID, operation, branch, published SHA,
        code digest, approval and the optional source commit and run reference.
        Receipts sort by ``recorded_at`` descending and publication ID
        descending for equal instants; unstamped legacy product receipts sort
        last. ``next_cursor`` continues strictly after the last returned row;
        it is null on the final page. ``total`` counts every matching receipt
        across all pages (a lower bound when ``scan_stopped``), and
        ``truncated`` says whether matching rows follow this page. A cursor
        does not change ``total``. ``scan_stopped`` says that a scan bound cut
        the search short, so ``total`` is a lower bound and a matching receipt
        may exist beyond it: an empty page with ``scan_stopped`` means "not
        found within the scanned set", never "no receipt exists". Filters:
        exact code digest, branch and published SHA matches.
        Bounds: at most 100 entries per page; cursors are opaque strings of at
        most 128 characters; publication_evidence policy bounds name processing
        and candidate validation (defaults 10000/1000). All directory names
        are materialized and sorted before these bounds apply; they do not
        bound the initial enumeration or its memory.
        Trust: locally recorded evidence; an unreadable, oversized or
        invalid record is skipped rather than returned unvalidated, and it
        never denies the valid records beside it. Failure: ``Denied`` on
        invalid arguments or cursor; a missing publications directory lists
        nothing. Internal ``strict`` consumers refuse any unreadable
        candidate rather than treating skipped corrupt evidence as proof of
        uniqueness.
        """
        policy = policy if policy is not None else PublicationEvidence()
        cursor_key = _listing_cursor_key(cursor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise Denied("Invalid publication listing limit")
        if code_digest is not None and (not isinstance(code_digest, str)
                                        or not DIGEST.fullmatch(code_digest)):
            raise Denied("Invalid code digest for publication listing")
        if published_sha is not None and (not isinstance(published_sha, str)
                                          or not SHA.fullmatch(published_sha)):
            raise Denied("Invalid published SHA for publication listing")
        if branch is not None:
            from .vcs import check_branch
            check_branch(branch)
        names = cls._receipt_names(project_root)
        entries, scan_stopped = cls._scan_receipts(
            project_root, names, strict=strict, policy=policy)
        matches = [entry for entry in entries
                   if (code_digest is None or entry[1]["code_digest"] == code_digest)
                   and (branch is None or entry[1]["branch"] == branch)
                   and (published_sha is None or entry[1]["published_sha"] == published_sha)]
        total = len(matches)
        matches.sort(key=lambda entry: entry[0], reverse=True)
        if cursor_key is not None:
            matches = [entry for entry in matches if entry[0] < cursor_key]
        page = matches[:limit]
        more = len(matches) > len(page)
        return {"publications": [summary for _, summary in page], "total": total,
                "truncated": more, "scan_stopped": scan_stopped,
                "next_cursor": _listing_cursor_token(page[-1][0]) if more else None}

    @classmethod
    def key_observation(cls, project_root: Path, *,
                        policy: PublicationEvidence | None = None) -> ReceiptKeyObservation:
        """Create a lazy, call-local observation of the writer-owned key index.

        No I/O happens here. The first answer loads the index, compares the
        receipt-directory stamp once and checks every indexed receipt's
        SHA-256 against its bounded no-follow byte read; later keys reuse that
        one bounded observation. The result is owned by the caller and is never
        kept on the module or on a project/store object: each poll or other
        coherent observation builds a fresh one.
        """
        return ReceiptKeyObservation(
            project_root, policy if policy is not None else PublicationEvidence())

    @classmethod
    def receipt_for_key(cls, project_root: Path, branch: str, code_digest: str, *,
                        policy: PublicationEvidence,
                        observation: ReceiptKeyObservation | None = None) -> str | None:
        """Resolve the one receipt bound to a branch and whole-capture digest.

        Schema: a publication ID, or ``None`` for zero or ambiguous evidence.
        ``observation`` optionally supplies one call-local key inventory (see
        ``key_observation``) shared by several distinct keys; without it a
        fresh inventory is used for this call. It must belong to the same
        project root and policy, or the key is refused.
        Bounds: on a fresh observation the writer-owned index is one bounded
        fixed-path read; every indexed receipt is read once with its create-only
        bound and the receipt directory stamp is compared before any key is
        resolved. A supplied observation answers all its keys from those
        reads. There is no directory enumeration. The byte read checked against
        the index digest is passed to receipt validation, whose process-local
        reuse is keyed by those exact bytes; no metadata witness authorizes
        validation reuse.
        Trust: the index is written only by the receipt writer. A missing index
        means completeness cannot be established and resolves nothing. A stamp
        or content mismatch, an incomplete rebuild, a corrupt index or a
        corrupt candidate is Denied, never treated as absence. An out-of-band
        receipt name change fails closed until the writer rebuilds the index.
        Failure: ``Denied`` for invalid arguments or invalid evidence.
        """
        policy = policy if policy is not None else PublicationEvidence()
        from .vcs import check_branch
        check_branch(branch)
        if not isinstance(code_digest, str) or not DIGEST.fullmatch(code_digest):
            raise Denied("Invalid code digest for publication listing")
        if observation is None:
            observation = cls.key_observation(project_root, policy=policy)
        elif (not isinstance(observation, ReceiptKeyObservation)
              or observation.root != Path(project_root)
              or observation.policy != policy):
            raise Denied("Invalid publication key observation")
        return observation.resolve(branch, code_digest)

    def complete(self, intent: dict, published_sha: str, observation=None) -> dict:
        if not isinstance(published_sha, str) or not SHA.fullmatch(published_sha):
            raise Denied(f"Invalid published SHA: {self.id}")
        receipt = {**intent, "published_sha": published_sha}
        if lineage.validate(observation, published_sha) is not None:
            receipt["lineage"] = observation
        before_stamp = _directory_stamp(self.root)
        cls = type(self)
        cls._write_with_key_index(self, receipt, "receipt",
                                  PublicationEvidence(), before_stamp)
        return receipt


class ReceiptKeyObservation:
    """One bounded, call-local answer set for writer-indexed receipt keys.

    The first answer reads the index once, pins its directory stamp and checks
    the SHA-256 of every indexed receipt against one bounded no-follow read of
    its bytes. Later answers resolve distinct (branch, code_digest) keys from
    those same bytes with the ordinary candidate validation. Only the bytes of
    unambiguous candidates are retained; all other reads are checked and
    discarded. Bounds are those of the index (``policy.record_limit`` entries
    and keys) and ``RECORD_BYTES`` per receipt. No receipt-directory
    enumeration, long-lived process cache or cross-poll reuse is introduced:
    the observation dies with the call that owns it.
    """

    def __init__(self, project_root, policy):
        self.root = Path(project_root)
        self.policy = policy
        self._loaded = False
        self._keys = {}
        self._observed = {}

    def _load(self):
        if self._loaded:
            return
        store = Publication._receipt_store(self.root)
        index = Publication._read_key_index(store, self.policy)
        if index is not None:
            if index["stamp"] != _directory_stamp(store.root):
                raise Denied("Publication evidence changed")
            if index["incomplete"]:
                raise Denied("Publication evidence scan incomplete")
            for key in index["keys"]:
                self._keys.setdefault(
                    (key["branch"], key["code_digest"]), []).extend(key["publication_ids"])
            candidates = {ids[0] for ids in self._keys.values() if len(ids) == 1}
            for entry in index["entries"]:
                name = _receipt_file_name(entry["publication_id"], entry["kind"])
                try:
                    raw = safe_read(store.root, name, RECORD_BYTES)
                except FileNotFoundError as exc:
                    raise Denied("Publication evidence changed") from exc
                sha = digest(raw)
                if sha != entry["sha256"]:
                    raise Denied("Publication evidence changed")
                if entry["publication_id"] in candidates:
                    self._observed[entry["publication_id"]] = {entry["kind"]: (raw, sha)}
        self._loaded = True

    def resolve(self, branch, code_digest) -> str | None:
        """Resolve one exact key from this observation's pinned inventory."""
        self._load()
        ids = self._keys.get((branch, code_digest), ())
        if len(ids) != 1:
            return None
        publication_id = ids[0]
        observed = self._observed.get(publication_id)
        if observed is None:
            raise Denied("Publication evidence changed")
        store = Publication._receipt_store_for_id(self.root, publication_id)
        receipt = Publication._validated_receipt(store, publication_id, observed=observed)
        if receipt["code_digest"] != code_digest or receipt["branch"] != branch:
            return None
        return publication_id
