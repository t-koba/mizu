"""Offline gate preflight and structured, privacy-safe unittest diagnostics."""
from contextlib import nullcontext
from functools import partial
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_script('check')
CHILD = load_script('check_unittest')
INSTALLER = load_script('install')


def write_adapter(base: Path, deps, installed: dict):
    (base).mkdir(parents=True, exist_ok=True)
    (base / 'package.json').write_text(json.dumps({"dependencies": deps}), encoding='utf-8')
    for name, version in installed.items():
        target = base / 'node_modules' / name
        target.mkdir(parents=True, exist_ok=True)
        (target / 'package.json').write_text(json.dumps({"version": version}), encoding='utf-8')
    return base


class InstallerStagingWaiverTests(unittest.TestCase):
    def test_core_only_waives_all_unprovisioned_adapter_gates(self):
        self.assertEqual(INSTALLER.staging_not_run_checks(core_only=True),
                         'node-durable-contract-tests,node-pi-sdk-contract-tests,'
                         'node-pi-reasoning-replay-tests')

    def test_pi_staging_runs_pi_sdk_contract_and_waives_only_missing_adapters(self):
        self.assertEqual(INSTALLER.staging_not_run_checks(core_only=False),
                         'node-durable-contract-tests,node-pi-reasoning-replay-tests')


class CheckTimeoutTests(unittest.TestCase):
    def test_windows_budget_covers_setup_child_timeout_and_receipt_path(self):
        child_timeout = CHECK._unittest_timeout_seconds('nt')
        self.assertEqual(child_timeout, 1450)
        self.assertEqual(CHECK._unittest_timeout_seconds('posix'), 1000)
        self.assertEqual(CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS, 600)
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 40 || 20 }}", workflow)
        # Exercise the 60-second setup case, one later execute timeout, both
        # bounded cleanup waits, process-group termination (1 s grace + 5 s
        # reap), and the suffix/receipt reserve: 2296 s total, leaving 104 s.
        required = (60 + child_timeout + CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS
                    + 2 * CHECK.PROCESS_CLEANUP_TIMEOUT_SECONDS + 6 + 120)
        self.assertEqual(required, 2296)
        self.assertGreaterEqual(40 * 60, required)

    def test_ci_provisions_both_pi_adapters_for_unwaived_contract_gates(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        self.assertIn('node-pi-sdk-contract-tests,node-pi-reasoning-replay-tests', workflow)
        for adapter in ('pi', 'pi-durable'):
            self.assertIn(f'working-directory: adapters/{adapter}', workflow)

    def test_gate_cleans_up_descendant_after_parent_exit(self):
        self._gate_with_inherited_stdout(timed_out=False)

    def test_gate_cleans_up_descendant_after_timeout(self):
        self._gate_with_inherited_stdout(timed_out=True)

    def _gate_with_inherited_stdout(self, *, timed_out):
        """Run main's real execute/pump and real process-tree helpers.

        Only the suite command and unrelated preflights are replaced. The
        descendant confirms startup by opening a listener before the parent
        exits/sleeps, and inherits the gate's stdout pipe for its whole life.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / 'ready.json'
            report = root / 'report.json'
            descendant = root / 'descendant.py'
            descendant.write_text(textwrap.dedent('''\
                import json, os, socket, sys, time
                from pathlib import Path
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    listener.listen()
                    Path(sys.argv[1]).write_text(json.dumps({
                        'pid': os.getpid(), 'port': listener.getsockname()[1]
                    }), encoding='utf-8')
                    time.sleep(90)
                '''), encoding='utf-8')
            parent = root / 'parent.py'
            parent.write_text(textwrap.dedent('''\
                import subprocess, sys, time
                from pathlib import Path
                child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
                deadline = time.monotonic() + 20
                while not Path(sys.argv[2]).exists():
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('descendant failed to start')
                    time.sleep(.01)
                print('descendant confirmed', flush=True)
                print('Ran 1 test in 0.1s', flush=True)
                if sys.argv[3] == 'timeout':
                    time.sleep(90)
                '''), encoding='utf-8')
            processes, results, errors = [], [], []
            progress = threading.Event()

            class LiveOutput(io.StringIO):
                def write(self, text):
                    result = super().write(text)
                    if '[+' in text and 'descendant confirmed' in text:
                        progress.set()
                    return result

            output = LiveOutput()
            real_spawn = CHECK.spawn

            def launch(command, **kwargs):
                self.assertTrue(any('check_unittest.py' in part for part in command), command)
                process = real_spawn([sys.executable, '-u', str(parent), str(descendant),
                                      str(ready), 'timeout' if timed_out else 'exit'], **kwargs)
                processes.append(process)
                return process

            def run_gate():
                try:
                    results.append(CHECK.main())
                except BaseException as exc:
                    errors.append(exc)

            # A separate thread lets the test enforce a wall-clock bound and
            # still kill/reap the captured tree in finally if cleanup regresses.
            gate = threading.Thread(target=run_gate, daemon=True)
            with mock.patch.object(CHECK, 'ROOT', root), \
                    mock.patch.object(CHECK.sys, 'argv', ['check.py', '--report', str(report)]), \
                    mock.patch.object(CHECK.sys, 'stdout', output), \
                    mock.patch.object(CHECK, 'spawn', side_effect=launch), \
                    mock.patch.object(CHECK, '_unittest_timeout_seconds', return_value=3), \
                    mock.patch.object(CHECK, 'PROCESS_CLEANUP_TIMEOUT_SECONDS', 1), \
                    mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                    mock.patch.object(CHECK, 'bash_available', return_value=False):
                started = time.monotonic()
                gate.start()
                try:
                    self.assertTrue(progress.wait(6), 'confirmed descendant progress must stream live')
                    if timed_out:
                        self.assertTrue(gate.is_alive(), 'progress must arrive before the timeout')
                    gate.join(timeout=9)
                    elapsed = time.monotonic() - started
                    self.assertFalse(gate.is_alive(), 'gate blocked on inherited stdout')
                    self.assertLess(elapsed, 14, '3 s child cap plus cleanup/startup margin')
                    self.assertFalse(errors, errors)
                    self.assertEqual(results, [1])  # node is deliberately unavailable
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].returncode, 'direct child must be reaped')
                    self.assertTrue(processes[0].stdout.closed, 'reader must reach EOF and close its stream')
                    receipt = json.loads(report.read_text(encoding='utf-8'))
                    entry = next(c for c in receipt['checks'] if c['name'] == 'python-unit-and-contract-tests')
                    self.assertEqual(entry['tests'], {'run': 1, 'failed': 0, 'skipped': 0})
                    self.assertEqual(entry['status'], 'fail' if timed_out else 'pass')
                    if timed_out:
                        # Windows Job Object termination can report process exit
                        # status 0; the gate records timeout in status/details.
                        self.assertIn('timed out after 3 s', entry['details'])
                        self.assertIn('unittest failure details unavailable', entry['details'])
                    else:
                        self.assertEqual(entry['exit_code'], 0)
                        self.assertNotIn('details', entry)
                    # Grandchildren aren't waitpid-able by this process on POSIX (and
                    # may briefly be zombies). A closed listener verifies portably that
                    # the confirmed descendant no longer runs holding its resources.
                    port = json.loads(ready.read_text(encoding='utf-8'))['port']
                    deadline = time.monotonic() + 3
                    while True:
                        with socket.socket() as probe:
                            probe.settimeout(.2)
                            stopped = probe.connect_ex(('127.0.0.1', port)) != 0
                        if stopped or time.monotonic() >= deadline:
                            break
                        time.sleep(.02)
                    self.assertTrue(stopped, 'descendant must not survive gate completion')
                finally:
                    for process in processes:
                        CHECK.terminate_process(process)
                    gate.join(timeout=5)


class PiSdkDepsProbeTests(unittest.TestCase):
    def test_ready_when_pinned_versions_installed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {"left-pad": "1.3.0"})
            self.assertEqual(CHECK.pi_sdk_deps_status(adapter), ("ready", ""))

    def test_absent_when_nothing_installed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {})
            state, details = CHECK.pi_sdk_deps_status(adapter)
            self.assertEqual(state, "absent")
            self.assertIn("left-pad@1.3.0", details)

    def test_mismatch_when_version_differs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {"left-pad": "9.9.9"})
            state, details = CHECK.pi_sdk_deps_status(adapter)
            self.assertEqual(state, "mismatch")
            self.assertIn("9.9.9", details)

    def test_mismatch_when_manifest_unreadable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / 'package.json').write_text("not json", encoding='utf-8')
            state, _ = CHECK.pi_sdk_deps_status(base)
            self.assertEqual(state, "mismatch")

    def test_real_adapter_reports_ready_where_provisioned(self):
        # Environment-coupled by nature: where the operator provisioned the
        # pinned SDK the probe must agree; elsewhere it skips instead of
        # failing the gate for provisioning. A version mismatch still fails:
        # wrong versions are a contract violation, not missing provisioning.
        state, details = CHECK.pi_sdk_deps_status(ROOT / 'adapters/pi-durable')
        if state == "absent":
            self.skipTest(f"pinned SDK not provisioned here: {details}")
        self.assertEqual((state, details), ("ready", ""))


class UnittestFailureSummaryTests(unittest.TestCase):
    def run_case(self, case):
        """Exercise the real TextTestResult and the parent's sidecar reader."""
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'failures.jsonl'
            with path.open('w', encoding='utf-8') as sidecar:
                result = unittest.TextTestRunner(
                    stream=stream, verbosity=2,
                    resultclass=partial(CHILD.SafeTextTestResult, sidecar=sidecar),
                ).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
            data = path.read_text(encoding='utf-8')
            summary = CHECK.unittest_failure_summary(path)
        return result, stream.getvalue(), data, summary

    def assert_private(self, output, data, summary, *values):
        for text in (output, data, summary):
            self.assertNotIn(str(ROOT), text)
            for value in values:
                self.assertNotIn(value, text)

    def test_failure_summary_keeps_stable_id_repo_location_and_type(self):
        class FailureTest(unittest.TestCase):
            def test_expected_result(self):
                """private_description_value"""
                self.assertEqual('private_fixture_value', 'expected_value')
        result, output, data, summary = self.run_case(FailureTest)
        self.assertEqual(len(result.failures), 1)
        self.assertIn('FAIL ' + FailureTest('test_expected_result').id(), summary)
        self.assertIn('tests/test_check_offline.py:', summary)
        self.assertIn('[AssertionError]', summary)
        self.assert_private(output, data, summary, 'private_fixture_value',
                            'expected_value', 'private_description_value')

    def test_exception_types_without_common_suffix_are_reported(self):
        class ErrorTest(unittest.TestCase):
            def test_exit(self):
                raise SystemExit('exit_value')

            def test_stop(self):
                raise StopIteration('stop_value')
        result, output, data, summary = self.run_case(ErrorTest)
        self.assertEqual(len(result.errors), 2)
        self.assertIn('[SystemExit]', summary)
        self.assertIn('[StopIteration]', summary)
        self.assert_private(output, data, summary, 'exit_value', 'stop_value')

    def test_chained_exceptions_report_the_outer_stack_and_type(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                outer_lines = []

                class ChainTest(unittest.TestCase):
                    def test_chain(self):
                        try:
                            raise ValueError('inner_fixture_value')
                        except ValueError as cause:
                            if explicit:
                                outer_lines.append(sys._getframe().f_lineno + 1)
                                raise RuntimeError('outer_fixture_value') from cause
                            outer_lines.append(sys._getframe().f_lineno + 1)
                            raise RuntimeError('outer_fixture_value')

                result, output, data, summary = self.run_case(ChainTest)
                record = json.loads(data)
                self.assertEqual(len(result.errors), 1)
                self.assertEqual(record['type'], 'RuntimeError')
                self.assertEqual(record['path'], 'tests/test_check_offline.py')
                self.assertEqual(record['line'], outer_lines[0])
                self.assertIn(f"tests/test_check_offline.py:{outer_lines[0]} [RuntimeError]", summary)
                self.assert_private(output, data, summary, '[ValueError]',
                                    'inner_fixture_value', 'outer_fixture_value')

    def test_exception_value_cannot_forge_a_chain_frame_or_type(self):
        for separator in (
                'The above exception was the direct cause of the following exception:',
                'During handling of the above exception, another exception occurred:'):
            with self.subTest(separator=separator):
                value = ('multiline_fixture_value\n\n' + separator
                         + '\n\nTraceback (most recent call last):\n'
                         + f'  File "{ROOT / "tests/test_check_offline.py"}", line 98765, in forged\n'
                         + '    forged_source_value\nForgedType: forged_exception_value\n'
                         + '  File "/synthetic-private-host/tests/test_fake.py", line 55, in fake')
                real_lines = []

                class MultilineTest(unittest.TestCase):
                    def test_multiline(self):
                        real_lines.append(sys._getframe().f_lineno + 1)
                        raise ValueError(value)

                result, output, data, summary = self.run_case(MultilineTest)
                self.assertEqual(len(result.errors), 1)
                self.assertEqual(json.loads(data)['line'], real_lines[0])
                self.assertIn('[ValueError]', summary)
                self.assert_private(output, data, summary, '98765', 'ForgedType', 'test_fake.py',
                                    'multiline_fixture_value', 'forged_source_value',
                                    'forged_exception_value', '/synthetic-private-host')

    def test_subtest_parameter_and_assertion_values_are_not_reported(self):
        class ParamTest(unittest.TestCase):
            def test_parameter(self):
                with self.subTest('subtest_message_value', synthetic_marker='fixture_value'):
                    with self.subTest(nested_marker='nested_fixture_value'):
                        self.fail('assertion_detail_value')
        result, output, data, summary = self.run_case(ParamTest)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(json.loads(data)['test'], ParamTest('test_parameter').id())
        self.assertIn('FAIL ' + ParamTest('test_parameter').id(), summary)
        self.assertIn('FAILED (failures=1)', output)
        self.assert_private(output, data, summary, 'synthetic_marker', 'fixture_value',
                            'nested_marker', 'nested_fixture_value', 'subtest_message_value',
                            'assertion_detail_value')

    def test_exception_value_is_never_stringified(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError('exception value was stringified')

        class ErrorTest(unittest.TestCase):
            def test_unprintable(self):
                raise UnprintableError()
        result, output, data, summary = self.run_case(ErrorTest)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('[UnprintableError]', summary)
        self.assert_private(output, data, summary, 'exception value was stringified')

    def test_only_real_repository_frames_are_recorded(self):
        # An unrelated directory named tests is NOT a repository frame.
        with tempfile.TemporaryDirectory() as temporary:
            external = Path(temporary) / 'tests/test_external.py'
            external.parent.mkdir()
            external.write_text("raise ValueError('external_fixture_value')", encoding='utf-8')
            namespace = {}
            try:
                exec(compile(external.read_text(), str(external), 'exec'), namespace)
            except ValueError:
                # Drop this test's real frame so only the external one remains.
                error_type, value, tb = sys.exc_info()
                record = CHILD.failure_record('ERROR', self, (error_type, value, tb.tb_next))
            self.assertIsNone(record['path'])
            self.assertIsNone(record['line'])
            self.assertNotIn(str(external), json.dumps(record))

    def test_counts_and_skip_expected_failure_unexpected_success_display(self):
        class OutcomesTest(unittest.TestCase):
            def test_error(self):
                raise OSError('error_value')

            def test_failure(self):
                self.fail('failure_value')

            def test_skip(self):
                self.skipTest('/synthetic-private-host/skip_value')

            @unittest.expectedFailure
            def test_expected_failure(self):
                self.fail('expected_failure_value')

            @unittest.expectedFailure
            def test_unexpected_success(self):
                pass
        result, output, data, summary = self.run_case(OutcomesTest)
        self.assertEqual(result.testsRun, 5)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(len(result.expectedFailures), 1)
        self.assertEqual(len(result.unexpectedSuccesses), 1)
        self.assertEqual(len(data.splitlines()), 3)
        self.assertIn('Ran 5 tests', output)
        self.assertIn('FAILED (failures=1, errors=1, skipped=1, expected failures=1, unexpected successes=1)', output)
        for kind, method in (('ERROR', 'test_error'), ('FAIL', 'test_failure'),
                             ('UNEXPECTED SUCCESS', 'test_unexpected_success')):
            self.assertIn(kind + ' ' + OutcomesTest(method).id(), summary)
        self.assert_private(output, data, summary, 'error_value', 'failure_value',
                            'skip_value', '/synthetic-private-host', 'expected_failure_value')

    def test_missing_empty_malformed_and_oversized_sidecars_fail_generically(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'missing.jsonl'
            generic = 'unittest failure details unavailable'
            self.assertEqual(CHECK.unittest_failure_summary(path), generic)
            for content in (b'', b'{malformed', b'\xff', b'{}\n', b'[]\n', b'x' * 1_000_001):
                with self.subTest(content_length=len(content)):
                    path.write_bytes(content)
                    self.assertEqual(CHECK.unittest_failure_summary(path), generic)

    def test_sidecar_rejects_unsafe_metadata_and_bounds_summary(self):
        record = {'kind': 'ERROR', 'test': 'tests.ExampleTests.test_example',
                  'type': 'ValueError', 'path': 'tests/test_check_offline.py', 'line': 10}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'failures.jsonl'
            for field, value in (('test', '/synthetic-private-host/test'),
                                 ('test', 'test_example (fixture_value)'),
                                 ('type', 'ValueError: fixture_value'),
                                 ('type', None),
                                 ('kind', 'UNEXPECTED SUCCESS'),
                                 ('path', '/synthetic-private-host/tests/test_fake.py'),
                                 ('path', '../tests/test_check_offline.py'),
                                 ('path', 'tests/test_missing_file.py'),
                                 ('line', True)):
                with self.subTest(field=field):
                    path.write_text(json.dumps({**record, field: value}), encoding='utf-8')
                    self.assertEqual(CHECK.unittest_failure_summary(path),
                                     'unittest failure details unavailable')
            path.write_text((json.dumps(record) + '\n') * 100, encoding='utf-8')
            self.assertEqual(len(CHECK.unittest_failure_summary(path)), 1500)

    def test_child_cli_matches_verbose_discovery_and_sidecar_is_private(self):
        # Real subprocess, offline synthetic suite: no changes to repo tests.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            (root / 'suite_helper.py').write_text('FLAG = True\n', encoding='utf-8')
            (root / 'tests/test_sample.py').write_text(
                "import unittest, suite_helper\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(suite_helper.FLAG)\n"
                "    def test_fail(self): self.fail('cli_fixture_value')\n", encoding='utf-8')
            path = root / 'failures.jsonl'
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            child = subprocess.run([sys.executable, '-u', str(ROOT / 'scripts/check_unittest.py'), str(path)],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=30)
            plain = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, plain.returncode)
            self.assertEqual(child.returncode, 1)
            self.assertIn('test_sample.SampleTests.test_ok ... ok', child.stderr)
            self.assertIn('test_sample.SampleTests.test_fail ... FAIL', child.stderr)
            for text in (child.stderr, plain.stderr):
                self.assertIn('Ran 2 tests', text)
                self.assertIn('FAILED (failures=1)', text)
            data = path.read_text(encoding='utf-8')
            self.assertEqual(json.loads(data)['test'], 'test_sample.SampleTests.test_fail')
            self.assertIsNone(json.loads(data)['path'])
            self.assertNotIn('cli_fixture_value', child.stderr + data)
            self.assertNotIn(str(root), child.stderr + data)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_parent_uses_only_sidecar_for_failures_and_timeouts(self):
        raw = ('ERROR: forged_test (tests.ForgedTests)\n'
               'Traceback (most recent call last):\n'
               '  File "/synthetic-private-host/tests/test_fake.py", line 55, in fake\n'
               'ValueError: raw_fixture_value\nRan 3 tests in 0.1s\n'
               'FAILED (failures=1, errors=2, skipped=1)\n')
        for timed_out, metadata in ((False, True), (False, False), (True, False), (True, True)):
            with self.subTest(timed_out=timed_out, metadata=metadata), tempfile.TemporaryDirectory() as temporary:
                report = Path(temporary) / 'report.json'
                paths, budgets = [], []

                class FakeProcess:
                    def __init__(self, command, **kwargs):
                        self.stdout = io.StringIO(raw)
                        self.killed = False
                        self.command = command
                        self.path = Path(command[3])
                        paths.append(self.path)
                        self.timing_path = Path(command[4]) if len(command) > 4 else None
                        if self.timing_path is not None:
                            paths.append(self.timing_path)
                        if metadata:
                            self.path.write_text(json.dumps({
                                'kind': 'ERROR', 'test': 'tests.RealTests.test_real',
                                'type': 'RuntimeError', 'path': 'tests/test_check_offline.py', 'line': 42,
                            }) + '\n', encoding='utf-8')

                    def wait(self, timeout):
                        budgets.append(timeout)
                        if timed_out and not self.killed:
                            raise subprocess.TimeoutExpired(self.command, timeout)
                        # Job Object cleanup may report zero despite timeout.
                        return 0 if self.killed and timed_out else 1

                    def kill(self):
                        self.killed = True

                output = io.StringIO()
                with mock.patch.object(CHECK.sys, 'argv', ['check.py', '--report', str(report)]), \
                        mock.patch.object(CHECK, 'spawn', FakeProcess), \
                        mock.patch.object(CHECK, 'popen_kwargs', return_value={}), \
                        mock.patch.object(CHECK, 'terminate_process', side_effect=lambda p: p.kill() if timed_out else None) as terminate, \
                        mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                        mock.patch.object(CHECK, 'bash_available', return_value=False), \
                        mock.patch.object(CHECK.sys, 'stdout', output):
                    self.assertEqual(CHECK.main(), 1)
                receipt = json.loads(report.read_text(encoding='utf-8'))
                entry = next(c for c in receipt['checks'] if c['name'] == 'python-unit-and-contract-tests')
                details = entry['details']
                self.assertEqual(entry['status'], 'fail')
                terminate.assert_called_once()
                self.assertEqual(budgets[-1], CHECK.PROCESS_CLEANUP_TIMEOUT_SECONDS)
                timeout = CHECK._unittest_timeout_seconds()
                self.assertEqual(budgets[0], timeout)
                self.assertEqual(entry['tests'], {'run': 3, 'failed': 3, 'skipped': 1})
                self.assertFalse(paths[0].parent.exists(), 'private sidecar directory must be cleaned')
                self.assertEqual(len(paths), 1, 'local checks do not collect timing sidecars without opt-in')
                self.assertFalse((Path(temporary) / 'private-validation'
                                  / 'test-timings.json').exists())
                self.assertNotIn('raw_fixture_value', details)
                self.assertNotIn('forged_test', details)
                self.assertNotIn('test_fake.py', details)
                self.assertNotIn('/synthetic-private-host', details)
                if metadata:
                    self.assertIn('ERROR tests.RealTests.test_real', details)
                    self.assertIn('tests/test_check_offline.py:42 [RuntimeError]', details)
                else:
                    self.assertIn('unittest failure details unavailable', details)
                if timed_out:
                    self.assertIn(f'timed out after {timeout} s', details)
                self.assertIn('[+', output.getvalue(), 'live timestamped output is still relayed')


class UnittestTimingEvidenceTests(unittest.TestCase):
    """Per-test runtime evidence only; never a timeout policy or gate."""

    def run_timing_case(self, case):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            failures = Path(temporary) / 'failures.jsonl'
            timings = Path(temporary) / 'timings.jsonl'
            with failures.open('w', encoding='utf-8') as sidecar, \
                    timings.open('w', encoding='utf-8', newline='\n') as timing:
                result = unittest.TextTestRunner(
                    stream=stream, verbosity=2,
                    resultclass=partial(CHILD.SafeTextTestResult, sidecar=sidecar, timing=timing),
                ).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
            parsed = CHECK.parse_unittest_timings(timings, elapsed_seconds=10.0)
            raw = timings.read_text(encoding='utf-8')
        return result, stream.getvalue(), raw, parsed

    def test_complete_timings_record_durations_and_completion(self):
        class TimingTest(unittest.TestCase):
            def test_fast(self):
                pass

            def test_slow(self):
                time.sleep(0.02)

        result, output, raw, parsed = self.run_timing_case(TimingTest)
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(len(parsed), 2)
        by_test = {entry['test']: entry for entry in parsed}
        for method in ('test_fast', 'test_slow'):
            test_id = TimingTest(method).id()
            self.assertIn(test_id, by_test)
            entry = by_test[test_id]
            self.assertTrue(entry['completed'])
            self.assertIsInstance(entry['duration_seconds'], float)
            self.assertGreaterEqual(entry['duration_seconds'], 0.0)
            self.assertLessEqual(entry['duration_seconds'], 100000.0)
        self.assertLessEqual(by_test[TimingTest('test_fast').id()]['duration_seconds'],
                             by_test[TimingTest('test_slow').id()]['duration_seconds'] + 5.0)
        # Value-free: only IDs, events, monotonic numbers.
        for line in raw.splitlines():
            record = json.loads(line)
            self.assertEqual(set(record), {'test', 'event', 't'})
        self.assertNotIn(str(ROOT), raw)

    def test_partial_unfinished_entry_survives_kill(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'timings.jsonl'
            path.write_text(
                json.dumps({'test': 'tests.TimingTests.test_done', 'event': 'start', 't': 0.0}) + '\n'
                + json.dumps({'test': 'tests.TimingTests.test_done', 'event': 'stop',
                              't': 0.02}) + '\n'
                + json.dumps({'test': 'tests.TimingTests.test_killed', 'event': 'start',
                              't': 0.03}) + '\n',
                encoding='utf-8')
            parsed = CHECK.parse_unittest_timings(path, elapsed_seconds=10.0)
        self.assertEqual(len(parsed), 2)
        done = next(e for e in parsed if e['test'] == 'tests.TimingTests.test_done')
        killed = next(e for e in parsed if e['test'] == 'tests.TimingTests.test_killed')
        self.assertEqual((done['completed'], done['duration_seconds']), (True, 0.02))
        self.assertEqual((killed['completed'], killed['duration_seconds']), (False, 9.97))

    def test_timing_parser_bounds_and_sanitizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'timings.jsonl'
            # Missing/empty/oversized yield no evidence, never a failure.
            self.assertEqual(CHECK.parse_unittest_timings(Path(temporary) / 'missing.jsonl', elapsed_seconds=10.0), [])
            path.write_bytes(b'')
            self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=10.0), [])
            path.write_bytes(b'x' * 1_000_001)
            self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=10.0), [])
            path.write_text('\n' * 20001, encoding='utf-8')
            self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=10.0), [])
            # Malformed and unsafe lines are skipped; valid ones survive sanitized.
            good_start = json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'start', 't': 1.0})
            good_stop = json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'stop',
                                    't': 1.5})
            bad_lines = [
                '{malformed',
                json.dumps({'test': '/synthetic-private-host/test', 'event': 'start', 't': 0.0}),
                json.dumps({'test': 'tests.RealTests.test_ok (fixture_value)', 'event': 'start', 't': 0.0}),
                json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'start',
                            't': 'raw_fixture_value'}),
                json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'stop',
                            't': 1.5, 'duration': 'fixture_value'}),
                json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'stop',
                            't': 1.5, 'duration': True}),
                json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'stop',
                            't': float('inf'), 'duration': 0.1}),
                json.dumps({'test': 'tests.RealTests.test_ok', 'event': 'start',
                            't': -1.0, 'extra': 'fixture_value'}),
                json.dumps({'test': 'tests.RealTests.test_bad', 'event': 'stop',
                            't': 2.0, 'duration': 200000.0}),
                json.dumps({'test': '../tests/test_check_offline.py', 'event': 'start', 't': 0.0}),
                '[]',
            ]
            path.write_text('\n'.join([good_start] + bad_lines + [good_stop]) + '\n', encoding='utf-8')
            parsed = CHECK.parse_unittest_timings(path, elapsed_seconds=10.0)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0], {'test': 'tests.RealTests.test_ok',
                                         'duration_seconds': 0.5, 'completed': True})
            text = json.dumps(parsed)
            for value in ('fixture_value', 'synthetic-private-host', 'raw_fixture_value', str(ROOT)):
                self.assertNotIn(value, text)
            # Duplicate starts/stops do not duplicate or overwrite evidence.
            path.write_text(
                good_start + '\n' + good_start + '\n' + good_stop + '\n' + good_stop + '\n',
                encoding='utf-8')
            parsed = CHECK.parse_unittest_timings(path, elapsed_seconds=10.0)
            self.assertEqual(parsed, [{'test': 'tests.RealTests.test_ok',
                                       'duration_seconds': 0.5, 'completed': True}])
            # A stop without a matching start is malformed.
            path.write_text(good_stop + '\n', encoding='utf-8')
            self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=10.0), [])

    def test_stops_require_bounded_matching_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'timings.jsonl'
            test_id = 'tests.RealTests.test_ok'
            start = {'test': test_id, 'event': 'start', 't': 1.0}
            for moment in (0.5, 100001.0, float('nan')):
                with self.subTest(moment=moment):
                    stop = {'test': test_id, 'event': 'stop', 't': moment}
                    path.write_text(json.dumps(start) + '\n' + json.dumps(stop) + '\n',
                                    encoding='utf-8')
                    self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=3.0),
                                     [{'test': test_id, 'completed': False,
                                       'duration_seconds': 2.0}])
            valid_stop = {'test': test_id, 'event': 'stop', 't': 2.0}
            path.write_text(json.dumps(start) + '\n' + json.dumps(valid_stop) + '\n',
                            encoding='utf-8')
            self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=3.0),
                             [{'test': test_id, 'completed': True,
                               'duration_seconds': 1.0}])
            # An impossible child-exit/start delta is omitted, not exported.
            path.write_text(json.dumps(start) + '\n', encoding='utf-8')
            for elapsed in (0.5, 100002.0, float('nan')):
                self.assertEqual(CHECK.parse_unittest_timings(path, elapsed_seconds=elapsed), [])

    def test_sidecar_growth_preserves_a_bounded_parseable_prefix(self):
        for name in ('TIMING_SIDECAR_MAX_BYTES', 'TIMING_SIDECAR_MAX_LINES',
                     'TIMING_SIDECAR_MAX_TESTS', 'TIMING_MAX_SECONDS'):
            self.assertEqual(getattr(CHECK, name), getattr(CHILD, name))

        class GrowthTest(unittest.TestCase):
            def __init__(self, index):
                super().__init__()
                self.index = index

            def id(self):
                return f'tests.GrowthTests.test_{self.index}_' + 'x' * 400

            def runTest(self):
                pass

        # Hit the real byte bound, then independently exercise line/test caps.
        for limits in ({}, {'TIMING_SIDECAR_MAX_LINES': 5},
                       {'TIMING_SIDECAR_MAX_TESTS': 2}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'timings.jsonl'
                suite = unittest.TestSuite(GrowthTest(i) for i in range(2000))
                with path.open('w', encoding='utf-8', newline='\n') as timing, \
                        (mock.patch.multiple(CHILD, **limits) if limits else nullcontext()):
                    result = unittest.TextTestRunner(
                        stream=io.StringIO(), resultclass=partial(
                            CHILD.SafeTextTestResult, sidecar=io.StringIO(), timing=timing),
                    ).run(suite)
                self.assertTrue(result.wasSuccessful())
                self.assertEqual(result.testsRun, 2000)
                self.assertLessEqual(path.stat().st_size, CHECK.TIMING_SIDECAR_MAX_BYTES)
                lines = path.read_text(encoding='utf-8').splitlines()
                self.assertLessEqual(len(lines), limits.get('TIMING_SIDECAR_MAX_LINES',
                                                            CHECK.TIMING_SIDECAR_MAX_LINES))
                parsed = CHECK.parse_unittest_timings(path, elapsed_seconds=10.0)
                self.assertGreater(len(parsed), 0)
                self.assertLess(len(parsed), 2000)
                self.assertEqual(len(lines), 2 * len(parsed))
                self.assertTrue(all(entry['completed'] for entry in parsed))
                if limits:
                    self.assertEqual(len(parsed), 2)

    def test_timing_io_failures_do_not_change_test_outcome(self):
        class PassingTest(unittest.TestCase):
            def runTest(self):
                pass

        timing = mock.Mock()
        timing.write.side_effect = OSError('synthetic timing write failure')
        result = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=partial(
                CHILD.SafeTextTestResult, sidecar=io.StringIO(), timing=timing),
        ).run(unittest.TestSuite([PassingTest()]))
        self.assertTrue(result.wasSuccessful())
        with tempfile.TemporaryDirectory() as temporary:
            blocked = Path(temporary) / 'not_a_directory'
            blocked.write_text('fixture', encoding='utf-8')
            CHECK.write_timings_artifact(blocked / 'timings.json', tests=[],
                                        status='pass', exit_code=0, elapsed_seconds=1.0)

    def test_local_acceptance_does_not_emit_artifact_without_opt_in(self):
        for options in ([], ['--report']):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                argv = ['check.py'] + (options + [str(root / 'report.json')] if options else [])
                process = mock.Mock(stdout=io.StringIO('Ran 0 tests\nOK\n'))
                process.wait.return_value = 0
                with mock.patch.object(CHECK, 'ROOT', root), \
                        mock.patch.object(CHECK.sys, 'argv', argv), \
                        mock.patch.object(CHECK, 'spawn', return_value=process), \
                        mock.patch.object(CHECK, 'terminate_process'), \
                        mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                        mock.patch.object(CHECK, 'bash_available', return_value=False), \
                        mock.patch.object(CHECK.sys, 'stdout', io.StringIO()), \
                        mock.patch.object(CHECK, 'write_timings_artifact') as writer:
                    self.assertEqual(CHECK.main(), 1)  # unprovisioned tools
                writer.assert_not_called()
                self.assertFalse((root / 'private-validation').exists())
                self.assertEqual(sorted(p.name for p in root.iterdir()),
                                 ['report.json'] if options else [])

    def test_real_child_termination_uses_shared_monotonic_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_hung.py').write_text(
                'import time, unittest\n'
                'class HungTest(unittest.TestCase):\n'
                '    def test_hung(self): time.sleep(60)\n', encoding='utf-8')
            failures, timings = root / 'failures.jsonl', root / 'timings.jsonl'
            # Deliberately predate the launch to catch a child-local origin.
            origin = time.monotonic() - 10.0
            child = subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'scripts/check_unittest.py'),
                 str(failures), str(timings), str(origin)], cwd=root,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if timings.exists() and timings.read_bytes().endswith(b'\n'):
                        break
                    time.sleep(0.01)
                else:
                    self.fail('child did not flush its test start')
                start = json.loads(timings.read_text(encoding='utf-8'))['t']
                self.assertGreaterEqual(start, 10.0)
                time.sleep(0.05)
                ended_at = time.monotonic()
            finally:
                child.kill()
                child.wait(timeout=10)
            parsed = CHECK.parse_unittest_timings(timings, elapsed_seconds=ended_at - origin)
            self.assertEqual(parsed, [{'test': 'test_hung.HungTest.test_hung',
                                      'completed': False,
                                      'duration_seconds': round(ended_at - origin - start, 3)}])
            self.assertGreaterEqual(parsed[0]['duration_seconds'], 0.04)
            self.assertLess(parsed[0]['duration_seconds'], ended_at - origin - 9.0)

    def test_timings_never_govern_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            failures = Path(temporary) / 'failures.jsonl'
            timings = Path(temporary) / 'timings.jsonl'
            failures.write_text(json.dumps({
                'kind': 'FAIL', 'test': 'tests.RealTests.test_real',
                'type': 'AssertionError', 'path': 'tests/test_check_offline.py', 'line': 10,
            }) + '\n', encoding='utf-8')
            timings.write_bytes(b'{malformed\n')
            # Failure evidence still decides; broken timings yield empty evidence.
            self.assertIn('FAIL tests.RealTests.test_real',
                          CHECK.unittest_failure_summary(failures))
            self.assertEqual(CHECK.parse_unittest_timings(timings, elapsed_seconds=10.0), [])
            # Artifact writing is best-effort and holds no diagnostics.
            artifact = Path(temporary) / 'test-timings.json'
            CHECK.write_timings_artifact(artifact, tests=[], status='fail', exit_code=1,
                                          elapsed_seconds=2.5, phases=[])
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(payload['suite'], 'test-timings')
            self.assertEqual(payload['tests'], [])
            self.assertEqual(payload['unittest_elapsed_seconds'], 2.5)
            self.assertNotIn('details', json.dumps(payload))
            self.assertNotIn('diagnostic', json.dumps(payload).lower())

    def test_timing_artifact_has_bounded_hotspot_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / 'test-timings.json'
            tests = [
                {'test': 'sample.Suite.test_fast', 'duration_seconds': 1.0, 'completed': True},
                {'test': 'sample.Suite.test_medium', 'duration_seconds': 2.0, 'completed': True},
                {'test': 'sample.Suite.test_slow', 'duration_seconds': 4.0, 'completed': True},
                {'test': 'sample.Suite.test_unfinished', 'duration_seconds': 9.5, 'completed': False},
            ]
            CHECK.write_timings_artifact(artifact, tests=tests, status='fail', exit_code=1,
                                          elapsed_seconds=10.0)
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            summary = payload['summary']
            self.assertEqual(summary['tests_recorded'], 4)
            self.assertEqual(summary['tests_completed'], 3)
            self.assertEqual(summary['completed_duration_seconds'], 7.0)
            self.assertEqual(summary['completed_duration_percentiles_seconds'],
                             {'p50': 2.0, 'p90': 4.0, 'p95': 4.0, 'p99': 4.0})
            self.assertEqual([item['test'] for item in summary['slowest_tests']],
                             ['sample.Suite.test_unfinished', 'sample.Suite.test_slow',
                              'sample.Suite.test_medium', 'sample.Suite.test_fast'])
            self.assertFalse(summary['slowest_tests'][0]['completed'])
            self.assertEqual(payload['tests'], tests)

            many_artifact = Path(temporary) / 'many-timings.json'
            many_tests = [
                {'test': f'sample.Suite.test_{index:02}',
                 'duration_seconds': float(index), 'completed': True}
                for index in range(25)
            ]
            CHECK.write_timings_artifact(many_artifact, tests=many_tests, status='pass',
                                          exit_code=0, elapsed_seconds=300.0)
            many_summary = json.loads(many_artifact.read_text(encoding='utf-8'))['summary']
            self.assertEqual(len(many_summary['slowest_tests']), 20)
            self.assertEqual(many_summary['slowest_tests'][0]['test'],
                             'sample.Suite.test_24')

    def test_child_cli_writes_both_sidecars_privately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            (root / 'suite_helper.py').write_text('FLAG = True\n', encoding='utf-8')
            (root / 'tests/test_sample.py').write_text(
                "import unittest, suite_helper\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self): self.assertTrue(suite_helper.FLAG)\n"
                "    def test_slow(self):\n"
                "        import time; time.sleep(0.01)\n",
                encoding='utf-8')
            failures = root / 'failures.jsonl'
            timings = root / 'timings.jsonl'
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            child = subprocess.run([sys.executable, '-u', str(ROOT / 'scripts/check_unittest.py'),
                                    str(failures), str(timings)],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(failures.read_text(encoding='utf-8'), '')
            parsed = CHECK.parse_unittest_timings(timings, elapsed_seconds=10.0)
            self.assertEqual(len(parsed), 2)
            self.assertTrue(all(entry['completed'] for entry in parsed))
            self.assertTrue(all(entry['duration_seconds'] is not None for entry in parsed))
            ids = {entry['test'] for entry in parsed}
            self.assertEqual(ids, {'test_sample.SampleTests.test_ok',
                                   'test_sample.SampleTests.test_slow'})
            self.assertNotIn(b'\r\n', timings.read_bytes())
            combined = failures.read_text(encoding='utf-8') + timings.read_text(encoding='utf-8')
            self.assertNotIn(str(root), child.stderr + combined)
            if os.name == 'posix':
                self.assertEqual(failures.stat().st_mode & 0o777, 0o600)
                self.assertEqual(timings.stat().st_mode & 0o777, 0o600)

    def test_workflow_uploads_only_dedicated_timings(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        # Immutable v4.6.2 pin; short retention; always with missing-file tolerance.
        self.assertIn('actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02', workflow)
        self.assertIn('name: Upload Windows per-test timings', workflow)
        self.assertIn('--test-timings private-validation/windows-test-timings.json', workflow)
        ordinary = workflow.split('name: Offline core and transport contracts', 1)[1].split(
            '      - name: Offline core and transport contracts with timings', 1)[0]
        self.assertNotIn('--test-timings', ordinary)
        timed = workflow.split('name: Offline core and transport contracts with timings', 1)[1].split(
            '      - name: Core installer', 1)[0]
        self.assertIn("if: runner.os == 'Windows' && matrix.python == '3.13'", timed)
        self.assertNotIn('test-timings-${{ matrix.os }}', workflow)
        upload = workflow.split('name: Upload Windows per-test timings', 1)[1].split('  durable-contract:', 1)[0]
        self.assertEqual([line.strip() for line in upload.splitlines() if line.strip().startswith('path:')],
                         ['path: private-validation/windows-test-timings.json'])
        self.assertIn('private-validation/windows-test-timings.json', workflow)
        self.assertIn('retention-days: 7', workflow)
        self.assertIn('if-no-files-found: warn', workflow)
        self.assertIn("if: always() && runner.os == 'Windows' && matrix.python == '3.13'", workflow)
        # Do not upload offline reports, which may contain diagnostics.
        self.assertNotIn('path: private-validation/offline.json', workflow)
        self.assertNotIn('path: private-validation/durable.json', workflow)
        # Timing stays diagnostic; the original 40-minute/1450-second scope is
        # enforced while the always-uploaded artifact preserves partial progress.
        self.assertIn('1450', workflow)
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 40 || 20 }}", workflow)
        self.assertEqual(CHECK._unittest_timeout_seconds('nt'), 1450)

    def test_offline_steps_run_exactly_once_per_matrix_cell(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        ordinary = workflow.split('name: Offline core and transport contracts', 1)[1].split(
            '      - name: Offline core and transport contracts with timings', 1)[0]
        timed = workflow.split('name: Offline core and transport contracts with timings', 1)[1].split(
            '      - name: Core installer', 1)[0]

        def condition(section):
            for line in section.splitlines():
                stripped = line.strip()
                if stripped.startswith('if: '):
                    return stripped[len('if: '):].strip()
            return None

        def holds(expression, runner_os, matrix_python):
            values = {'runner.os': runner_os, 'matrix.python': matrix_python}

            def term(text):
                left, _, right = text.strip().partition(' ')
                operator, _, literal = right.partition(' ')
                literal = literal.strip().strip('"').strip("'")
                value = values[left.strip()]
                return value != literal if operator == '!=' else value == literal

            return any(all(term(part) for part in clause.split('&&'))
                       for clause in expression.split('||'))

        ordinary_if = condition(ordinary)
        timed_if = condition(timed)
        self.assertIsNotNone(ordinary_if)
        self.assertIsNotNone(timed_if)
        # The two offline steps are exact complements, so every matrix cell
        # runs the gate once. Two runs on the Windows 3.13 cell would exceed
        # that cell's job budget and lose the timing evidence it exists for.
        # runner.os names the platform, not the matrix label.
        cells = [(runner_os, matrix_python)
                 for runner_os in ('Linux', 'macOS', 'Windows')
                 for matrix_python in ('3.11', '3.12', '3.13', '3.14')]
        for runner_os, matrix_python in cells:
            with self.subTest(os=runner_os, python=matrix_python):
                self.assertNotEqual(holds(ordinary_if, runner_os, matrix_python),
                                    holds(timed_if, runner_os, matrix_python))
        self.assertTrue(holds(timed_if, 'Windows', '3.13'))
        self.assertFalse(holds(timed_if, 'Linux', '3.13'))
        self.assertFalse(holds(timed_if, 'Windows', '3.12'))

    def test_timings_artifact_survives_timeout_with_only_evidence(self):
        raw = ('test_sample.SampleTests.test_slow ... ok\nRan 1 test in 0.1s\nOK\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / 'report.json'
            artifact = root / 'private-validation' / 'test-timings.json'

            class FakeProcess:
                def __init__(self, command, **kwargs):
                    self.stdout = io.StringIO(raw)
                    self.command = command
                    self.calls = 0
                    self.failure_path = Path(command[3])
                    self.timing_path = Path(command[4])
                    self.timing_path.write_text(
                        json.dumps({'test': 'tests.RealTests.test_done', 'event': 'start', 't': 0.0})
                        + '\n'
                        + json.dumps({'test': 'tests.RealTests.test_done', 'event': 'stop',
                                      't': 0.5}) + '\n'
                        + json.dumps({'test': 'tests.RealTests.test_hung', 'event': 'start',
                                      't': 0.6}) + '\n',
                        encoding='utf-8')

                def wait(self, timeout):
                    self.calls += 1
                    if self.calls == 1:
                        # End snapshot is 8 s after the shared origin; cleanup
                        # must not be included in the hung-test duration.
                        clock.return_value = float(self.command[5]) + 8.0
                        raise subprocess.TimeoutExpired(self.command, timeout)
                    return 0

                def kill(self):
                    clock.return_value += 30.0

            output = io.StringIO()
            with mock.patch.object(CHECK, 'ROOT', root), \
                    mock.patch.object(CHECK.sys, 'argv', ['check.py', '--report', str(report),
                                                          '--test-timings', str(artifact)]), \
                    mock.patch.object(CHECK, 'spawn', FakeProcess), \
                    mock.patch.object(CHECK.time, 'monotonic', return_value=1234.0) as clock, \
                    mock.patch.object(CHECK, 'popen_kwargs', return_value={}), \
                    mock.patch.object(CHECK, 'terminate_process', side_effect=lambda p: p.kill()), \
                    mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                    mock.patch.object(CHECK, 'bash_available', return_value=False), \
                    mock.patch.object(CHECK.sys, 'stdout', output):
                self.assertEqual(CHECK.main(), 1)
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(payload['suite'], 'test-timings')
            self.assertEqual(payload['unittest_status'], 'fail')
            self.assertEqual(payload['unittest_elapsed_seconds'], 8.0)
            self.assertIn('timed out after', json.loads(report.read_text(encoding='utf-8'))['checks'][1]['details'])
            by_test = {entry['test']: entry for entry in payload['tests']}
            self.assertEqual(by_test['tests.RealTests.test_done'],
                             {'test': 'tests.RealTests.test_done',
                              'duration_seconds': 0.5, 'completed': True})
            self.assertEqual(by_test['tests.RealTests.test_hung'],
                             {'test': 'tests.RealTests.test_hung',
                              'duration_seconds': 7.4, 'completed': False})
            # Only sanitized IDs, durations, completion, a bounded summary,
            # bounded phases, and minimal metadata; artifact is written after
            # all checks so phases are complete despite the unittest timeout.
            self.assertEqual(set(payload), {'suite', 'mizu_version', 'python', 'os',
                                            'unittest_timeout_seconds', 'unittest_elapsed_seconds',
                                            'unittest_status',
                                            'unittest_exit_code', 'summary', 'tests', 'phases'})
            for entry in payload['tests']:
                self.assertEqual(set(entry), {'test', 'duration_seconds', 'completed'})
            by_phase = {phase['name']: phase for phase in payload['phases']}
            # The timed-out unittest phase is present with fail status; later
            # phases (syntax, manifest) are also recorded because the write
            # happens after checks finish.
            self.assertIn('python-unit-and-contract-tests', by_phase)
            self.assertEqual(by_phase['python-unit-and-contract-tests']['status'], 'fail')
            self.assertIn('python-syntax', by_phase)
            self.assertIn('source-manifest-stable', by_phase)
            for phase in payload['phases']:
                self.assertEqual(set(phase) - {'duration_seconds', 'tests'}, {'name', 'status'})
                self.assertNotIn('details', phase)
                self.assertNotIn('exit_code', phase)
            blob = json.dumps(payload)
            self.assertNotIn('timed out after', blob)
            self.assertNotIn('output tail', blob)
            self.assertNotIn('details', blob)

    def test_early_timings_artifact_survives_later_interruption(self):
        # A later hang/job timeout must not lose per-test evidence: the
        # artifact is written immediately after unittest parsing with partial
        # phases, then rewritten with complete phases. Simulate an
        # interruption after unittest returns and assert the early sanitized
        # artifact remains with only partial phase summaries.
        raw = 'Ran 1 test in 0.1s\nOK\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / 'private-validation' / 'test-timings.json'

            class FakeProcess:
                def __init__(self, command, **kwargs):
                    self.stdout = io.StringIO(raw)
                    if len(command) > 4:
                        Path(command[4]).write_text(
                            json.dumps({'test': 'tests.RealTests.test_done',
                                        'event': 'start', 't': 0.0}) + '\n'
                            + json.dumps({'test': 'tests.RealTests.test_done',
                                          'event': 'stop', 't': 0.5}) + '\n',
                            encoding='utf-8')

                def wait(self, timeout=None):
                    return 0

            def _boom(name):
                raise RuntimeError('simulated later interruption')

            output = io.StringIO()
            with mock.patch.object(CHECK, 'ROOT', root), \
                    mock.patch.object(CHECK.sys, 'argv',
                                      ['check.py', '--test-timings', str(artifact)]), \
                    mock.patch.object(CHECK, 'node_version', return_value=None), \
                    mock.patch.object(CHECK, 'spawn', FakeProcess), \
                    mock.patch.object(CHECK, 'popen_kwargs', return_value={}), \
                    mock.patch.object(CHECK, 'terminate_process',
                                      side_effect=lambda process: None), \
                    mock.patch.object(CHECK.shutil, 'which', side_effect=_boom), \
                    mock.patch.object(CHECK.sys, 'stdout', output):
                with self.assertRaises(RuntimeError):
                    CHECK.main()
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(payload['suite'], 'test-timings')
            self.assertEqual(payload['unittest_status'], 'pass')
            self.assertEqual(payload['unittest_exit_code'], 0)
            by_test = {entry['test']: entry for entry in payload['tests']}
            self.assertEqual(by_test['tests.RealTests.test_done'],
                             {'test': 'tests.RealTests.test_done',
                              'duration_seconds': 0.5, 'completed': True})
            # Only sanitized IDs, durations, completion, a bounded summary,
            # partial phases, and minimal metadata; the final rewrite with
            # complete phases never ran because the later check was
            # interrupted.
            self.assertEqual(set(payload), {'suite', 'mizu_version', 'python', 'os',
                                            'unittest_timeout_seconds', 'unittest_elapsed_seconds',
                                            'unittest_status',
                                            'unittest_exit_code', 'summary', 'tests', 'phases'})
            for entry in payload['tests']:
                self.assertEqual(set(entry), {'test', 'duration_seconds', 'completed'})
            by_phase = {phase['name']: phase for phase in payload['phases']}
            self.assertIn('python-syntax', by_phase)
            self.assertIn('python-unit-and-contract-tests', by_phase)
            self.assertEqual(by_phase['python-unit-and-contract-tests']['status'], 'pass')
            self.assertNotIn('source-manifest-stable', by_phase)
            self.assertNotIn('node-available', by_phase)
            for phase in payload['phases']:
                self.assertEqual(set(phase) - {'duration_seconds', 'tests'}, {'name', 'status'})
                self.assertNotIn('details', phase)
                self.assertNotIn('exit_code', phase)
            blob = json.dumps(payload)
            self.assertNotIn('simulated later interruption', blob)
            self.assertNotIn('details', blob)

    def test_timing_phases_are_sanitized_and_bounded(self):
        # Only stable name/status/duration/counts survive; details, exit
        # codes, output, and diagnostics are dropped. Malformed names,
        # statuses, durations, and counts are skipped; entries are bounded.
        checks = [
            {'name': 'python-syntax', 'status': 'pass'},
            {'name': 'python-unit-and-contract-tests', 'status': 'fail',
             'seconds': 12.5, 'exit_code': 1,
             'tests': {'run': 10, 'failed': 2, 'skipped': 1},
             'details': 'timed out after 1450 s; secret output tail'},
            {'name': 'javascript-syntax:adapters/pi/launcher.mjs', 'status': 'pass',
             'seconds': 0.4},
            {'name': 'node-transport-and-extension-tests', 'status': 'pass',
             'seconds': 1.0, 'tests': {'run': 5, 'failed': 0, 'skipped': 0}},
            {'name': '/synthetic-private-host/evil', 'status': 'pass', 'seconds': 1.0,
             'details': 'raw_fixture_value'},
            {'name': 'tests.RealTests.test_ok (fixture_value)', 'status': 'pass'},
            {'name': 'good-name', 'status': 'bogus', 'seconds': 1.0},
            {'name': 'good-name-2', 'status': 'pass', 'seconds': float('inf')},
            {'name': 'good-name-3', 'status': 'pass', 'seconds': 1.0,
             'tests': {'run': 'many', 'failed': 0}},
            {'name': 'good-name-4', 'status': 'pass', 'seconds': 1.0,
             'tests': {'run': 1, 'extra': 2}},
            {'name': 'x' * 501, 'status': 'pass'},
            'not-a-dict',
        ]
        phases = CHECK.sanitize_timing_phases(checks)
        self.assertEqual([phase['name'] for phase in phases],
                         ['python-syntax', 'python-unit-and-contract-tests',
                          'javascript-syntax:adapters/pi/launcher.mjs',
                          'node-transport-and-extension-tests'])
        unit = next(phase for phase in phases if phase['name'] == 'python-unit-and-contract-tests')
        self.assertEqual(unit, {'name': 'python-unit-and-contract-tests', 'status': 'fail',
                               'duration_seconds': 12.5,
                               'tests': {'run': 10, 'failed': 2, 'skipped': 1}})
        blob = json.dumps(phases)
        for leaked in ('raw_fixture_value', 'synthetic-private-host', 'fixture_value',
                       'secret', 'output tail', 'details', 'exit_code'):
            self.assertNotIn(leaked, blob)
        # Bound: at most 200 phases even with many valid checks.
        many = [{'name': f'phase-{index:04}', 'status': 'pass', 'seconds': 0.1}
                for index in range(500)]
        self.assertEqual(len(CHECK.sanitize_timing_phases(many)), 200)
        # Counts must be ints in range; bools and negatives are rejected.
        self.assertIsNone(CHECK._valid_timing_counts({'run': True}))
        self.assertIsNone(CHECK._valid_timing_counts({'run': -1}))
        self.assertIsNone(CHECK._valid_timing_counts({'run': 1000001}))
        self.assertEqual(CHECK._valid_timing_counts({'run': 3}), {'run': 3})

    def test_timing_artifact_pass_records_phases_without_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / 'test-timings.json'
            tests = [{'test': 'sample.Suite.test_ok', 'duration_seconds': 0.5,
                      'completed': True}]
            phases = [{'name': 'python-syntax', 'status': 'pass'},
                      {'name': 'python-unit-and-contract-tests', 'status': 'pass',
                       'seconds': 3.0, 'tests': {'run': 1, 'failed': 0, 'skipped': 0},
                       'details': 'must not leak', 'exit_code': 0}]
            CHECK.write_timings_artifact(artifact, tests=tests, status='pass',
                                         exit_code=0, elapsed_seconds=3.0, phases=phases)
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(payload['suite'], 'test-timings')
            self.assertEqual(payload['unittest_status'], 'pass')
            self.assertEqual(payload['unittest_exit_code'], 0)
            self.assertEqual(payload['tests'], tests)
            self.assertEqual(payload['phases'],
                             [{'name': 'python-syntax', 'status': 'pass'},
                              {'name': 'python-unit-and-contract-tests', 'status': 'pass',
                               'duration_seconds': 3.0,
                               'tests': {'run': 1, 'failed': 0, 'skipped': 0}}])
            blob = json.dumps(payload)
            self.assertNotIn('must not leak', blob)
            self.assertNotIn('details', blob)

    def test_matrix_scope_timeouts_gates_and_jobs_preserved(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        # The 3x4 matrix, timeouts, installer step, and durable job are unchanged.
        self.assertIn('os: [ubuntu-24.04, macos-latest, windows-latest]', workflow)
        self.assertIn('python: ["3.11", "3.12", "3.13", "3.14"]', workflow)
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 40 || 20 }}", workflow)
        self.assertIn('run: python scripts/check.py --report private-validation/offline.json', workflow)
        self.assertIn('--allow-not-run node-durable-contract-tests,node-pi-sdk-contract-tests,node-pi-reasoning-replay-tests', workflow)
        self.assertIn("- name: Core installer and CLI lifecycle\n        if: runner.os == 'Linux' && matrix.python == '3.13'", workflow)
        self.assertIn('run: ./scripts/test-install.sh private-validation/offline.json', workflow)
        self.assertIn('  durable-contract:', workflow)
        self.assertIn('run: python scripts/check.py --report private-validation/durable.json', workflow)


class SdkGateEntryTests(unittest.TestCase):
    NAME = 'node-durable-contract-tests'

    def test_each_pi_gate_requires_its_own_explicit_waiver(self):
        for name in ('node-pi-sdk-contract-tests', 'node-pi-reasoning-replay-tests'):
            with self.subTest(name=name):
                self.assertIsNone(CHECK.sdk_gate_entry('ready', '', frozenset(), name=name))
                entry = CHECK.sdk_gate_entry('absent', 'missing SDK', frozenset({self.NAME}), name=name)
                self.assertEqual((entry['name'], entry['status']), (name, 'fail'))
                entry = CHECK.sdk_gate_entry('absent', 'missing SDK', frozenset({name}), name=name)
                self.assertEqual(entry['status'], 'not_run')
                entry = CHECK.sdk_gate_entry('mismatch', 'wrong SDK', frozenset({name}), name=name)
                self.assertEqual(entry['status'], 'fail')

    def test_ready_runs_the_suite(self):
        self.assertIsNone(CHECK.sdk_gate_entry("ready", "", frozenset()))

    def test_absent_fails_closed_by_default(self):
        entry = CHECK.sdk_gate_entry("absent", "left-pad@1.3.0 is not installed", frozenset())
        self.assertEqual(entry["status"], "fail")
        self.assertIn("not installed", entry["details"])

    def test_absent_waived_records_not_run(self):
        entry = CHECK.sdk_gate_entry("absent", "left-pad@1.3.0 is not installed", frozenset({self.NAME}))
        self.assertEqual(entry["status"], "not_run")
        self.assertIn("waived", entry["details"])

    def test_mismatch_fails_even_when_waived(self):
        entry = CHECK.sdk_gate_entry("mismatch", "left-pad installed at 9.9.9", frozenset({self.NAME}))
        self.assertEqual(entry["status"], "fail")

    def test_unknown_waiver_name_does_not_waive(self):
        entry = CHECK.sdk_gate_entry("absent", "not installed", frozenset({"other-suite"}))
        self.assertEqual(entry["status"], "fail")

    def test_allow_list_parsing(self):
        self.assertEqual(CHECK._allow_list(""), frozenset())
        self.assertEqual(CHECK._allow_list("a, b ,,a"), frozenset({"a", "b"}))



class ReusedSourceReportTests(unittest.TestCase):
    """Explicit content-bound reuse for core-only staging."""

    def valid_report(self):
        from mizu.distribution import OFFLINE_REPORT_VERSION, source_manifest_sha
        sha = source_manifest_sha(ROOT)
        bash_available = INSTALLER.functional_bash_available()
        return {
            "suite": "offline",
            "report_version": OFFLINE_REPORT_VERSION,
            "mizu_version": CHECK.__version__ if hasattr(CHECK, "__version__") else __import__("mizu").__version__,
            "source_sha256": sha,
            "allowed_not_run": sorted(INSTALLER.staging_not_run_checks(core_only=True).split(",")),
            "bash_available": bash_available,
            "node_version": INSTALLER.current_node_version(),
            "ok": True,
            "python": sys.version.split()[0],
            "os": sys.platform,
            "checks": [
                {"name": "python-syntax", "status": "pass"},
                {"name": "python-unit-and-contract-tests", "status": "pass"},
                {"name": "node-transport-and-extension-tests", "status": "pass"},
                {"name": "source-manifest-stable", "status": "pass"},
                {"name": "node-durable-contract-tests", "status": "not_run"},
                {"name": "node-pi-sdk-contract-tests", "status": "not_run"},
                {"name": "node-pi-reasoning-replay-tests", "status": "not_run"},
            ] + ([{"name": "shell-syntax:" + path.name, "status": "pass"}
                  for path in (ROOT / "scripts").glob("*.sh")]
                 if bash_available else
                 [{"name": "shell-syntax", "status": "not_run"}])
              + [{"name": "javascript-syntax:" + path.relative_to(ROOT).as_posix(), "status": "pass"}
                 for directory in ("adapters/pi", "adapters/pi-durable", "scripts")
                 for path in (ROOT / directory).glob("*.mjs")],
        }

    def test_receipt_identity_uses_shared_manifest(self):
        from mizu.distribution import OFFLINE_REPORT_VERSION as expected_version
        from mizu.distribution import source_manifest as shared_manifest
        from mizu.distribution import source_manifest_sha
        self.assertEqual(shared_manifest(ROOT), INSTALLER.source_manifest())
        self.assertEqual(INSTALLER.OFFLINE_REPORT_VERSION if hasattr(INSTALLER, "OFFLINE_REPORT_VERSION") else expected_version,
                         expected_version)
        self.assertEqual(CHECK.OFFLINE_REPORT_VERSION, expected_version)
        report = self.valid_report()
        self.assertEqual(report["source_sha256"], source_manifest_sha(ROOT))
        entries = INSTALLER.validate_reused_source_checks(report, expected_sha=report["source_sha256"])
        self.assertEqual(entries, report["checks"])

    def test_valid_reuse_succeeds(self):
        report = self.valid_report()
        entries = INSTALLER.validate_reused_source_checks(report, expected_sha=report["source_sha256"])
        self.assertEqual(entries, report["checks"])

    def test_malformed_reports_rejected(self):
        report = self.valid_report()
        sha = report["source_sha256"]
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks([], expected_sha=sha)
        bad = dict(report, suite="other")
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(bad, expected_sha=sha)
        bad = dict(report, checks=[{"name": "python-syntax"}])
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(bad, expected_sha=sha)

    def test_stale_schema_version_and_source_rejected(self):
        report = self.valid_report()
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, report_version=999),
                                                    expected_sha=report["source_sha256"])
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, mizu_version="0.0.0-stale"),
                                                    expected_sha=report["source_sha256"])
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, source_sha256="0" * 64),
                                                    expected_sha=report["source_sha256"])

    def test_failed_and_incomplete_reports_rejected(self):
        report = self.valid_report()
        sha = report["source_sha256"]
        failed = dict(report, ok=False)
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(failed, expected_sha=sha)
        with_fail = dict(report, ok=True,
                          checks=[{**entry, "status": "fail"} if entry["name"] == "python-syntax" else entry
                                  for entry in report["checks"]])
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(with_fail, expected_sha=sha)
        missing_required = dict(report, checks=[e for e in report["checks"]
                                                if e["name"] != "python-unit-and-contract-tests"])
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(missing_required, expected_sha=sha)
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, checks=[]), expected_sha=sha)

    def test_wrong_environment_rejected(self):
        report = self.valid_report()
        sha = report["source_sha256"]
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, python="0.0.0"), expected_sha=sha)
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, os="mismatched-os"), expected_sha=sha)
        with self.assertRaises(INSTALLER.Denied):
            INSTALLER.validate_reused_source_checks(dict(report, node_version="v0.0.0"), expected_sha=sha)
        with mock.patch.object(INSTALLER, "functional_bash_available", return_value=not report["bash_available"]):
            with self.assertRaises(INSTALLER.Denied):
                INSTALLER.validate_reused_source_checks(report, expected_sha=sha)

    def test_exact_requested_waiver_set_required(self):
        report = self.valid_report()
        expected = report["allowed_not_run"]
        for allowed in (None, [], expected[:-1], expected + ["other"],
                        list(reversed(expected)), expected + [expected[0]], ",".join(expected)):
            with self.subTest(allowed=allowed), self.assertRaises(INSTALLER.Denied):
                INSTALLER.validate_reused_source_checks(dict(report, allowed_not_run=allowed),
                                                        expected_sha=report["source_sha256"])

    def test_passing_sdk_gates_accepted_with_same_requested_waivers(self):
        report = self.valid_report()
        for gate in INSTALLER.SDK_WAIVABLE_GATE_NAMES:
            with self.subTest(gate=gate):
                passing = dict(report, checks=[{**e, "status": "pass"} if e["name"] == gate else e
                                               for e in report["checks"]])
                self.assertEqual(INSTALLER.validate_reused_source_checks(
                    passing, expected_sha=report["source_sha256"]), passing["checks"])

    def test_missing_duplicate_or_nonpassing_required_gates_rejected(self):
        report = self.valid_report()
        for entry in report["checks"]:
            name = entry["name"]
            for checks in ([e for e in report["checks"] if e["name"] != name],
                           report["checks"] + [entry],
                           [{**e, "status": "not_run"} if e["name"] == name else e
                            for e in report["checks"]] if entry["status"] == "pass" else []):
                with self.subTest(name=name, checks=checks), self.assertRaises(INSTALLER.Denied):
                    INSTALLER.validate_reused_source_checks(dict(report, checks=checks),
                                                            expected_sha=report["source_sha256"])

    def test_unsupported_not_run_rejected(self):
        report = self.valid_report()
        for name in ("node-extra-sdk-tests", "other-check", "javascript-syntax:extra.mjs"):
            with self.subTest(name=name), self.assertRaises(INSTALLER.Denied):
                INSTALLER.validate_reused_source_checks(
                    dict(report, checks=report["checks"] + [{"name": name, "status": "not_run"}]),
                    expected_sha=report["source_sha256"])

    def test_shell_syntax_inventory_requires_functional_bash_status(self):
        report = self.valid_report()
        other_status = not report["bash_available"]
        with mock.patch.object(INSTALLER, "functional_bash_available", return_value=other_status):
            with self.assertRaises(INSTALLER.Denied):
                INSTALLER.validate_reused_source_checks(report, expected_sha=report["source_sha256"])
        if report["bash_available"]:
            report["checks"] = [entry for entry in report["checks"]
                                 if not entry["name"].startswith("shell-syntax:")]
            with self.assertRaises(INSTALLER.Denied):
                INSTALLER.validate_reused_source_checks(report, expected_sha=report["source_sha256"])
        else:
            report["bash_available"] = True
            report["checks"] = [entry for entry in report["checks"] if entry["name"] != "shell-syntax"] + [
                {"name": "shell-syntax:" + path.name, "status": "pass"}
                for path in (ROOT / "scripts").glob("*.sh")]
            with mock.patch.object(INSTALLER, "functional_bash_available", return_value=True):
                self.assertEqual(INSTALLER.validate_reused_source_checks(
                    report, expected_sha=report["source_sha256"]), report["checks"])
                report["checks"].pop()
                with self.assertRaises(INSTALLER.Denied):
                    INSTALLER.validate_reused_source_checks(report, expected_sha=report["source_sha256"])

    def test_source_manifest_stability_and_canonical_waivers(self):
        # Fake passing subprocesses isolate the real manifest/check/report path.
        for change in ("stable", "mutate", "unreadable"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "README.md"
                source.write_text("before", encoding="utf-8")
                # Both adapters have launcher.mjs: syntax names must be unique.
                for directory in ("adapters/pi", "adapters/pi-durable"):
                    (root / directory).mkdir(parents=True)
                    (root / directory / "launcher.mjs").write_text("", encoding="utf-8")
                report_path = root / "report.json"
                initial_sha = CHECK.source_manifest_sha(root)
                real_manifest_sha = CHECK.source_manifest_sha
                calls = []

                def manifest_sha(path):
                    calls.append(path)
                    if change == "unreadable" and len(calls) == 2:
                        raise OSError("unreadable fixture")
                    return real_manifest_sha(path)

                def spawn(command, **kwargs):
                    if change == "mutate":
                        source.write_text("after", encoding="utf-8")
                    return mock.Mock(stdout=io.StringIO(""), wait=mock.Mock(return_value=0))

                waivers = sorted(INSTALLER.staging_not_run_checks(core_only=True).split(","))
                with mock.patch.object(CHECK, "ROOT", root), \
                        mock.patch.object(INSTALLER, "ROOT", root), \
                        mock.patch.object(CHECK.sys, "argv", ["check.py", "--report", str(report_path),
                            "--allow-not-run", ", ".join(reversed(waivers)) + "," + waivers[0]]), \
                        mock.patch.object(CHECK, "source_manifest_sha", side_effect=manifest_sha), \
                        mock.patch.object(CHECK, "node_version", return_value=INSTALLER.current_node_version()), \
                        mock.patch.object(INSTALLER, "functional_bash_available", return_value=False), \
                        mock.patch.object(CHECK, "spawn", side_effect=spawn), \
                        mock.patch.object(CHECK, "terminate_process"), \
                        mock.patch.object(CHECK.shutil, "which", return_value="node"), \
                        mock.patch.object(CHECK, "pi_sdk_deps_status", return_value=("absent", "fixture")), \
                        mock.patch.object(CHECK, "bash_available", return_value=False), \
                        mock.patch.object(CHECK.sys, "stdout", io.StringIO()):
                    self.assertEqual(CHECK.main(), 0 if change == "stable" else 1)
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    self.assertEqual(calls, [root, root])
                    self.assertEqual(report["source_sha256"], initial_sha)
                    self.assertEqual(report["allowed_not_run"], waivers)
                    stable = next(e for e in report["checks"] if e["name"] == "source-manifest-stable")
                    self.assertEqual(stable["status"], "pass" if change == "stable" else "fail")
                    self.assertEqual(report["ok"], change == "stable")
                    if change == "stable":
                        INSTALLER.validate_reused_source_checks(report, expected_sha=initial_sha)
                    else:
                        with self.assertRaises(INSTALLER.Denied):
                            INSTALLER.validate_reused_source_checks(report, expected_sha=initial_sha)
                        # Even a forged top-level ok cannot hide the failed stability check.
                        with self.assertRaises(INSTALLER.Denied):
                            INSTALLER.validate_reused_source_checks(dict(report, ok=True), expected_sha=initial_sha)

    def test_reuse_requires_core_only(self):
        import argparse
        report = self.valid_report()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "offline.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            args = argparse.Namespace(core_only=False, reuse_source_checks=path,
                                      prefix=Path(temporary) / "prefix",
                                      bin_dir=Path(temporary) / "bin",
                                      config=Path(temporary) / "config.toml",
                                      allow_install_scripts=False, stage_only=True,
                                      activate=False, allow_mode_change=False)
            with mock.patch.object(INSTALLER._platform, "is_root", return_value=False):
                with self.assertRaises(INSTALLER.Denied):
                    INSTALLER.stage(args)

    def _stage_with_fake_gate(self, *, reuse_path=None):
        import argparse
        manifest = {"a.py": {"sha256": "0" * 64, "executable": False}}
        args = argparse.Namespace(core_only=True, reuse_source_checks=reuse_path,
                                  prefix=Path(tempfile.mkdtemp()),
                                  bin_dir=Path(tempfile.mkdtemp()),
                                  config=Path(tempfile.mkdtemp()) / "config.toml",
                                  allow_install_scripts=False, stage_only=True,
                                  activate=False, allow_mode_change=False)
        for directory in (args.prefix, args.bin_dir, args.config.parent):
            self.addCleanup(shutil.rmtree, directory)
        calls = []

        def fake_command(argv, *, cwd=None, timeout=None):
            calls.append(list(map(str, argv)))
            target = Path(cwd) / "installation-checks.json"
            target.write_text(json.dumps({"checks": self.valid_report()["checks"]}), encoding="utf-8")

        with mock.patch.object(INSTALLER._platform, "is_root", return_value=False), \
                mock.patch.object(INSTALLER, "source_manifest", return_value=manifest), \
                mock.patch.object(INSTALLER, "source_files", return_value=[]), \
                mock.patch.object(INSTALLER, "command", side_effect=fake_command):
            result = INSTALLER.stage(args)
        release = Path(result["release"])
        receipt = json.loads((release / "installation.json").read_text(encoding="utf-8"))
        return calls, receipt

    def test_standalone_path_still_runs_staged_gate(self):
        calls, receipt = self._stage_with_fake_gate(reuse_path=None)
        self.assertTrue(any("scripts/check.py" in part for command in calls for part in command),
                        "standalone staging must run the full staged gate")
        self.assertEqual(receipt["checks_provenance"], "staged-gate")
        self.assertEqual(receipt["validation"], "local-install-checks-passed")

    def test_explicit_report_read_fails_closed(self):
        for kind in ("missing", "directory", "malformed", "encoding", "oversize", "read-error"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "offline.json"
                if kind == "directory":
                    path.mkdir()
                elif kind != "missing":
                    path.write_bytes({"malformed": b"{", "encoding": b"\xff",
                                      "oversize": b" " * (1024 * 1024 + 1)}.get(kind, b"{}"))
                patch = (mock.patch.object(Path, "open", side_effect=OSError("fixture read error"))
                         if kind == "read-error" else nullcontext())
                with patch, self.assertRaises(INSTALLER.Denied):
                    self._stage_with_fake_gate(reuse_path=path)

    def test_reuse_path_skips_staged_gate_and_records_provenance(self):
        import argparse
        report = self.valid_report()
        # Bind the fabricated report to the fake staged manifest used below.
        manifest = {"a.py": {"sha256": "0" * 64, "executable": False}}
        from mizu.fs import canonical, digest
        sha = digest(canonical(manifest))
        report = {**report, "source_sha256": sha}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "offline.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            calls, receipt = self._stage_with_fake_gate(reuse_path=path)
        self.assertEqual(calls, [], "reuse must not run the nested staged gate")
        self.assertEqual(receipt["checks_provenance"], "reused-source-gate")
        self.assertEqual(receipt["checks"], report["checks"])
        self.assertEqual(receipt["validation"], "local-install-checks-passed")

    def test_ci_passes_prior_report_explicitly_and_default_stays_staged(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("./scripts/test-install.sh private-validation/offline.json", workflow)
        script = (ROOT / "scripts" / "test-install.sh").read_text(encoding="utf-8")
        self.assertIn('EXPECTED_PROVENANCE="staged-gate"', script)
        self.assertIn('EXPECTED_PROVENANCE="reused-source-gate"', script)
        self.assertIn("--reuse-source-checks", script)
        # No implicit default: the installer only reuses with the explicit flag.
        self.assertNotIn(":-private-validation", script)
        self.assertNotIn("=private-validation", script)
        self.assertNotIn('REUSE_ARGS=("private', script)

if __name__ == "__main__":
    unittest.main()

class FailFastChildTests(unittest.TestCase):
    def test_first_failure_stops_later_tests_and_retains_private_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tests').mkdir()
            (root/'tests/test_sample.py').write_text(
                'import unittest\nfrom pathlib import Path\n'
                'class Sample(unittest.TestCase):\n'
                '    def test_a_fail(self): self.fail("fixture_secret")\n'
                '    def test_b_later(self): Path("later").touch()\n',encoding='utf-8')
            sidecar=root/'failures.jsonl'
            child=subprocess.run([sys.executable,str(ROOT/'scripts/check_unittest.py'),str(sidecar),'--fail-fast'],cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(child.returncode,1)
            self.assertFalse((root/'later').exists())
            self.assertIn('Ran 1 test',child.stderr)
            self.assertNotIn('fixture_secret',child.stderr+sidecar.read_text())
            self.assertIn('test_a_fail',sidecar.read_text())
