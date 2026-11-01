"""Publication-receipt post-listing budgets: strict ranges and fragment loading."""
import dataclasses

from support import Fixture
from mizu.config import MAX_USAGE_POLICY_COUNT, PublicationEvidence, load
from mizu.errors import ConfigError


class PublicationEvidenceConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        expected = dict(name_processing_limit=10000, record_limit=1000)
        self.assertEqual(dataclasses.asdict(self.config.publication_evidence), expected)
        self.assertEqual(dataclasses.asdict(PublicationEvidence()), expected)
        original = self.file.read_text()
        for field, default in expected.items():
            maximum = MAX_USAGE_POLICY_COUNT
            accepted = tuple(map(str, (1, default, default + 1, maximum)))
            for value in (*accepted, '0', '-1', str(maximum + 1),
                          'true', 'false', '1.5', '"1"', '[]', '{}'):
                with self.subTest(field=field, value=value):
                    self.file.write_text(original + f'\n[publication_evidence]\n{field} = {value}\n')
                    if value in accepted:
                        self.assertEqual(dataclasses.asdict(load(self.file).publication_evidence),
                                         {**expected, field: int(value)})
                    else:
                        with self.assertRaisesRegex(ConfigError, f'publication_evidence.{field}'):
                            load(self.file)

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[publication_evidence]\nfile = 1\n',
                     'publication_evidence = 1\n' + original,
                     'publication_evidence = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'publication_evidence'):
                    load(self.file)
        (self.file.parent / 'evidence.toml').write_text(
            '[publication_evidence]\nname_processing_limit = 7\nrecord_limit = 3\n')
        self.file.write_text('include = ["evidence.toml"]\n' + original)
        self.assertEqual(load(self.file).publication_evidence,
                         PublicationEvidence(name_processing_limit=7, record_limit=3))
