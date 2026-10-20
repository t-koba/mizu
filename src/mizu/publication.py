"""Immutable publication identity/evidence; no retries or remote reconciliation.

Records live in project evidence/publications, outside the workspace. Callers
hold the per-ID kernel lock across inspection and dispatch. Each JSON record is
create-only and read with a fixed 8 KiB no-follow bound. A dispatch marker is a
conservative point of no return: without a valid receipt it requires manual
reconciliation, even if the process died before actually starting the adapter.

Two receipt shapes share this store. The schema-1 receipt is the product's own
publish path: intent, dispatch marker and receipt for one publication ID. The
schema-2 external receipt is recorded after the fact by an operator-run
publisher that pushed outside the product. It is create-only, idempotent for an
identical request, and carries the same caller-visible fields, so an exact-SHA
CI wait can be pinned to either shape.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import stat
from pathlib import Path

from . import process
from .errors import Denied, LimitExceeded
from .fs import (DIGEST, canonical, digest, identifier, loads_finite,
                 lock as kernel_lock, now, safe_read, sync_dir, write_json)

SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
RECORD_BYTES = 8192

#: Operation label of a receipt recorded by an operator-run publisher.
EXTERNAL_OP = "external"
#: Schema version of an external receipt record.
EXTERNAL_SCHEMA = 2
#: Exact fields of an external receipt record.
EXTERNAL_FIELDS = {"schema", "publication_id", "op", "branch", "published_sha",
                   "code_digest", "approval", "source_commit", "run", "recorded_at"}
#: File names of recorded receipts inside the publications directory.
RECEIPT_FILE = re.compile(r"([0-9a-f]{64})\.(external|receipt)\.json\Z")
#: Directory entries read by one bounded listing.
RECEIPT_SCAN_LIMIT = 10000
#: Candidate records validated by one bounded listing.
RECEIPT_RECORD_LIMIT = 1000
#: Maximum characters of an operator approval or run reference.
REFERENCE_CHARS = 256


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


def _external_record(record, publication_id: str) -> dict:
    """Validate one external receipt record against its stable publication ID.

    Schema: exactly the recorded fields, schema 2, operation ``external``, and
    a publication ID equal to the file name and to the digest of its identity.
    Bounds: 8 KiB record (read bound), 40/64-hex commit SHAs, a 64-hex code
    digest, and references of at most 256 characters. Trust: the operator
    asserts the recorded content; it is never inferred from a branch head.
    Failure: ``Denied`` for any mismatch.
    """
    if not isinstance(record, dict) or set(record) != EXTERNAL_FIELDS:
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    if (type(record.get("schema")) is not int or record["schema"] != EXTERNAL_SCHEMA
            or record.get("op") != EXTERNAL_OP
            or record.get("publication_id") != publication_id):
        raise Denied(f"Invalid external publication receipt: {publication_id}")
    identity = {"op": EXTERNAL_OP, "branch": record["branch"],
                "code_digest": record["code_digest"],
                "published_sha": record["published_sha"]}
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
    return {"publication_id": record["publication_id"], "op": record["op"],
            "branch": record["branch"], "published_sha": record["published_sha"],
            "code_digest": record["code_digest"], "approval": record["approval"],
            "source_commit": record.get("source_commit"), "run": record.get("run")}


def source_commit(workspace: Path, *, cancel=lambda: False) -> str:
    """Read only this workspace's Git HEAD commit, never a remote/parent ref."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    result = process.run(
        ["git", "--git-dir=.git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=workspace, env=env, timeout=5, maximum=4096, cancel=cancel)
    sha = result.stdout.strip()
    if result.reason != "exited" or result.exit_code != 0 or not SHA.fullmatch(sha):
        raise Denied("Publication requires a valid local workspace Git HEAD commit")
    return sha


class Publication:
    def __init__(self, project_root: Path, source: str, code_digest: str,
                 branch: str, op: str):
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
        self.id = digest(canonical(self.identity))
        self.root = project_root / "evidence" / "publications"

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

    def _read(self, kind: str) -> dict | None:
        try:
            self.path(kind).lstat()
        except FileNotFoundError:
            return None
        raw = safe_read(self.root, self.path(kind).name, RECORD_BYTES)
        try:
            record = loads_finite(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise Denied(f"Invalid publication {kind}: {self.id}") from exc
        if not isinstance(record, dict):
            raise Denied(f"Invalid publication {kind}: {self.id}")
        return record

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
        evidence is refused, never reconstructed from a branch head.
        """
        if not isinstance(publication_id, str) or not DIGEST.fullmatch(publication_id):
            raise Denied("Invalid publication ID")
        store = cls.__new__(cls)
        store.id = publication_id
        store.root = Path(project_root) / "evidence" / "publications"
        store.identity = None
        store._check_directories()
        intent = store._read("intent")
        dispatched = store._read("dispatch")
        receipt = store._read("receipt")
        if intent is None and dispatched is None and receipt is None:
            # An operator-run publisher records one receipt without the
            # product's intent/dispatch trio; every other combination stays
            # unresolved exactly as before.
            external = store._read("external")
            if external is not None:
                return _external_record(external, publication_id)
            raise Denied(f"Publication receipt unavailable or unresolved: {publication_id}")
        if intent is None or dispatched is None or receipt is None:
            raise Denied(f"Publication receipt unavailable or unresolved: {publication_id}")
        from .vcs import PUBLISH_OPS, check_branch
        fields = {"source_commit", "code_digest", "branch", "op", "schema",
                  "publication_id", "approval", "run"}
        if set(intent) != fields:
            raise Denied(f"Invalid publication intent: {publication_id}")
        identity = {key: intent[key] for key in ("source_commit", "code_digest", "branch", "op")}
        if (type(intent.get("schema")) is not int or intent.get("schema") != 1
                or intent.get("publication_id") != publication_id
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
                or receipt != {**intent, "published_sha": published_sha}):
            raise Denied(f"Invalid publication receipt: {publication_id}")
        return receipt

    def prepare(self, approval: str, run: str) -> tuple[dict, dict | None]:
        """Reuse intent/receipt; refuse any uncertain prior dispatch."""
        self._check_directories()
        intent = self._read("intent")
        receipt = self._read("receipt")
        dispatched = self._read("dispatch")
        if intent is None:
            if receipt is not None or dispatched is not None:
                raise Denied(f"Missing publication intent: {self.id}")
            intent = {**self.identity, "schema": 1, "publication_id": self.id,
                      "approval": identifier(approval), "run": identifier(run)}
            self._write("intent", intent)
        expected = {**self.identity, "schema": 1, "publication_id": self.id}
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
                    or receipt != {**intent, "published_sha": sha}):
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
                        run: str | None = None) -> dict:
        """Record one operator-run publication as a durable receipt.

        The publisher pushes outside the product, then records what it
        published. Schema: branch (ref name), published SHA (40 or 64 hex),
        code digest (64 hex), an approval reference, an optional source commit
        and an optional run reference; the record carries schema 2, the
        operation ``external``, the publication ID and the recording time.
        Identity: the publication ID is the digest of the operation, branch,
        code digest and published SHA, so the same publication keeps one ID.
        Bounds: 8 KiB record, 256 characters per reference. Trust: the
        operator asserts this content; the product never infers a published
        SHA from a branch head. Retry: none; the write is create-only and an
        identical request replays the stored record, a conflicting one is
        refused. Failure: ``Denied`` for invalid input or a conflicting
        receipt.
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
        publication_id = digest(canonical(identity))
        store = cls.__new__(cls)
        store.id = publication_id
        store.root = Path(project_root) / "evidence" / "publications"
        store.identity = identity
        record = {"schema": EXTERNAL_SCHEMA, "publication_id": publication_id,
                  "op": EXTERNAL_OP, "branch": branch, "published_sha": published_sha,
                  "code_digest": code_digest, "approval": approval,
                  "source_commit": source_commit, "run": run, "recorded_at": now()}
        with kernel_lock(store.lock):
            try:
                existing = store._read("external")
            except LimitExceeded as exc:
                # An unreadable or oversized record is a refusal, never a
                # silent overwrite of a receipt the product cannot read.
                raise Denied(f"Invalid external publication receipt: {publication_id}") from exc
            if existing is not None:
                stored = _external_record(existing, publication_id)
                if any(stored[key] != record[key] for key in
                       ("branch", "published_sha", "code_digest", "approval",
                        "source_commit", "run")):
                    raise Denied(f"Conflicting external publication receipt: {publication_id}")
                return stored
            store._write("external", record)
        return record

    @classmethod
    def list_receipts(cls, project_root: Path, *, code_digest: str | None = None,
                      branch: str | None = None, published_sha: str | None = None,
                      limit: int = 100) -> dict:
        """List the recorded publication receipts of one project.

        Schema: one bounded page, ``{"publications": [...], "total": int,
        "truncated": bool, "scan_stopped": bool}``. A summary carries the
        publication ID, operation, branch, published SHA, code digest, approval
        and the optional source commit and run reference. ``total`` counts the
        matching receipts the scan found and ``truncated`` says whether the
        page dropped matches. ``scan_stopped`` says that a scan bound cut the
        search short, so ``total`` is a lower bound and a matching receipt may
        exist beyond it: an empty page with ``scan_stopped`` means "not found
        within the scanned set", never "no receipt exists". Filters: exact
        code digest, branch and published SHA matches.
        Bounds: at most 100 entries per page, at most 10000 directory entries
        read and at most 1000 candidate records validated.
        Trust: locally recorded evidence; an unreadable, oversized or
        invalid record is skipped rather than returned unvalidated, and it
        never denies the valid records beside it. Failure: ``Denied`` on
        invalid arguments; a missing publications directory lists nothing.
        """
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
        store = cls.__new__(cls)
        store.id = ""
        store.root = Path(project_root) / "evidence" / "publications"
        store.identity = None
        store._check_directories()
        try:
            names = sorted(os.listdir(store.root))
        except (FileNotFoundError, NotADirectoryError):
            return {"publications": [], "total": 0, "truncated": False, "scan_stopped": False}
        except OSError as exc:
            raise Denied("Publication evidence is unavailable") from exc
        records = []
        seen = set()
        scan_stopped = len(names) > RECEIPT_SCAN_LIMIT
        for name in names[:RECEIPT_SCAN_LIMIT]:
            match = RECEIPT_FILE.fullmatch(name)
            if match is None:
                continue
            publication_id = match.group(1)
            if publication_id in seen:
                continue
            seen.add(publication_id)
            if len(seen) > RECEIPT_RECORD_LIMIT:
                scan_stopped = True
                break
            try:
                record = cls.read_receipt(project_root, publication_id)
            except (Denied, LimitExceeded, OSError):
                # An unreadable, oversized or invalid record is never listed
                # unvalidated, and it never denies the records beside it.
                continue
            if code_digest is not None and record.get("code_digest") != code_digest:
                continue
            if branch is not None and record.get("branch") != branch:
                continue
            if published_sha is not None and record.get("published_sha") != published_sha:
                continue
            records.append(_summary(record))
        records.sort(key=lambda item: item["publication_id"])
        page = records[:limit]
        return {"publications": page, "total": len(records),
                "truncated": len(records) > len(page), "scan_stopped": scan_stopped}

    def complete(self, intent: dict, published_sha: str) -> dict:
        if not isinstance(published_sha, str) or not SHA.fullmatch(published_sha):
            raise Denied(f"Invalid published SHA: {self.id}")
        receipt = {**intent, "published_sha": published_sha}
        self._write("receipt", receipt)
        return receipt
