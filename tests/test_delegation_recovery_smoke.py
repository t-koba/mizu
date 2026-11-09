"""Synthetic trusted-CI job-ledger recovery probe; no runtime/adapter integration.

Ordinary discovery runs only the offline tests. Live commands require explicit
CLI opt-in and Ubuntu 24.04; no unavailable-evidence path is a successful skip.
"""
import argparse
import copy
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))
from mizu.delegation_jobs import DelegationJobs
from mizu.errors import Denied
from mizu.fs import canonical, loads_finite
CGROOT = Path('/sys/fs/cgroup')
IMAGE = 'docker.io/library/busybox:1.37.0'
MARKER = 'io.mizu.recovery.run'
COMMAND_DIAGNOSTIC_BYTES_MAX = 1024


def command(*argv, seconds=10, codes=(0,), maximum=8192, diagnostic_bytes=0):
    # Reuse the bounded pipe/group teardown primitive, not shell capture or an
    # unbounded log file. A timeout/output overflow is unknown, never absence.
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


def ledger(path):
    # Recovery never initializes a missing store or reconstructs identities from
    # fixture files, process memory, or the current branch. Prepared recovery
    # discovers only through the exact name/marker intent already in this ledger.
    root = path.parent / 'delegation-jobs'
    for target, mode in ((path.parent, 0o700), (root, 0o700),
                         (root / 'state.json', 0o600), (root / 'store.lock', 0o600)):
        info = target.lstat()  # missing lock/state is never initialized/repaired
        if (stat.S_IMODE(info.st_mode) != mode or info.st_uid != os.getuid()
                or stat.S_ISLNK(info.st_mode)):
            raise RuntimeError('ledger is not private and owned')
    return DelegationJobs(path.parent)


def create_job(path, key='smoke'):
    store = DelegationJobs(path.parent)
    store.start('worker', key, {'role': 'helper', 'task': 'Synthetic recovery fixture',
                                  'profile': 'fixture', 'source_snapshot': 'a' * 64, 'authority_digest': 'b' * 64})
    reservation = store.reservation()
    directory = path.parent / reservation['run_id']
    directory.mkdir(mode=0o700)
    return store, reservation, directory


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


def clean_container(container):
    name, saved = container['name'], container['id']
    if container['uid'] != os.getuid():
        raise RuntimeError('OCI runtime namespace mismatch')
    by_name, by_id = container_exists(name), container_exists(saved)
    if by_name != by_id:
        raise RuntimeError('renamed or mismatched container identity')
    if by_name:
        for selector in (name, saved):
            found = command('podman', 'inspect', '--format', '{{.Id}} {{.Name}}', selector).stdout.strip()
            if found.split() != [saved, name] and found.split() != [saved, '/' + name]:
                raise RuntimeError('container identity mismatch')
        command('podman', 'rm', '--force', '--time=2', saved)
    # Check both exact selectors independently; CLI errors are unknown, not absence.
    absent_name = not container_exists(name)
    absent_id = not container_exists(saved)
    return absent_name and absent_id


def fixture_container(reservation, image):
    """Expected inspection projection, persisted before create, not learned after."""
    name, marker = 'mizu-recovery-' + reservation['run_id'], reservation['run_id']
    argv = ['podman', 'create', '--pull=never', '--name', name, '--label', MARKER + '=' + marker,
            '--network=none', '--read-only', '--read-only-tmpfs=false', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user=65534:65534', '--pids-limit=16',
            '--cpus=0.1', '--memory=32m', '--timeout=90', '--log-driver=none',
            image, '/bin/sleep', '90']
    return {'name': name, 'uid': os.getuid(), 'marker': marker, 'image': image,
            'config': {'CreateCommand': argv, 'Cmd': ['/bin/sleep', '90'], 'Entrypoint': [],
                       'EffectiveCaps': [], 'BoundingCaps': [],
                       'User': '65534:65534', 'Labels': {MARKER: marker},
                       'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                       'Binds': [], 'Mounts': [], 'CapAdd': [],
                       'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 16,
                       'Memory': 33554432, 'CpuPeriod': 100000, 'CpuQuota': 10000,
                       'LogDriver': 'none'}}


def intent_candidates(container):
    """Two bounded exact selectors; unknown or truncated inventories raise."""
    if container['uid'] != os.getuid():
        raise RuntimeError('OCI runtime namespace mismatch')
    found = []
    for selector in ('name=^/?' + container['name'] + '$',
                     'label=' + MARKER + '=' + container['marker']):
        ids = command('podman', 'ps', '--all', '--no-trunc', '--filter', selector,
                      '--format', '{{.ID}}').stdout.splitlines()
        if (len(ids) > 1 or any(re.fullmatch(r'[0-9a-f]{64}', cid) is None for cid in ids)):
            raise RuntimeError('ambiguous or malformed provisioning inventory')
        found.append(ids)
    if found[0] != found[1]:
        raise RuntimeError('missing, renamed or substituted provisioning container')
    return found[0]


def inspection_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise RuntimeError('ambiguous duplicate inspection field')
    return value


def inspect_prepared(container, cid):
    """Validate both exact selectors before removal; never remove a substitute."""
    for selector in (container['name'], cid):
        observed = loads_finite(command('podman', 'inspect', '--format', '{{json .}}', selector,
                                        maximum=32768).stdout, object_pairs_hook=inspection_object)
        config, host, state = observed['Config'], observed['HostConfig'], observed['State']
        projection = {k: config[k] for k in ('CreateCommand', 'Cmd', 'User', 'Labels')}
        projection.update({k: host[k] for k in ('NetworkMode', 'ReadonlyRootfs', 'Privileged',
                          'SecurityOpt', 'PidsLimit', 'Memory', 'CpuPeriod', 'CpuQuota')})
        # Podman versions encode empty optional lists as either null or [].
        projection.update({k: [] if host[k] is None else host[k] for k in ('Binds', 'CapAdd')})
        entrypoint = config['Entrypoint']
        projection.update(Entrypoint=[] if entrypoint in (None, '', []) else entrypoint,
                          LogDriver=host['LogConfig']['Type'])
        for field in ('EffectiveCaps', 'BoundingCaps', 'Mounts'):
            projection[field] = [] if observed[field] is None else observed[field]
        if (observed['Id'] != cid or observed['Name'] not in (container['name'], '/' + container['name'])
                or observed['Image'].removeprefix('sha256:') != container['image']
                or canonical(projection) != canonical(container['config'])
                or state['Status'] not in {'configured', 'created'}
                or state['Running'] is not False or type(state['Pid']) is not int or state['Pid'] != 0
                or state['StartedAt'] != '0001-01-01T00:00:00Z'
                or state['FinishedAt'] != '0001-01-01T00:00:00Z'
                or type(observed['RestartCount']) is not int or observed['RestartCount'] != 0):
            raise RuntimeError('provisioning identity, configuration or prelaunch state mismatch')


def recover_prepared(store, credentials):
    # The trusted fixture controller/provisioner must have stopped and all create
    # commands settled. The durable fence prevents a later bind/runner claim; it
    # is not a distributed fence against an unrelated external Podman client.
    intent = store.runner_recover_prepared(*credentials)
    require_host()
    scope, container = intent['scope'], intent['container']
    fields = unit_state(scope)
    try:
        (CGROOT / scope['cgroup'].lstrip('/')).lstat()
    except FileNotFoundError:
        scope_absent = True
    else:
        scope_absent = False
    if (fields != {'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}
            or not scope_absent):
        raise RuntimeError('prepared child scope may have started')
    candidates = intent_candidates(container)
    cid = intent['id']
    if candidates:
        candidate = candidates[0]
        if cid is not None and candidate != cid:
            raise RuntimeError('provisioning container ID mismatch')
        inspect_prepared(container, candidate)
        # Write uncertainty MUST abort before rm; retry inspects the persisted ID.
        intent = store.runner_recover_prepared(*credentials, container_id=candidate)
        cid = intent['id']
        command('podman', 'rm', cid)  # never force a running or raced candidate
    elif cid is None:
        raise RuntimeError('initial provisioning container evidence missing')
    absent_name = not container_exists(container['name'])
    absent_id = not container_exists(cid)
    if not absent_name or not absent_id or intent_candidates(container):
        raise RuntimeError('provisioning container absence unproven')
    store.runner_prepared_quiescent(*credentials, 'Synthetic prelaunch fixture cleanup verified',
                                   cleanup={'intent': intent, 'scope_unstarted': True,
                                            'container_absent': cid})


def recover(path):
    store = ledger(path)
    reservation = store.reservation()
    if reservation is None:
        return  # Already sealed by an earlier, acknowledged or uncertain commit.
    credentials = reservation['id'], reservation['token']
    if (reservation['binding'] is None or (reservation['intent'] is not None
            and not reservation['intent']['claimed'] and reservation['execution'] is None)):
        recover_prepared(store, credentials)
        return
    binding = store.runner_binding(*credentials)
    scope = binding['scope']
    cgroup_clean, absent = None, []
    require_host()
    try:
        fields = unit_state(scope)
        if fields['LoadState'] == 'loaded':
            command('sudo', '-n', 'systemctl', 'kill', '--kill-whom=all',
                    '--signal=SIGKILL', scope['unit'], codes=(0, 1))
            command('sudo', '-n', 'systemctl', 'stop', scope['unit'])
            command('sudo', '-n', 'systemctl', 'reset-failed', scope['unit'], codes=(0, 1))
        fields = unit_state(scope)
        cgroup_clean = fields['ActiveState'] == 'inactive' and cgroup_empty(scope)
    finally:
        # Failure in one scope must not prevent cleanup attempts for every
        # other bound resource. Never seal after an exception/unknown result.
        errors = []
        for container in binding['containers']:
            try:
                if clean_container(container) is True:
                    absent.append(container['id'])
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]
    store.runner_quiescent(*credentials, 'Synthetic descendant and OCI cleanup verified',
                           cleanup={'binding': binding, 'scope_clean': cgroup_clean,
                                    'containers_absent': absent})


def process_in_scope(pid, record):
    return Path(f'/proc/{pid}/cgroup').read_text().strip() == '0::' + record['cgroup']


def supervise(path):
    # Actual child directory comes only from the durable ledger. No runtime,
    # socket, credentials, or production IDs enter this synthetic unit.
    store = ledger(path)
    reservation = store.reservation()
    if (reservation is None or reservation['status'] != 'running'
            or reservation['execution'] is not None or reservation['intent'] is None
            or not reservation['intent']['claimed']):
        raise RuntimeError('synthetic supervisor requires a durable runner claim')
    binding = store.runner_binding(reservation['id'], reservation['token'])
    child = subprocess.Popen(['/usr/bin/setsid', '/usr/bin/sleep', '90'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    atomic_json(Path(binding['directory']['path']) / 'ready.json',
                {'supervisor': os.getpid(), 'descendant': child.pid})
    time.sleep(90)


def provision(store, reservation, directory, scope):
    """The fixture's create/bind boundary; every external create has an intent."""
    credentials = reservation['id'], reservation['token']
    image = command('podman', 'image', 'inspect', '--format', '{{.Id}}', IMAGE).stdout.strip()
    container = fixture_container(reservation, image.removeprefix('sha256:'))
    store.runner_prepare(*credentials, str(directory), scope, container)
    cid = command(*container['config']['CreateCommand']).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{64}', cid):
        raise RuntimeError('missing container ID')
    # Never claim the stdout ID post hoc: the durable intent alone selects the
    # candidate, and inspection must confirm its exact image/config/prelaunch
    # state before the ID is bound and claimed. A retagged or substituted image
    # fails here and retains the hold without binding.
    candidates = intent_candidates(container)
    if candidates != [cid]:
        raise RuntimeError('provisioning container identity mismatch')
    inspect_prepared(container, cid)
    store.runner_bind(*credentials, str(directory), scope,
                      [{'name': container['name'], 'id': cid, 'uid': container['uid']}], authority_digest='b' * 64)
    return cid


def live(path):
    path.parent.mkdir(mode=0o700)  # refuse reuse; CI owns this disposable directory
    require_host()
    command('podman', 'pull', '--quiet', IMAGE, seconds=60)
    # Phase 1: bounded real pre-binding recovery. A fixture container is durably
    # prepared and created, then this controller simulates interruption before ID
    # binding; a separate recovery process must fence, validate the singleton
    # intent candidate, persist its ID before removal, and seal the failed
    # outcome. Uses its own idempotency key and exact ledger IDs; no production
    # integration.
    store, reservation, directory = create_job(path, key='smoke-prepared')
    credentials = reservation['id'], reservation['token']
    name = 'mizu-recovery-' + reservation['run_id']
    scope = {'unit': name + '.service', 'cgroup': '/system.slice/' + name + '.service'}
    image = command('podman', 'image', 'inspect', '--format', '{{.Id}}', IMAGE).stdout.strip()
    container = fixture_container(reservation, image.removeprefix('sha256:'))
    store.runner_prepare(*credentials, str(directory), scope, container)
    cid_prepared = command(*container['config']['CreateCommand']).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{64}', cid_prepared):
        raise RuntimeError('missing prepared container ID')
    # Simulated crash before runner_bind/runner_started: no binding or claim.
    if ledger(path).reservation()['id'] != reservation['id']:
        raise RuntimeError('pre-binding ledger identity mismatch')
    command(sys.executable, str(Path(__file__).resolve()), '--recover', str(path), seconds=75)
    if ledger(path).reservation() is not None:
        raise RuntimeError('pre-binding recovery did not release the synthetic reservation')
    if store.status('worker', reservation['id'])['execution'] != 'failed':
        raise RuntimeError('pre-binding terminal outcome missing')
    if container_exists(container['name']) or container_exists(cid_prepared):
        raise RuntimeError('pre-binding container absence unproven')
    if intent_candidates(container):
        raise RuntimeError('pre-binding marker absence unproven')
    print('Synthetic pre-binding fixture recovery proven; reservation released.')
    # Phase 2: existing systemd/descendant/bound-OCI recovery with its own key.
    store, reservation, directory = create_job(path, key='smoke-bound')
    credentials = reservation['id'], reservation['token']
    name = 'mizu-recovery-' + reservation['run_id']
    scope = {'unit': name + '.service', 'cgroup': '/system.slice/' + name + '.service'}
    try:
        cid = provision(store, reservation, directory, scope)
        store.runner_started(*credentials, authority_digest='b' * 64)  # durable identities AND claim precede execution
        command('sudo', '-n', 'systemd-run', '--unit=' + scope['unit'],
                '--slice=system.slice', '--service-type=exec',
                '--property=User=' + str(os.getuid()), '--property=WorkingDirectory=/',
                '--property=ExitType=cgroup', '--property=KillMode=control-group',
                '--property=TasksMax=8', '--property=CPUQuota=10%',
                '--property=MemoryMax=64M', '--property=RuntimeMaxSec=120',
                '--property=TimeoutStopSec=5', '--property=StandardOutput=null',
                '--property=StandardError=null', '--property=NoNewPrivileges=yes',
                '--property=PrivateNetwork=yes', sys.executable, str(Path(__file__).resolve()),
                '--supervise', str(path))
        ready_path = directory / 'ready.json'
        deadline = time.monotonic() + 5
        while not ready_path.is_file() and time.monotonic() < deadline:
            time.sleep(.05)
        ready = json.loads(ready_path.read_text())
        while os.getsid(ready['descendant']) != ready['descendant'] and time.monotonic() < deadline:
            time.sleep(.05)
        if (os.getsid(ready['descendant']) != ready['descendant']
                or not all(process_in_scope(pid, scope) for pid in ready.values())
                or unit_state(scope)['ControlGroup'] != scope['cgroup']):
            raise RuntimeError('supervisor/setsid cgroup evidence missing')
        command('podman', 'start', cid)
        if command('podman', 'inspect', '--format', '{{.State.Running}}', cid).stdout.strip() != 'true':
            raise RuntimeError('container not running')
        command('sudo', '-n', 'systemctl', 'kill', '--kill-whom=main',
                '--signal=SIGKILL', scope['unit'])
        deadline = time.monotonic() + 5
        while Path(f"/proc/{ready['supervisor']}").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if (Path(f"/proc/{ready['supervisor']}").exists()
                or not process_in_scope(ready['descendant'], scope)
                or cgroup_empty(scope) or not container_exists(cid)
                or ledger(path).reservation()['binding']['containers'][0]['id'] != cid):
            raise RuntimeError('supervisor-death/held-reservation evidence missing')
        # All launches have settled and the execution supervisor is dead, but
        # its setsid descendant AND container remain: terminal != quiescent.
        store.runner_finished(*credentials, 'failed')
        if ledger(path).reservation() is None:
            raise RuntimeError('terminal execution released reservation prematurely')
    finally:
        # Separate process uses ONLY the actual durable job ledger. CI repeats
        # recovery in always() if the controller dies. Unsettled execution or
        # unknown evidence retains the hold; a prepared fixture needs its own
        # positive prelaunch and absence proofs.
        command(sys.executable, str(Path(__file__).resolve()), '--recover', str(path), seconds=75)
    if ledger(path).reservation() is not None:
        raise RuntimeError('recovery did not release the synthetic reservation')
    print('Synthetic supervisor death and scoped ledger recovery proven; reservation released.')


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

    def fixture(self, path, terminal=True, containers=1):
        store, reservation, directory = create_job(path)
        credentials = reservation['id'], reservation['token']
        scope = {'unit': 'fixture.service', 'cgroup': '/system.slice/fixture.service'}
        saved = [{'name': 'fixture-' + str(i), 'id': str(i + 1) * 64, 'uid': os.getuid()}
                 for i in range(containers)]
        store.runner_bind(*credentials, str(directory), scope, saved, authority_digest='b' * 64)
        store.runner_started(*credentials, authority_digest='b' * 64)
        if terminal:
            store.runner_finished(*credentials, 'failed')
        return store

    @unittest.skipUnless(os.name == 'posix', 'systemd/rootless OCI recovery fixture')
    def test_recovery_requires_every_scope_and_terminal_execution(self):
        module = sys.modules[__name__]
        for group, container, terminal in ((True, True, True), (False, True, True),
                (None, True, True), (True, False, True), (True, None, True),
                (True, True, False), (OSError('unknown cgroup'), True, True),
                (True, OSError('unknown runtime'), True)):
            with self.subTest(group=group, container=container, terminal=terminal), \
                    tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                self.fixture(path, terminal, containers=2)
                def probe(value):
                    self.assertIsNotNone(ledger(path).reservation())
                    if isinstance(value, Exception):
                        raise value
                    return value
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(module, 'cgroup_empty', side_effect=lambda _: probe(group)),
                      mock.patch.object(module, 'clean_container', side_effect=lambda _: probe(container)) as cleanup):
                    if group is True and container is True and terminal:
                        recover(path)
                        recover(path)  # uncertain final acknowledgement: inspect, never relaunch
                        self.assertIsNone(ledger(path).reservation())
                    else:
                        with self.assertRaises((Denied, OSError)):
                            recover(path)
                        self.assertIsNotNone(ledger(path).reservation())
                    self.assertEqual(cleanup.call_count, 2)

    @unittest.skipUnless(os.name == 'posix', 'systemd/rootless OCI recovery fixture')
    def test_unavailable_host_or_missing_binding_never_releases(self):
        module = sys.modules[__name__]
        for bound in (False, True):
            with self.subTest(bound=bound), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                if bound:
                    self.fixture(path)
                else:
                    create_job(path)
                with mock.patch.object(module, 'require_host', side_effect=RuntimeError('unsupported host')):
                    with self.assertRaises((RuntimeError, Denied)):
                        recover(path)
                self.assertIsNotNone(ledger(path).reservation())

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_recovery_rejects_unsafe_incomplete_or_changed_directory_before_commands(self):
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'state.json'
            store = self.fixture(path)
            directory = Path(store.reservation()['binding']['directory']['path'])
            targets = [path.parent, store.root, store.root / 'state.json']
            with mock.patch.object(module, 'command') as invoke:
                for target in targets:
                    mode = stat.S_IMODE(target.stat().st_mode)
                    target.chmod(mode | 0o044)
                    with self.assertRaisesRegex(RuntimeError, 'private'):
                        recover(path)
                    target.chmod(mode)
                lock = store.root / 'store.lock'
                lock.rename(store.root / 'missing.lock')
                with self.assertRaises(FileNotFoundError):
                    recover(path)
                self.assertFalse(lock.exists())
                (store.root / 'missing.lock').rename(lock)
                directory.rename(directory.with_name('renamed'))
                with self.assertRaises(OSError):
                    recover(path)
                invoke.assert_not_called()
            self.assertIsNotNone(store.reservation())

    @unittest.skipUnless(os.name == 'posix', 'rootless OCI identity namespace')
    def test_exact_container_identity_rename_mismatch_unknown_and_absence(self):
        module = sys.modules[__name__]
        saved = {'name': 'fixture', 'id': 'a' * 64, 'uid': os.getuid()}
        for exists, inspected, success in [
                ([False, False, False, False], None, True),
                ([True, True, False, False], 'a' * 64 + ' fixture', True),
                ([False, True], None, False), ([True, False], None, False),
                ([True, True], 'b' * 64 + ' fixture', False),
                ([True, True], 'a' * 64 + ' renamed', False),
                ([OSError('unknown runtime')], None, False)]:
            with self.subTest(exists=exists, inspected=inspected):
                with (mock.patch.object(module, 'container_exists', side_effect=exists),
                      mock.patch.object(module, 'command', return_value=mock.Mock(stdout=inspected)) as invoke):
                    if success:
                        self.assertTrue(clean_container(saved))
                    else:
                        with self.assertRaises((OSError, RuntimeError)):
                            clean_container(saved)
                        if inspected is None:
                            invoke.assert_not_called()
                        self.assertFalse(any(call.args[:2] == ('podman', 'rm') for call in invoke.call_args_list))
        with mock.patch.object(module, 'command') as invoke:
            with self.assertRaises(RuntimeError):
                clean_container({**saved, 'uid': os.getuid() + 1})
            invoke.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'systemd/rootless OCI recovery fixture')
    def test_recovery_uses_saved_identities_not_fixture_or_derived_names(self):
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'state.json'
            store = self.fixture(path)
            binding = store.reservation()['binding']
            # A misleading old fixture file is not recovery authority.
            atomic_json(path, {'name': 'wrong', 'container_id': 'b' * 64})
            with (mock.patch.object(module, 'require_host'),
                  mock.patch.object(module, 'unit_state', return_value={
                      'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}) as scope,
                  mock.patch.object(module, 'cgroup_empty', return_value=True),
                  mock.patch.object(module, 'clean_container', return_value=True) as cleanup):
                recover(path)
                scope.assert_called_with(binding['scope'])
                cleanup.assert_called_once_with(binding['containers'][0])
            self.assertIsNone(store.reservation())

    def prepared_fixture(self, path, uid_offset=0, prepared=True):
        store, reservation, directory = create_job(path)
        credentials = reservation['id'], reservation['token']
        container = fixture_container(reservation, 'a' * 64)
        container['uid'] += uid_offset
        name = container['name']
        scope = {'unit': name + '.service', 'cgroup': '/system.slice/' + name + '.service'}
        if prepared:
            store.runner_prepare(*credentials, str(directory), scope, container)
        cid = 'b' * 64
        config = copy.deepcopy(container['config'])
        observed = {'Id': cid, 'Name': name, 'Image': container['image'], 'RestartCount': 0,
                    'Config': {k: config[k] for k in ('CreateCommand', 'Cmd', 'Entrypoint', 'User', 'Labels')},
                    'EffectiveCaps': [], 'BoundingCaps': [],
                    'HostConfig': {k: config[k] for k in ('NetworkMode', 'ReadonlyRootfs', 'Privileged',
                        'Binds', 'CapAdd', 'SecurityOpt', 'PidsLimit', 'Memory', 'CpuPeriod', 'CpuQuota')},
                    'Mounts': [], 'State': {'Status': 'configured', 'Running': False, 'Pid': 0,
                        'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z'}}
        observed['HostConfig']['LogConfig'] = {'Type': 'none'}
        runtime = {'name': [cid], 'marker': [cid], 'observed': observed}
        def invoke(*argv, **kwargs):
            if argv[:3] == ('podman', 'image', 'inspect'):
                return mock.Mock(stdout=container['image'])
            if argv[:2] == ('podman', 'create'):
                current = ledger(path).reservation()
                self.assertEqual(current['intent']['container'], container)
                self.assertIsNone(current['binding'])
                self.assertFalse(current['intent']['claimed'])
                self.assertEqual(list(argv), container['config']['CreateCommand'])
                return mock.Mock(stdout=cid)
            if argv[:2] == ('podman', 'ps'):
                selector = argv[argv.index('--filter') + 1]
                expected = ('name=^/?' + name + '$' if selector.startswith('name=')
                            else 'label=' + MARKER + '=' + container['marker'])
                self.assertEqual(selector, expected)
                ids = runtime['name' if selector.startswith('name=') else 'marker']
                return mock.Mock(stdout='\n'.join(ids))
            if argv[:2] == ('podman', 'inspect'):
                self.assertIn(argv[-1], (name, cid))
                if isinstance(runtime['observed'], Exception):
                    raise runtime['observed']
                return mock.Mock(stdout=(runtime['observed'] if isinstance(runtime['observed'], str)
                                         else json.dumps(runtime['observed'])))
            if argv[:2] == ('podman', 'rm'):
                self.assertEqual(argv, ('podman', 'rm', cid))
                current = ledger(path).reservation()
                self.assertIsNotNone(current)
                self.assertEqual(current['intent']['id'], cid)  # durable BEFORE rm
                self.assertTrue(current['intent']['recovering'])
                runtime.update(name=[], marker=[])
                return mock.Mock(stdout=cid)
            if argv[:3] == ('podman', 'container', 'exists'):
                self.assertIn(argv[-1], (name, cid))
                return mock.Mock(exit_code=0 if runtime['name'] else 1)
            raise AssertionError(argv)
        return store, credentials, runtime, invoke

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_supervisor_cannot_start_a_prepared_or_recovering_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'state.json'
            store, credentials, runtime, invoke = self.prepared_fixture(path)
            intent = store.reservation()['intent']
            with mock.patch.object(subprocess, 'Popen') as launch:
                for stage in ('unbound', 'bound', 'recovering'):
                    if stage == 'bound':
                        store.runner_bind(*credentials, intent['directory']['path'], intent['scope'],
                            [{'name': intent['container']['name'], 'id': 'b' * 64, 'uid': os.getuid()}], authority_digest='b' * 64)
                    elif stage == 'recovering':
                        store.runner_recover_prepared(*credentials)
                    with self.assertRaisesRegex(RuntimeError, 'durable runner claim'):
                        supervise(path)
                launch.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_created_before_bind_and_uncertain_bind_write_recover_without_launch(self):
        module = sys.modules[__name__]
        # No bind, failed pre-replace bind, and committed-but-unacknowledged bind.
        for failure in ('interrupted', 'before', 'after'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store, credentials, runtime, invoke = self.prepared_fixture(path, prepared=False)
                reservation = store.reservation()
                directory = path.parent / reservation['run_id']
                name = runtime['observed']['Name']
                scope = {'unit': name + '.service', 'cgroup': '/system.slice/' + name + '.service'}
                save = store._save
                def uncertain(state):
                    binding = state['jobs'][credentials[0]]['binding']
                    if binding is not None:
                        if failure == 'after':
                            save(state)
                        raise OSError('injected bind acknowledgement loss')
                    save(state)
                def interrupted(*argv, **kwargs):
                    result = invoke(*argv, **kwargs)
                    if failure == 'interrupted' and argv[:2] == ('podman', 'create'):
                        raise OSError('injected interruption after external create')
                    return result
                with (mock.patch.object(store, '_save', side_effect=uncertain),
                      mock.patch.object(module, 'command', side_effect=interrupted)):
                    with self.assertRaises(OSError):
                        provision(store, reservation, directory, scope)
                self.assertEqual(store.reservation()['binding'] is not None, failure == 'after')
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(module, 'command', side_effect=invoke) as commands):
                    recover(path)
                    recover(path)
                    self.assertEqual(sum(c.args[:2] == ('podman', 'rm') for c in commands.call_args_list), 1)
                self.assertIsNone(store.reservation())
                self.assertEqual(store.status('worker', credentials[0])['execution'], 'failed')

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_provision_verifies_singleton_identity_before_bind(self):
        # The stdout ID is never claimed post hoc: provision must find exactly
        # the durable intent candidate and confirm its image/config/prelaunch
        # state before runner_bind. Any mismatch retains the hold unbound.
        module = sys.modules[__name__]
        for failure in ('mismatch-id', 'duplicate', 'wrong-image', 'wrong-command', 'running'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store, credentials, runtime, invoke = self.prepared_fixture(path, prepared=False)
                reservation = store.reservation()
                directory = path.parent / reservation['run_id']
                name = runtime['observed']['Name']
                scope = {'unit': name + '.service', 'cgroup': '/system.slice/' + name + '.service'}
                observed = runtime['observed']
                if failure == 'mismatch-id':
                    runtime.update(name=['b' * 64], marker=['c' * 64])
                elif failure == 'duplicate':
                    runtime['marker'] = ['b' * 64, 'c' * 64]
                elif failure == 'wrong-image':
                    observed['Image'] = 'c' * 64
                elif failure == 'wrong-command':
                    observed['Config']['Cmd'] = ['substitute']
                elif failure == 'running':
                    observed['State']['Running'] = True
                with mock.patch.object(module, 'command', side_effect=invoke):
                    with self.assertRaises(RuntimeError):
                        provision(store, reservation, directory, scope)
                current = store.reservation()
                self.assertIsNotNone(current)
                self.assertIsNotNone(current['intent'])
                self.assertIsNone(current['binding'])
                self.assertEqual(current['intent']['container']['marker'], reservation['run_id'])

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_prepared_unknown_candidates_never_remove_or_release(self):
        module = sys.modules[__name__]
        mutations = ['missing', 'renamed', 'duplicate', 'malformed-id', 'unobservable', 'malformed', 'duplicate-json', 'nonfinite',
                     'wrong-id', 'wrong-name', 'wrong-label', 'wrong-image', 'wrong-command',
                     'wrong-entrypoint', 'wrong-caps',
                     'writable', 'running', 'started', 'scope', 'namespace']
        for failure in mutations:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store, credentials, runtime, invoke = self.prepared_fixture(
                    path, uid_offset=1 if failure == 'namespace' else 0)
                observed = runtime['observed']
                fields = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}
                if failure == 'missing':
                    runtime.update(name=[], marker=[])
                elif failure == 'renamed':
                    runtime['name'] = []
                elif failure == 'duplicate':
                    runtime['marker'] = ['b' * 64, 'c' * 64]
                elif failure == 'malformed-id':
                    runtime.update(name=['short'], marker=['short'])
                elif failure == 'unobservable':
                    runtime['observed'] = OSError('runtime unavailable')
                elif failure == 'malformed':
                    runtime['observed'] = {}
                elif failure == 'duplicate-json':
                    runtime['observed'] = '{"Id":"' + 'c' * 64 + '",' + json.dumps(observed)[1:]
                elif failure == 'nonfinite':
                    observed['Unknown'] = float('nan')
                elif failure == 'wrong-id':
                    observed['Id'] = 'c' * 64
                elif failure == 'wrong-name':
                    observed['Name'] = 'substitute'
                elif failure == 'wrong-label':
                    observed['Config']['Labels'] = {MARKER: 'c' * 32}
                elif failure == 'wrong-image':
                    observed['Image'] = 'c' * 64
                elif failure == 'wrong-command':
                    observed['Config']['Cmd'] = ['substitute']
                elif failure == 'wrong-entrypoint':
                    observed['Config']['Entrypoint'] = '/bin/sh'
                elif failure == 'wrong-caps':
                    observed['BoundingCaps'] = ['CAP_CHOWN']
                elif failure == 'writable':
                    observed['HostConfig']['ReadonlyRootfs'] = False
                elif failure == 'running':
                    observed['State']['Running'] = True
                elif failure == 'started':
                    observed['State']['StartedAt'] = '2026-01-01T00:00:00Z'
                elif failure == 'scope':
                    fields['LoadState'] = 'loaded'
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value=fields),
                      mock.patch.object(module, 'command', side_effect=invoke) as commands):
                    with self.assertRaises((RuntimeError, OSError, KeyError, ValueError)):
                        recover(path)
                    self.assertFalse(any(c.args[:2] == ('podman', 'rm') for c in commands.call_args_list))
                self.assertIsNotNone(store.reservation())

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_prepared_removal_and_cleanup_write_uncertainty_are_retryable(self):
        module = sys.modules[__name__]
        for failure in ('id-before', 'id-after', 'remove', 'proof', 'name-survivor',
                        'id-survivor', 'marker-survivor', 'seal-after'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store, credentials, runtime, invoke = self.prepared_fixture(path)
                real_save = DelegationJobs._save
                def save(instance, state):
                    active = state['active']
                    candidate = state['jobs'][credentials[0]]['intent']
                    if failure in ('id-before', 'id-after') and candidate is not None and candidate['id'] is not None:
                        if failure == 'id-after':
                            real_save(instance, state)
                        raise OSError('injected ID write uncertainty')
                    real_save(instance, state)
                    if failure == 'seal-after' and active is None:
                        raise OSError('injected seal acknowledgement loss')
                def interrupted(*argv, **kwargs):
                    result = invoke(*argv, **kwargs)
                    if failure == 'remove' and argv[:2] == ('podman', 'rm'):
                        raise OSError('injected removal acknowledgement loss')
                    if failure == 'proof' and argv[:3] == ('podman', 'container', 'exists'):
                        raise OSError('injected absence uncertainty')
                    if argv[:3] == ('podman', 'container', 'exists'):
                        if ((failure == 'id-survivor' and argv[-1] == 'b' * 64)
                                or (failure == 'name-survivor' and argv[-1] != 'b' * 64)):
                            return mock.Mock(exit_code=0)
                    if (failure == 'marker-survivor' and not runtime['name']
                            and argv[:2] == ('podman', 'ps')
                            and argv[argv.index('--filter') + 1].startswith('label=')):
                        return mock.Mock(stdout='b' * 64)
                    return result
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(DelegationJobs, '_save', new=save),
                      mock.patch.object(module, 'command', side_effect=interrupted) as commands):
                    with self.assertRaises((OSError, RuntimeError)):
                        recover(path)
                    if failure in ('id-before', 'id-after'):
                        self.assertFalse(any(c.args[:2] == ('podman', 'rm') for c in commands.call_args_list))
                self.assertEqual(store.reservation() is None, failure == 'seal-after')
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(module, 'command', side_effect=invoke)):
                    recover(path)
                    recover(path)
                self.assertIsNone(store.reservation())


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
