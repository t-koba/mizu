"""Offline systemctl wire/command fixtures, not proof of host settlement."""
from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import sys
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mizu import delegation_systemd as systemd
from mizu.errors import Denied
from mizu.process import Result


class SystemdObservationTests(unittest.TestCase):
    def setUp(self):
        # Synthetic UID on every platform; no host-manager claim.
        uid = mock.patch.object(os, 'getuid', return_value=1000, create=True)
        uid.start()
        self.addCleanup(uid.stop)
        self.run_id = 'a1' * 16
        self.unit = f'mizu-delegation-{self.run_id}.service'
        self.config = SimpleNamespace(services=SimpleNamespace(systemd_verify_timeout_seconds=17),
                                      limits=SimpleNamespace(output_bytes=2048))
        self.fields = {'Id': self.unit, 'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
                       'ControlGroup': systemd.user_cgroup(self.unit),
                       'InvocationID': 'b' * 32}
        self.version = 'Version=255.4-1ubuntu8.4\n'
        self.owner = 's ":1.42"\n'
        self.endpoint = (os.getuid(), '11111111-2222-3333-4444-555555555555',
                         '/fixture/runtime/bus', ((1, 2, os.getuid(), os.getuid(), 0o140600, 3),))
        patcher = mock.patch.object(systemd, '_endpoint', return_value=self.endpoint)
        self.endpoint_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def result(self, text):
        return Result(0, text, '', 'exited', 0)

    def properties(self, **changes):
        return ''.join(f'{k}={v}\n' for k, v in {**self.fields, **changes}.items())

    def outputs(self, jobs=''):
        return [self.result(self.owner), self.result(self.version), self.result(self.owner),
                self.result(self.properties()), self.result(jobs), self.result(self.owner),
                self.result(self.version), self.result(self.owner)]

    def observe(self, outputs=None):
        with mock.patch('mizu.process.run', side_effect=self.outputs() if outputs is None else outputs) as run:
            observation = systemd.observe(self.config, self.run_id)
        return observation, run

    def test_exact_argv_bounds_environment_and_immutable_point_in_time_result(self):
        jobs = f'  71 {self.unit} start waiting\n72 {self.unit} stop running\n'
        with mock.patch.dict(os.environ, {'PATH': '/fixture/bin', 'HOME': '/fixture/home',
                                         'XDG_RUNTIME_DIR': '/fixture/runtime',
                                         'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/fixture/runtime/bus',
                                         'LANG': 'wrong', 'LC_ALL': 'wrong',
                                         'SYSTEMD_HOST': 'untrusted', 'UNRELATED_SECRET': 'excluded'}, clear=True):
            observation, run = self.observe(self.outputs(jobs))
        prefix = ['systemctl', '--user', '--no-pager']
        env = {'PATH': '/fixture/bin', 'HOME': '/fixture/home', 'XDG_RUNTIME_DIR': '/fixture/runtime',
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/fixture/runtime/bus',
               'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8',
               'SYSTEMD_COLORS': '0', 'SYSTEMD_URLIFY': '0'}
        kwargs = {'timeout': 17, 'maximum': 2048, 'env': env}
        owner_call = mock.call(['busctl', '--no-pager', '--address=unix:path=/fixture/runtime/bus',
                                'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                'org.freedesktop.DBus', 'GetNameOwner', 's', 'org.freedesktop.systemd1'], **kwargs)
        self.assertEqual(run.call_args_list, [
            owner_call,
            mock.call(prefix + ['show', '--all', '--property=Version'], **kwargs),
            owner_call,
            mock.call(prefix + ['show', '--all',
                                '--property=Id,LoadState,ActiveState,SubState,ControlGroup,InvocationID',
                                self.unit], **kwargs),
            mock.call(prefix + ['list-jobs', '--no-legend', '--plain', '--full', self.unit], **kwargs),
            owner_call,
            mock.call(prefix + ['show', '--all', '--property=Version'], **kwargs),
            owner_call])
        self.assertEqual(observation.unit, self.unit)
        self.assertEqual(observation.manager.name_owner, ":1.42")
        self.assertEqual(observation.manager.socket_path, self.endpoint[2])
        self.assertEqual(observation.manager_version, '255.4-1ubuntu8.4')
        self.assertEqual((observation.load_state, observation.active_state, observation.sub_state),
                         ('loaded', 'active', 'running'))
        self.assertEqual(observation.control_group, self.fields['ControlGroup'])
        self.assertEqual(observation.invocation_id, 'b' * 32)
        self.assertEqual(observation.jobs, (systemd.PendingJob(71, self.unit, 'start', 'waiting'),
                                           systemd.PendingJob(72, self.unit, 'stop', 'running')))
        self.assertTrue(observation.point_in_time)
        self.assertFalse(observation.release_authority)
        with self.assertRaises(FrozenInstanceError):
            observation.release_authority = True
        with self.assertRaises(FrozenInstanceError):
            observation.jobs[0].state = 'done'

    def test_explicit_empty_queue_and_absent_unit_have_no_release_authority(self):
        for properties in (self.properties(), self.properties(LoadState='not-found', ActiveState='inactive',
                                                              SubState='dead', ControlGroup='', InvocationID='')):
            with self.subTest(properties=properties):
                outputs = self.outputs()
                outputs[3] = self.result(properties)
                observation, _ = self.observe(outputs)
                self.assertEqual(observation.jobs, ())
                self.assertTrue(observation.point_in_time)
                self.assertFalse(observation.release_authority)

    def test_invalid_identity_and_bounds_never_invoke_process(self):
        with mock.patch('mizu.process.run') as run:
            for identity in (None, 1, '', 'A' * 32, 'a' * 31, 'a' * 33, self.run_id + '\n',
                             '../' + self.run_id, self.unit, '*' * 32):
                with self.subTest(identity=identity), self.assertRaises(Denied):
                    systemd.observe(self.config, identity)
            for target, key in ((self.config.services, 'systemd_verify_timeout_seconds'),
                                (self.config.limits, 'output_bytes')):
                original = getattr(target, key)
                for invalid in (0, -1, True, None, 1.5):
                    setattr(target, key, invalid)
                    with self.subTest(key=key, invalid=invalid), self.assertRaises(Denied):
                        systemd.observe(self.config, self.run_id)
                setattr(target, key, original)
            run.assert_not_called()

    def test_unknown_or_malformed_manager_version_stops_before_unit_read(self):
        for text in ('', 'Version=\n', 'Version=v255\n', 'Version=systemd\n', 'Version=abc\n',
                     'Version=255x\n', 'Version= 255\n', 'Version=255 \n', 'Version=.255\n',
                     'Version=255.\n', 'Version=255-\n', 'Version=-1\n', 'Version=--foo\n',
                     'Version=255\nVersion=255\n', 'Version=255\nExtra=yes\n', 'Version=255',
                     'Version=255\r\n', 'Version=255\x00\n', 'systemd 255\n', 'Version=v\n'):
            with self.subTest(text=text), mock.patch('mizu.process.run',
                    side_effect=[self.result(self.owner), self.result(text)]) as run:
                with self.assertRaises(Denied):
                    systemd.observe(self.config, self.run_id)
                self.assertEqual(run.call_count, 2)
        for version in ('253', '255', '256', '252', '999', '253.1', '255.4', '256.7-1ubuntu1',
                        '255.4.1', '255.4-1ubuntu8.4'):
            with self.subTest(version=version):
                outputs = self.outputs()
                outputs[1] = self.result(f'Version={version}\n')
                outputs[6] = self.result(f'Version={version}\n')
                observation, _ = self.observe(outputs)
                self.assertEqual(observation.manager_version, version)

    def test_incomplete_duplicate_unknown_and_malformed_unit_fields(self):
        bad = ['', self.properties()[:-1], self.properties() + 'ActiveState=active\n',
               self.properties(Id='other.service'),
               self.properties() + 'Id=other.service\n', self.properties().replace('SubState=running\n', ''),
               self.properties().replace('ActiveState=active', 'ActiveState'),
               self.properties(LoadState='unknown'), self.properties(ActiveState='unknown'),
               self.properties(SubState='unknown'), self.properties(InvocationID='b' * 31),
               self.properties(InvocationID='B' * 32), self.properties(ControlGroup='/'),
               self.properties(ControlGroup='relative'), self.properties(ControlGroup='/app.slice/../wrong'),
               self.properties(ControlGroup='/app.slice//wrong'), self.properties(ControlGroup='/app.slice/with space'),
               self.properties(ControlGroup='/app.slice/\x1bwrong'), self.properties(ControlGroup='/app.slice/\ufffd'),
               self.properties(LoadState='not-found'),
               self.properties(LoadState='not-found', ActiveState='inactive', SubState='dead', ControlGroup='')]
        for text in bad:
            with self.subTest(text=text), mock.patch('mizu.process.run',
                    side_effect=[self.result(self.owner), self.result(self.version),
                                 self.result(self.owner), self.result(text)]) as run:
                with self.assertRaises(Denied):
                    systemd.observe(self.config, self.run_id)
                self.assertEqual(run.call_count, 4)

    def test_job_rows_require_exact_unit_known_vocabulary_and_complete_response(self):
        valid = f'71 {self.unit} start waiting\n'
        bad = ['\n', ' \n', 'No jobs running.\n', '0 jobs listed.\n', 'JOB UNIT TYPE STATE\n',
               valid[:-1], valid + '\n', valid + valid, valid.replace(self.unit, 'other.service'),
               valid.replace(self.unit, self.unit + '.extra'), valid.replace('start', 'unknown'),
               valid.replace('waiting', 'done'), valid.replace('waiting', 'waiting extra'),
               valid.replace('71', '0'), valid.replace('71', '01'), valid.replace('71', '4294967296'),
               valid.replace(' start', '\tstart'), valid.replace('\n', '\r\n'),
               valid + '72 other.service stop running\n']
        for text in bad:
            with self.subTest(text=text), self.assertRaises(Denied):
                self.observe(self.outputs(text))

    def test_process_failure_at_every_read_never_returns_partial_observation(self):
        failures = [replace(self.result(''), reason='timeout'), replace(self.result(''), reason='output_limit'),
                    replace(self.result(''), reason='cancelled'), replace(self.result(''), exit_code=1),
                    replace(self.result(''), exit_code=None), replace(self.result(''), exit_code=False),
                    replace(self.result(''), survivors=('mizu-stdout',)),
                    replace(self.result(''), stderr='diagnostic'), self.result('x' * 2049),
                    self.result('\u00e9' * 1025)]
        for phase in range(8):
            for failure in failures:
                outputs = self.outputs()
                outputs[phase] = failure
                with self.subTest(phase=phase, failure=failure), mock.patch('mizu.process.run',
                                                                         side_effect=outputs) as run:
                    with self.assertRaises(Denied):
                        systemd.observe(self.config, self.run_id)
                    self.assertEqual(run.call_count, phase + 1)
            outputs = self.outputs()[:phase] + [OSError('unavailable')]
            with self.subTest(phase=phase, failure='spawn'), mock.patch('mizu.process.run', side_effect=outputs):
                with self.assertRaises(Denied):
                    systemd.observe(self.config, self.run_id)

    def test_manager_changes_or_unknown_owner_never_return_observation(self):
        for index, value in ((6, 'Version=256\n'), (7, 's ":1.43"\n'),
                             (0, ''), (0, 's ":' + '1' * 254 + '.1"\n'),
                             (7, 's "org.freedesktop.systemd1"\n')):
            outputs = self.outputs()
            outputs[index] = self.result(value)
            with self.subTest(index=index, value=value), self.assertRaises(Denied):
                self.observe(outputs)
        for field, value in enumerate((1001, 'changed-boot', '/fixture/other-bus', ((9, 9),))):
            for index in range(1, 4):
                endpoints = [self.endpoint] * 4
                changed = list(self.endpoint)
                changed[field] = value
                endpoints[index] = tuple(changed)
                self.endpoint_mock.side_effect = endpoints
                with self.subTest(field=field, endpoint_read=index), self.assertRaises(Denied):
                    self.observe()
        self.endpoint_mock.side_effect = None
        # A manager restart between the owner query and version read must not
        # pair one manager's version with another manager's owner.
        changed_owner = self.outputs()
        changed_owner[2] = self.result('s ":1.43"\n')
        with self.assertRaises(Denied):
            self.observe(changed_owner)
        # Mismatch across stop observations is refused too, without ID mutation.
        outputs = self.outputs() + [self.result('')] + self.outputs()
        outputs[8 + 5] = self.result('s ":1.43"\n')
        outputs[8 + 7] = self.result('s ":1.43"\n')
        with self.assertRaises(Denied), mock.patch('mizu.process.run', side_effect=outputs) as run:
            systemd._cancel_stop(self.config, self.run_id)
        self.assertFalse(any('cancel' in arg.lower() for call in run.call_args_list for arg in call.args[0]))

    def test_exact_current_uid_cgroup_required(self):
        group = self.fields['ControlGroup']
        for wrong in ('/system.slice/' + self.unit, group.replace(self.unit, 'other.service'),
                      group.replace(f'user-{os.getuid()}.slice', f'user-{os.getuid()+1}.slice'),
                      group.replace(f'user@{os.getuid()}.service', f'user@{os.getuid()+1}.service')):
            with self.subTest(group=wrong), self.assertRaises(Denied):
                self.observe([*self.outputs()[:2], self.result(self.properties(ControlGroup=wrong))])

    def test_pending_jobs_stop_exact_unit_synchronously_and_reobserve(self):
        # Pending jobs are diagnostic only: even with a diagnostic manager pin,
        # mutate no manager-local IDs. A synchronous replace-stop targets only
        # the exact run-derived unit, then the queue is re-observed.
        jobs = (f'71 {self.unit} start waiting\n'
                f'72 {self.unit} stop running\n'
                f'73 {self.unit} start running\n')
        remaining = f'74 {self.unit} start running\n'
        outputs = self.outputs(jobs) + [self.result('')] + self.outputs(remaining)
        with mock.patch('mizu.process.run', side_effect=outputs) as run:
            diagnostic = systemd._cancel_stop(self.config, self.run_id)
        self.assertEqual(run.call_args_list[8].args[0],
                         ['systemctl', '--user', '--no-pager', '--job-mode=replace', 'stop', self.unit])
        self.assertEqual(run.call_count, 17)
        self.assertFalse(any('cancel' in arg.lower() for call in run.call_args_list for arg in call.args[0]))
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 17)
            self.assertEqual(call.kwargs['maximum'], 2048)
        self.assertEqual(tuple(job.id for job in diagnostic.before.jobs), (71, 72, 73))
        self.assertEqual(diagnostic.after.jobs[0].id, 74)
        self.assertFalse(diagnostic.release_authority)
        with self.assertRaises(FrozenInstanceError):
            diagnostic.release_authority = True
        with self.assertRaises(FrozenInstanceError):
            diagnostic.before.jobs = ()

    def test_unknown_or_mismatched_pending_job_refuses_before_mutation(self):
        for row in (f'71 other.service start waiting\n', f'71 {self.unit} future waiting\n',
                    f'71 {self.unit} start unknown\n'):
            with self.subTest(row=row), mock.patch('mizu.process.run', side_effect=self.outputs(row)) as run:
                with self.assertRaises(Denied):
                    systemd._cancel_stop(self.config, self.run_id)
                self.assertEqual(run.call_count, 5)

    def test_cancel_stop_and_reobservation_failures_never_return_diagnostics(self):
        base = self.outputs(f'71 {self.unit} start waiting\n') + [self.result('')] + self.outputs()
        # Every mutation/re-observation command must complete without partial
        # output, diagnostics, timeout, overflow or surviving I/O workers.
        failures = [self.result('unexpected'), replace(self.result(''), exit_code=1),
                    replace(self.result(''), reason='timeout'),
                    replace(self.result(''), reason='overflow'),
                    replace(self.result(''), survivors=('reader',))]
        for index in range(8, len(base)):
            for failure in failures:
                with self.subTest(index=index, failure=failure), mock.patch('mizu.process.run',
                        side_effect=base[:index] + [failure]) as run:
                    with self.assertRaises(Denied):
                        systemd._cancel_stop(self.config, self.run_id)
                    self.assertEqual(run.call_count, index + 1)


@unittest.skipUnless(sys.platform == 'linux', 'Linux unix socket identity fixtures')
class EndpointParsingTests(unittest.TestCase):
    def test_literal_address_and_documented_private_fallback_only(self):
        self.assertEqual(systemd._socket_path({'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/fixture/bus'}),
                         '/fixture/bus')
        self.assertEqual(systemd._socket_path({'XDG_RUNTIME_DIR': '/fixture/runtime'}),
                         '/fixture/runtime/systemd/private')
        for address in ('', 'unix:abstract=bus', 'tcp:host=localhost', 'unix:path=relative',
                        'unix:path=/bus;unix:path=/other', 'unix:path=/bus,guid=abc',
                        'unix:path=/bus%2fother', 'unix:path=//bus', 'unix:path=/../bus',
                        'unix:path=/bus/./socket', 'unix:path=/bus\n'):
            with self.subTest(address=address), self.assertRaises(Denied):
                systemd._socket_path({'DBUS_SESSION_BUS_ADDRESS': address})
        for runtime in ('', 'relative', '/runtime/', '/runtime/..', '/runtime;other'):
            with self.subTest(runtime=runtime), self.assertRaises(Denied):
                systemd._socket_path({'XDG_RUNTIME_DIR': runtime})

    def test_socket_identity_replacement_symlinks_and_uid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / 'runtime'
            directory.mkdir()
            path = directory / 'bus'
            env = {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(path)}
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path))
                pin = systemd._endpoint(env)
                self.assertEqual(pin, systemd._endpoint(env))
                self.assertEqual(pin[0], os.getuid())
                with mock.patch.object(systemd.os, 'getuid', return_value=os.getuid()+1), self.assertRaises(Denied):
                    systemd._endpoint(env)
                path.rename(directory / 'saved')
                path.symlink_to(directory / 'saved')
                with self.assertRaises(Denied):
                    systemd._endpoint(env)
                path.unlink()
                with socket.socket(socket.AF_UNIX) as replacement:
                    replacement.bind(str(path))
                    self.assertNotEqual(pin, systemd._endpoint(env))
                directory.rename(root / 'saved-runtime')
                directory.symlink_to(root / 'saved-runtime', target_is_directory=True)
                with self.assertRaises(Denied):
                    systemd._endpoint(env)
            with self.assertRaises(Denied):
                systemd._endpoint({'XDG_RUNTIME_DIR': str(root / 'absent')})


if __name__ == '__main__':
    unittest.main()
