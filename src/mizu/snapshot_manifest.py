"""Bounded retention-only manifest reader; never retain the embedded goal.

Non-goal JSON keeps its normal finite-JSON syntax. The goal token must use
Mizu's canonical UTF-8 string spelling (not alternate Unicode/slash escapes).
Top-level duplicate keys are refused. These textual restrictions keep streaming
identity validation small; all manifests written by Mizu already satisfy them.
"""
from __future__ import annotations

import hashlib
import io
import re

from .config import MAX_PROJECT_GOAL_BYTES
from .errors import Denied, LimitExceeded
from .fs import canonical, loads_finite

# Buffer size is an I/O mechanism, not an input acceptance limit.
_CHUNK = io.DEFAULT_BUFFER_SIZE
_STRING_SPECIAL = re.compile(rb'\\(?:u[0-9a-fA-F]{4}|.)|"')
_VALUE_SPECIAL = re.compile(rb'["\\{}\[\],]')


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


def read_manifest(stream, metadata_bytes: int, snapshot_id: str) -> tuple[dict, str]:
    """Validate content identity with O(metadata_bytes + I/O buffer) memory.

    First frame the top-level object, replacing only goal with an empty token.
    Then hash canonical metadata around a bounded reread of the canonical goal
    token. Its content hash must match the first pass, not merely its stat.
    The caller must provide a pinned regular stream and check its stability.
    """
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

    if nonspace() != b'{':
        raise Denied('Invalid snapshot object')
    keys, goal = set(), None
    delimiter = b','
    while delimiter == b',':
        byte = nonspace()
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
        key = loads_finite(bytes(metadata[start:]))
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
        else:
            if byte in (b',', b'}'):
                delimiter = byte
            else:
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
    if delimiter != b'}' or goal is None:
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

    goal_digest, goal_token_digest, offset, length = goal
    body_hash = hashlib.sha256(b'{')
    for index, key in enumerate(sorted(k for k in record if k != 'id')):
        if index:
            body_hash.update(b',')
        body_hash.update(canonical(key)[:-1] + b':')
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
    del record['goal']
    return record, goal_digest
