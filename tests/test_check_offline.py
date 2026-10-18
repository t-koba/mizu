"""Offline gate preflight and structured, privacy-safe unittest diagnostics."""
from contextlib import nullcontext
from functools import partial
import importlib.util
import io
import json
import os
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
        self.assertEqual(child_timeout, 1800)
        self.assertEqual(CHECK._unittest_timeout_seconds('posix'), 1000)
        self.assertEqual(CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS, 600)
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 45 || 20 }}", workflow)
        # Exercise the reviewer's 60-second setup case, one later execute timeout,
        # both bounded cleanup waits, process-group termination (1 s grace + 5 s
        # reap), and the suffix/receipt reserve: 2646 s total, leaving 54 s slack.
        required = (60 + child_timeout + CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS
                    + 2 * CHECK.PROCESS_CLEANUP_TIMEOUT_SECONDS + 6 + 120)
        self.assertGreaterEqual(45 * 60, required)

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
                    mock.patch.object(CHECK, '_unittest_timeout_seconds', return_value=10), \
                    mock.patch.object(CHECK, 'PROCESS_CLEANUP_TIMEOUT_SECONDS', 1), \
                    mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                    mock.patch.object(CHECK, 'bash_available', return_value=False):
                started = time.monotonic()
                gate.start()
                try:
                    self.assertTrue(progress.wait(8), 'confirmed descendant progress must stream live')
                    if timed_out:
                        self.assertTrue(gate.is_alive(), 'progress must arrive before the timeout')
                    gate.join(timeout=17)
                    elapsed = time.monotonic() - started
                    self.assertFalse(gate.is_alive(), 'gate blocked on inherited stdout')
                    self.assertLess(elapsed, 25, '10 s child cap plus conservative cleanup/startup margin')
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
                        self.assertIn('timed out after 10 s', entry['details'])
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
                                  / 'windows-test-timings.json').exists())
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
            artifact = Path(temporary) / 'windows-test-timings.json'
            CHECK.write_timings_artifact(artifact, tests=[], status='fail', exit_code=1,
                                          elapsed_seconds=2.5)
            payload = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(payload['suite'], 'windows-test-timings')
            self.assertEqual(payload['tests'], [])
            self.assertEqual(payload['unittest_elapsed_seconds'], 2.5)
            self.assertNotIn('details', json.dumps(payload))
            self.assertNotIn('diagnostic', json.dumps(payload).lower())

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
        upload = workflow.split('name: Upload Windows per-test timings', 1)[1].split('  durable-contract:', 1)[0]
        self.assertEqual([line.strip() for line in upload.splitlines() if line.strip().startswith('path:')],
                         ['path: private-validation/windows-test-timings.json'])
        self.assertIn('private-validation/windows-test-timings.json', workflow)
        self.assertIn('retention-days: 7', workflow)
        self.assertIn('if-no-files-found: warn', workflow)
        self.assertIn("if: always() && runner.os == 'Windows' && matrix.python == '3.13'", workflow)
        # Budgets unchanged; no scope amendment claimed here.
        self.assertIn('1800', workflow)
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 45 || 20 }}", workflow)
        self.assertEqual(CHECK._unittest_timeout_seconds('nt'), 1800)

    def test_timings_artifact_survives_timeout_with_only_evidence(self):
        raw = ('test_sample.SampleTests.test_slow ... ok\nRan 1 test in 0.1s\nOK\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / 'report.json'
            artifact = root / 'private-validation' / 'windows-test-timings.json'

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
            self.assertEqual(payload['suite'], 'windows-test-timings')
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
            # Only sanitized IDs, durations, completion, and minimal metadata.
            self.assertEqual(set(payload), {'suite', 'mizu_version', 'python', 'os',
                                            'unittest_timeout_seconds', 'unittest_elapsed_seconds',
                                            'unittest_status',
                                            'unittest_exit_code', 'tests'})
            for entry in payload['tests']:
                self.assertEqual(set(entry), {'test', 'duration_seconds', 'completed'})


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


if __name__ == "__main__":
    unittest.main()
