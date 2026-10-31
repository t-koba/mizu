"""Dispatch retention and samples: strict ranges and fragment loading."""
import dataclasses

from support import Fixture
from mizu.config import (MAX_DISPATCH_EVIDENCE_BYTES, MAX_DISPATCH_EVIDENCE_COUNT,
                         MIN_DISPATCH_EVIDENCE_BYTES, DispatchEvidence, load)
from mizu.errors import ConfigError


class DispatchEvidenceConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        expected = dict(max_attempts=64, max_bytes=262144, max_models=16, unknown_keys=20)
        self.assertEqual(dataclasses.asdict(self.config.dispatch_evidence), expected)
        self.assertEqual(dataclasses.asdict(DispatchEvidence()), expected)
        original = self.file.read_text()
        for field, default in expected.items():
            minimum = (MIN_DISPATCH_EVIDENCE_BYTES if field == "max_bytes" else
                       1 if field == "max_attempts" else 0)
            maximum = MAX_DISPATCH_EVIDENCE_BYTES if field == "max_bytes" else MAX_DISPATCH_EVIDENCE_COUNT
            accepted = tuple(map(str, (minimum, default, default + 1, maximum)))
            for value in (*accepted, str(minimum - 1), str(maximum + 1),
                          'true', 'false', '1.5', '"1"', '[]', '{}'):
                with self.subTest(field=field, value=value):
                    self.file.write_text(original + f'\n[dispatch_evidence]\n{field} = {value}\n')
                    if value in accepted:
                        self.assertEqual(dataclasses.asdict(load(self.file).dispatch_evidence),
                                         {**expected, field: int(value)})
                    else:
                        with self.assertRaisesRegex(ConfigError, f'dispatch_evidence.{field}'):
                            load(self.file)

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[dispatch_evidence]\nfile = 1\n',
                     'dispatch_evidence = 1\n' + original,
                     'dispatch_evidence = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'dispatch_evidence'):
                    load(self.file)
        (self.file.parent / 'evidence.toml').write_text(
            '[dispatch_evidence]\nmax_attempts = 7\nmax_bytes = 1024\nmax_models = 3\nunknown_keys = 4\n')
        self.file.write_text('include = ["evidence.toml"]\n' + original)
        self.assertEqual(load(self.file).dispatch_evidence,
                         DispatchEvidence(max_attempts=7, max_bytes=1024, max_models=3, unknown_keys=4))
