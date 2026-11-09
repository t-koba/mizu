"""Declared publisher and role records in the existing SharedRecords store.

The publisher CLI is a trusted host entry point, not a model capability. Its
principal is fixed here. Declarations are data: exact snapshot subjects and
bounded discriminated publisher payloads or declared role JSON objects, with no
executable policy.
"""
import contextlib
import os
from functools import partial

from .errors import ConfigError, Denied
from .fs import DIGEST, PREVIEW_BYTES, READ_PAGE_CHARS, canonical, digest, identifier, loads_finite, lock, mkdir, now, sync_dir
from .shared_records import KindPolicy, RECORD_BYTES, SharedRecords, _name

PUBLISHER = 'publisher.cli'
# Schema-v1 structural ceilings, not operational scan/retention policy.
SCHEMA_ENTRIES = 16
# Publication adapter result contract; field validation itself stays declarative.
PUBLICATION_FIELDS = {'refused': {'reason': 'text'},
                      'superseded': {'replacement': 'newer_snapshot'}}


def configuration(raw=None):
    raw = {} if raw is None else raw
    try:
        if type(raw) is not dict or len(raw) > SCHEMA_ENTRIES:
            raise ValueError
        routes = set()
        for kind, row in raw.items():
            _name(kind, kind=True)
            if (type(row) is dict and isinstance(row.get('writer'), str)
                    and row['writer'].startswith('role.')):
                # Role record IDs use '/' as the boundary between the kind and
                # caller ID. Keep that boundary injective while still allowing
                # slash-separated caller IDs.
                if '/' in kind:
                    raise ValueError
                identifier(row['writer'][5:])
                if (set(row) != {'writer', 'route', 'wake', 'retention',
                                 'acknowledgement', 'subject', 'maximum'}
                        or row['route'] not in ('none', 'operator_question')
                        or type(row['wake']) is not bool
                        or (row['route'] == 'none' and row['wake'])
                        or row['retention'] != 'permanent'
                        or row['acknowledgement'] != 'explicit' or row['subject'] != 'object'
                        or type(row['maximum']) is not int or not 1 <= row['maximum'] <= PREVIEW_BYTES):
                    raise ValueError
                continue
            _name(f'host/{kind}/' + 'a' * 64)
            if (type(row) is not dict or set(row) != {
                    'writer', 'route', 'wake', 'retention', 'acknowledgement',
                    'subject', 'maximum', 'variants'}
                    or row['writer'] != PUBLISHER or row['route'] != 'publication_wait'
                    or type(row['wake']) is not bool or row['retention'] != 'permanent'
                    or row['acknowledgement'] != 'explicit' or row['subject'] != 'snapshot'
                    or type(row['maximum']) is not int or not 1 <= row['maximum'] <= PREVIEW_BYTES
                    or row['route'] in routes):
                raise ValueError
            routes.add(row['route'])  # one canonical result authority per route
            variants = row['variants']
            if type(variants) is not dict or not 1 <= len(variants) <= SCHEMA_ENTRIES:
                raise ValueError
            for tag, fields in variants.items():
                _name(tag, kind=True)
                expected = PUBLICATION_FIELDS.get(tag)
                if (expected is None or type(fields) is not dict
                        or set(fields) != set(expected) or len(fields) > SCHEMA_ENTRIES):
                    raise ValueError
                for field, spec in fields.items():
                    _name(field, kind=True)
                    if type(spec) is not dict:
                        raise ValueError
                    if expected[field] == 'newer_snapshot':
                        if spec != {'type': 'newer_snapshot'}:
                            raise ValueError
                    elif (set(spec) != {'type', 'maximum'} or spec['type'] != 'text'
                          or type(spec['maximum']) is not int
                          or not 1 <= spec['maximum'] <= PREVIEW_BYTES):
                        raise ValueError
            if set(variants) != set(PUBLICATION_FIELDS):
                raise ValueError
        return raw
    except (ValueError, TypeError, Denied) as exc:
        raise ConfigError('Invalid declared record policy') from exc


def _snapshot(snapshots, value):
    if (type(value) is not dict or set(value) != {'snapshot_id', 'code_digest'}
            or any(not isinstance(v, str) or not DIGEST.fullmatch(v) for v in value.values())):
        raise Denied('Expected an exact snapshot and code digest')
    snapshot = snapshots.get(value['snapshot_id'])
    if snapshot['id'] != value['snapshot_id'] or snapshot['code_digest'] != value['code_digest']:
        raise Denied('Host record snapshot digest mismatch')
    return snapshot


def _validate(snapshots, policy, subject, payload):
    if policy['writer'].startswith('role.'):
        return  # SharedRecords validates bounded JSON objects; no source authority.
    _snapshot(snapshots, subject)
    if type(payload) is not dict or type(payload.get('outcome')) is not str:
        raise Denied('Host record outcome is required')
    fields = policy['variants'].get(payload['outcome'])
    if fields is None or set(payload) != {'outcome', *fields}:
        raise Denied('Host record payload differs from declaration')
    for field, spec in fields.items():
        value = payload[field]
        if spec['type'] == 'text':
            if not isinstance(value, str) or not value.strip() or len(value) > spec['maximum']:
                raise Denied('Host record text is blank or exceeds declared bound')
        else:
            _snapshot(snapshots, value)
            snapshots.require_descendant(subject['snapshot_id'], value['snapshot_id'])


def store(root, snapshots, policies):
    return SharedRecords(root, {kind: KindPolicy(frozenset({row['writer']}),
        partial(_validate, snapshots, row), maximum=row['maximum'])
        for kind, row in configuration(policies).items()})


def _id(kind, subject):
    return f'host/{kind}/{subject["snapshot_id"]}'


def write_publisher(project, *, kind, subject, payload):
    """Authenticated host route; no caller-supplied principal or timestamp.

    One create-only identity per kind/candidate makes uncertain-write replay
    exact and conflicting outcomes fail closed. No event-ID search or index.
    """
    policies = configuration(project.config.record_kinds)
    if kind not in policies or policies[kind]['writer'] != PUBLISHER:
        raise Denied('Publisher record kind is undeclared')
    shared = store(project.root, project.snapshots, policies)
    with lock(project.root / 'locks' / 'publication.lock'):
        shared.policies[kind].validate(subject, payload)
        record_id = _id(kind, subject)
        envelope = dict(schema=1, id=record_id, kind=kind, writer=PUBLISHER,
                        created_at=now(), subject=subject, payload=payload)
        existing = any(shared._read(name, RECORD_BYTES) is not None
                       for name in shared._record_names(record_id))
        if existing:
            envelope['created_at'] = shared.get(record_id)[0]['created_at']
        # _validate has resolved each exact snapshot and digest. Do not require
        # current rolling-history membership here: a blocked waiter may keep an
        # older candidate alive after that index advances, and the replacement
        # may itself be a retained, not-yet-published candidate.
        computed = shared.put(envelope, writer=PUBLISHER)
        if existing:
            # New writes fsync through SharedRecords.put; replay repairs a prior
            # post-rename directory-sync failure without duplicating that fsync.
            _sync(shared, record_id)
    # Non-authoritative platform hint only when this declaration enables wake.
    if policies[kind]['wake']:
        _wake(project)
    return {'record': envelope, 'record_digest': computed}


def _wake(project):
    """Best-effort host watch hint, never delivery or model admission authority."""
    with contextlib.suppress(OSError, NotImplementedError, Denied):
        from .work_items import _directory
        directory = project.root / 'waits'
        _directory(directory)
        mkdir(directory)
        os.utime(directory, None, follow_symlinks=False)


def write_role(context, args):
    """Runtime-authenticated declared data; no caller principal or source IDs."""
    if "record_write" not in context.role.capabilities:
        raise Denied("Role record writing requires the record_write grant")
    if set(args) != {'kind', 'id', 'subject', 'payload'}:
        raise Denied('Role record write requires exactly kind, id, subject and payload')
    writer = 'role.' + identifier(context.role.name)
    policies = configuration(context.config.record_kinds)
    kind = args['kind']
    if type(kind) is not str or kind not in policies or policies[kind]['writer'] != writer:
        raise Denied('Role record kind or writer is undeclared')
    raw_id = args['id']
    if type(raw_id) is not str:
        raise Denied('Expected a bounded record name')
    record_id = _name(f"role/{context.role.name}/{kind}/{raw_id}")
    try:
        subject, payload = (loads_finite(args[key]) for key in ('subject', 'payload'))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied('Role record subject and payload must be JSON objects') from exc
    shared = store(context.project.root, context.project.snapshots, policies)
    with lock(context.project.root / 'locks' / 'role-records.lock'):
        envelope = dict(schema=1, id=record_id, kind=kind, writer=writer,
                        created_at=now(), subject=subject, payload=payload)
        existing = any(shared._read(name, RECORD_BYTES) is not None
                       for name in shared._record_names(record_id))
        if existing:
            envelope['created_at'] = shared.get(record_id)[0]['created_at']
        computed = shared.put(envelope, writer=writer)
        if existing:
            _sync(shared, record_id)
    if policies[kind]['wake']:
        _wake(context.project)
    return {'record': envelope, 'record_digest': computed}


def read_role(context, args):
    """Author-only exact retrieval; no listing, routing, wake or acknowledgement."""
    configured_role = context.config.roles.get(context.role.name)
    if configured_role is None or "record_read" not in configured_role.capabilities:
        raise Denied("Role record reading requires an explicit record_read grant")
    if 'ack_token' in args:
        raise Denied('Role record read takes kind and id, not ack_token')
    if set(args) - {'kind', 'id', 'offset', 'limit'} or {'kind', 'id'} - set(args):
        raise Denied('Role record read requires exactly kind and id with optional paging')
    writer = 'role.' + identifier(context.role.name)
    policies = configuration(context.config.record_kinds)
    kind = args['kind']
    if type(kind) is not str or kind not in policies or policies[kind]['writer'] != writer:
        raise Denied('Role record kind or writer is undeclared')
    raw_id = args['id']
    if type(raw_id) is not str:
        raise Denied('Expected a bounded record name')
    record_id = _name(f"role/{context.role.name}/{kind}/{raw_id}")
    offset = args.get('offset', 0)
    limit = args.get('limit', READ_PAGE_CHARS)
    if (type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= READ_PAGE_CHARS):
        raise Denied('Invalid shared record page bounds')
    shared = store(context.project.root, context.project.snapshots, policies)
    envelope, computed = shared.get(record_id)
    if envelope['kind'] != kind or envelope['writer'] != writer or envelope['id'] != record_id:
        raise Denied('Shared record canonical identity mismatch')
    text = canonical(envelope).decode('utf-8')
    if offset > len(text):
        raise Denied('Shared record offset exceeds text length')
    end = min(offset + limit, len(text))
    return {'record_id': record_id, 'record_digest': computed,
            'offset': offset, 'total_chars': len(text),
            'next_offset': end if end < len(text) else None,
            'text': text[offset:end]}


def _sync(shared, record_id):
    # Repair a prior create-only write whose final directory sync failed.
    for name in shared._record_names(record_id):
        if shared._read(name, RECORD_BYTES) is not None:
            sync_dir((shared.root / name).parent)


def publication_result(work_items, item, *, sync=True):
    """Exact source lookup only; no receipt/absence/head inference.

    The existing CI-wait delivery owns holder/revision receipts. This record is
    the permanent source authority and is revalidated on delivery and ack.
    """
    wait = item['wait']
    if wait['kind'] != 'publication_ci':
        return None
    policies = configuration(work_items.record_kinds)
    subject = {key: wait[key] for key in ('snapshot_id', 'code_digest')}
    shared = store(work_items.root, work_items.snapshots, policies)
    for kind, policy in policies.items():
        if policy['route'] != 'publication_wait' or not policy['wake']:
            continue
        record_id = _id(kind, subject)
        if not any(shared._read(name, RECORD_BYTES) is not None for name in shared._record_names(record_id)):
            continue
        envelope, computed = shared.get(record_id)
        payload = envelope['payload']
        expected = PUBLICATION_FIELDS.get(payload['outcome'])
        declared = policy['variants'][payload['outcome']]
        if (envelope['subject'] != subject or expected is None
                or {field: spec['type'] for field, spec in declared.items()} != expected):
            return None  # unknown/incomplete adapter contract, even if schema-valid
        if sync:
            _sync(shared, record_id)
        return {'kind': 'publication_outcome', 'trust': 'host-record',
                'work_item': item['id'], 'rev': item['rev'], 'holder': item['holder'],
                **subject, 'wait_digest': digest(canonical(wait)),
                'source_record': record_id, 'source_digest': computed,
                'outcome': envelope['payload'], 'observed_at': envelope['created_at']}
    return None
