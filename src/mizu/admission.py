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


def _valid_resource(resource):
    """Whether a resource path names a project-relative lock the queue holds."""
    if not isinstance(resource, str):
        return False
    parts = resource.split("/")
    if len(parts) != 3 or not ID.fullmatch(parts[0]) or parts[1] != "locks":
        return False
    name = parts[2]
    return name == "workspace.lock" or (name.startswith("run-") and name.endswith(".lock")
                                        and ID.fullmatch(name[4:-5]) is not None)


def _valid_enqueued_at(value):
    """Whether a value is a bounded, aware enqueue instant."""
    if not isinstance(value, str) or len(value) > 32:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _read_state(root: Path, capacity: int, state_bytes: int) -> list:
    """Read the queue file's tickets, or fail closed.

    Schema: ``{"version": 1, "tickets": [...]}``; a ticket names its
    identity, its pending-slot owner and the resources it waits for, and
    may carry the aware instant it was enqueued at. Bounds: the file is at
    most ``state_bytes``, the ticket list is at most ``capacity`` long, and
    every field is bounded. Trust: local coordination state. Failure:
    ``_Invalid`` for an unreadable, oversized or malformed file, so a
    caller never orders admissions from state nobody can read.
    """
    try:
        path = root / "queue.json"
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
            if info.st_size > state_bytes:
                raise ValueError("Oversized queue state")
            # The configured bound can be enormous even for a tiny file.
            # Allocate only for bytes received, with fixed-size requests.
            raw = bytearray()
            while chunk := os.read(fd, min(64 * 1024, state_bytes + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > state_bytes:  # Also catch growth after fstat.
                    raise ValueError("Oversized queue state")
        finally:
            os.close(fd)
        state = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        raise _Invalid("Execution admission queue is unreadable") from None
    if (not isinstance(state, dict) or set(state) != {"version", "tickets"}
            or type(state["version"]) is not int or state["version"] != 1
            or not isinstance(state["tickets"], list) or len(state["tickets"]) > capacity):
        raise _Invalid("Execution admission queue is invalid")
    tickets = state["tickets"]
    owners, identities = set(), set()
    for ticket in tickets:
        if (not isinstance(ticket, dict) or set(ticket) not in ({"id", "owner", "resources"},
                                                               {"id", "owner", "resources", "enqueued_at"})
                or not isinstance(ticket["id"], str) or len(ticket["id"]) != 32
                or any(c not in "0123456789abcdef" for c in ticket["id"])
                or type(ticket["owner"]) is not int or not 0 <= ticket["owner"] < capacity
                or not isinstance(ticket["resources"], list) or len(ticket["resources"]) > 2
                or any(not _valid_resource(p) for p in ticket["resources"])
                or ticket["owner"] in owners or ticket["id"] in identities
                or ("enqueued_at" in ticket and not _valid_enqueued_at(ticket["enqueued_at"]))):
            raise _Invalid("Execution admission queue is invalid")
        owners.add(ticket["owner"])
        identities.add(ticket["id"])
    return tickets


_PENDING_ADMISSIONS: dict[tuple[str, ...], tuple[str, object]] = {}


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
        if len(self.resources) > 2 or any(not _valid_resource(p) for p in self.resources):
            raise Busy("Execution admission resources are invalid")
        # Bound storage and parsing by the largest canonical ticket schema.
        self.state_bytes = _state_bytes(self.capacity)
        self.identity = uuid.uuid4().hex
        self.alive = None

    def _pending(self, owner):
        return lock(self.root / f"pending-{owner}.lock", blocking=False)

    def _read(self):
        tickets = _read_state(self.root, self.capacity, self.state_bytes)
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
            _PENDING_ADMISSIONS[tuple(self.resources)] = (self.identity, manager)
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
            key = tuple(self.resources)
            entry = _PENDING_ADMISSIONS.get(key)
            if entry is not None and entry[0] == self.identity:
                del _PENDING_ADMISSIONS[key]
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


def waiting(config, project: str) -> list:
    """Every live admission ticket of one project, for the operator's view.

    A ticket is live when its pending-slot lock is still held, so a ticket
    whose owner died is not reported as a waiter. Schema: one entry per
    ticket, naming the roles it waits for, the slot it holds and the
    instant it was enqueued. Bounds: at most one entry per configured
    slot. Trust: local coordination state. Failure: an unreadable or
    malformed queue reports nothing rather than a waiter nobody can verify.
    """
    root = config.data / "slots"
    capacity = max(0, config.limits.parallel_runs)
    try:
        tickets = _read_state(root, capacity, _state_bytes(capacity))
    except _Invalid:
        return []
    live = []
    for ticket in tickets:
        try:
            with lock(root / f"pending-{ticket['owner']}.lock", blocking=False):
                continue
        except Busy:
            pass
        view = _wait_view(ticket)
        if view is not None and view["project"] == project:
            live.append(view)
    return live


def _wait_view(ticket: dict) -> dict | None:
    """One waiting ticket, as the operator sees it, or None."""
    projects = {path.split("/")[0] for path in ticket["resources"]}
    roles = sorted(path.split("/")[2][4:-5] for path in ticket["resources"]
                   if path.split("/")[2].startswith("run-"))
    if len(projects) != 1 or not roles:
        # A ticket the operator cannot attribute names no waiter.
        return None
    view = {"project": projects.pop(), "roles": roles, "slot": ticket["owner"]}
    if "enqueued_at" in ticket:
        view["enqueued_at"] = ticket["enqueued_at"]
    return view


def _state_bytes(capacity: int) -> int:
    """The byte bound of the queue file for a given pending capacity."""
    longest = f"{'a' * 63}/locks/run-{'a' * 63}.lock"
    return len(canonical({"version": 1, "tickets": []})) + capacity * len(canonical(
        {"id": "f" * 32, "owner": capacity - 1, "resources": [longest, longest],
         "enqueued_at": "2099-01-01T00:00:00.000000+00:00"}))


@contextlib.contextmanager
def admission(config, resources=(), *, wait=False, enqueue=False, stop=None, check=None):
    """Admit one work unit; lend nothing and never preempt an active driver.

    Pending capacity is derived from parallel_runs (one waiter per slot).
    Overflow and invalid state are immediate Busy. The operator selects the
    admission_wait_seconds bound on the wait for capacity (0 means none);
    the bound starts when the wait starts, so a caller that is admitted
    without waiting is never refused as an expired wait, and an admitted
    run is never refused by it. Poll at
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
    # The bound is a wait for capacity, so it is armed when the wait begins
    # and not before. A caller that is admitted without waiting has no wait
    # to expire, and a revalidation that is slow on a loaded host must not
    # refuse a run that never queued.
    deadline = None
    queue = _Queue(config, resources)
    if not wait and enqueue:
        _reuse_pending_admission(queue)
    try:
        while True:
            if stop.is_set():
                raise Busy("Run stopped before execution admission")
            if check is not None:
                check()
            if deadline is not None and time.monotonic() >= deadline:
                raise Busy("Execution admission wait expired")
            try:
                attempt = queue.attempt(enqueue=bool(wait or enqueue))
            except (_Full, _Invalid) as exc:
                raise Busy(str(exc)) from None
            except Busy:
                if not wait:
                    raise
                if deadline is None and window:
                    deadline = time.monotonic() + window
                remaining = (max(0.0, deadline - time.monotonic())
                             if deadline is not None else config.limits.idle_seconds)
                stop.wait(min(config.limits.idle_seconds, remaining))
                continue
            # The successful transaction already retired the pending ticket,
            # so the wait for capacity is over. The dispatch revalidation
            # still applies, but an expired wait cannot refuse a run that
            # has already been admitted.
            with attempt:
                if stop.is_set():
                    raise Busy("Run stopped before execution admission")
                if check is not None:
                    check()
                yield max(0.0, time.monotonic() - admission_started)
            return
    finally:
        key = tuple(queue.resources)
        entry = _PENDING_ADMISSIONS.get(key)
        keep_queued = (not wait and bool(enqueue) and not stop.is_set()
                       and entry is not None and entry[0] == queue.identity
                       and queue.alive is not None)
        if keep_queued:
            # A fail-fast daemon ticket keeps its pending place and lock alive
            # for the next admission attempt; it is not a blocking wait.
            pass
        else:
            queue.close()


def _reuse_pending_admission(queue: "_Queue") -> None:
    """Reuse a pending ticket this process already queued, if it is still live."""
    key = tuple(queue.resources)
    entry = _PENDING_ADMISSIONS.get(key)
    if entry is None:
        return
    ticket_id, manager = entry
    queue.identity = ticket_id
    queue.alive = manager
    try:
        tickets = queue._read()
    except Busy:
        tickets = None
    if tickets is not None and not any(
            isinstance(t, dict) and t.get("id") == ticket_id for t in tickets):
        with contextlib.suppress(Exception):
            manager.__exit__(None, None, None)
        del _PENDING_ADMISSIONS[key]
        queue.identity = uuid.uuid4().hex
        queue.alive = None
