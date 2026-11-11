"""Host-only wait reconciliation; reads never call this lifecycle writer."""
from __future__ import annotations

import datetime as dt

from .errors import Denied, LimitExceeded
from .fs import DIGEST, canonical, digest
from .snapshot import SnapshotNotDescendant


def _digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def observe(store, item, *, receipt_cache=None):
    """Return exact local proof, or no resolution; never request CI.

    Candidate and receipt discovery reuse the existing bounded CI mechanisms.
    Ancestry checkers live only for this observation; manifests and exact receipts
    are still validated per candidate. Discovery is advisory; transition repeats
    it uncached under the publication lock.
    """
    wait = store._stored_wait(item['wait'])
    if wait['kind'] == 'code_change':
        snapshot = store.snapshots.get()
        if snapshot['code_digest'] == wait['digest']:
            return None
        return {'kind': 'due', 'evidence': {
            'snapshot_id': snapshot['id'], 'code_digest': snapshot['code_digest']}}
    if wait['kind'] != 'publication_ci' or not item.get('delivery'):
        return None
    from .ci_waits import _candidates, _resolved
    from .publication import Publication
    wait_distance = store.snapshots._descendant_distance_from(wait['snapshot_id'])
    delivery_distance = store.snapshots._descendant_distance_from(item['delivery']['snapshot_id'])
    for sid, candidate in _candidates(store, item, True, newest_first=True,
                                     wait_distance=wait_distance):
        if candidate is None:
            continue
        try:
            delivery_distance(sid)
        except SnapshotNotDescendant:
            # Discovery proves wait ancestry, not delivery ancestry. Skip only
            # proven negatives; uncertain delivery evidence must stop resolution.
            continue
        snapshot = store.snapshots.get(sid)
        if snapshot['code_digest'] in (wait['code_digest'], item['delivery']['code_digest']):
            continue
        evidence = {'snapshot_id': sid, 'code_digest': snapshot['code_digest']}
        resolved = _resolved(store, wait, evidence, receipt_cache=receipt_cache,
                             wait_distance=wait_distance)
        if resolved is not None:
            receipt = Publication.read_receipt(store.root, resolved['publication_id'])
            return {'kind': 'superseded', 'evidence': {
                **evidence, 'publication_id': receipt['publication_id'],
                'receipt_digest': digest(canonical(receipt))}}
    return None


def validate(store, previous, record):
    """Replay immutable proof, not today's rolling discovery/uniqueness view."""
    value = record['wait_resolution']
    wait = previous.get('wait')
    if (not isinstance(value, dict)
            or set(value) != {'wait_rev', 'wait_digest', 'kind', 'evidence', 'resolved_at'}
            or previous['status'] != 'blocked' or record['status'] != 'open'
            or previous['holder'] is None or record['actor'] != previous['holder']
            or record['holder'] != previous['holder']
            or type(value['wait_rev']) is not int or value['wait_rev'] != previous['rev']
            or value['wait_digest'] != digest(canonical(wait))
            or value['resolved_at'] != record['updated_at']
            or any(record.get(key) != previous.get(key) for key in
                   ('scope', 'delivery', 'smoke_refs'))):
        raise Denied('Invalid work-item wait resolution binding')
    # The writer uses this exact UTC seconds format, bounding the new field.
    stamp = value['resolved_at']
    try:
        valid_stamp = (isinstance(stamp, str) and len(stamp) == 25
                       and dt.datetime.fromisoformat(stamp).isoformat(timespec='seconds') == stamp
                       and stamp.endswith('+00:00'))
    except ValueError:
        valid_stamp = False
    if not valid_stamp:
        raise Denied('Invalid wait resolution time')
    wait = store._stored_wait(wait)
    evidence = value['evidence']
    publication = wait['kind'] == 'publication_ci'
    fields = {'snapshot_id', 'code_digest'} | ({'publication_id', 'receipt_digest'} if publication else set())
    if (not isinstance(evidence, dict) or set(evidence) != fields
            or not all(_digest(v) for v in evidence.values())):
        raise Denied('Invalid wait resolution evidence')
    snapshot = store.snapshots.binding(evidence['snapshot_id'])
    if snapshot['code_digest'] != evidence['code_digest']:
        raise Denied('Wait resolution snapshot digest mismatch')
    if wait['kind'] == 'code_change':
        if value['kind'] != 'due' or snapshot['code_digest'] == wait['digest']:
            raise Denied('Wait resolution does not prove code change')
    elif publication:
        delivery = previous.get('delivery')
        if (value['kind'] != 'superseded' or not delivery
                or snapshot['code_digest'] in (wait['code_digest'], delivery['code_digest'])):
            raise Denied('Wait resolution does not supersede delivery')
        store.snapshots.require_descendant(delivery['snapshot_id'], snapshot['id'])
        store.snapshots.require_descendant(wait['snapshot_id'], snapshot['id'])
        from .publication import Publication
        receipt = Publication.read_receipt(store.root, evidence['publication_id'])
        if (receipt['branch'] != wait['branch'] or receipt['code_digest'] != snapshot['code_digest']
                or digest(canonical(receipt)) != evidence['receipt_digest']):
            raise Denied('Wait resolution publication mismatch')
    else:
        raise Denied('Unsupported automatic wait resolution')


# Intrinsic canonical member growth, including its leading comma. The exact
# maximum shape (64-byte hex identities, three-digit rev, UTC seconds) is
# checked in test_work_item_resolution; no general record budget is raised.
MAX_RESOLUTION_BYTES = 521


def reconcile(project, role_name, *, limit, on_resolution=None):
    """Capture the open/order projection, resolving proven waits in that scan.

    The private transform runs only on validated active records, outside read
    locks. Public read APIs never supply it. Pending/unknown evidence consumes
    no transition budget; each append repeats proof under publication/record
    locks and CAS. ``on_resolution`` is an in-process scheduling hint called
    after each durable reopening; it does not alter proof or authority. No extra
    ledger scan, candidate list, cursor or assignment.
    """
    policy = project.settings.get('work_item_wait_resolution', 'reopen')
    if policy == 'manual':
        return project.work_items.page_open_for_order(limit=limit)
    if policy != 'reopen':
        raise Denied('Invalid wait resolution policy')
    from .work_items import MAX_REVISIONS, MAX_OPEN_PAGE_ROWS
    if type(limit) is not int or not 1 <= limit <= MAX_OPEN_PAGE_ROWS:
        raise Denied('Invalid wait reconciliation limit')
    store = project.work_items
    resolved = 0
    receipt_cache = {}  # Call-local advisory reuse, capped by snapshot history_index.

    def resolve(item):
        nonlocal resolved
        if (resolved == limit or item['status'] != 'blocked'
                or item['holder'] != role_name or item['rev'] == MAX_REVISIONS
                or (item.get('wait') or {}).get('kind') not in ('code_change', 'publication_ci')):
            return item
        try:
            proof = observe(store, item, receipt_cache=receipt_cache)
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError):
            # Missing/corrupt/ambiguous evidence confers no transition authority.
            return item
        if proof is None:
            return item
        try:
            opened = store.transition(
                item['id'], expected_rev=item['rev'], status='open', actor=role_name,
                reason='Automatically resolved recorded wait', _resolve_wait=True)
        except (Denied, LimitExceeded, OSError, ValueError, TypeError, KeyError):
            # CAS refusal or an uncertain write may follow a competing append;
            # use a fresh validated projection, never the scanned stale record.
            return store.get(item['id'])
        resolved += 1
        if on_resolution is not None:
            on_resolution()
        return opened

    return store._page_open_impl(cursor=None, limit=limit, readonly=True,
                                 order_fingerprint=True, wait_resolution_role=role_name,
                                 _active_transform=resolve)
