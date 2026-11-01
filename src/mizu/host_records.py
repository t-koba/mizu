"""Declared host records in SharedRecords; no role writer or second source store.

The publisher CLI is a trusted host entry point, not a model capability. Its
principal is fixed here. Declarations are data: exact snapshot subjects and
bounded discriminated payloads, with no executable policy.
"""
import contextlib
import os
from functools import partial

from .errors import ConfigError, Denied
from .fs import DIGEST, PREVIEW_BYTES, canonical, digest, lock, mkdir, now, sync_dir
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
        raise ConfigError('Invalid declared host record policy') from exc


def _snapshot(snapshots, value):
    if (type(value) is not dict or set(value) != {'snapshot_id', 'code_digest'}
            or any(not isinstance(v, str) or not DIGEST.fullmatch(v) for v in value.values())):
        raise Denied('Expected an exact snapshot and code digest')
    snapshot = snapshots.get(value['snapshot_id'])
    if snapshot['id'] != value['snapshot_id'] or snapshot['code_digest'] != value['code_digest']:
        raise Denied('Host record snapshot digest mismatch')
    return snapshot


def _validate(snapshots, policy, subject, payload):
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
            replacement = _snapshot(snapshots, value)
            if value == subject:
                raise Denied('Replacement must be a newer snapshot')
            # Bound ancestry work by the existing configured snapshot history budget.
            for _ in range(snapshots.history_index):
                parent = replacement.get('parent')
                if parent == subject['snapshot_id']:
                    break
                if parent is None:
                    raise Denied('Replacement is not a newer descendant')
                replacement = snapshots.get(parent)
            else:
                raise Denied('Replacement ancestry is incomplete within history budget')


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
        with contextlib.suppress(OSError, NotImplementedError, Denied):
            from .work_items import _directory
            directory = project.root / 'waits'
            _directory(directory)
            mkdir(directory)
            os.utime(directory, None, follow_symlinks=False)
    return {'record': envelope, 'record_digest': computed}


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
