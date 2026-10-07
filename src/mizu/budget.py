"""Per-project request admission with an optional shared day total."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .errors import Denied, InfraExceeded
from .fs import atomic_write, canonical, identifier, lock, read_json

#: Day-file byte bound (~160K admissions at ~25 B/entry); count cap binds first.
BUDGET_DAY_BYTES = 4 * 1024 * 1024


class Budget:
    def __init__(self, root: Path, daily: int, retention_days: int = 31,
                 shared_daily: int = 0, timezone: str = "UTC"):
        from .config import resolve_timezone
        self.root, self.daily = root, daily
        self.retention_days = retention_days
        self.shared_daily = shared_daily
        self.timezone = timezone
        self._tz = resolve_timezone(timezone)

    def _today(self) -> str:
        """Current budget day in the configured zone (UTC default unchanged)."""
        return dt.datetime.now(self._tz).date().isoformat()

    def gc(self, *, keep_days: int | None = None) -> int:
        """Remove day-files older than the window. Best-effort; never fails admission.

        Schema/bounds: day-files ``YYYY-MM-DD.json`` in the configured zone; ``keep_days`` defaults to
        the operator-selected ``retention_days`` (``[limits] retention_days``,
        default 31, 0 disables reaping). Trust: local operator state only.
        Retry: best-effort, skips unreadable entries. Evidence: returns the
        reaped count. Failure: never raises for I/O; corrupt names are skipped.
        """
        if keep_days is None:
            keep_days = self.retention_days
        if keep_days <= 0:
            return 0
        removed = 0
        try:
            today = dt.datetime.now(self._tz).date()
            for path in self.root.glob("????-??-??.json"):
                try:
                    if (today - dt.date.fromisoformat(path.stem)).days > keep_days:
                        path.unlink()
                        removed += 1
                except (ValueError, OSError):
                    continue
        except OSError:
            pass
        return removed

    @staticmethod
    def _check_project(project: str) -> str:
        if not project:
            return ""
        try:
            return identifier(project)
        except Denied as exc:
            raise Denied(f"Invalid budget project: {exc}") from exc

    @staticmethod
    def _split(record: dict) -> tuple[list, dict]:
        requests = record.get("requests", [])
        if not isinstance(requests, list):
            requests = []
        projects = record.get("projects", {})
        if not isinstance(projects, dict):
            projects = {}
        clean: dict[str, list] = {}
        for name, entries in projects.items():
            if isinstance(name, str) and isinstance(entries, list):
                clean[name] = entries
        return requests, clean

    def take(self, request_id: str, *, day: str | None = None, project: str = "") -> int:
        """Admit one request against the per-project limit and the shared total.

        Schema: ``request_id`` is an opaque caller string (``run:sequence``);
        ``project`` is the validated project name (``\"\"`` keeps the legacy
        shared-only accounting used by unit callers). Bounds: per-project
        count below ``daily``; shared aggregate below ``shared_daily`` when
        positive; day-file below ``BUDGET_DAY_BYTES``. Trust: local operator
        state under ``budget.lock``. Retry: idempotent per (project,
        request_id); a repeat returns the per-project count without charging.
        Evidence: returns the per-project count (shared count when no project
        is named) plus ``day`` in the configured zone and ``zone`` naming it. Failure: ``InfraExceeded`` when disabled, per-project
        exhausted, shared exhausted, or oversize; legacy files without
        ``projects`` keep their aggregate and start per-project counts at 0.
        """
        day = day or self._today()
        dt.date.fromisoformat(day)
        project = self._check_project(project)
        if not isinstance(request_id, str) or not request_id or "\x00" in request_id or "\n" in request_id:
            raise Denied("Invalid budget request identity")
        if self.daily <= 0:
            raise InfraExceeded("Model calls are disabled: set limits.daily_requests explicitly")
        with lock(self.root / "budget.lock"):
            path = self.root / f"{day}.json"
            record = read_json(path, {"day": day, "zone": self.timezone, "requests": []})
            if not isinstance(record, dict):
                record = {"day": day, "zone": self.timezone, "requests": []}
            record["day"] = day
            record["zone"] = self.timezone
            requests, projects = self._split(record)
            entries = projects.get(project, [])
            if request_id in entries:
                return len(entries)
            if len(entries) >= self.daily:
                if project:
                    raise InfraExceeded(
                        f"{self.timezone} daily model-request budget exhausted for project '{project}'")
                raise InfraExceeded(f"{self.timezone} daily model-request budget exhausted")
            if self.shared_daily > 0 and len(requests) >= self.shared_daily:
                raise InfraExceeded(f"{self.timezone} daily model-request shared budget exhausted")
            entries = [*entries, request_id]
            projects[project] = entries
            if request_id not in requests:
                requests = [*requests, request_id]
            record["requests"] = requests
            record["projects"] = projects
            body = canonical(record)
            if len(body) > BUDGET_DAY_BYTES:
                raise InfraExceeded("Budget day-file exceeds byte bound; inspect data/budget")
            atomic_write(path, body)
            count = len(entries)
        self.gc()
        return count

    def audit(self) -> dict:
        """Scope-correct structural diagnosis of today's ledger.

        Schema: reports the shared day total against ``shared_daily`` plus
        each project's own count against ``daily``. Bounds: reads today's
        day-file only. Trust: local operator state. Failure: never raises;
        structural problems are returned as ``malformed`` entries for the
        caller (doctor) to fail loudly on. Over-limit counts -- legitimate
        exhaustion or a later config reduction -- are reported as data, not
        corruption: only the ledger shape is judged here, never the policy
        values.
        """
        day = self._today()
        path = self.root / f"{day}.json"
        unreadable = False
        try:
            record = read_json(path, None)
        except (OSError, ValueError):
            record, unreadable = None, True
        malformed: list[str] = []
        projects: dict[str, int] = {}
        shared = 0
        if unreadable:
            malformed.append("record-unreadable")
        elif record is None:
            pass
        elif not isinstance(record, dict):
            malformed.append("record-must-be-object")
        else:
            requests = record.get("requests", [])
            if not isinstance(requests, list):
                malformed.append("requests-must-be-list")
            else:
                shared = len(requests)
                if any(not isinstance(entry, str) for entry in requests):
                    malformed.append("requests-entries-must-be-strings")
            raw_projects = record.get("projects", {})
            if not isinstance(raw_projects, dict):
                malformed.append("projects-must-be-object")
            else:
                for name, entries in raw_projects.items():
                    if not isinstance(name, str) or not isinstance(entries, list):
                        malformed.append(f"project-{name}-must-be-list")
                        continue
                    if any(not isinstance(entry, str) for entry in entries):
                        malformed.append(f"project-{name}-entries-must-be-strings")
                    projects[name] = len(entries)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        reaped = self.gc()
        exhausted = sorted(name for name, count in projects.items()
                           if isinstance(count, int) and 0 < self.daily <= count)
        return {"day": day, "zone": self.timezone, "shared_used": shared,
                "shared_limit": self.shared_daily, "limit": self.daily,
                "projects": projects, "malformed": malformed,
                "exhausted_projects": exhausted,
                "shared_exhausted": bool(self.shared_daily > 0 and shared >= self.shared_daily),
                "bytes": size, "reaped": reaped}

    def usage(self, project: str = "") -> dict:
        """Report per-project usage plus the shared day total.

        Schema: ``project=\"\"`` reports the shared aggregate as ``used``
        (legacy shape); a named project reports its own count as ``used``
        with ``limit`` as the per-project cap. Both shapes always carry
        ``shared_used``/``shared_limit``. Bounds: reads today's day-file only.
        Trust: local operator state. Failure: never raises for I/O; corrupt
        shapes read as empty.
        """
        project = self._check_project(project)
        day = self._today()
        path = self.root / f"{day}.json"
        record = read_json(path, {"requests": []})
        if not isinstance(record, dict):
            record = {"requests": []}
        requests, projects = self._split(record)
        shared = len(requests)
        if project:
            used, limit = len(projects.get(project, [])), self.daily
        else:
            used, limit = shared, self.daily
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        reaped = self.gc()
        return {"day": day, "zone": self.timezone, "used": used, "limit": limit,
                "shared_used": shared, "shared_limit": self.shared_daily,
                "project": project, "bytes": size, "reaped": reaped}
