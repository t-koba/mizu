"""Private durable state for the single-child async-delegation pilot.

This is not a scheduler, tool API, workspace lock, or check-job authority. The
trusted host supplies a dedicated project-state root (never the live tree),
holds the real workspace lock while starting, and must fence tree access while
``reservation()`` is non-null. All callers must share this store. Only trusted
supervisor code may use ``reservation`` or ``runner_*``: the stored random token
is a runner binding, not a grant exposed to a model. The state directory and its
ancestors must be protected from concurrent untrusted host mutation. Symlink,
hardlink, special-file, invalid-schema and oversized state is refused, never
repaired or treated as an empty queue. This module alone launches nothing.

Schema v3: one canonical JSON ledger, {version, active, jobs}. Jobs are keyed by
SHA256([owner, key]); each contains owner/key, immutable request digest, status,
runner token, stable child run identity, execution outcome, request, result and
result digest. The run identity is a random lowercase 32-hex string generated
once on first start, retained in terminal tombstones for crash reconciliation,
and never exposed in owner views. The supervisor-only binding is null until
explicitly bound, then immutable and retained in terminal tombstones; it pins
the exact run-directory path/device/inode, systemd unit/cgroup and the full
rootless-Podman name/64-hex-ID/UID inventory. It is not launch authority. Older
ledgers are refused, never migrated or reset. A request is exactly
{role, task, profile, source_snapshot}; profile may be null, and snapshot is an
immutable SHA256 snapshot ID. The host must pin the rest of the invocation
authority before using this primitive; these fields grant no execution rights.
Requests are bounded to 128 KiB canonical UTF-8 (task <= 65536 UTF-8 bytes),
results to 32 KiB UTF-8, and the ledger to 1 MiB. Only the latest terminal
result is retained. Old jobs retain identity/status/result-digest/binding
tombstones indefinitely. The 1024-job history cap is shared project-state
capacity policy pending this pilot's configuration section: capacity exhaustion
refuses new starts, never forgets a key. The byte ceilings and schema are
private format/safety limits, not scheduling policy.

Every mutation uses a dedicated file lock and one fsynced atomic replacement.
Start commits identity AND reservation before returning; an interrupted write
leaves the complete old or new ledger. Retry the same owner/key/exact request:
it returns the original job even after completion/result eviction. Conflicting
reuse is always denied. An interrupted first initialization fails closed if the
new directory exists without a ledger. Never delete state to recover a job.

queued -> running (one successful runner claim only), queued/running ->
cancel-requested, and any nonterminal state -> terminal only after two reports:
terminal child execution, THEN quiescent driver/bridge/process teardown. Even a
queued cancellation or failed/dead runner retains the reservation until the
supervisor proves no child can still access the tree. Cancellation is a durable
request, not a kill acknowledgement. Recovery is inspection only: running jobs
are NEVER requeued, claimed twice, or automatically replayed. A supervisor must
reconcile uncertain execution and prove quiescence before handback. Result
text is untrusted evidence, not verification or publication authority. Missing
results are reported as evicted; tombstones still prevent duplicate execution.
"""
from __future__ import annotations

import contextlib
import re
import secrets
import stat
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, canonical, digest, identifier, loads_finite, lock,
                 safe_read, safe_write, sync_dir, write_json)

MAX_JOBS = 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESULT_BYTES = 32768
MAX_TASK_BYTES = 65536
# Private serialization/safety ceiling, not a resource-admission policy.
MAX_BINDING_BYTES = 8192
_STATES = {'queued', 'running', 'cancel-requested', 'terminal'}
_OUTCOMES = {'succeeded', 'failed', 'cancelled'}
_FIELDS = {'owner', 'key', 'request_digest', 'status', 'token', 'run_id', 'execution',
           'request', 'result', 'result_digest', 'binding'}
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def _sha(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _run_id(value):
    return isinstance(value, str) and RUN_ID.fullmatch(value) is not None


def _text(value, maximum):
    if not isinstance(value, str):
        raise Denied('Invalid delegation text')
    try:
        if len(value.encode('utf-8')) > maximum:
            raise Denied('Delegation text exceeds byte bound')
    except UnicodeError:
        raise Denied('Invalid delegation text encoding') from None


def _request(value):
    if type(value) is not dict or set(value) != {'role', 'task', 'profile', 'source_snapshot'}:
        raise Denied('Invalid delegation request schema')
    identifier(value['role'])
    if value['profile'] is not None:
        identifier(value['profile'])
    _text(value['task'], MAX_TASK_BYTES)
    if not value['task'].strip() or not _sha(value['source_snapshot']):
        raise Denied('Invalid delegation task or source snapshot')
    raw = canonical(value)
    if len(raw) > MAX_REQUEST_BYTES:
        raise Denied('Delegation request exceeds byte bound')
    return digest(raw)


def _directory_identity(path):
    """Exact absolute directory, no symlink components or lexical aliases.

    The supervisor must protect its ancestors against concurrent mutation, just
    like the ledger root. No directory content is trusted or executed here.
    """
    if (not isinstance(path, str) or '\x00' in path
            or not Path(path).is_absolute() or str(Path(path)) != path):
        raise Denied('Invalid delegation run directory')
    target = Path(path)
    if '..' in target.parts:
        raise Denied('Invalid delegation run directory')
    for component in (*reversed(target.parents), target):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise Denied('Unsafe delegation run directory')
    info = target.lstat()
    if info.st_ino <= 0:
        raise Denied('Unobservable delegation directory identity')
    return {'path': path, 'device': info.st_dev, 'inode': info.st_ino}


def _binding(value, run_id):
    """Validate the bounded supervisor-only systemd/rootless-Podman format."""
    if (type(value) is not dict
            or set(value) != {'run_id', 'directory', 'scope', 'containers'}
            or value['run_id'] != run_id or len(canonical(value)) > MAX_BINDING_BYTES):
        raise Denied('Invalid delegation execution binding')
    directory, scope, containers = value['directory'], value['scope'], value['containers']
    if (type(directory) is not dict or set(directory) != {'path', 'device', 'inode'}
            or not isinstance(directory['path'], str) or '\x00' in directory['path']
            or not Path(directory['path']).is_absolute()
            or str(Path(directory['path'])) != directory['path']
            or '..' in Path(directory['path']).parts
            or any(type(directory[k]) is not int or directory[k] < 0 for k in ('device', 'inode'))
            or directory['inode'] == 0
            or type(scope) is not dict or set(scope) != {'unit', 'cgroup'}
            or not isinstance(scope['unit'], str)
            or re.fullmatch(r'[A-Za-z0-9_-]+\.service', scope['unit']) is None
            or scope['cgroup'] != '/system.slice/' + scope['unit']
            or type(containers) is not list):
        raise Denied('Invalid delegation directory or scope binding')
    names, ids = set(), set()
    for container in containers:
        if (type(container) is not dict or set(container) != {'name', 'id', 'uid'}
                or not isinstance(container['name'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container['name']) is None
                or not _sha(container['id'])
                or type(container['uid']) is not int or container['uid'] < 0
                or container['id'] in ids or container['name'] in names):
            raise Denied('Invalid or ambiguous delegation OCI binding')
        names.add(container['name'])
        ids.add(container['id'])


def _bound(job):
    binding = job['binding']
    if (binding is None
            or _directory_identity(binding['directory']['path']) != binding['directory']):
        raise Denied('Delegation execution binding unavailable or mismatched')
    return binding


def _identity(owner, key):
    identifier(owner)
    identifier(key)
    return digest(canonical([owner, key]))


def _regular(path):
    """Check existing lock/ledger targets without following a link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Denied('Unsafe delegation state file')


class DelegationJobs:
    """One project's durable reservation. Construction/recovery never starts work.

    ``start/status/result/cancel`` require the authenticated owner role from the
    host, not a model-supplied identity. Owner views omit runner tokens/requests,
    the stable run identity and binding. ``reservation`` is internal recovery
    evidence, including the pinned request, token, run identity and binding;
    it is NOT permission to replay it. All returned values are fresh decoded copies. OS/durability
    errors propagate: an error after replacement has uncertain acknowledgement,
    so inspect/retry, never launch from the error.
    """

    def __init__(self, project_root: Path):
        self.root = Path(project_root) / 'delegation-jobs'
        self._directory(self.root.parent)
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            # Directory existence is the initialization marker. If interrupted
            # here, absence of state is an error, not permission for a new start.
            sync_dir(self.root.parent)
            write_json(self.root / 'state.json', {'version': 3, 'active': None, 'jobs': {}},
                       exclusive=True)
        with self._locked():
            self._load()

    @classmethod
    def existing_reservation(cls, project_root: Path):
        """Read without initializing a store; only an absent directory is clear.

        Invalid, incomplete or unreadable existing state is denied, never
        repaired. The store lock serializes reads with ledger mutations; trusted
        starts must additionally hold workspace.lock, as must callers needing
        a stable no-reservation decision across live-tree access. No liveness or
        quiescence is inferred from acquiring either process lock.
        """
        store = cls.__new__(cls)
        store.root = Path(project_root) / 'delegation-jobs'
        try:
            store._directory(store.root.parent)
            try:
                store._directory(store.root)
            except FileNotFoundError:
                return None
            # A completed initialization has both files. Do not create a
            # missing lock while inspecting an interrupted initialization.
            (store.root / 'store.lock').lstat()
            return store.reservation()
        except (OSError, LimitExceeded):
            raise Denied('Unreadable delegation state') from None

    @classmethod
    def guard(cls, project_root: Path):
        """Refuse live-workspace admission while a reservation may be active."""
        if cls.existing_reservation(project_root) is not None:
            raise Denied('Project has an active delegation reservation')

    @staticmethod
    def _directory(path):
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise Denied('Unsafe delegation state directory')

    @contextlib.contextmanager
    def _locked(self):
        self._directory(self.root.parent)
        self._directory(self.root)
        _regular(self.root / 'store.lock')
        with lock(self.root / 'store.lock'):
            _regular(self.root / 'store.lock')
            yield

    def _load(self):
        try:
            raw = safe_read(self.root, 'state.json', MAX_STATE_BYTES)
            state = loads_finite(raw)
            self._validate(state)
            if raw != canonical(state):
                raise Denied('Noncanonical delegation state')
            return state
        except (ValueError, TypeError, KeyError, RecursionError, FileNotFoundError):
            raise Denied('Invalid or missing delegation state') from None

    @staticmethod
    def _validate(state):
        if (type(state) is not dict or set(state) != {'version', 'active', 'jobs'}
                or type(state['version']) is not int or state['version'] != 3
                or type(state['jobs']) is not dict or len(state['jobs']) > MAX_JOBS):
            raise Denied('Invalid delegation ledger')
        active = []
        retained = 0
        seen_runs = set()
        for job_id, job in state['jobs'].items():
            if (type(job) is not dict or set(job) != _FIELDS
                    or job_id != _identity(job['owner'], job['key'])
                    or not _sha(job['request_digest'])
                    or not _run_id(job['run_id'])
                    or not isinstance(job['status'], str) or job['status'] not in _STATES):
                raise Denied('Invalid delegation job')
            if job['run_id'] in seen_runs:
                raise Denied('Duplicate delegation run identity')
            seen_runs.add(job['run_id'])
            if job['binding'] is not None:
                _binding(job['binding'], job['run_id'])
            execution = job['execution']
            if execution is not None and (not isinstance(execution, str) or execution not in _OUTCOMES):
                raise Denied('Invalid delegation execution outcome')
            if job['status'] != 'terminal':
                active.append(job_id)
                if (not _sha(job['token']) or _request(job['request']) != job['request_digest']
                        or job['result'] is not None or job['result_digest'] is not None
                        or (job['status'] == 'queued' and execution is not None)):
                    raise Denied('Invalid active delegation reservation')
            else:
                if (job['binding'] is None or execution is None
                        or job['token'] is not None or job['request'] is not None
                        or not _sha(job['result_digest'])):
                    raise Denied('Invalid delegation tombstone')
                if job['result'] is not None:
                    _text(job['result'], MAX_RESULT_BYTES)
                    retained += 1
                    if digest(job['result'].encode()) != job['result_digest']:
                        raise Denied('Invalid delegation result digest')
        if retained > 1 or active != ([] if state['active'] is None else [state['active']]):
            raise Denied('Invalid delegation reservation index')

    def _save(self, state):
        self._validate(state)
        raw = canonical(state)
        if len(raw) > MAX_STATE_BYTES:
            raise Denied('Delegation ledger exceeds byte bound')
        safe_write(self.root, 'state.json', raw)

    @staticmethod
    def _owned(state, owner, job_id):
        identifier(owner)
        if not _sha(job_id):
            raise Denied('Invalid delegation job ID')
        job = state['jobs'].get(job_id)
        if job is None or job['owner'] != owner:
            raise Denied('Delegation job unavailable to owner')
        return job

    @staticmethod
    def _view(job_id, job):
        # Owner-facing view: run identity, token and request stay supervisor-only.
        return {'id': job_id, 'status': job['status'], 'execution': job['execution'],
                'reserved': job['status'] != 'terminal',
                'result_available': job['result'] is not None}

    def start(self, owner, key, request):
        """Atomically reserve or return the exact original start; never launch."""
        job_id = _identity(owner, key)
        fingerprint = _request(request)
        # Detach caller-owned mutable input before committing it.
        request = loads_finite(canonical(request))
        with self._locked():
            state = self._load()
            old = state['jobs'].get(job_id)
            if old is not None:
                if old['request_digest'] != fingerprint:
                    raise Denied('Conflicting delegation start key')
                return self._view(job_id, old)
            if state['active'] is not None:
                raise Denied('Project already has an active delegation reservation')
            if len(state['jobs']) >= MAX_JOBS:
                raise Denied('Delegation idempotency history is full')
            run_id = secrets.token_hex(16)
            while any(existing.get('run_id') == run_id for existing in state['jobs'].values()):
                run_id = secrets.token_hex(16)
            job = {'owner': owner, 'key': key, 'request_digest': fingerprint,
                   'status': 'queued', 'token': secrets.token_hex(32), 'run_id': run_id,
                   'execution': None, 'request': request, 'result': None, 'result_digest': None,
                   'binding': None}
            state['jobs'][job_id] = job
            state['active'] = job_id
            self._save(state)
            return self._view(job_id, job)

    def status(self, owner, job_id):
        with self._locked():
            return self._view(job_id, self._owned(self._load(), owner, job_id))

    def result(self, owner, job_id):
        """Return bounded terminal evidence, or refuse pending/evicted results."""
        with self._locked():
            job = self._owned(self._load(), owner, job_id)
            if job['status'] != 'terminal' or job['result'] is None:
                raise Denied('Delegation result pending or evicted')
            return {**self._view(job_id, job), 'result': job['result']}

    def cancel(self, owner, job_id):
        """Idempotent request only; neither failure nor cancellation frees tree."""
        with self._locked():
            state = self._load()
            job = self._owned(state, owner, job_id)
            if job['status'] not in {'cancel-requested', 'terminal'}:
                job['status'] = 'cancel-requested'
                self._save(state)
            return self._view(job_id, job)

    def reservation(self):
        """Internal recovery inspection; no requeue/replay or liveness inference."""
        with self._locked():
            state = self._load()
            job_id = state['active']
            return None if job_id is None else {'id': job_id, **state['jobs'][job_id]}

    @staticmethod
    def _runner(state, job_id, token):
        if not _sha(job_id) or not _sha(token):
            raise Denied('Invalid delegation runner binding')
        job = state['jobs'].get(job_id)
        if (job is None or state['active'] != job_id
                or not secrets.compare_digest(job['token'], token)):
            raise Denied('Delegation runner does not own reservation')
        return job

    def runner_bind(self, job_id, token, run_directory, scope, containers):
        """Persist exact execution identities before launch; never launch or discover.

        Only the trusted supervisor may supply the complete resource inventory.
        Scope is {unit, cgroup}; containers are exact rootless-Podman {name, id,
        uid} records, including every writable MCP/execution container (empty
        only when none can exist). Directory identity is captured without links.
        The canonical binding is capped at 8 KiB within the unchanged 1 MiB
        ledger. While reserved, exact retries return the original; conflicting or
        late binds refuse. Failed/uncertain writes require inspection, not another launch.
        """
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            binding = loads_finite(canonical({
                'run_id': job['run_id'], 'directory': _directory_identity(run_directory),
                'scope': scope, 'containers': containers}))
            _binding(binding, job['run_id'])
            if job['binding'] is not None:
                if job['binding'] != binding:
                    raise Denied('Conflicting delegation execution binding')
                return job['binding']
            if (job['status'] not in {'queued', 'cancel-requested'}
                    or job['execution'] is not None):
                raise Denied('Delegation binding must precede runner claim')
            job['binding'] = binding
            self._save(state)
            return binding

    def runner_binding(self, job_id, token):
        """Read ONLY persisted recovery identities; unknown directory retains hold.

        This does not discover resources, rebind paths, or assert quiescence.
        The supervisor must still independently inspect every saved resource.
        """
        with self._locked():
            return _bound(self._runner(self._load(), job_id, token))

    def runner_started(self, job_id, token):
        """Consume the queued claim once BEFORE launch; uncertain claim never replays.

        A failed acknowledgement requires reconciliation, not a second launch.
        The complete binding must precede the claim. A queued cancellation cannot
        be claimed; supervisor reports no-execution cancellation then quiescence
        instead. An unbound cancellation remains held for reconciliation.
        """
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            if job['status'] != 'queued' or job['binding'] is None:
                raise Denied('Delegation is unbound or not queued; do not replay execution')
            _bound(job)
            job['status'] = 'running'
            self._save(state)

    def runner_finished(self, job_id, token, outcome):
        """Report terminal execution, NOT teardown. Reservation remains held.

        A supervisor may report failed/cancelled without launch for a queued
        job; success requires a running child. Repeating the same report is safe.
        """
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise Denied('Invalid delegation execution outcome')
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            if job['execution'] is not None:
                if job['execution'] != outcome:
                    raise Denied('Conflicting delegation execution outcome')
                return
            if job['status'] == 'queued':
                if outcome == 'succeeded':
                    raise Denied('Queued delegation cannot succeed')
                job['status'] = 'running'
            job['execution'] = outcome
            self._save(state)

    def runner_quiescent(self, job_id, token, result, *, cleanup=None):
        """Seal bounded evidence and hand back tree ONLY after proven teardown.

        The trusted supervisor supplies independent positive teardown evidence:
        cleanup is exactly {binding: the saved binding, scope_clean: True,
        containers_absent: [every saved container ID in inventory order]}.
        scope_clean covers child, driver, bridge and ALL descendants. The
        supervisor must inspect each exact OCI name AND ID in the local default
        rootless-Podman namespace for the saved uid. A different runtime/store,
        renamed/mismatched or unobservable resource is unknown, never absent;
        the supervisor must not query a substitute namespace. This module
        checks evidence identity, not the OS.
        Directory identity is independently rechecked here. Missing binding or
        any unknown proof retains the reservation, including a queued failure.
        No PID/timeout/owner cancellation implies quiescence.
        An uncertain acknowledgement is resolved by owner status/result, not by
        rerunning the child. Oversized evidence refuses handback; submit a bounded
        summary after teardown instead. Old retained result text is evicted in
        this same commit, but identity/outcome/digest tombstones are never removed.
        """
        _text(result, MAX_RESULT_BYTES)
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            if job['execution'] is None:
                raise Denied('Delegation execution is not terminal')
            binding = _bound(job)
            if (type(cleanup) is not dict
                    or set(cleanup) != {'binding', 'scope_clean', 'containers_absent'}
                    or canonical(cleanup['binding']) != canonical(binding)
                    or cleanup['scope_clean'] is not True
                    or cleanup['containers_absent'] != [c['id'] for c in binding['containers']]):
                raise Denied('Delegation cleanup proof missing or mismatched')
            for old in state['jobs'].values():
                old['result'] = None
            job.update(status='terminal', token=None, request=None, result=result,
                       result_digest=digest(result.encode()))
            state['active'] = None
            self._save(state)
