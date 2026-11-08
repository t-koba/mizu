"""Limits validation and settings-table coverage, not a code-only bounds inventory."""
import dataclasses
import json
import threading
from pathlib import Path
import re
import unittest

from support import Fixture
from mizu.config import (DEFAULT_INSIGHT_HISTORY_ENTRIES, DEFAULT_PROBE_EXECUTABLE_BYTES,
                         MAX_CHECK_RETAINED,
                         MAX_DECISION_PROJECTION_RECORDS, MAX_DECISION_RECEIPT_DIAGNOSTICS,
                         MAX_HISTORY_TEMPORARIES, MAX_INSIGHT_HISTORY_ENTRIES,
                         MAX_QUEUE_OBSERVATIONS, MAX_DECISION_RECONCILIATION_BYTES,
                         MAX_PROJECT_GOAL_BYTES, TOML_INTEGER_MAX, MAX_STORAGE_RUN_ENTRIES,
                         Limits, ROOT_KEYS, _LIMIT_RANGES, load)
from mizu.errors import ConfigError
from mizu.project import Project
from mizu import fs, platform


class InsightHistoryLimitConfigTests(Fixture):
    def test_default_override_range_and_project_wiring(self):
        self.assertEqual(MAX_INSIGHT_HISTORY_ENTRIES, 1000)
        self.assertEqual(DEFAULT_INSIGHT_HISTORY_ENTRIES, 1000)
        self.assertEqual(Limits().insight_history_entries, 1000)
        self.assertEqual(_LIMIT_RANGES["insight_history_entries"], (1, 1000))
        base = re.sub(r'(?m)^insight_history_entries = .*\n', '', self.file.read_text())
        self.file.write_text(base)
        self.assertEqual(load(self.file).limits.insight_history_entries, 1000)
        for value in ("1", "500", "1000", "0", "1001", "-1", "true", "1.5", "\"1\"", "[]", "{}"):
            with self.subTest(value=value):
                self.file.write_text(base.replace(
                    "\n[limits]\n", f"\n[limits]\ninsight_history_entries = {value}\n"))
                if value in ("1", "500", "1000"):
                    config = load(self.file)
                    self.assertEqual(config.limits.insight_history_entries, int(value))
                    self.assertEqual(Project(config, "sample").insights.history_entries, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, "limits.insight_history_entries"):
                        load(self.file)


class WorkItemReadViewCacheConfigTests(Fixture):
    def test_defaults_exact_overrides_and_refusals(self):
        defaults = {
            "work_item_read_view_snapshot_cache_bytes": 33554432,
            "work_item_read_view_file_cache_bytes": 8388608,
            "work_item_read_view_item_cache_bytes": 8388608,
            "work_item_read_view_item_cache_entries": 1024,
        }
        base = re.sub(r'(?m)^work_item_read_view_.*\n', '', self.file.read_text())
        self.file.write_text(base)
        default_limits = load(self.file).limits
        for name, default in defaults.items():
            self.assertEqual(getattr(default_limits, name), default)
            self.assertEqual(getattr(Limits(), name), default)
            self.assertEqual(_LIMIT_RANGES[name], (1, TOML_INTEGER_MAX))
            for value in ("1", "37", str(TOML_INTEGER_MAX), "0", "-1",
                          str(TOML_INTEGER_MAX + 1), "true", "false", "1.0",
                          '"1"', "[]", "{}"):
                with self.subTest(name=name, value=value):
                    self.file.write_text(base.replace(
                        "\n[limits]\n", f"\n[limits]\n{name} = {value}\n"))
                    if value in ("1", "37", str(TOML_INTEGER_MAX)):
                        self.assertEqual(getattr(load(self.file).limits, name), int(value))
                    else:
                        with self.assertRaisesRegex(ConfigError, f"limits.{name}"):
                            load(self.file)


class SessionDeltaDetailConfigTests(Fixture):
    def test_default_override_and_invalid_values(self):
        base = re.sub(r'(?m)^session_delta_details = .*\n', '', self.file.read_text())
        self.file.write_text(base)
        self.assertEqual(load(self.file).limits.session_delta_details, "elide")
        for value in ('"full"', '"elide"', '"unknown"', '"FULL"', 'true', '1', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(base.replace(
                    "\n[limits]\n", f"\n[limits]\nsession_delta_details = {value}\n"))
                if value in ('"full"', '"elide"'):
                    self.assertEqual(load(self.file).limits.session_delta_details, json.loads(value))
                else:
                    with self.assertRaisesRegex(ConfigError, "limits.session_delta_details"):
                        load(self.file)


class ProbeExecutableLimitConfigTests(Fixture):
    def test_default_override_and_fail_closed_range(self):
        self.assertEqual(DEFAULT_PROBE_EXECUTABLE_BYTES, 256 * 1024 * 1024)
        self.assertEqual(Limits().probe_executable_bytes, DEFAULT_PROBE_EXECUTABLE_BYTES)
        self.assertEqual(_LIMIT_RANGES["probe_executable_bytes"], (1, TOML_INTEGER_MAX))
        base = re.sub(r'(?m)^probe_executable_bytes = .*\n', '', self.file.read_text())
        self.file.write_text(base)
        self.assertEqual(load(self.file).limits.probe_executable_bytes,
                         DEFAULT_PROBE_EXECUTABLE_BYTES)
        for value in ("1", "268435456", str(TOML_INTEGER_MAX), "0", "-1",
                      "true", "1.5", "\"268435456\"", "[]", "{}"):
            with self.subTest(value=value):
                self.file.write_text(base.replace(
                    "\n[limits]\n", f"\n[limits]\nprobe_executable_bytes = {value}\n"))
                if value in ("1", "268435456", str(TOML_INTEGER_MAX)):
                    self.assertEqual(load(self.file).limits.probe_executable_bytes, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, "limits.probe_executable_bytes"):
                        load(self.file)


class LimitsCoverageTests(unittest.TestCase):
    def test_every_field_has_explicit_validation(self):
        # These fields use the boolean, progress-signal or finite-cost validators
        # in load(), rather than the integer range table.
        special = {"lock_diagnostics", "drain_yield", "missing_finish_counts_as_failure",
                   "writer_wait_with_ready_work", "progress_signals", "session_max_cost_usd",
                   "session_delta_details"}
        fields = {field.name for field in dataclasses.fields(Limits)}
        self.assertFalse(set(_LIMIT_RANGES) & special)
        self.assertEqual(fields, set(_LIMIT_RANGES) | special)

    def test_every_field_is_in_limits_settings_table(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
        section = text.split("\n## Limits\n", 1)[1].split("\n## ", 1)[0]
        table = section.split("| Key | Scope and description |\n|---|---|\n", 1)[1].split("\n\n", 1)[0]
        documented = {key for row in table.splitlines()
                      for key in re.findall(r"`([a-z_]+)`", row.split("|")[1])}
        self.assertEqual({field.name for field in dataclasses.fields(Limits)}, documented)


class LockTimeoutConfigTests(Fixture):
    def test_default_override_and_fail_closed_range(self):
        self.assertEqual(Limits().lock_timeout_seconds, 30)
        self.assertEqual(_LIMIT_RANGES["lock_timeout_seconds"], (0, 86400))
        original = re.sub(r'(?m)^lock_timeout_seconds = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.lock_timeout_seconds, 30)
        for value in ('0', '1', '30', '86400', '-1', '86401', 'true', 'false',
                      '1.5', 'nan', 'inf', '"30"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\nlock_timeout_seconds = {value}\n'))
                if value in ('0', '1', '30', '86400'):
                    self.assertEqual(load(self.file).limits.lock_timeout_seconds, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.lock_timeout_seconds'):
                        load(self.file)


class PipePollConfigTests(Fixture):
    def test_default_override_and_fail_closed_range(self):
        self.assertEqual(Limits().pipe_poll_interval_ms, 100)
        self.assertEqual(_LIMIT_RANGES['pipe_poll_interval_ms'], (1, 500))
        original = re.sub(r'(?m)^pipe_poll_interval_ms = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.pipe_poll_interval_ms, 100)
        for value in ('1', '100', '500', '0', '-1', '501', 'true', 'false',
                      '1.5', 'nan', 'inf', '"100"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\npipe_poll_interval_ms = {value}\n'))
                if value in ('1', '100', '500'):
                    self.assertEqual(load(self.file).limits.pipe_poll_interval_ms, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.pipe_poll_interval_ms'):
                        load(self.file)

    def configured(self, value):
        return dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, pipe_poll_interval_ms=value, lock_timeout_seconds=7,
            lock_retry_interval_ms=9))

    def test_runtime_policy_is_nested_exception_safe_and_thread_local(self):
        outer, inner = self.configured(500), self.configured(1)
        self.assertEqual(platform.current_pipe_poll_interval(), .1)
        seen = []
        def worker():
            seen.append(platform.current_pipe_poll_interval())
            with inner.runtime_policy():
                seen.append(platform.current_pipe_poll_interval())
            seen.append(platform.current_pipe_poll_interval())
        with outer.runtime_policy():
            self.assertEqual(platform.current_pipe_poll_interval(), .5)
            self.assertEqual(fs._lock_timeout.get(), 7)
            self.assertEqual(fs._lock_retry_interval_ms.get(), 9)
            with self.assertRaisesRegex(RuntimeError, 'restore'):
                with inner.runtime_policy():
                    self.assertEqual(platform.current_pipe_poll_interval(), .001)
                    raise RuntimeError('restore')
            self.assertEqual(platform.current_pipe_poll_interval(), .5)
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(platform.current_pipe_poll_interval(), .5)
        self.assertEqual(seen, [.1, .001, .1])
        self.assertEqual(platform.current_pipe_poll_interval(), .1)
        for value in (0, 501, True, 1.5, '100'):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, 'limits.pipe_poll_interval_ms'):
                with self.configured(value).runtime_policy():
                    self.fail('invalid derived config bound policy')
        self.assertEqual(platform.current_pipe_poll_interval(), .1)


class WriterWaitPolicyConfigTests(Fixture):
    def test_default_boolean_and_strict_type(self):
        self.assertIs(Limits().writer_wait_with_ready_work, True)
        original = re.sub(r'(?m)^writer_wait_with_ready_work = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertIs(load(self.file).limits.writer_wait_with_ready_work, True)
        for value in ('true', 'false', '1', '0', '"true"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\nwriter_wait_with_ready_work = {value}\n'))
                if value in ('true', 'false'):
                    self.assertIs(load(self.file).limits.writer_wait_with_ready_work,
                                  value == 'true')
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.writer_wait_with_ready_work'):
                        load(self.file)


class RootOwnerTemplateTests(unittest.TestCase):
    def test_template_names_every_accepted_root_owner(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "config/config.example.toml").read_text()
        tables = set(re.findall(r"(?m)^(?:# ?)?\[([a-z_]+)(?:\.|\])", template))
        scalars = set(re.findall(r"(?m)^(?:# ?)?([a-z_]+) =", template))
        # record_kinds is the one deliberate default-closed declaration; the
        # template explains its absence instead of showing a block.
        self.assertEqual(ROOT_KEYS - (tables | scalars), {"record_kinds"})
        self.assertIn("record_kinds absent", template)

    def test_limits_template_names_every_field(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "config/config.example.toml").read_text()
        section = template.split("\n[limits]\n", 1)[1].split("\n[", 1)[0]
        keys = set(re.findall(r"(?m)^(?:# ?)?([a-z_]+) =", section))
        self.assertEqual(sorted({field.name for field in dataclasses.fields(Limits)} - keys), [])


class ProjectGoalBytesConfigTests(Fixture):
    def test_default_and_strict_range(self):
        self.assertEqual(MAX_PROJECT_GOAL_BYTES, 1073741824)
        self.assertEqual(Limits().project_goal_bytes, 65536)
        original = re.sub(r'(?m)^project_goal_bytes = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.project_goal_bytes, 65536)
        for value in ('1', '65536', '1073741824', '0', '-1', '1073741825',
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\nproject_goal_bytes = {value}\n'))
                if value in ('1', '65536', '1073741824'):
                    self.assertEqual(load(self.file).limits.project_goal_bytes, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.project_goal_bytes'):
                        load(self.file)


class CheckRetainedConfigTests(Fixture):
    def test_default_and_strict_range(self):
        self.assertEqual(MAX_CHECK_RETAINED, 1024)
        self.assertEqual(Limits().check_retained, 1024)
        original = re.sub(r'(?m)^check_retained = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.check_retained, 1024)
        for value in ('1', '7', '1024', '0', '-1', '1025',
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace('\n[limits]\n', f'\n[limits]\ncheck_retained = {value}\n'))
                if value in ('1', '7', '1024'):
                    limits = load(self.file).limits
                    self.assertEqual(limits.check_retained, int(value))
                    self.assertEqual(limits.check_pending, 32)
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.check_retained'):
                        load(self.file)

    def test_include_can_lower_retained_target_independently(self):
        original = re.sub(r'(?m)^check_retained = .*\n', '', self.file.read_text())
        (self.file.parent / 'retention.toml').write_text('[limits]\ncheck_retained = 2\n')
        self.file.write_text('include = ["retention.toml"]\n' + original)
        limits = load(self.file).limits
        self.assertEqual(limits.check_retained, 2)
        self.assertEqual(limits.check_pending, 32)


class RunRetentionScanConfigTests(Fixture):
    def test_default_and_strict_range(self):
        self.assertEqual(MAX_STORAGE_RUN_ENTRIES, 100000)
        self.assertEqual(_LIMIT_RANGES["run_retention_scan_runs"],
                         (1, MAX_STORAGE_RUN_ENTRIES))
        self.assertEqual(Limits().run_retention_scan_runs, 5000)
        original = re.sub(r'(?m)^run_retention_scan_runs = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.run_retention_scan_runs, 5000)
        for value in ('1', '5000', '100000', '0', '-1', '100001',
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\nrun_retention_scan_runs = {value}\n'))
                if value in ('1', '5000', '100000'):
                    limits = load(self.file).limits
                    self.assertEqual(limits.run_retention_scan_runs, int(value))
                    self.assertEqual(limits.run_retention_days, 31)
                    self.assertEqual(load(self.file).usage.runs_scanned, 5000)
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.run_retention_scan_runs'):
                        load(self.file)


class HistoryTemporariesConfigTests(Fixture):
    def test_default_and_absolute_range(self):
        self.assertEqual(MAX_HISTORY_TEMPORARIES, 4096)
        self.assertEqual(Limits().history_temporaries, 64)
        original = re.sub(r'(?m)^history_temporaries = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.history_temporaries, 64)
        for value in ('1', '64', '65', '4096', '0', '-1', '4097',
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n', f'\n[limits]\nhistory_temporaries = {value}\n'))
                if value in ('1', '64', '65', '4096'):
                    self.assertEqual(load(self.file).limits.history_temporaries, int(value))
                else:
                    with self.assertRaisesRegex(ConfigError, 'limits.history_temporaries'):
                        load(self.file)


class SmokeRetentionScanConfigTests(Fixture):
    DEFAULTS = {
        "smoke_retention_projects": 5000,
        "smoke_retention_pairs": 5000,
        "smoke_retention_refs": 20000,
        "smoke_retention_entries": 100000,
        "smoke_retention_metadata_bytes": 64 * 1024 * 1024,
        "smoke_retention_report_bytes": 5000 * 16 * 1024 * 1024,
    }

    def test_defaults_and_format_bound_ranges(self):
        self.assertEqual(TOML_INTEGER_MAX, (1 << 63) - 1)
        self.assertEqual({name: getattr(Limits(), name) for name in self.DEFAULTS},
                         self.DEFAULTS)
        for name in self.DEFAULTS:
            self.assertEqual(_LIMIT_RANGES[name], (1, TOML_INTEGER_MAX))
        base = self.file.read_text()
        for name, default in self.DEFAULTS.items():
            with self.subTest(name=name):
                original = re.sub(rf'(?m)^{name} = .*\n', '', base)
                self.file.write_text(original)
                self.assertEqual(getattr(load(self.file).limits, name), default)
                for value in ('1', str(default), str(TOML_INTEGER_MAX),
                              '0', '-1', 'true', 'false', '1.5', '"1"', '[]', '{}'):
                    with self.subTest(value=value):
                        self.file.write_text(original.replace(
                            '\n[limits]\n', f'\n[limits]\n{name} = {value}\n'))
                        if value in ('1', str(default), str(TOML_INTEGER_MAX)):
                            self.assertEqual(getattr(load(self.file).limits, name), int(value))
                        else:
                            with self.assertRaisesRegex(ConfigError, f'limits.{name}'):
                                load(self.file)
                self.file.write_text(base)


class DecisionProjectionConfigTests(Fixture):
    def test_default_and_absolute_range(self):
        self.assertEqual(MAX_DECISION_PROJECTION_RECORDS, 512)
        self.assertEqual(Limits().decision_projection_records, 512)
        original = re.sub(r'(?m)^decision_projection_records = .*\n', '', self.file.read_text())
        self.file.write_text(original)
        self.assertEqual(load(self.file).limits.decision_projection_records, 512)
        for value in ('1', '512', '513', '1000000', '0', '-1',
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace(
                    '\n[limits]\n',
                    f'\n[limits]\ndecision_projection_records = {value}\n'))
                if value in ('1', '512'):
                    self.assertEqual(load(self.file).limits.decision_projection_records,
                                     int(value))
                else:
                    with self.assertRaisesRegex(ConfigError,
                                                'limits.decision_projection_records'):
                        load(self.file)


class ReconciliationPlanConfigTests(Fixture):
    def test_default_and_absolute_range(self):
        self.assertEqual(MAX_DECISION_RECONCILIATION_BYTES, 64 * 1024 * 1024)
        self.assertEqual(Limits().decision_reconciliation_bytes, 8 * 1024 * 1024)
        base = self.file.read_text()
        for name, default, ceiling in (
                ('decision_reconciliation_bytes', 8 * 1024 * 1024,
                 MAX_DECISION_RECONCILIATION_BYTES),):
            with self.subTest(name=name):
                original = re.sub(rf'(?m)^{name} = .*\n', '', base)
                self.file.write_text(original)
                self.assertEqual(getattr(load(self.file).limits, name), default)
                for value in ('1', str(default), str(ceiling), str(ceiling + 1),
                              '0', '-1', 'true', 'false', '1.5', '"1"', '[]', '{}'):
                    with self.subTest(value=value):
                        self.file.write_text(original.replace(
                            '\n[limits]\n', f'\n[limits]\n{name} = {value}\n'))
                        if value in ('1', str(default), str(ceiling)):
                            self.assertEqual(getattr(load(self.file).limits, name), int(value))
                        else:
                            with self.assertRaisesRegex(ConfigError, f'limits.{name}'):
                                load(self.file)
                self.file.write_text(base)


class DiagnosticSampleConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        self.assertEqual(MAX_DECISION_RECEIPT_DIAGNOSTICS, 100)
        self.assertEqual(MAX_QUEUE_OBSERVATIONS, 100)
        self.assertEqual(Limits().decision_receipt_diagnostics, 10)
        self.assertEqual(Limits().queue_observations, 8)
        base = self.file.read_text()
        for name, default, ceiling in (
                ('decision_receipt_diagnostics', 10, 100),
                ('queue_observations', 8, 100)):
            with self.subTest(name=name):
                original = re.sub(rf'(?m)^{name} = .*\n', '', base)
                self.file.write_text(original)
                self.assertEqual(getattr(load(self.file).limits, name), default)
                for value in ('0', str(default), str(ceiling), str(ceiling + 1),
                              '-1', 'true', 'false', '1.5', '"1"', '[]', '{}'):
                    with self.subTest(value=value):
                        self.file.write_text(original.replace(
                            '\n[limits]\n', f'\n[limits]\n{name} = {value}\n'))
                        if value in ('0', str(default), str(ceiling)):
                            self.assertEqual(getattr(load(self.file).limits, name), int(value))
                        else:
                            with self.assertRaisesRegex(ConfigError, f'limits.{name}'):
                                load(self.file)
                self.file.write_text(base)


if __name__ == "__main__":
    unittest.main()
