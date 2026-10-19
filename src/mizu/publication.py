"""Immutable publication identity/evidence; no retries or remote reconciliation.

Records live in project evidence/publications, outside the workspace. Callers
hold the per-ID kernel lock across inspection and dispatch. Each JSON record is
create-only and read with a fixed 8 KiB no-follow bound. A dispatch marker is a
conservative point of no return: without a valid receipt it requires manual
reconciliation, even if the process died before actually starting the adapter.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import process
from .errors import Denied
from .fs import (DIGEST, canonical, digest, identifier, loads_finite, safe_read,
                 sync_dir, write_json)

SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
RECORD_BYTES = 8192


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

    def complete(self, intent: dict, published_sha: str) -> dict:
        if not isinstance(published_sha, str) or not SHA.fullmatch(published_sha):
            raise Denied(f"Invalid published SHA: {self.id}")
        receipt = {**intent, "published_sha": published_sha}
        self._write("receipt", receipt)
        return receipt
