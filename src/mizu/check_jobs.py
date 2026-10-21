"""Durable sandbox checks on frozen inputs, independent of model work units.

Only explicitly granted check jobs use this queue. They never execute on the
host or mutate the live workspace. A separate runner owns compute slots; a model
can continue work or finish while its job runs. Results are not acceptance until
claimed against the current source and execution configuration.
"""
from __future__ import annotations
import contextlib
import os
import dataclasses
import shutil
import uuid
from pathlib import Path
from .errors import Busy, Denied
from .fs import text_preview, canonical, digest, lock, mkdir, now, read_json, write_json, DIGEST
from .sandbox import Sandbox, cleanup


def execution_key(config):
    return digest(canonical({'sandbox': dataclasses.asdict(config.sandbox),
                             'command_seconds': config.limits.command_seconds,
                             'output_bytes': config.limits.output_bytes}))


def root(project):
    return project.root / 'check-jobs'


def read(project, job_id):
    if not isinstance(job_id, str) or not DIGEST.fullmatch(job_id):
        raise Denied('Invalid check job ID')
    value = read_json(root(project) / job_id / 'job.json')
    if not isinstance(value, dict) or value.get('id') != job_id:
        raise Denied('Check job unavailable')
    return value


def submit(context, script=None, *, kind="experiment"):
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
    # Run-scoped idempotency prevents a retried tool request duplicating work.
    # Cross-run result reuse is deliberately not inferred from shell text:
    # mounted dependencies and network inputs need explicit immutable identities.
    identity = {'run': context.run_dir.name, 'source': capture['code_digest'],
                'execution': execution_key(context.config), 'commands': commands,
                'kind': 'verify' if script is None else kind}
    job_id = digest(canonical(identity))
    directory = root(project) / job_id
    with lock(project.root / 'locks/check-jobs.lock'):
        if (directory / 'job.json').exists():
            return summary(read(project, job_id))
        unfinished = sum(1 for p in root(project).glob('*/job.json')
                         if read_json(p, {}).get('status') in ('queued', 'running'))
        records = sorted(root(project).glob('*/job.json'), key=lambda p: p.stat().st_mtime)
        if len(records) >= 1024:
            for old in records:
                if read_json(old, {}).get('acknowledged'):
                    shutil.rmtree(old.parent)
                    break
            else:
                raise Busy('Retained check evidence is full; inspect and acknowledge results')
        if unfinished >= context.config.limits.check_pending:
            raise Busy('Check job queue is full')
        mkdir(directory)
        try:
            project.snapshots.materialize(capture, directory / 'input')
            job = {'id': job_id, 'role': owner, 'producer_role': context.role.name, 'run': context.run_dir.name,
                   'kind': identity['kind'],
                   'code_digest': capture['code_digest'], 'execution': identity['execution'],
                   'commands': commands, 'status': 'queued', 'created_at': now(),
                   'acknowledged': False}
            write_json(directory / 'job.json', job)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return summary(job)


def summary(job):
    return {k: job[k] for k in ('id', 'role', 'run', 'kind', 'code_digest', 'status',
                               'created_at', 'finished_at', 'passed', 'error') if k in job}


def pending(project, role):
    if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
        return []
    return [summary(j) for p in sorted(root(project).glob('*/job.json'))
            if isinstance((j := read_json(p)), dict) and j.get('role') == role
            and j.get('status') in ('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed')
            and not j.get('acknowledged')][:32]


def list_jobs(project, role):
    jobs = [summary(j) for p in root(project).glob('*/job.json')
            if isinstance((j := read_json(p)), dict) and j.get('role') == role]
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
        record = read_json(root(project) / job_id / 'result.json', {})
        output['result'] = record
        if job['kind'] == 'verify' and job['status'] == 'completed' and record.get('passed'):
            capture = project.snapshots.capture_files(context.workspace)
            matches = (not capture['skipped'] and capture['code_digest'] == job['code_digest']
                       and execution_key(context.config) == job['execution']
                       and list(project.verify) == job['commands'])
            output['accepted_for_current_source'] = matches
            if matches:
                context.verification = record
                write_json(context.run_dir / 'verification.json', record)
        consumed = getattr(context, 'check_results_read', set())
        consumed.add(job_id)
        context.check_results_read = consumed
        return output


def cancel(context, job_id):
    job = read(context.project, job_id)
    if context.role.name not in (job['role'], job.get('producer_role')):
        raise Denied('Check job belongs to another role')
    if job['status'] not in ('queued', 'running'):
        return summary(job)
    write_json(root(context.project) / job_id / 'cancel.json', {'requested_at': now()})
    return {**summary(job), 'cancel_requested': True}


def acknowledge(project, role, ids):
    with lock(project.root / 'locks/check-jobs.lock'):
        for job_id in ids:
            job = read(project, job_id)
            if job['role'] != role:
                raise Denied('Check job role mismatch')
            if job['status'] in ('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed'):
                job['acknowledged'] = True
                write_json(root(project) / job_id / 'job.json', job)


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
            if job.get('status') not in ('queued', 'running'):
                continue
            try:
                owner = lock(path.parent / 'owner.lock', blocking=False)
                stack.enter_context(owner)
            except Busy:
                continue
            job = read_json(path, {})
            if job.get('status') not in ('queued', 'running'):
                return {'status': 'idle'}
            if job['status'] == 'running':
                try:
                    cleanup(project.config, project.root, 'check-' + job['id'][:24])
                except Exception as exc:
                    return quarantine(project, job, slot, str(exc))
                job.update(status='interrupted', error='Runner exited before recording completion', finished_at=now())
                write_json(path, job)
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
        write_json(path, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if job['execution'] != execution_key(project.config) or (job['kind'] == 'verify' and list(project.verify) != job['commands']):
        job.update(status='refused', error='Execution configuration changed', finished_at=now())
        write_json(path, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if (directory / 'cancel.json').exists():
        job.update(status='cancelled', finished_at=now())
        write_json(path, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    job.update(status='running', started_at=now())
    write_json(path, job)
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
        passed = unchanged and representable and all(r['exit_code'] == 0 and r['reason'] == 'exited' for r in results)
        record = {'passed': passed, 'code_digest': job['code_digest'],
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
    write_json(path, job)
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
    return read(project, record['id'])['status'] in ('queued', 'running')


def quarantine(project, job, slot, error):
    job.update(status='cleanup_failed', error=error[:512], finished_at=now())
    write_json(root(project) / job['id'] / 'job.json', job)
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
            write_json(root(project) / job['id'] / 'job.json', job)
            fault.unlink()
            shutil.rmtree(root(project) / job['id'] / 'input', ignore_errors=True)
            os.utime(root(project), None)
            recovered.append(job['id'])
    return {'recovered': recovered}
