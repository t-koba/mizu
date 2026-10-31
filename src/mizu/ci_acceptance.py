"""Operator-only CI proof ingestion; never accepts caller-supplied verdicts.

One configured workflow/run/attempt supplies all declared coverage. The trusted
adapter authenticates provider facts; this module checks their exact scope.
"""
from __future__ import annotations

import re

from .errors import ConfigError, Denied
from .fs import canonical, digest


def _text(value):
    try:
        return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 1024
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


def policy(value, commands):
    """Validate an explicit, versioned operator declaration, not infer equivalence."""
    from .acceptance import _digest
    if (not isinstance(commands, (list, tuple)) or not 1 <= len(commands) <= 32
            or not isinstance(value, dict)
            or set(value) != {'schema', 'version', 'repository', 'workflow', 'workflow_digest', 'acceptance_scope', 'checks'}
            or type(value['schema']) is not int or value['schema'] != 1
            or any(not _text(value[k]) for k in ('version', 'repository', 'workflow'))
            or not _digest(value['workflow_digest']) or not _digest(value['acceptance_scope'])
            or not isinstance(value['checks'], list) or not 1 <= len(value['checks']) <= 32):
        raise Denied('Invalid CI equivalence policy')
    names, covered = set(), set()
    for row in value['checks']:
        if (not isinstance(row, dict) or set(row) != {'check', 'commands', 'coverage', 'environment'}
                or not _text(row['check']) or row['check'] in names
                or not isinstance(row['commands'], list) or not 1 <= len(row['commands']) <= 32
                or any(type(i) is not int or not 0 <= i < len(commands) for i in row['commands'])
                or len(set(row['commands'])) != len(row['commands'])
                or not isinstance(row['coverage'], list) or not 1 <= len(row['coverage']) <= 32
                or any(not _text(c) for c in row['coverage'])
                or len(set(row['coverage'])) != len(row['coverage'])
                or not isinstance(row['environment'], dict) or not 1 <= len(row['environment']) <= 32
                or any(not _text(k) or not _text(v) for k, v in row['environment'].items())):
            raise Denied('Invalid CI check equivalence')
        names.add(row['check'])
        covered.update(row['commands'])
    if not commands or covered != set(range(len(commands))) or len(canonical(value)) > 32768:
        raise Denied('CI equivalence must cover the complete acceptance scope within 32 KiB')
    return value


def configured(settings, commands):
    value = settings.get('ci_acceptance')
    if value is not None:
        try:
            policy(value, commands)
        except (Denied, UnicodeError) as exc:
            raise ConfigError('Invalid project.ci_acceptance policy') from exc
    return value


def bound(project, snapshot, local):
    """Pure selection scope; no adapter I/O and no evidence writes."""
    from .acceptance import _digest
    value = configured(project.settings, project.verify)
    if value is None or local is None or not project.config.vcs.get('command'):
        return None
    # CI is an independent producer: local timeout/failure evidence remains
    # untouched, but does not veto a complete receipt for this exact scope.
    if snapshot.get('skipped') or not _digest(snapshot.get('id')):
        return None
    from .acceptance import _scope
    if value['acceptance_scope'] != _scope(local):
        return None
    return {**local, 'ci': {'policy': value, 'adapter': digest(canonical(project.config.vcs)),
                            'snapshot_id': snapshot['id']}}


def validate(value):
    """Validate the immutable schema-2 receipt, including all provider facts."""
    from .acceptance import MAX_BYTES, _digest, _scope
    try:
        if len(canonical(value)) > MAX_BYTES:
            raise Denied('CI acceptance receipt exceeds byte bound')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied('Invalid CI acceptance JSON') from exc
    if (set(value) != {'schema', 'code_digest', 'scope', 'publication_id', 'passed', 'evidence'}
            or value.get('passed') is not True or not _digest(value.get('code_digest'))
            or not _digest(value.get('publication_id'))):
        raise Denied('Invalid CI acceptance receipt')
    _scope(value['scope'])
    ci = value['scope'].get('ci')
    if ci is None:
        raise Denied('Missing CI acceptance scope')
    spec, evidence = ci['policy'], value['evidence']
    if (not isinstance(evidence, dict) or set(evidence) != {
            'schema', 'repository', 'workflow', 'workflow_digest', 'sha', 'code_digest',
            'run', 'attempt', 'current_attempt', 'complete', 'checks'}
            or type(evidence['schema']) is not int or evidence['schema'] != 1
            or any(evidence[k] != spec[k] for k in ('repository', 'workflow', 'workflow_digest'))
            or evidence['code_digest'] != value['code_digest']
            or not isinstance(evidence['sha'], str)
            or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', evidence['sha'])
            or not _text(evidence['run']) or type(evidence['attempt']) is not int or evidence['attempt'] < 1
            or evidence['attempt'] > 2147483647
            or evidence['complete'] is not True or evidence['current_attempt'] is not True
            or not isinstance(evidence['checks'], list)
            or len(evidence['checks']) != len(spec['checks'])):
        raise Denied('Incomplete or mismatched CI provenance')
    expected = {r['check']: r for r in spec['checks']}
    seen, ids = set(), set()
    for row in evidence['checks']:
        if (not isinstance(row, dict) or set(row) != {
                'check', 'check_id', 'commands', 'coverage', 'environment', 'conclusion'}
                or not _text(row['check']) or row['check'] not in expected or row['check'] in seen
                or not _text(row['check_id']) or row['check_id'] in ids
                or row['conclusion'] != 'success'
                or canonical({k: v for k, v in row.items() if k not in ('check_id', 'conclusion')})
                   != canonical(expected[row['check']])):
            raise Denied('CI coverage is not one-to-one successful equivalence')
        seen.add(row['check'])
        ids.add(row['check_id'])
    return value


def publication_matches(root, value):
    from .publication import Publication
    record = Publication.read_receipt(root, value['publication_id'])
    if (record['code_digest'] != value['code_digest']
            or record['published_sha'] != value['evidence']['sha']):
        raise Denied('CI proof does not match the immutable publication')


def selected(project, snapshot_id):
    """Read only schema-2 proof for an exact published snapshot/current scope.

    Missing, disabled or stale evidence refuses; never fall back to local proof,
    ingest/repair evidence, or query the adapter. Provider freshness is known
    only at ingestion, not rechecked here. Returns (receipt_id, receipt).
    """
    from . import acceptance
    from .check_jobs import cache_guard
    snapshot = project.snapshots.get(snapshot_id)
    if (snapshot_id not in project.snapshots._history_ids()
            or snapshot.get('skipped') or snapshot.get('reserved')):
        raise Denied('CI reuse requires an exact representable published snapshot')
    with cache_guard(project, blocking=False):
        scope = bound(project, snapshot, acceptance._current_scope(project))
        if scope is None:
            raise Denied('CI reuse is disabled or the current acceptance scope is ineligible')
        name = acceptance._index(project.root, snapshot['code_digest'], scope)
        if not acceptance._present(project.root, name):
            raise Denied('No retained CI acceptance for this snapshot and current scope')
        receipt_id = acceptance._selected(project.root, name, snapshot['code_digest'], scope)
        receipt = acceptance.get(project.root, receipt_id)
        if receipt['schema'] != 2:
            raise Denied('CI reuse requires schema-2 acceptance')
        return receipt_id, receipt


def retain(project, *, snapshot_id, publication_id, run, attempt):
    """Host CLI only. Identity selectors are not proof; query the trusted adapter.

    No build/CI dispatch or retries. Cache guard precedes acceptance.lock, just
    as for local producers. A retry first resolves the exact immutable index.
    """
    from . import acceptance, vcs
    from .check_jobs import cache_guard
    from .publication import Publication
    if not _text(run) or type(attempt) is not int or not 1 <= attempt <= 2147483647:
        raise Denied('Invalid CI run/attempt selector')
    snapshot = project.snapshots.get(snapshot_id)
    if snapshot_id not in project.snapshots._history_ids():
        raise Denied('CI acceptance requires a retained published snapshot')
    publication = Publication.read_receipt(project.root, publication_id)
    if publication['code_digest'] != snapshot['code_digest']:
        raise Denied('CI publication source tree mismatch')
    with cache_guard(project, blocking=False):
        scope = bound(project, snapshot, acceptance._current_scope(project))
        if scope is None:
            raise Denied('CI acceptance is disabled or local evidence/scope is ineligible')
        name = acceptance._index(project.root, snapshot['code_digest'], scope)
        if acceptance._present(project.root, name):
            receipt_id = acceptance._selected(project.root, name, snapshot['code_digest'], scope)
            existing = acceptance.get(project.root, receipt_id)
            if (existing['publication_id'] != publication_id or existing['evidence']['run'] != run
                    or existing['evidence']['attempt'] != attempt):
                raise Denied('CI acceptance already pinned to a different publication/run/attempt')
            return {'receipt_id': receipt_id}
        spec = scope['ci']['policy']
        # Deliberately not a vcs_read op: read tools never mint retained authority.
        evidence = vcs.invoke(project.config.vcs, {
            'op': 'acceptance', 'repository': spec['repository'], 'workflow': spec['workflow'],
            'sha': publication['published_sha'], 'run': run, 'attempt': attempt})
        if evidence.get('run') != run or evidence.get('attempt') != attempt:
            raise Denied('CI run/attempt selector mismatch')
        value = {'schema': 2, 'code_digest': snapshot['code_digest'], 'scope': scope,
                 'publication_id': publication_id, 'passed': True, 'evidence': evidence}
        validate(value)
        # Provider enumeration order is not receipt identity.
        evidence['checks'] = sorted(evidence['checks'], key=lambda row: row['check'])
        publication_matches(project.root, value)
        receipt_id = acceptance._retain(project.root, value)
        if receipt_id != digest(canonical(value)):
            raise Denied('Concurrent CI acceptance pinned different evidence')
        return {'receipt_id': receipt_id}
