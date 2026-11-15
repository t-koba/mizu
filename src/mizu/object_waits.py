"""Work-item object waits: exact subjects, history proofs and dependency edges."""
from contextvars import ContextVar
from contextlib import contextmanager

from .errors import Denied
from .fs import canonical, digest, identifier

# Per-call recursion guard, not retained state. Uncertain/corrupt graphs fail closed.
_reading = ContextVar('object_wait_targets', default=frozenset())


def normalize(wait):
    from .operator_records import _subject
    from .work_items import ITEM_ID
    if type(wait) is not dict or set(wait) != {'kind', 'subject'} or wait['kind'] != 'object_resolved':
        raise Denied('An object_resolved wait requires only a supported subject')
    subject = wait['subject']
    if type(subject) is not dict:
        raise Denied('Expected an object wait subject')
    kind = subject.get('type')
    if kind == 'record':
        _subject(subject)
    elif kind in ('insight', 'work_item') and set(subject) == {'type', 'id'}:
        identifier(subject['id'])
        if kind == 'work_item' and not ITEM_ID.fullmatch(subject['id']):
            raise Denied('Expected an exact work-item ID')
    else:
        raise Denied('Unsupported object wait subject')
    return {'kind': 'object_resolved', 'subject': dict(subject)}


def consolidate(wait):
    """Canonicalize new normalized insight/record registrations only."""
    if wait is None or wait['kind'] not in ('insight_decided', 'record_closed'):
        return wait
    result = normalize({'kind': 'object_resolved', 'subject': subject_of(wait)})
    if 'smoke_refs' in wait:
        result['smoke_refs'] = wait['smoke_refs']
    return result


def subject_of(wait):
    """Historical wait bytes are never rewritten on read/replay."""
    if wait['kind'] == 'object_resolved':
        return wait['subject']
    if wait['kind'] == 'insight_decided':
        return {'type': 'insight', 'id': wait['target']}
    if wait['kind'] == 'record_closed':
        return wait['subject']
    return None


@contextmanager
def reading(item_id):
    seen = _reading.get()
    if item_id in seen:
        raise Denied('Cyclic object wait proof')
    token = _reading.set(seen | {item_id})
    try:
        yield
    except RecursionError as exc:
        raise Denied('Object wait traversal unavailable') from exc
    finally:
        _reading.reset(token)


def target(store, item_id):
    """Validate the target's actual history, never trust an index projection."""
    with reading(item_id):
        return store._get(item_id)


def require_acyclic(store, waiter, subject):
    if subject['type'] != 'work_item':
        return
    seen = {waiter}
    current = subject['id']
    while True:
        if current in seen:
            raise Denied('Work-item waits cannot depend on themselves or form cycles')
        seen.add(current)
        item = target(store, current)  # Missing/unsafe traversal cannot authorize a new edge.
        if item['status'] != 'blocked' or not item.get('wait'):
            return
        next_subject = subject_of(store._stored_wait(item['wait']))
        if next_subject is None or next_subject['type'] != 'work_item':
            return
        current = next_subject['id']


def work_item_state(store, subject):
    from .work_items import _rows
    # A complete ledger plus no on-disk history establishes absence. A known
    # identity whose history cannot be replayed is unknown, not missing.
    with store._ledger.read('items', locked=True) as db:
        exists = _rows(db, 'SELECT id FROM items WHERE id=?', (subject['id'],))
    if not exists and store._history_revisions(subject['id'], recover=False) is None:
        return 'impossible', 'target_missing'
    item = target(store, subject['id'])
    return ('due', 'target_terminal') if item['status'] in ('done', 'dropped') else ('pending', 'target_active')


def _proof(record, event, rev, state):
    return {'event': event, 'digest': digest(canonical(record)), 'rev': rev, 'state': state}


def observe(store, wait, item):
    subject = subject_of(wait)
    if subject is None:
        return None
    from .work_item_waits import liveness
    kind = subject['type']
    if kind != 'record' and liveness(store, wait, None, item=item)['state'] != 'due':
        return None
    if kind == 'work_item':
        record = target(store, subject['id'])
        if record['status'] not in ('done', 'dropped'):
            return None
        proof = _proof(record, subject['id'], record['rev'], record['status'])
    elif kind == 'insight':
        record = store._optional_record(f"decisions/{subject['id']}.json")
        if record is None:
            return None  # Advisory liveness may race removal/unavailability.
        action, rev = store._validate_current_decision_shape(record, subject['id'])
        if action not in ('accept', 'modify', 'reject'):
            return None
        event = record.get('decision_key')
        if event is None:
            # Only immutable pre-key decisions need legacy discovery.
            matches = [path.stem for path in (store.root / 'decision-history').glob('*.json')
                       if store._read(f'decision-history/{path.name}') == record]
            if len(matches) != 1:
                raise Denied('Ambiguous legacy decision history')
            event = matches[0]
        proof = _proof(record, event, rev, action)
    else:
        if store.record_answer_evidence is None:
            return None
        proof = store.record_answer_evidence(subject, item['holder'], None)
        if proof is None:
            return None
    validate(store, wait, item['holder'], proof)
    return {'kind': 'due', 'evidence': proof}


def validate(store, wait, holder, proof):
    """Replay the selected immutable revision, not the current decision/status."""
    from .fs import DIGEST
    subject = subject_of(wait)
    if (subject is None or type(proof) is not dict
            or set(proof) != {'event', 'digest', 'rev', 'state'}
            or not isinstance(proof['digest'], str) or not DIGEST.fullmatch(proof['digest'])):
        raise Denied('Invalid object resolution proof')
    kind = subject['type']
    if kind == 'record':
        if store.record_answer_evidence is None:
            raise Denied('Answer proof reader unavailable')
        store.record_answer_evidence(subject, holder, proof)
        return
    rev, state = proof['rev'], proof['state']
    if type(rev) is not int or rev < 1:
        raise Denied('Invalid object resolution revision')
    identifier(proof['event'])
    if kind == 'insight':
        if state not in ('accept', 'modify', 'reject'):
            raise Denied('Object insight proof must be substantive')
        record = store._read(f"decision-history/{proof['event']}.json")
        action, revision = store._validate_current_decision_shape(record, subject['id'])
        if (action, revision) != (state, rev):
            raise Denied('Insight resolution revision/state mismatch')
        if record.get('decision_key', proof['event']) != proof['event']:
            raise Denied('Insight resolution event mismatch')
    else:
        if proof['event'] != subject['id'] or state not in ('done', 'dropped'):
            raise Denied('Work-item resolution identity/state mismatch')
        # Stop at the selected immutable revision: later dropped-to-done
        # corrections neither revoke nor replace the old terminal proof.
        from .work_items import MAX_REVISIONS
        record = None
        with reading(subject['id']):
            for candidate in store.history(subject['id']):
                if candidate['rev'] == rev:
                    record = candidate
                    break
        if rev == MAX_REVISIONS and state == 'dropped' and record and record['status'] != 'dropped':
            record = target(store, subject['id'])  # immutable retirement sidecar
        if record is None or (record['rev'], record['status']) != (rev, state):
            raise Denied('Work-item resolution revision/state mismatch')
    if digest(canonical(record)) != proof['digest']:
        raise Denied('Object resolution digest mismatch')
