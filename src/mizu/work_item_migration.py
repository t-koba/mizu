"""Host-only readiness scan and filename conversion for legacy work items."""
from __future__ import annotations

import contextlib
import os
import stat

from .errors import Busy, ConfigError, Denied, LimitExceeded, MizuError
from .fs import PREVIEW_BYTES, lock, rename_exclusive, safe_read, sync_dir
from .storage import quiescent
from .work_items import (ITEM_ID, _CURRENT_RESOLUTION_NAME, _LEGACY_RESOLUTION_NAME,
                         _TEMP_NAME, _directory, _present, _resolution_name)

DEFAULT_SCAN_LIMIT = 10_000
_SOURCE_ERRORS = (MizuError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


class _ScanLimit(LimitExceeded):
    pass


def _require_paused(project, operation):
    if (_present(project.root / "control.json")
            and project.work_items._read("control.json").get("paused") is not True):
        raise Denied(f"Pause the project before {operation} work-item migration")


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
        _require_paused(project, "previewing")
        yield
        # A restored project may lack locks. The first cooperating writer
        # must create this inode before changing records and never unlinks it.
        if not locked and _present(path):
            raise Busy("Work-item records may have changed; retry the migration preview")


def _candidate(event):
    return (event["action"] in ("accept", "modify")
            and not {"work_item", "work_items", "work_item_specs"}.intersection(event))


@contextlib.contextmanager
def _scan_entries(directory, scan_limit):
    def bounded(entries):
        for count, entry in enumerate(entries):
            if count >= scan_limit:
                raise _ScanLimit("Work-item migration scan exceeds entry bound")
            yield entry
    with os.scandir(directory) as entries:
        yield bounded(entries)


def legacy_resolutions_present(project, *, scan_limit=DEFAULT_SCAN_LIMIT) -> bool:
    """Prove presence or absence by name in one complete, bounded directory scan."""
    items = project.root / "work-items"
    if not _directory(items):
        return False
    found = False
    with _scan_entries(items, scan_limit) as entries:
        for entry in entries:
            item = items / entry.name
            if ITEM_ID.fullmatch(entry.name) is None or not _directory(item):
                raise Denied("Invalid work-item directory entry")
            if _present(item / _LEGACY_RESOLUTION_NAME):
                found = True
    return found


def _scan(project, scan_limit, report, moves):
    store = project.work_items
    history = project.root / "decision-history"
    items = project.root / "work-items"
    candidates = set()
    by_origin = {}

    def scan(directory):
        return _scan_entries(directory, scan_limit)

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
                    if moves is not None and resolution is not None:
                        name = _resolution_name(items / item_id)
                        if name == _LEGACY_RESOLUTION_NAME:
                            moves.append(item_id)
                        elif name == _CURRENT_RESOLUTION_NAME:
                            report["already_current"] += 1
                        else:
                            raise Denied("Missing validated work-item resolution")
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


def _plan(project, scan_limit, *, moves=None, locked=False) -> dict:
    if type(scan_limit) is not int or scan_limit < 1:
        raise ConfigError("Work-item migration scan limit must be a positive integer")
    report = dict(project=project.name, dry_run=True, scan_limit=scan_limit,
                  resolved_open=0, resolved_drop=0, explicit_no_work=0, unresolved=0,
                  ambiguous_or_corrupt=0, truncated=False, scan_complete=True)
    if moves is not None:
        report.update(dry_run=False, already_current=0, renamed=0, converted=0, errors=[])
    with contextlib.nullcontext() if locked else _record_snapshot(project):
        try:
            _scan(project, scan_limit, report, moves)
        except _ScanLimit:
            report.update(truncated=True, scan_complete=False)
        except _SOURCE_ERRORS:
            report.update(ambiguous_or_corrupt=report["ambiguous_or_corrupt"] + 1, scan_complete=False)
    if moves is not None:
        report["candidates"] = len(moves)
    report["ok"] = report["scan_complete"] and not (report["ambiguous_or_corrupt"] or report["unresolved"])
    if not report["ok"]:
        report["action"] = ("List events with mizu work-items <project> --legacy and resolve unresolved "
                            "events with mizu work-items reconcile-legacy. Restore or repair ambiguous or corrupt "
                            "sources and retry on a paused project; raise --scan-limit if truncated.")
    return report


def plan(project, *, scan_limit=DEFAULT_SCAN_LIMIT) -> dict:
    """Return only counts from bounded, authoritative file readers, not the index."""
    return _plan(project, scan_limit)


def apply(project, *, scan_limit=DEFAULT_SCAN_LIMIT) -> dict:
    """Convert only after a fresh, complete scan under storage exclusion."""
    moves = []
    # Refuse an unready project before taking maintenance locks, which can
    # initialize missing lock inodes. This scan never authorizes a rename.
    report = _plan(project, scan_limit, moves=moves)
    if not (report["ok"] and report["scan_complete"]):
        return report

    with quiescent(project, snapshot_gc=True):  # Includes insights.lock.
        _require_paused(project, "applying")
        moves.clear()
        report = _plan(project, scan_limit, moves=moves, locked=True)
        if not (report["ok"] and report["scan_complete"]):
            return report
        for item_id in moves:
            relative = f"work-items/{item_id}"
            old = f"{relative}/{_LEGACY_RESOLUTION_NAME}"
            new = f"{relative}/{_CURRENT_RESOLUTION_NAME}"
            stage = "read_source"
            try:
                raw = safe_read(project.root, old, PREVIEW_BYTES)
                stage = "rename"
                rename_exclusive(project.root / old, project.root / new)
                report["renamed"] += 1
                stage = "verify"
                if safe_read(project.root, new, PREVIEW_BYTES) != raw or _present(project.root / old):
                    raise Denied("Work-item resolution rename failed read-back verification")
                stage = "sync_directory"
                sync_dir(project.root / relative)
            except _SOURCE_ERRORS as exc:
                error = dict(stage=stage, type=type(exc).__name__)
                if isinstance(exc, OSError) and exc.errno is not None:
                    error["errno"] = exc.errno
                report["errors"].append(error)
                report.update(ok=False, action="Check the failure and rerun on a paused project.")
                break
            report["converted"] += 1
    return report
