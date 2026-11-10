"""Mocked Podman wire/identity tests; no supported-host settlement evidence."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mizu import delegation_podman as podman
from mizu.config import Limits, Sandbox
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.process import Result
from mizu.sandbox import runtime_base, runtime_env


@unittest.skipUnless(sys.platform == 'linux', 'Linux no-follow O_PATH identities')
class PodmanIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.file('bin/podman', executable=True)
        self.helper = self.file('bin/helper', executable=True)
        self.oci = self.file('bin/crun', executable=True)
        self.conmon = self.file('bin/conmon', executable=True)
        self.storage_config = self.file('config/storage.conf')
        self.graph = self.root / 'graph'
        self.runroot = self.root / 'run'
        self.graph.mkdir()
        self.runroot.mkdir()
        self.config = SimpleNamespace(
            sandbox=Sandbox(executable=str(self.executable), cleanup_timeout_seconds=19),
            limits=Limits(output_bytes=32768))
        self.environment = mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin'),
                                                      'HOME': str(self.root), 'LANG': 'C.UTF-8'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.info = {
            'version': {'Version': '5.8.4', 'Built': 1},
            'host': {'serviceIsRemote': False, 'security': {'rootless': True},
                     'cgroupManager': 'systemd', 'cgroupVersion': 'v2',
                     'idMappings': {'uidmap': [{'container_id': 0, 'host_id': 1000, 'size': 1},
                                               {'container_id': 1, 'host_id': 100000, 'size': 65536}],
                                    'gidmap': [{'container_id': 0, 'host_id': 1000, 'size': 1}]},
                     'ociRuntime': {'name': 'crun', 'path': str(self.oci), 'version': 'crun version 1.20\n+SYSTEMD'},
                     'conmon': {'path': str(self.conmon), 'version': 'conmon version 2.1.12'},
                     'uptime': '1s'},
            'store': {'configFile': str(self.storage_config), 'graphRoot': str(self.graph),
                      'runRoot': str(self.runroot), 'graphDriverName': 'overlay',
                      'graphOptions': {'overlay.mountopt': 'nodev',
                                       'overlay.mount_program': {'Executable': str(self.oci), 'Version': 'fixture'}},
                      'containerStore': {'number': 1}}}

    def file(self, name, *, executable=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture\n')
        path.chmod(0o700 if executable else 0o600)
        return path

    def result(self, info=None):
        return Result(0, json.dumps(self.info if info is None else info), '', 'exited', 0)

    def capture(self, info=None):
        with mock.patch('mizu.process.run', return_value=self.result(info)) as run:
            pin = podman.capture(self.config)
        return pin, run

    def revalidate(self, pin, info=None):
        with mock.patch('mizu.process.run', return_value=self.result(info)) as run:
            current = podman.revalidate(self.config, pin)
        return current, run

    def test_exact_local_argv_limits_environment_and_serializable_non_releasing_pin(self):
        pin, run = self.capture()
        run.assert_called_once_with([*runtime_base(self.config), '--remote=false', 'info', '--format=json'],
                                    timeout=19, maximum=32768, env=runtime_env(self.config))
        self.assertEqual(json.loads(canonical(pin)), pin)
        self.assertIs(pin['release_authority'], False)
        self.assertEqual(pin['uid'], os.getuid())
        self.assertEqual(pin['argv'], [str(self.executable), '--remote=false', 'info', '--format=json'])
        self.assertEqual(pin['runtime']['path'], str(self.executable))
        self.assertEqual(len(pin['environment_sha256']), 64)
        self.assertNotIn('environment', pin)
        self.assertEqual(self.revalidate(pin)[0], pin)
        # Volatile/unselected info and JSON/key/mapping order have no authority.
        info = deepcopy(self.info)
        info['version']['Built'] += 1
        info['host']['uptime'] = '2s'
        info['store']['containerStore']['number'] += 1
        info['host']['idMappings']['uidmap'].reverse()
        with mock.patch('mizu.process.run', return_value=replace(self.result(info), stdout=json.dumps(info, sort_keys=True))):
            self.assertEqual(podman.revalidate(self.config, pin), pin)

    def test_store_directory_activity_is_stable_but_replacement_is_not(self):
        pin, _ = self.capture()
        (self.graph / 'activity').write_text('new container metadata')
        (self.runroot / 'activity').mkdir()
        self.assertEqual(self.revalidate(pin)[0], pin)
        self.graph.rename(self.root / 'old-graph')
        self.graph.mkdir()
        with self.assertRaises(Denied):
            self.revalidate(pin)

    def test_every_selected_wire_fact_change_refuses_revalidation(self):
        pin, _ = self.capture()
        changes = [(('version', 'Version'), '5.8.5'),
                   (('host', 'serviceIsRemote'), True), (('host', 'security', 'rootless'), False),
                   (('host', 'cgroupManager'), 'cgroupfs'), (('host', 'cgroupVersion'), 'v1'),
                   (('host', 'idMappings', 'gidmap'), [{'container_id': 0, 'host_id': 2000, 'size': 1}]),
                   (('host', 'ociRuntime', 'name'), 'runc'),
                   (('host', 'ociRuntime', 'version'), 'crun version 1.21'),
                   (('host', 'conmon', 'version'), 'conmon version 2.2'),
                   (('store', 'graphDriverName'), 'vfs'), (('store', 'graphOptions'), {}),
                   (('store', 'graphRoot'), str(self.runroot)), (('store', 'runRoot'), str(self.graph)),
                   (('store', 'configFile'), str(self.file('config/other.conf'))),
                   (('host', 'ociRuntime', 'path'), str(self.conmon)),
                   (('host', 'conmon', 'path'), str(self.oci))]
        for keys, value in changes:
            with self.subTest(keys=keys):
                info = deepcopy(self.info)
                parent = info
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.assertRaises(Denied):
                    self.revalidate(pin, info)

    def test_changed_uid_effective_environment_runtime_and_helper_identities(self):
        self.config.sandbox = replace(self.config.sandbox, namespace_helper=(str(self.helper),))
        pin, run = self.capture()
        self.assertEqual(run.call_args.args[0], [str(self.helper), str(self.executable),
                                                '--remote=false', 'info', '--format=json'])
        self.assertEqual(pin['namespace_helpers'][0]['path'], str(self.helper))
        with mock.patch('mizu.delegation_podman.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(Denied):
                self.revalidate(pin)
        for key in ('PATH', 'HOME', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                    'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LC_ALL'):
            with self.subTest(environment=key), mock.patch.dict(os.environ, {key: '/fixture/changed'}):
                with self.assertRaises(Denied):
                    self.revalidate(pin)
        # The sanitized environment, not irrelevant host variables, is pinned.
        with mock.patch.dict(os.environ, {'CONTAINER_HOST': 'ignored', 'UNRELATED_FIXTURE': 'ignored'}):
            self.assertEqual(self.revalidate(pin)[0], pin)
        for path in (self.executable, self.helper, self.oci, self.conmon, self.storage_config):
            with self.subTest(path=path.name):
                before, _ = self.capture()
                path.write_text('changed fixture contents')
                with self.assertRaises(Denied):
                    self.revalidate(before)
        other = self.file('other/podman', executable=True)
        self.config.sandbox = replace(self.config.sandbox, executable=str(other))
        with self.assertRaises(Denied):
            self.revalidate(pin)
        self.config.sandbox = replace(self.config.sandbox, executable=str(self.executable), namespace_helper=())
        with self.assertRaises(Denied):
            self.revalidate(pin)

    def test_invalid_configuration_never_calls_podman(self):
        original = self.config.sandbox
        cases = [replace(original, mode='single'), replace(original, mode='rootful'),
                 replace(original, executable='docker'), replace(original, executable='podman-remote'),
                 replace(original, executable=''), replace(original, executable='relative/podman'),
                 replace(original, executable=str(self.root / 'missing' / 'podman')),
                 replace(original, executable=str(self.executable.parent) + '/./podman'),
                 replace(original, namespace_helper=(str(self.root / 'missing-helper'),))]
        cases += [replace(original, cleanup_timeout_seconds=value) for value in (0, -1, True, 1.5, '1')]
        with mock.patch('mizu.process.run') as run:
            for sandbox in cases:
                with self.subTest(sandbox=sandbox):
                    self.config.sandbox = sandbox
                    with self.assertRaises(Denied):
                        podman.capture(self.config)
            self.config.sandbox = original
            for maximum in (0, -1, True, 1.5, '1'):
                self.config.limits = SimpleNamespace(output_bytes=maximum)
                with self.assertRaises(Denied):
                    podman.capture(self.config)
            self.config.limits = Limits()
            with mock.patch('mizu.delegation_podman.sys.platform', 'darwin'):
                with self.assertRaises(Denied):
                    podman.capture(self.config)
            run.assert_not_called()

    def test_bare_executable_uses_only_safe_path_and_pins_resolution(self):
        self.config.sandbox = replace(self.config.sandbox, executable='podman')
        # runtime_env normally inserts '.', which is intentionally ambiguous.
        with mock.patch('mizu.process.run') as run:
            with self.assertRaises(Denied):
                podman.capture(self.config)
            run.assert_not_called()
        env = {'PATH': str(self.root / 'bin')}
        with mock.patch('mizu.delegation_podman.runtime_env', return_value=env):
            pin, _ = self.capture()
            self.assertEqual(pin['runtime']['path'], str(self.executable))
            self.assertEqual(pin['argv'][0], 'podman')

    def test_unsafe_and_missing_paths_refuse_without_following_links(self):
        link = self.root / 'linked-graph'
        link.symlink_to(self.graph, target_is_directory=True)
        parent_link = self.root / 'linked-config'
        parent_link.symlink_to(self.storage_config.parent, target_is_directory=True)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        paths = ['', '/', 'relative', str(self.root) + '//graph', str(self.root) + '/./graph',
                 str(self.root) + '/graph/../graph', str(self.graph) + '/', str(self.root / 'absent'),
                 str(link), str(self.storage_config), str(fifo), str(self.graph) + '\x00',
                 str(self.graph) + '\n', str(self.graph) + '\ufffd']
        for path in paths:
            with self.subTest(path=repr(path)):
                info = deepcopy(self.info)
                info['store']['graphRoot'] = path
                with self.assertRaises(Denied):
                    self.capture(info)
        info = deepcopy(self.info)
        info['store']['configFile'] = str(parent_link / 'storage.conf')
        with self.assertRaises(Denied):
            self.capture(info)
        # A configured runtime symlink is rejected before executing any command.
        self.executable.rename(self.root / 'real-podman')
        self.executable.symlink_to(self.root / 'real-podman')
        with mock.patch('mizu.process.run') as run:
            with self.assertRaises(Denied):
                podman.capture(self.config)
            run.assert_not_called()

    def test_nonexecutable_or_wrong_file_types_refuse(self):
        for path in (self.executable, self.oci, self.conmon):
            path.chmod(0o600)
            with self.assertRaises(Denied):
                self.capture()
            path.chmod(0o700)
        info = deepcopy(self.info)
        info['store']['configFile'] = str(self.graph)
        with self.assertRaises(Denied):
            self.capture(info)

    def test_failure_results_and_incomplete_or_ambiguous_json_are_not_evidence(self):
        good = self.result()
        cases = [replace(good, reason=reason) for reason in ('timeout', 'output_limit', 'cancelled', 'pipe_error')]
        cases += [replace(good, exit_code=code) for code in (1, None, False)]
        cases += [replace(good, survivors=('mizu-output',)), replace(good, stderr='warning'),
                  replace(good, stdout=good.stdout[:-1]), replace(good, stdout=good.stdout + '{}'),
                  replace(good, stdout=''), replace(good, stdout='[]'), replace(good, stdout='null'),
                  replace(good, stdout=good.stdout.replace('"serviceIsRemote": false',
                                                          '"serviceIsRemote": true, "serviceIsRemote": false')),
                  replace(good, stdout=good.stdout.replace('"Built": 1', '"Built": NaN'))]
        for result in cases:
            with self.subTest(result=result.reason, code=result.exit_code), \
                    mock.patch('mizu.process.run', return_value=result):
                with self.assertRaises(Denied):
                    podman.capture(self.config)
        self.config.limits = Limits(output_bytes=len(good.stdout.encode()) - 1)
        with mock.patch('mizu.process.run', return_value=good):
            with self.assertRaises(Denied):
                podman.capture(self.config)
        with mock.patch('mizu.process.run', side_effect=OSError('fixture failure')):
            with self.assertRaises(Denied):
                podman.capture(self.config)

    def test_missing_or_malformed_required_identity_fields_refuse(self):
        keys = [('version', 'Version'), ('host', 'serviceIsRemote'), ('host', 'security', 'rootless'),
                ('host', 'cgroupManager'), ('host', 'cgroupVersion'), ('host', 'idMappings'),
                ('host', 'ociRuntime', 'path'), ('host', 'ociRuntime', 'name'), ('host', 'ociRuntime', 'version'),
                ('host', 'conmon', 'path'), ('host', 'conmon', 'version'),
                *[('store', key) for key in ('configFile', 'graphRoot', 'runRoot', 'graphDriverName', 'graphOptions')]]
        for path in keys:
            for value in (None, False, 0, [], '', ' '):
                if path == ('host', 'serviceIsRemote') and value is False:
                    continue
                with self.subTest(path=path, value=value):
                    info = deepcopy(self.info)
                    parent = info
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = value
                    with self.assertRaises(Denied):
                        self.capture(info)
            info = deepcopy(self.info)
            parent = info
            for key in path[:-1]:
                parent = parent[key]
            del parent[path[-1]]
            with self.assertRaises(Denied):
                self.capture(info)
        for version in ('5', 'v5.8.4', '5.8.4 ', '05.8.4', '5.8.4-..', '5.8.4-01'):
            info = deepcopy(self.info)
            info['version']['Version'] = version
            with self.assertRaises(Denied):
                self.capture(info)
        info = deepcopy(self.info)
        info['version']['Version'] = '6.0.0-dev.1+build.2'
        self.assertEqual(self.capture(info)[0]['version'], '6.0.0-dev.1+build.2')

    def test_mapping_and_options_malformed_values_refuse(self):
        bad_rows = [[], [{}], [{'container_id': 0, 'host_id': True, 'size': 1}],
                    [{'container_id': -1, 'host_id': 1000, 'size': 1}],
                    [{'container_id': 0, 'host_id': 1000, 'size': 0}],
                    [{'container_id': 0, 'host_id': 2**32 - 1, 'size': 2}],
                    [{'container_id': 0, 'host_id': 1000, 'size': 2},
                     {'container_id': 1, 'host_id': 2000, 'size': 1}],
                    [{'container_id': 0, 'host_id': 1000, 'size': 2},
                     {'container_id': 2, 'host_id': 1001, 'size': 1}]]
        for rows in bad_rows:
            info = deepcopy(self.info)
            info['host']['idMappings']['uidmap'] = rows
            with self.assertRaises(Denied):
                self.capture(info)
        for options in ({'key': []}, {'key': {}}, {'key': {'Version': None}}, {'': 'value'}):
            info = deepcopy(self.info)
            info['store']['graphOptions'] = options
            with self.assertRaises(Denied):
                self.capture(info)

    def test_runtime_change_during_query_and_forged_pin_refuse(self):
        def query(*args, **kwargs):
            self.executable.write_text('changed during read')
            return self.result()
        with mock.patch('mizu.process.run', side_effect=query):
            with self.assertRaises(Denied):
                podman.capture(self.config)
        pin, _ = self.capture()
        for mutate in (lambda p: p.pop('store'), lambda p: p.update(extra=True),
                       lambda p: p.update(uid=True), lambda p: p.update(schema=True),
                       lambda p: p.update(release_authority=True)):
            changed = deepcopy(pin)
            mutate(changed)
            with self.assertRaises(Denied):
                self.revalidate(changed)
        for invalid in (None, [], {'schema': 1, 'release_authority': False, 'bad': float('nan')}):
            with self.assertRaises(Denied):
                self.revalidate(invalid)


if __name__ == '__main__':
    unittest.main()
