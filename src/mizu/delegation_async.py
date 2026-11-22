"""Mizu-owned single-child in-process delegation start/join over the job ledger.

This is the opt-in async entrypoint, not a scheduler or a general runner. The
child is an ephemeral Engine execution thread inside the admitted caller
process: it shares the caller's workspace lease, slot and spend ledger, and
uses the durable reservation ledger as its only durable state. No external
scope, worktree, container or background process is created by this module.
Capabilities that could create them are refused before launch.

The durable claim precedes the thread. Recovery never relaunches a claimed job;
a lost process leaves the reservation held and join stays pending. Handback
commits only after the thread has ended, every Mizu-owned driver channel is
quiescent and its process group is absent. All proofs are recomputed by trusted
code under the ledger lock, not supplied by the model.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .admission import role_exclusion
from .delegation_jobs import DelegationJobs, MAX_RESULT_BYTES, _directory_identity
from .engine_config import effective
from .errors import Busy, Denied
from .fs import canonical, digest, loads_finite, lock

#: Capabilities that can spawn processes, containers, writers other than the
#: one child tree, or another collaboration. Async children are file-edit
#: workers only; the caller keeps verification and publication.
FORBIDDEN = frozenset({"delegate", "delegate_join", "consult", "exec", "experiment",
                       "verify", "materialize", "sync", "check_job", "report",
                       "research", "queues", "work_order", "work_items_create",
                       "work_items_transition", "work_items_reopen",
                       "vcs_publish", "vcs_request", "vcs_retire", "vcs_dispose",
                       "review_verdict", "feed_updates"})

_CHILDREN: dict[str, _Child] = {}
_LOCK = threading.RLock()


@dataclass
class _Child:
    job_id: str
    token: str
    run_id: str
    run_dir: Path
    task: str
    request: dict
    store: DelegationJobs
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    context: object | None = None
    guard: contextlib.ExitStack | None = None
    external_resources: bool = True

    def proof(self):
        """Recompute teardown facts; never accept caller assertions."""
        channels = tuple(getattr(self.context, "_driver_channels", ()) or ())
        return {
            'thread_done': self.done.is_set(),
            'drivers_quiescent': all(channel.quiescent is True for channel in channels),
            'process_groups_absent': all(_group_absent(channel.process.pid) for channel in channels),
            'no_external_resources': self.external_resources is False,
            'directory': _directory_identity(str(self.run_dir)),
        }


def _group_absent(pid) -> bool:
    """A driver runs in its own session; its session leader's group must be gone."""
    if not hasattr(os, 'killpg') or type(pid) is not int or pid <= 1:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _gate(engine, role, profile):
    """Refuse an async child that could leave any substrate we cannot prove."""
    if (set(role.capabilities) & FORBIDDEN or role.engine_tools
            or role.persistent_exec):
        raise Denied('Async delegation child requires a file-only role without native tools')
    settings = effective(engine.config, role, profile)
    if settings['mcp_servers']:
        raise Denied('Async delegation child cannot use MCP servers')


def _live_run(store: DelegationJobs, job_id: str):
    """Exact child run identity from the live ledger reservation, if present."""
    reservation = store.reservation()
    if reservation is not None and reservation.get('id') == job_id:
        return reservation['run_id']
    return None


def _clear_gate(parent, job_id: str) -> None:
    with parent.mutex:
        if (parent._reservation_view is not None
                and parent._reservation_view.get('id') == job_id):
            parent._reservation_view = None


def _abort(child: _Child, parent, reason: str) -> None:
    """Seal a never-launched reservation and remove its transient run tree."""
    child.done.set()
    child.external_resources = False
    # The directory identity is part of the durable handback proof even when
    # the child never started; create it, prove it, then remove it below.
    child.run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    sealed = False
    try:
        child.store.runner_finished(child.job_id, child.token, 'failed')
        child.store.runner_quiescent_inprocess(child.job_id, child.token,
            canonical({'error': reason, 'status': 'interrupted'}).decode('utf-8'),
            run_id=child.run_id, probe=child.proof)
        sealed = True
        _clear_gate(parent, child.job_id)
    finally:
        if child.guard is not None:
            child.guard.close()
        # An uncertain handback keeps the directory for reconciliation; only
        # a committed tombstone makes this transient tree safe to remove.
        if sealed:
            shutil.rmtree(child.run_dir, ignore_errors=True)


def start(engine, parent, key: str, role_name: str, task: str, snap: dict) -> dict:
    """Reserve, claim and launch one in-process child; return its durable id."""
    from .runtime import mkdir, now, write_json

    owner = parent.role.name
    store = DelegationJobs(parent.project.root)
    job_id = digest(canonical([owner, key]))
    try:
        view = store.status(owner, job_id)
    except Denied:
        view = None
    if view is not None and view['status'] == 'terminal':
        return join(parent, job_id)
    if view is not None:
        # An exact retry (or a restart with a held reservation) never launches
        # another child. Its run identity is still live in the reservation;
        # fail closed before fencing if that exact identity is unavailable.
        run = _live_run(store, job_id)
        if run is None:
            raise Denied('Delegation run identity is unavailable')
        with parent.mutex:
            parent._reservation_view = DelegationJobs.reservation_projection(parent.project.root)
        return view | {'run': run}

    request = {'role': role_name, 'task': task,
               'profile': None, 'source_snapshot': snap['id']}
    with parent.mutex:
        view = engine._reserve_delegation(parent, key, request)
    job_id = view['id']
    child = None
    try:
        with _LOCK:
            if job_id in _CHILDREN:
                return {'id': job_id, 'status': 'running', 'reserved': True,
                        'run': _CHILDREN[job_id].run_id}
            reservation = store.reservation()
            if (reservation is None or reservation['id'] != job_id
                    or reservation['owner'] != owner):
                raise Denied('Delegation reservation is no longer active')
            run_id = reservation['run_id']
            run_dir = parent.project.root / 'runs' / run_id
            child = _Child(job_id, reservation['token'], run_id, run_dir, task,
                           request, store)
            fresh = engine._resolve_delegation_request(parent.project, owner, reservation['request'])
            role = engine._delegate_role(parent.role, fresh['role'])
            profile = fresh['profile']
            _gate(engine, role, profile)
            if run_dir.exists():
                raise Denied('Delegation child directory already exists')
            guard = contextlib.ExitStack()
            try:
                guard.enter_context(lock(role_exclusion(parent.project.root, role.name),
                                         blocking=False))
            except Busy as exc:
                guard.close()
                raise Denied('The delegate role is active; async delegation needs it free') from exc
            child.guard = guard
            child.request = fresh
            mkdir(run_dir)
            write_json(run_dir / 'started.json', {'run': run_id, 'parent_run': parent.run_dir.name,
                       'delegation': True, 'role': role.name, 'profile': profile,
                       'snapshot': snap['id'], 'workspace': str(parent.workspace),
                       'started_at': now()})
            store.runner_claim_inprocess(job_id, child.token,
                                         authority_digest=fresh['authority_digest'], run_id=run_id)
            child.external_resources = False
            # The claim is durable before any thread exists; a restart can only
            # observe a running job and never relaunches it.
            child.thread = threading.Thread(
                target=_run, args=(engine, parent, child, role, profile, snap),
                name='mizu-delegation-' + run_id, daemon=True)
            try:
                child.thread.start()
            except OSError as exc:
                _abort(child, parent, 'Delegation child thread did not start')
                raise Denied('Delegation child thread did not start') from exc
            _CHILDREN[job_id] = child
    except BaseException as exc:
        if child is None:
            child = _Child(job_id, (store.reservation() or {}).get('token', ''),
                           (store.reservation() or {}).get('run_id', ''),
                           parent.project.root / 'runs' / ((store.reservation() or {}).get('run_id') or 'missing'),
                           task, request, store)
        with _LOCK:
            _CHILDREN.pop(job_id, None)
        if child.token:
            _abort(child, parent, str(exc))
        raise
    return {'id': job_id, 'status': 'running', 'reserved': True, 'run': run_id}


def _bounded_result(record: dict) -> str:
    """Keep the exact child record when it fits; a bounded refusal when not."""
    raw = canonical(record)
    if len(raw) <= MAX_RESULT_BYTES:
        return raw.decode('utf-8')
    bounded = {key: str(record.get(key, ''))[:64]
               for key in ('role', 'run', 'snapshot') if key in record}
    bounded.update(status='bounded', error='Delegation result exceeded handback bound')
    return canonical(bounded).decode('utf-8')


def _seal(child: _Child, record: dict, outcome: str) -> None:
    result = _bounded_result(record)
    try:
        child.store.runner_finished(child.job_id, child.token, outcome)
        child.store.runner_quiescent_inprocess(child.job_id, child.token, result,
                                               run_id=child.run_id, probe=child.proof)
    except Denied:
        # A terminal ledger or a missing proof cannot be repaired here. The
        # reservation stays held and join reports pending, never a result.
        pass


def _run(engine, parent, child: _Child, role, profile: str, snap: dict) -> None:
    outcome = 'succeeded'
    try:
        def capture(ctx):
            child.context = ctx
        record = engine._delegate_child(parent, role, child.task, snap, child.run_dir,
                                        resolved_profile=profile, context_sink=capture,
                                        role_guard=child.guard)
    except BaseException as exc:
        outcome = 'failed'
        record = {'task': child.task, 'role': role.name, 'run': child.run_id,
                  'snapshot': snap['id'], 'status': 'interrupted', 'error': str(exc)}
    finally:
        child.done.set()
    try:
        context = child.context
        if context is not None:
            context.closed = True
            context.close_exec()
            for channel in tuple(getattr(context, '_driver_channels', ())):
                channel.close()
    except BaseException:
        outcome = 'failed'
    _seal(child, record, outcome)


def join(context, job_id: str) -> dict:
    """Return the exact terminal child record, or a typed pending status."""
    from .runtime import Busy

    store = DelegationJobs(context.project.root)
    view = store.status(context.role.name, job_id)
    if view['status'] != 'terminal' or not view['result_available']:
        if context._reservation_view is not None and context._reservation_view.get('id') != job_id:
            raise Busy('Another delegation reservation is active')
        response = {'id': job_id, 'status': view['status'], 'reserved': True,
                    'result_available': False}
        run = _live_run(store, job_id)
        if run is not None:
            response['run'] = run
        return response
    result = loads_finite(store.result(context.role.name, job_id)['result'].encode('utf-8'))
    with context.mutex:
        if (context._reservation_view is not None
                and context._reservation_view.get('id') == job_id):
            context._reservation_view = None
    with _LOCK:
        _CHILDREN.pop(job_id, None)
    return result
