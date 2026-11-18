"""Host-only, read-only readiness scan for legacy work-item conversion."""
from __future__ import annotations

import contextlib
import os
import stat

from .errors import Busy, ConfigError, Denied, LimitExceeded, MizuError
from .fs import lock
from .work_items import ITEM_ID, _TEMP_NAME, _directory, _present, _resolution_name

DEFAULT_SCAN_LIMIT = 10_000
_SOURCE_ERRORS = (MizuError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


class _ScanLimit(LimitExceeded):
    pass


@contextlib.contextmanager
def _record_snapshot(project):
    """Freeze the decision and work-item files without creating a lock inode."""
    root = project.root
    path = root / "locks" / "insights.lock"
    with contextlib.ExitStack() as stack:
        locked = False
        if _directory(root) and _directory(path.parent):
            try:
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise Denied("Unsafe work-item record lock")
                stack.enter_context(lock(path, blocking=False, create=False))
                locked = True
            except FileNotFoundError:
                pass
        if (_present(root / "control.json")
                and project.work_items._read("control.json").get("paused") is not True):
            raise Denied("Pause the project before previewing work-item migration")
        yield
        # A restored project may lack locks. The first cooperating writer
        # must create this inode before changing records and never unlinks it.
        if not locked and _present(path):
            raise Busy("Work-item records may have changed; retry the migration preview")


def _candidate(event):
    return (event["action"] in ("accept", "modify")
            and not {"work_item", "work_items", "work_item_specs"}.intersection(event))


def _scan(project, scan_limit, report):
    store = project.work_items
    history = project.root / "decision-history"
    items = project.root / "work-items"
    candidates = set()
    by_origin = {}

    @contextlib.contextmanager
    def scan(directory):
        def bounded(entries):
            for count, entry in enumerate(entries):
                if count >= scan_limit:
                    raise _ScanLimit("Work-item migration scan exceeds entry bound")
                yield entry
        with os.scandir(directory) as entries:
            yield bounded(entries)

    if store._prepare_decision_history(history, recover=False, scan=scan):
        with scan(history) as entries:
            for entry in entries:
                if _TEMP_NAME.fullmatch(entry.name):
                    continue
                try:
                    event_id, event = store._read_decision_event(entry.name)
                    if not _candidate(event):
                        continue
                    item_id = f"wi_{event_id}"
                    candidates.add(item_id)
                    origin = store._legacy_source(event)["insight_id"]
                    if origin is not None:
                        by_origin.setdefault(origin, []).append(entry.name)
                    revisions = store._history_revisions(item_id, recover=False)
                    if revisions is not None:
                        store._direct_record(item_id)  # Refuse a colliding creation record.
                    resolution = store._legacy_resolution(event_id, event, history_revisions=revisions)
                    if resolution is None:
                        result = "explicit_no_work" if event.get("work_item_intent") == "none" else "unresolved"
                    elif resolution["outcome"] == "open":
                        store._get(item_id, seed_event=event, history_revisions=revisions,
                                   verify_snapshots=False, verify_current_delivery=True)
                        result = "resolved_open"
                    elif store.is_drop_only(item_id):
                        result = "resolved_drop"
                    else:
                        raise Denied("Legacy drop is not the only work-item record")
                    report[result] += 1
                except _SOURCE_ERRORS:
                    report["ambiguous_or_corrupt"] += 1

    if _directory(items):
        with scan(items) as entries:
            for entry in entries:
                if entry.name in candidates:
                    continue
                try:
                    if ITEM_ID.fullmatch(entry.name) is None or not _directory(items / entry.name):
                        raise Denied("Invalid work-item directory entry")
                    if _resolution_name(items / entry.name) is not None:
                        raise Denied("Work-item resolution has no eligible immutable decision source")
                except _SOURCE_ERRORS:
                    report["ambiguous_or_corrupt"] += 1

    # Current legacy decisions omit the event ID. Reuse the same exact-match
    # linkage check as runtime; each source is reread at most once here.
    for origin, names in by_origin.items():
        try:
            current = store._optional_record(f"decisions/{origin}.json")
            if current is not None and _candidate(current):
                store._validate_current_decision_shape(current, origin)
                store._resolve_current_work_item(
                    current, readonly=True, resolve=False,
                    events=lambda: (store._read_decision_event(name) for name in names))
        except _SOURCE_ERRORS:
            report["ambiguous_or_corrupt"] += 1


def plan(project, *, scan_limit=DEFAULT_SCAN_LIMIT) -> dict:
    """Return only counts from bounded, authoritative file readers, not the index."""
    if type(scan_limit) is not int or scan_limit < 1:
        raise ConfigError("Work-item migration scan limit must be a positive integer")
    report = dict(project=project.name, dry_run=True, scan_limit=scan_limit,
                  resolved_open=0, resolved_drop=0, explicit_no_work=0, unresolved=0,
                  ambiguous_or_corrupt=0, truncated=False, scan_complete=True)
    with _record_snapshot(project):
        try:
            _scan(project, scan_limit, report)
        except _ScanLimit:
            report.update(truncated=True, scan_complete=False)
        except _SOURCE_ERRORS:
            report.update(ambiguous_or_corrupt=report["ambiguous_or_corrupt"] + 1, scan_complete=False)
    report["ok"] = not (report["truncated"] or report["ambiguous_or_corrupt"] or report["unresolved"])
    if not report["ok"]:
        report["action"] = ("Before upgrading, resolve unresolved events with the existing writer "
                            "tool mizu_work_items_reconcile. Restore or repair ambiguous or corrupt "
                            "sources and retry on a paused project; raise --scan-limit if truncated.")
    return report
