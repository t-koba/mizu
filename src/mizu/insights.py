"""Immutable proposals, separate decisions, and an untrusted Editor outbox."""
from __future__ import annotations

import json
import time
import uuid
import os
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import PREVIEW_BYTES, canonical, digest, identifier, lock, now, read_json, safe_read, write_json, mkdir, DIGEST


class Insights:
    def __init__(self, root: Path, maximum: int = PREVIEW_BYTES):
        self.root, self.maximum = root, maximum

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
            old = read_json(path)
            if old:
                if {k: old[k] for k in payload} != payload:
                    raise Denied("Insight ID was reused with different content")
                return old
            record = {"schema": 1, **payload, "created_at": now()}
            write_json(path, record, exclusive=True)
            return record

    def list(self, *, pending: bool = True, limit: int = 30) -> list[dict]:
        records = []
        for path in sorted((self.root / "inbox").glob("*.json")):
            if path.is_symlink():
                continue
            try:
                item = read_json(path)
                identifier(item["id"])  # File content never becomes a path unchecked.
                decision_path = self.root / "decisions" / f"{item['id']}.json"
                decision = None if decision_path.is_symlink() else read_json(decision_path)
                if pending and decision and decision["action"] != "defer":
                    continue
                records.append({k: item[k] for k in ("id", "source", "title", "created_at", "base_snapshot")})
                records[-1]["decision"] = decision
            except (OSError, ValueError, KeyError, TypeError, AttributeError, Denied):
                continue  # One corrupt proposal must not break the projection.
        return sorted(records, key=lambda x: (x["created_at"], x["id"]))[:limit]

    def read(self, insight_id: str) -> dict:
        path = self.root / "inbox" / f"{identifier(insight_id)}.json"
        if path.is_symlink():
            raise Denied("Insight not found")
        record = read_json(path)
        if not record:
            raise Denied("Insight not found")
        return record

    def generation(self) -> str:
        return digest(canonical(sorted(p.stem for p in (self.root / "inbox").glob("*.json"))))

    def decide(self, insight_id: str, action: str, reason: str, revisit: str, run: str) -> dict:
        self.read(insight_id)
        if action not in ("accept", "modify", "defer", "reject") or not reason.strip():
            raise Denied("Decision requires a supported action and a reason")
        if action == "defer" and not revisit.strip():
            raise Denied("Deferred proposals require a revisit condition")
        record = {"id": insight_id, "action": action, "reason": reason, "revisit": revisit,
                  "run": run, "created_at": now()}
        with lock(self.root / "locks" / "decisions.lock"):
            write_json(self.root / "decision-history" / f"{uuid.uuid4().hex}.json", record, exclusive=True)
            write_json(self.root / "decisions" / f"{insight_id}.json", record)
        return record

    def gc_decided(self, *, keep_days: int = 31) -> int:
        """Remove long-decided proposals. Decisions persist separately in
        decisions/ and decision-history/; deferred and undecided stay."""
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
                    if path.stat().st_mtime > cutoff:
                        continue
                except (OSError, ValueError, AttributeError):
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def ingest_editor(self) -> dict:
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
            self.gc_decided()
        return {"accepted": accepted, "rejected": rejected}
