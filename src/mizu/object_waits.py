"""Work-item waits on other work items: exact subjects, history proofs and dependency edges."""
from contextvars import ContextVar
from contextlib import contextmanager

from .errors import Denied
from .fs import canonical, digest, identifier

# Per-call recursion guard, not retained state. Uncertain/corrupt graphs fail closed.
_reading = ContextVar('object_wait_targets', default=frozenset())


def normalize(wait):
    from .work_items import ITEM_ID
    if type(wait) is not dict or set(wait) != {'kind', 'subject'} or wait['kind'] != 'object_resolved':
        raise Denied('An object_resolved wait requires only a work-item subject')
    subject = wait['subject']
    if (type(subject) is not dict or set(subject) != {'type', 'id'}
            or subject.get('type') != 'work_item'
            or not isinstance(subject.get('id'), str) or not ITEM_ID.fullmatch(subject['id'])):
        raise Denied('An object_resolved wait needs an exact work-item subject')
    return {'kind': 'object_resolved', 'subject': dict(subject)}


def subject_of(wait):
    """The work-item subject of an evaluated object wait, else None."""
    from .work_item_waits import evaluated
    return wait['subject'] if wait.get('kind') == 'object_resolved' and evaluated(wait) else None


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
    if liveness(store, wait, None, item=item)['state'] != 'due':
        return None
    record = target(store, subject['id'])
    if record['status'] not in ('done', 'dropped'):
        return None
    proof = _proof(record, subject['id'], record['rev'], record['status'])
    validate(store, wait, item['holder'], proof)
    return {'kind': 'due', 'evidence': proof}


def validate(store, wait, holder, proof):
    """Replay the selected immutable revision, not the current status."""
    from .fs import DIGEST
    subject = subject_of(wait)
    if (subject is None or type(proof) is not dict
            or set(proof) != {'event', 'digest', 'rev', 'state'}
            or not isinstance(proof['digest'], str) or not DIGEST.fullmatch(proof['digest'])):
        raise Denied('Invalid object resolution proof')
    rev, state = proof['rev'], proof['state']
    if type(rev) is not int or rev < 1:
        raise Denied('Invalid object resolution revision')
    identifier(proof['event'])
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
