"""Request-scoped reuse for the read-only work-items command.

The first observation of each record is authoritative for this request, not
for later requests. This is a live, non-atomic read (as before); it never takes
writer locks, recovers files, persists a cache, or changes a writer's store.
Every snapshot passes normal full manifest validation once in this request.
Native reads and task-chain/receipt validation still run.
Exceptions are never cached. Cache eviction only costs another validated read.
"""
from collections import OrderedDict
from copy import copy, deepcopy
from functools import lru_cache

from .fs import canonical
from .work_items import _HISTORY_UNSET


class _Records:
    def __init__(self, read, budget, *, copy_values=True):
        self.read, self.budget, self.copy_values = read, budget, copy_values
        self.records = OrderedDict()
        self.size = 0

    def __call__(self, key):
        if key in self.records:
            value, size = self.records.pop(key)
            self.records[key] = (value, size)
            return deepcopy(value) if self.copy_values else value
        value = self.read(key)
        size = len(canonical(value))
        if size <= self.budget:
            while self.records and self.size + size > self.budget:
                _, (_, old_size) = self.records.popitem(last=False)
                self.size -= old_size
            self.records[key] = (deepcopy(value) if self.copy_values else value, size)
            self.size += size
        return value


def readonly_view(store):
    """Private read facade; discard after rendering one work-items response.

    The unchanged page/receipt readers own schema, limits, evidence and failure
    semantics. Serialized cached records are bounded to 48 MiB in total; parsed
    objects have normal Python overhead. Path checks are bounded to 8192 names.
    Do not use this facade for writes. All records remain scoped to this CLI
    invocation; there is no persistent validation cache or alternate reader.
    """
    view = copy(store)
    snapshots = copy(store.snapshots)
    snapshots._path_key = lru_cache(maxsize=8192)(snapshots._path_key)
    read_snapshot = snapshots.get
    # The chain validator only compares these private manifests; none are
    # returned to the caller or changed while constructing page rows.
    records = _Records(read_snapshot, 32 * 1024 * 1024, copy_values=False)

    def get(snapshot_id=None):
        # The mutable current pointer and invalid arguments always take the
        # original path; only explicit content-addressed IDs can be memoized.
        if not isinstance(snapshot_id, str):
            return read_snapshot(snapshot_id)
        return records(snapshot_id)

    snapshots.get = get
    view.snapshots = snapshots
    view._read = _Records(view._read, 8 * 1024 * 1024)
    read_item = view._get
    items = {}
    item_bytes = 0

    def get_item(item_id, *, seed_event=None, history_revisions=_HISTORY_UNSET):
        nonlocal item_bytes
        # Cache only callers supplying an explicit history observation and
        # seed. A changed observation must still replay the whole chain.
        if (not isinstance(item_id, str) or not isinstance(seed_event, dict)
                or not isinstance(history_revisions, (set, type(None)))):
            return read_item(item_id, seed_event=seed_event,
                             history_revisions=history_revisions)
        key = (item_id, canonical(seed_event),
               None if history_revisions is None else frozenset(history_revisions))
        if key not in items:
            value = read_item(item_id, seed_event=seed_event,
                              history_revisions=history_revisions)
            size = len(canonical(value)) + len(key[1])
            if size > 8 * 1024 * 1024:
                return value
            if len(items) >= 1024 or item_bytes + size > 8 * 1024 * 1024:
                items.clear()
                item_bytes = 0
            items[key] = value
            item_bytes += size
        return deepcopy(items[key])

    view._get = get_item
    return view
