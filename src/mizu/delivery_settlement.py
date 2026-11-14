"""Host-only, exact SharedRecords delivery settlement for actual executions.

One canonical bounded file per run lives outside disposable run directories.
Registration precedes dispatch; neither selectors nor readiness register. Only
live finalization records selections/effects, and only Context revocation plus
successful execution cleanup records closure. Recovery never manufactures either
fact from a result, seal, process observation or missing pointer. Uncertain files
remain roots; corrupt/over-budget inventories stop maintenance without truncation.
"""
from dataclasses import asdict
import re
import stat

from .errors import Denied, LimitExceeded
from .fs import ATOMIC_WRITE_TEMP_PREFIX, MAX_FRAME, atomic_write, canonical, digest, identifier, loads_finite, safe_read, sync_dir
from .shared_records import MANIFEST_ROWS, Delivery, SharedRecords

DIRECTORY = 'delivery-settlements'
FAMILIES = ('decision', 'operator', 'ci', 'proposal')
_RUN = re.compile(r'[0-9a-f]{32}\Z')
_FIELDS = {'schema', 'run', 'role', 'manifests', 'selected', 'effects', 'closed', 'finalized'}
_STATES = {'unresolved', 'succeeded', 'not-selected', 'check-join', 'bounded'}


def _path(root, run):
    if not isinstance(run, str) or not _RUN.fullmatch(run):
        raise Denied('Invalid delivery settlement run')
    return root / DIRECTORY / (run + '.json')


def _validate(row, run):
    if (type(row) is not dict or set(row) != _FIELDS or type(row['schema']) is not int
            or row['schema'] != 1 or row['run'] != run or not _RUN.fullmatch(run)
            or type(row['closed']) is not bool or type(row['finalized']) is not bool):
        raise Denied('Invalid delivery settlement binding')
    identifier(row['role'])
    for field in ('manifests', 'effects'):
        if type(row[field]) is not dict or set(row[field]) != set(FAMILIES):
            raise Denied('Invalid delivery settlement families')
    selected = row['selected']
    if selected is not None and (type(selected) is not dict or set(selected) != set(FAMILIES)):
        raise Denied('Invalid delivery settlement selection')
    if selected is None and row['finalized']:
        raise Denied('Delivery finalized without selections')
    for family in FAMILIES:
        entries = row['manifests'][family]
        if type(entries) is not list or len(entries) > MANIFEST_ROWS:
            raise Denied('Invalid delivery settlement manifest')
        try:
            manifest = tuple(Delivery(**entry) for entry in entries)
            allowed = SharedRecords._manifest(row['role'], manifest)
        except (TypeError, ValueError) as exc:
            raise Denied('Invalid delivery settlement manifest') from exc
        effects = row['effects'][family]
        if (type(effects) is not dict or set(effects) != set(allowed)
                or any(type(value) is not str or value not in _STATES for value in effects.values())):
            raise Denied('Invalid delivery settlement effects')
        if selected is None:
            if any(value != 'unresolved' for value in effects.values()):
                raise Denied('Delivery effect without selection boundary')
        else:
            tokens = selected[family]
            if (type(tokens) is not list or any(type(t) is not str for t in tokens)
                    or len(tokens) != len(set(tokens)) or not set(tokens) <= set(allowed)):
                raise Denied('Invalid delivery settlement tokens')
            for token, state in effects.items():
                if ((token not in tokens and state != 'not-selected')
                        or (token in tokens and state == 'not-selected')
                        or (state in ('check-join', 'bounded') and family not in ('ci', 'proposal'))):
                    raise Denied('Invalid delivery settlement disposition')
    if len(canonical(row)) > MAX_FRAME:
        raise LimitExceeded('Delivery settlement exceeds frame bound')
    return row


def _read(root, run):
    path = _path(root, run)
    raw = safe_read(root, path.relative_to(root).as_posix(), MAX_FRAME)
    return _decode(raw, run)


def _decode(raw, run):
    try:
        envelope = loads_finite(raw)
        if (type(envelope) is not dict or set(envelope) != {'record', 'digest'}
                or envelope['digest'] != digest(canonical(envelope['record']))):
            raise Denied('Delivery settlement digest mismatch')
        row = _validate(envelope['record'], run)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Denied('Corrupt delivery settlement') from exc
    if canonical(envelope) != raw:
        raise Denied('Noncanonical delivery settlement')
    return row


def _write(root, row, *, exclusive=False):
    _validate(row, row['run'])
    raw = canonical({'record': row, 'digest': digest(canonical(row))})
    if len(raw) > MAX_FRAME:
        raise LimitExceeded('Delivery settlement exceeds frame bound')
    path = _path(root, row['run'])
    # Persist the registry directory before publishing a pin in it.
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.parent.lstat().st_mode):
        raise Denied('Unsafe delivery settlement directory')
    if exclusive:
        sync_dir(root)
    atomic_write(path, raw, exclusive=exclusive)


def register(context):
    manifests = dict(zip(FAMILIES, (
        context.decision_manifest if context.shared_decisions else (),
        context.operator_record_manifest, context.ci_wait_manifest, context.proposal_wait_manifest)))
    if not any(manifests.values()):
        return
    for entries in manifests.values():
        SharedRecords._manifest(context.role.name, entries)
    row = {'schema': 1, 'run': context.run_dir.name, 'role': context.role.name,
           'manifests': {family: [asdict(e) for e in entries] for family, entries in manifests.items()},
           'selected': None,
           'effects': {family: {e.ack_token: 'unresolved' for e in entries}
                       for family, entries in manifests.items()},
           'closed': False, 'finalized': False}
    # Set before publication: even an ambiguous registration failure must not
    # erase a pin. No dispatch is permitted unless this write returns.
    context.delivery_settlement_run = context.run_dir.name
    _write(context.project.root, row, exclusive=True)


def _owned(context):
    run = getattr(context, 'delivery_settlement_run', None)
    if run is None:
        return None
    row = _read(context.project.root, run)
    if row['role'] != context.role.name or run != context.run_dir.name:
        raise Denied('Delivery settlement Context mismatch')
    return row


def begin(context, finished):
    row = _owned(context)
    if row is None:
        return
    if row['selected'] is not None or row['closed']:
        raise Denied('Delivery settlement already selected or closed')
    tokens = set(finished.get('ack_events', []))
    row['selected'] = {}
    for family in FAMILIES:
        effects = row['effects'][family]
        row['selected'][family] = sorted(tokens & effects.keys())
        for token in effects:
            if token not in tokens:
                effects[token] = 'not-selected'
            elif family in ('ci', 'proposal'):
                if finished.get('wait_for_check'):
                    effects[token] = 'check-join'
                elif finished.get('outcome') == 'bounded' or context.bound_hit:
                    effects[token] = 'bounded'
    _write(context.project.root, row)


def succeeded(context, family):
    """Call only after the existing family receipt operation returned normally.

    Exceptions (including a partially written batch) leave unresolved effects.
    In particular no receipt probing or source-current bypass is used on replay.
    """
    row = _owned(context)
    if row is None:
        return
    if row['selected'] is None or row['closed']:
        raise Denied('No open delivery settlement selection')
    changed = False
    for token in row['selected'][family]:
        if row['effects'][family][token] == 'unresolved':
            row['effects'][family][token] = 'succeeded'
            changed = True
    if changed:
        _write(context.project.root, row)


def finalized(context):
    row = _owned(context)
    if row is not None:
        if row['selected'] is None:
            raise Denied('No delivery settlement selection')
        row['finalized'] = True
        _write(context.project.root, row)


def terminal(row):
    return (row['closed'] and row['finalized'] and row['selected'] is not None
            and all(state != 'unresolved' for effects in row['effects'].values()
                    for state in effects.values()))


def cleanup(root, run):
    """Exact idempotent replay after the durable terminal boundary only."""
    path = _path(root, run)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    row = _read(root, run)
    if terminal(row):
        # A prior writer may have died/errored after rename but before its
        # directory flush. Re-establish that boundary before releasing the pin.
        sync_dir(path.parent)
        path.unlink()
        sync_dir(path.parent)


def closed(context):
    """Caller revoked Context execution under its mutex and closed exec."""
    with context.mutex:
        if (not context.closed or context.exec_session is not None
                or context.operation_active.is_set() or not context.joins_idle.is_set()):
            raise Denied('Delivery settlement requires closed Context execution')
    row = _owned(context)
    if row is not None:
        row['closed'] = True
        _write(context.project.root, row)
        cleanup(context.project.root, row['run'])


def inventory(root, scan):
    """Use maintenance's existing aggregate entry/byte budgets, never truncate.

    All records, including corrupt records and terminal cleanup leftovers, are
    inspected before maintenance acts. Invalid data stops it globally, including
    already-committed quarantine deletion. Backup retains these raw bytes.
    """
    pins, settled, temporary = set(), [], []
    for path in scan.children(root / DIRECTORY):
        raw = scan.raw(path)
        if path.name.startswith(ATOMIC_WRITE_TEMP_PREFIX):
            temporary.append(path.name)
            continue
        if path.suffix != '.json' or not _RUN.fullmatch(path.stem):
            raise Denied('Unknown delivery settlement registry entry')
        row = _decode(raw, path.stem)
        if terminal(row):
            settled.append(row['run'])
        else:
            pins.add(row['run'])
    return pins, settled, temporary


def backup_preflight(root, scan):
    """Refuse backups that would omit a trusted settlement reference.

    Corrupt settlement bytes with safe filenames remain opaque backup evidence;
    valid rows must retain their exact run, source records and success receipts.
    The caller supplies its aggregate bounded scan to keep backup work governed
    by storage's existing limits.
    """
    from . import shared_records

    source_store = shared_records.SharedRecords(root)
    source_digests = {}
    verified_receipts = set()
    for path in scan.children(root / DIRECTORY):
        raw = scan.raw(path)
        if path.name.startswith(ATOMIC_WRITE_TEMP_PREFIX):
            continue
        if path.suffix != ".json" or not _RUN.fullmatch(path.stem):
            raise Denied("Backup refused: unknown delivery settlement member")
        target = root / "runs" / path.stem
        try:
            target_info = target.lstat()
        except FileNotFoundError as exc:
            raise Denied("Backup refused: missing settlement run evidence") from exc
        if (not stat.S_ISDIR(target_info.st_mode)
                or getattr(target_info, "st_file_attributes", 0) & 0x400):
            raise Denied("Backup refused: unsafe settlement run evidence")
        try:
            row = _decode(raw, path.stem)
        except (Denied, LimitExceeded):
            # Corrupt bytes are still backed up for repair; they cannot
            # authorize a claim about which source records are required.
            continue
        for family, entries in row["manifests"].items():
            for entry in entries:
                record_id, expected = entry["record_id"], entry["record_digest"]
                if record_id in source_digests:
                    if source_digests[record_id] != expected:
                        raise Denied("Backup refused: conflicting settlement source digests")
                else:
                    source_digests[record_id] = expected
                    canonical_name, legacy_name = source_store._record_names(record_id)
                    canonical_raw = source_store._read(canonical_name, shared_records.RECORD_BYTES)
                    legacy_raw = (None if legacy_name == canonical_name else
                                  source_store._read(legacy_name, shared_records.RECORD_BYTES))
                    if (canonical_raw is not None and legacy_raw is not None
                            and canonical_raw != legacy_raw):
                        raise Denied("Backup refused: conflicting settlement source records")
                    source_raw = canonical_raw if canonical_raw is not None else legacy_raw
                    if source_raw is None or digest(source_raw) != expected:
                        raise Denied("Backup refused: missing or changed settlement source")
                if row["effects"][family][entry["ack_token"]] != "succeeded":
                    continue
                receipt_key = (entry["role"], record_id, expected)
                if receipt_key in verified_receipts:
                    continue
                verified_receipts.add(receipt_key)
                receipt_raw = source_store._read(
                    source_store._receipt_name(entry["role"], record_id), shared_records.RECEIPT_BYTES)
                if receipt_raw is None:
                    raise Denied("Backup refused: missing settlement success receipt")
                try:
                    receipt = shared_records.loads_finite(receipt_raw)
                except (ValueError, TypeError, UnicodeError) as exc:
                    raise Denied("Backup refused: corrupt settlement success receipt") from exc
                if (type(receipt) is not dict
                        or set(receipt) != {"role", "record_id", "record_digest", "acknowledged_at"}
                        or receipt["role"] != entry["role"]
                        or receipt["record_id"] != record_id
                        or receipt["record_digest"] != expected):
                    raise Denied("Backup refused: mismatched settlement success receipt")
                shared_records._time(receipt["acknowledged_at"])
                if shared_records._bytes(receipt, shared_records.RECEIPT_BYTES) != receipt_raw:
                    raise Denied("Backup refused: noncanonical settlement success receipt")

def recover(root, settled, temporary):
    """Under maintenance exclusions, replay terminal cleanup and abandon temps.

    atomic_write renames, never links, its temporary. A leftover .new- file is
    not a committed registration or update; it cannot authorize dispatch. Its
    bytes/shape were charged and checked by inventory before any cleanup.
    """
    for run in settled:
        cleanup(root, run)
    for name in temporary:
        (root / DIRECTORY / name).unlink()
    if temporary:
        sync_dir(root / DIRECTORY)
