"""Non-releasing observation/cancellation of one delegation's user unit.

``observe(config, run_id)`` derives the unit; no caller-selected unit or manager
endpoint is accepted. Eight sequential systemctl/busctl reads are bounded
individually by services.systemd_verify_timeout_seconds and limits.output_bytes.
One minimal process environment selects the current user manager and pins
UTF-8/C locale.
Endpoint identity is revalidated around the reads; no retries or cgroup reads
are provided. Each new D-Bus connection still has a TOCTOU window. The
internal cancellation path runs only after DelegationJobs commits and releases
its fence lock; it uses the same per-command bounds for an exact-unit stop.
Manager-local job IDs remain diagnostic only and are never mutated. Failure
raises Denied without partial evidence.

The decoder records any nonempty syntactically numeric manager version
(numeric major with optional dotted components and a distro suffix). This is
a parsing boundary, NOT a claim that any host or manager version is verified
for lifecycle settlement.
A successful, exactly empty list-jobs stdout is the explicit empty response with
--no-legend; whitespace, diagnostics and incomplete reads are not an empty queue.
Even an empty queue is only a point-in-time observation, never quiescence proof.
"""
from dataclasses import dataclass, field
import os
import re
import stat
import sys

from . import process
from .config import Config
from .delegation_jobs import user_cgroup
from .errors import Denied


@dataclass(frozen=True)
class PendingJob:
    id: int
    unit: str
    type: str
    state: str


@dataclass(frozen=True)
class ManagerPin:
    uid: int
    boot_id: str
    socket_path: str
    socket_identity: tuple
    version: str
    name_owner: str


@dataclass(frozen=True)
class Observation:
    unit: str
    manager: ManagerPin
    load_state: str
    active_state: str
    sub_state: str
    control_group: str
    invocation_id: str
    jobs: tuple[PendingJob, ...]
    point_in_time: bool = field(default=True, init=False)
    release_authority: bool = field(default=False, init=False)

    @property
    def manager_version(self):
        return self.manager.version


# Wire vocabulary understood by this decoder, not lifecycle policy.
_LOAD = {'stub', 'loaded', 'not-found', 'bad-setting', 'error', 'merged', 'masked'}
_ACTIVE = {'active', 'reloading', 'inactive', 'failed', 'activating', 'deactivating', 'maintenance'}
_SUB = {'dead', 'condition', 'start-pre', 'start', 'start-post', 'running', 'exited',
        'reload', 'reload-signal', 'reload-notify', 'stop', 'stop-watchdog',
        'stop-sigterm', 'stop-sigkill', 'stop-post', 'final-watchdog',
        'final-sigterm', 'final-sigkill', 'failed', 'auto-restart',
        'auto-restart-queued', 'cleaning', 'dead-before-auto-restart', 'failed-before-auto-restart'}
_JOB_TYPES = {'start', 'verify-active', 'stop', 'reload', 'restart', 'try-restart',
              'reload-or-start', 'nop'}
_PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'ControlGroup', 'InvocationID')


def _fields(text: str, names: tuple[str, ...]) -> dict[str, str]:
    fields = {}
    # Require complete newline-terminated property records; never salvage a prefix.
    if not text.endswith('\n') or '\r' in text or not text.isascii():
        raise Denied('Malformed delegation systemd properties')
    for line in text[:-1].split('\n'):
        key, sep, value = line.partition('=')
        if not sep or key not in names or key in fields or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise Denied('Malformed delegation systemd properties')
        fields[key] = value
    if set(fields) != set(names):
        raise Denied('Incomplete delegation systemd properties')
    return fields


def _jobs(text: str, unit: str) -> tuple[PendingJob, ...]:
    if text == '':
        return ()
    if not text.endswith('\n') or '\r' in text:
        raise Denied('Malformed delegation systemd jobs')
    rows = []
    ids = set()
    for line in text[:-1].split('\n'):
        match = re.fullmatch(r' *([1-9][0-9]*) +([^ ]+) +([^ ]+) +(waiting|running) *', line)
        if match is None:
            raise Denied('Malformed delegation systemd job row')
        number, name, kind, state = match.groups()
        # systemd job IDs are uint32 on the wire.
        if len(number) > 10 or int(number) > 2**32 - 1 or number in ids or name != unit or kind not in _JOB_TYPES:
            raise Denied('Unknown or mismatched delegation systemd job')
        ids.add(number)
        rows.append(PendingJob(int(number), name, kind, state))
    return tuple(rows)


def _command(config: Config, *args, env=None, tool='systemctl') -> str:
    """One bounded command, no retry and no interpretation of partial output."""
    timeout = config.services.systemd_verify_timeout_seconds
    maximum = config.limits.output_bytes
    if type(timeout) is not int or timeout <= 0 or type(maximum) is not int or maximum <= 0:
        raise Denied('Delegation systemd command bounds required')
    env = _environment() if env is None else env
    if tool not in ('systemctl', 'busctl'):
        raise Denied('Unknown delegation systemd command')
    prefix = [tool, '--no-pager']
    if tool == 'systemctl':
        prefix.insert(1, '--user')
    try:
        result = process.run([*prefix, *args], timeout=timeout, maximum=maximum, env=env)
    except OSError as exc:
        raise Denied('Unavailable delegation systemd controller') from exc
    if (result.reason != 'exited' or type(result.exit_code) is not int or result.exit_code != 0
            or result.survivors or result.stderr
            or len(result.stdout.encode('utf-8')) + len(result.stderr.encode('utf-8')) > maximum):
        raise Denied('Unobservable delegation systemd state')
    return result.stdout


def _environment():
    return process.environment(extra={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                                      'SYSTEMD_COLORS': '0', 'SYSTEMD_URLIFY': '0'})


def _socket_path(env):
    # Deliberately support only literal, canonical paths: no percent decoding,
    # guid/options, multiple addresses, abstract sockets or remote transports.
    address = env.get('DBUS_SESSION_BUS_ADDRESS')
    if address is not None:
        match = re.fullmatch(r'unix:path=(/[A-Za-z0-9_./-]+)', address)
        if match is None:
            raise Denied('Ambiguous delegation systemd bus address')
        path = match[1]
    else:
        runtime = env.get('XDG_RUNTIME_DIR', '')
        path = runtime + '/systemd/private' if runtime else ''
    if (not path.startswith('/') or not path.isascii()
            or any(part in ('', '.', '..') for part in path[1:].split('/'))
            or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-' for c in path)):
        raise Denied('Unsafe delegation systemd socket path')
    return path


def _endpoint(env):
    """Local read-only identity, no symlink following and no socket connection.

    The complete directory chain is retained, not only the leaf inode. Matching
    stats are diagnostic, not a guarantee about a later D-Bus connection.
    """
    if sys.platform != 'linux':
        raise Denied('Linux delegation systemd identity required')
    uid = os.getuid()
    path = _socket_path(env)
    try:
        fd = os.open('/proc/sys/kernel/random/boot_id', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            boot = os.read(fd, 38).decode('ascii')
        finally:
            os.close(fd)
        if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n', boot) is None:
            raise Denied('Unknown delegation systemd boot identity')
        fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        chain = []
        try:
            parts = path[1:].split('/')
            for index, part in enumerate(parts):
                child = os.open(part, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                entry = os.fstat(fd)
                leaf = index == len(parts) - 1
                if ((leaf and (not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != uid))
                        or (not leaf and not stat.S_ISDIR(entry.st_mode))):
                    raise Denied('Unsafe delegation systemd socket identity')
                chain.append((entry.st_dev, entry.st_ino, entry.st_uid, entry.st_gid,
                              entry.st_mode, entry.st_ctime_ns if leaf else None))
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise Denied('Unavailable delegation systemd endpoint identity') from exc
    return uid, boot.strip(), path, tuple(chain)


def _name_owner(config, env, socket_path):
    output = _command(config, '--address=unix:path=' + socket_path, 'call',
                      'org.freedesktop.DBus', '/org/freedesktop/DBus',
                      'org.freedesktop.DBus', 'GetNameOwner', 's',
                      'org.freedesktop.systemd1', env=env, tool='busctl')
    match = re.fullmatch(r's "(:[0-9]+\.[0-9]+)"\n', output)
    if match is None or len(match[1]) > 255:
        # If the owner cannot be queried (including a private peer without the
        # method), or exceeds D-Bus's bus-name limit, refuse identity inference.
        raise Denied('Unknown delegation systemd manager owner')
    return match[1]


def _manager(config, env):
    endpoint = _endpoint(env)
    owner_before = _name_owner(config, env, endpoint[2])
    version = _fields(
        _command(config, 'show', '--all', '--property=Version', env=env), ('Version',))['Version']
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9.+~]+)?', version) is None:
        raise Denied('Unknown delegation systemd manager version')
    owner_after = _name_owner(config, env, endpoint[2])
    if owner_before != owner_after or _endpoint(env) != endpoint:
        raise Denied('Changed delegation systemd manager endpoint')
    return ManagerPin(*endpoint, version, owner_after)


def _observe(config: Config, run_id: str, env) -> Observation:
    """Read exact unit state and pending jobs with a diagnostic manager identity.

    Results span separate reads and may already be stale when returned. Unknown
    versions, wire vocabulary, incomplete output or subprocess failures refuse
    the entire observation, including timeout/overflow and surviving I/O threads.
    """
    if not isinstance(run_id, str) or re.fullmatch(r'[0-9a-f]{32}', run_id) is None:
        raise Denied('Invalid delegation systemd run identity')
    unit = f'mizu-delegation-{run_id}.service'
    manager = _manager(config, env)
    fields = _fields(_command(config, 'show', '--all',
                              '--property=' + ','.join(_PROPERTIES), unit, env=env), _PROPERTIES)
    group = fields['ControlGroup']
    invocation = fields['InvocationID']
    if (fields['Id'] != unit or fields['LoadState'] not in _LOAD
            or fields['ActiveState'] not in _ACTIVE
            or fields['SubState'] not in _SUB
            or (invocation != '' and re.fullmatch(r'[0-9a-f]{32}', invocation) is None)
            or (group != '' and group != user_cgroup(unit))
            or (fields['LoadState'] == 'not-found'
                and (fields['ActiveState'] != 'inactive' or fields['SubState'] != 'dead' or group or invocation))):
        raise Denied('Unknown or malformed delegation systemd unit state')
    # --full prevents ellipsizing the exact unit name in list-jobs output.
    jobs = _jobs(_command(config, 'list-jobs', '--no-legend', '--plain', '--full', unit, env=env), unit)
    if _manager(config, env) != manager:
        raise Denied('Changed delegation systemd manager identity')
    return Observation(unit, manager, fields['LoadState'], fields['ActiveState'],
                       fields['SubState'], group, invocation, jobs)


def observe(config: Config, run_id: str) -> Observation:
    """Capture one minimal environment and observe one derived user unit."""
    return _observe(config, run_id, _environment())


@dataclass(frozen=True)
class Cancellation:
    before: Observation
    after: Observation
    release_authority: bool = field(default=False, init=False)


def _cancel_stop(config: Config, run_id: str) -> Cancellation:
    """Request stop for the exact run unit; immutable diagnostics, NEVER settlement.

    Only DelegationJobs.runner_cancel_effects calls this after committing and
    releasing the durable fence. Manager-local job IDs are not cancelled: the
    diagnostic pin cannot bind a new connection, so an ID could be stale/reused
    and mutate an unrelated job. A bounded synchronous stop targets only the
    derived unit and replaces conflicting jobs for that unit. Re-observe once;
    this still does not prove manager identity, creator settlement, job
    settlement or absence.
    """
    env = _environment()
    before = _observe(config, run_id, env)
    if _command(config, '--job-mode=replace', 'stop', before.unit, env=env) != '':
        raise Denied('Unexpected delegation systemd stop output')
    after = _observe(config, run_id, env)
    if before.manager != after.manager:
        raise Denied('Changed delegation systemd manager during stop')
    return Cancellation(before, after)
