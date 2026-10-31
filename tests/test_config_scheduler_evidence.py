"""Separate diagnostic budgets: strict ranges and fragment loading."""
import dataclasses

from support import Fixture
from mizu.config import (MAX_USAGE_POLICY_COUNT, TOML_INTEGER_MAX,
                         SchedulerEvidence, load)
from mizu.errors import ConfigError


class SchedulerEvidenceConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        expected = dict(candidate_entries=5000, files=200, scan_seconds=2)
        self.assertEqual(dataclasses.asdict(self.config.scheduler_evidence), expected)
        self.assertEqual(dataclasses.asdict(SchedulerEvidence()), expected)
        original = self.file.read_text()
        for field, default in expected.items():
            maximum = TOML_INTEGER_MAX if field == "scan_seconds" else MAX_USAGE_POLICY_COUNT
            accepted = tuple(map(str, (1, default, default + 1, maximum)))
            for value in (*accepted, '0', '-1', str(maximum + 1),
                          'true', 'false', '1.5', '"1"', '[]', '{}'):
                with self.subTest(field=field, value=value):
                    self.file.write_text(original + f'\n[scheduler_evidence]\n{field} = {value}\n')
                    if value in accepted:
                        self.assertEqual(dataclasses.asdict(load(self.file).scheduler_evidence),
                                         {**expected, field: int(value)})
                    else:
                        with self.assertRaisesRegex(ConfigError, f'scheduler_evidence.{field}'):
                            load(self.file)

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[scheduler_evidence]\nfile = 1\n',
                     'scheduler_evidence = 1\n' + original,
                     'scheduler_evidence = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'scheduler_evidence'):
                    load(self.file)
        (self.file.parent / 'evidence.toml').write_text(
            '[scheduler_evidence]\ncandidate_entries = 7\nfiles = 3\nscan_seconds = 4\n')
        self.file.write_text('include = ["evidence.toml"]\n' + original)
        self.assertEqual(load(self.file).scheduler_evidence,
                         SchedulerEvidence(candidate_entries=7, files=3, scan_seconds=4))
