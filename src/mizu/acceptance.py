"""Retained acceptance evidence, not current-workspace acceptance or delivery.

Objects and first-writer indexes are immutable. An index selects one exact
receipt, never a mutable latest verdict. No command output is retained here.
Only the two acceptance producers supply finalized, input-bound results.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import DIGEST, atomic_write, canonical, digest, lock, safe_read

MAX_BYTES = 1048576
_BASE = 'evidence/acceptance'


def _digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


# Only safely read cache content can cross this boundary. Store/path failures
# and durable proof validation deliberately remain ordinary Denied/OSError.
class InvalidProducer(Denied):
    CODES = frozenset({'job_json', 'job_schema', 'job_scope',
                      'job_integrity', 'result_json', 'result_integrity',
                      'job_file_bytes', 'result_file_bytes', 'job_subscription'})

    def __init__(self, job_id, code):
        if not _digest(job_id) or code not in self.CODES:
            raise Denied('Invalid producer diagnostic')
        self.job_id, self.code = job_id, code
        super().__init__(f'Invalid acceptance producer {job_id}: {code}')


def producer_diagnostics(invalid):
    """Bound the public view, not the internal exclusion/deduplication set."""
    if not invalid:
        return {}
    entries = []
    for key in sorted(invalid, key=str)[:32]:
        identity = {'entry_digest': key[1]} if isinstance(key, tuple) else {'job_id': key}
        entries.append({**identity, 'code': invalid[key]})
    return {'invalid_producers': {'entries': entries, 'total': len(invalid),
                                  'omitted': len(invalid) - len(entries),
                                  'coverage_complete': False}}


def _producer_read(project, job_id, name):
    # safe_read failures are NOT content failures. In particular, never catch
    # Denied here: it includes unsafe links, changing files and shared paths.
    raw = safe_read(project.root, f'check-jobs/{job_id}/{name}.json', project.config.limits.file_bytes)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidProducer(job_id, name + '_json') from exc


def read_job(project, job_id):
    if not _digest(job_id):
        raise Denied('Invalid acceptance job ID')
    job = _producer_read(project, job_id, 'job')
    # Validate every field consumed by sorting, filtering, summaries and the
    # one-hop subscription overlay. Missing legacy fields retain their defaults.
    strings = ('role', 'run', 'kind', 'status', 'created_at', 'finished_at', 'error',
               'purpose')
    identities = ('code_digest', 'execution', 'environment', 'cache_input', 'shared_key',
                  'shared_job', 'replaced_by', 'acceptance_receipt_id')
    flags = ('acknowledged', 'detached', 'passed')
    if (not isinstance(job, dict) or job.get('id') != job_id
            or any(k in job and not isinstance(job[k], str) for k in strings)
            or any(k in job and job[k] is not None and not _digest(job[k]) for k in identities)
            or any(k in job and type(job[k]) is not bool for k in flags)
            or not isinstance(job.get('commands', []), list)
            or len(job.get('commands', [])) > 32
            or any(not isinstance(c, str) for c in job.get('commands', []))):
        raise InvalidProducer(job_id, 'job_schema')
    scratch = job.get('scratch_paths', [])
    if (not isinstance(scratch, list) or len(scratch) > 32
            or any(not isinstance(name, str) or len(name) > 255
                   or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in scratch)
            or len(set(scratch)) != len(scratch)
            or scratch and (job.get('kind') != 'experiment' or job.get('shared_job')
                            or job.get('shared_key') is not None or job.get('acceptance_scope') is not None)):
        raise InvalidProducer(job_id, 'job_schema')
    try:
        canonical(job)  # Reject non-finite numbers and unencodable JSON strings.
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidProducer(job_id, 'job_schema') from exc
    return job


def job_paths(project, invalid=None):
    """Enumerate under check-jobs.lock; unsafe paths and I/O always refuse.

    A non-producer name is classified only after safely opening its ordinary
    job leaf. The digest is domain-separated and never a usable job identity.
    Callers without a diagnostic sink retain strict refusal.
    """
    base = project.root / 'check-jobs'
    if not _present(project.root, 'check-jobs'):
        return []
    if not stat.S_ISDIR(base.lstat().st_mode):
        raise Denied('Unsafe acceptance producer directory')
    paths = []
    for child in sorted(base.iterdir()):
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise Denied('Unsafe acceptance producer directory')
        if stat.S_ISDIR(info.st_mode):
            relative = f'check-jobs/{child.name}/job.json'
            if not _present(project.root, relative):
                continue
            if _digest(child.name):
                paths.append(child / 'job.json')
            else:
                # Even oversized leaves must first pass safe_read's path/link
                # checks. Only its size disposition is content-only.
                try:
                    safe_read(project.root, relative, project.config.limits.file_bytes)
                except LimitExceeded:
                    pass
                if invalid is None:
                    raise Denied('Invalid acceptance producer entry')
                key = digest(b'mizu.check-job-entry.v1\0' + os.fsencode(child.name))
                invalid[('entry', key)] = 'non_producer_entry'
    return paths


def _inspect_evidence(project, job):
    """Shared pure inspection for discovery/retention; never mint or repair proof."""
    job_id = job['id']
    bound = job.get('acceptance_scope')
    if bound is None:
        return None, None
    if (not isinstance(bound, dict) or job.get('shared_job') or job.get('kind') != 'verify'
            or job.get('status') != 'completed' or job.get('passed') is not True
            or _present(project.root, f'check-jobs/{job_id}/cancel.json')
            or any(bound.get(k) != job.get(k) for k in ('commands', 'execution', 'environment', 'cache_input'))):
        raise InvalidProducer(job_id, 'job_integrity')
    try:
        _scope(bound)
    except (Denied, ValueError, UnicodeError) as exc:
        raise InvalidProducer(job_id, 'job_scope') from exc
    existing = job.get('acceptance_receipt_id')
    if existing is not None:
        receipt = get(project.root, existing)  # Durable-store corruption stays fatal.
        if receipt['code_digest'] != job.get('code_digest') or receipt['scope'] != bound:
            raise InvalidProducer(job_id, 'job_integrity')
        return existing, None
    try:
        record = _producer_read(project, job_id, 'result')
    except LimitExceeded as exc:
        raise InvalidProducer(job_id, 'result_file_bytes') from exc
    try:
        value = normalize(record, bound, producer={'kind': 'check_verify', 'id': job_id, 'run': job.get('run')})
    except (Denied, ValueError, UnicodeError) as exc:
        raise InvalidProducer(job_id, 'result_integrity') from exc
    if record.get('code_digest') != job.get('code_digest'):
        raise InvalidProducer(job_id, 'result_integrity')
    return None, value


def discover(project):
    """One queue-locked read-only snapshot, with complete internal exclusions.

    Only safely read individual content failures are isolated. No retention,
    acknowledgement, quarantine, proof creation or producer writes occur here.
    Raw records are retained so overlays remain exactly one hop.
    """
    jobs, invalid = {}, {}
    with lock(project.root / 'locks/check-jobs.lock'):
        for path in job_paths(project, invalid):
            job_id = path.parent.name
            try:
                try:
                    job = read_job(project, job_id)
                except LimitExceeded as exc:
                    raise InvalidProducer(job_id, 'job_file_bytes') from exc
                _inspect_evidence(project, job)
                jobs[job_id] = job
            except InvalidProducer as exc:
                invalid[exc.job_id] = exc.code
        projected = {}
        for job_id, raw in jobs.items():
            job = dict(raw)
            if job.get('shared_job') and not job.get('detached'):
                original = jobs.get(job['shared_job'])
                if original is None:
                    invalid[job_id] = 'job_subscription'
                    continue
                for key in ('status', 'passed', 'error', 'finished_at'):
                    if key in original:
                        job[key] = original[key]
            elif job.get('detached'):
                job.update(status='cancelled', passed=False)
            projected[job_id] = job
    return projected, invalid


def _directory(root, relative):
    # Managed directories are never aliases. Reads additionally pin components
    # with safe_read. Writers are serialized by acceptance.lock.
    path = root
    for part in Path(relative).parts:
        path = path / part
        try:
            path.mkdir()
        except FileExistsError:
            pass
        if not stat.S_ISDIR(path.lstat().st_mode) or path.is_symlink():
            raise Denied('Unsafe acceptance evidence directory')
    return path


def _present(root, relative):
    path = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or (index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode)):
            raise Denied('Unsafe acceptance evidence path')
    return True


def _read(root, relative, maximum=MAX_BYTES):
    try:
        return json.loads(safe_read(root, relative, maximum))
    except (ValueError, UnicodeError) as exc:
        raise Denied('Malformed acceptance evidence') from exc


def scope(project, *, _cache_locked=False):
    from .check_jobs import cache_guard
    if _cache_locked:
        return _current_scope(project)
    with cache_guard(project):
        return _current_scope(project)


def _current_scope(project):
    """Build a scope with cache_guard already held; never reacquire it."""
    from .check_jobs import environment, execution_key, cache_input
    if not project.verify:
        return None
    env = environment(project)
    if env is None:
        return None
    value = {'commands': list(project.verify), 'execution': execution_key(project.config),
             'environment': env, 'image': project.config.sandbox.image}
    if project.config.sandbox.check_cache_paths:
        current = cache_input(project)
        if current is None:
            return None
        value['cache_input'] = current
    try:
        _scope(value)
    except Denied:
        return None  # Fresh unrepresentable inputs cannot select retained proof.
    return value


def _scope(value):
    if (not isinstance(value, dict) or set(value) not in ({'commands', 'execution', 'environment', 'image'},
                                                         {'commands', 'execution', 'environment', 'image', 'cache_input'})
            or 'cache_input' in value and not _digest(value['cache_input'])
            or not _digest(value['execution']) or not _digest(value['environment'])
            or not isinstance(value['image'], str)
            or not re.fullmatch(r'(?:.*@)?sha256:[a-f0-9]{64}', value['image'])
            or not isinstance(value['commands'], list) or not 1 <= len(value['commands']) <= 32
            or any(not isinstance(c, str) or not c or '\x00' in c or '\n' in c for c in value['commands'])):
        raise Denied('Invalid acceptance scope')
    if len(canonical(value)) > MAX_BYTES // 2:
        raise Denied('Acceptance scope exceeds receipt bound')
    return digest(canonical(value))


def normalize(record, bound, *, producer):
    """Validate a producer result and discard previews, logs and cache paths."""
    if bound is None:
        return None
    _scope(bound)
    if (not isinstance(record, dict) or any(record.get(k) is not True for k in
            ('passed', 'snapshot_representable', 'unchanged_during_verification', 'dependencies_unchanged'))
            or not _digest(record.get('code_digest')) or record.get('image') != bound['image']):
        raise Denied('Invalid acceptance producer result')
    if (not isinstance(producer, dict) or set(producer) != {'kind', 'id', 'run'}
            or producer['kind'] not in ('verify', 'check_verify')
            or not isinstance(producer['run'], str) or not re.fullmatch('[a-f0-9]{32}', producer['run'])
            or not isinstance(producer['id'], str)
            or not re.fullmatch('[a-f0-9]{64}' if producer['kind'] == 'check_verify' else '[a-f0-9]{32}', producer['id'])
            or (producer['kind'] == 'verify' and producer['id'] != producer['run'])
            or record.get('run') != producer['run']
            or (producer['kind'] == 'check_verify' and record.get('job') != producer['id'])):
        raise Denied('Invalid acceptance producer identity')
    commands = record.get('commands')
    if (not isinstance(commands, list) or len(commands) != len(bound['commands'])
            or any(not isinstance(c, dict) or c.get('reason') != 'exited'
                   or type(c.get('exit_code')) is not int or c['exit_code'] != 0
                   or not isinstance(c.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', c['id'])
                   for c in commands)):
        raise Denied('Invalid acceptance command results')
    # Full scripts belong to scope, not to the producer's truncated previews.
    return {'schema': 1, 'code_digest': record['code_digest'], 'scope': bound,
            'producer': producer, 'passed': True,
            'commands': [{'id': c['id'], 'exit_code': 0, 'reason': 'exited'} for c in commands]}


def _validate(value, receipt_id):
    if (not isinstance(value, dict) or set(value) != {'schema', 'code_digest', 'scope', 'producer', 'passed', 'commands'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['scope'], dict) or not isinstance(value['producer'], dict)
            or len(canonical(value)) > MAX_BYTES
            or digest(canonical(value)) != receipt_id):
        raise Denied('Invalid acceptance receipt identity')
    record = {**value, 'snapshot_representable': True, 'unchanged_during_verification': True,
              'dependencies_unchanged': True, 'image': value['scope'].get('image'),
              'run': value['producer'].get('run'), 'job': value['producer'].get('id')}
    if normalize(record, value['scope'], producer=value['producer']) != value:
        raise Denied('Invalid acceptance receipt')
    return value


def get(root, receipt_id):
    """Resolve only an exact immutable ID; no scope/index substitution."""
    if not _digest(receipt_id):
        raise Denied('Invalid acceptance receipt ID')
    return _validate(_read(root, f'{_BASE}/objects/{receipt_id}.json'), receipt_id)


def _index(root, code, bound):
    if not _digest(code):
        raise Denied('Invalid acceptance source digest')
    return f'{_BASE}/index/{code}/{_scope(bound)}.json'


def _selected(root, name, code, bound):
    value = _read(root, name)
    if not isinstance(value, dict) or set(value) != {'schema', 'receipt_id'} or type(value['schema']) is not int or value['schema'] != 1:
        raise Denied('Invalid acceptance index')
    receipt = get(root, value['receipt_id'])
    if receipt['code_digest'] != code or receipt['scope'] != bound:
        raise Denied('Acceptance index scope mismatch')
    return value['receipt_id']


def retain(project, record, bound, *, producer):
    value = normalize(record, bound, producer=producer)
    if value is None:
        return None
    return _retain(project.root, value)


def _retain(root, value):
    receipt_id = digest(canonical(value))
    _validate(value, receipt_id)
    name = _index(root, value['code_digest'], value['scope'])
    with lock(root / 'locks/acceptance.lock'):
        _directory(root, f'{_BASE}/objects')
        _directory(root, str(Path(name).parent))
        target = root / f'{_BASE}/objects/{receipt_id}.json'
        if target.exists() or target.is_symlink():
            get(root, receipt_id)
        else:
            atomic_write(target, canonical(value), exclusive=True)
        # Object is durable before index. A retry repairs this crash window.
        if (root / name).exists() or (root / name).is_symlink():
            return _selected(root, name, value['code_digest'], value['scope'])
        atomic_write(root / name, canonical({'schema': 1, 'receipt_id': receipt_id}), exclusive=True)
    return receipt_id


def effective(project, snapshot):
    """Separate projection; never mutates a snapshot or imports current proof."""
    if snapshot.get("skipped"):
        return None
    from .check_jobs import cache_guard
    # Keep the observed cache input stable through the final receipt selection.
    # This takes neither queue nor acceptance exclusion; scope is told not to
    # reacquire the non-reentrant cache lock already held here.
    with cache_guard(project):
        bound = scope(project, _cache_locked=True)
        if bound is None:
            return None
        name = _index(project.root, snapshot['code_digest'], bound)
        if not _present(project.root, name):
            return None
        receipt_id = _selected(project.root, name, snapshot['code_digest'], bound)
        return {'passed': True, 'code_digest': snapshot['code_digest'],
                'source': 'retained_acceptance', 'receipt_id': receipt_id}


def audit(root, *, blocking=True):
    """Validate the durable store; maintenance may opt out of waiting for its lock."""
    with lock(root / 'locks/acceptance.lock', blocking=blocking):
        return _audit(root)


def _audit(root):
    base = root / _BASE
    if not _present(root, _BASE):
        return 0
    evidence = root / 'evidence'
    if not stat.S_ISDIR(evidence.lstat().st_mode) or evidence.is_symlink():
        raise Denied('Unsafe acceptance evidence directory')
    def children(relative):
        path = root / relative
        if not stat.S_ISDIR(path.lstat().st_mode) or path.is_symlink():
            raise Denied('Unsafe acceptance evidence directory')
        return sorted(path.iterdir())
    members = {p.name for p in children(_BASE)}
    if members - {'objects', 'index'}:
        raise Denied('Invalid acceptance store members')
    count = 0
    for path in children(f'{_BASE}/objects') if 'objects' in members else ():
        if not path.name.endswith('.json') or not _digest(path.stem):
            raise Denied('Invalid acceptance object name')
        get(root, path.stem)
        count += 1
    for directory in children(f'{_BASE}/index') if 'index' in members else ():
        if not _digest(directory.name):
            raise Denied('Invalid acceptance index source')
        for path in children(f'{_BASE}/index/{directory.name}'):
            if not path.name.endswith('.json') or not _digest(path.stem):
                raise Denied('Invalid acceptance index name')
            value = _read(root, path.relative_to(root).as_posix())
            receipt = get(root, value.get('receipt_id') if isinstance(value, dict) else None)
            if _scope(receipt['scope']) != path.stem:
                raise Denied('Acceptance index scope mismatch')
            _selected(root, path.relative_to(root).as_posix(), directory.name, receipt['scope'])
    return count


def recover(root):
    """Idempotently index validated immutable producer receipts after a crash.

    No check-jobs lock is acquired here. Cache eviction cannot lose these
    objects, and no cache result or preview is promoted into authority.
    """
    # Audit and enumerate under the writer's lock: temporary atomic-write
    # files are never mistaken for corrupt receipts by concurrent recovery.
    with lock(root / 'locks/acceptance.lock'):
        _audit(root)
        base = root / _BASE / 'objects'
        values = [get(root, path.stem) for path in sorted(base.iterdir())] if base.exists() else []
    for value in values:
        _retain(root, value)


def recover_run(project, run_id):
    if not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id):
        raise Denied('Invalid acceptance run ID')
    try:
        record = _read(project.root, f'runs/{run_id}/verification.json', project.config.limits.file_bytes)
    except LimitExceeded:
        return None  # Unrepresentable producer evidence cannot mint proof.
    bound = record.get('acceptance_scope') if isinstance(record, dict) else None
    if bound is not None:
        return retain(project, record, bound, producer={'kind': 'verify', 'id': run_id, 'run': run_id})
    return None


def recover_job(project, job_id):
    # Snapshot the producer under its own lock, then release BEFORE retaining.
    if not _digest(job_id):
        raise Denied('Invalid acceptance job ID')
    with lock(project.root / 'locks/check-jobs.lock'):
        if not _present(project.root, f'check-jobs/{job_id}/job.json'):
            return None  # Cache eviction is not loss of durable proof.
        try:
            job = read_job(project, job_id)
        except LimitExceeded:
            return None
        try:
            existing, value = _inspect_evidence(project, job)
        except InvalidProducer as exc:
            if exc.code != 'result_file_bytes':
                raise
            # Recovery alone owns this terminal non-authoritative size
            # disposition. Read-only discovery leaves the scope and bytes alone.
            job.pop('acceptance_scope')
            job['acceptance_retention_refusal'] = 'result_file_bytes'
            from .fs import write_json
            write_json(project.root / f'check-jobs/{job_id}/job.json', job)
            return None
        if existing is not None or value is None:
            return existing
    receipt_id = _retain(project.root, value)
    # Separate acquisitions: never acceptance.lock -> check-jobs.lock while
    # retaining. Eviction waits for this durable-proof acknowledgement.
    with lock(project.root / 'locks/check-jobs.lock'):
        if not _present(project.root, f'check-jobs/{job_id}/job.json'):
            return receipt_id
        latest = read_job(project, job_id)
        if latest.get('acceptance_receipt_id') != receipt_id:
            latest['acceptance_receipt_id'] = receipt_id
            from .fs import write_json
            write_json(project.root / f'check-jobs/{job_id}/job.json', latest)
    return receipt_id


def recover_producers(project, *, include_runs=False):
    """Recovery trigger for runner/backup; terminal producers are idempotent.

    Old/unbound results have no scope marker and cannot acquire one in recovery.
    Invalid cache content is returned by ID/code, never modified or retained.
    Callers bound the visible diagnostics separately from the exclusion set.
    Runner recovery scans only the bounded job cache. Backup maintenance can
    also retry retained run records and unindexed objects. Durable acceptance
    projection stays indexed; discover() separately inspects the cache without
    retaining results or scanning historical runs.
    """
    if include_runs:
        recover(project.root)
    invalid = {}
    with lock(project.root / 'locks/check-jobs.lock'):
        paths = job_paths(project, invalid)
    for path in paths:
        try:
            recover_job(project, path.parent.name)
        except InvalidProducer as exc:
            invalid[exc.job_id] = exc.code
    if include_runs:
        for path in sorted((project.root / 'runs').glob('*/verification.json')):
            recover_run(project, path.parent.name)
    return invalid
