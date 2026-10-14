"""Offline gate preflight: durable deps probe. Offline only."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_check():
    spec = importlib.util.spec_from_file_location('check_gate', ROOT / 'scripts/check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load_check()


def write_adapter(base: Path, deps, installed: dict):
    (base).mkdir(parents=True, exist_ok=True)
    (base / 'package.json').write_text(json.dumps({"dependencies": deps}), encoding='utf-8')
    for name, version in installed.items():
        target = base / 'node_modules' / name
        target.mkdir(parents=True, exist_ok=True)
        (target / 'package.json').write_text(json.dumps({"version": version}), encoding='utf-8')
    return base


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
    def test_failure_summary_keeps_test_and_repo_location_but_redacts_values(self):
        output = r"""======================================================================
FAIL: test_expected_result (tests.test_example.ExampleTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\a\mizu\mizu\tests\test_example.py", line 42, in test_expected_result
    self.assertEqual(actual, expected)
AssertionError: private_fixture_value

----------------------------------------------------------------------
Ran 1 test in 0.1s
FAILED (failures=1)
"""
        summary = CHECK.unittest_failure_summary(output)
        self.assertIn('FAIL test_expected_result (tests.test_example.ExampleTests)', summary)
        self.assertIn('tests/test_example.py:42', summary)
        self.assertIn('[AssertionError]', summary)
        self.assertNotIn('D:/a/', summary)
        self.assertNotIn('private_fixture_value', summary)

    def test_exception_types_without_common_suffix_are_reported(self):
        output = """ERROR: test_exit (tests.test_example.ExampleTests)
Traceback (most recent call last):
  File "tests/test_example.py", line 10, in test_exit
SystemExit: 2

ERROR: test_stop (tests.test_example.ExampleTests)
Traceback (most recent call last):
  File "tests/test_example.py", line 20, in test_stop
StopIteration
"""
        summary = CHECK.unittest_failure_summary(output)
        self.assertIn('[SystemExit]', summary)
        self.assertIn('[StopIteration]', summary)

    def test_multiline_exception_value_is_not_reported_as_a_type(self):
        output = """ERROR: test_multiline (tests.test_example.ExampleTests)
Traceback (most recent call last):
  File "tests/test_example.py", line 30, in test_multiline
    raise ValueError('safe context')
ValueError: safe context
private_fixture_value
"""
        summary = CHECK.unittest_failure_summary(output)
        self.assertIn('[ValueError]', summary)
        self.assertNotIn('private_fixture_value', summary)

    def test_error_and_failure_names_are_both_reported(self):
        output = """ERROR: test_error (tests.test_example.ExampleTests)
Traceback (most recent call last):
  File "tests/test_example.py", line 10, in test_error
OSError: example

FAIL: test_failure (tests.test_example.ExampleTests)
Traceback (most recent call last):
  File "tests/test_example.py", line 20, in test_failure
AssertionError: example
"""
        summary = CHECK.unittest_failure_summary(output)
        self.assertIn('ERROR test_error (tests.test_example.ExampleTests)', summary)
        self.assertIn('FAIL test_failure (tests.test_example.ExampleTests)', summary)


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
