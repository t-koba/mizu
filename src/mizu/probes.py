"""Paid recovery events, not work units. No routing, health or publication paths."""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import shutil
import time
import uuid
from pathlib import Path

from .admission import role_exclusion
from .budget import Budget
from .config import Role, load, role_requests_per_run, role_tools_per_run
from .drivers import driver_for
from .engine_config import adapter_digest, effective, local_settings_digest
from .errors import Busy, Cancelled, ConfigError, Denied, InfraExceeded, LimitExceeded
from .fs import canonical, digest, lock, mkdir, read_json
from .project import Project
from .selection import (INTENT_RESERVE_BYTES, State, active, bounded, consume_intent_reservation,
                        prune_probe_events, terminal_intent)

# Execution ceilings, not a quota policy. Every admission still uses the real
# sponsor Budget and smaller configured limits win. Work per tick: one event.
MAX_SECONDS = 90
MAX_REQUESTS = 2
MAX_TOOLS = 1
POLICY = ('This is an availability challenge, not a project task. Call mizu_finish '
          'once with outcome="done" and summary exactly equal to the challenge. '
          'Do not use other tools or provide any other content.\n')


def probe_role(profile, policy=()):
    return Role('recovery-probe', profile, tuple(policy), 'none', ('finish',),
                requests_per_run=MAX_REQUESTS, tools_per_run=MAX_TOOLS)


def _locator_binding(path):
    """Hash a resolved auth/settings locator without opening its contents."""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigError('Probe source locator unavailable') from exc
    return digest(str(resolved).encode())


def target_binding(config, profile):
    """Opaque binding; no argv, settings paths or credential values in evidence."""
    settings = effective(config, probe_role(profile), profile)
    engine = settings['engine']
    # Bind locators, not secret content: refreshes within a source are allowed.
    sources = {'directory': _locator_binding(config.agent_dir(engine))}
    if engine in ('pi', 'pi-durable'):
        sources['credentials_env'] = _locator_binding(config.file.parent / 'credentials.env')
    if engine == 'codex':
        from .codex import auth_source
        sources['auth_file'] = _locator_binding(auth_source())
        provider = settings['options'].get('model_providers', {}).get(settings['provider'], {})
        if provider.get('env_key') or provider.get('env_http_headers'):
            sources['credentials_env'] = _locator_binding(config.file.parent / 'credentials.env')
    command = config.command(engine)
    executable = shutil.which(command[0])
    if not executable:
        raise ConfigError('Probe command unavailable')
    path = Path(executable)
    if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
        raise ConfigError('Probe executable exceeds identity bound')
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            sha.update(chunk)
    return digest(canonical({'profile': profile, 'settings': settings, 'command': command,
                             'executable': sha.hexdigest(), 'adapter': adapter_digest(settings['engine']),
                             'native': local_settings_digest(config, engine), 'sources': sources}))


def sponsor_binding(config, sponsor, role):
    root = config.data / 'projects' / sponsor
    path = root / 'project.toml'
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ConfigError('Probe sponsor unavailable')
    project = Project(config, sponsor)
    if role not in project.roles or role not in config.roles:
        raise ConfigError('Probe sponsoring role unavailable')
    stat = root.stat()
    return digest(canonical({'project': sponsor, 'role': role, 'device': stat.st_dev,
                             'inode': stat.st_ino, 'settings': digest(path.read_bytes())}))


def authorization(config, c, stop):
    """Stable reason codes only; never persist exception messages/native paths."""
    if stop.is_set():
        return 'stopped', None
    reason, project = sponsor_authorization(config, c)
    return (reason or target_authorization(config, c)), project


def sponsor_authorization(config, c):
    """Sponsor check, also usable for read-only status (never admission caching)."""
    try:
        if sponsor_binding(config, c['sponsor'], c['role']) != c['sponsor_binding']:
            return 'sponsor_changed', None
        project = Project(config, c['sponsor'])
    except (ConfigError, OSError, ValueError):
        return 'sponsor_missing', None
    control = project.control()
    if not control.get('armed'):
        return 'unarmed', project
    if control.get('paused'):
        return 'paused', project
    if control.get('draining'):
        return 'draining', project
    return None, project


def target_authorization(config, c):
    """Exact target check; status may deduplicate it within one read only."""
    try:
        if target_binding(config, c['profile']) != c['target']:
            return 'target_changed'
        settings = effective(config, probe_role(c['profile']), c['profile'])
        # Profiles with extension/native surfaces cannot be silently reduced
        # to a different target. Refuse rather than launch external servers or
        # extensions for an availability event.
        options = settings['options']
        if (settings['resources'] or settings['mcp_servers'] or options.get('plugins')
                or options.get('skills') or options.get('hooks')
                or options.get('codemode') or options.get('toolSearch')):
            return 'native_tools_incompatible'
    except (ConfigError, OSError, ValueError, KeyError):
        return 'target_missing'
    return None


def _summary(model):
    """Numeric usage only, no raw output or arbitrary provider strings."""
    if not isinstance(model, dict):
        return None
    from .usage import normalize
    totals = {}
    usage = model.get('usage')
    known = model.get('usage_known') is True and isinstance(usage, list) and 0 < len(usage) <= MAX_REQUESTS
    for value in model.get('usage', [])[:MAX_REQUESTS] if isinstance(model.get('usage'), list) else []:
        if not isinstance(value, dict):
            known = False
            continue
        normalized = normalize(value, model.get('engine', 'unknown'))
        known = known and not normalized['unknown_shape']
        for key, count in normalized.items():
            if type(count) in (int, float) and 0 <= count <= 2**53:
                totals[key] = totals.get(key, 0) + count
    return {'usage_known': bool(known), 'tokens': totals}


def _exact_result(config, c, model):
    expected = {'profile': c['profile'], 'engine': config.engine(c['profile']), **config.model(c['profile'])}
    if not isinstance(model, dict) or any(model.get(k) != v for k, v in expected.items()):
        return False
    # Explicit contrary observations are not proof that this exact target
    # recovered (unknown attribution remains unknown, not fabricated).
    for field in ('usage_observations', 'observed_models'):
        observations = model.get(field, [])
        if not isinstance(observations, list):
            return False
        for observation in observations:
            if not isinstance(observation, dict):
                return False
            for key in ('provider', 'model'):
                if observation.get(key) is not None and observation[key] != expected[key]:
                    return False
    return True


def _probe_limits(config, role_name):
    role = config.roles[role_name]
    return dataclasses.replace(config.limits, run_seconds=min(MAX_SECONDS, config.limits.run_seconds),
                               requests_per_run=min(MAX_REQUESTS, role_requests_per_run(config, role)),
                               tools_per_run=min(MAX_TOOLS, role_tools_per_run(config, role)),
                               event_stream_bytes=min(65536, config.limits.event_stream_bytes))


def _budget_available(config, project):
    # A preflight only, not a reservation. The real take still arbitrates spend.
    usage = Budget(config.data / 'budget', config.limits.daily_requests, config.limits.retention_days,
                   config.limits.shared_daily_requests, config.timezone).usage(project.name)
    if (usage['used'] >= usage['limit'] or
            usage['shared_limit'] > 0 and usage['shared_used'] >= usage['shared_limit']):
        raise InfraExceeded('Recovery budget unavailable')


def _event(identity, c, at):
    return {'contribution': identity, 'revision': c['revision'], 'target': c['target'],
            'started_at': at, 'finished_at': None, 'status': 'intent', 'admitted': False,
            'settled': False, 'challenge_ok': False, 'requests': 0, 'model': None, 'reason': None,
            'reserve': ' ' * INTENT_RESERVE_BYTES}


def next_deadline(config, project_name, *, at=None):
    """Return the earliest deadline for any contribution sponsored by this project."""
    at = time.time() if at is None else at
    state = State(config).read()
    if state['exhausted']:
        return None
    times = []
    for c in state['contributions'].values():
        if active(c, at) and c['sponsor'] == project_name:
            times.append(c['next_probe_at'])
            if c['until'] is not None:
                times.append(c['until'])
    return min(times, default=None)


def service_due(config, project_name, stop, *, resolve=None, reload_config=None, at=None):
    """Service one due project contribution before a daemon's readiness gate.

    Any daemon for the project may service it; _service still revalidates the
    stored sponsor and role and uses their locks, budget and exact target. A
    persisted intent plus an obligation lock prevents duplicate spending. An
    abandoned intent is settled as unknown only after the live ceiling; its
    next attempt waits a full probe interval. Terminal success is never replayed
    after a crash to guess whether recovery was applied.
    """
    clock = time.time if at is None else lambda: at
    at = clock()
    store = State(config)
    due = sorted((c['next_probe_at'], identity) for identity, c in store.read()['contributions'].items()
                 if c['status'] == 'active' and c['sponsor'] == project_name
                 and (c['next_probe_at'] <= at or c['until'] is not None and c['until'] <= at))
    for _, identity in due:
        try:
            with lock(store.root / 'claims' / (identity + '.lock'), blocking=False):
                return _service(config, store, identity, stop, resolve, reload_config, at, clock)
        except Busy:
            # Do not walk the entire backlog or race a live executor.
            return None
    return None


def _service(config, store, identity, stop, resolve, reload_config, at, clock):
    with lock(store.root / 'state.lock'):
        previous = store.read()
        state = copy.deepcopy(previous)
        c = state['contributions'][identity]
        if c['status'] != 'active':
            return None
        if c['until'] is not None and c['until'] <= at:
            if c['claim']:
                claim = c['claim']
                e = state['events'][claim]
                if e['status'] == 'intent':
                    e = terminal_intent(e, {'status': 'unknown', 'finished_at': at,
                                             'reason': 'upper_expiry'})
                    state['events'][claim] = e
                c['last_event'] = claim
                shutil.rmtree(store.root / 'work' / claim, ignore_errors=True)
            c.update(status='expired', revision=c['revision'] + 1, claim=None, reason='upper_expiry')
            prune_probe_events(state, identity)
            store.save_or_close(state, previous)
            return {'event': 'recovery_probe', 'status': 'expired', 'contribution': identity}
        if c['claim']:
            e = state['events'][c['claim']]
            if e['status'] == 'intent' and at < e['started_at'] + MAX_SECONDS:
                return None
            if e['status'] == 'intent':
                work = store.root / 'work' / c['claim']
                admission = work / 'admission.json'
                updates = {}
                if admission.is_file() and not admission.is_symlink() and admission.stat().st_size <= 1024:
                    evidence = read_json(admission)
                    requests = evidence.get('requests')
                    if type(requests) is int and 0 <= requests <= MAX_REQUESTS:
                        updates.update(requests=requests, admitted=requests > 0)
                e = consume_intent_reservation(e, updates)
                e = terminal_intent(e, {'status': 'unknown', 'finished_at': at, 'reason': 'interrupted'})
                state['events'][c['claim']] = e
            shutil.rmtree(store.root / 'work' / c['claim'], ignore_errors=True)
            c.update(claim=None, last_event=c['claim'], revision=c['revision'] + 1,
                     next_probe_at=at + c['probe_seconds'], reason='uncertain_event')
            prune_probe_events(state, identity)
            store.save_or_close(state, previous)
            return {'event': 'recovery_probe', 'status': 'unknown', 'contribution': identity}
        if c['next_probe_at'] > at:
            return None
        if state['exhausted']:
            return {'event': 'recovery_probe', 'status': 'deferred', 'reason': 'selection_capacity_exhausted'}
        claim = digest(uuid.uuid4().bytes)
        c.update(claim=claim, revision=c['revision'] + 1, next_probe_at=at + MAX_SECONDS)
        event = _event(identity, c, at)
        state['events'][claim] = event
        prune_probe_events(state, identity)
        # Intent is durable before locks, admission or driver launch.
        store.save_or_close(state, previous)
        c = copy.deepcopy(c)
    def fresh_authorization():
        updated = reload_config() if reload_config else load(config.file)
        if updated.data != config.data:
            return updated, 'sponsor_changed', None
        reason, project = authorization(updated, c, stop)
        return updated, reason, project

    reason = None
    context = model = None
    status = 'deferred'
    work = store.root / 'work' / claim
    try:
        current, reason, project = fresh_authorization()
        if reason is None:
            slot_limit = current.limits.parallel_runs
            from .runtime import Context, slot

            class ProbeContext(Context):
                def cancelled(self):
                    return super().cancelled() or self.project.control().get('draining', False)

                def revalidate_admission(self):
                    nonlocal reason
                    fresh, reason, sponsor = fresh_authorization()
                    if reason is not None:
                        raise Denied('Recovery authority revoked before admission')
                    if fresh.limits.parallel_runs != slot_limit:
                        reason = 'configuration_changed'
                        raise Denied('Recovery slot configuration changed')
                    limits = _probe_limits(fresh, c['role'])
                    self.config = dataclasses.replace(fresh, limits=limits)
                    self.project = sponsor
                    self.role = dataclasses.replace(self.role, requests_per_run=limits.requests_per_run,
                                                    tools_per_run=limits.tools_per_run)
                    self.deadline = min(self.deadline, launched_at + limits.run_seconds)
                    if self.cancelled():
                        raise Cancelled('Recovery cancelled before admission')
                    _budget_available(self.config, sponsor)

                def recovery_progress(self):
                    model = {'engine': self.config.engine(c['profile']),
                             'usage_known': bool(self.runtime_usage), 'usage': list(self.runtime_usage.values())}
                    store.probe_progress(identity, revision=c['revision'], claim=claim,
                                         requests=self.request_count, model=_summary(model))

                def handle(self, operation, arguments):
                    with self.mutex:
                        if operation == '_engine_tool':
                            raise Denied('Recovery events have no native tools')
                        if operation == '_model_usage':
                            sequence = arguments.get('sequence')
                            if (self.execution_attempt, sequence) not in self.request_sequences:
                                raise Denied('Recovery usage requires an admitted request')
                        if operation == '_budget':
                            self.revalidate_admission()
                            sequence = arguments.get('sequence')
                            if ((self.execution_attempt, sequence) not in self.request_sequences
                                    and self.request_count >= self.config.limits.requests_per_run):
                                self.bound_hit = 'requests'
                                self.admission_error = 'request_ceiling'
                                raise LimitExceeded('Recovery request ceiling reached')
                        if operation == 'finish':
                            if set(arguments) != {'summary', 'outcome'} or arguments.get('outcome') != 'done':
                                raise Denied('Recovery events accept only the challenge seal')
                            if self.tool_count >= self.config.limits.tools_per_run:
                                raise LimitExceeded('Recovery tool ceiling reached')
                        result = super().handle(operation, arguments)
                        if operation in ('_budget', '_model_usage'):
                            self.recovery_progress()
                        return result

            with contextlib.ExitStack() as stack:
                stack.enter_context(lock(role_exclusion(project.root, c['role']), blocking=False))
                writer_locked = current.roles[c['role']].workspace == 'write'
                if writer_locked:
                    stack.enter_context(lock(project.root / 'locks' / 'workspace.lock', blocking=False))
                stack.enter_context(slot(current))
                current, reason, project = fresh_authorization()
                if reason is None and current.limits.parallel_runs != slot_limit:
                    reason = 'configuration_changed'
                if reason is None and not writer_locked and current.roles[c['role']].workspace == 'write':
                    stack.enter_context(lock(project.root / 'locks' / 'workspace.lock', blocking=False))
                    current, reason, project = fresh_authorization()
                    if reason is None and current.limits.parallel_runs != slot_limit:
                        reason = 'configuration_changed'
                if reason is None:
                    _budget_available(current, project)
                    if shutil.disk_usage(project.root).free < current.limits.free_disk_mb * 1048576:
                        raise InfraExceeded('Recovery disk reserve unavailable')
                    mkdir(work.parent)
                    # The named disposable directory keeps Budget admission ids
                    # bound to the persisted claim, never to a normal run.
                    mkdir(work)
                    policy = work / 'policy.md'
                    policy.write_text(POLICY, encoding='utf-8')
                    limits = _probe_limits(current, c['role'])
                    probe_config = dataclasses.replace(current, limits=limits)
                    pr = dataclasses.replace(probe_role(c['profile'], (policy,)),
                                             requests_per_run=limits.requests_per_run, tools_per_run=limits.tools_per_run)
                    workspace = work / 'empty'
                    mkdir(workspace)
                    launched_at = time.monotonic()
                    context = ProbeContext(probe_config, project, pr, work, {'goal': ''}, workspace, stop=stop, goal='')
                    context.ephemeral = True
                    context.recovery_probe = True
                    challenge = 'mizu-recovery-' + claim
                    driver = resolve(c['profile']) if resolve else driver_for(probe_config, c['profile'], {})
                    model = driver.execute(context, canonical({'challenge': challenge}).decode(), profile=c['profile'])
                    event['settled'] = isinstance(model, dict) and model.get('settled') is True
                    event['challenge_ok'] = bool(context.finished and context.finished['summary'] == challenge
                                                 and context.finished['outcome'] == 'done')
                    if context.cancelled():
                        raise Cancelled('Recovery cancelled')
                    _, reason, _ = fresh_authorization()
                    if reason is None and not _exact_result(current, c, model):
                        reason = 'wrong_target'
                    if reason is None and not event['settled']:
                        reason = 'missing_terminal'
                    if reason is None and not event['challenge_ok']:
                        reason = 'wrong_challenge'
                    if reason is None and (context.request_count < 1 or context.admission_error
                                           or context.bound_hit is not None):
                        reason = 'admission_unconfirmed'
                    status = 'success' if reason is None else 'failure'
    except Busy:
        reason, status = 'locks_unavailable', 'deferred'
    except InfraExceeded:
        reason, status = 'budget_unavailable', 'deferred'
    except Cancelled:
        reason, status = 'cancelled', 'cancelled'
    except (ConfigError, Denied):
        reason, status = reason or 'configuration_denied', 'deferred'
    except Exception:
        # No arbitrary error string/raw completion text in shared evidence.
        reason, status = 'driver_failed', 'failure'
    finally:
        if context is not None:
            context.closed = True
            event['requests'] = context.request_count
            event['admitted'] = context.request_count > 0
            evidence = model or getattr(context, 'model_evidence', None)
            if not evidence and context.runtime_usage:
                evidence = {'engine': config.engine(c['profile']), 'usage_known': True,
                            'usage': list(context.runtime_usage.values())}
            event['model'] = _summary(evidence)
        shutil.rmtree(work, ignore_errors=True)
    at = clock()
    event.update(status=status, reason=reason, finished_at=at)
    event.pop('reserve', None)
    bounded(event, 4096)
    if store.probe_update(identity, revision=c['revision'], claim=claim, event=event, at=at):
        store.probe_update(identity, revision=c['revision'], claim=claim, recover=status == 'success', at=at, reason=reason)
    return {'event': 'recovery_probe', 'id': claim, 'contribution': identity, 'status': status,
            'reason': reason, 'requests': event['requests']}
