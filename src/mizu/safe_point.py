"""Durable fail-closed workspace fence for optional safe-point seals.

All mutations require writer/workspace exclusions, then publication.lock.
The marker is local coordination data, not a model-controlled tool surface.
"""
from __future__ import annotations

import re

from . import continuations
from .errors import Denied
from .fs import DIGEST, ID, lock, publish_pointer, read_json, sync_dir, write_json

NAME = 'safe-point.json'
_RUN = re.compile(r'[0-9a-f]{32}\Z')
_FIELDS = {'version', 'role', 'profile', 'source_run', 'run', 'base', 'snapshot'}


def read(store):
    missing = object()
    row = read_json(store.root / NAME, missing, max_bytes=4096)
    if row is missing:
        return None
    if (not isinstance(row, dict) or set(row) != _FIELDS
            or type(row['version']) is not int or row['version'] != 1
            or any(not isinstance(row[k], str) or not pattern.fullmatch(row[k])
                   for k, pattern in (('role', ID), ('profile', ID), ('source_run', _RUN),
                                      ('run', _RUN), ('base', DIGEST)))
            or (row['snapshot'] is not None and (not isinstance(row['snapshot'], str)
                                                 or not DIGEST.fullmatch(row['snapshot'])))):
        raise Denied('Malformed safe-point workspace fence')
    return row


def pending_hint(store):
    """Fail-closed pre-admission hint; guard() remains the authority.

    A malformed or temporarily unreadable fence must not make an event-driven
    writer fail before its ordinary FIFO admission. Treat uncertainty as a
    reason to avoid an early unchanged-work skip, then let guard() validate it
    under the acquired exclusions.
    """
    try:
        return read(store) is not None
    except Exception:
        return True


def _current(store, pointer):
    current = store.get()
    if pointer.get('snapshot') != current['id'] or store._history_ids()[-1] != current['id']:
        raise Denied('Ambiguous safe-point publication evidence')
    return current


def _target(store, row):
    target = store.get(row['snapshot'])
    started = read_json(store.root / 'runs' / row['run'] / 'started.json', None,
                        max_bytes=continuations.RUN_BYTES)
    finish = read_json(store.root / 'runs' / row['run'] / 'finish-intent.json', None,
                       max_bytes=continuations.RUN_BYTES)
    if (not isinstance(started, dict) or started.get('run') != row['run']
            or started.get('role') != row['role'] or started.get('snapshot') != row['base']
            or not isinstance(finish, dict) or finish.get('outcome') not in ('done', 'wait', 'continue', 'blocked', 'bounded')
            or target.get('run') != row['run'] or target.get('parent') != row['base']
            or any(target.get(k) != finish.get(k) for k in ('outcome', 'summary', 'state'))):
        raise Denied('Safe-point seal binding mismatch')
    return target


def _inspect(store, row, pointer):
    current = _current(store, pointer)
    for run in {row['source_run'], row['run']}:
        started = read_json(store.root / 'runs' / run / 'started.json', None,
                            max_bytes=continuations.RUN_BYTES)
        if (not isinstance(started, dict) or started.get('run') != run
                or started.get('role') != row['role'] or started.get('snapshot') != row['base']):
            raise Denied('Safe-point source execution is unconfirmed')
    rows = continuations.entries(pointer)
    if row['snapshot'] == current['id']:
        target = _target(store, row)
        matching = [r for r in rows if r['source_run'] == row['run']]
        if target['outcome'] == 'continue':
            if (len(matching) != 1 or matching[0]['role'] != row['role']
                    or matching[0]['snapshot'] != target['id'] or matching[0]['state'] != 'committed'):
                raise Denied('Safe-point continuation commitment is unconfirmed')
        elif matching:
            raise Denied('Unexpected safe-point continuation')
        return True
    if current['id'] != row['base']:
        raise Denied('Workspace head moved with an unresolved safe-point fence')
    if row['snapshot'] is not None:
        _target(store, row)
    matching = [r for r in rows if r['source_run'] == row['run']]
    if any(r['role'] != row['role'] or r['snapshot'] != row['snapshot']
           or r['state'] != 'prepared' for r in matching):
        raise Denied('Ambiguous unpublished safe-point continuation')
    return False


def guard(store, role):
    """After FIFO/execution admission, reconcile or block before workspace use.

    Only an exact current publication plus its durable seal and (for continue)
    atomic committed continuation can retire a marker left after publication.
    No history-only or result-sidecar claim is publication proof.
    """
    with lock(store.root / 'locks/publication.lock'):
        row = read(store)
        if row is None:
            return None
        if _inspect(store, row, continuations.pointer_at(store.root)):
            (store.root / NAME).unlink()
            sync_dir(store.root)
            return None
        if row['role'] != role:
            raise Denied('Workspace fenced by an unsealed safe-point run; original role must recover')
        return row


def begin(context, profile):
    store = context.project.snapshots
    with lock(store.root / 'locks/publication.lock'):
        if read(store) is not None:
            raise Denied('Safe-point workspace fence already exists')
        row = dict(version=1, role=context.role.name, profile=profile,
                   source_run=context.run_dir.name, run=context.run_dir.name,
                   base=context.snapshot['id'], snapshot=None)
        if _current(store, continuations.pointer_at(store.root))['id'] != row['base']:
            raise Denied('Safe-point input is no longer current')
        write_json(store.root / NAME, row, exclusive=True)


def abandon(context):
    """Remove this run's fence when the checkpoint claim never succeeded."""
    store = context.project.snapshots
    with lock(store.root / 'locks/publication.lock'):
        row = read(store)
        if row is None:
            raise Denied('Safe-point fence disappeared before checkpoint claim')
        if (row['role'] != context.role.name or row['source_run'] != context.run_dir.name
                or row['run'] != context.run_dir.name or row['base'] != context.snapshot['id']
                or row['snapshot'] is not None):
            raise Denied('Safe-point fence changed before checkpoint claim')
        (store.root / NAME).unlink()
        sync_dir(store.root)


def recover(store, row, run):
    """Begin a new finish-only attempt, without resetting any workspace files.

    A prepared continuation of an exactly bound unpublished attempt may be
    retired under the same publication exclusion. It was never runnable.
    The old run/finish/error records remain unchanged.
    """
    with lock(store.root / 'locks/publication.lock'):
        if read(store) != row:
            raise Denied('Safe-point fence changed before recovery')
        pointer = continuations.pointer_at(store.root)
        if _inspect(store, row, pointer):
            raise Denied('Safe-point seal already published; retry admission')
        rows = continuations.entries(pointer)
        remaining = [r for r in rows if r['source_run'] != row['run']]
        # An interrupted continuation successor must first be reconciled by
        # its existing exactly-once admission owner, never bypassed here.
        if any(r['role'] == row['role'] for r in remaining):
            raise Denied('Pending continuation must settle before safe-point recovery')
        if rows != remaining:
            publish_pointer(store.root, continuations._with_entries(pointer, remaining), name='current.json')
        write_json(store.root / NAME, {**row, 'run': run, 'snapshot': None})


def target(store, run, snapshot):
    with lock(store.root / 'locks/publication.lock'):
        row = read(store)
        if row is None or row['run'] != run or row['snapshot'] is not None:
            raise Denied('Safe-point publication lost its workspace fence')
        candidate = {**row, 'snapshot': snapshot}
        _target(store, candidate)
        write_json(store.root / NAME, candidate)
