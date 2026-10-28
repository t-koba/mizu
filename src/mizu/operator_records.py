"""Operator CLI records; immutable content and closure events in SharedRecords.

The host CLI is the authenticated operator channel (as for insight submit).
There is no model writer. Runtime readers receive only a frozen manifest.
Policy controls enablement, routing and permanent retention. Pending records
are also the scheduler signal; no second queue or mutable delivery cursor exists.
"""
from dataclasses import asdict, replace
import datetime as dt
import heapq

from .errors import ConfigError, Denied
from .fs import DIGEST, identifier, lock, now
from .shared_records import KindPolicy, SharedRecords

WRITER = 'operator.cli'
ROUTES = {'answer': 'question_source', 'operator.notice': 'addressee'}


def configuration(raw=None):
    raw = {} if raw is None else raw
    if type(raw) is not dict or set(raw) - ROUTES.keys():
        raise ConfigError('operator_records must declare supported kinds only')
    result = {}
    for kind, route in ROUTES.items():
        declared = kind in raw
        row = raw.get(kind, {})
        if type(row) is not dict or set(row) - {'enabled', 'route', 'retention', 'wake'}:
            raise ConfigError('Invalid operator record policy')
        # An absent kind is not a declaration: existing configs stay closed.
        row = {'enabled': declared, 'route': route, 'retention': 'permanent', 'wake': True, **row}
        if (type(row['enabled']) is not bool or type(row['wake']) is not bool or row['route'] != route
                or row['retention'] != 'permanent'):
            raise ConfigError('Unsupported operator record route or retention')
        result[kind] = row
    return result


def _subject(subject):
    if (type(subject) is not dict or set(subject) != {'type', 'id', 'rev'} or subject['type'] != 'insight'
            or type(subject['rev']) is not int or subject['rev'] < 1):
        raise Denied('Expected an exact insight subject and revision')
    identifier(subject['id'])


def _validate(kind, subject, payload):
    if subject:
        _subject(subject)
    if kind == 'answer' and not subject:
        raise Denied('Answer requires an exact question subject')
    if set(payload) != {'action', 'body', 'addressee', 'target'}:
        raise Denied('Invalid operator record payload')
    identifier(payload['addressee'])
    body, action, target = payload['body'], payload['action'], payload['target']
    if not isinstance(body, str) or len(body) > 32768:
        raise Denied('Operator record body exceeds bound')
    if action not in ('publish', 'withdraw', 'supersede') or (kind == 'answer' and action != 'publish'):
        raise Denied('Invalid operator record action')
    if action == 'withdraw':
        if body or subject:
            raise Denied('Withdrawal must not contain content')
    elif not body.strip():
        raise Denied('Operator record body is required')
    if action == 'publish':
        if target is not None:
            raise Denied('Publish cannot close another record')
    elif (type(target) is not dict or set(target) != {'id', 'digest'}
          or not isinstance(target['digest'], str) or not DIGEST.fullmatch(target['digest'])
          or not isinstance(target['id'], str) or len(target['id']) > 256):
        raise Denied('Closure requires an exact record and digest')


def store(project):
    return SharedRecords(project.insights.root, {
        kind: KindPolicy(frozenset({WRITER}),
                         lambda subject, payload, kind=kind: _validate(kind, subject, payload),
                         maximum=140000) for kind in ROUTES})


def _question(project, subject):
    _subject(subject)
    current = project.insights.read(subject['id'])
    if current.get('id') != subject['id']:
        raise Denied('Subject must use the full exact insight ID')
    if current.get('rev', 1) == subject['rev']:
        return current
    matches = [row for row in project.insights.history(subject['id'])
               if type(row) is dict and row.get('id') == subject['id']
               and type(row.get('rev', 1)) is int and row.get('rev', 1) == subject['rev']]
    if len(matches) != 1:
        raise Denied('Question revision is unavailable or ambiguous')
    item = matches[0]
    # Source is identity, not revision content: reject a historical record whose
    # routing principal disagrees with the current, source-validated insight.
    if item.get('source') != current.get('source'):
        raise Denied('Question source changed across revisions')
    return item


def _role(project, role):
    identifier(role)
    if role not in project.roles or role not in project.config.roles:
        raise Denied('Operator record addressee is not a configured project role')
    return role


def _route(project, envelope):
    role = _role(project, envelope['payload']['addressee'])
    if envelope['kind'] == 'answer':
        question = _question(project, envelope['subject'])
        if question.get('source') != role:
            raise Denied('Answer must address the authenticated asking role')
    return role


def _target(shared, envelope):
    target = envelope['payload']['target']
    old, _ = shared.get(target['id'], expected_digest=target['digest'])
    if (old['kind'] != 'operator.notice' or old['writer'] != envelope['writer']
            or old['payload']['action'] == 'withdraw' or old['id'] == envelope['id']
            or old['payload']['addressee'] != envelope['payload']['addressee']):
        raise Denied('Only the notice writer may close an exact notice')
    return old


def write(project, *, event_id, kind, body='', addressee=None, subject=None,
          action='publish', target=None):
    """Trusted CLI only. Stable event ID is required for uncertain-write replay.

    No writer parameter: identity is bound by this host entry point. A retry
    reuses the retained timestamp and must match all immutable content exactly.
    All records/closures serialize under one host lock; failures never repair.
    """
    identifier(event_id)
    policy = configuration(project.config.operator_records)
    if not isinstance(kind, str) or kind not in policy or not policy[kind]['enabled']:
        raise Denied('Operator record kind is disabled or undeclared')
    shared = store(project)
    record_id = f'operator/{project.name}/{event_id}'
    subject = {} if subject is None else subject
    if kind == 'answer':
        question = _question(project, subject)
        asking = _role(project, question.get('source'))
        if addressee is not None and addressee != asking:
            raise Denied('Answer addressee differs from asking role')
        addressee = asking
    elif subject:
        # A notice's optional subject is checked at write time; it never changes
        # the independently configured addressee route on later deliveries.
        _question(project, subject)
    envelope = dict(schema=1, id=record_id, kind=kind, created_at=now(), writer=WRITER,
                    subject=subject, payload=dict(action=action, body=body,
                                                addressee=addressee, target=target))
    with lock(project.root / 'locks' / 'operator-records.lock'):
        # Existence is tested safely; absence only, never corruption, is a new write.
        # Both the sharded canonical file and a pre-partition flat legacy file
        # count as existence; get() fails closed on conflicting duplicates.
        canonical, legacy = shared._record_names(record_id)
        if (shared._read(canonical, 140000) is not None
                or (legacy != canonical and shared._read(legacy, 140000) is not None)):
            previous, _ = shared.get(record_id)
            envelope['created_at'] = previous['created_at']
        shared._validate(envelope)
        _route(project, envelope)
        if target is not None:
            _target(shared, envelope)
            for old, _ in shared.records(namespace='operator'):
                if (old['id'] != record_id and old['payload']['target'] is not None
                        and old['payload']['target']['id'] == target['id']):
                    raise Denied('Notice already closed by another event')
        computed = shared.put(envelope, writer=WRITER)
    return {'record': envelope, 'record_digest': computed}


def pending(project, role):
    with lock(project.root / "locks" / "operator-records.lock"):
        return _pending(project, role)


def _pending(project, role):
    """Bounded references across enabled kinds; receipts filter before selection.

    Full content is available only by digest-pinned manifest paging. Withdrawals
    remain audit-only; supersessions deliver replacement content. No retention deletion or source mutation.
    """
    policy = configuration(project.config.operator_records)
    empty = {'events': [], 'pending_count': 0, 'truncated': False, 'wake_pending': False}
    if not any(row['enabled'] for row in policy.values()):
        return empty, ()
    shared = store(project)
    closures = {}
    for envelope, _ in shared.records(namespace='operator'):
        if envelope['payload']['target'] is not None:
            _target(shared, envelope)
            target_id = envelope['payload']['target']['id']
            if target_id in closures and closures[target_id] != envelope['id']:
                raise Denied('Notice has conflicting closure events')
            closures[target_id] = envelope['id']
    closed = set(closures)
    count, wake_pending = 0, False
    def candidates():
        nonlocal count, wake_pending
        for envelope, computed in shared.records(namespace='operator'):
            if (not policy[envelope['kind']]['enabled'] or envelope['id'] in closed
                    or envelope['payload']['action'] == 'withdraw'):
                continue
            if envelope['payload']['addressee'] != role.name:
                continue
            _route(project, envelope)
            if not shared.received(role.name, envelope['id'], computed):
                count += 1
                wake_pending |= policy[envelope['kind']]['wake']
                yield envelope
    eligible = heapq.nsmallest(20, candidates(), key=lambda row:
                              (dt.datetime.fromisoformat(row['created_at']), row['id']))
    manifest = shared.freeze(role.name, [row['id'] for row in eligible])
    events = [dict(asdict(entry), kind=row['kind'], subject=row['subject'])
              for entry, row in zip(manifest, eligible)]
    return {'events': events, 'pending_count': count, 'truncated': count > len(events),
            'wake_pending': wake_pending}, manifest


def due(project, role_name=None):
    if role_name is not None:
        _role(project, role_name)
    names = [role_name] if role_name is not None else [
        name for name in project.roles if project.config.roles[name].workspace == 'write']
    return any(pending(project, project.config.roles[name])[0]['wake_pending'] for name in names)


def bind(context, state=None):
    context.operator_record_page, context.operator_record_manifest = (
        pending(context.project, context.role) if state is None else state)
    if context.operator_record_manifest and 'record_read' not in context.role.capabilities:
        context.role = replace(context.role, capabilities=(*context.role.capabilities, 'record_read'))


def validate(context, tokens):
    shared = store(context.project)
    allowed = shared._manifest(context.role.name, context.operator_record_manifest)
    for token in tokens:
        if token not in allowed:
            raise Denied('Record token is not in this unit manifest')
        entry = allowed[token]
        shared.get(entry.record_id, expected_digest=entry.record_digest)


def acknowledge(context, tokens):
    validate(context, tokens)
    shared = store(context.project)
    with context.project.insights._decision_receipt_lock(context.role.name):
        shared.acknowledge(context.role.name,
            [asdict(entry) for entry in context.operator_record_manifest if entry.ack_token in tokens],
            manifest=context.operator_record_manifest)


def read(context, args):
    return store(context.project).read_page(context.role.name, args['ack_token'],
        manifest=context.operator_record_manifest, offset=args.get('offset', 0), limit=args.get('limit', 8192))
