"""Durable sandbox checks on frozen inputs, independent of model work units.

Only explicitly granted check jobs use this queue. They never execute on the
host or mutate the live workspace. A separate runner owns compute slots; a model
can continue work or finish while its job runs. Results are not acceptance until
claimed against the current source and execution configuration.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import stat
import os
import re
import dataclasses
import shutil
import time
import math
from pathlib import Path
from .admission import check_slot_exclusion
from .errors import Busy, Denied
from .fs import text_preview, canonical, digest, lock, mkdir, now, read_json, write_json, DIGEST, SCRIPT_MAX
from .sandbox import Sandbox, cleanup, cleanup_refused, scratch_selection


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


def _positive_finite_deadline(value):
    """Normalize a recorded epoch bound, refusing overflow and non-finite values."""
    if type(value) not in (int, float):
        return None
    try:
        deadline = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(deadline) or deadline <= 0:
        return None
    return deadline


def job_role(job_id):
    """The container role a job owns.

    The role names the job's own containers, so ordinary role cleanup never
    touches them and one job's cleanup never touches another's.
    """
    return 'check-' + job_id[:24]


#: A purpose is a short opaque label: it names a demand, never a path.
PURPOSE_MAX = 64


def purpose_label(purpose):
    """Validate the caller's own label for what a demand is for.

    The label says only which of the caller's own demands a later request
    replaces. It carries no authority: it never selects another role's
    evidence, never shortens a bound and never makes a result acceptable.
    """
    if purpose is None:
        return None
    if not isinstance(purpose, str) or not 1 <= len(purpose) <= PURPOSE_MAX:
        raise Denied('Invalid check job purpose')
    if not purpose.strip() or not all(character.isprintable() for character in purpose):
        raise Denied('Invalid check job purpose')
    if '/' in purpose or '\\' in purpose:
        raise Denied('Invalid check job purpose')
    return purpose


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
        for key in ('status', 'passed', 'error', 'finished_at', 'cache_input', 'shared_key'):
            if key in original:
                job[key] = original[key]
    elif job.get('detached'):
        job.update(status='cancelled', passed=False)
    return job



def save_execution(project, job):
    # Execution progress and subscription state have different writers.
    with lock(project.root / 'locks/check-jobs.lock'):
        latest = raw_read(project, job['id'])
        # The purpose is stated on the record while the runner holds an
        # older view of it, so the record's own label is merged here too;
        # otherwise the write-back would drop it and the demand would stop
        # matching the later request that replaces it.
        for key in ('detached', 'acknowledged', 'replaced_by', 'purpose'):
            if key in latest:
                job[key] = latest[key]
        write_json(root(project) / job['id'] / 'job.json', job)


def _configured_input_bounds(project):
    return {'files': project.config.sandbox.check_input_files,
            'bytes': project.config.sandbox.check_input_bytes}


def _mount_fingerprint(project, bounds):
    """Fingerprint local dependencies within validated, explicit traversal bounds."""
    if (not isinstance(bounds, dict) or set(bounds) != {'files', 'bytes'}
            or any(type(value) is not int or not 1 <= value <= 1099511627776
                   for value in bounds.values())):
        return None
    config = project.config
    if config.sandbox.network != 'none' or not re.fullmatch(r'(?:.*@)?sha256:[a-f0-9]{64}', config.sandbox.image):
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
                    if len(entries) >= bounds['files']:
                        return None
                    st = path.lstat()
                    identity = list(stamp(st))
                    key = str(path)
                    if stat.S_ISDIR(st.st_mode):
                        children = []
                        for child in path.iterdir():
                            # A directory and its children must fit even before
                            # traversing the children; never list an unbounded tree.
                            if len(entries) + len(pending) + len(children) + 1 >= bounds['files']:
                                return None
                            children.append(child)
                        children.sort()
                        pending.extend(reversed(children))
                        value = {'stat': identity, 'children': [c.name for c in children]}
                    elif stat.S_ISREG(st.st_mode):
                        total_bytes += st.st_size
                        if total_bytes > bounds['bytes']:
                            return None
                        old = cached.get(key, {})
                        if os.name == 'posix' and old.get('stat') == identity and old.get('digest'):
                            content = old['digest']
                        else:
                            with path.open('rb') as handle:
                                # Path and handle stat fields need not have identical
                                # representations on Windows. Compare like with like,
                                # and bind the open handle to the current named file.
                                opened = stamp(os.fstat(handle.fileno()))
                                with path.open('rb') as named:
                                    if not os.path.sameopenfile(handle.fileno(), named.fileno()):
                                        return None
                                content = hashlib.file_digest(handle, 'sha256').hexdigest()
                                if stamp(os.fstat(handle.fileno())) != opened:
                                    return None
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


def environment(project, *, bounds=None):
    """Fingerprint observed local dependencies; never share network/tag inputs.

    File digests are cached only against complete local stat identities. The
    identity itself also participates in the receipt, so even a dependency
    changed and restored during execution invalidates it on POSIX filesystems.
    Unsupported files or concurrent edits disable reuse rather than execution.
    """
    if not project.config.sandbox.check_reuse:
        return None
    return _mount_fingerprint(project, _configured_input_bounds(project) if bounds is None else bounds)


def execution_environment(project, job):
    """Revalidate with the bounds that originally bound this job's environment.

    Reuse policy may change after submission, but validation must stay bounded.
    Legacy records without bounds use current configuration; malformed retained
    bounds fail closed, never falling back to configuration or an unbounded scan.
    Bounds are validation metadata, not execution identity or sharing inputs.
    """
    return _mount_fingerprint(project, job.get('environment_bounds', _configured_input_bounds(project)))


class CacheOwned(Busy):
    """The cache lock is held; no owner identity is implied."""


@contextlib.contextmanager
def cache_guard(project, *, blocking=True):
    """Cache precedes queue exclusion everywhere; never hash under queue lock."""
    if not project.config.sandbox.check_cache_paths:
        yield
        return
    guard = lock(project.root / 'locks/check-cache.lock', blocking=blocking)
    try:
        guard.__enter__()
    except Busy as exc:
        raise CacheOwned('Configured check cache busy; retry after its owner releases it') from exc
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


def cache_input(project):
    """Fingerprint configured pre-execution trees while holding cache_guard.

    None means unbound, never empty. Count directories and absent names as
    entries too, and stream file bytes within the existing dependency bounds.
    Permissions and mtimes participate as command-visible inputs; inode/ctime
    only detect concurrent edits. Moving an otherwise unchanged tree into/out
    of a workspace must not change its identity. Hardlinks are bound only when
    every name of their inode is found inside these trees. Bind their topology
    by a deterministic relative name, not by a host inode number. Each name
    still counts against both traversal and byte bounds.
    """
    from . import platform
    from .fs import _same_file
    names = project.config.sandbox.check_cache_paths
    if not names:
        return None  # Preserve scopes/identities for no-cache configurations.
    base = Path('check-cache') / execution_key(project.config)
    configured = {base / name for name in names}
    observed, entries, hardlinks = {}, {}, {}
    total = 0

    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def inspect(relative):
        path = project.root
        for part in relative.parts:
            path /= part
            try:
                info = path.lstat()
            except FileNotFoundError:
                observed.setdefault(path, None)
                return None
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or path != project.root / relative and not stat.S_ISDIR(info.st_mode)):
                raise Denied('Unsafe build cache path')
            observed.setdefault(path, stamp(info))
        return info

    @contextlib.contextmanager
    def opened(relative, directory):
        # Pin every component on POSIX. The portable fallback checks named
        # components before/after use, like safe_read's documented fallback.
        if platform.HAS_OPENAT and platform.HAS_DIR_FD:
            fd = os.open(project.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for index, part in enumerate(relative.parts):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if directory or index < len(relative.parts) - 1:
                        flags |= os.O_DIRECTORY
                    child = os.open(part, flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                yield fd
            finally:
                os.close(fd)
        else:
            inspect(relative)
            yield project.root / relative
            inspect(relative)

    try:
        pending = [base / name for name in reversed(names)]
        while pending:
            relative = pending.pop()
            if len(entries) >= project.config.sandbox.check_input_files:
                return None
            info = inspect(relative)
            key = relative.relative_to(base).as_posix()
            if info is None:
                entries[key] = {'absent': True}
                continue
            value = {'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
            if stat.S_ISDIR(info.st_mode):
                children = []
                with opened(relative, True) as node, os.scandir(node) as listing:
                    if isinstance(node, int) and not _same_file(info, os.fstat(node)):
                        return None
                    for child in listing:
                        if len(entries) + len(pending) + len(children) + 1 >= project.config.sandbox.check_input_files:
                            return None
                        children.append(child.name)
                children.sort()
                value['children'] = children
                pending.extend(relative / name for name in reversed(children))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink >= 1 and relative not in configured:
                total += info.st_size
                if total > project.config.sandbox.check_input_bytes:
                    return None
                with opened(relative, False) as node:
                    stream = os.fdopen(os.dup(node), 'rb') if isinstance(node, int) else node.open('rb')
                    with stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode) or not _same_file(info, before):
                            return None
                        hasher, remaining = hashlib.sha256(), info.st_size
                        while remaining:
                            data = stream.read(min(remaining, 1048576))
                            if not data:
                                return None
                            remaining -= len(data)
                            hasher.update(data)
                        if stream.read(1) or stamp(os.fstat(stream.fileno())) != stamp(before):
                            return None
                        value['digest'] = hasher.hexdigest()
                if info.st_nlink > 1:
                    if not info.st_ino:
                        return None  # No trustworthy identity for link accounting.
                    identity = (info.st_dev, info.st_ino)
                    prior, members = hardlinks.setdefault(identity, (stamp(info), []))
                    if prior != stamp(info):
                        return None
                    members.append(key)
            else:
                return None
            entries[key] = value
        for prior, members in hardlinks.values():
            if len(members) != prior[3]:  # st_nlink includes names outside our trees.
                return None
            first = min(members)
            for key in members:
                entries[key]['hardlink'] = first
        for path, before in observed.items():
            try:
                after = stamp(path.lstat())
            except FileNotFoundError:
                after = None
            if after != before:
                return None
        return digest(canonical({'names': list(names), 'entries': entries}))
    except (OSError, Denied, ValueError, UnicodeError):
        return None


def cache_matches(project, job, current):
    return (job.get('kind') != 'verify' or not project.config.sandbox.check_cache_paths
            or current is not None and job.get('cache_input') == current)


def reusable(job):
    return (not job.get('cache_binding_pending') and not job.get('scratch_paths') and not job.get('shared_job') and not job.get('detached')
            and (job.get('status') in ('queued', 'running')
                 or job.get('status') == 'completed' and job.get('passed')))


def available(context, *, validate=True, discovery=None):
    """Discover bounded candidates; validate dependencies only on tool use."""
    if ('check_job' not in context.role.capabilities
            or not context.config.sandbox.check_reuse or context.role.workspace == 'none'):
        return []
    key = execution_key(context.config)
    if discovery is None:
        from .acceptance import discover
        discovery = discover(context.project)
    candidates = [j for j in discovery[0].values() if reusable(j)
                  and j.get('environment') is not None and j.get('execution') == key]
    if not candidates:
        return []
    context.close_exec()
    capture = context.project.snapshots.capture_files(context.workspace)
    if capture['skipped']:
        return []
    candidates = [j for j in candidates if j.get('code_digest') == capture['code_digest']]
    if not candidates:
        return []
    if validate:
        env = environment(context.project)
        with cache_guard(context.project, blocking=False):
            current = cache_input(context.project)
            candidates = [j for j in candidates if env is not None and j['environment'] == env
                          and cache_matches(context.project, j, current)]
    return [{**summary(j), 'requires_input_check': not validate} for j in candidates[:32]]


def join(context, job_id):
    if job_id not in {j['id'] for j in available(context)}:
        raise Denied('No matching reusable check')
    project = context.project
    with cache_guard(project, blocking=False):
        current = cache_input(project)
        with lock(project.root / 'locks/check-jobs.lock'):
            original = raw_read(project, job_id)
            if (not reusable(original) or not cache_matches(project, original, current)
                    or (root(project) / job_id / 'cancel.json').exists()):
                raise Denied('Check no longer reusable')
            return _subscribe(context, original)


def _delivered_summary(project, job_id, owner):
    """Return the summary, durably acknowledging a synchronously delivered success.

    A completed, passed result returned synchronously in this response must
    not remain an unread wake. Only the returned request's own record is
    marked; queued/running and unsuccessful results stay unread until the
    normal result-read path, and ownership, execution and evidence are kept.
    The caller holds check-jobs.lock, so the mark is written directly.
    """
    job = read(project, job_id)
    if (job.get('role') == owner and job.get('status') == 'completed'
            and job.get('passed') is True and not job.get('acknowledged')):
        raw = raw_read(project, job_id)
        if raw.get('role') == owner:
            raw['acknowledged'] = True
            write_json(root(project) / job_id / 'job.json', raw)
            job = read(project, job_id)
    return summary(job)


def _subscribe(context, original, *, purpose=None, request=None, request_id=None,
               request_fresh=None, request_bounds=None, request_reuse=None):
    receiver = getattr(context, 'spend_role', None) if getattr(context, 'delegated_child', False) else None
    owner = receiver.name if receiver is not None else context.role.name
    if receiver is not None and 'check_job' not in receiver.capabilities:
        raise Denied('Delegated checks require check_job on the receiving parent')
    if request_id is None and original['run'] == context.run_dir.name and original['role'] == owner:
        return _delivered_summary(context.project, original['id'], owner)
    job_id = request_id or digest(canonical({'shared_job': original['id'], 'run': context.run_dir.name, 'role': owner}))
    path = root(context.project) / job_id / 'job.json'
    if path.exists():
        return _delivered_summary(context.project, job_id, owner)
    _make_room(context.project, protect={original['id']})
    # A terminal reusable result is returned synchronously in this response;
    # recording a second unread completion would manufacture a later wake.
    # Queued/running subscriptions remain unread until the normal result path.
    already_delivered = (original.get('status') == 'completed'
                         and original.get('passed') is True)
    job = {**original, 'id': job_id, 'shared_job': original['id'], 'role': owner,
           'producer_role': context.role.name, 'run': context.run_dir.name,
           'created_at': now(), 'acknowledged': already_delivered}
    job.pop('cache_request', None)
    if request is not None:
        job['cache_request'] = request
        # The subscription states the subscriber's own durable request
        # identity, never the original's execution-bound fields.
        job['fresh'] = request_fresh
        job['environment_bounds'] = dict(request_bounds) if isinstance(request_bounds, dict) else request_bounds
        job['inputs_bound_required'] = request_reuse
    for key in ('acceptance_scope', 'acceptance_receipt_id', 'acceptance_retention_refusal'):
        job.pop(key, None)  # Only the original execution produces proof.
    # The subscription states the subscriber's own demand. The shared job
    # keeps its own label, because a purpose never crosses a role boundary.
    if purpose is None:
        job.pop('purpose', None)
    else:
        job['purpose'] = purpose
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
        # A detached subscription is cancelled immediately; its physical runner
        # may still own the frozen inputs until it observes cancellation.
        executing = not value.get('shared_job') and value.get('status') in ('queued', 'running')
        pending_proof = value.get('acceptance_scope') is not None and not value.get('acceptance_receipt_id')
        if (value.get('acknowledged') and not executing and not pending_proof
                and value.get('id') not in protect and value.get('id') not in referenced
                and read(project, value['id']).get('status') not in ('queued', 'running')):
            shutil.rmtree(old.parent)
            return
    raise Busy('Retained check evidence is full; inspect and acknowledge results')


def submit(context, script=None, *, commands=None, kind="experiment", fresh=False, purpose=None, scratch_paths=None):
    project = context.project
    has_script = script is not None
    has_commands = commands is not None
    if has_script and has_commands:
        raise Denied('script and commands are exclusive')
    if has_commands:
        if not isinstance(commands, (list, tuple)) or not 1 <= len(commands) <= 32:
            raise Denied('commands must list 1-32 commands')
        normalized = []
        for entry in commands:
            if not isinstance(entry, str):
                raise Denied('Invalid check commands')
            try:
                encoded = entry.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise Denied('Invalid check commands') from exc
            if chr(0) in entry or not entry.strip() or len(encoded) > SCRIPT_MAX:
                raise Denied('Invalid check commands')
            normalized.append(entry)
        commands = normalized
    custom = has_script or has_commands
    scratch_paths = scratch_selection(project.config, scratch_paths)
    if scratch_paths and (not custom or kind != 'experiment'):
        raise Denied('Scratch requires a custom queued experiment')
    if any(not project.snapshots.excluded(name) for name in scratch_paths):
        raise Denied('Build cache must be excluded from source')
    receiver = getattr(context, 'spend_role', None) if getattr(context, 'delegated_child', False) else None
    owner = receiver.name if receiver is not None else context.role.name
    if receiver is not None and 'check_job' not in receiver.capabilities:
        raise Denied('Delegated checks require check_job on the receiving parent')
    if context.role.workspace == 'none':
        raise Denied('Check jobs require visible source')
    job_commands = list(project.verify) if not custom else ([script] if has_script else list(commands))
    if len(job_commands) > 32:
        raise Denied("At most 32 commands per check job")
    if not job_commands:
        raise Denied('No acceptance commands configured')
    if kind not in ('experiment', 'exec') or (kind == 'exec' and context.role.workspace == 'write'):
        raise Denied('Cannot detach live workspace writes')
    if custom and kind not in context.role.capabilities:
        raise Denied('Custom check jobs require the corresponding sandbox capability')
    if not custom and 'verify' not in context.role.capabilities:
        raise Denied('Acceptance check jobs require verify capability')
    purpose = purpose_label(purpose)
    if shutil.disk_usage(project.root).free < context.config.limits.free_disk_mb * 1048576:
        raise Busy('Free disk space is below the check input reserve')
    context.close_exec()
    capture = project.snapshots.capture_files(context.workspace)
    if capture['skipped']:
        raise Denied('Check job input is not representable')
    # Run-local identity remains the fallback when observed inputs cannot be shared.
    env_bounds = _configured_input_bounds(project)
    env = environment(project, bounds=env_bounds)
    request_identity = {'version': 1, 'owner': owner, 'producer': context.role.name,
                        'run': context.run_dir.name, 'source': capture['code_digest'],
                        'execution': execution_key(context.config), 'commands': job_commands,
                        'kind': 'verify', 'environment': env, 'fresh': fresh,
                        'input_bounds': env_bounds, 'reuse': project.config.sandbox.check_reuse,
                        'cache_paths': list(project.config.sandbox.check_cache_paths)}
    cache_request = digest(canonical(request_identity))

    def request_slot():
        # Strictly validate every retained record: an unreadable or ambiguous
        # store must not conceal an unresolved request.
        from . import acceptance
        matches = []
        for path in acceptance.job_paths(project):
            record = acceptance.read_job(project, path.parent.name)
            if record.get('cache_request') != cache_request:
                continue
            # Every decomposed request field is compared so a changed
            # fresh, bounds, owner, source or configuration cannot coalesce
            # with an unrelated demand. The request identity survives
            # binding and start; only the execution binding is rebound.
            if (record.get('role') != owner or record.get('producer_role') != context.role.name
                    or record.get('run') != context.run_dir.name
                    or record.get('code_digest') != capture['code_digest']
                    or record.get('execution') != request_identity['execution']
                    or record.get('commands') != job_commands or record.get('environment') != env
                    or record.get('fresh') != fresh
                    or record.get('environment_bounds') != env_bounds
                    or record.get('inputs_bound_required') != project.config.sandbox.check_reuse):
                raise Denied('Invalid check request identity')
            if record.get('detached') or acceptance._present(project.root, f"check-jobs/{record['id']}/cancel.json"):
                continue
            if ('cache_input' not in record and 'cache_binding_pending' not in record
                    and not record.get('shared_job')):
                raise Denied('Existing check lacks matching cache inputs')
            if record.get('shared_job'):
                original = acceptance.read_job(project, record['shared_job'])
                if original.get('shared_job'):
                    raise Denied('Invalid check subscription')
                # A pending or unbound original is impossible as shared
                # execution: refuse the corrupt subscription, never reuse it.
                if original.get('cache_binding_pending'):
                    raise Denied('Invalid check subscription')
                if (original.get('kind') == 'verify' and project.config.sandbox.check_cache_paths
                        and 'cache_input' not in original):
                    raise Denied('Invalid check subscription')
                if acceptance._present(project.root, f"check-jobs/{original['id']}/cancel.json"):
                    continue
                status = original.get('status')
            else:
                status = record.get('status')
            if status not in ('queued', 'running', 'completed', 'refused', 'cancelled', 'interrupted', 'cleanup_failed'):
                raise Denied('Invalid check request status')
            if status in ('queued', 'running'):
                matches.append(record['id'])
        if len(matches) > 1:
            raise Denied('Ambiguous check request identity')
        if matches:
            return matches[0], True
        for index in range(1025):
            key = digest(canonical({'cache_request': cache_request, 'sequence': index}))
            if not acceptance._present(project.root, f'check-jobs/{key}'):
                return key, False
            if (not (root(project) / key).is_dir()
                    or not acceptance._present(project.root, f'check-jobs/{key}/job.json')):
                raise Denied('Incomplete check request record')
        raise Busy('Retained check evidence is full; inspect and acknowledge results')

    cache_configured = not custom and bool(project.config.sandbox.check_cache_paths)
    with contextlib.ExitStack() as stack:
        pending_binding = False
        if not custom:
            try:
                stack.enter_context(cache_guard(project, blocking=False))
            except CacheOwned:
                pending_binding = True
        # Pending retries have priority even after the owner releases the lock.
        if cache_configured:
            with lock(project.root / 'locks/check-jobs.lock'):
                pending_id, exists = request_slot()
                if exists:
                    _state_purpose(project, pending_id, purpose)
                    _retire_superseded(project, owner, purpose, keep={pending_id}, replaced_by=pending_id)
                    return _delivered_summary(project, pending_id, owner)
        current = cache_input(project) if not custom and not pending_binding else None
        if cache_configured and not pending_binding and current is None:
            raise Denied('Cannot bind configured check cache inputs; verification not queued')
        cache_binding = ({'cache_input': current}
                         if cache_configured and not pending_binding else {})
        # A fresh request asks for a measurement of its own: it never joins an
        # existing execution. Its own result still carries the shared key, so a
        # later request with the same inputs reuses that measurement instead of
        # repeating it. Asking for a remeasurement and sharing the result are
        # separate decisions.
        shared_key = digest(canonical({'source': capture['code_digest'], 'execution': execution_key(context.config),
                                       'commands': job_commands, 'kind': 'verify' if not custom else kind,
                                       'environment': env, **cache_binding})) if (env is not None and not scratch_paths
                                       and not pending_binding and (not cache_binding or current is not None)) else None
        identity = {'run': context.run_dir.name, 'source': capture['code_digest'],
                    'execution': execution_key(context.config), 'commands': job_commands,
                    'kind': 'verify' if not custom else kind, 'environment': env, 'fresh': fresh}
        if scratch_paths:
            identity['scratch_paths'] = list(scratch_paths)
        job_id = digest(canonical(identity))
        directory = root(project) / job_id
        with lock(project.root / 'locks/check-jobs.lock'):
            if cache_configured:
                pending_id, exists = request_slot()
                if exists:
                    _state_purpose(project, pending_id, purpose)
                    _retire_superseded(project, owner, purpose, keep={pending_id}, replaced_by=pending_id)
                    return _delivered_summary(project, pending_id, owner)
                # Every configured-cache submission has its own durable
                # request identity. Cache-specific execution and sharing
                # identity is bound separately below, under cache ownership.
                job_id = pending_id
                directory = root(project) / job_id
            if (directory / 'job.json').exists():
                if not cache_matches(project, read(project, job_id), current):
                    raise Denied('Existing check lacks matching cache inputs')
                # The same request in the same unit is one demand: state its
                # purpose and retire the role's other demand for that purpose.
                _state_purpose(project, job_id, purpose)
                _retire_superseded(project, owner, purpose, keep={job_id}, replaced_by=job_id)
                return _delivered_summary(project, job_id, owner)
            if shared_key is not None and not fresh:
                for path in sorted(root(project).glob('*/job.json')):
                    if cache_configured:
                        from .acceptance import read_job
                        other = read_job(project, path.parent.name)
                    else:
                        other = read_json(path, {})
                    if (other.get('shared_key') == shared_key and reusable(other)
                            and cache_matches(project, other, current)
                            and not (path.parent / 'cancel.json').exists()):
                        job = _subscribe(context, other, purpose=purpose,
                                         request=cache_request if cache_configured else None,
                                         request_id=job_id if cache_configured else None,
                                         request_fresh=fresh if cache_configured else None,
                                         request_bounds=env_bounds if cache_configured else None,
                                         request_reuse=project.config.sandbox.check_reuse if cache_configured else None)
                        # The shared job is the surviving work, so it is never
                        # the one this request replaces.
                        _retire_superseded(project, owner, purpose,
                                           keep={other['id'], job['id']}, replaced_by=job['id'])
                        return job
            pending = _pending_jobs(project)
            counted = {job['id'] for job in pending}
            superseded = _superseded(project, owner, purpose, keep={job_id})
            # Only demand the queue still counts can be credited. A job that was
            # already replaced holds no capacity, so crediting it a second time
            # would let the queue grow past its cap while an earlier cancellation
            # is still outstanding.
            freed = [job for job in superseded
                     if job['id'] in counted and _withdrawable(project, job)]
            unfinished = len(pending) - len(freed)
            _make_room(project)
            if unfinished >= context.config.limits.check_pending:
                raise Busy('Check job queue is full')
            mkdir(directory)
            try:
                project.snapshots.materialize(capture, directory / 'input')
                job = {'id': job_id, 'role': owner, 'producer_role': context.role.name, 'run': context.run_dir.name,
                       'kind': identity['kind'],
                       'code_digest': capture['code_digest'], 'execution': identity['execution'],
                       'commands': job_commands, 'status': 'queued', 'created_at': now(),
                       'acknowledged': False, 'environment': env, 'shared_key': shared_key,
                       'inputs_bound_required': context.config.sandbox.check_reuse}
                if cache_configured:
                    job['cache_request'] = cache_request
                    # Durable request mode and bounds survive binding and
                    # start; they are validated and compared on every lookup,
                    # even when the environment itself is unbound (None).
                    job['fresh'] = fresh
                    job['environment_bounds'] = dict(env_bounds)
                if pending_binding:
                    job.pop('shared_key', None)
                    job['cache_binding_pending'] = 1
                if env is not None and not cache_configured:
                    job['environment_bounds'] = env_bounds
                if cache_binding:
                    job.update(cache_binding)
                if scratch_paths:
                    job['scratch_paths'] = list(scratch_paths)
                if purpose is not None:
                    job['purpose'] = purpose
                write_json(directory / 'job.json', job)
                os.utime(root(project), None)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            for record in superseded:
                _withdraw_locked(project, record['id'], role=owner, replaced_by=job_id)
        return summary(job)


def _withdrawable(project, job):
    """Whether the record's demand ends when its owner replaces it.

    A shared execution still runs for its remaining consumers, so it keeps
    its place in the queue until the last consumer leaves.
    """
    records = [read_json(p, {}) for p in root(project).glob('*/job.json')]
    return not _interest(records, job['id'], exclude_id=job['id'])


def _retire_superseded(project, role, purpose, *, keep=frozenset(), replaced_by=None):
    """Withdraw the calling role's own earlier demand for the same purpose."""
    for record in _superseded(project, role, purpose, keep=keep):
        _withdraw_locked(project, record['id'], role=role, replaced_by=replaced_by)


def summary(job):
    continuation = ({'wait': {'operation': 'finish',
                             'arguments': {'outcome': 'wait', 'wait_for_check': job['id']},
                             'supply': ['summary', 'state'],
                             'resumes_on': 'check completion or new input'}}
                    if job.get('status') in ('queued', 'running') else {})
    return {**continuation, **({'accepted_for_current_source': False} if job.get('kind') == 'verify' and job.get('status') in ('queued', 'running') else {}), **{'command_count': len(job.get('commands', [])),
               'command_preview': [text_preview(c, 128)[0] for c in job.get('commands', [])[:4]]},
            **{k: text_preview(job[k], 512)[0] if isinstance(job[k], str) else job[k]
               for k in ('id', 'role', 'run', 'kind', 'code_digest', 'status',
                               'created_at', 'finished_at', 'passed', 'error', 'shared_job',
                               'purpose', 'replaced_by', 'scratch_paths') if k in job}}


def pending(project, role, *, discovery=None):
    # Keep a list result: diagnostics alone are never a completion wake.
    if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
        return []
    if discovery is None:
        from .acceptance import discover
        discovery = discover(project)
    return [summary(j) for j in discovery[0].values() if j.get('role') == role
            and j.get('status') in ('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed')
            and not j.get('acknowledged')][:32]


def list_jobs(project, role, *, include_acknowledged=True, discovery=None):
    if role not in project.config.roles or 'check_job' not in project.config.roles[role].capabilities:
        return []
    if discovery is None:
        from .acceptance import discover
        discovery = discover(project)
    jobs = [j for j in discovery[0].values() if j.get('role') == role
            and (include_acknowledged or not j.get('acknowledged'))]
    return [summary(j) for j in sorted(jobs, key=lambda j: j.get('created_at', ''), reverse=True)[:32]]


def prompt_projection(context):
    """Share one read snapshot across prompt views; cap aggregate UTF-8 bytes."""
    if 'check_job' not in context.role.capabilities:
        return {'check_jobs': [], 'check_continuation': None, 'shared_checks': []}
    from .acceptance import discover, producer_diagnostics
    discovery = discover(context.project)
    result = {'check_jobs': [],
              'check_continuation': continuation(context.project, context.role.name,
                                                context.snapshot, discovery=discovery),
              'shared_checks': [],
              'check_discovery': {'coverage_complete': not discovery[1],
                                  'summaries_omitted': 0, 'continuation_omitted': False,
                                  **producer_diagnostics(discovery[1])}}
    views = (
        ('check_jobs', list_jobs(context.project, context.role.name,
                                 include_acknowledged=False, discovery=discovery)),
        ('shared_checks', available(context, validate=False, discovery=discovery)))
    result['check_discovery']['summaries_omitted'] = sum(len(values) for _, values in views)
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 65536:
        result['check_continuation'] = None
        result['check_discovery']['continuation_omitted'] = True
    for key, values in views:
        for value in values:
            result[key].append(value)
            result['check_discovery']['summaries_omitted'] -= 1
            if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 65536:
                result[key].pop()
                result['check_discovery']['summaries_omitted'] += 1
    return result


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
        context.close_exec()
        capture = project.snapshots.capture_files(context.workspace)
        # Import the execution's proof, not its mutable compiler outputs.
        # Older results may omit the echo, but the job must still bind an input.
        bound_input = job.get('cache_input')
        cache_bound = (not project.config.sandbox.check_cache_paths
                       or isinstance(bound_input, str) and DIGEST.fullmatch(bound_input) is not None
                       and record.get('cache_input', bound_input) == bound_input)
        matches = (not capture['skipped'] and capture['code_digest'] == job['code_digest']
                   and execution_key(context.config) == job['execution']
                   and list(project.verify) == job['commands']
                   and cache_bound
                   and (not job.get('inputs_bound_required') or job.get('environment') is not None)
                   and (job.get('environment') is None or execution_environment(project, job) == job['environment'])
                   and 'verify' in context.role.capabilities)
        with lock(project.root / 'locks/check-jobs.lock'):
            # A subscriber may have detached while inputs were inspected.
            matches = matches and read(project, job_id)['status'] == 'completed'
            output['accepted_for_current_source'] = matches
            if matches:
                context.verification = record
                write_json(context.run_dir / 'verification.json', record)
    consumed = getattr(context, 'check_results_read', set())
    consumed.add(job_id)
    context.check_results_read = consumed
    return output



def logs(context, job_id, evidence_id=None, *, offset=0, limit=8192, content_id=None):
    job = read(context.project, job_id)
    if context.role.name not in (job['role'], job.get('producer_role')):
        raise Denied('Check job belongs to another role')
    if evidence_id is None:
        directory = root(context.project) / job.get('shared_job', job_id) / 'commands'
        candidates = sorted(p.stem for p in directory.glob('*.json')
                            if re.fullmatch(r'[a-f0-9]{32}', p.stem) and p.is_file() and not p.is_symlink())[:32]
        return {'trust': 'recorded-local-untrusted', 'status': job['status'],
                'commands': [{'evidence_id': identifier,
                              'request': {'action': 'logs', 'id': job_id, 'evidence_id': identifier}}
                             for identifier in candidates]}
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


def _interest(records, original_id, *, exclude_id=None):
    """Whether any consumer other than ``exclude_id`` still wants the job."""
    return any(not record.get('detached') and record.get('id') != exclude_id
               and (record.get('id') == original_id or record.get('shared_job') == original_id)
               for record in records)


def _pending_jobs(project):
    """Physical jobs that still demand execution, counted against the cap.

    A job whose owner replaced it has no demand left, so it leaves the
    queue. A shared execution still runs for its remaining consumers, so it
    keeps its place until the last consumer leaves.
    """
    records = [read_json(p, {}) for p in root(project).glob('*/job.json')]
    pending = []
    for record in records:
        if not isinstance(record, dict) or record.get('shared_job'):
            continue
        if record.get('status') not in ('queued', 'running'):
            continue
        if record.get('replaced_by') and not _interest(records, record['id'], exclude_id=record['id']):
            continue
        pending.append(record)
    return pending


def _superseded(project, role, purpose, *, keep=frozenset()):
    """This role's own unfinished physical jobs that a same-purpose request replaces.

    Only the calling role's own physical jobs are eligible: a subscription
    belongs to its subscriber and another role's job is never this role's
    demand to retire.
    """
    if purpose is None:
        return []
    found = []
    for path in sorted(root(project).glob('*/job.json')):
        record = read_json(path, {})
        if not isinstance(record, dict) or record.get('shared_job'):
            continue
        if record.get('role') != role or record.get('purpose') != purpose:
            continue
        if record.get('id') in keep or record.get('status') not in ('queued', 'running'):
            continue
        found.append(record)
    return found


def _state_purpose(project, job_id, purpose):
    """State the request's purpose on the record that satisfies it."""
    if purpose is None:
        return
    raw = raw_read(project, job_id)
    if raw.get('purpose') == purpose:
        return
    raw['purpose'] = purpose
    write_json(root(project) / job_id / 'job.json', raw)


def _withdraw_locked(project, job_id, *, role, replaced_by=None):
    """Withdraw one owned job's demand to execute, and stop the runner if alone.

    The caller holds the check-jobs lock; the lock is not re-entrant, so the
    public cancel path and the replacement path both come through here
    instead of calling each other.

    The record keeps its directory, its frozen input and its command output:
    only the demand to run is withdrawn. The physical runner is asked to
    cancel the job only when no other consumer still wants it, so a shared
    execution survives for the consumers that remain.
    """
    job = read(project, job_id)
    if job.get('role') != role:
        raise Denied('Check job belongs to another role')
    if job['status'] not in ('queued', 'running'):
        return summary(job)
    original_id = job.get('shared_job', job_id)
    raw = raw_read(project, job_id)
    raw['detached'] = True
    if replaced_by is not None:
        # The owner replaced this demand on purpose: the record states what
        # replaced it and raises no completion event of its own.
        raw['replaced_by'] = replaced_by
        raw['acknowledged'] = True
    write_json(root(project) / job_id / 'job.json', raw)
    records = [read_json(p, {}) for p in root(project).glob('*/job.json')]
    interested = _interest(records, original_id, exclude_id=job_id)
    if replaced_by is not None and not interested and raw.get('status') == 'queued':
        # The replaced job never started: it leaves the queue at once, so the
        # runner never spends a slot on it.
        raw['status'] = 'cancelled'
        write_json(root(project) / job_id / 'job.json', raw)
    if not interested:
        write_json(root(project) / original_id / 'cancel.json', {'requested_at': now()})
    os.utime(root(project), None)
    return {**summary(read(project, job_id)), 'cancel_requested': True}


def cancel(context, job_id):
    project = context.project
    with lock(project.root / 'locks/check-jobs.lock'):
        return _withdraw_locked(project, job_id, role=context.role.name)


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
    from . import acceptance
    invalid = {}
    result = _run_one(project, stop, sandbox_factory, invalid)
    return {**result, **acceptance.producer_diagnostics(invalid)}


def _run_one(project, stop, sandbox_factory, invalid):
    from . import acceptance
    # Never while holding check-jobs.lock. Keep ALL invalid IDs out of the
    # scheduler's reads/sort, even when only 32 diagnostics can be displayed.
    invalid.update(acceptance.recover_producers(project))
    from .workspace_cache import maintain
    maintain(project)  # No owner/cache/slot exclusion has been acquired yet.
    control = project.control()
    if not control.get('armed') or control.get('paused') or control.get('draining') or stop.is_set():
        return {'status': 'deferred'}
    if shutil.disk_usage(project.root).free < project.config.limits.free_disk_mb * 1048576:
        return {'status': 'disk-reserve'}
    blocked = False
    candidates = []
    with lock(project.root / 'locks/check-jobs.lock'):
        for path in acceptance.job_paths(project, invalid):
            if path.parent.name in invalid:
                continue
            try:
                job = acceptance.read_job(project, path.parent.name)
            except acceptance.InvalidProducer as exc:
                invalid[exc.job_id] = exc.code
                continue
            candidates.append((path, job))
    for path, job in sorted(candidates, key=lambda pair: pair[1].get('created_at', '')):
        if job.get('shared_job') or job.get('status') not in ('queued', 'running'):
            continue
        # Acquire only the resources this job uses. A blocked cache owner or
        # live job must not hold a compute slot or stop unrelated queued work.
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(lock(path.parent / 'owner.lock', blocking=False))
                if ((job.get('kind') == 'verify' and project.config.sandbox.check_cache_paths)
                        or job.get('scratch_paths')):
                    stack.enter_context(lock(project.root / 'locks/check-cache.lock', blocking=False))
            except Busy:
                blocked = True
                continue
            try:
                with lock(project.root / 'locks/check-jobs.lock'):
                    if not acceptance._present(project.root, f'check-jobs/{path.parent.name}/job.json'):
                        continue  # Acknowledged cache eviction since enumeration.
                    job = acceptance.read_job(project, path.parent.name)
            except acceptance.InvalidProducer as exc:
                invalid[exc.job_id] = exc.code
                continue
            if job.get('shared_job') or job.get('status') not in ('queued', 'running'):
                continue
            for slot in range(project.config.limits.parallel_checks):
                fault = project.config.data / 'check-slots' / f'{slot}.fault.json'
                if fault.exists():
                    continue
                guard = lock(project.config.data / 'check-slots' / f'{slot}.lock', blocking=False)
                try:
                    guard.__enter__()
                except Busy:
                    continue
                if fault.exists():
                    guard.__exit__(None, None, None)
                    continue
                stack.callback(guard.__exit__, None, None, None)
                break
            else:
                return {'status': 'busy'}
            try:
                stack.enter_context(lock(check_slot_exclusion(project.root, slot), blocking=False))
            except Busy:
                blocked = True
                continue
            if job['status'] == 'running':
                try:
                    cleanup(project.config, project.root, job_role(job['id']),
                            owner=True, exclusion=check_slot_exclusion(project.root, slot))
                except Exception as exc:
                    return quarantine(project, job, slot, str(exc))
                job.update(status='interrupted', error='Runner exited before recording completion', finished_at=now())
                save_execution(project, job)
                shutil.rmtree(path.parent / 'input', ignore_errors=True)
                os.utime(root(project), None)
                return summary(job)
            try:
                result = _execute(project, job, stop, sandbox_factory, slot)
            except acceptance.InvalidProducer as exc:
                # Execution has finalized; only its retention content failed.
                invalid[exc.job_id] = exc.code
                os.utime(root(project), None)
                result = summary(job)
            if result['status'] == 'cleanup_failed':
                return quarantine(project, read(project, job['id']), slot, result.get('error', 'Cleanup failed'))
            return result
    return {'status': 'busy' if blocked else 'idle'}


def run_pending(project, stop, *, sandbox_factory=Sandbox):
    """Drain a bounded batch, releasing admission between jobs.

    At most check_pending (1–1024) terminal jobs are processed. No polling or
    sleeping: idle, resource contention, pause, cancellation or cleanup failure
    returns immediately to the operator's existing event/recovery trigger.
    Each job retains its own durable result and frozen input checks.
    """
    from . import acceptance
    invalid = {}
    processed = 0
    for _ in range(project.config.limits.check_pending):
        result = _run_one(project, stop, sandbox_factory, invalid)
        if result.get('status') not in ('completed', 'refused', 'cancelled', 'interrupted'):
            break
        processed += 1
    return {**result, 'processed': processed, **acceptance.producer_diagnostics(invalid)}


def _execute(project, job, stop, sandbox_factory, slot):
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
    # Verification cache inputs bind at execution start under the caller's
    # check-cache.lock (held by _run_one across _execute), never at enqueue.
    # The request identity (job id, derived at submit) is preserved; the
    # execution reuse identity (cache_input/shared_key) is rebound here to
    # the actual fingerprint. A legitimate change since enqueue therefore
    # runs instead of refusing. Fail closed when the execution input cannot
    # be bounded or when the record predates cache binding.
    if job.get('cache_binding_pending') and not (job['kind'] == 'verify' and project.config.sandbox.check_cache_paths):
        job.update(status='refused', error='Execution configuration, dependency or cache inputs changed', finished_at=now())
        save_execution(project, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if job['kind'] == 'verify' and project.config.sandbox.check_cache_paths:
        if 'cache_input' not in job and job.get('cache_binding_pending') != 1:
            job.update(status='refused', error='Execution configuration, dependency or cache inputs changed', finished_at=now())
            save_execution(project, job)
            shutil.rmtree(directory / 'input', ignore_errors=True)
            os.utime(root(project), None)
            return summary(job)
        current_cache = cache_input(project)
        if current_cache is None:
            job.update(status='refused', error='Cannot bind configured check cache inputs; verification refused', finished_at=now())
            save_execution(project, job)
            shutil.rmtree(directory / 'input', ignore_errors=True)
            os.utime(root(project), None)
            return summary(job)
    else:
        current_cache = None
    if (job['kind'] == 'verify' and job.get('inputs_bound_required') and job.get('environment') is None) or (job.get('environment') is not None and execution_environment(project, job) != job['environment']) or job['execution'] != execution_key(project.config) or (job['kind'] == 'verify' and list(project.verify) != job['commands']):
        job.update(status='refused', error=('Dependency inputs could not be bound for reusable acceptance'
                   if job.get('inputs_bound_required') and job.get('environment') is None
                   else 'Execution configuration, dependency or cache inputs changed'), finished_at=now())
        save_execution(project, job)
        shutil.rmtree(directory / 'input', ignore_errors=True)
        os.utime(root(project), None)
        return summary(job)
    if current_cache is not None:
        # Rebind the actual execution input before staging any cache tree.
        # The job id stays the submit-time request identity; shared_key is
        # the execution reuse identity and must match the recorded input so
        # later reuse requires the same current cache.
        job['cache_input'] = current_cache
        job.pop('cache_binding_pending', None)
        if job.get('environment') is not None and not job.get('scratch_paths'):
            job['shared_key'] = digest(canonical({'source': job['code_digest'], 'execution': job['execution'],
                                                  'commands': job['commands'], 'kind': 'verify',
                                                  'environment': job['environment'], 'cache_input': current_cache}))
        else:
            job['shared_key'] = None
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
    sandbox = sandbox_factory(project.config, project.root, job_role(job['id']), directory,
                              cancel=lambda: stop.is_set() or (directory / 'cancel.json').exists())
    workspace = directory / 'input'
    try:
        before = project.snapshots.capture_files(workspace)
        if before['code_digest'] != job['code_digest'] or before['skipped']:
            raise Denied('Check job source changed before execution')
        cache = project.root / 'check-cache' / job['execution']
        scratch_paths = scratch_selection(project.config, job.get('scratch_paths'))
        if scratch_paths and job['kind'] != 'experiment':
            raise Denied('Scratch requires a custom queued experiment')
        cache_names = project.config.sandbox.check_cache_paths if job['kind'] == 'verify' else scratch_paths
        for name in cache_names:
            if not project.snapshots.excluded(name) or (workspace / name).exists() or (workspace / name).is_symlink():
                raise Denied('Build cache must be excluded from source: ' + name)
            previous = cache / name
            if previous.exists() or previous.is_symlink():
                if previous.is_symlink() or not previous.is_dir():
                    raise Denied('Build cache is not a directory')
                previous.replace(workspace / name)
            if scratch_paths:
                mkdir(workspace / name)
        results = []
        for command in job['commands']:
            if stop.is_set():
                raise Denied('Check job interrupted')
            if (directory / 'cancel.json').exists():
                break
            scratch = {'scratch_paths': scratch_paths} if scratch_paths else {}
            results.append(sandbox.execute(workspace, command, writable=job['kind'] == 'verify', experiment=job['kind'] == 'experiment', **scratch))
        after = project.snapshots.capture_files(workspace)
        unchanged = after['code_digest'] == before['code_digest']
        representable = not after['skipped']
        dependencies_unchanged = ((job.get('environment') is None or execution_environment(project, job) == job['environment'])
                                  and execution_key(project.config) == job['execution']
                                  and (job['kind'] != 'verify' or list(project.verify) == job['commands']))
        passed = (unchanged and representable and dependencies_unchanged
                  and [r['script'] for r in results] == job['commands']
                  and all(r['exit_code'] == 0 and r['reason'] == 'exited' for r in results))
        record = {'passed': passed, 'dependencies_unchanged': dependencies_unchanged, 'code_digest': job['code_digest'],
                  'snapshot_representable': representable, 'unchanged_during_verification': unchanged,
                  'image': project.config.sandbox.image, 'run': job['run'], 'job': job['id'],
                  'created_at': now(), 'commands': [{**{k: r[k] for k in ('id', 'exit_code', 'reason')},
                      'script': text_preview(r['script'], 512)[0],
                      'stdout': text_preview(r.get('stdout', ''), 512)[0],
                      'stderr': text_preview(r.get('stderr', ''), 512)[0],
                      'evidence': 'check-jobs/' + job['id'] + '/commands/' + r['id'] + '.json'} for r in results]}
        if job['kind'] == 'verify' and project.config.sandbox.check_cache_paths:
            record['cache_input'] = job['cache_input']
        write_json(directory / 'result.json', record)
        job.update(status='cancelled' if (directory / 'cancel.json').exists() else 'completed', passed=passed and not (directory / 'cancel.json').exists(), finished_at=now())
    except Exception as exc:
        job.update(status='interrupted', error=str(exc)[:512], finished_at=now())
        try:
            # The runner holds the exclusion of the slot it occupies, so
            # its own cleanup takes that exclusion and leaves no exclusion
            # file of the job's own behind.
            cleanup(project.config, project.root, job_role(job['id']),
                    owner=True, exclusion=check_slot_exclusion(project.root, slot))
        except Exception as cleanup_exc:
            # The cleanup failed, so the container may still be running:
            # the job is recorded as needing recovery, never as finished.
            job.update(status='cleanup_failed', error=('Container cleanup requires recovery: ' + str(cleanup_exc))[:512])
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
            if project.config.sandbox.check_cache_paths:
                # Informational only: unbindable outputs do not invalidate the
                # proof of this execution or replace its immutable input.
                record['cache_output'] = cache_input(project)
                write_json(directory / 'result.json', record)
        if not job.get('scratch_paths'):
            shutil.rmtree(directory / 'input', ignore_errors=True)
    clean_scratch = (bool(job.get('scratch_paths')) and job['status'] == 'completed'
                     and all(r['reason'] == 'exited' for r in results))
    if clean_scratch:
        try:
            # Delete frozen source before committing artifacts. Neither source
            # cleanup nor potentially large scratch discards hold the queue lock.
            for child in workspace.iterdir():
                if child.name not in scratch_paths:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        except OSError as exc:
            job.update(status='cleanup_failed', passed=False,
                       error=('Scratch cleanup requires recovery: ' + str(exc))[:512])
    # Finalize only after execution/cache cleanup. Until then the producer is
    # running on disk and cannot be collected or evicted. Cancel, artifact
    # commit and terminal publication serialize on check-jobs.lock.
    while True:
        discard_scratch = False
        with lock(project.root / 'locks/check-jobs.lock'):
            latest = raw_read(project, job['id'])
            for key in ('detached', 'acknowledged', 'replaced_by', 'purpose'):
                if key in latest:
                    job[key] = latest[key]
            if job['status'] == 'completed' and (stop.is_set() or (directory / 'cancel.json').exists()):
                job.update(status='cancelled', passed=False)
            if job.get('scratch_paths') and job['status'] != 'cleanup_failed' and workspace.exists():
                if job['status'] == 'completed' and clean_scratch:
                    try:
                        cache = project.root / 'check-cache' / job['execution']
                        mkdir(cache)
                        for name in scratch_paths:
                            artifact = workspace / name
                            if artifact.is_dir() and not artifact.is_symlink():
                                artifact.replace(cache / name)
                        workspace.rmdir()
                    except OSError as exc:
                        job.update(status='cleanup_failed', passed=False,
                                   error=('Scratch cleanup requires recovery: ' + str(exc))[:512])
                else:
                    discard_scratch = True
            if not discard_scratch:
                if job['status'] == 'completed' and job.get('passed') and job['kind'] == 'verify' and job.get('environment') is not None:
                    bound = {'commands': job['commands'], 'execution': job['execution'],
                             'environment': job['environment'], 'image': record['image']}
                    if 'cache_input' in job:
                        bound['cache_input'] = job['cache_input']
                    from .acceptance import MAX_BYTES
                    if len(canonical(record)) > project.config.limits.file_bytes:
                        # The exact canonical result bytes are what recovery reads.
                        # A fitting job/scope alone must not create a pinned producer.
                        job['acceptance_retention_refusal'] = 'result_file_bytes'
                    elif (len(canonical(bound)) <= MAX_BYTES // 2
                            and len(canonical({**job, 'acceptance_scope': bound})) <= project.config.limits.file_bytes):
                        job['acceptance_scope'] = bound
                write_json(path, job)
        if not discard_scratch:
            break
        try:
            shutil.rmtree(workspace)
        except OSError as exc:
            job.update(status='cleanup_failed', passed=False,
                       error=('Scratch cleanup requires recovery: ' + str(exc))[:512])
    from .acceptance import recover_job
    recover_job(project, job['id'])
    os.utime(root(project), None)
    return summary(job)


def wait_signature(project):
    control = project.control()
    pointer = read_json(project.root / 'current.json', {})
    # A new publication, operator wake or incoming decision is independent work.
    return {'snapshot': pointer.get('snapshot'), 'wake': control.get('wake_generation'),
            'inbox': project.insights.generation()}


def set_wait(project, role, job_id, wait_until=None, *, continuation=None):
    """Record the role's deferral on one job and the bound that ends it.

    Schema: ``check-jobs/<role>.wait.json`` carries the job id, the wait
    signature and ``wait_until``, the epoch second at which the deferral
    ends. Bounds: one record per role and one job per record; the bound is
    a single number. Trust: local recorded state. Failure: a job that
    belongs to another role is refused; a bound that is not a positive
    number is refused. The bound defaults to the project's configured
    default wait, so a recorded deferral is never unbounded. Only finite
    positive epoch bounds are accepted.
    """
    job = read(project, job_id)
    if job['role'] != role:
        raise Denied('Check wait belongs to another role')
    if wait_until is None:
        wait_until = time.time() + project.config.limits.default_wait_seconds
    deadline = _positive_finite_deadline(wait_until)
    if deadline is None:
        raise Denied('Invalid check wait bound')
    record = {'id': job_id, 'inputs': wait_signature(project), 'wait_until': deadline}
    if continuation is not None:
        record['continuation'] = {k: continuation[k] for k in ('run', 'snapshot', 'code_digest', 'summary', 'state') if k in continuation}
    write_json(root(project) / (role + '.wait.json'), record)
    return record


def deferral(project, role):
    """The role's live deferral on one check job, with the bound that ends it.

    Schema: ``{"id", "wait_until", "status"}`` for the job that holds the
    role now, or ``None`` when no deferral holds it. Bounds: one record per
    role, one job per record, one scalar bound. Trust: local recorded state;
    the job record is the authority for the job's status. Failure: a
    missing, malformed, stale or elapsed record holds nothing, so the role
    is admitted rather than held by state nobody can read. A deferral ends
    when the job reaches a terminal status, when the recorded inputs go
    stale, or when its bound passes, whichever comes first.
    """
    try:
        if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
            return None
        record = read_json(root(project) / (role + '.wait.json'), {})
        if not isinstance(record, dict) or record.get('inputs') != wait_signature(project):
            return None
        deadline = _positive_finite_deadline(record.get('wait_until'))
        if deadline is None or time.time() >= deadline:
            # The bound that ended the unit's wait ended this deferral too.
            return None
        job_id = record.get('id')
        status = read(project, job_id)['status']
    except (Denied, OSError, ValueError, KeyError, TypeError):
        # Consumed evidence may be reaped; its stale wait is no longer pending.
        return None
    if status not in ('queued', 'running'):
        return None
    return {'id': job_id, 'wait_until': float(deadline), 'status': status}


def deferrals(project):
    """Every role the check queue holds now, with the bound that ends it."""
    held = {}
    for name, role in project.config.roles.items():
        if "check_job" not in role.capabilities:
            continue
        hold = deferral(project, name)
        if hold is not None:
            held[name] = hold
    return held
def clear_wait(project, role):
    (root(project) / (role + '.wait.json')).unlink(missing_ok=True)


def continuation(project, role, snapshot, *, discovery=None):
    """Bounded prior-role report for a check-wait continuation, never evidence.

    Available only to the owning role with check_job. Source changes are
    explicit: the prior report cannot certify the current code or acceptance.
    """
    if role not in project.config.roles or 'check_job' not in project.config.roles[role].capabilities:
        return None
    record = read_json(root(project) / (role + '.wait.json'), {})
    report = record.get('continuation')
    if not isinstance(report, dict) or not isinstance(report.get('run'), str):
        return None
    if discovery is None:
        from .acceptance import discover
        discovery = discover(project)
    job = discovery[0].get(record.get('id')) if isinstance(record.get('id'), str) else None
    if job is None or job.get('role') != role:
        return None
    return {'trust': 'prior-role-report', 'run': text_preview(report['run'], 128)[0],
            **{k: text_preview(report[k], 128)[0] if isinstance(report.get(k), str) else None
               for k in ('snapshot', 'code_digest')},
            'same_code': report.get('code_digest') == snapshot.get('code_digest'),
            'summary': text_preview(str(report.get('summary', '')), 4000)[0],
            'state': text_preview(str(report.get('state', '')), 4000)[0],
            'check': summary(job)}


def waiting(project, role):
    """True while a check job holds the role."""
    return deferral(project, role) is not None


#: Terminal statuses a completed explicit wait can wake on.
TERMINAL_WAKE_STATUSES = frozenset(('completed', 'interrupted', 'refused', 'cancelled', 'cleanup_failed'))


def wake_ready(project, role, *, discovery=None):
    """True only when the role's explicit wait names a terminal owned result.

    Schema: the role's ``<role>.wait.json`` record must name one job, carry
    the current ``wait_signature`` and an unexpired ``wait_until`` bound.
    Bounds: one record per role, one job per record. Trust: local recorded
    state; the job record is the authority for status and acknowledgement.
    Failure: a missing, malformed, stale, expired, other-role, running or
    acknowledged record reads as no wake, never as admission. Listing and
    result visibility are unchanged: a completed job without such a wait
    stays inspectable but wakes nobody.
    """
    try:
        if role not in project.config.roles or "check_job" not in project.config.roles[role].capabilities:
            return False
        record = read_json(root(project) / (role + '.wait.json'), {})
        if not isinstance(record, dict) or record.get('inputs') != wait_signature(project):
            return False
        deadline = _positive_finite_deadline(record.get('wait_until'))
        if deadline is None or time.time() >= deadline:
            return False
        job_id = record.get('id')
        if not isinstance(job_id, str):
            return False
        if discovery is not None:
            job = discovery[0].get(job_id)
            if job is None:
                return False
        else:
            job = read(project, job_id)
        if job.get('role') != role:
            return False
        if job.get('status') not in TERMINAL_WAKE_STATUSES:
            return False
        if job.get('acknowledged'):
            return False
    except (Denied, OSError, ValueError, KeyError, TypeError):
        return False
    return True


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
    refused = []
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
            # The recovery takes the exclusion of the slot it is clearing,
            # so a sweep refuses while it removes the container and no
            # exclusion file of the job's own is left behind.
            removed = cleanup(project.config, project.root, job_role(job['id']),
                             exclusion=check_slot_exclusion(project.root, slot))
            if cleanup_refused(removed):
                # The container is still there, so the slot stays reserved
                # and the operator is told the recovery did not happen.
                refused.append(job['id'])
                continue
            job.update(status='interrupted', error='Cleanup recovered; previous execution did not complete', finished_at=now())
            save_execution(project, job)
            fault.unlink()
            shutil.rmtree(root(project) / job['id'] / 'input', ignore_errors=True)
            os.utime(root(project), None)
            recovered.append(job['id'])
    return {'recovered': recovered, 'refused': refused}
