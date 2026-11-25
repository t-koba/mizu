"""Portable change detection for the resident daemon's idle wait.

A settled daemon must not re-read configuration, rebuild its project, or run
full admission on a fixed idle tick.  It takes a cheap, metadata-only
fingerprint of the authoritative state paths (the project root plus the
authoritative deployment config file) and blocks
until that fingerprint changes, an explicit deadline passes, or the daemon is
stopped.  A changed fingerprint is only a reason to look: admission still reads
durable state and decides whether a role may run.

Detection relies on writer-published top-level hints plus exact deadlines,
never traversal of retained state: writers bump a fixed watched directory when
a nested revision is committed, and delayed appends carry a ``deliver_at``
deadline supplied by the caller.  The fingerprint stats a fixed set of paths,
so its cost is independent of how many retained files or entity
directories exist. The project root itself is deliberately not watched: its
mtime churns with ledger journal files and would wake the daemon on every pass.  Nothing here reads file contents or
enumerates directories.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Wake latency of the portable detector.  This is the wait boundary's
# granularity, not a scheduling policy: a native notification backend can lower
# it to zero without changing the daemon's admission or deadline logic.
POLL_SECONDS = 1.0

# Every top-level input that admission can read, plus the directories where
# writers publish nested hints. A negative admission result is memoized until
# this fingerprint changes or an actionable deadline lapses, so an unwatched
# readiness input would keep a settled daemon asleep indefinitely. These are
# fixed paths only: no traversal of retained state.
WATCHED_FILES = (
    "control.json", "current.json", "PROJECT.md", "goal.md", "project.toml",
    "bridge.json", "ci-wait-absence-generation.json",
)

WATCHED_DIRS = (
    "wake", "queues", "histories", "snapshots", "check-jobs", "waits",
    "work-items", "work-item-receipts", "work-order", "spool",
    "ci-wait-observations", "ci-wait-absence", "active", "health",
    "role-state", "routing", "selection", "observed", "shared-records",
)


def fingerprint(root: Path | None, config_file: Path | None = None,
                *, config_files=()) -> tuple:
    """Return a metadata-only fingerprint of the authoritative paths.

    Missing paths contribute a stable sentinel.  The fingerprint changes when a
    watched file is created, replaced, resized or rewritten, or when a watched
    directory gains, loses or replaces a direct child. ``config_file`` is the
    root deployment config path and ``config_files`` is its bounded authoritative
    input set (the root plus every transitive ``include`` actually loaded); each
    is statted once, in that order, before the project-root rows. A config edit
    therefore wakes a settled daemon even when there is no project or deadline.
    """
    rows = []
    authoritative = []
    if config_file is not None:
        authoritative.append(config_file)
    for path in config_files:
        if path not in authoritative:
            authoritative.append(path)
    for path in authoritative:
        try:
            st = path.stat()
            rows.append(("config", str(path),
                         (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)))
        except (OSError, TypeError, ValueError):
            rows.append(("config", str(path), None))
    if root is None:
        return tuple(rows)
    for name in WATCHED_FILES:
        try:
            st = (root / name).stat()
            rows.append((name, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)))
        except (OSError, TypeError, ValueError):
            rows.append((name, None))
    for name in WATCHED_DIRS:
        try:
            st = (root / name).stat()
            rows.append((name, (st.st_dev, st.st_ino, st.st_mtime_ns)))
        except (OSError, TypeError, ValueError):
            rows.append((name, None))
    return tuple(rows)


def wait(stop, root: Path | None, baseline: tuple, deadline: float | None,
         poll: float | None = None, config_file: Path | None = None,
         *, config_files=()) -> str:
    """Block until state changes, ``deadline`` passes, or ``stop`` is set.

    ``deadline`` is a monotonic instant.  Returns ``"change"``, ``"deadline"``
    or ``"stop"``.  A change already present when this is called returns
    immediately, so a change that happens during a state read cannot be lost.
    """
    if poll is None:
        # Production callers leave poll unset: use the platform's native change
        # notification so a settled daemon does no periodic work. An explicit
        # poll keeps the portable loop below (and every existing assertion).
        from . import daemon_notify
        if daemon_notify.available():
            try:
                return _wait_native(stop, root, baseline, deadline,
                                    config_file, config_files)
            except OSError as exc:
                # A backend that cannot be armed at all is reported as a fact;
                # only then is the portable loop used, never as a silent default.
                print(json.dumps({"event": "native_wake_unavailable",
                                  "error": str(exc)}), flush=True)
    else:
        return _wait_portable(stop, root, baseline, deadline, poll,
                              config_file, config_files)
    return _wait_portable(stop, root, baseline, deadline, POLL_SECONDS,
                          config_file, config_files)


def _wait_portable(stop, root, baseline, deadline, poll, config_file, config_files) -> str:
    while not stop.is_set():
        if fingerprint(root, config_file, config_files=config_files) != baseline:
            return "change"
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return "deadline"
        sleep_for = poll
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, deadline - now))
        if sleep_for <= 0:
            continue
        stop.wait(sleep_for)
    return "stop"


def _wait_native(stop, root, baseline, deadline, config_file, config_files) -> str:
    from . import daemon_notify
    with daemon_notify.Watcher(root, config_file, config_files, stop) as watcher:
        if watcher.unwatchable:
            print(json.dumps({"event": "native_wake_unwatchable",
                              "paths": [p.name for p in watcher.unwatchable]}),
                  flush=True)
        while not stop.is_set():
            if fingerprint(root, config_file, config_files=config_files) != baseline:
                return "change"
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return "deadline"
            watcher.wait(deadline)
    return "stop"
