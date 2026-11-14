"""Offline evidence fixtures, not a host systemd/cgroup integration proof."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mizu.delegation_scope import _scope_empty
from mizu.delegation_jobs import user_cgroup
from mizu.errors import Denied
from mizu.process import Result


@unittest.skipUnless(sys.platform == 'linux', 'Linux cgroup read-only proof')
class ScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_id = 'a' * 32
        self.unit = 'mizu-delegation-' + self.run_id + '.service'
        self.group = user_cgroup(self.unit)
        self.binding = {'run_id': self.run_id, 'directory': {
            'path': '/fixture/run', 'device': 1, 'inode': 1},
            'scope': {'unit': self.unit, 'cgroup': self.group}, 'containers': []}
        (self.root / 'cgroup.controllers').write_text('cpu memory pids\n')
        self.path = self.root / self.group[1:]
        self.path.mkdir(parents=True)
        (self.path / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        (self.path / 'cgroup.procs').write_text('')
        self.query = mock.Mock(return_value=self.state())

    def state(self, load='loaded', active='inactive', group=None):
        return Result(0, f'LoadState={load}\nActiveState={active}\n'
                      f'ControlGroup={self.group if group is None else group}\n', '', 'exited', 0)

    def proof(self):
        return _scope_empty(self.binding, query=self.query, maximum=8192, cgroup_root=self.root)

    def test_exact_identity_empty_scope_and_read_only_boundary(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        binding = copy.deepcopy(self.binding)
        with mock.patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            self.assertTrue(self.proof())
        self.query.assert_called_once_with(self.unit)
        self.assertEqual(self.binding, binding)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_wrong_run_unit_or_cgroup_never_queries(self):
        saved = copy.deepcopy(self.binding)
        for change in ('run_id', 'unit', 'cgroup', 'missing'):
            with self.subTest(change=change):
                self.binding = copy.deepcopy(saved)
                if change == 'run_id':
                    self.binding['run_id'] = 'b' * 32
                elif change == 'missing':
                    del self.binding['scope']
                else:
                    self.binding['scope'][change] = 'unrelated.service'
                    if change == 'unit':
                        self.binding['scope']['cgroup'] = '/system.slice/unrelated.service'
                with self.assertRaises(Denied):
                    self.proof()
                self.query.assert_not_called()

    def test_system_manager_wrong_uid_and_wrong_unit_bindings_refuse_before_query(self):
        for group in ('/system.slice/' + self.unit,
                      self.group.replace(f'user-{os.getuid()}.slice', f'user-{os.getuid()+1}.slice'),
                      self.group.replace(f'user@{os.getuid()}.service', f'user@{os.getuid()+1}.service'),
                      self.group.replace(self.unit, 'other.service')):
            with self.subTest(group=group), self.assertRaises(Denied):
                self.binding['scope']['cgroup'] = group
                self.proof()
            self.query.assert_not_called()

    def test_active_failed_descendant_or_direct_process_is_not_empty(self):
        for active, events, procs in (
                ('active', 'populated 0\n', ''), ('failed', 'populated 0\n', ''),
                ('inactive', 'populated 1\n', ''),  # descendant only
                ('inactive', 'populated 0\n', '123\n')):
            with self.subTest(active=active, events=events, procs=procs):
                self.query.return_value = self.state(active=active)
                (self.path / 'cgroup.events').write_text(events)
                (self.path / 'cgroup.procs').write_text(procs)
                self.assertFalse(self.proof())

    def test_absence_requires_observable_v2_parent_and_consistent_systemd(self):
        for p in self.path.iterdir():
            p.unlink()
        self.path.rmdir()
        with self.assertRaises(Denied):
            self.proof()  # systemd still reports this missing group
        for load in ('loaded', 'not-found'):
            self.query.return_value = self.state(load=load, group='')
            self.assertTrue(self.proof())
        self.path.parent.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.proof()  # absent ancestor is not exact-group ENOENT
        self.path.parent.mkdir()
        (self.root / 'cgroup.controllers').unlink()
        with self.assertRaises(FileNotFoundError):
            self.proof()

    def test_present_group_requires_exact_controlgroup_even_if_empty(self):
        for load in ('loaded', 'not-found'):
            self.query.return_value = self.state(load=load, group='')
            with self.assertRaises(Denied):
                self.proof()

    def test_missing_symlinked_nonregular_and_malformed_files_fail_closed(self):
        for path in (self.root / 'cgroup.controllers', self.path / 'cgroup.events',
                     self.path / 'cgroup.procs'):
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    self.proof()
                path.symlink_to(self.root / 'missing')  # dangling links are not ENOENT proof
                with self.assertRaises(OSError):
                    self.proof()
                path.unlink()
                os.mkfifo(path)
                with self.assertRaises(Denied):
                    self.proof()  # must not block opening nonregular evidence
                path.unlink()
                path.write_bytes(original)
        for name, contents in (
                ('cgroup.events', b''), ('cgroup.events', b'populated 2\n'),
                ('cgroup.events', b'populated 0\npopulated 0\n'),
                ('cgroup.events', b'populated 0 extra\n'), ('cgroup.events', b'\xff'),
                ('cgroup.procs', b'not-a-pid\n'), ('cgroup.procs', b'\n'),
                ('cgroup.procs', b'1' * 8193)):
            path = self.path / name
            original = path.read_bytes()
            with self.subTest(name=name, contents=contents[:40]):
                path.write_bytes(contents)
                with self.assertRaises((Denied, UnicodeError)):
                    self.proof()
                path.write_bytes(original)
        (self.root / 'cgroup.controllers').write_text('')
        with self.assertRaises(Denied):
            self.proof()

    def test_symlinked_scope_parent_and_root_fail_closed(self):
        for path in (self.path, *self.path.parents[:4], self.root):
            with self.subTest(path=path.name):
                moved = path.with_name(path.name + '-saved')
                path.rename(moved)
                try:
                    path.symlink_to(moved, target_is_directory=True)
                    with self.assertRaises(OSError):
                        self.proof()
                finally:
                    path.unlink()
                    moved.rename(path)

    def test_unavailable_permission_denied_timeout_and_systemctl_mismatch(self):
        for error in (FileNotFoundError(), PermissionError(), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.query.side_effect = error
                with self.assertRaises(type(error)):
                    self.proof()
        self.query.side_effect = None
        for result in (
                Result(None, '', '', 'timeout', 1), Result(1, '', '', 'exited', 0),
                Result(1, self.state().stdout, '', 'exited', 0),
                Result(0, self.state().stdout, '', 'exited', 0, ('reader',)),
                self.state(group='/system.slice/other.service'), self.state(active='unknown'),
                self.state(load='not-found'), self.state(load='error'),
                Result(0, self.state().stdout + 'LoadState=loaded\n', '', 'exited', 0),
                Result(0, 'x' * 8193, '', 'exited', 0)):
            with self.subTest(result=result):
                self.query.return_value = result
                with self.assertRaises(Denied):
                    self.proof()
        self.query.return_value = self.state()
        real_open = os.open
        def denied(name, flags, **kwargs):
            if name == 'cgroup.events':
                raise PermissionError('unobservable')
            return real_open(name, flags, **kwargs)
        with mock.patch('mizu.delegation_scope.os.open', side_effect=denied):
            with self.assertRaises(PermissionError):
                self.proof()

    def test_systemd_properties_must_be_complete_and_stderr_free(self):
        complete = self.state().stdout
        for result in (Result(0, complete[:-1], '', 'exited', 0),
                       Result(0, complete, 'warning', 'exited', 0),
                       Result(0, complete.replace('\n', '\r\n'), '', 'exited', 0)):
            with self.subTest(result=result), mock.patch.object(self, 'query', return_value=result):
                with self.assertRaises(Denied):
                    self.proof()

    def test_non_linux_remains_unproven(self):
        with mock.patch('mizu.delegation_scope.sys.platform', 'darwin'):
            with self.assertRaises(Denied):
                self.proof()
        self.query.assert_not_called()


if __name__ == '__main__':
    unittest.main()
