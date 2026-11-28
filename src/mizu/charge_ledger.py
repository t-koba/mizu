"""Append-only charge records with bounded admission state.

One day's charges go to a single append-only log. Running counts and request
membership are answered from small bounded sidecars instead of that log, so
the write and read cost of one charge does not depend on accumulated volume.
State is local and caller-locked; every bound is a mechanism limit and every
violation is refused.
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
from .fs import atomic_write, canonical, digest, loads_finite, lock, mkdir, read_json

#: Mechanism bounds. Strings are bounded in characters, one serialized line in
#: bytes, and each page of a read in records; none is an operator setting.
MAX_REQUEST_ID_CHARS = 1024
MAX_PROJECT_CHARS = 63
MAX_LINE_BYTES = 8192
READ_PAGE = 256
MAX_PROJECTS = 4096
MAX_IDS = 65536
MAX_COUNTS_BYTES = 512 * 1024
INTEGER_LIMIT = (1 << 63) - 1

_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_COUNT_KEYS = frozenset({"shared", "projects"})
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
    ``<day>.counts.json`` (running counts) and ``<day>.ids/<digest>``
    (one membership key per scope and request id).

    Callers hold :meth:`lock` across a group of writes; the sidecars and log
    are then coherent with each other. Reads take no lock beyond the atomic
    replacement guarantees of :func:`~mizu.fs.read_json`.
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
        self.index_dir = self.root / f"{day}.ids"
        self.lock_path = self.root / f"{day}.charges.lock"

    @contextlib.contextmanager
    def lock(self):
        """Caller-held exclusion for the log and both sidecars."""
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
                or type(value["shared"]) is not int or not 0 <= value["shared"] <= MAX_IDS
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

    def _add_key(self, key: str) -> None:
        if self._has_key(key):
            raise Denied("Duplicate charge request id")
        if self.index_dir.exists() and (self.index_dir.is_symlink() or not self.index_dir.is_dir()):
            raise Denied("Charge id index is not a directory")
        mkdir(self.index_dir)
        try:
            atomic_write(self.index_dir / key, b"", exclusive=True)
        except Denied as exc:
            raise Denied("Duplicate charge request id") from exc

    def add(self, project: str, request_id: str) -> None:
        """Record one membership key, refusing a duplicate or a full index."""
        key = self._key(project, request_id)
        if self.counts()["shared"] >= MAX_IDS:
            raise InfraExceeded("Charge id index is full")
        self._add_key(key)

    def _append_log(self, line: bytes) -> None:
        mkdir(self.root)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.log_path, flags, 0o600)
        try:
            written = os.write(fd, line)
        finally:
            os.close(fd)
        if written != len(line):
            raise Denied("Incomplete charge append")

    def append(self, charge: Charge) -> None:
        """Append one charge and its bounded state under the caller's lock."""
        line = charge_line(charge)
        counts = self.counts()
        if charge.seq != counts["shared"]:
            raise Denied("Charge sequence is out of order")
        projects = counts["projects"]
        if charge.project not in projects and len(projects) >= MAX_PROJECTS:
            raise InfraExceeded("Charge project table is full")
        if counts["shared"] >= MAX_IDS:
            raise InfraExceeded("Charge id index is full")
        self._add_key(self._key(charge.project, charge.id))
        updated = {"shared": counts["shared"] + 1,
                   "projects": {**projects,
                                charge.project: projects.get(charge.project, 0) + 1}}
        self._write_counts(updated)
        self._append_log(line)

    def read_from(self, seq: int) -> tuple[tuple[Charge, int], ...]:
        """Return at most one bounded page of ``(charge, next_seq)`` from ``seq``.

        Preceding records are consumed as bounded lines and discarded; they
        are never all held in memory. A malformed or out-of-order line fails
        closed, and ``next_seq`` is the position from which to continue.
        """
        if type(seq) is not int or seq < 0:
            raise Denied("Invalid read position")
        try:
            info = self.log_path.lstat()
        except FileNotFoundError:
            return ()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied("Charge log is not a regular file")
        page: list[tuple[Charge, int]] = []
        stream = self.log_path.open("rb")
        with stream:
            ordinal = 0
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
        return tuple(page)
