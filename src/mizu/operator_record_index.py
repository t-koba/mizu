"""Disposable operator routing metadata, never delivery or record authority.

Call under operator-records.lock. Every query enumerates canonical filenames,
reads/hashes their bytes and checks the projection against those bytes. Only
missing projections decode bodies; invalid existing projections are refused.
Legacy flat files (including other source families) are covered too. This cache
is outside shared-records and the backup member list; restore rebuilds it.
"""
from copy import deepcopy
import re
import stat

from .errors import Denied
from .fs import DIGEST, atomic_write, canonical, digest, loads_finite
from .shared_records import RECORD_BYTES, _ENVELOPE, _name, _namespace, _time

DIRECTORY = 'operator-record-index'
_FILE = re.compile(r'[0-9a-f]{64}\.json\Z')
_TEMP = re.compile(r'\.new-[a-z0-9_]{8}\Z')


def _files(shared, directory):
    if not shared._parents(f'{directory}/probe'):
        return {}
    result = {}
    for path in sorted((shared.root / directory).iterdir()):
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
    if _namespace(envelope['id']) != 'operator':
        # Foreign legacy records need only identity classification. Their
        # payload/subject can be large; the bounded ID precedes both in JSON.
        return (raw.split(b'"payload":', 1)[0] + b'"payload":',
                b',"writer":' + canonical(envelope['writer']).rstrip(b'\n') + b'}\n')
    marker = b'"body":""'
    if raw.count(marker) != 1:
        raise Denied('Invalid operator record index projection')
    head, tail = raw.split(marker)
    return head + b'"body":', tail


def _projection(shared, raw, name, *, operator):
    try:
        row = loads_finite(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Denied('Corrupt shared record JSON') from exc
    if (type(row) is not dict or set(row) != _ENVELOPE
            or type(row['schema']) is not int or row['schema'] != 1
            or type(row['subject']) is not dict or type(row['payload']) is not dict):
        raise Denied('Invalid shared record envelope')
    record_id = _name(row['id'])
    _name(row['kind'], kind=True)
    is_operator = _namespace(record_id) == 'operator'
    if (name not in shared._record_names(record_id) or (operator and not is_operator)
            or canonical(row) != raw):
        raise Denied('Shared record filename or canonical identity mismatch')
    if is_operator:
        if shared._validate(row) != raw:
            raise Denied('Invalid operator record')
        row['payload']['body'] = ''
    else:
        _name(row['writer'])
        _time(row['created_at'])
        row['payload'] = row['subject'] = {}
    return {'envelope': row, 'digest': digest(raw)}


def _check(shared, ref, raw, name, *, operator):
    if (type(ref) is not dict or set(ref) != {'envelope', 'digest'}
            or not isinstance(ref['digest'], str) or not DIGEST.fullmatch(ref['digest'])):
        raise Denied('Invalid operator record index reference')
    row = ref['envelope']
    if (type(row) is not dict or set(row) != _ENVELOPE
            or type(row['schema']) is not int or row['schema'] != 1):
        raise Denied('Invalid operator record index envelope')
    record_id = _name(row['id'])
    _name(row['kind'], kind=True)
    is_operator = _namespace(record_id) == 'operator'
    if name not in shared._record_names(record_id) or (operator and not is_operator):
        raise Denied('Operator record index identity mismatch')
    if is_operator:
        copy = deepcopy(row)
        if type(copy['payload']) is not dict or copy['payload'].get('body') != '':
            raise Denied('Invalid operator record index body projection')
        if copy['payload'].get('action') != 'withdraw':
            copy['payload']['body'] = 'x'
        shared._validate(copy)
    else:
        _name(row['writer'])
        _time(row['created_at'])
        if row['payload'] != {} or row['subject'] != {}:
            raise Denied('Invalid legacy record index projection')
    head, tail = _parts(row)
    if (not raw.startswith(head) or not raw.endswith(tail)
            or len(raw) <= len(head) + len(tail) or digest(raw) != ref['digest']):
        raise Denied('Operator record index differs from canonical record')
    return row, ref['digest']


def _reference(shared, name, raw, *, operator):
    filename = name.rsplit('/', 1)[1]
    ref_name = f'{DIRECTORY}/{filename}'
    ref_raw = shared._read(ref_name, RECORD_BYTES)
    if ref_raw is None:
        ref = _projection(shared, raw, name, operator=operator)
        shared._parents(ref_name, create=True)
        atomic_write(shared.root / ref_name, canonical(ref), exclusive=True)
    else:
        try:
            ref = loads_finite(ref_raw)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise Denied('Corrupt operator record index') from exc
        if canonical(ref) != ref_raw:
            raise Denied('Noncanonical operator record index')
    return _check(shared, ref, raw, name, operator=operator)


def remember(shared, envelope):
    """After a successful canonical put, repair/check this event's ref only."""
    _reference(shared, shared._record_name(envelope['id']), canonical(envelope), operator=True)


def catalog(shared):
    """Return checked compact operator envelopes; repair missing refs only.

    All canonical bytes are hashed, including unrelated recipients and disabled
    kinds. No mtime trust, mutable cursor or per-recipient canonical identity.
    Identical legacy/sharded duplicates share a ref; conflicting copies refuse.
    """
    files = _files(shared, 'shared-records/operator')
    legacy = {}
    if shared._parents('shared-records/probe'):
        for path in (shared.root / 'shared-records').iterdir():
            if path.name.endswith('.json'):
                if not _FILE.fullmatch(path.name):
                    raise Denied('Unsafe legacy shared record filename')
                legacy[path.name] = f'shared-records/{path.name}'
    refs = _files(shared, DIRECTORY)
    if refs.keys() - (files.keys() | legacy.keys()):
        raise Denied('Dangling operator record index reference')
    result = {}
    for filename in sorted(files.keys() | legacy.keys()):
        name = files.get(filename, legacy.get(filename))
        raw = shared._read(name, RECORD_BYTES)
        if raw is None:
            raise Denied('Shared record is unavailable')
        if filename in files and filename in legacy:
            if shared._read(legacy[filename], RECORD_BYTES) != raw:
                raise Denied('Shared record identity conflict')
        row, computed = _reference(shared, name, raw, operator=filename in files)
        if _namespace(row['id']) == 'operator':
            result[row['id']] = (row, computed)
    return result
