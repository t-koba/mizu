"""Synthetic trusted-CI recovery probe; no delegation/runtime integration.

Ordinary discovery runs only the offline tests. Live commands require explicit
CLI opt-in and Ubuntu 24.04; no unavailable-evidence path is a successful skip.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
CGROOT = Path('/sys/fs/cgroup')
IMAGE = 'docker.io/library/busybox:1.37.0'
COMMAND_DIAGNOSTIC_BYTES_MAX = 1024


def command(*argv, seconds=10, codes=(0,), maximum=8192, diagnostic_bytes=0):
    # Reuse the bounded pipe/group teardown primitive, not shell capture or an
    # unbounded log file. A timeout/output overflow is unknown, never absence.
    if str(ROOT / 'src') not in sys.path:
        sys.path.insert(0, str(ROOT / 'src'))
    from mizu.process import run
    result = run(list(argv), timeout=seconds, maximum=maximum)
    if result.reason != 'exited' or result.exit_code not in codes or result.survivors:
        detail = ''
        limit = (min(diagnostic_bytes, COMMAND_DIAGNOSTIC_BYTES_MAX)
                 if type(diagnostic_bytes) is int and diagnostic_bytes > 0 else 0)
        if result.reason == 'exited' and result.exit_code != 0 and limit > 0:
            raw = result.stderr.encode('utf-8')[-limit:]
            excerpt = raw.decode('utf-8', 'replace')
            for path in (str(Path.home()), os.environ.get('RUNNER_TEMP')):
                if path:
                    excerpt = excerpt.replace(path, '<path>')
            if excerpt.strip():
                detail = f'; stderr_tail={excerpt!r}'
        raise RuntimeError(f'{argv[0]} failed: {result.reason}/{result.exit_code}{detail}')
    return result


def atomic_json(path, value):
    """Same-directory replace, private file, fsync file AND parent directory."""
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_record():
    name = 'mizu-recovery-' + uuid.uuid4().hex
    return dict(version=1, name=name, unit=name + '.service',
                cgroup='/system.slice/' + name + '.service', container_id=None,
                launches_settled=False, reservation='held',
                cgroup_clean=False, container_absent=False)


def settle(record, cgroup_clean, container_absent):
    """Only affirmative independent evidence for BOTH scopes releases a hold."""
    record.update(cgroup_clean=cgroup_clean is True, container_absent=container_absent is True)
    record['reservation'] = ('released' if record['launches_settled'] is True
                             and cgroup_clean is True and container_absent is True else 'held')


def read_record(path):
    for target, mode in ((path.parent, 0o700), (path, 0o600)):
        info = target.lstat()
        if (stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid()
                or stat.S_ISLNK(info.st_mode)):
            raise RuntimeError('record is not private and owned')
    if path.stat().st_size > 2048:
        raise RuntimeError('oversized record')
    record = json.loads(path.read_text())
    if (set(record) != set(new_record()) or record['version'] != 1
            or not re.fullmatch(r'mizu-recovery-[0-9a-f]{32}', record['name'])
            or record['unit'] != record['name'] + '.service'
            or record['cgroup'] != '/system.slice/' + record['unit']
            or (record['container_id'] is not None
                and not re.fullmatch(r'[0-9a-f]{64}', record['container_id']))
            or type(record['launches_settled']) is not bool
            or record['reservation'] not in ('held', 'released')):
        raise RuntimeError('invalid fixture identity')
    return record


def require_host():
    import platform
    release = platform.freedesktop_os_release()
    if (release.get('ID'), release.get('VERSION_ID')) != ('ubuntu', '24.04') or os.geteuid() == 0:
        raise RuntimeError('requires non-root Ubuntu 24.04 trusted CI user')
    if not (CGROOT / 'cgroup.controllers').read_text().strip():
        raise RuntimeError('requires cgroup v2 controllers')
    # cgroup v2 is checked from the kernel's controllers file above. Keep the
    # Podman query to HostInfo fields present in supported versions; some
    # Podman releases do not expose Host.CgroupVersion in their template.
    info = command('podman', 'info', '--format',
                   '{{.Host.Security.Rootless}} {{.Host.CgroupManager}}',
                   diagnostic_bytes=COMMAND_DIAGNOSTIC_BYTES_MAX)
    if info.stdout.strip() != 'true systemd':
        raise RuntimeError('requires rootless Podman with systemd/cgroup v2')


def unit_state(record):
    result = command('sudo', '-n', 'systemctl', 'show', record['unit'],
                     '--property=LoadState,ActiveState,ControlGroup', codes=(0, 1))
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
    if (set(fields) != {'LoadState', 'ActiveState', 'ControlGroup'}
            or fields['LoadState'] not in ('loaded', 'not-found')
            or fields['ControlGroup'] not in ('', record['cgroup'])):
        raise RuntimeError('unknown or mismatched systemd identity')
    return fields


def cgroup_empty(record):
    path = CGROOT / record['cgroup'].lstrip('/')
    try:
        path.lstat()
    except FileNotFoundError:
        return True  # cgroupfs exact-path ENOENT, not a failed CLI/read
    # populated covers descendants, unlike cgroup.procs alone.
    events = dict(line.split() for line in (path / 'cgroup.events').read_text().splitlines())
    return events.get('populated') == '0' and not (path / 'cgroup.procs').read_text().strip()


def container_exists(identity):
    result = command('podman', 'container', 'exists', identity, codes=(0, 1))
    return result.exit_code == 0  # Podman: 1 absent; 125 runtime error (refused above)


def clean_container(record):
    name, saved = record['name'], record['container_id']
    if container_exists(name):
        found = command('podman', 'inspect', '--format', '{{.Id}} {{.Name}}', name).stdout.strip()
        identity, actual_name = found.split()
        if (not re.fullmatch(r'[0-9a-f]{64}', identity) or actual_name.lstrip('/') != name
                or (saved is not None and identity != saved)):
            raise RuntimeError('container identity mismatch')
        command('podman', 'rm', '--force', '--time=2', identity)
    # A saved ID may still exist even if the name disappeared/was renamed.
    # Do not remove a renamed/mismatched resource; retain the reservation.
    return not container_exists(name) and (saved is None or not container_exists(saved))


def recover(path):
    record = read_record(path)  # no inherited in-memory launch state
    settle(record, None, None)
    atomic_json(path, record)
    cgroup_clean = container_absent = None
    try:
        require_host()
        try:
            fields = unit_state(record)
            if fields['LoadState'] == 'loaded':
                command('sudo', '-n', 'systemctl', 'kill', '--kill-whom=all',
                        '--signal=SIGKILL', record['unit'], codes=(0, 1))
                command('sudo', '-n', 'systemctl', 'stop', record['unit'])
                # A killed main process can leave a failed transient unit even
                # after its cgroup drains. Reset only this recorded unit.
                command('sudo', '-n', 'systemctl', 'reset-failed', record['unit'], codes=(0, 1))
            fields = unit_state(record)
            cgroup_clean = fields['ActiveState'] == 'inactive' and cgroup_empty(record)
        finally:
            # One scope's failure must not prevent best-effort cleanup of the other.
            container_absent = clean_container(record)
    finally:
        settle(record, cgroup_clean, container_absent)
        atomic_json(path, record)
    if record['reservation'] != 'released':
        raise RuntimeError('cleanup incomplete or launch uncertain; reservation held')


def process_in_scope(pid, record):
    return Path(f'/proc/{pid}/cgroup').read_text().strip() == '0::' + record['cgroup']


def supervise(path):
    # No runtime, socket, credentials, or production IDs in the synthetic unit.
    child = subprocess.Popen(['/usr/bin/setsid', '/usr/bin/sleep', '90'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    atomic_json(path.parent / 'ready.json', {'supervisor': os.getpid(), 'descendant': child.pid})
    time.sleep(90)


def live(path):
    path.parent.mkdir(mode=0o700)  # refuse reuse; CI owns this disposable directory
    record = new_record()
    atomic_json(path, record)  # identities and held reservation precede preflight/launch
    try:
        require_host()
        command('podman', 'pull', '--quiet', IMAGE, seconds=60)
        command('sudo', '-n', 'systemd-run', '--unit=' + record['unit'],
                '--slice=system.slice', '--service-type=exec',
                '--property=User=' + str(os.getuid()), '--property=WorkingDirectory=/',
                '--property=ExitType=cgroup', '--property=KillMode=control-group',
                '--property=TasksMax=8', '--property=CPUQuota=10%',
                '--property=MemoryMax=64M', '--property=RuntimeMaxSec=120',
                '--property=TimeoutStopSec=5', '--property=StandardOutput=null',
                '--property=StandardError=null', '--property=NoNewPrivileges=yes',
                '--property=PrivateNetwork=yes', sys.executable, str(Path(__file__).resolve()), '--supervise', str(path))
        ready_path = path.parent / 'ready.json'
        deadline = time.monotonic() + 5
        while not ready_path.is_file() and time.monotonic() < deadline:
            time.sleep(.05)
        ready = json.loads(ready_path.read_text())
        # setsid may not have exec'd yet; wait boundedly for its new session.
        while os.getsid(ready['descendant']) != ready['descendant'] and time.monotonic() < deadline:
            time.sleep(.05)
        if (os.getsid(ready['descendant']) != ready['descendant']
                or not all(process_in_scope(pid, record) for pid in ready.values())
                or unit_state(record)['ControlGroup'] != record['cgroup']):
            raise RuntimeError('supervisor/setsid cgroup evidence missing')
        cid = command('podman', 'create', '--pull=never', '--name', record['name'], '--network=none',
                      '--read-only', '--read-only-tmpfs=false', '--cap-drop=ALL',
                      '--security-opt=no-new-privileges', '--user=65534:65534',
                      '--pids-limit=16', '--cpus=0.1', '--memory=32m', '--timeout=90',
                      '--log-driver=none', IMAGE, '/bin/sleep', '90').stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            raise RuntimeError('missing container ID')
        record['container_id'] = cid
        atomic_json(path, record)  # container ID is durable BEFORE start
        command('podman', 'start', cid)
        if command('podman', 'inspect', '--format', '{{.State.Running}}', cid).stdout.strip() != 'true':
            raise RuntimeError('container not running')
        record['launches_settled'] = True
        atomic_json(path, record)
        command('sudo', '-n', 'systemctl', 'kill', '--kill-whom=main',
                '--signal=SIGKILL', record['unit'])
        deadline = time.monotonic() + 5
        while Path(f"/proc/{ready['supervisor']}").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if (Path(f"/proc/{ready['supervisor']}").exists()
                or not process_in_scope(ready['descendant'], record)
                or cgroup_empty(record) or not container_exists(cid)
                or read_record(path)['reservation'] != 'held'):
            raise RuntimeError('supervisor-death/held-reservation evidence missing')
    finally:
        # Separate process reads only the durable record. CI also invokes this
        # entry point in an always() step if this entire controller is killed.
        command(sys.executable, str(Path(__file__).resolve()), '--recover', str(path), seconds=75)
    if read_record(path)['reservation'] != 'released':
        raise RuntimeError('recovery did not release the synthetic reservation')
    print('Synthetic supervisor death and scoped recovery proven; reservation released.')


class RecoveryReservationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'host preflight is POSIX-only')
    def test_host_preflight_uses_supported_podman_template_fields(self):
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as temporary:
            cgroup_root = Path(temporary)
            (cgroup_root / 'cgroup.controllers').write_text('cpu memory\n')
            with (mock.patch('platform.freedesktop_os_release', return_value={
                      'ID': 'ubuntu', 'VERSION_ID': '24.04'}),
                  mock.patch.object(os, 'geteuid', return_value=1000),
                  mock.patch.object(module, 'CGROOT', cgroup_root),
                  mock.patch.object(module, 'command', return_value=mock.Mock(
                      stdout='true systemd\n')) as invoke):
                require_host()
        invoke.assert_called_once_with(
            'podman', 'info', '--format',
            '{{.Host.Security.Rootless}} {{.Host.CgroupManager}}',
            diagnostic_bytes=COMMAND_DIAGNOSTIC_BYTES_MAX)

    def test_host_preflight_diagnostic_is_bounded_and_redacts_user_path(self):
        if str(ROOT / 'src') not in sys.path:
            sys.path.insert(0, str(ROOT / 'src'))
        from mizu.process import Result
        home = str(Path.home())
        result = Result(exit_code=125, stdout='', stderr='x' * 4096 + home,
                        reason='exited', seconds=0)
        with mock.patch('mizu.process.run', return_value=result):
            with self.assertRaises(RuntimeError) as raised:
                command('podman', 'info', diagnostic_bytes=128)
        message = str(raised.exception)
        self.assertIn('stderr_tail=', message)
        self.assertNotIn(home, message)
        self.assertLess(len(message), 512)

    def test_only_both_affirmative_proofs_release(self):
        for group in (True, False, None, 'unknown', 1):
            for container in (True, False, None, 'unknown', 1):
                with self.subTest(group=group, container=container):
                    record = new_record()
                    record['launches_settled'] = True
                    settle(record, group, container)
                    self.assertEqual(record['reservation'] == 'released',
                                     group is True and container is True)

    def test_partial_launch_and_later_unknown_retain_hold(self):
        record = new_record()
        settle(record, True, True)
        self.assertEqual(record['reservation'], 'held')
        record['launches_settled'] = True
        settle(record, True, True)
        self.assertEqual(record['reservation'], 'released')
        settle(record, None, True)
        self.assertEqual(record['reservation'], 'held')

    @unittest.skipUnless(os.name == 'posix', 'private POSIX recovery record')
    def test_recovery_persists_hold_on_either_scope_failure(self):
        # These are injected evidence results, NOT systemd/OCI validation.
        module = sys.modules[__name__]
        for group, container in ((True, True), (False, True), (True, False),
                                 (OSError('unknown cgroup'), True),
                                 (True, OSError('unknown runtime'))):
            with self.subTest(group=group, container=container), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'state.json'
                record = new_record()
                record['launches_settled'] = True
                atomic_json(path, record)
                def group_probe(_):
                    self.assertEqual(read_record(path)['reservation'], 'held')
                    if isinstance(group, Exception):
                        raise group
                    return group
                def container_probe(_):
                    self.assertEqual(read_record(path)['reservation'], 'held')
                    if isinstance(container, Exception):
                        raise container
                    return container
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(module, 'cgroup_empty', side_effect=group_probe),
                      mock.patch.object(module, 'clean_container', side_effect=container_probe) as cleanup):
                    if group is True and container is True:
                        recover(path)
                    else:
                        with self.assertRaises((RuntimeError, OSError)):
                            recover(path)
                    cleanup.assert_called_once()
                self.assertEqual(read_record(path)['reservation'],
                                 'released' if group is True and container is True else 'held')

    @unittest.skipUnless(os.name == 'posix', 'private POSIX recovery record')
    def test_unavailable_host_keeps_the_persisted_reservation_held(self):
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            record = new_record()
            record['launches_settled'] = True
            atomic_json(path, record)
            with mock.patch.object(module, 'require_host',
                                   side_effect=RuntimeError('unsupported host')):
                with self.assertRaisesRegex(RuntimeError, 'unsupported host'):
                    recover(path)
            self.assertEqual(read_record(path)['reservation'], 'held')

    def test_atomic_record_is_private_and_exactly_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            record = new_record()
            atomic_json(path, record)
            self.assertEqual(json.loads(path.read_text()), record)
            if os.name == 'posix':
                self.assertEqual(read_record(path), record)
                record['cgroup'] = '/system.slice/unrelated.service'
                atomic_json(path, record)
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    read_record(path)
                path.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, 'private'):
                    read_record(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--prepare', action='store_true')
    actions.add_argument('--live', type=Path)
    actions.add_argument('--recover', type=Path)
    actions.add_argument('--supervise', type=Path)
    args = parser.parse_args()
    if args.prepare:
        command('sudo', '-n', 'apt-get', '-qq', 'update', seconds=60, maximum=32768)
        command('sudo', '-n', 'apt-get', '-y', '-qq', 'install', 'podman', 'uidmap',
                'slirp4netns', 'fuse-overlayfs', seconds=90, maximum=32768)
    elif args.live:
        live(args.live)
    elif args.recover:
        recover(args.recover)
    else:
        supervise(args.supervise)
