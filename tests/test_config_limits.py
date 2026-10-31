"""Limits validation and settings-table coverage, not a code-only bounds inventory."""
import dataclasses
from pathlib import Path
import re
import unittest

from mizu.config import Limits, _LIMIT_RANGES


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


if __name__ == "__main__":
    unittest.main()
