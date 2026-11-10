"""Bounded local Podman identity observations, never handback authority.

capture(Config) returns a JSON-serializable pin; revalidate(Config, pin) requires
an exact canonical match. Only selected info fields survive. The pin binds the
host uid, runtime/helper argv and file identities, a digest of the entire
sanitized runtime_env, Version.Version, uid/gid maps, store paths/driver/options,
and the selected OCI runtime/conmon paths and versions. No environment values or
unselected info fields are retained. The schema is private, not a ledger format.

All paths must be absolute, lexically canonical and present, with no symlink in
any component. Bare runtime names are looked up only through unambiguous absolute
PATH entries. Each path walk is linear in its spelling, uses no-follow dirfds and
stats only (no file reads or directory enumeration). File identities include
size/mtime/ctime; directory identities deliberately omit those mutable fields so
ordinary store activity does not invalidate a pin. Graph options support the
Podman string/string-map wire forms; unknown forms fail closed.

One info read uses sandbox.cleanup_timeout_seconds and limits.output_bytes;
there are no retries, cancellation, cleanup, launch, grants or release operations.
Filesystem observations are not atomic with execution, and neither a matching
pin nor a successful local info command proves fencing, namespace absence or
settlement. This adapter is NOT supported-host lifecycle proof.
"""
import hashlib
import json
import os
import re
import stat
import sys

from . import process
from .config import Config
from .errors import Denied
from .fs import canonical
from .sandbox import runtime_base, runtime_env


# Version.Version is the Podman semver string, not its build timestamp or the
# remote API version. This is syntax validation, not an approved-version list.
_NUMBER = r'(?:0|[1-9][0-9]*)'
_PRERELEASE = r'(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
_VERSION = re.compile(rf'{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-{_PRERELEASE}(?:\.{_PRERELEASE})*)?'
                      r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')


def _text(value, *, multiline=False):
    if (not isinstance(value, str) or not value.strip()
            or any((ord(c) < 32 and not (multiline and c in '\n\t'))
                   or ord(c) == 127 or c == '\ufffd' or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        raise Denied('Malformed delegation Podman identity text')
    return value


def _path(value):
    value = _text(value)
    if not value.startswith('/') or any(p in ('', '.', '..') for p in value[1:].split('/')):
        raise Denied('Unsafe delegation Podman identity path')
    return value


def _identity(path, *, directory=False, executable=False):
    """Walk from / with no-follow opens, retaining stable ancestor identities."""
    path = _path(path)
    fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    chain = []
    try:
        parts = path[1:].split('/')
        for index, part in enumerate(parts):
            child = os.open(part, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            entry = os.fstat(fd)
            is_dir = index < len(parts) - 1 or directory
            if ((is_dir and not stat.S_ISDIR(entry.st_mode))
                    or (not is_dir and not stat.S_ISREG(entry.st_mode))
                    or (not is_dir and executable and not entry.st_mode & 0o111)):
                raise Denied('Unsafe delegation Podman identity file type')
            item = {'dev': entry.st_dev, 'ino': entry.st_ino, 'mode': entry.st_mode,
                    'uid': entry.st_uid, 'gid': entry.st_gid}
            if not is_dir:
                item.update(size=entry.st_size, mtime_ns=entry.st_mtime_ns, ctime_ns=entry.st_ctime_ns)
            chain.append(item)
    finally:
        os.close(fd)
    return {'path': path, 'chain': chain}


def _runtime_identity(executable, env):
    _text(executable)
    if '/' in executable:
        return _identity(executable, executable=True)
    # Do not silently resolve a relative/empty PATH element against the host cwd.
    entries = env.get('PATH', '').split(os.pathsep)
    for entry in entries:
        _path(entry)
    for entry in entries:
        candidate = entry + '/' + executable
        try:
            return _identity(candidate, executable=True)
        except FileNotFoundError:
            continue
    raise Denied('Missing delegation Podman executable')


def _nonfinite(_):
    raise ValueError('Nonfinite JSON')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Denied('Duplicate delegation Podman info field')
        result[key] = value
    return result


def _mappings(value):
    if not isinstance(value, dict) or set(value) != {'uidmap', 'gidmap'}:
        raise Denied('Missing delegation Podman ID mappings')
    result = {}
    for kind in ('uidmap', 'gidmap'):
        rows = value[kind]
        if not isinstance(rows, list) or not rows:
            raise Denied('Malformed delegation Podman ID mappings')
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'container_id', 'host_id', 'size'}
                    or any(type(v) is not int or v < 0 for v in row.values())
                    or row['size'] == 0
                    # Linux uid/gid map entries use uint32 IDs, not booleans.
                    or max(row['container_id'], row['host_id']) + row['size'] > 2**32):
                raise Denied('Malformed delegation Podman ID mapping')
        for key in ('container_id', 'host_id'):
            ordered = sorted(rows, key=lambda row: row[key])
            if any(a[key] + a['size'] > b[key] for a, b in zip(ordered, ordered[1:])):
                raise Denied('Overlapping delegation Podman ID mappings')
        result[kind] = sorted(rows, key=lambda row: row['container_id'])
    return result


def _options(value):
    if not isinstance(value, dict):
        raise Denied('Malformed delegation Podman graph options')
    for key, option in value.items():
        _text(key)
        if isinstance(option, dict):
            if not option:
                raise Denied('Malformed delegation Podman graph option')
            for name, text in option.items():
                _text(name)
                _text(text)
        else:
            _text(option)
    return value


def _program(value, *, named=False):
    if not isinstance(value, dict):
        raise Denied('Missing delegation Podman runtime identity')
    result = {'file': _identity(value['path'], executable=True), 'version': _text(value['version'], multiline=True)}
    if named:
        result['name'] = _text(value['name'])
    return result


def _capture(config):
    if sys.platform != 'linux' or config.sandbox.mode != 'rootless':
        raise Denied('Delegation Podman identity requires Linux rootless mode')
    executable = _text(config.sandbox.executable)
    if executable.rsplit('/', 1)[-1] != 'podman':
        raise Denied('Delegation Podman identity requires configured Podman')
    timeout, maximum = config.sandbox.cleanup_timeout_seconds, config.limits.output_bytes
    if type(timeout) is not int or timeout <= 0 or type(maximum) is not int or maximum <= 0:
        raise Denied('Delegation Podman identity bounds required')
    env = runtime_env(config)
    # Capture identities before runtime_base (which itself checks helper access).
    # Reject unsafe configuration without making any Podman call.
    runtime = _runtime_identity(executable, env)
    helpers = [_identity(path, executable=True) for path in config.sandbox.namespace_helper]
    argv = [*runtime_base(config), '--remote=false', 'info', '--format=json']
    result = process.run(argv, timeout=timeout, maximum=maximum, env=env)
    if (result.reason != 'exited' or type(result.exit_code) is not int or result.exit_code != 0
            or result.survivors or result.stderr
            or len(result.stdout.encode('utf-8')) > maximum):
        raise Denied('Incomplete delegation Podman info')
    info = json.loads(result.stdout, object_pairs_hook=_pairs,
                      parse_constant=_nonfinite)
    host, store = info['host'], info['store']
    if (host['serviceIsRemote'] is not False or host['security']['rootless'] is not True
            or host['cgroupManager'] != 'systemd' or host['cgroupVersion'] != 'v2'):
        raise Denied('Unsupported delegation Podman local/rootless/cgroup facts')
    version = _text(info['version']['Version'])
    if _VERSION.fullmatch(version) is None:
        raise Denied('Malformed delegation Podman version')
    pin = {'schema': 1, 'release_authority': False, 'uid': os.getuid(),
           'argv': argv, 'runtime': runtime, 'namespace_helpers': helpers,
           'environment_sha256': hashlib.sha256(canonical(env)).hexdigest(),
           'version': version, 'local': True, 'rootless': True,
           'cgroup_manager': 'systemd', 'cgroup_version': 'v2',
           'id_mappings': _mappings(host['idMappings']),
           'store': {'config_file': _identity(store['configFile']),
                     'graph_root': _identity(store['graphRoot'], directory=True),
                     'run_root': _identity(store['runRoot'], directory=True),
                     'driver': _text(store['graphDriverName']), 'options': _options(store['graphOptions'])},
           'oci_runtime': _program(host['ociRuntime'], named=True), 'conmon': _program(host['conmon'])}
    if (runtime != _runtime_identity(executable, env)
            or helpers != [_identity(path, executable=True) for path in config.sandbox.namespace_helper]
            or env != runtime_env(config)):
        raise Denied('Delegation Podman runtime changed during observation')
    return pin


def capture(config: Config) -> dict:
    """Return selected, serializable identity facts or Denied; never partial data."""
    try:
        return _capture(config)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        # Do not echo info output, environment contents or host paths on failure.
        raise Denied('Unavailable or malformed delegation Podman identity') from exc


def revalidate(config: Config, pin: dict) -> dict:
    """Require exact JSON identity equality; return a fresh non-releasing pin.

    The pin is caller-held observation data, not an attestation or capability.
    Missing/extra fields and changed JSON types refuse; no legacy inference.
    """
    try:
        expected = canonical(pin)
        if (not isinstance(pin, dict) or type(pin.get('schema')) is not int or pin.get('schema') != 1
                or pin.get('release_authority') is not False):
            raise Denied('Invalid delegation Podman identity pin')
        current = capture(config)
        if canonical(current) != expected:
            raise Denied('Delegation Podman identity changed')
        return current
    except (ValueError, TypeError, RecursionError) as exc:
        raise Denied('Malformed delegation Podman identity pin') from exc
