"""Private durable state for the single-child async-delegation pilot.

This is not a scheduler, tool API, runner, or check-job authority. The
trusted host supplies a dedicated project-state root (never the live tree),
uses ``workspace_start`` (or Engine's private admitted-lease handoff)
while preparing and starting, and must fence tree access while
``reservation()`` is non-null. All callers must share this store. Only trusted
supervisor code may use ``reservation`` or ``runner_*``: the stored random token
is a runner binding, not a grant exposed to a model. The state directory and its
ancestors must be protected from concurrent untrusted host mutation. Symlink,
hardlink, special-file, invalid-schema and oversized state is refused, never
repaired or treated as an empty queue. This module constructs no launch command;
its internal `runner_effect` only admits a trusted bounded operation under the
fence lock and grants no settlement or handback authority.

Schema v5: one canonical JSON ledger, {version, active, jobs}. Jobs are keyed by
SHA256([owner, key]); each contains owner/key, immutable request digest, status,
runner token, stable child run identity, execution outcome, request, result and
result digest. The run identity is a random lowercase 32-hex string generated
once on first start, retained in terminal tombstones for crash reconciliation,
and never exposed in owner views. The supervisor-only binding pins the exact
run-directory path/device/inode, systemd unit/cgroup and full rootless-Podman
name/64-hex-ID/UID inventory. A provisioning intent pins one synthetic fixture
container's expected bounded safe name/UID/run-id marker/image/config before
creation; the marker must equal run_id while the name is any bounded safe name,
and only the persisted intent is recovery authority. Intent
and binding, including submit/create/start attempts, share the 8 KiB inventory
allowance and clear at terminal handback.
Recovery fences bind/claim/finish before inspection and persists a validated ID
before removal in the test-only fixture. Even independent unstarted-scope and
name/ID/marker absence observations never authorize product handback. A durable claim bit survives cancellation: uncertain binds can be
recovered, but claimed runners never take this prelaunch path. Neither record
is launch authority. V5 intents carry per-effect not-issued/issued/completed
attempts; issued is ambiguous after any interruption and never replayable.
Completed results contain only the exact unit or full container ID, not output.
The cancel-requested state is the durable fence for all mediated effects. Older
ledger versions are refused, never migrated or reset.
V5 requests are {role, task, profile, source_snapshot}, optionally extended with
an authority_digest (64 lowercase hex). Bound requests require a resolved profile;
legacy requests may retain null. Engine's admitted-parent handoff resolves the
child and includes child_authority_digest in the immutable request fingerprint.
Snapshot is an immutable SHA256 snapshot ID. V5 requests and tombstones
remain readable/retryable without rewriting; an unbound legacy request cannot
be upgraded by retry. runner_bind/runner_started require an explicitly supplied
matching resolved authority digest, even for an already saved resource binding.
Legacy jobs remain reserved for inspection/recovery, never launch. The host must
resolve the same effective inputs before comparison; copying a stored digest is
not validation. Matching configuration still grants no execution rights.
Requests are bounded to 128 KiB canonical UTF-8 (task <= 65536 UTF-8 bytes),
results to 32 KiB UTF-8, and the ledger to 1 MiB plus 14 bytes per job for the
explicit intent field plus one maximum active binding/intent inventory and
86 bytes for the active request authority_digest. Terminal handback clears the
request. At most one job is active, so these active allowances do not accumulate
across history. Only the latest terminal result is retained; old
jobs retain small identity/status/result-digest tombstones indefinitely. The
1024-job history cap is shared project-state capacity policy: exhaustion refuses
new starts rather than forgetting a key. The private Engine reservation handoff
has no runner or model-visible configuration path, so defer an operator setting
until async delegation is integrated instead of exposing an inert option. The
byte ceilings and schema are private format/safety limits, not scheduling policy.

Every mutation uses a dedicated file lock and one fsynced atomic replacement.
Start commits identity AND reservation before returning; an interrupted write
leaves the complete old or new ledger. Retry the same owner/key/exact request:
it returns the original job even after completion/result eviction. Conflicting
reuse is always denied. An interrupted first initialization fails closed if the
new directory exists without a ledger. Never delete state to recover a job.

queued -> running (one successful runner claim only), queued/running ->
cancel-requested, and any nonterminal state -> terminal only after two reports:
terminal child execution, THEN Mizu-proven driver/bridge/process teardown.
Post-execution handback is currently refused: the internal effect fence does
not establish creator or supervisor settlement and is not runner-integrated. Caller
cleanup assertions cannot release the reservation. Even a
queued cancellation or failed/dead runner retains the reservation until the
supervisor proves no child can still access the tree. The prepared path also
refuses handback: no product-owned process/container verifier or runner
integration exists, so caller scope/container assertions cannot authorize
handback either. `runner_recover_prepared` still durably fences bind/claim/finish
before inspection. Cancellation is
a durable request, not a kill acknowledgement. Recovery is inspection only: running jobs
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
import threading
from pathlib import Path

from .errors import Denied, LimitExceeded
from .fs import (DIGEST, canonical, digest, held, identifier, loads_finite, lock,
                 safe_read, safe_write, sync_dir, write_json)

MAX_JOBS = 1024
# One active job has one bounded intent/binding inventory; handback clears it.
MAX_BINDING_BYTES = 8192
# Every job carries an explicit `"intent":null` (14 serialized bytes: comma +
# `"intent":null`) even in terminal tombstones, so the 1024-tombstone ceiling
# preserves the prior capacity above the 1 MiB base.
_INTENT_NULL_OVERHEAD = 14
# Only the single active request carries this field: comma + quoted key +
# colon + quoted 64-hex digest. Terminal handback clears the request.
_AUTHORITY_DIGEST_OVERHEAD = 86
MAX_STATE_BYTES = (1024 * 1024 + MAX_BINDING_BYTES + _INTENT_NULL_OVERHEAD * MAX_JOBS
                   + _AUTHORITY_DIGEST_OVERHEAD)
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESULT_BYTES = 32768
MAX_TASK_BYTES = 65536
_STATES = {'queued', 'running', 'cancel-requested', 'terminal'}
_OUTCOMES = {'succeeded', 'failed', 'cancelled'}
_FIELDS = {'owner', 'key', 'request_digest', 'status', 'token', 'run_id', 'execution',
           'request', 'result', 'result_digest', 'binding', 'intent'}
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
    if type(value) is not dict or set(value) not in (
            {'role', 'task', 'profile', 'source_snapshot'},
            {'role', 'task', 'profile', 'source_snapshot', 'authority_digest'}):
        raise Denied('Invalid delegation request schema')
    if 'authority_digest' in value and (not _sha(value['authority_digest'])
                                       or value['profile'] is None):
        raise Denied('Invalid resolved delegation authority')
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


def _intent(value, run_id):
    """Private, single-container provisioning format; no launch authority.

    config is a nonempty bounded JSON inspection projection owned by the trusted
    supervisor. It must include every fixture launch option, not model input.
    Only the persisted intent is recovery authority: the bounded safe name is
    never derived post hoc, while the marker must equal run_id.
    """
    if (type(value) is not dict or set(value) != {
            'run_id', 'directory', 'scope', 'container', 'recovering', 'claimed', 'id', 'attempts'}
            or type(value['recovering']) is not bool or type(value['claimed']) is not bool
            or (value['recovering'] and value['claimed'])
            or (value['id'] is not None and (not value['recovering'] or not _sha(value['id'])))
            or type(value['container']) is not dict
            or set(value['container']) != {'name', 'uid', 'marker', 'image', 'config'}):
        raise Denied('Invalid delegation provisioning intent')
    attempts = value['attempts']
    if type(attempts) is not dict or set(attempts) != {'submit', 'create', 'start'}:
        raise Denied('Invalid delegation effect inventory')
    for phase, attempt in attempts.items():
        if (type(attempt) is not dict or set(attempt) != {'state', 'result'}
                or attempt['state'] not in ('not-issued', 'issued', 'completed')):
            raise Denied('Invalid delegation effect attempt')
        if attempt['state'] == 'completed':
            _effect_result(value, phase, attempt['result'])
        elif attempt['result'] is not None:
            raise Denied('Unexpected delegation effect result')
    if (attempts['create']['state'] != 'not-issued'
            and attempts['submit']['state'] != 'completed'):
        raise Denied('Invalid delegation effect ordering')
    if (attempts['start']['state'] != 'not-issued'
            and (attempts['submit']['state'] != 'completed'
                 or attempts['create']['state'] != 'completed')):
        raise Denied('Invalid delegation effect ordering')
    container = value['container']
    _binding({k: value[k] for k in ('run_id', 'directory', 'scope')} | {
        'containers': [{'name': container['name'], 'uid': container['uid'], 'id': '0' * 64}]}, run_id)
    # Safe bounded name is validated by _binding above; only the marker pins run_id.
    if (container['marker'] != run_id
            or not _sha(container['image']) or type(container['config']) is not dict
            or not container['config'] or len(canonical(value)) > MAX_BINDING_BYTES):
        raise Denied('Invalid delegation provisioning identity or config')


def _effect_result(intent, phase, result):
    # Identities only, never unbounded command output or a settlement assertion.
    if phase == 'submit':
        valid = result == f"mizu-delegation-{intent['run_id']}.service"
    elif phase == 'create':
        valid = _sha(result)
    else:
        created = intent['attempts']['create']
        valid = (created['state'] == 'completed' and _sha(result)
                 and result == created['result'])
    if not isinstance(result, str) or not valid:
        raise Denied('Invalid delegation effect identity result')


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


class _WorkspaceLease:
    """Host-only proof minted by Engine.run under its admitted write exclusion.

    The run ExitStack revokes this object *before* releasing admission. Its
    mutex keeps revocation from racing a commit. It is not a bearer/model grant:
    both the minting Engine and exact parent Context must match. No lock is
    acquired here; held() is only an additional sanity check, not mint authority.
    """

    def __init__(self, engine, parent, stack):
        self.root = parent.project.root.resolve(strict=True)
        self.owner = parent.role.name
        self.engine, self.parent = engine, parent
        self.mutex = threading.RLock()
        self.active = True
        self.attempt = None
        if parent.role.workspace != 'write' or not held(self.root / 'locks' / 'workspace.lock'):
            raise Denied('Workspace lease requires admitted write exclusion')
        stack.callback(self.close)

    def close(self):
        with self.mutex:
            self.active = False

    @contextlib.contextmanager
    def start_scope(self, project_root, owner, engine, parent, key, request):
        """Private reservation handoff, not launch authority or a model API.

        Only Engine's admission-minted live lease can use the already held
        workspace lock. Burn it before I/O; only an exact retry may follow an
        uncertain acknowledgement. Never clear a reservation on failure.
        The caller commits and refreshes its Context gate inside this scope,
        including on uncertain acknowledgement. Revocation waits for both;
        lock order is Context then lease, matching Context shutdown.
        """
        with parent.mutex, self.mutex:
            if (not self.active or self.engine is not engine or self.parent is not parent
                    or parent.closed or parent.finished is not None or not parent._slot_lease
                    or self.root != Path(project_root).resolve(strict=True)
                    or self.owner != owner or parent.role.name != owner
                    or parent.role.workspace != 'write'
                    or not held(self.root / 'locks' / 'workspace.lock')):
                raise Denied('Stale or mismatched admitted workspace lease')
            _identity(owner, key)
            request = loads_finite(canonical(request))
            _request(request)
            if request['source_snapshot'] != parent.snapshot['id']:
                raise Denied('Delegation source differs from parent snapshot')
            request = engine._delegation_request(parent, request)
            attempt = digest(canonical([key, _request(request)]))
            if self.attempt is not None and self.attempt != attempt:
                raise Denied('Workspace lease already used for another start')
            self.attempt = attempt
            yield request


class DelegationJobs:
    """One project's durable reservation. Construction/recovery never starts work.

    ``start/status/result/cancel`` require the authenticated owner role from the
    host, not a model-supplied identity. Owner views omit runner tokens/requests,
    the stable run identity, intent and binding. ``reservation`` is internal recovery
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
            write_json(self.root / 'state.json', {'version': 5, 'active': None, 'jobs': {}},
                       exclusive=True)
        with self._locked():
            self._load()

    @classmethod
    @contextlib.contextmanager
    def workspace_start(cls, project_root: Path):
        """Fence preparation and reservation against live-writer admission.

        Trusted coordinator API, not a model grant or runner: acquire the same
        workspace.lock as Engine admission, refuse existing/unknown reservations
        BEFORE yielding for source capture, and retain the lock through start's
        durable commit. Lock order is workspace then store. Contention is Busy,
        with no waiting, capture, or store initialization. The caller must start
        inside this scope; no tree access or launch authority escapes with it.

        An already admitted writer must retain its original workspace lock,
        not release/reacquire it with this API (locks are not reentrant). Use the
        private Engine._reserve_delegation handoff for that lease instead. On ANY
        exception, release only the process lock, never clear durable state:
        commit acknowledgement may be uncertain, and only reconciliation plus
        proven quiescence can release a committed reservation. Inspect owner status;
        retry exact low-level start under workspace exclusion, never repeat
        preparation against a held reservation.
        """
        project_root = Path(project_root)
        with lock(project_root / 'locks' / 'workspace.lock', blocking=False):
            cls.guard(project_root)
            yield cls(project_root)

    @classmethod
    def reservation_projection(cls, project_root):
        """Minimal trusted Context gate view; never return runner/task authority."""
        reservation = cls.existing_reservation(project_root)
        if reservation is None:
            return None
        return {key: reservation[key] for key in ('id', 'owner', 'status')} | {
            'request': {'source_snapshot': reservation['request']['source_snapshot']}}

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
                or type(state['version']) is not int or state['version'] != 5
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
            if job['intent'] is not None:
                _intent(job['intent'], job['run_id'])
                if len(canonical([job['intent'], job['binding']])) > MAX_BINDING_BYTES:
                    raise Denied('Delegation resource inventory exceeds byte bound')
                intent, binding = job['intent'], job['binding']
                if binding is not None and (binding['directory'] != intent['directory']
                        or binding['scope'] != intent['scope'] or len(binding['containers']) != 1
                        or binding['containers'][0]['name'] != intent['container']['name']
                        or binding['containers'][0]['uid'] != intent['container']['uid']
                        or (intent['recovering'] and binding['containers'][0]['id'] != intent['id'])):
                    raise Denied('Delegation binding differs from provisioning intent')
                if (intent['claimed'] and (binding is None or job['status'] == 'queued')
                        or (job['status'] == 'running' and job['execution'] is None
                            and not intent['claimed'])):
                    raise Denied('Invalid delegation provisioning claim')
                if intent['recovering'] and (job['status'] != 'cancel-requested'
                        or job['execution'] is not None):
                    raise Denied('Invalid delegation provisioning recovery')
                attempts = intent['attempts']
                if attempts['start']['state'] != 'not-issued':
                    if (not intent['claimed'] or binding is None
                            or binding['containers'][0]['id'] != attempts['create']['result']
                            or job['status'] == 'queued'):
                        raise Denied('Invalid delegation start linkage')
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
                if (job['binding'] is not None or job['intent'] is not None or execution is None
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
                   'binding': None, 'intent': None}
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

    def runner_prepare(self, job_id, token, run_directory, scope, container):
        """Commit a bounded synthetic provisioning intent BEFORE external create.

        Exact retry returns the original; uncertainty must be inspected, never
        interpreted as permission to create again. The marker pins run_id and the
        persisted intent alone is recovery authority; the bounded safe name is
        never derived post hoc. Only the trusted supervisor defines the
        expected image/config projection.
        """
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            intent = loads_finite(canonical({
                'run_id': job['run_id'], 'directory': _directory_identity(run_directory),
                'scope': scope, 'container': container, 'recovering': False, 'claimed': False, 'id': None,
                'attempts': {phase: {'state': 'not-issued', 'result': None}
                             for phase in ('submit', 'create', 'start')}}))
            _intent(intent, job['run_id'])
            if job['intent'] == intent:
                return intent
            if (job['intent'] is not None or job['binding'] is not None
                    or job['status'] != 'queued' or job['execution'] is not None):
                raise Denied('Conflicting or late delegation provisioning intent')
            job['intent'] = intent
            self._save(state)
            return intent

    def runner_effect(self, job_id, token, intent, phase, operation, *, authority_digest=None):
        """Admit one trusted external operation, NOT a model/runner grant.

        The internal controller supplies the exact current persisted intent and
        a bounded operation on its pinned identities, returning only the exact
        unit (submit) or full container ID (create/start). Submit is first while
        queued/unclaimed/unbound; create follows completed submit while
        queued/unclaimed/unbound; start follows completed submit/create after
        runner_bind recorded exactly the create-result ID for the intended
        name/UID and runner_started claimed the job (running, claimed). The
        callback is work, never evidence of cleanup/settlement. It must not
        re-enter this store or leave background submitters. No callback or
        result can release a hold. Persist issued BEFORE calling it; retain
        store.lock through result commit. Any exception/death/invalid result
        leaves issued and forbids all replay. Even completed calls are not
        retried. Caller time/output bounds remain mandatory; this admission
        primitive adds no timeout or polling policy.
        """
        if phase not in ('submit', 'create', 'start') or not callable(operation):
            raise Denied('Invalid delegation effect')
        intent = loads_finite(canonical(intent))
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            self._authority(job, authority_digest)
            saved = job['intent']
            if (saved is None or saved != intent
                    or job['execution'] is not None or saved['recovering']
                    or saved['scope']['unit'] != f"mizu-delegation-{job['run_id']}.service"
                    or _directory_identity(saved['directory']['path']) != saved['directory']
                    or any(a['state'] == 'issued' for a in saved['attempts'].values())
                    or saved['attempts'][phase]['state'] != 'not-issued'):
                raise Denied('Fenced, stale or ambiguous delegation effect; do not replay')
            attempts = saved['attempts']
            if phase == 'submit':
                if (job['status'] != 'queued' or saved['claimed'] or job['binding'] is not None
                        or attempts['submit']['state'] != 'not-issued'
                        or attempts['create']['state'] != 'not-issued'
                        or attempts['start']['state'] != 'not-issued'):
                    raise Denied('Fenced, stale or ambiguous delegation effect; do not replay')
            elif phase == 'create':
                if (job['status'] != 'queued' or saved['claimed'] or job['binding'] is not None
                        or attempts['submit']['state'] != 'completed'
                        or attempts['create']['state'] != 'not-issued'
                        or attempts['start']['state'] != 'not-issued'):
                    raise Denied('Fenced, stale or ambiguous delegation effect; do not replay')
            else:
                binding = job['binding']
                if (job['status'] != 'running' or not saved['claimed'] or binding is None
                        or len(binding['containers']) != 1
                        or binding['containers'][0]['name'] != saved['container']['name']
                        or binding['containers'][0]['uid'] != saved['container']['uid']
                        or binding['containers'][0]['id'] != attempts['create']['result']
                        or attempts['submit']['state'] != 'completed'
                        or attempts['create']['state'] != 'completed'
                        or attempts['start']['state'] != 'not-issued'):
                    raise Denied('Fenced, stale or ambiguous delegation effect; do not replay')
            attempt = saved['attempts'][phase]
            attempt['state'] = 'issued'
            self._save(state)
            result = operation()
            _effect_result(saved, phase, result)
            attempt.update(state='completed', result=result)
            self._save(state)
            return result  # immutable identity, not release authority

    def runner_cancel_effects(self, job_id, token, config):
        """Commit the launch fence before exact-unit systemd stop diagnostics.

        Only an active nonterminal reservation may be fenced; a terminal or
        tombstone job is refused without mutation. Lock contention/error
        retains the hold, not permission to cancel ahead of an admitted
        effect. The durable fence survives all systemd failures. This API
        never calls a handback path or treats completion as settlement.
        """
        from .delegation_systemd import _cancel_stop

        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            if job['status'] == 'terminal':
                raise Denied('Delegation reservation is terminal; no fence to commit')
            if job['status'] not in {'queued', 'running', 'cancel-requested'}:
                raise Denied('Delegation reservation is not active; no fence to commit')
            if job['status'] != 'cancel-requested':
                job['status'] = 'cancel-requested'
                self._save(state)
            run_id = job['run_id']
        # The fence is durable; release store.lock so a delayed helper can read
        # it and exit while the bounded synchronous systemd stop waits.
        return _cancel_stop(config, run_id)

    def runner_recover_prepared(self, job_id, token, *, container_id=None):
        """Fence provisioning/claim before inspection; never infer OS absence.

        This fences later ledger bind/claim/finish calls and waits for any
        runner_effect holding store.lock. Unmediated external work is not fenced. It cannot authorize cleanup or handback. A validated
        candidate ID may be saved before test-only removal; missing initial
        evidence and uncertain external settlement retain the reservation.
        """
        if container_id is not None and not _sha(container_id):
            raise Denied('Invalid delegation provisioning container ID')
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            intent = job['intent']
            if (intent is None or intent['claimed']
                    or job['status'] not in {'queued', 'cancel-requested'}
                    or job['execution'] is not None
                    or _directory_identity(intent['directory']['path']) != intent['directory']):
                raise Denied('Delegation provisioning cannot be recovered')
            if intent['id'] is not None and container_id not in (None, intent['id']):
                raise Denied('Conflicting delegation provisioning container ID')
            if job['binding'] is not None:
                saved_id = _bound(job)['containers'][0]['id']
                if container_id not in (None, saved_id):
                    raise Denied('Provisioning recovery differs from bound container ID')
                container_id = saved_id
            if intent['recovering'] and container_id in (None, intent['id']):
                return intent
            intent['recovering'] = True
            if container_id is not None:
                intent['id'] = container_id
            job['status'] = 'cancel-requested'
            self._save(state)
            return intent

    def runner_prepared_quiescent(self, job_id, token, result, *, cleanup):
        """Refuse handback until Mizu owns fencing and supervisor settlement.

        Caller-provided cleanup dictionaries are not OS/lifecycle proof, even
        with the exact persisted recovering intent, saved container ID and
        positive scope/container booleans. Validate the active token, exact
        persisted recovering intent, container ID and directory identity, but
        never clear the reservation on these assertions. No substrate is
        currently supported. An interrupted/retried call leaves the same
        durable reservation held. `runner_recover_prepared` remains the durable
        fence against bind/claim/finish.
        """
        _text(result, MAX_RESULT_BYTES)
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            intent = job['intent']
            if (intent is None or not intent['recovering'] or intent['id'] is None
                    or intent['claimed'] or job['status'] != 'cancel-requested'
                    or job['execution'] is not None
                    or _directory_identity(intent['directory']['path']) != intent['directory']
                    or type(cleanup) is not dict
                    or set(cleanup) != {'intent', 'scope_unstarted', 'container_absent'}
                    or canonical(cleanup['intent']) != canonical(intent)
                    or cleanup['container_absent'] != intent['id']
                    or cleanup['scope_unstarted'] is not True):
                raise Denied('Delegation provisioning cleanup proof missing or mismatched')
            raise Denied('Delegation handback unsupported: no Mizu-owned launch fence and supervisor settlement')

    def runner_bind(self, job_id, token, run_directory, scope, containers, *, authority_digest=None):
        """Persist exact execution identities before launch; never launch or discover.

        Only the trusted supervisor may supply the complete resource inventory.
        Scope is {unit, cgroup}; containers are exact rootless-Podman {name, id,
        uid} records, including every writable MCP/execution container (empty
        only when none can exist). Directory identity is captured without links.
        The binding and any intent share an 8 KiB inventory allowance above
        the 1 MiB base plus per-record intent overhead. While reserved, exact retries return the original; conflicting or
        late binds refuse. authority_digest must be freshly resolved by the trusted
        host, not copied from the ledger; missing/mismatched/legacy authority denies
        before mutation. Failed/uncertain writes require inspection, not another launch.
        """
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            self._authority(job, authority_digest)
            if job['intent'] is not None and job['intent']['recovering']:
                raise Denied('Delegation provisioning recovery is fenced')
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

    @staticmethod
    def _authority(job, authority_digest):
        saved = (job['request'] or {}).get('authority_digest')
        if not _sha(authority_digest) or saved != authority_digest:
            raise Denied('Missing or mismatched delegation authority; do not launch')

    def runner_started(self, job_id, token, *, authority_digest=None):
        """Consume the queued claim once BEFORE launch; uncertain claim never replays.

        A paused claimant can still launch after a terminal report: neither
        revokes outstanding external work. Uncertain claims never replay. The
        binding and freshly resolved matching authority_digest must precede the
        claim; legacy requests cannot be claimed. A queued cancellation cannot
        be claimed; supervisor reports no-execution cancellation then quiescence
        instead. An unbound cancellation remains held for reconciliation.
        """
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            self._authority(job, authority_digest)
            if job['status'] != 'queued' or job['binding'] is None:
                raise Denied('Delegation is unbound or not queued; do not replay execution')
            _bound(job)
            if job['intent'] is not None:
                job['intent']['claimed'] = True
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
            if job['intent'] is not None and job['intent']['recovering']:
                raise Denied('Delegation provisioning recovery is fenced')
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
        """Refuse handback until Mizu owns fencing and supervisor settlement.

        Caller-provided cleanup dictionaries are not OS/lifecycle proof, even
        with the exact saved binding and every container ID. Validate the active
        token, terminal execution and directory identity, but never clear the
        reservation on these assertions. No substrate is currently supported.
        An interrupted/retried call leaves the same durable reservation held.
        """
        _text(result, MAX_RESULT_BYTES)
        with self._locked():
            state = self._load()
            job = self._runner(state, job_id, token)
            if job['execution'] is None:
                raise Denied('Delegation execution is not terminal')
            _bound(job)
            raise Denied('Delegation handback unsupported: no Mizu-owned launch fence and supervisor settlement')

    @staticmethod
    def _terminal(state, job, result, outcome):
        for old in state['jobs'].values():
            old['result'] = None
        job.update(status='terminal', token=None, request=None, binding=None, intent=None,
                   execution=outcome, result=result, result_digest=digest(result.encode()))
        state['active'] = None
