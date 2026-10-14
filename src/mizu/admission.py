"""Local cross-process FIFO admission, separate from active execution locks."""
from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import time
import uuid
from pathlib import Path

from .errors import Busy
from .fs import ID, canonical, lock, write_json


class _Full(Busy):
    """Queue overflow is a deferral, not another wait for a pending place."""


class _Queue:
    """A bounded list under queue.lock; fixed pending locks prove liveness.

    State is local coordination data: version 1, tickets in arrival order, each
    with a hex id, pending-lock index and zero to two project-relative lock paths.
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
            {"id": "f" * 32, "owner": self.capacity - 1, "resources": [longest, longest]}))
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
            raise Busy("Execution admission queue is unreadable") from None
        if (not isinstance(state, dict) or set(state) != {"version", "tickets"}
                or type(state["version"]) is not int or state["version"] != 1
                or not isinstance(state["tickets"], list) or len(state["tickets"]) > self.capacity):
            raise Busy("Execution admission queue is invalid")
        tickets = state["tickets"]
        owners, identities = set(), set()
        for ticket in tickets:
            if (not isinstance(ticket, dict) or set(ticket) != {"id", "owner", "resources"}
                    or not isinstance(ticket["id"], str) or len(ticket["id"]) != 32
                    or any(c not in "0123456789abcdef" for c in ticket["id"])
                    or type(ticket["owner"]) is not int or not 0 <= ticket["owner"] < self.capacity
                    or not isinstance(ticket["resources"], list) or len(ticket["resources"]) > 2
                    or any(not self._valid_resource(p) for p in ticket["resources"])
                    or ticket["owner"] in owners or ticket["id"] in identities):
                raise Busy("Execution admission queue is invalid")
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
                            "resources": list(self.resources)})
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

        Queued callers may skip older resource-blocked tickets, retaining their
        original place. They never skip a ready ticket. Fail-fast callers do
        not enqueue or bypass *any* live ticket (including recovery probes).
        """
        with lock(self.root / "queue.lock", blocking=False):
            tickets = self._read()
            if tickets and self.alive is None:
                if not enqueue:
                    raise Busy("Older execution admissions are pending")
                self._enqueue(tickets)
            for ticket in tickets:
                if ticket["id"] == self.identity:
                    break
                try:
                    with contextlib.ExitStack() as probe:
                        self._resources(probe, ticket["resources"])
                except Busy:
                    continue  # No slot acquired; resource-blocked head cannot deadlock the queue.
                raise Busy("Older execution admission is ready")
            attempt = contextlib.ExitStack()
            try:
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
    Overflow is immediate Busy. Retry waits use busy_retry_seconds, polling at
    idle_seconds with no role/workspace/slot locks; stop wakes the wait. check
    revalidates normal authority/budget before each attempt and at dispatch,
    outside the queue transaction. Only Busy contention retries. Success
    removes the ticket and releases its pending lock in one transaction;
    timeout, cancellation and other errors invalidate it in finally.
    Dead/stale entries are reaped by the next transaction. Active slot lock
    files remain the independent hard cap. Queue/slot files live under data/slots;
    lock inodes are never unlinked. Invalid state fails closed with Busy.
    """
    stop = stop if stop is not None else threading.Event()
    window = max(0, config.limits.busy_retry_seconds) if wait else 0
    admission_started = time.monotonic()
    deadline = admission_started + window
    queue = _Queue(config, resources)
    retrying = False
    try:
        while True:
            if stop.is_set():
                raise Busy("Run stopped before execution admission")
            if check is not None:
                check()
            if retrying and time.monotonic() >= deadline:
                raise Busy("Execution admission wait expired")
            try:
                attempt = queue.attempt(enqueue=window > 0)
            except _Full as exc:
                raise Busy(str(exc)) from None
            except Busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                stop.wait(min(config.limits.idle_seconds, remaining))
                retrying = True
                continue
            # The successful transaction already retired the pending ticket.
            with attempt:
                if stop.is_set():
                    raise Busy("Run stopped before execution admission")
                if check is not None:
                    check()
                if retrying and time.monotonic() >= deadline:
                    raise Busy("Execution admission wait expired")
                yield max(0.0, time.monotonic() - admission_started)
            return
    finally:
        queue.close()
