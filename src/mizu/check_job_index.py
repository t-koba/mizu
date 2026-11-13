"""Index domain of check jobs: current job records with their validated evidence.

One row per entry of ``check-jobs/``. A job directory with a ``job.json``
becomes a row holding the exact job record plus the columns readers filter
on. ``valid`` is false, with the ``code`` an operator can see, for a record
whose schema, scope or retained result fails validation, and for a directory
entry that is not a job; those never reach callers. Evidence is therefore
validated when a job is written, when the index is built and in the audit,
never on a read. Subscription overlays (a job sharing another's execution)
are applied by readers from the original's row, because they change
whenever the original does.
"""
from __future__ import annotations

import json
import os
import stat

from .errors import Denied, LimitExceeded
from .fs import canonical, digest, safe_read
from .ledger import Domain

_TABLES = """
CREATE TABLE check_jobs(
    id TEXT PRIMARY KEY, valid INTEGER NOT NULL, code TEXT, role TEXT, status TEXT,
    acknowledged INTEGER, detached INTEGER, wake INTEGER, shared_job TEXT, created_at TEXT,
    proof_pending INTEGER, job TEXT);
CREATE INDEX check_jobs_role ON check_jobs(role, valid, acknowledged);
CREATE INDEX check_jobs_status ON check_jobs(valid, status, shared_job);
"""
TERMINAL = ("completed", "interrupted", "refused", "cancelled", "cleanup_failed")


class _CheckJobs(Domain):
    def put(self, db, entry) -> None:
        db.execute("INSERT OR REPLACE INTO check_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)

    def replace(self, db, key, entries) -> None:
        db.execute("DELETE FROM check_jobs WHERE id=?", (key,))
        for entry in entries:
            self.put(db, entry)

    def snapshot(self, db):
        if db is None:
            return {}
        return {row[0]: tuple(row) for row in db.execute("SELECT * FROM check_jobs")}


#: Bump when the derived rows change shape; the next locked use rebuilds them.
CHECK_JOBS = _CheckJobs("check_jobs", 1, _TABLES)


def entry_key(name: str) -> str:
    """Public identity of a directory entry that is not a job."""
    return digest(b"mizu.check-job-entry.v1\0" + os.fsencode(name))


class CheckJobSource:
    """Replays job directories into rows, validating each record and its evidence."""

    def __init__(self, project):
        self.project = project

    def _row(self, name: str) -> tuple | None:
        from . import acceptance
        project = self.project
        relative = f"check-jobs/{name}/job.json"
        if not acceptance._present(project.root, relative):
            return None
        if not acceptance._digest(name):
            # Even oversized leaves must first pass the path and link checks.
            try:
                safe_read(project.root, relative, project.config.limits.file_bytes)
            except LimitExceeded:
                pass
            return (name, 0, "non_producer_entry", None, None, None, None, None, None, None, 0, None)
        try:
            try:
                job = acceptance.read_job(project, name)
            except LimitExceeded as exc:
                raise acceptance.InvalidProducer(name, "job_file_bytes") from exc
            acceptance._inspect_evidence(project, job)
        except acceptance.InvalidProducer as exc:
            # The raw record, when it parses, still lets retention see and evict it.
            try:
                raw = json.loads(safe_read(project.root, relative, project.config.limits.file_bytes))
                raw = canonical(raw).decode() if isinstance(raw, dict) and raw.get("id") == name else None
            except (Denied, LimitExceeded, ValueError, UnicodeError, RecursionError):
                raw = None
            return (name, 0, exc.code, None, None, None, None, None, None, None, 0, raw)
        wake = acceptance._present(project.root, f"check-jobs/{name}/wake.json")
        return (name, 1, None, job.get("role"), job.get("status"), int(bool(job.get("acknowledged"))),
                int(bool(job.get("detached"))), int(bool(wake)), job.get("shared_job"),
                job.get("created_at", ""),
                int(job.get("acceptance_scope") is not None and not job.get("acceptance_receipt_id")),
                canonical(job).decode())

    def ledger_entries(self, *, recover: bool, skip=frozenset()):
        base = self.project.root / "check-jobs"
        from . import acceptance
        if not acceptance._present(self.project.root, "check-jobs"):
            return
        if not stat.S_ISDIR(base.lstat().st_mode):
            raise Denied("Unsafe acceptance producer directory")
        for child in sorted(base.iterdir()):
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise Denied("Unsafe acceptance producer directory")
            if stat.S_ISDIR(info.st_mode):
                row = self._row(child.name)
                if row is not None:
                    yield row

    def ledger_item(self, key: str, hint, *, recover: bool) -> list:
        row = self._row(key)
        return [] if row is None else [row]


def audit(ledger, *, repair: bool) -> dict:
    """Validate every job record and compare the files' replay with the index."""
    from .work_items import MAX_OPEN_PAGE_ROWS
    files = {row[0]: row for row in ledger.domains["check_jobs"][1].ledger_entries(recover=False)}
    snapshot = ledger.snapshot("check_jobs")
    state, indexed = ("missing", {}) if snapshot is None else snapshot
    corrupt = sorted(key for key, row in files.items() if not row[1])
    divergent = sorted(key for key in set(files) | set(indexed) if files.get(key) != indexed.get(key))
    repaired = bool(repair and divergent)
    if repaired:
        ledger.rebuild("check_jobs")
    return {"jobs": len(files), "index": state, "consistent": not corrupt and not divergent,
            "repaired": repaired,
            "corrupt": [{"id": key, "error": files[key][2]} for key in corrupt[:MAX_OPEN_PAGE_ROWS]],
            "corrupt_count": len(corrupt),
            "divergent": divergent[:MAX_OPEN_PAGE_ROWS], "divergent_count": len(divergent)}
