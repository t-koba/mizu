"""Shared, crash-conservative request admission across all roles and projects."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .errors import LimitExceeded
from .fs import atomic_write, canonical, lock, read_json

#: Day-file byte bound (~160K admissions at ~25 B/entry); count cap binds first.
BUDGET_DAY_BYTES = 4 * 1024 * 1024


class Budget:
    def __init__(self, root: Path, daily: int):
        self.root, self.daily = root, daily

    def gc(self, *, keep_days: int = 31) -> int:
        """Remove day-files older than the window. Best-effort; never fails admission."""
        removed = 0
        try:
            today = dt.datetime.now(dt.timezone.utc).date()
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

    def take(self, request_id: str, *, day: str | None = None) -> int:
        day = day or dt.datetime.now(dt.timezone.utc).date().isoformat()
        dt.date.fromisoformat(day)
        if self.daily <= 0:
            raise LimitExceeded("Model calls are disabled: set limits.daily_requests explicitly")
        with lock(self.root / "budget.lock"):
            path = self.root / f"{day}.json"
            record = read_json(path, {"day": day, "requests": []})
            if request_id in record["requests"]:
                return len(record["requests"])
            if len(record["requests"]) >= self.daily:
                raise LimitExceeded("UTC daily model-request budget exhausted")
            record["requests"].append(request_id)
            body = canonical(record)
            if len(body) > BUDGET_DAY_BYTES:
                raise LimitExceeded("Budget day-file exceeds byte bound; inspect data/budget")
            atomic_write(path, body)
            count = len(record["requests"])
        self.gc()
        return count

    def usage(self) -> dict:
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        path = self.root / f"{day}.json"
        record = read_json(path, {"requests": []})
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        self.gc()
        return {"day": day, "used": len(record["requests"]), "limit": self.daily, "bytes": size}
