"""Opt-in trusted-host rootless OCI integration, never a role runtime grant.

CI runs this only in the existing Ubuntu 24.04/Python 3.13 core cell. The
ordinary offline gate reports it skipped, not runtime-isolation evidence.
"""
import dataclasses
import os
import shutil
import threading
import unittest

from support import Fixture
from mizu import check_jobs
from mizu.process import run
from mizu.sandbox import runtime_base, runtime_env
from mizu.sandbox_operations import exec_experiment


@unittest.skipUnless(os.environ.get('MIZU_OCI_IMAGE'), 'real OCI requires explicit test image')
class PersistentOCITests(Fixture):
    def test_rootless_state_reset_independent_checks_and_removal(self):
        runtime = shutil.which('podman')
        self.assertIsNotNone(runtime)
        info = run([runtime, 'info', '--format', '{{.Host.Security.Rootless}}'], timeout=15, maximum=8192)
        self.assertEqual((info.exit_code, info.stdout.strip()), (0, 'true'))
        worker = dataclasses.replace(self.config.roles['worker'], persistent_exec=True,
                                     capabilities=(*self.config.roles['worker'].capabilities, 'check_job'))
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, executable=runtime,
                                        image=os.environ['MIZU_OCI_IMAGE']),
            limits=dataclasses.replace(self.config.limits, command_seconds=30),
            roles={**self.config.roles, 'worker': worker})
        self.project.config = self.config
        ctx = self.context()
        self.addCleanup(ctx.close_exec)
        containers = []
        def execute(script):
            result = exec_experiment(ctx, 'exec', {'script': script})
            if ctx.exec_session is not None:
                containers.append(ctx.exec_session.name)
            return result
        def passed(result):
            self.assertEqual((result['reason'], result['exit_code']), ('exited', 0), result)
        passed(execute('cd /tmp; export MIZU_OCI_STATE=kept; f() { printf function; }; sleep 120 & bg=$!'))
        original = ctx.exec_session
        result = execute('printf "%s:%s:" "$PWD" "$MIZU_OCI_STATE"; f; kill -0 "$bg"; false')
        self.assertEqual((result['exit_code'], result['stdout']), (1, '/tmp:kept:function'))
        self.assertIs(ctx.exec_session, original)
        # Assert live isolation, not just an argv mock. No runtime mounted into
        # the role; rootless podman exists only on this trusted CI host.
        passed(execute('''python3 - <<'CHECK'
import os
from pathlib import Path
status = Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
assert os.getuid() != 0
assert not Path('/var/run/docker.sock').exists()
assert not Path('/run/podman/podman.sock').exists()
try:
    Path('/mizu-must-be-readonly').write_text('no')
except OSError:
    pass
else:
    raise AssertionError('writable root')
Path('/workspace/oci-write').write_text('allowed')
CHECK'''))
        # Close the live writer before a one-shot experiment/acceptance handoff.
        # Experiments themselves must not share its cwd/env/function state.
        clean = 'test -z "${MIZU_OCI_STATE:-}" && test "$PWD" = /workspace && ! command -v f'
        experiment = ctx.sandbox.execute(ctx.workspace,
            'test -z "${MIZU_OCI_STATE:-}" && test "$PWD" = /work && ! command -v f',
            writable=False, experiment=True)
        passed(experiment)
        # Synchronous verify: each configured command gets its own new shell.
        ctx.role = dataclasses.replace(worker, capabilities=tuple(c for c in worker.capabilities if c != 'check_job'))
        self.project.verify = ['export MIZU_OCI_STATE=dirty; cd /tmp', clean]
        verification = ctx._op_verify({})
        self.assertTrue(verification['passed'], verification)
        self.assertTrue(original.closed)
        # Queued acceptance uses Sandbox.execute on frozen source, never the
        # opted-in role's shell, including between its own acceptance commands.
        ctx.role = worker
        passed(execute('export MIZU_OCI_STATE=dirty'))
        queued_session = ctx.exec_session
        job = check_jobs.submit(ctx)
        self.assertTrue(queued_session.closed)
        completion = check_jobs.run_one(self.project, threading.Event())
        self.assertEqual(completion['status'], 'completed', completion)
        self.assertTrue(check_jobs.read(self.project, job['id'])['passed'])
        # A timeout kills the entire container, not merely the CLI/shell. The
        # command is never replayed: a workspace counter changes exactly once.
        passed(execute(':'))  # absorb image/startup work outside the short bound
        ctx.sandbox.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, command_seconds=1))
        result = execute('echo once >> /workspace/oci-counter; sleep 120')
        self.assertEqual(result['reason'], 'timeout', result)
        self.assertTrue(result['state_lost'])
        self.assertTrue(result['execution_may_be_partial'])
        ctx.sandbox.config = self.config
        passed(execute(clean))
        self.assertEqual((ctx.workspace / 'oci-counter').read_text(), 'once\n')
        ctx.cancel_operations()
        for name in set(containers):
            remaining = run([*runtime_base(self.config), 'ps', '--all', '--filter', 'name=' + name,
                             '--format', '{{.ID}}'], timeout=15, maximum=8192, env=runtime_env(self.config))
            self.assertEqual((remaining.exit_code, remaining.stdout.strip()), (0, ''))
