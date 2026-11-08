"""Bounded retention-only manifest reader; never retain the embedded goal.

Non-goal JSON keeps its normal finite-JSON syntax. The goal token must use
Mizu's canonical UTF-8 string spelling (not alternate Unicode/slash escapes).
Duplicate top-level and file-map keys are refused. These textual restrictions
keep streaming identity validation small; manifests written by Mizu satisfy them.
"""
from __future__ import annotations

import hashlib
import io
import os
import re

from . import platform as _platform
from .config import MAX_PROJECT_GOAL_BYTES
from .errors import Denied, LimitExceeded
from .fs import DIGEST, canonical, loads_finite

# Buffer size is an I/O mechanism, not an input acceptance limit.
_CHUNK = io.DEFAULT_BUFFER_SIZE
_STRING_SPECIAL = re.compile(rb'\\(?:u[0-9a-fA-F]{4}|.)|"')
_VALUE_SPECIAL = re.compile(rb'["\\{}\[\],]')

# Individual file-map member bounds, justified by supported host filesystem
# semantics rather than the generic 1 MiB reference cap.
#
# Decoded workspace path framing is platform-derived, never a universal
# product cap: POSIX hosts use PATH_MAX from pathconf on the project
# filesystem; Windows hosts use the documented extended-length limit
# (32767 UTF-16 code units), consistent with relative_parts imposing no
# Mizu-specific length cap. Raw JSON path tokens allow the worst-case 6x
# escape expansion (\\uXXXX per char) plus quotes.
_WINDOWS_EXTENDED_PATH_UNITS = 32767
# File entries are fixed-schema objects (~113 canonical bytes). 4 KiB covers
# whitespace and field-order variations while rejecting huge extra fields
# before any large allocation.
_MAX_ENTRY_JSON_BYTES = 4096
# A single whitespace run between file-map tokens is formatting, not content.
# 1 KiB covers pretty-printed manifests while rejecting huge padding quickly.
_MAX_FILES_WHITESPACE_RUN = 1024


def _posix_path_max(path) -> int | None:
    """Return PATH_MAX for the filesystem holding ``path``, if bounded."""

    pathconf = getattr(os, "pathconf", None)
    names = getattr(os, "pathconf_names", None)
    if pathconf is None or names is None or "PC_PATH_MAX" not in names:
        return None
    try:
        value = pathconf(path, "PC_PATH_MAX")
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, int) or value <= 0:
        # -1 means no limit; zero/negative is not a usable framing bound.
        return None
    return value


def decoded_path_limit(project_root=None) -> int:
    """Platform-derived decoded path framing bound.

    POSIX hosts use PATH_MAX from pathconf on the project filesystem;
    Windows hosts use the extended-length limit, consistent with
    relative_parts imposing no shorter Mizu cap. When the POSIX inquiry
    is unavailable or unbounded, fall back to the larger derived host
    limit so valid Windows paths are never narrowed by a failed inquiry.
    """

    if _platform.IS_WINDOWS:
        return _WINDOWS_EXTENDED_PATH_UNITS
    candidate = project_root if project_root is not None else "."
    posix = _posix_path_max(candidate)
    if posix is None:
        return _WINDOWS_EXTENDED_PATH_UNITS
    return posix


def path_token_limit(decoded_limit: int) -> int:
    """Raw JSON path-token bound for a decoded path framing bound."""

    return decoded_limit * 6 + 2


def file_member_limit(decoded_limit: int) -> int:
    """Per-member bound including the three separately bounded whitespace runs."""

    return (path_token_limit(decoded_limit) + 3 * _MAX_FILES_WHITESPACE_RUN
            + _MAX_ENTRY_JSON_BYTES + 2)


class _Reader:
    """Bounded read-ahead over a seekable pinned file stream."""

    def __init__(self, stream):
        self.stream = stream
        self.buffer = b''
        self.position = 0

    def peek(self, maximum):
        if maximum <= 0:
            return b''
        if self.position == len(self.buffer):
            self.buffer = self.stream.read(_CHUNK)
            self.position = 0
        return self.buffer[self.position:self.position + maximum]

    def consume(self, count):
        self.position += count

    def read(self, size):
        result = bytearray()
        while size:
            chunk = self.peek(size)
            if not chunk:
                break
            result.extend(chunk)
            self.consume(len(chunk))
            size -= len(chunk)
        return bytes(result)

    def tell(self):
        return self.stream.tell() - (len(self.buffer) - self.position)

    def seek(self, offset):
        self.buffer = b''
        self.position = 0
        self.stream.seek(offset)


def _goal(stream):
    """Read after the opening quote, hashing bounded decoded/canonical chunks."""
    offset = stream.tell() - 1
    text_hash, token_hash = hashlib.sha256(), hashlib.sha256(b'"')
    size, token_size, pending = 0, 1, b''

    def consume(raw):
        nonlocal size, token_size
        text = loads_finite(b'"' + raw + b'"')
        if canonical(text)[1:-2] != raw:
            raise Denied('Snapshot goal requires canonical JSON string spelling')
        encoded = text.encode()
        size += len(encoded)
        if size > MAX_PROJECT_GOAL_BYTES:
            raise LimitExceeded('Snapshot goal exceeds supported byte limit')
        text_hash.update(encoded)
        token_hash.update(raw)
        token_size += len(raw)

    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            raise Denied('Unterminated snapshot goal')
        data = pending + chunk
        # Keep room for an escape and a UTF-8 codepoint across reads.
        # Never split a JSON escape or UTF-8 sequence.
        cut = max(0, len(data) - 12)
        closing = None
        for match in _STRING_SPECIAL.finditer(data):
            if match.group() == b'"':
                closing = match
                break
            if match.start() < cut < match.end():
                cut = match.start()
        if closing is not None:
            consume(data[:closing.start()])
            stream.seek(stream.tell() - len(data) + closing.end())
            token_hash.update(b'"')
            return text_hash.hexdigest(), token_hash.digest(), offset, token_size + 1
        for _ in range(3):  # UTF-8 has at most three continuation bytes.
            if not cut or data[cut] & 0xc0 != 0x80:
                break
            cut -= 1
        if cut and data[cut] & 0xc0 == 0x80:
            raise Denied('Invalid UTF-8 in snapshot goal')
        consume(data[:cut])
        pending = data[cut:]


def _check_file_entry(entry, max_file: int) -> None:
    if (not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str)
            or not DIGEST.fullmatch(entry["sha256"])
            or type(entry.get("bytes")) is not int
            or not 0 <= entry["bytes"] <= max_file
            or type(entry.get("executable")) is not bool):
        raise Denied('Invalid snapshot file metadata')
    # Reject huge extra payloads before any large canonical build. Valid
    # Mizu-written entries are ~113 canonical bytes; 4 KiB covers whitespace
    # and field-order variations.
    if len(canonical(entry)) > _MAX_ENTRY_JSON_BYTES:
        raise LimitExceeded('Snapshot file member exceeds audit limit')


def read_manifest(stream, metadata_bytes: int, snapshot_id: str,
                  max_files: int, max_file: int,
                  max_decoded_path: int) -> tuple[dict, str]:
    """Validate identity without loading the serialized file map or whole goal.

    Goal and files are replaced with empty tokens while framing the object.
    File members stream with platform-derived per-member bounds (POSIX
    PATH_MAX via pathconf on the project filesystem, Windows extended
    limit; maximally escaped paths plus small fixed-schema entries);
    all other fields share the generic metadata budget. Retained path/entry
    records are bounded by max_files and the per-member limits so they can be
    sorted for canonical hashing, never copied into a second serialized map.
    The caller must provide a pinned regular stream and check its stability.
    Returns an audit-only record (parent/time/digests plus file-name and
    file-digest lists for path/GC validation) and the streamed goal digest.
    """
    if not isinstance(max_decoded_path, int) or max_decoded_path <= 0:
        raise Denied("Invalid snapshot path bound")
    path_token_bytes = path_token_limit(max_decoded_path)
    metadata = bytearray()
    stream = _Reader(stream)

    def take():
        if len(metadata) >= metadata_bytes:
            raise LimitExceeded('Snapshot non-goal metadata exceeds audit limit')
        byte = stream.read(1)
        if not byte:
            raise Denied('Incomplete snapshot manifest')
        metadata.extend(byte)
        return byte

    def nonspace():
        byte = take()
        if byte not in b' \t\r\n':
            return byte
        while True:
            chunk = read_metadata_chunk()
            whitespace = len(chunk) - len(chunk.lstrip(b' \t\r\n'))
            if whitespace == len(chunk):
                metadata.extend(chunk)
                stream.consume(len(chunk))
                continue
            consumed = whitespace + 1
            metadata.extend(chunk[:consumed])
            stream.consume(consumed)
            return chunk[whitespace:whitespace + 1]

    def read_metadata_chunk():
        remaining = metadata_bytes - len(metadata)
        if remaining <= 0:
            raise LimitExceeded('Snapshot non-goal metadata exceeds audit limit')
        chunk = stream.peek(min(_CHUNK, remaining))
        if not chunk:
            raise Denied('Incomplete snapshot manifest')
        return chunk

    def read_key(byte):
        if byte != b'"':
            raise Denied('Invalid snapshot key')
        start, escaped = len(metadata) - 1, False
        while True:
            chunk = read_metadata_chunk()
            position = 1 if escaped else 0
            escaped = False
            end = None
            while position < len(chunk):
                quote = chunk.find(b'"', position)
                slash = chunk.find(b'\\', position)
                if slash >= 0 and (quote < 0 or slash < quote):
                    if slash + 1 == len(chunk):
                        escaped = True
                        position = len(chunk)
                    else:
                        position = slash + 2
                elif quote >= 0:
                    end = quote + 1
                    break
                else:
                    position = len(chunk)
            count = len(chunk) if end is None else end
            metadata.extend(chunk[:count])
            stream.consume(count)
            if end is not None:
                break
        return loads_finite(bytes(metadata[start:]))

    def read_value(byte):
        if byte in (b',', b'}'):
            return byte
        value = byte[0]
        depth, quoted, escaped = 0, False, False
        if value == 34:
            quoted = True
        elif value in (123, 91):
            depth = 1
        elif value in (125, 93):
            depth = -1
        delimiter = None
        while delimiter is None:
            chunk = read_metadata_chunk()
            consumed = len(chunk)
            position = 0
            while position < len(chunk):
                if quoted:
                    if escaped:
                        escaped = False
                        position += 1
                        continue
                    quote = chunk.find(b'"', position)
                    slash = chunk.find(b'\\', position)
                    if slash >= 0 and (quote < 0 or slash < quote):
                        escaped = True
                        position = slash + 1
                    elif quote >= 0:
                        quoted = False
                        position = quote + 1
                    else:
                        break
                    continue
                match = _VALUE_SPECIAL.search(chunk, position)
                if match is None:
                    break
                value = match.group()[0]
                position = match.end()
                if value == 34:
                    quoted = True
                elif value in (123, 91):
                    depth += 1
                elif value in (125, 93):
                    if depth == 0 and value == 125:
                        delimiter = b'}'
                        consumed = position
                        break
                    depth -= 1
                elif value == 44 and depth == 0:
                    delimiter = b','
                    consumed = position
                    break
            metadata.extend(chunk[:consumed])
            stream.consume(consumed)
        return delimiter

    # File-map framing uses bounded per-member buffers, never the generic
    # metadata budget. Each member's raw (path token through entry delimiter)
    # stays within the platform-derived per-member bound; path tokens and
    # entries have tighter filesystem-justified sub-bounds enforced after decode.
    def files_skip_space():
        run = 0
        while True:
            chunk = stream.peek(min(_CHUNK, _MAX_FILES_WHITESPACE_RUN + 1 - run))
            if not chunk:
                raise Denied('Incomplete snapshot manifest')
            stripped = chunk.lstrip(b' \t\r\n')
            consumed = len(chunk) - len(stripped)
            if consumed:
                stream.consume(consumed)
                run += consumed
            if run > _MAX_FILES_WHITESPACE_RUN:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
            if consumed < len(chunk):
                return

    def files_read_path():
        first = stream.read(1)
        if first != b'"':
            raise Denied('Invalid snapshot file field')
        raw = bytearray(first)
        escaped = False
        while True:
            if len(raw) >= path_token_bytes:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
            chunk = stream.peek(min(_CHUNK, path_token_bytes - len(raw)))
            if not chunk:
                raise Denied('Incomplete snapshot manifest')
            # Scan for the closing quote, honouring backslash escapes. A
            # trailing backslash defers to the next window via escaped.
            consumed = len(chunk)
            position = 0
            if escaped:
                # Previous window ended on a lone backslash; first byte is escaped.
                position = 1
                escaped = False
                if position > len(chunk):
                    position = len(chunk)
            end = None
            while position < len(chunk):
                quote = chunk.find(b'"', position)
                slash = chunk.find(b'\\', position)
                if slash >= 0 and (quote < 0 or slash < quote):
                    if slash + 1 >= len(chunk):
                        # Lone trailing backslash; consume all and defer.
                        escaped = True
                        position = len(chunk)
                        break
                    position = slash + 2
                elif quote >= 0:
                    end = quote + 1
                    break
                else:
                    position = len(chunk)
                    break
            if end is None:
                count = len(chunk) if not escaped else len(chunk)
                # If we deferred on a trailing backslash, consume all.
                raw.extend(chunk)
                stream.consume(len(chunk))
                if len(raw) > path_token_bytes:
                    raise LimitExceeded('Snapshot file member exceeds audit limit')
                continue
            count = end
            raw.extend(chunk[:count])
            stream.consume(count)
            if len(raw) > path_token_bytes:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
            break
        try:
            name = loads_finite(bytes(raw))
        except ValueError as exc:
            raise Denied('Invalid snapshot file field') from exc
        if not isinstance(name, str):
            raise Denied('Invalid snapshot file field')
        if _platform.IS_WINDOWS:
            # Windows extended-length semantics count UTF-16 code units,
            # consistent with relative_parts imposing no Mizu length cap.
            if len(name.encode("utf-16-le")) // 2 > max_decoded_path:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
        elif len(name.encode()) > max_decoded_path:
            raise LimitExceeded('Snapshot file member exceeds audit limit')
        return name, len(raw)

    def files_read_entry():
        # Accumulate one JSON value plus its trailing member delimiter
        # (',' or files-map '}'), honouring strings and nesting. The entry
        # object itself must fit _MAX_ENTRY_JSON_BYTES; the trailing
        # delimiter adds at most one byte plus bounded surrounding space
        # already skipped by the caller (space before value) and handled here
        # (space between value and delimiter is part of the scan).
        first = stream.read(1)
        if not first:
            raise Denied('Incomplete snapshot manifest')
        if first in (b',', b'}'):
            raise Denied('Invalid snapshot file metadata')
        raw = bytearray(first)
        value = first[0]
        depth, quoted, escaped = 0, False, False
        if value == 34:
            quoted = True
        elif value in (123, 91):
            depth = 1
        elif value in (125, 93):
            depth = -1
        delimiter = None
        # Fast path for single-byte values already complete is handled by the
        # loop below; bare scalars (numbers/literals) end at the first
        # structural delimiter at depth 0.
        while delimiter is None:
            if len(raw) > _MAX_ENTRY_JSON_BYTES + 1:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
            chunk = stream.peek(min(_CHUNK, _MAX_ENTRY_JSON_BYTES + 1 - len(raw) + 16))
            if not chunk:
                raise Denied('Incomplete snapshot manifest')
            consumed = len(chunk)
            position = 0
            while position < len(chunk):
                if quoted:
                    if escaped:
                        escaped = False
                        position += 1
                        continue
                    quote = chunk.find(b'"', position)
                    slash = chunk.find(b'\\', position)
                    if slash >= 0 and (quote < 0 or slash < quote):
                        escaped = True
                        position = slash + 1
                    elif quote >= 0:
                        quoted = False
                        position = quote + 1
                    else:
                        break
                    continue
                match = _VALUE_SPECIAL.search(chunk, position)
                if match is None:
                    break
                value = match.group()[0]
                position = match.end()
                if value == 34:
                    quoted = True
                elif value in (123, 91):
                    depth += 1
                elif value in (125, 93):
                    if depth == 0 and value == 125:
                        delimiter = b'}'
                        consumed = position
                        break
                    depth -= 1
                elif value == 44 and depth == 0:
                    delimiter = b','
                    consumed = position
                    break
            # Bound the entry before buffering more.
            if len(raw) + consumed > _MAX_ENTRY_JSON_BYTES + 1:
                raise LimitExceeded('Snapshot file member exceeds audit limit')
            raw.extend(chunk[:consumed])
            stream.consume(consumed)
            if delimiter is None and consumed < len(chunk):
                raise Denied('Incomplete snapshot manifest')
        if delimiter not in (b',', b'}'):
            raise Denied('Incomplete snapshot manifest')
        member_raw = bytes(raw[:-1])
        try:
            entry = loads_finite(member_raw)
        except ValueError as exc:
            raise Denied('Invalid snapshot file metadata') from exc
        return entry, delimiter, len(raw)

    def read_files():
        outer = metadata
        members: list[tuple[str, dict]] = []
        names: set[str] = set()
        files_skip_space()
        first = stream.read(1)
        if not first:
            raise Denied('Incomplete snapshot manifest')
        if first == b'}':
            state = outer
            state.extend(b'}')
            return members, state
        # Put back the first byte for uniform member parsing: it must be '"'.
        # We consumed one byte; dispatch on it directly.
        pending_first: bytes | None = first
        while True:
            if len(members) >= max_files:
                raise LimitExceeded('Snapshot file count exceeds audit limit')
            if pending_first is None:
                files_skip_space()
                pending_first = stream.read(1)
                if not pending_first:
                    raise Denied('Incomplete snapshot manifest')
            if pending_first != b'"':
                raise Denied('Invalid snapshot file field')
            # Rewind one byte so files_read_path sees the opening quote.
            # _Reader has no unread; seek back by one logical byte.
            stream.seek(stream.tell() - 1)
            name, _ = files_read_path()
            if name in names:
                raise Denied('Duplicate snapshot file key')
            files_skip_space()
            colon = stream.read(1)
            if colon != b':':
                raise Denied('Invalid snapshot file field')
            files_skip_space()
            entry, delimiter, _ = files_read_entry()
            _check_file_entry(entry, max_file)
            names.add(name)
            members.append((name, entry))
            if delimiter == b'}':
                state = outer
                state.extend(b'}')
                return members, state
            pending_first = None

    if nonspace() != b'{':
        raise Denied('Invalid snapshot object')
    keys, goal = set(), None
    members_holder: list[tuple[str, dict]] | None = None
    files_present = False
    delimiter = b','
    while delimiter == b',':
        key = read_key(nonspace())
        if key in keys:
            raise Denied('Duplicate snapshot key')
        keys.add(key)
        if nonspace() != b':':
            raise Denied('Invalid snapshot field')
        byte = nonspace()
        if key == 'goal':
            if byte != b'"':
                raise Denied('Invalid snapshot goal')
            goal = _goal(stream)
            metadata.extend(b'"')
            delimiter = nonspace()
        elif key == 'files':
            if byte != b'{':
                raise Denied('Invalid snapshot files')
            members_holder, metadata = read_files()
            files_present = True
            delimiter = nonspace()
        else:
            delimiter = read_value(byte)
    if delimiter != b'}' or goal is None or not files_present:
        raise Denied('Invalid snapshot manifest')
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        if (len(metadata) + len(chunk) > metadata_bytes
                or chunk.strip(b' \t\r\n')):
            raise Denied('Invalid snapshot trailing data')
        metadata.extend(chunk)
    record = loads_finite(bytes(metadata))
    if record.get('id') != snapshot_id:
        raise Denied('Snapshot integrity failure')

    members = members_holder
    # Verify code digest member-by-member in sorted order, never building
    # the serialized map or its canonical JSON.
    code_hash = hashlib.sha256(b'{')
    ordered = sorted(members, key=lambda item: item[0])
    for number, (name, entry) in enumerate(ordered):
        if number:
            code_hash.update(b',')
        code_hash.update(canonical(name)[:-1] + b':')
        code_hash.update(canonical(entry)[:-1])
    code_hash.update(b'}\n')
    if record.get('code_digest') != code_hash.hexdigest():
        raise Denied('Snapshot code digest mismatch')

    goal_digest, goal_token_digest, offset, length = goal
    body_hash = hashlib.sha256(b'{')
    for index, key in enumerate(sorted(k for k in record if k != 'id')):
        if index:
            body_hash.update(b',')
        body_hash.update(canonical(key)[:-1] + b':')
        if key == 'files':
            body_hash.update(b'{')
            for number, (name, entry) in enumerate(ordered):
                if number:
                    body_hash.update(b',')
                body_hash.update(canonical(name)[:-1] + b':')
                body_hash.update(canonical(entry)[:-1])
            body_hash.update(b'}')
            continue
        if key != 'goal':
            body_hash.update(canonical(record[key])[:-1])
            continue
        stream.seek(offset)
        remaining, token_hash = length, hashlib.sha256()
        while remaining:
            chunk = stream.read(min(_CHUNK, remaining))
            if not chunk:
                raise Denied('Snapshot changed while reading')
            remaining -= len(chunk)
            token_hash.update(chunk)
            body_hash.update(chunk)
        if token_hash.digest() != goal_token_digest:
            raise Denied('Snapshot goal changed while reading')
    body_hash.update(b'}\n')
    if body_hash.hexdigest() != snapshot_id:
        raise Denied('Snapshot integrity failure')
    audit = {
        'id': snapshot_id,
        'parent': record.get('parent'),
        'created_at': record.get('created_at'),
        'goal_digest': record.get('goal_digest'),
        'code_digest': record.get('code_digest'),
        'file_names': [name for name, _ in ordered],
        'file_shas': [entry['sha256'] for _, entry in ordered],
    }
    if 'progress' in record:
        audit['progress'] = record['progress']
    return audit, goal_digest
