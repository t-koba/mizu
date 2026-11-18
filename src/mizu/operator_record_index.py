"""Disposable operator routing metadata, never delivery or record authority.

Call under operator-records.lock. Every query enumerates canonical filenames,
reads/hashes their bytes and checks the projection against those bytes. Only
missing projections decode bodies; invalid existing projections are refused.
This cache is outside shared-records and the backup member list; restore rebuilds it.
"""
from copy import deepcopy
import os
import re
import stat

from .errors import Denied, LimitExceeded
from .fs import DIGEST, atomic_write, canonical, digest, loads_finite
from .shared_records import RECORD_BYTES, _ENVELOPE, _name, _namespace

DIRECTORY = 'operator-record-index'
_FILE = re.compile(r'[0-9a-f]{64}\.json\Z')
_TEMP = re.compile(r'\.new-[a-z0-9_]{8}\Z')


def _files(shared, directory, entries):
    if not shared._parents(f'{directory}/probe'):
        return {}
    result = {}
    for path in sorted(entries(directory)):
        info = path.lstat()
        if (_TEMP.fullmatch(path.name) and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1 and not getattr(info, 'st_file_attributes', 0) & 0x400):
            continue  # Unpublished atomic_write scratch, never a reference.
        if (not _FILE.fullmatch(path.name) or not stat.S_ISREG(info.st_mode)):
            raise Denied('Unsafe operator record index or canonical filename')
        result[path.name] = f'{directory}/{path.name}'
    return result


def _parts(envelope):
    """Canonical framing around the omitted JSON value; no body decoding.

    Matching both frames authenticates *all* routing/closure metadata against
    the canonical bytes, not just against a caller-editable digest in the ref.
    The full digest pins the omitted value, previously validated at creation.
    """
    raw = canonical(envelope)
    marker = b'"body":""'
    if raw.count(marker) != 1:
        raise Denied('Invalid operator record index projection')
    head, tail = raw.split(marker)
    return head + b'"body":', tail


def _projection(shared, raw, name):
    try:
        row = loads_finite(raw)
        row_raw = canonical(row)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Denied('Corrupt shared record JSON') from exc
    if (type(row) is not dict or set(row) != _ENVELOPE
            or type(row['schema']) is not int or row['schema'] != 1
            or type(row['subject']) is not dict or type(row['payload']) is not dict):
        raise Denied('Invalid shared record envelope')
    record_id = _name(row['id'])
    _name(row['kind'], kind=True)
    if (name != shared.record_name(record_id) or _namespace(record_id) != 'operator'
            or row_raw != raw):
        raise Denied('Shared record filename or canonical identity mismatch')
    if shared._validate(row) != raw:
        raise Denied('Invalid operator record')
    row['payload']['body'] = ''
    return {'envelope': row, 'digest': digest(raw)}


def _check(shared, ref, raw, name):
    if (type(ref) is not dict or set(ref) != {'envelope', 'digest'}
            or not isinstance(ref['digest'], str) or not DIGEST.fullmatch(ref['digest'])):
        raise Denied('Invalid operator record index reference')
    row = ref['envelope']
    if (type(row) is not dict or set(row) != _ENVELOPE
            or type(row['schema']) is not int or row['schema'] != 1):
        raise Denied('Invalid operator record index envelope')
    record_id = _name(row['id'])
    _name(row['kind'], kind=True)
    if name != shared.record_name(record_id) or _namespace(record_id) != 'operator':
        raise Denied('Operator record index identity mismatch')
    copy = deepcopy(row)
    if type(copy['payload']) is not dict or copy['payload'].get('body') != '':
        raise Denied('Invalid operator record index body projection')
    if copy['payload'].get('action') != 'withdraw':
        copy['payload']['body'] = 'x'
    shared._validate(copy)
    head, tail = _parts(row)
    if (not raw.startswith(head) or not raw.endswith(tail)
            or len(raw) <= len(head) + len(tail) or digest(raw) != ref['digest']):
        raise Denied('Operator record index differs from canonical record')
    return row, ref['digest']


def _reference(shared, name, raw, *, readonly=False):
    filename = name.rsplit('/', 1)[1]
    ref_name = f'{DIRECTORY}/{filename}'
    ref_raw = shared._read(ref_name, RECORD_BYTES)
    if ref_raw is None:
        ref = _projection(shared, raw, name)
        if not readonly:
            shared._parents(ref_name, create=True)
            atomic_write(shared.root / ref_name, canonical(ref), exclusive=True)
    else:
        try:
            ref = loads_finite(ref_raw)
            encoded_ref = canonical(ref)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise Denied('Corrupt operator record index') from exc
        if encoded_ref != ref_raw:
            raise Denied('Noncanonical operator record index')
    return _check(shared, ref, raw, name)


def remember(shared, envelope):
    """After a successful canonical put, repair/check this event's ref only."""
    _reference(shared, shared.record_name(envelope['id']), canonical(envelope))


def catalog(shared, *, readonly=False, scan_limit=None):
    """Return checked compact operator envelopes; repair missing refs only.

    All canonical bytes are hashed, including unrelated recipients and disabled
    kinds. No mtime trust, mutable cursor or per-recipient canonical identity.
    Read-only projections validate missing refs in memory without repairing them.
    Optional scan_limit bounds entries across canonical and index
    directories, including staging/unrelated entries. Exhaustion refuses the
    entire catalog, even at EOF, without an unbounded sort or lookahead.
    """
    if scan_limit is not None and (type(scan_limit) is not int or scan_limit < 1):
        raise Denied('Operator catalog scan bound must be a positive integer')
    remaining = scan_limit

    def entries(directory):
        nonlocal remaining
        with os.scandir(shared.root / directory) as paths:
            while True:
                if remaining == 0:
                    raise LimitExceeded('Operator catalog scan bound exhausted')
                entry = next(paths, None)
                if entry is None:
                    return
                if remaining is not None:
                    remaining -= 1
                yield shared.root / directory / entry.name

    files = _files(shared, 'shared-records/operator', entries)
    refs = _files(shared, DIRECTORY, entries)
    if refs.keys() - files.keys():
        raise Denied('Dangling operator record index reference')
    result = {}
    for name in files.values():
        raw = shared._read(name, RECORD_BYTES)
        if raw is None:
            raise Denied('Shared record is unavailable')
        row, computed = _reference(shared, name, raw, readonly=readonly)
        result[row['id']] = (row, computed)
    return result
