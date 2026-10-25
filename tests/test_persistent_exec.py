"""Local shell tests exercise transport only; they are NOT OCI evidence."""
import dataclasses
import os
import re
import shutil
import threading
import unittest
from unittest.mock import patch, Mock

from support import Fixture
from mizu import check_jobs
from mizu.config import load
from mizu.errors import ConfigError, Denied, ProtocolError
from mizu.fs import read_json
from mizu.persistent_exec import ExecSession
from mizu.sandbox_operations import exec_experiment


@unittest.skipUnless(os.name == 'posix' and shutil.which('sh'), 'POSIX shell transport fixture')
class PersistentExecTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.ctx.role = dataclasses.replace(self.ctx.role, persistent_exec=True)
        self.ctx.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(
            self.ctx.config.limits, command_seconds=60))
        self.ctx.sandbox.config = self.ctx.config
        self.removed = Mock(return_value=None)
        self.patches = [patch('mizu.persistent_exec.assert_no_checkpoint'),
                        patch.object(ExecSession, '_remove', self.removed),
                        patch.object(self.ctx.sandbox, 'base', return_value=[]),
                        patch.object(self.ctx.sandbox, 'argv', side_effect=lambda *a, **kw: ['sh', '--rm', '-c', ''])]
        # The transport inserts OCI's -i after 'run'; remove that fake OCI
        # flag in this host-shell-only fixture, preserving production framing.
        from mizu import platform
        spawn = platform.spawn
        self.patches.append(patch('mizu.persistent_exec.platform.spawn',
                                  side_effect=lambda argv, **kw: spawn(['sh', '-s'], **kw)))
        for p in self.patches:
            p.start()
        self.addCleanup(self.close)

    def close(self):
        try:
            # Fault tests may deliberately poison cleanup; clear fixture only.
            self.removed.side_effect = None
            self.removed.return_value = None
            if self.ctx.exec_session is not None:
                self.ctx.exec_session.cleanup_error = None
            self.ctx.close_exec()
        finally:
            for p in reversed(self.patches):
                p.stop()

    def call(self, script):
        return exec_experiment(self.ctx, 'exec', {'script': script})

    def test_state_status_markers_background_and_evidence(self):
        first = self.call("cd /tmp; export MIZU_TEST=value; f() { printf function; }; sleep 100 & bg=$!")
        self.assertEqual(first['exit_code'], 0)
        session = self.ctx.exec_session
        second = self.call('printf "%s:%s:" "$PWD" "$MIZU_TEST"; f; kill -0 "$bg"; false')
        self.assertEqual(second['exit_code'], 1)
        self.assertEqual(second['reason'], 'exited')
        self.assertEqual(second['stdout'], '/tmp:value:function')
        self.assertIs(self.ctx.exec_session, session)
        self.assertFalse(self.removed.called)
        third = self.call("printf '\\036MIZU:ordinary:0\\037'; printf error >&2")
        self.assertEqual(third['stdout'], '\x1eMIZU:ordinary:0\x1f')
        self.assertEqual(third['stderr'], 'error')
        record = read_json(self.ctx.run_dir / third['evidence'])
        self.assertEqual(record['container'], session.name)
        self.assertNotIn('env', record)
        self.ctx.cancel_operations()
        self.assertIsNone(self.ctx.exec_session)
        self.removed.assert_called_once()
        self.assertIsNotNone(session.process.poll())

    def test_command_stdin_is_closed_without_consuming_protocol_frames(self):
        result = self.call('read MIZU_INPUT; read_status=$?; printf "%s:%s" "$read_status" "${MIZU_INPUT-}"')
        self.assertEqual(result['reason'], 'exited')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['stdout'], '1:')
        # The following command proves the shell still received an intact
        # wrapper frame after the command's read saw EOF.
        next_result = self.call('printf next')
        self.assertEqual(next_result['stdout'], 'next')

    def test_timeout_death_cancel_overflow_reset_without_replay(self):
        for script, reason in [('sleep 100', 'timeout'), ('exit 7', 'shell_death'),
                               ('yes output', 'output_limit')]:
            with self.subTest(reason=reason):
                self.call(':')
                self.ctx.sandbox.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(self.ctx.config.limits, command_seconds=1))
                result = self.call(script)
                self.ctx.sandbox.config = self.ctx.config
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['exit_code'])
                self.assertTrue(result['state_lost'])
                self.assertTrue(result['execution_may_be_partial'])
                old = self.ctx.exec_session
                self.assertTrue(old.closed)
                self.assertIsNotNone(old.process.poll())
                result = self.call('printf clean')
                self.assertEqual(result['stdout'], 'clean')
                self.assertIsNot(self.ctx.exec_session, old)
                self.ctx.close_exec()
        self.ctx.stop.set()
        result = self.call('echo must-not-replay')
        self.assertEqual(result['reason'], 'cancelled')
        self.assertTrue(result['state_lost'])

    def test_timeout_cleanup_failure_reports_partial_and_blocks_relaunch(self):
        self.call(':')
        self.ctx.sandbox.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(
            self.ctx.config.limits, command_seconds=1))
        self.removed.side_effect = Denied('refused removal')
        with self.assertRaisesRegex(Denied, 'Shell state lost; execution may have partially occurred; not replayed.*cleanup failed'):
            self.call('sleep 100')
        session = self.ctx.exec_session
        self.assertTrue(session.closed)
        with self.assertRaisesRegex(Denied, 'cleanup failed'):
            self.call('printf must-not-run')
        self.assertEqual(self.removed.call_count, 1)

    def test_inflight_cancellation_and_idle_overflow_invalidate_state(self):
        import shlex
        flag = self.ctx.workspace / 'cancel-flag'
        self.ctx.sandbox.cancel = lambda: flag.exists()
        result = self.call('touch ' + shlex.quote(str(flag)) + '; sleep 100')
        self.assertEqual(result['reason'], 'cancelled')
        self.assertTrue(result['state_lost'])
        flag.unlink()
        self.ctx.sandbox.cancel = self.ctx.tools_cancelled
        release = self.ctx.workspace / 'release-background'
        self.call('(while ! test -f ' + shlex.quote(str(release)) + '; do sleep .01; done; yes idle-output) &')
        session = self.ctx.exec_session
        release.write_text('go')
        self.assertTrue(session.overflow.wait(10), 'background producer must fill the bounded idle buffer')
        result = self.call(':')
        self.assertEqual(result['reason'], 'output_limit')
        self.assertTrue(session.closed)

    def test_output_bound_excludes_completion_frames_and_default_remains_one_shot(self):
        self.ctx.sandbox.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(
            self.ctx.config.limits, output_bytes=1024))
        result = self.call("printf '%01024d' 0")
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(result['stdout']), 1024)
        self.ctx.close_exec()
        self.ctx.role = dataclasses.replace(self.ctx.role, persistent_exec=False)
        with patch.object(self.ctx.sandbox, 'execute', return_value={
                'id': 'a'*32, 'exit_code': 0, 'reason': 'exited'}) as once:
            self.call(':')
            self.call(':')
        self.assertEqual(once.call_count, 2)
        self.assertIsNone(self.ctx.exec_session)

    def test_partial_send_and_malformed_frame_remove(self):
        import mizu.persistent_exec as transport
        original = transport.send_bounded
        def malformed(proc, frame, **kwargs):
            token = re.search(rb'MIZU:([a-f0-9]{32}):', frame)[1]
            original(proc, b"printf '\\036MIZU:" + token + b":bad-status\\037'\n", **kwargs)
        with patch.object(transport, 'send_bounded', side_effect=malformed):
            result = self.call('printf not-sent')
        self.assertEqual(result['reason'], 'malformed_completion')
        self.assertTrue(self.ctx.exec_session.closed)
        with patch.object(transport, 'send_bounded', side_effect=ProtocolError('partial send')):
            result = self.call('printf not-sent')
        self.assertEqual(result['reason'], 'delivery_uncertain')
        self.assertTrue(result['execution_may_be_partial'])
        self.assertEqual(self.removed.call_count, 2)

    def test_final_evidence_failure_resets_and_reports_no_replay(self):
        import shlex
        from pathlib import Path
        import mizu.persistent_exec as transport
        marker = self.ctx.workspace / 'evidence-failure-counter'
        real_write = transport.write_json
        def fail_final(path, value):
            path = Path(path)
            if path.parent.name == 'commands' and not path.name.endswith('.started.json'):
                raise OSError('evidence store unavailable')
            return real_write(path, value)
        with patch.object(transport, 'write_json', side_effect=fail_final):
            with self.assertRaisesRegex(Denied, 'may have executed.*must not be replayed'):
                self.call('echo once >> ' + shlex.quote(str(marker)))
        poisoned = self.ctx.exec_session
        self.assertTrue(poisoned.closed)
        self.assertEqual(marker.read_text(), 'once\n')
        result = self.call('printf clean')
        self.assertEqual(result['stdout'], 'clean')
        self.assertIsNot(self.ctx.exec_session, poisoned)
        self.assertEqual(marker.read_text(), 'once\n')

    def test_missing_completion_and_cleanup_failure_poison_context(self):
        self.call(':')
        session = self.ctx.exec_session
        with self.assertRaisesRegex(Denied, 'updated short state'):
            self.ctx._op_finish({'summary': 'done', 'outcome': 'continue'})
        self.assertIs(self.ctx.exec_session, session)
        self.assertFalse(session.closed)
        self.ctx.sandbox.config = dataclasses.replace(self.ctx.config, limits=dataclasses.replace(self.ctx.config.limits, command_seconds=1))
        result = self.call('exec 1>/dev/null')
        self.ctx.sandbox.config = self.ctx.config
        self.assertEqual(result['reason'], 'timeout')
        self.call(':')
        self.removed.side_effect = Denied('refused removal')
        with self.assertRaisesRegex(Denied, 'cleanup failed'):
            self.ctx.close_exec()
        with self.assertRaisesRegex(Denied, 'cleanup failed'):
            self.call(':')
        with self.assertRaisesRegex(Denied, 'cleanup failed'):
            self.ctx._op_finish({'summary': 'done', 'outcome': 'continue', 'state': 'next'})

    def test_verify_and_check_jobs_are_independent_one_shots(self):
        self.call('export MIZU_TEST=dirty; cd /tmp')
        session = self.ctx.exec_session
        calls = []
        def execute(workspace, script, **kwargs):
            self.assertTrue(session.closed)
            self.assertIsNone(self.ctx.exec_session)
            calls.append((workspace, script, kwargs))
            return {'id': 'a'*32, 'script': script, 'exit_code': 0, 'reason': 'exited'}
        with patch.object(self.ctx.sandbox, 'execute', side_effect=execute):
            result = self.ctx._op_verify({})
        self.assertTrue(result['passed'])
        self.assertEqual(len(calls), len(self.project.verify))
        self.call('export MIZU_TEST=dirty')
        session = self.ctx.exec_session
        self.ctx.role = dataclasses.replace(self.ctx.role, capabilities=(*self.ctx.role.capabilities, 'check_job'))
        self.ctx.config = dataclasses.replace(self.ctx.config, roles={**self.ctx.config.roles, self.ctx.role.name: self.ctx.role})
        self.project.config = self.ctx.config
        job = check_jobs.submit(self.ctx)
        self.assertTrue(session.closed)
        self.assertIsNone(self.ctx.exec_session)
        from test_check_jobs import FakeSandbox
        FakeSandbox.calls = []
        self.assertTrue(check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox))
        self.assertEqual(len(FakeSandbox.calls), len(self.project.verify))
        self.assertNotEqual(FakeSandbox.calls[0][0], self.ctx.workspace)
        self.assertEqual(check_jobs.read(self.project, job['id'])['status'], 'completed')


class PersistentConfigTests(Fixture):
    def test_strict_opt_in_and_grant(self):
        self.assertFalse(self.config.roles['worker'].persistent_exec)
        path = self.file
        original = path.read_text()
        for value in ['"yes"', '1', 'true']:
            # reporter has no exec in default config
            path.write_text(original.replace('[roles.reporter]', '[roles.reporter]\npersistent_exec = ' + value))
            with self.assertRaises(ConfigError):
                load(path)
        for value in ['"yes"', '1']:
            # A role with exec still requires a strict TOML boolean.
            path.write_text(original.replace('[roles.worker]', '[roles.worker]\npersistent_exec = ' + value))
            with self.assertRaises(ConfigError):
                load(path)
        path.write_text(original.replace('[roles.worker]', '[roles.worker]\npersistent_exec = true'))
        self.assertTrue(load(path).roles['worker'].persistent_exec)


class PersistentLifecycleTests(Fixture):
    def test_top_level_failure_and_finish_close_before_publication(self):
        from mizu.runtime import Engine
        from support import ScriptDriver
        for failure in (False, True):
            with self.subTest(failure=failure):
                session = Mock()
                observed = []
                def callback(ctx, *_):
                    ctx.exec_session = session
                    observed.append(ctx)
                    if failure:
                        raise Denied('driver fault')
                    ctx.handle('finish', {'outcome': 'continue', 'summary': 'closed', 'state': 'next'})
                    session.close.assert_called_once()
                engine = Engine(self.config, driver=ScriptDriver(callback))
                if failure:
                    with self.assertRaisesRegex(Denied, 'driver fault'):
                        engine.run(self.project, 'worker')
                else:
                    engine.run(self.project, 'worker')
                session.close.assert_called_once()
                self.assertTrue(observed[0].closed)

    def test_delegated_and_consultation_error_teardowns(self):
        from mizu.runtime import Engine
        from support import ScriptDriver
        from test_delegate import _configured
        config = _configured(self)
        self.project.config = config
        children = []
        def callback(ctx, *_):
            if ctx.role.name == 'worker':
                for op, args in [('consult', {'roles': ['consult'], 'question': 'check'}),
                                 ('delegate', {'role': 'helper', 'task': 'check'})]:
                    result = ctx.handle(op, args)
                    self.assertIsInstance(result, dict)
            else:
                session = Mock()
                ctx.exec_session = session
                children.append((ctx, session))
                raise Denied('child fault')
        Engine(config, driver=ScriptDriver(callback)).run(self.project, 'worker')
        self.assertEqual(len(children), 2)
        for ctx, session in children:
            session.close.assert_called_once()
            self.assertTrue(ctx.closed)

    def test_removal_is_bounded_and_incomplete_observation_fails_closed(self):
        from mizu.process import Result
        session = ExecSession(self.context().sandbox, self.project.workspace, writable=True)
        with patch('mizu.persistent_exec.run', return_value=Result(0, '', '', 'timeout', 0)):
            with self.assertRaises(Denied):
                session._remove()
        for listed in ('', 'container-id'):
            with self.subTest(listed=listed), patch('mizu.persistent_exec.run',
                    return_value=Result(0, listed, '', 'exited', 0)) as run:
                session._remove()
                self.assertEqual(run.call_count, 1 if not listed else 2)
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs['timeout'], 15)
                    self.assertEqual(call.kwargs['maximum'], 8192)

    def test_persistent_launch_reuses_configured_floor_and_shell(self):
        ctx = self.context()
        sb = ctx.sandbox
        session = ExecSession(sb, ctx.workspace, writable=True)
        process = Mock(stdout=None, stderr=None)
        # Interrupt after argv construction: no host shell or OCI invocation.
        with patch('mizu.persistent_exec.assert_no_checkpoint') as inspect, patch(
                'mizu.persistent_exec.platform.spawn', side_effect=OSError('capture argv')) as spawn:
            with self.assertRaises(OSError):
                session._start()
        inspect.assert_called_once()
        actual = spawn.call_args.args[0]
        expected = sb.argv(session.name, ctx.workspace, '', writable=True)
        expected.remove('--rm')
        expected.insert(len(sb.base())+1, '-i')
        expected[-2:] = ['-s']
        self.assertEqual(actual, expected)
        self.assertIn('--read-only', actual)
        self.assertIn('--cap-drop=ALL', actual)
        self.assertIn('--security-opt=no-new-privileges', actual)
        for option in ('--memory', '--memory-swap', '--cpus', '--pids-limit', '--ulimit', '--init'):
            self.assertIn(option, actual)
        self.assertEqual(actual[actual.index('--entrypoint')+1], sb.config.sandbox.entrypoint)
        self.assertEqual(actual[-1], '-s')
        self.assertNotIn('-t', actual)
        self.assertNotIn('--privileged', actual)

    def test_real_oci_workflow_is_one_bounded_cell_without_scope_growth(self):
        from support import ROOT
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        section = workflow.split('      - name: Rootless OCI persistent exec integration', 1)[1].split('      - name:', 1)[0]
        self.assertIn("if: matrix.os == 'ubuntu-24.04' && matrix.python == '3.13'", section)
        self.assertEqual(workflow.count('test_persistent_exec_oci.py'), 1)
        self.assertIn('timeout-minutes: 10', section)
        self.assertIn('timeout 150 python -m unittest', section)
        self.assertIn('scripts/build-sandbox.sh', section)
        self.assertNotIn('sudo podman', section)
        self.assertLessEqual(3246 + 3300 + 600, 120 * 60)
        from test_check_offline import CORE_SCOPE_EXPRESSION
        self.assertIn(CORE_SCOPE_EXPRESSION, workflow)
