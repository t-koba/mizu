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
from mizu import delegation_recovery
from mizu.delegation_recovery import _recover_quiescent
from mizu.delegation_scope import _scope_empty
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
    # Rootless Podman startup can exceed the generic 10-second command bound
    # on a loaded CI host; keep a larger per-query bound inside the workflow cap.
    info = command('podman', 'info', '--format',
                   '{{.Host.Security.Rootless}} {{.Host.CgroupManager}}',
                   seconds=30, diagnostic_bytes=COMMAND_DIAGNOSTIC_BYTES_MAX)
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


def scope_query(unit):
    return command(
        'sudo', '-n', 'systemctl', 'show', unit,
        '--property=LoadState,ActiveState,ControlGroup', codes=(0, 1))


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
    name, marker = 'mizu-delegation-' + reservation['run_id'], reservation['run_id']
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
    # Host fixture owns the one-shot launch fence: no relaunch path exists,
    # create/start commands have settled, and --live waited for the synthetic
    # supervisor to die. This is NOT a fence against unrelated host mutation.
    # Validate terminal execution before any host query or cleanup action.
    if reservation['execution'] is None:
        raise Denied('Delegation execution is not terminal')
    def attest_launch_fence(saved):
        # Host one-shot fence is held for this exact binding; a fence attested
        # for any other binding is stale and must not release this reservation.
        if canonical(saved) != canonical(binding):
            raise Denied('stale launch fence attestation')
        return True

    def attest_supervisor_settled(saved):
        if canonical(saved) != canonical(binding):
            raise Denied('stale supervisor attestation')
        return True

    def query(unit):
        # All systemd preflight/settle failures are scope-proof failures: the
        # coordinator's try/finally still attempts every saved OCI cleanup, but
        # no runner_quiescent occurs without passing scope proof. Host commands
        # stay in this fixture query; Mizu itself never executes host commands.
        require_host()
        if unit != scope['unit']:
            raise RuntimeError('systemd unit mismatch')
        fields = unit_state(scope)
        if fields['LoadState'] == 'loaded':
            command('sudo', '-n', 'systemctl', 'kill', '--kill-whom=all',
                    '--signal=SIGKILL', scope['unit'], codes=(0, 1))
            command('sudo', '-n', 'systemctl', 'stop', scope['unit'])
            command('sudo', '-n', 'systemctl', 'reset-failed', scope['unit'], codes=(0, 1))
        return scope_query(unit)

    _recover_quiescent(store, *credentials, 'Synthetic descendant and OCI cleanup verified',
                       binding=binding, attest_launch_fence=attest_launch_fence,
                       attest_supervisor_settled=attest_supervisor_settled,
                       query=query, reconcile_container=clean_container, maximum=8192)


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
    name = 'mizu-delegation-' + reservation['run_id']
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
    name = 'mizu-delegation-' + reservation['run_id']
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
                or _scope_empty(store.runner_binding(*credentials), query=scope_query, maximum=8192)
                or not container_exists(cid)
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
            seconds=30, diagnostic_bytes=COMMAND_DIAGNOSTIC_BYTES_MAX)

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
        unit = 'mizu-delegation-' + reservation['run_id'] + '.service'
        scope = {'unit': unit, 'cgroup': '/system.slice/' + unit}
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
                      mock.patch.object(delegation_recovery, '_scope_empty', side_effect=lambda *a, **k: probe(group)),
                      mock.patch.object(module, 'clean_container', side_effect=lambda _: probe(container)) as cleanup):
                    if group is True and container is True and terminal:
                        recover(path)
                        recover(path)  # uncertain final acknowledgement: inspect, never relaunch
                        self.assertIsNone(ledger(path).reservation())
                    else:
                        with self.assertRaises((Denied, OSError)):
                            recover(path)
                        self.assertIsNotNone(ledger(path).reservation())
                    self.assertEqual(cleanup.call_count, 2 if terminal else 0)

    @unittest.skipUnless(os.name == 'posix', 'systemd/rootless OCI recovery fixture')
    def test_unavailable_host_or_missing_binding_never_releases(self):
        module = sys.modules[__name__]
        for bound in (False, True):
            with self.subTest(bound=bound), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                if bound:
                    store = self.fixture(path)
                    expected = len(store.reservation()['binding']['containers'])
                else:
                    create_job(path)
                    expected = 0
                with (mock.patch.object(module, 'require_host', side_effect=RuntimeError('unsupported host')),
                      mock.patch.object(module, 'clean_container', return_value=True) as cleanup):
                    with self.assertRaises((RuntimeError, Denied)):
                        recover(path)
                self.assertIsNotNone(ledger(path).reservation())
                # Settling preflight is a scope-proof failure inside the coordinator's
                # try/finally: every saved OCI cleanup is still attempted, but no
                # quiescent occurs without passing scope proof.
                self.assertEqual(cleanup.call_count, expected)

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
            with (mock.patch.object(module, 'require_host') as preflight,
                  mock.patch.object(module, 'unit_state', return_value={
                      'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}) as scope,
                  mock.patch.object(module, 'scope_query',
                                    return_value=mock.Mock(reason='exited', exit_code=1, survivors=(),
                                                           stdout=('LoadState=not-found\nActiveState=inactive\n'
                                                                   'ControlGroup=\n'))) as queried,
                  mock.patch.object(delegation_recovery, '_scope_empty', return_value=True) as proof,
                  mock.patch.object(module, 'clean_container', return_value=True) as cleanup):
                recover(path)
                self.assertEqual(proof.call_args.args, (binding,))
                cleanup.assert_called_once_with(binding['containers'][0])
                # The settling query is only exercised via the coordinator's scope
                # proof; a mocked proof bypasses it, so invoke the captured query
                # to prove it uses the saved scope, not the misleading file.
                query = proof.call_args.kwargs['query']
                scope.assert_not_called()
                queried.assert_not_called()
                preflight.assert_not_called()
                query(binding['scope']['unit'])
                preflight.assert_called_once_with()
                scope.assert_called_once_with(binding['scope'])
                queried.assert_called_once_with(binding['scope']['unit'])
                with self.assertRaises(RuntimeError):
                    query('mizu-delegation-' + '0' * 32 + '.service')
            self.assertIsNone(store.reservation())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux read-only scope proof')
    def test_supervisor_exit_recovery_uses_product_scope_proof(self):
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'state.json'
            store = self.fixture(path)  # terminal supervisor, reservation still held
            binding = store.runner_binding(store.reservation()['id'], store.reservation()['token'])
            root = path.parent / 'cgroup'
            group = root / binding['scope']['cgroup'].lstrip('/')
            group.mkdir(parents=True)
            (root / 'cgroup.controllers').write_text('cpu memory pids\n')
            events = group / 'cgroup.events'
            events.write_text('populated 1\n')  # surviving descendant, no direct process
            (group / 'cgroup.procs').write_text('')
            observed = mock.Mock(reason='exited', exit_code=0, survivors=(), stdout=(
                'LoadState=loaded\nActiveState=inactive\nControlGroup=' + binding['scope']['cgroup'] + '\n'))
            product_proof = _scope_empty
            def proof(saved, **kwargs):
                return product_proof(saved, **(kwargs | {'cgroup_root': root}))
            with (mock.patch.object(module, 'require_host'),
                  mock.patch.object(module, 'unit_state', return_value={'LoadState': 'not-found'}),
                  mock.patch.object(module, 'command', return_value=observed) as query,
                  mock.patch.object(delegation_recovery, '_scope_empty', side_effect=proof),
                  mock.patch.object(module, 'clean_container', return_value=True)):
                with self.assertRaises(Denied):
                    recover(path)
                self.assertIsNotNone(store.reservation())
                events.unlink()
                with self.assertRaises(FileNotFoundError):
                    recover(path)
                self.assertIsNotNone(store.reservation())
                events.write_text('populated 0\n')
                recover(path)
                self.assertIsNone(store.reservation())
                self.assertEqual(query.call_count, 3)
                for call in query.call_args_list:
                    self.assertEqual(call.args[:5], ('sudo', '-n', 'systemctl', 'show', binding['scope']['unit']))

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_coordinator_rejects_stale_or_unsettled_inputs_before_callbacks(self):
        for case in ('job', 'token', 'inactive', 'unbound', 'binding', 'missing-binding', 'directory',
                     'execution', 'result', 'missing-fence', 'missing-supervisor',
                     'fence-false', 'fence-truthy', 'fence-raises', 'stale-fence',
                     'supervisor-false', 'supervisor-raises', 'stale-supervisor'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                if case == 'unbound':
                    store, pending, _ = create_job(path)
                    store.runner_finished(pending['id'], pending['token'], 'failed')
                else:
                    store = self.fixture(path, terminal=case != 'execution')
                reservation = store.reservation()
                binding = reservation['binding']
                job_id, token = reservation['id'], reservation['token']
                stale = None if binding is None else copy.deepcopy(binding)
                if stale is not None:
                    stale['containers'][0]['id'] = 'e' * 64
                fence_seen, settled_seen = [], []
                def attest_fence(saved):
                    fence_seen.append(copy.deepcopy(saved))
                    if case == 'fence-false':
                        saved['run_id'] = '0' * 32
                        return False
                    if case == 'fence-truthy':
                        saved['run_id'] = '0' * 32
                        return 1
                    if case == 'fence-raises':
                        raise Denied('stale launch fence attestation')
                    if case == 'stale-fence':
                        # This fence attests a different binding, not the saved one.
                        verdict = canonical(saved) == canonical(stale)
                        saved['run_id'] = '0' * 32  # mutation must not escape the copy
                        return True if verdict else False
                    saved['run_id'] = '0' * 32  # mutation must not escape the copy
                    return True
                def attest_settled(saved):
                    settled_seen.append(copy.deepcopy(saved))
                    if case == 'supervisor-false':
                        saved['run_id'] = '0' * 32
                        return None
                    if case == 'supervisor-raises':
                        raise OSError('unobservable supervisor')
                    if case == 'stale-supervisor':
                        verdict = canonical(saved) == canonical(stale)
                        saved['run_id'] = '0' * 32
                        return True if verdict else {}
                    saved['run_id'] = '0' * 32
                    return True
                options = dict(binding=binding, attest_launch_fence=attest_fence,
                               attest_supervisor_settled=attest_settled,
                               query=mock.Mock(), reconcile_container=mock.Mock(), maximum=8192)
                result = 'Recovery evidence'
                if case == 'inactive':
                    store.runner_quiescent(job_id, token, result, cleanup={
                        'binding': binding, 'scope_clean': True,
                        'containers_absent': [c['id'] for c in binding['containers']]})
                elif case == 'job':
                    job_id = '0' * 64
                elif case == 'token':
                    token = '0' * 64
                elif case == 'binding':
                    options['binding'] = stale
                elif case == 'missing-binding':
                    options['binding'] = None
                elif case == 'directory':
                    directory = Path(binding['directory']['path'])
                    directory.rename(directory.with_name('replaced'))
                    directory.mkdir()
                elif case == 'missing-fence':
                    options['attest_launch_fence'] = None
                elif case == 'missing-supervisor':
                    options['attest_supervisor_settled'] = 'attested'
                elif case == 'result':
                    result = 'x' * (32 * 1024 + 1)
                before = (store.root / 'state.json').read_bytes()
                with self.assertRaises(Denied):
                    _recover_quiescent(store, job_id, token, result, **options)
                options['query'].assert_not_called()
                options['reconcile_container'].assert_not_called()
                if case in ('job', 'token', 'inactive', 'unbound', 'binding',
                            'missing-binding', 'directory', 'execution', 'result',
                            'missing-fence', 'missing-supervisor'):
                    # Token/binding/terminal/callback validation precedes attestations.
                    self.assertEqual(fence_seen, [])
                    self.assertEqual(settled_seen, [])
                elif case in ('fence-false', 'fence-truthy', 'fence-raises', 'stale-fence'):
                    # A refused fence never reaches the supervisor or any evidence query.
                    self.assertEqual(len(fence_seen), 1)
                    self.assertEqual(settled_seen, [])
                    if case == 'stale-fence':
                        self.assertNotEqual(canonical(fence_seen[0]), canonical(stale))
                        self.assertEqual(canonical(fence_seen[0]), canonical(binding))
                else:
                    self.assertEqual(len(fence_seen), 1)
                    self.assertEqual(len(settled_seen), 1)
                    self.assertEqual(canonical(fence_seen[0]), canonical(binding))
                    self.assertEqual(canonical(settled_seen[0]), canonical(binding))
                self.assertEqual((store.root / 'state.json').read_bytes(), before)

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_coordinator_requires_literal_absence_and_preserves_saved_inventory(self):
        for evidence in (None, False, 1, {}, {'absent': True}, True):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store = self.fixture(path, containers=2)
                reservation = store.reservation()
                binding = copy.deepcopy(reservation['binding'])
                seen = []
                fence_seen, settled_seen = [], []
                def attest_fence(saved):
                    fence_seen.append(copy.deepcopy(saved))
                    saved['containers'][0]['id'] = 'f' * 64  # cannot rewrite saved proof
                    return True
                def attest_settled(saved):
                    settled_seen.append(copy.deepcopy(saved))
                    saved['run_id'] = '0' * 32
                    return True
                def reconcile(container):
                    seen.append(copy.deepcopy(container))
                    container['id'] = 'f' * 64  # cannot rewrite saved cleanup proof
                    return evidence
                with (mock.patch.object(delegation_recovery, '_scope_empty', return_value=True),
                      mock.patch.object(store, 'runner_quiescent', wraps=store.runner_quiescent) as seal):
                    def attempt():
                        _recover_quiescent(store, reservation['id'], reservation['token'], 'Evidence',
                                           binding=binding, attest_launch_fence=attest_fence,
                                           attest_supervisor_settled=attest_settled,
                                           query=mock.Mock(), reconcile_container=reconcile, maximum=8192)
                    if evidence is True:
                        attempt()
                        self.assertEqual(seal.call_args.kwargs['cleanup'], {
                            'binding': binding, 'scope_clean': True,
                            'containers_absent': [c['id'] for c in binding['containers']]})
                        self.assertIsNone(store.reservation())
                    else:
                        with self.assertRaises(Denied):
                            attempt()
                        seal.assert_not_called()
                        self.assertEqual(store.reservation(), reservation)
                self.assertEqual(seen, binding['containers'])
                self.assertEqual(fence_seen, [binding])
                self.assertEqual(settled_seen, [binding])

    @unittest.skipUnless(os.name == 'posix', 'private POSIX job ledger')
    def test_bound_recovery_interrupted_cleanup_and_uncertain_commit_retry(self):
        module = sys.modules[__name__]
        for boundary in ('container', 'before-commit', 'after-commit'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store = self.fixture(path, containers=2)
                binding = store.reservation()['binding']
                remaining = {c['id'] for c in binding['containers']}
                interrupted = False
                def reconcile(container):
                    nonlocal interrupted
                    if boundary == 'container' and len(remaining) == 1 and not interrupted:
                        interrupted = True
                        raise OSError('interrupted after first removal')
                    remaining.discard(container['id'])
                    return True  # exact absence remains positive on retry
                original_save = DelegationJobs._save
                def uncertain_save(current, state):
                    if boundary == 'after-commit':
                        original_save(current, state)
                    raise OSError('interrupted handback acknowledgement')
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={'LoadState': 'not-found'}),
                      mock.patch.object(delegation_recovery, '_scope_empty', return_value=True),
                      mock.patch.object(module, 'clean_container', side_effect=reconcile) as cleanup):
                    if boundary == 'container':
                        with self.assertRaises(OSError):
                            recover(path)
                    else:
                        with mock.patch.object(DelegationJobs, '_save', uncertain_save):
                            with self.assertRaises(OSError):
                                recover(path)
                    self.assertEqual(ledger(path).reservation() is None, boundary == 'after-commit')
                    recover(path)  # reopen ledger; no persisted partial proof is reused
                    recover(path)  # uncertain success is resolved without any more callbacks
                    self.assertIsNone(ledger(path).reservation())
                    self.assertFalse(remaining)
                    self.assertEqual(cleanup.call_count, 2 if boundary == 'after-commit' else 4)

    @unittest.skipUnless(os.name == 'posix', 'systemd/rootless OCI recovery fixture')
    def test_settling_preflight_failure_still_attempts_all_container_cleanup_and_retries(self):
        # Regression for the reviewer finding: recover() must not skip saved OCI
        # cleanup when systemd preflight/settle fails. The settling query runs
        # inside the coordinator's try/finally, so every saved container is
        # still attempted, but no runner_quiescent occurs without scope proof.
        module = sys.modules[__name__]
        for failure in ('require_host', 'unit_state', 'settle_kill', 'settle_stop',
                        'settle_reset', 'scope_show'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'state.json'
                store = self.fixture(path, containers=2)
                binding = copy.deepcopy(store.reservation()['binding'])
                seen = []
                def tracking_cleanup(container):
                    seen.append(copy.deepcopy(container))
                    return True
                observed_show = mock.Mock(reason='exited', exit_code=1, survivors=(),
                                          stdout=('LoadState=not-found\nActiveState=inactive\n'
                                                  'ControlGroup=\n'))
                def settling_command(*argv, **kwargs):
                    sub = argv[3] if len(argv) > 3 else ''
                    if failure == 'settle_kill' and sub == 'kill':
                        raise RuntimeError('systemctl kill failed')
                    if failure == 'settle_stop' and sub == 'stop':
                        raise RuntimeError('systemctl stop failed')
                    if failure == 'settle_reset' and sub == 'reset-failed':
                        raise RuntimeError('systemctl reset-failed failed')
                    if failure == 'scope_show' and sub == 'show':
                        raise RuntimeError('systemctl show failed')
                    if sub == 'show':
                        return observed_show
                    return mock.Mock(reason='exited', exit_code=0, survivors=(), stdout='')
                require_patch = (mock.patch.object(module, 'require_host',
                                                   side_effect=RuntimeError('unsupported host'))
                                 if failure == 'require_host'
                                 else mock.patch.object(module, 'require_host'))
                if failure == 'unit_state':
                    unit_patch = mock.patch.object(module, 'unit_state',
                                                   side_effect=RuntimeError('unknown systemd identity'))
                elif failure.startswith('settle'):
                    unit_patch = mock.patch.object(module, 'unit_state', return_value={
                        'LoadState': 'loaded', 'ActiveState': 'active',
                        'ControlGroup': binding['scope']['cgroup']})
                else:
                    unit_patch = mock.patch.object(module, 'unit_state', return_value={
                        'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''})
                with (require_patch as preflight,
                      unit_patch as scope_state,
                      mock.patch.object(module, 'command', side_effect=settling_command) as settled,
                      mock.patch.object(module, 'clean_container', side_effect=tracking_cleanup) as cleanup):
                    # Real scope proof invokes the settling query; preflight/settle
                    # failures surface as scope-proof failures after cleanup.
                    with self.assertRaises((RuntimeError, Denied, OSError)):
                        recover(path)
                    # Exact per-container attempts in saved order, reservation held.
                    self.assertEqual(cleanup.call_count, 2)
                    self.assertEqual(seen, binding['containers'])
                    self.assertIsNotNone(store.reservation())
                    if failure == 'require_host':
                        scope_state.assert_not_called()
                        settled.assert_not_called()
                    elif failure == 'unit_state':
                        settled.assert_not_called()
                # Safe retry with passing scope proof releases the reservation and
                # reuses no persisted partial proof.
                with (mock.patch.object(module, 'require_host'),
                      mock.patch.object(module, 'unit_state', return_value={
                          'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': ''}),
                      mock.patch.object(delegation_recovery, '_scope_empty', return_value=True),
                      mock.patch.object(module, 'clean_container', return_value=True) as retry_cleanup):
                    recover(path)
                    self.assertIsNone(store.reservation())
                    self.assertEqual(retry_cleanup.call_count, 2)

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
