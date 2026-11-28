"""Append-only charge records with bounded admission state.

One day's charges go to a single append-only log. The log is the system of
record: a charge commits when its canonical line is written and fsynced there.
Running counts, request membership and a byte-offset checkpoint are
synchronously derived views in small bounded sidecars, so the write and read
cost of one charge does not depend on accumulated volume.

Callers hold :meth:`DayLedger.lock` across admission and append. A process
starting admission after a crash must call :meth:`DayLedger.recover` under
that lock before using :meth:`DayLedger.has` or :meth:`DayLedger.counts`, so a
durable-but-unindexed charge cannot be charged twice. Recovery seeks to the
checkpointed line for the counts position and replays only the tail; an
incoherent checkpoint falls back to a bounded scan from byte zero. Sidecar
writes are idempotent.
State is local; every bound is a mechanism limit and every violation is
refused.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import re
import stat
import unicodedata
from pathlib import Path

from .errors import Denied, InfraExceeded, LimitExceeded
from .fs import atomic_write, canonical, digest, loads_finite, lock, mkdir, read_json, sync_dir

#: Mechanism bounds. Strings are bounded in characters, one serialized line in
#: bytes, and each page of a read in records; none is an operator setting.
MAX_REQUEST_ID_CHARS = 1024
MAX_PROJECT_CHARS = 63
MAX_LINE_BYTES = 8192
READ_PAGE = 256
MAX_PROJECTS = 4096
MAX_COUNTS_BYTES = 512 * 1024
MAX_OFFSET_BYTES = 256
INTEGER_LIMIT = (1 << 63) - 1

_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_COUNT_KEYS = frozenset({"shared", "projects"})
_OFFSET_KEYS = frozenset({"shared", "offset"})
_RECORD_KEYS = frozenset({"id", "project", "seq"})


def _text(value: object, where: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise Denied(f"{where} must be a string of at most {maximum} characters")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise Denied(f"{where} must not contain control characters")
    return value


def _seq(value: object) -> int:
    if type(value) is not int or not 0 <= value <= INTEGER_LIMIT:
        raise Denied("Charge sequence must be a nonnegative integer within bounds")
    return value


class _Page(tuple):
    """A page of records with the byte position just past its last line."""

    def __new__(cls, charges, end_offset: int):
        page = super().__new__(cls, charges)
        page.end_offset = end_offset
        return page


@dataclasses.dataclass(frozen=True)
class Charge:
    """One charge: an opaque request id, a scope name and a day ordinal."""
    id: str
    project: str
    seq: int


def charge_line(charge: Charge) -> bytes:
    """Serialize one canonical, bounded record terminated by a single newline."""
    if not isinstance(charge, Charge):
        raise Denied("Invalid charge record")
    _text(charge.id, "request id", MAX_REQUEST_ID_CHARS)
    _text(charge.project, "project", MAX_PROJECT_CHARS)
    _seq(charge.seq)
    line = canonical({"id": charge.id, "project": charge.project, "seq": charge.seq})
    if len(line) > MAX_LINE_BYTES or line.count(b"\n") != 1 or not line.endswith(b"\n"):
        raise Denied("Charge line exceeds its byte bound")
    return line


def parse_charge_line(line: bytes) -> Charge:
    """Parse one canonical line, refusing malformed, noncanonical or over-bound input."""
    if (not isinstance(line, (bytes, bytearray)) or not line
            or len(line) > MAX_LINE_BYTES or line.count(b"\n") != 1
            or not line.endswith(b"\n")):
        raise Denied("Charge line is malformed or over bound")
    try:
        value = loads_finite(bytes(line))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Denied("Charge line is not parseable") from exc
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise Denied("Charge line has an unexpected shape")
    charge = Charge(id=value["id"], project=value["project"], seq=value["seq"])
    try:
        canonical_line = charge_line(charge)
    except (Denied, ValueError, UnicodeError, RecursionError) as exc:
        raise Denied("Charge line holds an out-of-bound record") from exc
    if canonical_line != bytes(line):
        raise Denied("Charge line is not canonical")
    return charge


class DayLedger:
    """One day's charge log and its bounded sidecars.

    Layout under ``root``: ``<day>.charges`` (append-only log),
    ``<day>.counts.json`` (running counts), ``<day>.offset.json`` (byte
    position of the counts checkpoint) and ``<day>.ids/<digest>`` (one
    membership key per scope and request id).

    The log is committed first and durably; the sidecars are derived from it.
    Callers hold :meth:`lock` across a group of writes and run :meth:`recover`
    before admission when a previous process may have died between the two.
    Reads take no lock beyond the atomic replacement guarantees of
    :func:`~mizu.fs.read_json`.
    """

    def __init__(self, root: Path, day: str):
        if not isinstance(day, str) or not _DAY.fullmatch(day):
            raise Denied("Invalid ledger day")
        try:
            dt.date.fromisoformat(day)
        except ValueError as exc:
            raise Denied("Invalid ledger day") from exc
        self.root = Path(root)
        self.day = day
        self.log_path = self.root / f"{day}.charges"
        self.counts_path = self.root / f"{day}.counts.json"
        self.offset_path = self.root / f"{day}.offset.json"
        self.index_dir = self.root / f"{day}.ids"
        self.lock_path = self.root / f"{day}.charges.lock"
        # Validated log prefix (counts checkpoint, log bytes) for this instance.
        self._recovered: tuple[int, int] | None = None

    @contextlib.contextmanager
    def lock(self):
        """Caller-held exclusion for the log and its sidecars."""
        with lock(self.lock_path):
            yield

    @staticmethod
    def _key(project: str, request_id: str) -> str:
        return digest(canonical({"project": _text(project, "project", MAX_PROJECT_CHARS),
                                 "id": _text(request_id, "request id", MAX_REQUEST_ID_CHARS)}))

    def counts(self) -> dict:
        """Return shared and per-project counts from the bounded sidecar only."""
        try:
            value = read_json(self.counts_path, None, max_bytes=MAX_COUNTS_BYTES)
        except (Denied, LimitExceeded, OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise Denied("Charge counts sidecar is unreadable") from exc
        if value is None:
            return {"shared": 0, "projects": {}}
        if (not isinstance(value, dict) or set(value) != _COUNT_KEYS
                or type(value["shared"]) is not int or not 0 <= value["shared"] <= INTEGER_LIMIT
                or not isinstance(value["projects"], dict)
                or len(value["projects"]) > MAX_PROJECTS):
            raise Denied("Charge counts sidecar is malformed")
        for name, count in value["projects"].items():
            _text(name, "project", MAX_PROJECT_CHARS)
            if type(count) is not int or not 0 <= count <= value["shared"]:
                raise Denied("Charge counts sidecar is malformed")
        return {"shared": value["shared"], "projects": dict(value["projects"])}

    def _write_counts(self, counts: dict) -> None:
        body = canonical(counts)
        if len(body) > MAX_COUNTS_BYTES:
            raise InfraExceeded("Charge counts sidecar exceeds its byte bound")
        mkdir(self.root)
        atomic_write(self.counts_path, body)

    def _write_offset(self, shared: int, offset: int) -> None:
        """Publish a byte checkpoint for the counts generation just written."""
        if (type(shared) is not int or not 0 <= shared <= INTEGER_LIMIT
                or type(offset) is not int or not 0 <= offset <= INTEGER_LIMIT):
            raise Denied("Invalid charge offset checkpoint")
        body = canonical({"shared": shared, "offset": offset})
        if len(body) > MAX_OFFSET_BYTES:
            raise InfraExceeded("Charge offset sidecar exceeds its byte bound")
        mkdir(self.root)
        atomic_write(self.offset_path, body)

    def _checkpoint(self, seq: int, log_size: int) -> int | None:
        """Return a trusted seek position for ``seq``, or None to scan from 0.

        The checkpoint names the counts generation it describes. Anything
        missing, malformed, negative, past the end of the log or describing a
        different generation is unusable: replay from byte zero is slower but
        correct. An offset of zero cannot describe a nonempty checkpointed
        prefix either.
        """
        try:
            value = read_json(self.offset_path, None, max_bytes=MAX_OFFSET_BYTES)
        except (Denied, LimitExceeded, OSError, ValueError, UnicodeError, RecursionError):
            return None
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != _OFFSET_KEYS
                or type(value["shared"]) is not int or value["shared"] != seq
                or type(value["offset"]) is not int
                or not 0 <= value["offset"] <= log_size
                or (value["offset"] == 0 and seq != 0)):
            return None
        return value["offset"]

    def _has_key(self, key: str) -> bool:
        entry = self.index_dir / key
        try:
            info = entry.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise Denied("Charge id index is unreadable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied("Charge id index entry is not a regular file")
        return True

    def has(self, project: str, request_id: str) -> bool:
        """Answer membership from one digest-keyed sidecar entry, never the log."""
        return self._has_key(self._key(project, request_id))

    def _add_key(self, key: str, *, exclusive: bool = True) -> None:
        if self._has_key(key):
            if exclusive:
                raise Denied("Duplicate charge request id")
            return
        if self.index_dir.exists() and (self.index_dir.is_symlink() or not self.index_dir.is_dir()):
            raise Denied("Charge id index is not a directory")
        mkdir(self.index_dir)
        try:
            atomic_write(self.index_dir / key, b"", exclusive=True)
        except Denied as exc:
            raise Denied("Duplicate charge request id") from exc

    def add(self, project: str, request_id: str) -> None:
        """Record one membership key, refusing a duplicate."""
        key = self._key(project, request_id)
        self._add_key(key)

    def _log_size(self) -> int:
        try:
            info = self.log_path.lstat()
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied("Charge log is not a regular file")
        return info.st_size

    def _append_log(self, line: bytes) -> None:
        mkdir(self.root)
        try:
            self.log_path.lstat()
            existed = True
        except FileNotFoundError:
            existed = False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= (getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                  | getattr(os, "O_BINARY", 0))
        fd = os.open(self.log_path, flags, 0o600)
        try:
            written = os.write(fd, line)
            if written == len(line):
                os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            sync_dir(self.root)
        if written != len(line):
            raise Denied("Incomplete charge append")

    def recover(self) -> int:
        """Derive sidecars from the log tail after the counts checkpoint.

        Caller holds :meth:`lock`. Replay is forward-only and bounded: the log
        is committed before any sidecar, so it is never behind the derived
        view. Existing membership keys are kept, making replay idempotent.
        Returns the number of records applied. A cold instance seeks straight
        to the checkpoint when it is coherent with the counts sidecar.
        """
        counts = self.counts()
        seq = counts["shared"]
        log_size = self._log_size()
        if self._recovered == (seq, log_size):
            return 0
        checkpoint = self._checkpoint(seq, log_size)
        start = checkpoint if checkpoint is not None else 0
        projects = dict(counts["projects"])
        applied = 0
        while True:
            page = self.read_from(seq, start_offset=start)
            if not page:
                if checkpoint is None and seq and page.end_offset:
                    # Repair an unusable checkpoint so later cold starts can
                    # seek; counts and log are already at this position. An
                    # empty ledger needs no sidecar for its zero checkpoint.
                    self._write_offset(seq, page.end_offset)
                self._recovered = (seq, log_size)
                return applied
            for charge, next_seq in page:
                self._add_key(self._key(charge.project, charge.id), exclusive=False)
                if charge.project not in projects and len(projects) >= MAX_PROJECTS:
                    raise InfraExceeded("Charge project table is full")
                projects[charge.project] = projects.get(charge.project, 0) + 1
                seq = next_seq
                applied += 1
            self._write_counts({"shared": seq, "projects": projects})
            start = page.end_offset
            self._write_offset(seq, start)

    def append(self, charge: Charge) -> None:
        """Commit one durable log record, then derive its sidecars."""
        line = charge_line(charge)
        self.recover()
        counts = self.counts()
        if charge.seq != counts["shared"]:
            raise Denied("Charge sequence is out of order")
        projects = counts["projects"]
        if charge.project not in projects and len(projects) >= MAX_PROJECTS:
            raise InfraExceeded("Charge project table is full")
        if self.has(charge.project, charge.id):
            raise Denied("Duplicate charge request id")
        offset = self._log_size()
        self._append_log(line)
        updated = {"shared": counts["shared"] + 1,
                   "projects": {**projects,
                                charge.project: projects.get(charge.project, 0) + 1}}
        self._add_key(self._key(charge.project, charge.id))
        self._write_counts(updated)
        self._write_offset(updated["shared"], offset + len(line))
        self._recovered = (updated["shared"], self._log_size())

    def admit(self, project: str, request_id: str, *, daily: int,
              shared_daily: int = 0) -> int:
        """Admit one request against the per-project and shared day limits.

        The caller holds :meth:`lock`; recovery runs under it first, so a
        durable-but-unindexed charge is never charged twice. Schema: opaque
        ``request_id`` and scope ``project`` (``""`` uses the shared set).
        Bounds: limits are nonnegative integers (bool excluded); a zero shared
        limit disables that ceiling. Trust: local operator state under the
        caller's lock, with the log as the system of record. Retry: idempotent
        per (project, request_id), returning the scope count with no new log
        line. Evidence: returns the scope count after admission. Failure:
        ``Denied`` for invalid limits or unreadable state, ``InfraExceeded``
        for per-project or shared exhaustion.
        """
        if (type(daily) is not int or daily < 0
                or type(shared_daily) is not int or shared_daily < 0):
            raise Denied("Admission limits must be nonnegative integers")
        self.recover()
        known = self.has(project, request_id)
        counts = self.counts()
        scope = counts["shared"] if not project else counts["projects"].get(project, 0)
        if known:
            return scope
        if scope >= daily:
            if project:
                raise InfraExceeded(
                    f"daily model-request budget exhausted for project {project}")
            raise InfraExceeded("daily model-request budget exhausted")
        if shared_daily > 0 and counts["shared"] >= shared_daily:
            raise InfraExceeded("daily model-request shared budget exhausted")
        self.append(Charge(id=request_id, project=project, seq=counts["shared"]))
        updated = self.counts()
        return updated["shared"] if not project else updated["projects"].get(project, 0)

    def read_from(self, seq: int, *, start_offset: int = 0) -> tuple[tuple[Charge, int], ...]:
        """Return at most one bounded page of ``(charge, next_seq)`` from ``seq``.

        The result is a tuple; its internal ``end_offset`` is the byte
        position where reading stopped, used to advance the checkpoint.
        Preceding records are consumed as bounded lines and discarded; they
        are never all held in memory. A malformed or out-of-order line fails
        closed, and ``next_seq`` is the position from which to continue.

        ``start_offset`` is internal to :meth:`recover`. It skips a validated
        checkpointed prefix; the first line found there must have ``seq`` as
        its ordinal or the read fails closed. Untrusted checkpoints are passed
        as 0, where records before ``seq`` are still consumed and discarded.
        """
        if type(seq) is not int or seq < 0:
            raise Denied("Invalid read position")
        if type(start_offset) is not int or start_offset < 0:
            raise Denied("Invalid read offset")
        log_size = self._log_size()
        if start_offset > log_size:
            raise Denied("Read offset exceeds log size")
        if not log_size:
            return _Page((), 0)
        page: list[tuple[Charge, int]] = []
        stream = self.log_path.open("rb")
        with stream:
            stream.seek(start_offset)
            ordinal = seq if start_offset else 0
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                charge = parse_charge_line(line)
                if charge.seq != ordinal:
                    raise Denied("Charge log sequence is inconsistent")
                ordinal += 1
                if ordinal <= seq:
                    continue
                page.append((charge, charge.seq + 1))
                if len(page) >= READ_PAGE:
                    break
            end_offset = stream.tell()
        return _Page(page, end_offset)
