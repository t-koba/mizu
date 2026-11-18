"""Own the decision/attention delivery idle-cache state."""
from __future__ import annotations

import os
import stat

from . import decision_delivery, platform as _platform
from .fs import digest


#: Process-local idle fast path, used on local POSIX where ctime is a
#: metadata-change witness; Windows always runs the exact scan. An empty
#: decision/attention page is reused only while every source the exact scan
#: reads is unchanged: the role's inbox, decision-history, decisions and
#: decision receipts; the authority/report marker; the shared
#: `shared-records/insight` record partition; and the hashed shared receipt
#: store for the role. File entry metadata is included. A non-empty result is
#: never cached as empty. This is a delivery
#: precondition, not a delivery authority: a real unit may bind the empty page
#: and manifest only while the same complete fingerprint is unchanged.
_DECISION_IDLE_CACHE: dict = {}
_DECISION_IDLE_CACHE_LIMIT = 256


def _empty_decision_state(shared: bool) -> dict:
    page = ({"events": [], "pending_count": 0, "truncated": False,
             "count_truncated": False, "byte_truncated": False}
            if shared else None)
    return {"shared": shared, "events": [], "diagnostics": [], "page": page,
            "manifest": ()}


def _decision_source_fingerprint(project, role_name):
    """Cheap, path-free metadata token for every decision/attention source.

    Each sampled directory includes the metadata of its entries, not only the
    directory identity, so a local POSIX in-place rewrite is observable through
    the file ctime. On Windows ctime is creation time, so callers disable the
    empty-page reuse instead of treating this token as a change witness.
    """
    root = project.insights.root
    parts = []

    def directory(name):
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return (name, None, ())
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return None
        entries = []
        try:
            with os.scandir(path) as scanned:
                for entry in scanned:
                    try:
                        entry_info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        return None
                    except OSError:
                        return None
                    entries.append((entry.name, entry_info.st_dev,
                                    entry_info.st_ino, entry_info.st_mode,
                                    entry_info.st_nlink,
                                    entry_info.st_mtime_ns,
                                    entry_info.st_ctime_ns,
                                    entry_info.st_size))
        except FileNotFoundError:
            return (name, None, ())
        except OSError:
            return None
        entries.sort()
        return (name, (info.st_dev, info.st_ino, info.st_mode,
                       info.st_nlink, info.st_mtime_ns, info.st_ctime_ns,
                       info.st_size), tuple(entries))

    names = ["inbox", "decision-history", "decisions", "decision-receipts",
             f"decision-receipts/{role_name}", "shared-records",
             "shared-records/insight", "shared-record-receipts",
             f"shared-record-receipts/{digest(role_name.encode())}"]
    for name in names:
        part = directory(name)
        if part is None:
            return None
        parts.append(part)
    for name in (decision_delivery.AUTHORITY_FILE, decision_delivery.REPORT_FILE):
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            parts.append((name, None))
            continue
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        parts.append((name, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
    return tuple(parts)


def _decision_policy_signature(project, role):
    return (role.name,
            tuple(getattr(role, "decision_events", ()) or ()),
            getattr(role, "decision_sources", "own") or "own",
            decision_delivery.role_decision_events_limit(project.config, role),
            getattr(role, "decision_page_bytes", 0),
            tuple(sorted(set(project.roles) & project.config.roles.keys())))


def _decision_delivery_state(project, role_name, role):
    """Return the exact pending decision/attention state, with an idle fast path.

    The fingerprint is sampled before and after a scan; an empty page is cached
    only when neither sample changed. A scan that observes a change, or returns
    any event/diagnostic, is never reused. Errors reading the source metadata
    simply disable the fast path for that evaluation.
    """
    if _platform.IS_WINDOWS:
        return decision_delivery.pending(project, role)
    key = (project.insights.root, role_name, _decision_policy_signature(project, role))
    before = _decision_source_fingerprint(project, role_name)
    cached = _DECISION_IDLE_CACHE.get(key)
    if before is not None and cached is not None and cached[0] == before:
        return _empty_decision_state(cached[1])
    shared = decision_delivery.authority(project.insights.root) is not None
    state = decision_delivery.pending(project, role)
    after = _decision_source_fingerprint(project, role_name)
    if (not state["events"] and not state["diagnostics"]
            and before is not None and before == after):
        if len(_DECISION_IDLE_CACHE) >= _DECISION_IDLE_CACHE_LIMIT:
            _DECISION_IDLE_CACHE.clear()
        _DECISION_IDLE_CACHE[key] = (before, shared)
    else:
        _DECISION_IDLE_CACHE.pop(key, None)
    return state
