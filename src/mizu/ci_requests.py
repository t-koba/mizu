"""Durable CI request records; one request per published commit.

A CI request asks the provider for one run per workflow at an exact
published commit. The record is create-only: a dispatch marker is written
before the provider is called and the outcome follows it. An unresolved
dispatch is never retried blindly, because a second request would spend a
second run for the same commit; the caller re-observes the CI status
instead.

Records live in the project's evidence tree, outside the workspace.
"""
from __future__ import annotations

from pathlib import Path

from .errors import Denied
from .fs import canonical, digest, loads_finite, mkdir, safe_read, sync_dir, write_json

#: Bounded record size (8 KiB), like the other evidence records.
RECORD_BYTES = 8192


def request_id(publication_id: str, sha: str, workflows: list[str]) -> str:
    """The stable identity of one request: the receipt, the commit, the workflows.

    The same request always names the same record, so a retry replays the
    stored outcome instead of spending a second run. The workflows are sorted:
    the same set of workflows in another order is the same request, not a
    second run.
    """
    return digest(canonical({"publication_id": publication_id, "sha": sha,
                             "workflows": sorted(workflows)}))


class CiRequests:
    """The project's durable CI request records."""

    def __init__(self, root: Path):
        self.root = Path(root) / "evidence" / "ci-requests"

    def _path(self, identity: str, kind: str) -> Path:
        return self.root / f"{identity}.{kind}.json"

    def _read(self, identity: str, kind: str) -> dict | None:
        path = self._path(identity, kind)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raw = safe_read(self.root, path.name, RECORD_BYTES)
        try:
            record = loads_finite(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise Denied(f"Invalid CI request record: {identity}") from exc
        if not isinstance(record, dict):
            raise Denied(f"Invalid CI request record: {identity}")
        return record

    def outcome(self, identity: str) -> dict | None:
        """The recorded outcome of a request, when it is known."""
        return self._read(identity, "request")

    def dispatched(self, identity: str) -> dict | None:
        """The dispatch marker of a request, when it was sent to the provider."""
        return self._read(identity, "dispatch")

    def mark_dispatch(self, identity: str, record: dict) -> None:
        """Write the point of no return before the provider is called."""
        self._write(identity, "dispatch", record)

    def record_outcome(self, identity: str, record: dict) -> None:
        """Write the outcome of a dispatched request."""
        self._write(identity, "request", record)

    def _write(self, identity: str, kind: str, record: dict) -> None:
        mkdir(self.root)
        write_json(self._path(identity, kind), record, exclusive=True)
        sync_dir(self.root)
