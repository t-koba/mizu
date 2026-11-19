"""Operator CLI records; immutable content and closure events in SharedRecords.

The host CLI is the authenticated operator channel (as for insight submit).
There is no model writer. Runtime readers receive only a frozen manifest.
Policy controls enablement, routing and permanent retention. Pending records
are also the scheduler signal; no second queue or mutable delivery cursor exists.
"""
from dataclasses import asdict, replace
import datetime as dt
import heapq

from .errors import Busy, ConfigError, Denied, LimitExceeded
from .fs import DIGEST, identifier, lock, now
from .shared_records import (RECORD_NAME_MAX_CHARS, KindPolicy, ScanProgress,
                             SharedRecords, _name)
from .operator_record_index import catalog, remember

WRITER = 'operator.cli'
ROUTES = {'answer': 'question_source', 'operator.notice': 'addressee'}
MAX_OPERATOR_RECORD_BODY_CHARS = 32768


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
    if type(subject) is dict and subject.get('type') == 'record':
        if (set(subject) != {'type', 'kind', 'id', 'digest'}
                or not isinstance(subject['digest'], str) or not DIGEST.fullmatch(subject['digest'])):
            raise Denied('Expected an exact record kind, ID and digest')
        _name(subject['kind'], kind=True)
        _name(subject['id'])
        return
    if (type(subject) is not dict or set(subject) != {'type', 'id', 'rev'} or subject['type'] != 'insight'
            or type(subject['rev']) is not int or subject['rev'] < 1):
        raise Denied('Expected an exact insight subject and revision')
    identifier(subject['id'])


def _validate(kind, subject, payload):
    if subject:
        _subject(subject)
        if subject['type'] == 'record' and kind != 'answer':
            raise Denied('Only answers may address role questions')
    if kind == 'answer' and not subject:
        raise Denied('Answer requires an exact question subject')
    if set(payload) != {'action', 'body', 'addressee', 'target'}:
        raise Denied('Invalid operator record payload')
    identifier(payload['addressee'])
    body, action, target = payload['body'], payload['action'], payload['target']
    if not isinstance(body, str) or len(body) > MAX_OPERATOR_RECORD_BODY_CHARS:
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
          or not isinstance(target['id'], str) or len(target['id']) > RECORD_NAME_MAX_CHARS):
        raise Denied('Closure requires an exact record and digest')


def store(project):
    return SharedRecords(project.insights.root, {
        kind: KindPolicy(frozenset({WRITER}),
                         lambda subject, payload, kind=kind: _validate(kind, subject, payload),
                         maximum=140000) for kind in ROUTES})


def _question(project, subject):
    _subject(subject)
    if subject['type'] != 'insight':
        raise Denied('Expected an insight subject')
    current = project.insights.read(subject['id'])
    if current.get('id') != subject['id']:
        raise Denied('Subject must use the full exact insight ID')
    if current.get('rev', 1) == subject['rev']:
        return current
    item = project.insights.read_revision(subject['id'], subject['rev'])
    if item is None:
        raise Denied('Question revision is unavailable or ambiguous')
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


def _record_question(project, subject):
    """Resolve only a declared route and its authenticated immutable writer."""
    from .host_records import configuration, store
    _subject(subject)
    policies = configuration(project.config.record_kinds)
    policy = policies.get(subject['kind'])
    if policy is None or policy['route'] != 'operator_question':
        raise Denied('Role question route is undeclared')
    shared = store(project.root, project.snapshots, policies)
    envelope, _ = shared.get(subject['id'], expected_digest=subject['digest'])
    return _question_writer(project, envelope, subject['kind'], policy)


def _question_writer(project, envelope, kind, policy):
    role = _role(project, envelope['writer'][5:])
    prefix = f"role/{role}/{kind}/"
    if (envelope['kind'] != kind or envelope['writer'] != policy['writer']
            or not envelope['id'].startswith(prefix) or not envelope['id'][len(prefix):]):
        raise Denied('Role question identity differs from declaration')
    return role


def _asking(project, subject):
    _subject(subject)
    if subject['type'] == 'record':
        return _record_question(project, subject)
    return _question(project, subject).get('source')


def questions(project, *, limit, scan_limit):
    """Bounded host-only view; no acknowledgements, queue, index or cursor.

    The caller supplies positive per-request row and scan bounds. A full scan
    budget is reported as incomplete without a lookahead read, even if it
    happened to reach EOF; directory enumeration and record reads stay within
    that requested budget. Closure validation uses a separate catalog budget
    of the same size; exhaustion refuses the view rather than guessing open.
    """
    from .host_records import configuration, store as role_store
    if (type(limit) is not int or limit < 1
            or type(scan_limit) is not int or scan_limit < 1):
        raise Denied('Question listing bounds must be positive integers')
    policies = configuration(project.config.record_kinds)
    if not any(row['route'] == 'operator_question' for row in policies.values()):
        return {'questions': [], 'scanned': 0, 'truncated': False}
    shared = role_store(project.root, project.snapshots, policies)
    rows, scanned, truncated = [], 0, False
    scan_progress = ScanProgress()
    records = shared.records(namespace='role', scan_limit=scan_limit,
                             scan_progress=scan_progress)
    try:
        for envelope, computed in records:
            scanned += 1
            if policies[envelope['kind']]['route'] != 'operator_question':
                continue
            subject = dict(type='record', kind=envelope['kind'], id=envelope['id'], digest=computed)
            _question_writer(project, envelope, envelope['kind'], policies[envelope['kind']])
            if len(rows) == limit:
                truncated = True
                break
            rows.append({'subject': subject, 'record': envelope,
                         'record_digest': computed, 'closed': False})
    finally:
        records.close()
    if rows:
        # Canonical answers are the closure events, not receipts. Validate the
        # existing compact authority once, read-only and within a caller bound.
        with lock(project.root / 'locks' / 'operator-records.lock'):
            indexed = catalog(store(project), readonly=True, scan_limit=scan_limit)
        by_subject = {(row['subject']['kind'], row['subject']['id'], row['subject']['digest']): row
                      for row in rows}
        for answer, _ in indexed.values():
            subject = answer['subject']
            if answer['kind'] != 'answer' or subject['type'] != 'record':
                continue
            row = by_subject.get((subject['kind'], subject['id'], subject['digest']))
            if row is not None:
                if answer['payload']['addressee'] != row['record']['writer'][5:]:
                    raise Denied('Answer must address the authenticated asking role')
                row['closed'] = True
    return {'questions': rows, 'scanned': scanned,
            'truncated': truncated or scan_progress.entries_examined == scan_limit}


def _route(project, envelope):
    role = _role(project, envelope['payload']['addressee'])
    if envelope['kind'] == 'answer':
        if _asking(project, envelope['subject']) != role:
            raise Denied('Answer must address the authenticated asking role')
    return role


def _target(envelope, indexed):
    target = envelope['payload']['target']
    found = indexed.get(target['id'])
    if found is None or found[1] != target['digest']:
        raise Denied('Closure target is unavailable or changed')
    old = found[0]
    if (old['kind'] != 'operator.notice' or old['writer'] != envelope['writer']
            or old['payload']['action'] == 'withdraw' or old['id'] == envelope['id']
            or old['payload']['addressee'] != envelope['payload']['addressee']):
        raise Denied('Only the notice writer may close an exact notice')
    return old


def _project_notice(project, envelope, computed):
    """Best-effort derived mirror; a projection failure never changes a write."""
    shadow = getattr(getattr(project, "insights", None), "kernel_shadow", None)
    if shadow is None:
        return
    try:
        shadow.noticed(envelope, computed)
    except Exception as exc:
        shadow.note_failure("operator.notice", envelope["id"], exc)


def _project_answer(project, envelope, computed):
    """Best-effort derived mirror; a projection failure never changes a write."""
    shadow = getattr(getattr(project, "insights", None), "kernel_shadow", None)
    if shadow is None:
        return
    try:
        subject = envelope["subject"]
        insight = _question(project, subject) if subject.get("type") == "insight" else None
        shadow.answered(envelope, insight)
    except Exception as exc:
        shadow.note_failure("answer", envelope["id"], exc)


def insight_answers(project, insight_id):
    """Return full answer envelopes addressed to one exact insight (read-only).

    The scan uses the configured operator-record catalog bound and never
    repairs legacy metadata or consults kernel state.
    """
    shared = store(project)
    return [(envelope, computed)
            for envelope, computed in shared.records(
                namespace='operator',
                scan_limit=project.config.limits.operator_record_catalog_scan_entries)
            if envelope['kind'] == 'answer'
            and envelope['subject'].get('type') == 'insight'
            and envelope['subject'].get('id') == insight_id]


def reproject_answers(project, insight_id):
    """Replay one insight's answer projections through the contained mirror.

    Returns the replayed envelopes so a caller can verify the derived objects
    observationally. A failed answer stays a bounded diagnostic and does not
    stop the other replays.
    """
    answers = insight_answers(project, insight_id)
    for envelope, computed in answers:
        _project_answer(project, envelope, computed)
    return answers


def write(project, *, event_id, kind, body='', addressee=None, subject=None,
          action='publish', target=None):
    """Trusted CLI only. Stable event ID is required for uncertain-write replay.

    No writer parameter: identity is bound by this host entry point. A retry
    reuses the retained timestamp and must match all immutable content exactly.
    All records/closures serialize under one host lock; retries repair missing
    derived metadata, never canonical envelopes.
    """
    identifier(event_id)
    policy = configuration(project.config.operator_records)
    if not isinstance(kind, str) or kind not in policy or not policy[kind]['enabled']:
        raise Denied('Operator record kind is disabled or undeclared')
    shared = store(project)
    record_id = f'operator/{project.name}/{event_id}'
    subject = {} if subject is None else subject
    if kind == 'answer':
        asking = _role(project, _asking(project, subject))
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
        if shared._read(shared.record_name(record_id), 140000) is not None:
            previous, _ = shared.get(record_id)
            envelope['created_at'] = previous['created_at']
        shared._validate(envelope)
        _route(project, envelope)
        if target is not None:
            indexed = catalog(shared)
            _target(envelope, indexed)
            for old, _ in indexed.values():
                if (old['id'] != record_id and old['payload']['target'] is not None
                        and old['payload']['target']['id'] == target['id']):
                    raise Denied('Notice already closed by another event')
        computed = shared.put(envelope, writer=WRITER)
        remember(shared, envelope)  # canonical-first; also repairs exact retries
        if kind == 'operator.notice':
            _project_notice(project, envelope, computed)
        elif kind == 'answer':
            _project_answer(project, envelope, computed)
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
    indexed = catalog(shared)
    closures = {}
    for envelope, _ in indexed.values():
        if envelope['payload']['target'] is not None:
            _target(envelope, indexed)
            target_id = envelope['payload']['target']['id']
            if target_id in closures and closures[target_id] != envelope['id']:
                raise Denied('Notice has conflicting closure events')
            closures[target_id] = envelope['id']
    closed = set(closures)
    count, wake_pending = 0, False
    def candidates():
        nonlocal count, wake_pending
        for envelope, computed in indexed.values():
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


def register_question_wait(project, subject, holder):
    """Registration authority only; closure observation never gates on enablement."""
    if not configuration(project.config.operator_records)['answer']['enabled']:
        raise Denied('Record waits require the operator answer route')
    if _record_question(project, subject) != holder:
        raise Denied('Record question writer must be the current work-item holder')


def record_question_lookup(project):
    """Lazy bounded, read-only closure evidence, cached once per projection.

    None means unavailable/unsafe, False means a complete catalog has no exact
    answer, True means at least one authenticated answer. No receipt is selected.
    Publishing policy does not revoke an already authenticated answer.
    """
    answers = None
    loaded = False
    questions = {}

    def closed(subject):
        nonlocal answers, loaded
        try:
            _subject(subject)
            if subject['type'] != 'record':
                raise Denied('Expected an exact record question')
            key = (subject['kind'], subject['id'], subject['digest'])
            if key not in questions:
                questions[key] = None
                questions[key] = _record_question(project, subject)
            if questions[key] is None:
                return None
            if not loaded:
                loaded = True  # A failed scan is not retried within this projection.
                with lock(project.root / 'locks' / 'operator-records.lock'):
                    indexed = catalog(store(project), readonly=True,
                        scan_limit=project.config.limits.operator_record_catalog_scan_entries)
                found = {}
                for envelope, _ in indexed.values():
                    target = envelope['subject']
                    if envelope['kind'] == 'answer' and target['type'] == 'record':
                        subject_key = (target['kind'], target['id'], target['digest'])
                        found.setdefault(subject_key, set()).add(envelope['payload']['addressee'])
                answers = found
            if answers is None:
                return None
            recipients = answers.get(key, set())
            if any(recipient != questions[key] for recipient in recipients):
                return None
            return bool(recipients)
        except (Busy, ConfigError, Denied, LimitExceeded, OSError, ValueError,
                TypeError, AttributeError, KeyError):
            return None

    return closed


def record_answer_evidence(project, subject, holder, proof=None):
    """Select or replay one exact authenticated answer; never a rolling catalog on replay.

    Registration already authenticated the immutable question and its writer.
    Replay needs only the permanent answer route, independent of today's enablement
    and role-kind declarations. No new records or acknowledgements are written.
    """
    from .fs import canonical, digest
    shared = store(project)
    if proof is None:
        if _record_question(project, subject) != holder:
            raise Denied('Answer resolution question writer mismatch')
        with lock(project.root / 'locks' / 'operator-records.lock'):
            indexed = catalog(shared, readonly=True,
                              scan_limit=project.config.limits.operator_record_catalog_scan_entries)
        candidates = [(envelope, computed) for envelope, computed in indexed.values()
                      if envelope['kind'] == 'answer' and envelope['subject'] == subject]
        if any(envelope['payload']['addressee'] != holder for envelope, _ in candidates):
            raise Denied('Answer resolution route mismatch')
        if not candidates:
            return None
        envelope, computed = min(candidates, key=lambda row: row[0]['id'])
        return {'event': envelope['id'], 'digest': computed or digest(canonical(envelope)),
                'rev': subject['digest'], 'state': 'answered'}
    if proof['rev'] != subject['digest'] or proof['state'] != 'answered':
        raise Denied('Answer resolution subject/state mismatch')
    parts = proof['event'].split('/') if isinstance(proof['event'], str) else []
    if len(parts) != 3 or parts[:2] != ['operator', project.name]:
        raise Denied('Answer resolution event identity mismatch')
    identifier(parts[2])
    envelope, _ = shared.get(proof['event'], expected_digest=proof['digest'])
    if (envelope['kind'] != 'answer' or envelope['subject'] != subject
            or envelope['writer'] != WRITER or envelope['payload']['addressee'] != holder):
        raise Denied('Answer resolution route mismatch')
    return proof


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
