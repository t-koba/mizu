"""Limits validation and settings-table coverage, not a code-only bounds inventory."""
import dataclasses
from pathlib import Path
import re
import unittest

from support import Fixture
from mizu.config import (MAX_CHECK_RETAINED, MAX_DECISION_PROJECTION_RECORDS,
                         MAX_HISTORY_TEMPORARIES, Limits, ROOT_KEYS, _LIMIT_RANGES, load)
from mizu.errors import ConfigError


class LimitsCoverageTests(unittest.TestCase):
    def test_every_field_has_explicit_validation(self):
        # These fields use the boolean, progress-signal or finite-cost validators
        # in load(), rather than the integer range table.
        special = {"lock_diagnostics", "drain_yield", "missing_finish_counts_as_failure",
                   "progress_signals", "session_max_cost_usd"}
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


if __name__ == "__main__":
    unittest.main()
