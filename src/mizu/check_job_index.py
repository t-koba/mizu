"""Index domain of check jobs: current job records with their validated evidence.

One row per entry of ``check-jobs/``. A job directory with a ``job.json``
becomes a row holding the exact job record plus the columns readers filter
on. ``valid`` is false, with the ``code`` an operator can see, for a record
whose schema, scope or retained result fails validation, and for a directory
entry that is not a job; those never reach callers. Evidence is therefore
validated when a job is written, when the index is built and in the audit,
never on a read. Subscription overlays (a job sharing another's execution)
are applied by readers from the original's row, because they change
whenever the original does. Wait files have their own rows in the same domain;
a null wait job ID means unknown reference state, never an absent reference.
"""
from __future__ import annotations

import json
import os
import stat

from .errors import Denied, LimitExceeded
from .fs import canonical, digest, safe_read
from .ledger import Domain

_TABLES = """
CREATE TABLE check_waits(name TEXT PRIMARY KEY, job_id TEXT);
CREATE INDEX check_waits_job ON check_waits(job_id);
CREATE TABLE check_jobs(
    id TEXT PRIMARY KEY, valid INTEGER NOT NULL, code TEXT, role TEXT, status TEXT,
    acknowledged INTEGER, detached INTEGER, wake INTEGER, shared_job TEXT, created_at TEXT,
    proof_pending INTEGER, job TEXT, lifecycle_key TEXT, shared_key TEXT, purpose TEXT,
    cache_request TEXT, request_sequence INTEGER, passed INTEGER, cancel_requested INTEGER);
CREATE TABLE check_completion_changes(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, job_id TEXT NOT NULL);
CREATE INDEX check_completion_changes_role ON check_completion_changes(role, seq);
CREATE INDEX check_jobs_role ON check_jobs(role, valid, acknowledged);
CREATE INDEX check_jobs_shared ON check_jobs(shared_job);
CREATE INDEX check_jobs_shared_key ON check_jobs(valid, shared_key, status, passed, shared_job, detached, cancel_requested, created_at);
CREATE INDEX check_jobs_purpose ON check_jobs(valid, role, purpose, status, shared_job);
CREATE INDEX check_jobs_request ON check_jobs(valid, cache_request, status, detached, cancel_requested, request_sequence);
CREATE INDEX check_jobs_request_sequence ON check_jobs(valid, cache_request, request_sequence);
CREATE INDEX check_jobs_lifecycle ON check_jobs(lifecycle_key);
CREATE INDEX check_jobs_status ON check_jobs(valid, status, shared_job);
"""
TERMINAL = ("completed", "interrupted", "refused", "cancelled", "cleanup_failed")


def effective_status(status, *, detached, shared, original_status=None):
    """The status every reader projects: detached is cancelled, shared follows its original."""
    if detached:
        return 'cancelled'
    if shared and original_status is not None:
        return original_status
    return status


def completion_pairs(rows):
    """Role/job pairs whose effective result is terminal in one row inventory."""
    by_id = {row[0]: row for row in rows if len(row) > 2}
    for job_id, row in by_id.items():
        _, valid, _, role, status, _, detached, _, shared, *_ = row
        if not valid:
            continue
        original_status = None
        if shared and not detached:
            original = by_id.get(shared)
            if original is not None and original[1]:
                original_status = original[4]
        if effective_status(status, detached=detached, shared=shared,
                            original_status=original_status) in TERMINAL:
            yield role, job_id


def lifecycle_key(job):
    """Identity shared by every command lookup on the same frozen inputs."""
    return digest(canonical({
        'source': job.get('code_digest'), 'execution': job.get('execution'),
        'environment': job.get('environment'), 'verify': job.get('kind') == 'verify',
        'cache_input': job.get('cache_input') if job.get('kind') == 'verify' else None}))


class _CheckJobs(Domain):
    @staticmethod
    def _completion_role(db, job_id: str):
        """Role whose live unit should see this job's effective terminal result."""
        row = db.execute(
            "SELECT valid, role, status, shared_job, detached FROM check_jobs WHERE id=?",
            (job_id,)).fetchone()
        if row is None or not row[0]:
            return None
        _, role, status, shared, detached = row
        original_status = None
        if shared and not detached:
            original = db.execute(
                "SELECT valid, status FROM check_jobs WHERE id=?", (shared,)).fetchone()
            if original is not None and original[0]:
                original_status = original[1]
        status = effective_status(status, detached=detached, shared=shared,
                                  original_status=original_status)
        return role if status in TERMINAL else None

    def put(self, db, entry) -> None:
        if len(entry) == 2:
            db.execute("INSERT OR REPLACE INTO check_waits VALUES (?, ?)", entry)
            return
        db.execute("INSERT OR REPLACE INTO check_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)

    def replace(self, db, key, entries) -> None:
        # A subscription's effective status comes from its original, so the
        # original's terminal transition is an event for every live subscriber.
        affected = {key} | {row[0] for row in db.execute(
            "SELECT id FROM check_jobs WHERE shared_job=?", (key,))}
        before = {job_id: self._completion_role(db, job_id) for job_id in affected}
        db.execute("DELETE FROM check_waits WHERE name=?", (key,))
        db.execute("DELETE FROM check_jobs WHERE id=?", (key,))
        for entry in entries:
            self.put(db, entry)
        for job_id in sorted(affected):
            role = self._completion_role(db, job_id)
            if role is None:
                # A released job or a subscriber whose original is gone has no
                # live completion left to present; its change row ends here.
                db.execute("DELETE FROM check_completion_changes WHERE job_id=?", (job_id,))
            elif before.get(job_id) is None:
                db.execute("INSERT INTO check_completion_changes(role, job_id) VALUES (?, ?)",
                           (role, job_id))

    def finish(self, db) -> None:
        # A rebuilt index has no incremental history: seed one exact change per
        # currently effective terminal job, oldest first so seq orders by time.
        db.execute("DELETE FROM check_completion_changes")
        rows = db.execute(
            "SELECT id, valid, NULL, role, status, NULL, detached, NULL, shared_job "
            "FROM check_jobs ORDER BY created_at, id").fetchall()
        for role, job_id in completion_pairs(rows):
            db.execute("INSERT INTO check_completion_changes(role, job_id) VALUES (?, ?)",
                       (role, job_id))

    def snapshot(self, db):
        if db is None:
            return {}
        return {row[0]: tuple(row) for table in ("check_jobs", "check_waits")
                for row in db.execute(f"SELECT * FROM {table}")} | {
            row[0]: tuple(row) for row in db.execute(
                "SELECT seq, role, job_id FROM check_completion_changes")}


#: Bump when the derived rows change shape; the next locked use rebuilds them.
CHECK_JOBS = _CheckJobs("check_jobs", 8, _TABLES)


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
            return (name, 0, "non_producer_entry", None, None, None, None, None, None, None, 0, None, None, None, None, None, None, None, None)
        try:
            try:
                job = acceptance.read_job(project, name)
            except LimitExceeded as exc:
                raise acceptance.InvalidProducer(name, "job_file_bytes") from exc
            acceptance._inspect_evidence(project, job)
        except acceptance.InvalidProducer as exc:
            # Keep diagnostics for invalid records; they are never release candidates.
            try:
                raw = json.loads(safe_read(project.root, relative, project.config.limits.file_bytes))
                raw = canonical(raw).decode() if isinstance(raw, dict) and raw.get("id") == name else None
            except (Denied, LimitExceeded, ValueError, UnicodeError, RecursionError):
                raw = None
            return (name, 0, exc.code, None, None, None, None, None, None, None, 0, raw, None, None, None, None, None, None, None)
        from .check_jobs import reusable
        candidate = (job.get("status") == "completed" and reusable(job)
                     and job.get("kind") in ("verify", "experiment", "exec")
                     and all(acceptance._digest(job.get(key)) for key in
                             ("code_digest", "execution", "environment")))
        wake = acceptance._present(project.root, f"check-jobs/{name}/wake.json")
        cancel = acceptance._present(project.root, f"check-jobs/{name}/cancel.json")
        return (name, 1, None, job.get("role"), job.get("status"), int(bool(job.get("acknowledged"))),
                int(bool(job.get("detached"))), int(bool(wake)), job.get("shared_job"),
                job.get("created_at", ""),
                int(job.get("acceptance_scope") is not None and not job.get("acceptance_receipt_id")),
                canonical(job).decode(), lifecycle_key(job) if candidate else None,
                job.get("shared_key"), job.get("purpose"), job.get("cache_request"),
                job.get("request_sequence"),
                int(job["passed"]) if type(job.get("passed")) is bool else None,
                int(bool(cancel)))

    def _wait_row(self, name):
        from . import acceptance
        relative = f"check-jobs/{name}"
        if not acceptance._present(self.project.root, relative):
            return None
        try:
            value = json.loads(safe_read(self.project.root, relative,
                                        self.project.config.limits.file_bytes))
            job_id = value.get('id') if isinstance(value, dict) else None
            return name, job_id if acceptance._digest(job_id) else None
        except (Denied, LimitExceeded, ValueError, UnicodeError, RecursionError):
            return name, None  # Unknown reference state protects all candidates.

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
            if child.name.endswith(".wait.json"):
                yield self._wait_row(child.name)
            elif stat.S_ISDIR(info.st_mode):
                row = self._row(child.name)
                if row is not None:
                    yield row

    def ledger_item(self, key: str, hint, *, recover: bool) -> list:
        row = self._wait_row(key) if key.endswith(".wait.json") else self._row(key)
        return [] if row is None else [row]


def audit(ledger, *, repair: bool) -> dict:
    """Validate every job record and compare the files' replay with the index."""
    from .work_items import MAX_OPEN_PAGE_ROWS
    files = {row[0]: row for row in ledger.domains["check_jobs"][1].ledger_entries(recover=False)}
    snapshot = ledger.snapshot("check_jobs")
    state, indexed = ("missing", {}) if snapshot is None else snapshot
    corrupt = sorted(key for key, row in files.items() if not row[1])
    divergent = sorted(key for key in set(files) | set(indexed)
                       if isinstance(key, str) and files.get(key) != indexed.get(key))
    changes = [value for key, value in indexed.items() if isinstance(key, int)]
    actual = {(value[1], value[2]) for value in changes}
    expected = set(completion_pairs(row for row in files.values() if len(row) > 2))
    change_consistent = len(actual) == len(changes) and actual == expected
    repaired = bool(repair and (divergent or not change_consistent))
    if repaired:
        ledger.rebuild("check_jobs")
    return {"jobs": sum(len(row) != 2 for row in files.values()), "index": state,
            "consistent": not corrupt and not divergent and change_consistent,
            "repaired": repaired,
            "completion_changes": len(changes), "completion_change_consistent": change_consistent,
            "corrupt": [{"id": key, "error": files[key][2] if len(files[key]) != 2 else "wait_json"} for key in corrupt[:MAX_OPEN_PAGE_ROWS]],
            "corrupt_count": len(corrupt),
            "divergent": divergent[:MAX_OPEN_PAGE_ROWS], "divergent_count": len(divergent)}
