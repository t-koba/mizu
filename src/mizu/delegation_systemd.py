"""Read-only, non-releasing observations of one delegation's user unit.

``observe(config, run_id)`` derives the unit; no caller-selected unit or manager
endpoint is accepted. Three sequential systemctl reads are bounded individually
by services.systemd_verify_timeout_seconds and limits.output_bytes. The minimal
process environment selects the current user manager and pins UTF-8/C locale.
No retries, mutations, launch fencing, endpoint identity pinning or cgroup reads
are provided. Failure raises Denied without returning partial evidence.

The decoder records any nonempty syntactically numeric manager version
(numeric major with optional dotted components and a distro suffix). This is
a parsing boundary, NOT a claim that any host or manager version is verified
for lifecycle settlement.
A successful, exactly empty list-jobs stdout is the explicit empty response with
--no-legend; whitespace, diagnostics and incomplete reads are not an empty queue.
Even an empty queue is only a point-in-time observation, never quiescence proof.
"""
from dataclasses import dataclass, field
import re

from . import process
from .config import Config
from .errors import Denied


@dataclass(frozen=True)
class PendingJob:
    id: int
    unit: str
    type: str
    state: str


@dataclass(frozen=True)
class Observation:
    unit: str
    manager_version: str
    load_state: str
    active_state: str
    sub_state: str
    control_group: str
    invocation_id: str
    jobs: tuple[PendingJob, ...]
    point_in_time: bool = field(default=True, init=False)
    release_authority: bool = field(default=False, init=False)


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


def observe(config: Config, run_id: str) -> Observation:
    """Read exact unit state and pending jobs; grant no release authority.

    Results span separate reads and may already be stale when returned. Unknown
    versions, wire vocabulary, incomplete output or subprocess failures refuse
    the entire observation, including timeout/overflow and surviving I/O threads.
    """
    if not isinstance(run_id, str) or re.fullmatch(r'[0-9a-f]{32}', run_id) is None:
        raise Denied('Invalid delegation systemd run identity')
    unit = f'mizu-delegation-{run_id}.service'
    timeout = config.services.systemd_verify_timeout_seconds
    maximum = config.limits.output_bytes
    if type(timeout) is not int or timeout <= 0 or type(maximum) is not int or maximum <= 0:
        raise Denied('Delegation systemd observation bounds required')
    env = process.environment(extra={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                                     'SYSTEMD_COLORS': '0', 'SYSTEMD_URLIFY': '0'})

    def query(*args):
        try:
            result = process.run(['systemctl', '--user', '--no-pager', *args],
                                 timeout=timeout, maximum=maximum, env=env)
        except OSError as exc:
            raise Denied('Unavailable delegation systemd observer') from exc
        if (result.reason != 'exited' or type(result.exit_code) is not int or result.exit_code != 0
                or result.survivors or result.stderr
                or len(result.stdout.encode('utf-8')) + len(result.stderr.encode('utf-8')) > maximum):
            raise Denied('Unobservable delegation systemd state')
        return result.stdout

    version = _fields(query('show', '--all', '--property=Version'), ('Version',))['Version']
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9.+~]+)?', version) is None:
        raise Denied('Unknown delegation systemd manager version')
    fields = _fields(query('show', '--all', '--property=' + ','.join(_PROPERTIES), unit), _PROPERTIES)
    group = fields['ControlGroup']
    invocation = fields['InvocationID']
    if (fields['Id'] != unit or fields['LoadState'] not in _LOAD
            or fields['ActiveState'] not in _ACTIVE
            or fields['SubState'] not in _SUB
            or (invocation != '' and re.fullmatch(r'[0-9a-f]{32}', invocation) is None)
            or (group != '' and (not group.startswith('/') or group == '/'
                                 or any(p in ('', '.', '..') for p in group[1:].split('/'))
                                 or any(c.isspace() for c in group)))
            or (fields['LoadState'] == 'not-found'
                and (fields['ActiveState'] != 'inactive' or fields['SubState'] != 'dead' or group or invocation))):
        raise Denied('Unknown or malformed delegation systemd unit state')
    # --full prevents ellipsizing the exact unit name in list-jobs output.
    jobs = _jobs(query('list-jobs', '--no-legend', '--plain', '--full', unit), unit)
    return Observation(unit, version, fields['LoadState'], fields['ActiveState'],
                       fields['SubState'], group, invocation, jobs)
