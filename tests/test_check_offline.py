"""Offline gate preflight and structured, privacy-safe unittest diagnostics."""
from functools import partial
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
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


def write_adapter(base: Path, deps, installed: dict):
    (base).mkdir(parents=True, exist_ok=True)
    (base / 'package.json').write_text(json.dumps({"dependencies": deps}), encoding='utf-8')
    for name, version in installed.items():
        target = base / 'node_modules' / name
        target.mkdir(parents=True, exist_ok=True)
        (target / 'package.json').write_text(json.dumps({"version": version}), encoding='utf-8')
    return base


class CheckTimeoutTests(unittest.TestCase):
    def test_windows_budget_covers_setup_child_timeout_and_receipt_path(self):
        child_timeout = CHECK._unittest_timeout_seconds('nt')
        self.assertEqual(child_timeout, 1450)
        self.assertEqual(CHECK._unittest_timeout_seconds('posix'), 1000)
        self.assertEqual(CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS, 600)
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')
        self.assertIn("timeout-minutes: ${{ matrix.os == 'windows-latest' && 40 || 20 }}", workflow)
        # Exercise the reviewer's 60-second setup case, one later execute timeout,
        # both bounded cleanup waits, and time for remaining checks/receipt.
        required = (60 + child_timeout + CHECK.DEFAULT_CHECK_TIMEOUT_SECONDS
                    + 2 * CHECK.PROCESS_CLEANUP_TIMEOUT_SECONDS + 120)
        self.assertGreaterEqual(40 * 60, required)


class DurableDepsProbeTests(unittest.TestCase):
    def test_ready_when_pinned_versions_installed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {"left-pad": "1.3.0"})
            self.assertEqual(CHECK.durable_deps_status(adapter), ("ready", ""))

    def test_absent_when_nothing_installed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {})
            state, details = CHECK.durable_deps_status(adapter)
            self.assertEqual(state, "absent")
            self.assertIn("left-pad@1.3.0", details)

    def test_mismatch_when_version_differs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            adapter = write_adapter(Path(td), {"left-pad": "1.3.0"}, {"left-pad": "9.9.9"})
            state, details = CHECK.durable_deps_status(adapter)
            self.assertEqual(state, "mismatch")
            self.assertIn("9.9.9", details)

    def test_mismatch_when_manifest_unreadable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / 'package.json').write_text("not json", encoding='utf-8')
            state, _ = CHECK.durable_deps_status(base)
            self.assertEqual(state, "mismatch")

    def test_real_adapter_reports_ready_where_provisioned(self):
        # Environment-coupled by nature: where the operator provisioned the
        # pinned SDK the probe must agree; elsewhere it skips instead of
        # failing the gate for provisioning. A version mismatch still fails:
        # wrong versions are a contract violation, not missing provisioning.
        state, details = CHECK.durable_deps_status(ROOT / 'adapters/pi-durable')
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
                        self.path = Path(command[-1])
                        paths.append(self.path)
                        if metadata:
                            self.path.write_text(json.dumps({
                                'kind': 'ERROR', 'test': 'tests.RealTests.test_real',
                                'type': 'RuntimeError', 'path': 'tests/test_check_offline.py', 'line': 42,
                            }) + '\n', encoding='utf-8')

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def wait(self, timeout):
                        budgets.append(timeout)
                        if timed_out and not self.killed:
                            raise subprocess.TimeoutExpired(self.command, timeout)
                        return -9 if self.killed else 1

                    def kill(self):
                        self.killed = True

                output = io.StringIO()
                with mock.patch.object(CHECK.sys, 'argv', ['check.py', '--report', str(report)]), \
                        mock.patch.object(CHECK.subprocess, 'Popen', FakeProcess), \
                        mock.patch.object(CHECK.shutil, 'which', return_value=None), \
                        mock.patch.object(CHECK, 'bash_available', return_value=False), \
                        mock.patch.object(CHECK.sys, 'stdout', output):
                    self.assertEqual(CHECK.main(), 1)
                receipt = json.loads(report.read_text(encoding='utf-8'))
                entry = next(c for c in receipt['checks'] if c['name'] == 'python-unit-and-contract-tests')
                details = entry['details']
                self.assertEqual(entry['status'], 'fail')
                timeout = CHECK._unittest_timeout_seconds()
                self.assertEqual(budgets[0], timeout)
                self.assertEqual(entry['tests'], {'run': 3, 'failed': 3, 'skipped': 1})
                self.assertFalse(paths[0].parent.exists(), 'private sidecar directory must be cleaned')
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


class DurableGateEntryTests(unittest.TestCase):
    NAME = 'node-durable-contract-tests'

    def test_ready_runs_the_suite(self):
        self.assertIsNone(CHECK.durable_gate_entry("ready", "", frozenset()))

    def test_absent_fails_closed_by_default(self):
        entry = CHECK.durable_gate_entry("absent", "left-pad@1.3.0 is not installed", frozenset())
        self.assertEqual(entry["status"], "fail")
        self.assertIn("not installed", entry["details"])

    def test_absent_waived_records_not_run(self):
        entry = CHECK.durable_gate_entry("absent", "left-pad@1.3.0 is not installed", frozenset({self.NAME}))
        self.assertEqual(entry["status"], "not_run")
        self.assertIn("waived", entry["details"])

    def test_mismatch_fails_even_when_waived(self):
        entry = CHECK.durable_gate_entry("mismatch", "left-pad installed at 9.9.9", frozenset({self.NAME}))
        self.assertEqual(entry["status"], "fail")

    def test_unknown_waiver_name_does_not_waive(self):
        entry = CHECK.durable_gate_entry("absent", "not installed", frozenset({"other-suite"}))
        self.assertEqual(entry["status"], "fail")

    def test_allow_list_parsing(self):
        self.assertEqual(CHECK._allow_list(""), frozenset())
        self.assertEqual(CHECK._allow_list("a, b ,,a"), frozenset({"a", "b"}))


if __name__ == "__main__":
    unittest.main()
