"""Durable sandbox checks on frozen inputs, independent of model work units.

Only explicitly granted check jobs use this queue. They never execute on the
host or mutate the live workspace. A separate runner owns compute slots; a model
can continue work or finish while its job runs. Results are not acceptance until
claimed against the current source and execution configuration.
"""
from __future__ import annotations
import contextlib
import hashlib
import stat
import os
import re
import dataclasses
import shutil
import uuid
from pathlib import Path
from .errors import Busy, Denied
from .fs import text_preview, canonical, digest, lock, mkdir, now, read_json, write_json, DIGEST
from .sandbox import Sandbox, cleanup


def execution_key(config):
    sandbox = dataclasses.asdict(config.sandbox)
    # Sharing controls do not change command execution or compiler artifacts.
    # Keep pre-sharing queued jobs and evidence valid across feature activation.
    for key in ('check_reuse', 'check_input_files', 'check_input_bytes'):
        sandbox.pop(key, None)
    return digest(canonical({'sandbox': sandbox,
                             'command_seconds': config.limits.command_seconds,
                             'output_bytes': config.limits.output_bytes}))


def root(project):
    return project.root / 'check-jobs'


def raw_read(project, job_id):
    if not isinstance(job_id, str) or not DIGEST.fullmatch(job_id):
        raise Denied('Invalid check job ID')
    value = read_json(root(project) / job_id / 'job.json')
    if not isinstance(value, dict) or value.get('id') != job_id:
        raise Denied('Check job unavailable')
    return value



def read(project, job_id):
    job = raw_read(project, job_id)
    if job.get('shared_job') and not job.get('detached'):
        original = raw_read(project, job['shared_job'])
        for key in ('status', 'passed', 'error', 'finished_at'):
            if key in original:
                job[key] = original[key]
    elif job.get('detached'):
        job.update(status='cancelled', passed=False)
    return job



def save_execution(project, job):
    # Execution progress and subscription state have different writers.
    with lock(project.root / 'locks/check-jobs.lock'):
        latest = raw_read(project, job['id'])
        for key in ('detached', 'acknowledged'):
            if key in latest:
                job[key] = latest[key]
        write_json(root(project) / job['id'] / 'job.json', job)


def environment(project):
    """Fingerprint observed local dependencies; never share network/tag inputs.

    File digests are cached only against complete local stat identities. The
    identity itself also participates in the receipt, so even a dependency
    changed and restored during execution invalidates it on POSIX filesystems.
    Unsupported files or concurrent edits disable reuse rather than execution.
    """
    config = project.config
    if not config.sandbox.check_reuse or config.sandbox.network != 'none' or not re.fullmatch(r'(?:.*@)?sha256:[a-f0-9]{64}', config.sandbox.image):
        return None
    def stamp(st):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with lock(project.root / 'locks/check-inputs.lock'):
        cache_path = project.root / 'check-inputs.json'
        cached = read_json(cache_path, {})
        entries = {}
        total_bytes = 0
        try:
            for mount in config.sandbox.mounts:
                base = Path(mount['source'])
                pending = [base]
                while pending:
                    path = pending.pop()
                    if len(entries) >= config.sandbox.check_input_files:
                        return None
                    st = path.lstat()
                    identity = list(stamp(st))
                    key = str(path)
                    if stat.S_ISDIR(st.st_mode):
                        children = sorted(path.iterdir())
                        pending.extend(reversed(children))
                        value = {'stat': identity, 'children': [c.name for c in children]}
                    elif stat.S_ISREG(st.st_mode):
                        total_bytes += st.st_size
                        if total_bytes > config.sandbox.check_input_bytes:
                            return None
                        old = cached.get(key, {})
                        if os.name == 'posix' and old.get('stat') == identity and old.get('digest'):
                            content = old['digest']
                        else:
                            with path.open('rb') as handle:
                                if stamp(os.fstat(handle.fileno())) != tuple(identity):
                                    return None
                                content = hashlib.file_digest(handle, 'sha256').hexdigest()
                        value = {'stat': identity, 'digest': content}
                    elif stat.S_ISLNK(st.st_mode):
                        target = os.readlink(path)
                        if Path(target).is_absolute() or not (path.parent / target).resolve().is_relative_to(base.resolve()):
                            return None
                        value = {'stat': identity, 'symlink': target}
                    else:
                        return None
                    if stamp(path.lstat()) != tuple(identity):
                        return None
                    entries[key] = value
            # Catch additions/removals or edits while traversing another subtree.
            if any(list(stamp(Path(k).lstat())) != v['stat'] for k, v in entries.items()):
                return None
        except OSError:
            return None
        if entries != cached:
            write_json(cache_path, entries)
        return digest(canonical(entries))


def reusable(job):
    return (not job.get('shared_job') and not job.get('detached')
            and (job.get('status') in ('queued', 'running')
                 or job.get('status') == 'completed' and job.get('passed')))


def available(context, *, validate=True):
    """Discover bounded candidates; validate dependencies only on tool use."""
    if not context.config.sandbox.check_reuse or context.role.workspace == 'none':
        return []
    key = execution_key(context.config)
    candidates = [j for p in root(context.project).glob('*/job.json')
                  if isinstance((j := read_json(p)), dict) and reusable(j)
                  and j.get('environment') is not None and j.get('execution') == key]
    if not candidates:
        return []
    capture = context.project.snapshots.capture_files(context.workspace)
    if capture['skipped']:
        return []
    candidates = [j for j in candidates if j.get('code_digest') == capture['code_digest']]
    if not candidates:
        return []
    if validate:
        env = environment(context.project)
        candidates = [j for j in candidates if env is not None and j['environment'] == env]
    return [{**summary(j), 'requires_input_check': not validate} for j in candidates[:32]]


def join(context, job_id):
    if job_id not in {j['id'] for j in available(context)}:
        raise Denied('No matching reusable check')
    project = context.project
    with lock(project.root / 'locks/check-jobs.lock'):
        original = raw_read(project, job_id)
        if not reusable(original) or (root(project) / job_id / 'cancel.json').exists():
            raise Denied('Check no longer reusable')
        return _subscribe(context, original)


def _subscribe(context, original):
    receiver = getattr(context, 'spend_role', None) if getattr(context, 'delegated_child', False) else None
    owner = receiver.name if receiver is not None else context.role.name
    if receiver is not None and 'check_job' not in receiver.capabilities:
        raise Denied('Delegated checks require check_job on the receiving parent')
    if original['run'] == context.run_dir.name and original['role'] == owner:
        return summary(read(context.project, original['id']))
    job_id = digest(canonical({'shared_job': original['id'], 'run': context.run_dir.name, 'role': owner}))
    path = root(context.project) / job_id / 'job.json'
    if path.exists():
        return summary(read(context.project, job_id))
    _make_room(context.project, protect={original['id']})
    job = {**original, 'id': job_id, 'shared_job': original['id'], 'role': owner,
           'producer_role': context.role.name, 'run': context.run_dir.name,
           'created_at': now(), 'acknowledged': False}
    write_json(path, job)
    os.utime(root(context.project), None)
    return summary(read(context.project, job_id))


def _make_room(project, *, protect=frozenset()):
    records = sorted(root(project).glob('*/job.json'), key=lambda p: p.stat().st_mtime)
    if len(records) < 1024:
        return
    referenced = {read_json(p, {}).get('shared_job') for p in records}
    for old in records:
        value = read_json(old, {})
        if value.get('acknowledged') and value.get('id') not in protect and value.get('id') not in referenced and read(project, value['id']).get('status') not in ('queued', 'running'):
            shutil.rmtree(old.parent)
            return
    raise Busy('Retained check evidence is full; inspect and acknowledge results')


def submit(context, script=None, *, kind="experiment", fresh=False):
    project = context.project
    receiver = getattr(context, 'spend_role', None) if getattr(context, 'delegated_child', False) else None
    owner = receiver.name if receiver is not None else context.role.name
    if receiver is not None and 'check_job' not in receiver.capabilities:
        raise Denied('Delegated checks require check_job on the receiving parent')
    if context.role.workspace == 'none':
        raise Denied('Check jobs require visible source')
    commands = list(project.verify) if script is None else [script]
    if len(commands) > 32:
        raise Denied("At most 32 commands per check job")
    if not commands:
        raise Denied('No acceptance commands configured')
    if kind not in ('experiment', 'exec') or (kind == 'exec' and context.role.workspace == 'write'):
        raise Denied('Cannot detach live workspace writes')
    if script is not None and kind not in context.role.capabilities:
        raise Denied('Custom check jobs require the corresponding sandbox capability')
    if script is None and 'verify' not in context.role.capabilities:
        raise Denied('Acceptance check jobs require verify capability')
    if shutil.disk_usage(project.root).free < context.config.limits.free_disk_mb * 1048576:
        raise Busy('Free disk space is below the check input reserve')
    capture = project.snapshots.capture_files(context.workspace)
    if capture['skipped']:
        raise Denied('Check job input is not representable')
    # Run-local identity remains the fallback when observed inputs cannot be shared.
    env = environment(project)
    shared_key = digest(canonical({'source': capture['code_digest'], 'execution': execution_key(context.config),
                                   'commands': commands, 'kind': 'verify' if script is None else kind,
                                   'environment': env})) if env is not None and not fresh else None
    identity = {'run': context.run_dir.name, 'source': capture['code_digest'],
                'execution': execution_key(context.config), 'commands': commands,
                'kind': 'verify' if script is None else kind, 'environment': env, 'fresh': fresh}
    job_id = digest(canonical(identity))
    directory = root(project) / job_id
    with lock(project.root / 'locks/check-jobs.lock'):
        if (directory / 'job.json').exists():
            return summary(read(project, job_id))
        if shared_key is not None:
            for path in sorted(root(project).glob('*/job.json')):
                other = read_json(path, {})
                if other.get('shared_key') == shared_key and reusable(other) and not (path.parent / 'cancel.json').exists():
                    return _subscribe(context, other)
        unfinished = sum(1 for p in root(project).glob('*/job.json')
                         if not read_json(p, {}).get('shared_job') and read_json(p, {}).get('status') in ('queued', 'running'))
        _make_room(project)
        if unfinished >= context.config.limits.check_pending:
            raise Busy('Check job queue is full')
        mkdir(directory)
        try:
            project.snapshots.materialize(capture, directory / 'input')
            job = {'id': job_id, 'role': owner, 'producer_role': context.role.name, 'run': context.run_dir.name,
                   'kind': identity['kind'],
                   'code_digest': capture['code_digest'], 'execution': identity['execution'],
                   'commands': commands, 'status': 'queued', 'created_at': now(),
                   'acknowledged': False, 'environment': env, 'shared_key': shared_key,
                   'inputs_bound_required': context.config.sandbox.check_reuse}
            write_json(directory / 'job.json', job)
            os.utime(root(project), None)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return summary(job)


def summary(job):
    return {**{'command_count': len(job.get('commands', [])),
               'command_preview': [text_preview(c, 128)[0] for c in job.get('commands', [])[:4]]},
            **{k: job[k] for k in ('id', 'role', 'run', 'kind', 'code_digest', 'status',
                               'created_at', 'finished_at', 'passed', 'error', 'shared_job') if k in job}}


def pending(project, role):
    if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
        return []
    return [summary(j) for p in sorted(root(project).glob('*/job.json'))
            if isinstance((j := read(project, p.parent.name)), dict) and j.get('role') == role
            and j.get('status') in ('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed')
            and not j.get('acknowledged')][:32]


def list_jobs(project, role):
    jobs = [summary(j) for p in root(project).glob('*/job.json')
            if isinstance((j := read(project, p.parent.name)), dict) and j.get('role') == role]
    return sorted(jobs, key=lambda j: j['created_at'], reverse=True)[:32]


def result(context, job_id):
    project = context.project
    with lock(project.root / 'locks/check-jobs.lock'):
        job = read(project, job_id)
        if context.role.name not in (job['role'], job.get('producer_role')):
            raise Denied('Check job belongs to another role')
        output = summary(job)
        if job['status'] not in ('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed'):
            return output
        record = read_json(root(project) / job.get('shared_job', job_id) / 'result.json', {})
        output['result'] = record
        if job['kind'] == 'verify' and job['status'] in ('completed', 'refused', 'interrupted', 'cleanup_failed'):
            output['accepted_for_current_source'] = False
        if job['kind'] == 'verify' and job['status'] == 'completed' and record.get('passed'):
            capture = project.snapshots.capture_files(context.workspace)
            matches = (not capture['skipped'] and capture['code_digest'] == job['code_digest']
                       and execution_key(context.config) == job['execution']
                       and list(project.verify) == job['commands']
                       and (not job.get('inputs_bound_required') or job.get('environment') is not None)
                       and (job.get('environment') is None or environment(project) == job['environment'])
                       and 'verify' in context.role.capabilities)
            output['accepted_for_current_source'] = matches
            if matches:
                context.verification = record
                write_json(context.run_dir / 'verification.json', record)
        consumed = getattr(context, 'check_results_read', set())
        consumed.add(job_id)
        context.check_results_read = consumed
        return output



def logs(context, job_id, evidence_id, *, offset=0, limit=8192, content_id=None):
    job = read(context.project, job_id)
    if context.role.name not in (job['role'], job.get('producer_role')):
        raise Denied('Check job belongs to another role')
    if not isinstance(evidence_id, str) or not re.fullmatch(r'[a-f0-9]{32}', evidence_id):
        raise Denied('Invalid command evidence ID')
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 8192:
        raise Denied('Invalid evidence page')
    path = root(context.project) / job.get('shared_job', job_id) / 'commands' / (evidence_id + '.json')
    if not path.is_file() or path.is_symlink() or path.stat().st_size > context.config.limits.output_bytes * 8 + 1048576:
        raise Denied('Command evidence unavailable')
    data = path.read_bytes()
    identity = digest(data)
    if content_id is not None and content_id != identity:
        raise Denied('Command evidence changed')
    text = data.decode('utf-8')
    end = min(len(text), offset + limit)
    return {'trust': 'recorded-local-untrusted', 'text': text[offset:end],
            'content_id': identity, 'offset': offset,
            'next_offset': end if end < len(text) else None}


def cancel(context, job_id):
    project = context.project
    with lock(project.root / 'locks/check-jobs.lock'):
        job = read(project, job_id)
        if context.role.name not in (job['role'], job.get('producer_role')):
            raise Denied('Check job belongs to another role')
        if job['status'] not in ('queued', 'running'):
            return summary(job)
        original_id = job.get('shared_job', job_id)
        raw = raw_read(project, job_id)
        raw['detached'] = True
        write_json(root(project) / job_id / 'job.json', raw)
        subscribers = [read_json(p, {}) for p in root(project).glob('*/job.json')]
        interested = any(not j.get('detached') and (j.get('id') == original_id or j.get('shared_job') == original_id) for j in subscribers)
        if not interested:
            write_json(root(project) / original_id / 'cancel.json', {'requested_at': now()})
        os.utime(root(project), None)
        return {**summary(read(project, job_id)), 'cancel_requested': True}


def acknowledge(project, role, ids):
    with lock(project.root / 'locks/check-jobs.lock'):
        for job_id in ids:
            job = read(project, job_id)
            if job['role'] != role:
                raise Denied('Check job role mismatch')
            if job['status'] in ('queued', 'running'):
                continue
            original_id = job.get('shared_job', job_id)
            for path in root(project).glob('*/job.json'):
                raw = read_json(path, {})
                same_result = (not job.get('detached') and raw.get('role') == role
                               and raw.get('shared_job', raw.get('id')) == original_id)
                if raw.get('id') == job_id or same_result:
                    raw['acknowledged'] = True
                    write_json(path, raw)


def run_one(project, stop, *, sandbox_factory=Sandbox):
    """Run at most one queued check; no model admission slot is acquired.

    The live per-job lock is crash ownership. A leftover running record is
    interrupted, never retried as successful; its requester can submit a new run.
    """
    control = project.control()
    if not control.get('armed') or control.get('paused') or control.get('draining') or stop.is_set():
        return {'status': 'deferred'}
    if shutil.disk_usage(project.root).free < project.config.limits.free_disk_mb * 1048576:
        return {'status': 'disk-reserve'}
    with contextlib.ExitStack() as stack:
        # Cache ownership is acquired before compute admission: waiting for an
        # artifact must never consume a scarce execution slot.
        try:
            stack.enter_context(lock(project.root / 'locks/check-cache.lock', blocking=False))
        except Busy:
            return {'status': 'busy'}
        for slot in range(project.config.limits.parallel_checks):
            if (project.config.data / 'check-slots' / f'{slot}.fault.json').exists():
                continue
            try:
                stack.enter_context(lock(project.config.data / 'check-slots' / f'{slot}.lock', blocking=False))
                if (project.config.data / 'check-slots' / f'{slot}.fault.json').exists():
                    stack.close()
                    continue
                break
            except Busy:
                continue
        else:
            return {'status': 'busy'}
        for path in sorted(root(project).glob('*/job.json'), key=lambda p: read_json(p, {}).get('created_at', '')):
            job = read_json(path, {})
            if job.get('shared_job') or job.get('status') not in ('queued', 'running'):
                continue
            try:
                owner = lock(path.parent / 'owner.lock', blocking=False)
                stack.enter_context(owner)
            except Busy:
                continue
            job = read_json(path, {})
            if job.get('shared_job') or job.get('status') not in ('queued', 'running'):
                return {'status': 'idle'}
            if job['status'] == 'running':
                try:
                    cleanup(project.config, project.root, 'check-' + job['id'][:24])
                except Exception as exc:
                    return quarantine(project, job, slot, str(exc))
                job.update(status='interrupted', error='Runner exited before recording completion', finished_at=now())
                save_execution(project, job)
                shutil.rmtree(path.parent / 'input', ignore_errors=True)
                os.utime(root(project), None)
                return summary(job)
            result = _execute(project, job, stop, sandbox_factory)
            if result['status'] == 'cleanup_failed':
                return quarantine(project, read(project, job['id']), slot, result.get('error', 'Cleanup failed'))
            return result
    return {'status': 'idle'}


def _execute(project, job, stop, sandbox_factory):
    directory = root(project) / job['id']
    path = directory / 'job.json'
    role = project.config.roles.get(job.get('producer_role', job['role']))
    receiver = project.config.roles.get(job['role'])
    if (role is None or receiver is None or 'check_job' not in receiver.capabilities or 'check_job' not in role.capabilities
            or job['kind'] not in role.capabilities):
        job.update(status='refused', error='Check authority withdrawn', finished_at=now())
        save_execution(project, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if (job['kind'] == 'verify' and job.get('inputs_bound_required') and job.get('environment') is None) or (job.get('environment') is not None and environment(project) != job['environment']) or job['execution'] != execution_key(project.config) or (job['kind'] == 'verify' and list(project.verify) != job['commands']):
        job.update(status='refused', error=('Dependency inputs could not be bound for reusable acceptance'
                   if job.get('inputs_bound_required') and job.get('environment') is None
                   else 'Execution configuration or dependency inputs changed'), finished_at=now())
        save_execution(project, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if (directory / 'cancel.json').exists():
        job.update(status='cancelled', finished_at=now())
        save_execution(project, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    job.update(status='running', started_at=now())
    save_execution(project, job)
    # A job-specific container role prevents ordinary role cleanup from deleting
    # a check after the requesting model unit has ended.
    sandbox = sandbox_factory(project.config, project.root, 'check-' + job['id'][:24], directory,
                              cancel=lambda: stop.is_set() or (directory / 'cancel.json').exists())
    workspace = directory / 'input'
    try:
        before = project.snapshots.capture_files(workspace)
        if before['code_digest'] != job['code_digest'] or before['skipped']:
            raise Denied('Check job source changed before execution')
        cache = project.root / 'check-cache' / job['execution']
        cache_names = project.config.sandbox.check_cache_paths if job['kind'] == 'verify' else ()
        for name in cache_names:
            if not project.snapshots.excluded(name) or (workspace / name).exists():
                raise Denied('Build cache must be excluded from source: ' + name)
            previous = cache / name
            if previous.exists() or previous.is_symlink():
                if previous.is_symlink() or not previous.is_dir():
                    raise Denied('Build cache is not a directory')
                previous.replace(workspace / name)
        results = []
        for command in job['commands']:
            if stop.is_set():
                raise Denied('Check job interrupted')
            results.append(sandbox.execute(workspace, command, writable=job['kind'] == 'verify', experiment=job['kind'] == 'experiment'))
        after = project.snapshots.capture_files(workspace)
        unchanged = after['code_digest'] == before['code_digest']
        representable = not after['skipped']
        dependencies_unchanged = job.get('environment') is None or environment(project) == job['environment']
        passed = unchanged and representable and dependencies_unchanged and all(r['exit_code'] == 0 and r['reason'] == 'exited' for r in results)
        record = {'passed': passed, 'dependencies_unchanged': dependencies_unchanged, 'code_digest': job['code_digest'],
                  'snapshot_representable': representable, 'unchanged_during_verification': unchanged,
                  'image': project.config.sandbox.image, 'run': job['run'], 'job': job['id'],
                  'created_at': now(), 'commands': [{**{k: r[k] for k in ('id', 'exit_code', 'reason')},
                      'script': text_preview(r['script'], 512)[0],
                      'stdout': text_preview(r.get('stdout', ''), 512)[0],
                      'stderr': text_preview(r.get('stderr', ''), 512)[0],
                      'evidence': 'check-jobs/' + job['id'] + '/commands/' + r['id'] + '.json'} for r in results]}
        write_json(directory / 'result.json', record)
        job.update(status='cancelled' if (directory / 'cancel.json').exists() else 'completed', passed=passed and not (directory / 'cancel.json').exists(), finished_at=now())
    except Exception as exc:
        job.update(status='interrupted', error=str(exc)[:512], finished_at=now())
        try:
            cleanup(project.config, project.root, 'check-' + job['id'][:24])
        except Exception as cleanup_exc:
            job.update(status='cleanup_failed', error=('Container cleanup requires recovery: ' + str(cleanup_exc))[:512])
    save_execution(project, job)
    if job['status'] != 'cleanup_failed':
        # Move only configured, snapshot-excluded build trees after sandbox
        # cleanup. They are compiler inputs, never evidence of a passing check.
        if job['status'] == 'completed' and job['kind'] == 'verify':
            cache = project.root / 'check-cache' / job['execution']
            mkdir(cache)
            for name in project.config.sandbox.check_cache_paths:
                artifact = workspace / name
                if project.snapshots.excluded(name) and artifact.is_dir() and not artifact.is_symlink():
                    artifact.replace(cache / name)
        shutil.rmtree(directory / 'input', ignore_errors=True)
    os.utime(root(project), None)
    return summary(job)


def wait_signature(project):
    control = project.control()
    pointer = read_json(project.root / 'current.json', {})
    # A new publication, operator wake or incoming decision is independent work.
    return {'snapshot': pointer.get('snapshot'), 'wake': control.get('wake_generation'),
            'inbox': project.insights.generation()}


def set_wait(project, role, job_id):
    job = read(project, job_id)
    if job['role'] != role:
        raise Denied('Check wait belongs to another role')
    write_json(root(project) / (role + '.wait.json'), {'id': job_id, 'inputs': wait_signature(project)})


def waiting(project, role):
    if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
        return False
    record = read_json(root(project) / (role + '.wait.json'), {})
    if not record or record.get('inputs') != wait_signature(project):
        return False
    try:
        return read(project, record['id'])['status'] in ('queued', 'running')
    except Denied:
        # Consumed evidence may be reaped; its stale wait is no longer pending.
        return False


def quarantine(project, job, slot, error):
    job.update(status='cleanup_failed', error=error[:512], finished_at=now())
    save_execution(project, job)
    write_json(project.config.data / 'check-slots' / f'{slot}.fault.json',
               {'project': project.name, 'id': job['id']})
    os.utime(root(project), None)
    return summary(job)


def recover(project):
    """Bounded operator recovery; never clear a reserved slot before cleanup."""
    recovered = []
    for slot in range(project.config.limits.parallel_checks):
        fault = project.config.data / 'check-slots' / f'{slot}.fault.json'
        record = read_json(fault, {})
        if record.get('project') != project.name:
            continue
        with lock(fault.with_name(f'{slot}.lock'), blocking=False):
            record = read_json(fault, {})
            if record.get('project') != project.name:
                continue
            job = read(project, record['id'])
            cleanup(project.config, project.root, 'check-' + job['id'][:24])
            job.update(status='interrupted', error='Cleanup recovered; previous execution did not complete', finished_at=now())
            save_execution(project, job)
            fault.unlink()
            shutil.rmtree(root(project) / job['id'] / 'input', ignore_errors=True)
            os.utime(root(project), None)
            recovered.append(job['id'])
    return {'recovered': recovered}
