"""Local cross-process FIFO admission, separate from active execution locks.

The queue orders the admissions; a role's exclusion is a different thing, and
its path is defined here because the admission is what takes it. A live
invocation holds its role's exclusion for the whole run, so the kernel
releases it exactly when the invocation ends.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from datetime import datetime

from .errors import Busy, Denied
from .fs import ID, canonical, lock, now, write_json


def role_exclusion(project: Path, role: str) -> Path:
    """Return the lock a live invocation of ``role`` holds for its whole run.

    Every caller that must exclude a live role takes this path, so the
    exclusion the invocation holds and the exclusion another caller avoids
    cannot drift apart.

    Bounds: one project root, one role name. Trust: local state. Failure:
    never raises.
    """
    return project / "locks" / f"run-{role}.lock"


def check_slot_exclusion(project: Path, slot: int) -> Path:
    """Return the lock the check runner holds while it occupies ``slot``.

    A check job's container belongs to the runner, not to a role, so the
    runner holds the exclusion of the compute slot it occupies. A sweep
    finds that exclusion held and refuses, and a runner cannot start a job
    inside a sweep's own window. The set is bounded by the number of
    compute slots, so it never grows with the number of jobs a project has
    run.

    Bounds: one project root, one slot index. Trust: local state. Failure:
    never raises.
    """
    return role_exclusion(project, f'check-slot-{slot}')


def check_slot_exclusions(project: Path, slots: int) -> list[Path]:
    """Return every compute slot exclusion a project-wide sweep must hold.

    The named slots are always included, whether or not a runner has
    occupied them yet, so a runner that starts inside a sweep's window
    cannot start at all.

    Bounds: one project root, one bounded slot count. Trust: local state.
    Failure: never raises.
    """
    return [check_slot_exclusion(project, slot) for slot in range(max(0, slots))]


def role_exclusions(project: Path, roles: Sequence[str] = ()) -> list[Path]:
    """Return every role exclusion the project must exclude.

    The named roles are always included, whether or not they have ever run,
    and so is every exclusion that already exists in the project: the
    existence of a lock file is not a liveness fact, because the file
    appears when a role takes its exclusion, and a delegated child's role
    need not be enabled for the project. The set must name both, or a role
    that has not run yet would be unprotected.

    Bounds: one project root, one bounded directory listing, one path per
    role. Trust: local state. Failure: an unreadable locks directory is
    refused, because an empty set would exclude nothing.
    """
    directory = project / "locks"
    found = {role_exclusion(project, role) for role in roles}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith("run-") and name.endswith(".lock") and ID.fullmatch(name[4:-5]):
                    found.add(directory / name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise Denied("Role exclusions are unreadable") from exc
    return sorted(found)


class _Invalid(Busy):
    """Invalid coordination state cannot be resolved by waiting for capacity."""


class _Full(Busy):
    """Queue overflow is a deferral, not another wait for a pending place."""


class _Queue:
    """A bounded list under queue.lock; fixed pending locks prove liveness.

    State is local coordination data: version 1, tickets in arrival order, each
    with a hex id, pending-lock index and zero to two project-relative lock paths.
    New tickets record enqueued_at as bounded, aware ISO-8601 display metadata;
    it never affects ordering, cancellation or kernel-lock liveness. Older
    tickets without that metadata retain their unknown start time.
    No Context, provider state, PID, clock lease or cache prediction is stored.
    Kernel locks make dead owners reclaimable without PID-reuse/TTL guesses.
    All dispatch locks are nonblocking, including the short queue transaction.
    """

    def __init__(self, config, resources):
        self.root = config.data / "slots"
        self.capacity = config.limits.parallel_runs
        self.projects = config.data / "projects"
        try:
            self.resources = tuple(Path(p).relative_to(self.projects).as_posix() for p in resources)
        except (TypeError, ValueError):
            raise Busy("Execution admission resources are invalid") from None
        if len(self.resources) > 2 or any(not self._valid_resource(p) for p in self.resources):
            raise Busy("Execution admission resources are invalid")
        # Bound storage and parsing by the largest canonical ticket schema.
        # The per-ticket newline covers the comma separating list entries.
        longest = f"{'a' * 63}/locks/run-{'a' * 63}.lock"
        self.state_bytes = len(canonical({"version": 1, "tickets": []})) + self.capacity * len(canonical(
            {"id": "f" * 32, "owner": self.capacity - 1, "resources": [longest, longest],
             "enqueued_at": "2099-01-01T00:00:00.000000+00:00"}))
        self.identity = uuid.uuid4().hex
        self.alive = None

    def _pending(self, owner):
        return lock(self.root / f"pending-{owner}.lock", blocking=False)

    @staticmethod
    def _valid_resource(resource):
        if not isinstance(resource, str):
            return False
        parts = resource.split("/")
        if len(parts) != 3 or not ID.fullmatch(parts[0]) or parts[1] != "locks":
            return False
        name = parts[2]
        return name == "workspace.lock" or (name.startswith("run-") and name.endswith(".lock")
                                            and ID.fullmatch(name[4:-5]) is not None)

    @staticmethod
    def _valid_enqueued_at(value):
        if not isinstance(value, str) or len(value) > 32:
            return False
        try:
            return datetime.fromisoformat(value).tzinfo is not None
        except ValueError:
            return False

    def _read(self):
        try:
            path = self.root / "queue.json"
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            try:
                # Without O_NOFOLLOW (notably Windows), reject known links and
                # special files first. This fallback cannot prevent replacement
                # between lstat and open; the local data directory must be trusted.
                if not nofollow and not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("Non-regular queue state")
                fd = os.open(path, flags)
            except FileNotFoundError:
                return []
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Non-regular queue state")
                if info.st_size > self.state_bytes:
                    raise ValueError("Oversized queue state")
                # The configured bound can be enormous even for a tiny file.
                # Allocate only for bytes received, with fixed-size requests.
                raw = bytearray()
                while chunk := os.read(fd, min(64 * 1024, self.state_bytes + 1 - len(raw))):
                    raw.extend(chunk)
                    if len(raw) > self.state_bytes:  # Also catch growth after fstat.
                        raise ValueError("Oversized queue state")
            finally:
                os.close(fd)
            state = json.loads(raw)
        except (OSError, ValueError, RecursionError):
            raise _Invalid("Execution admission queue is unreadable") from None
        if (not isinstance(state, dict) or set(state) != {"version", "tickets"}
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["tickets"], list) or len(state["tickets"]) > self.capacity):
            raise _Invalid("Execution admission queue is invalid")
        tickets = state["tickets"]
        owners, identities = set(), set()
        for ticket in tickets:
            if (not isinstance(ticket, dict) or set(ticket) not in ({"id", "owner", "resources"},
                                                                   {"id", "owner", "resources", "enqueued_at"})
                    or not isinstance(ticket["id"], str) or len(ticket["id"]) != 32
                    or any(c not in "0123456789abcdef" for c in ticket["id"])
                    or type(ticket["owner"]) is not int or not 0 <= ticket["owner"] < self.capacity
                    or not isinstance(ticket["resources"], list) or len(ticket["resources"]) > 2
                    or any(not self._valid_resource(p) for p in ticket["resources"])
                    or ticket["owner"] in owners or ticket["id"] in identities
                    or ("enqueued_at" in ticket and not self._valid_enqueued_at(ticket["enqueued_at"]))):
                raise _Invalid("Execution admission queue is invalid")
            owners.add(ticket["owner"])
            identities.add(ticket["id"])
        live = []
        for ticket in tickets:
            if ticket["id"] == self.identity and self.alive is not None:
                live.append(ticket)
                continue
            try:
                with self._pending(ticket["owner"]):
                    pass
            except Busy:
                live.append(ticket)
        if live != tickets:
            self._write(live)
        return live

    def _write(self, tickets):
        write_json(self.root / "queue.json", {"version": 1, "tickets": tickets})

    def _enqueue(self, tickets):
        if len(tickets) >= self.capacity:
            raise _Full("Execution admission queue is full")
        occupied = {ticket["owner"] for ticket in tickets}
        for owner in range(self.capacity):
            if owner in occupied:
                continue
            manager = self._pending(owner)
            try:
                manager.__enter__()
            except Busy:
                continue
            self.alive = manager
            tickets.append({"id": self.identity, "owner": owner,
                            "resources": list(self.resources), "enqueued_at": now()})
            self._write(tickets)
            return
        raise _Full("Execution admission queue is full")

    def _resources(self, stack, resources):
        for path in resources:
            stack.enter_context(lock(self.projects / path, blocking=False))

    def _slots(self, stack):
        for index in range(self.capacity):
            manager = lock(self.root / f"{index}.lock", blocking=False)
            try:
                stack.enter_context(manager)
                return
            except Busy:
                continue
        raise Busy("All configured execution slots are in use")

    def attempt(self, *, enqueue):
        """Return a held ExitStack, or Busy with no dispatch locks held.

        All callers may skip older resource-blocked tickets, retaining their
        original place. They never skip a ready ticket. Fail-fast callers do
        not enqueue. A pending place is needed only if dispatch cannot proceed.
        """
        with lock(self.root / "queue.lock", blocking=False):
            tickets = self._read()
            attempt = contextlib.ExitStack()
            try:
                for ticket in tickets:
                    if ticket["id"] == self.identity:
                        break
                    try:
                        with contextlib.ExitStack() as probe:
                            self._resources(probe, ticket["resources"])
                    except Busy:
                        continue
                    raise Busy("Older execution admission is ready")
                self._resources(attempt, self.resources)
                self._slots(attempt)
                if self.alive is not None:
                    self._write([t for t in tickets if t["id"] != self.identity])
                    # Free the pending place in the same transaction as its
                    # row: no peer may observe a free row with a held lock.
                    self._release_pending()
                return attempt
            except Busy:
                attempt.close()
                if enqueue and self.alive is None:
                    self._enqueue(tickets)
                raise
            except BaseException:
                attempt.close()
                raise

    def _release_pending(self):
        if self.alive is not None:
            self.alive.__exit__(None, None, None)
            self.alive = None

    def close(self):
        # Release first: even if another transaction is in progress, this
        # ticket is now inactive and the next reader reaps it before ordering
        # or capacity checks. Never wait for a peer to finish cleanup.
        if self.alive is not None:
            self._release_pending()
            try:
                with lock(self.root / "queue.lock", blocking=False):
                    self._read()
            except Busy:
                pass


@contextlib.contextmanager
def admission(config, resources=(), *, wait=False, stop=None, check=None):
    """Admit one work unit; lend nothing and never preempt an active driver.

    Pending capacity is derived from parallel_runs (one waiter per slot).
    Overflow and invalid state are immediate Busy. The operator selects the
    admission_wait_seconds deadline (0 means none); poll at
    idle_seconds with no role/workspace/slot locks; stop wakes the wait. check
    revalidates normal authority/budget before each attempt and at dispatch,
    outside the queue transaction. Only Busy contention retries. Success
    removes the ticket and releases its pending lock in one transaction;
    cancellation and other errors invalidate it in finally.
    Dead/stale entries are reaped by the next transaction. Active slot lock
    files remain the independent hard cap. Queue/slot files live under data/slots;
    lock inodes are never unlinked. Invalid state fails closed with Busy.
    """
    stop = stop if stop is not None else threading.Event()
    admission_started = time.monotonic()
    window = config.limits.admission_wait_seconds
    deadline = admission_started + window if wait and window else None
    queue = _Queue(config, resources)
    try:
        while True:
            if stop.is_set():
                raise Busy("Run stopped before execution admission")
            if check is not None:
                check()
            if deadline is not None and time.monotonic() >= deadline:
                raise Busy("Execution admission wait expired")
            try:
                attempt = queue.attempt(enqueue=wait)
            except (_Full, _Invalid) as exc:
                raise Busy(str(exc)) from None
            except Busy:
                if not wait:
                    raise
                remaining = (max(0.0, deadline - time.monotonic())
                             if deadline is not None else config.limits.idle_seconds)
                stop.wait(min(config.limits.idle_seconds, remaining))
                continue
            # The successful transaction already retired the pending ticket.
            with attempt:
                if stop.is_set():
                    raise Busy("Run stopped before execution admission")
                if check is not None:
                    check()
                if deadline is not None and time.monotonic() >= deadline:
                    raise Busy("Execution admission wait expired")
                yield max(0.0, time.monotonic() - admission_started)
            return
    finally:
        queue.close()
