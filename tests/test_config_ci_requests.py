"""CI-request uncertainty policy and read budgets: strict config and fragments."""
import dataclasses

from support import Fixture
from mizu.config import MAX_USAGE_POLICY_COUNT, TOML_INTEGER_MAX, CiRequestEvidence, load
from mizu.errors import ConfigError


class CiRequestEvidenceConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        expected = dict(unknown_after_seconds=86400, name_processing_limit=10000, record_limit=1000)
        self.assertEqual(dataclasses.asdict(self.config.ci_requests), expected)
        self.assertEqual(dataclasses.asdict(CiRequestEvidence()), expected)
        original = self.file.read_text()
        for field, default in expected.items():
            maximum = TOML_INTEGER_MAX if field == "unknown_after_seconds" else MAX_USAGE_POLICY_COUNT
            accepted = tuple(map(str, (1, default, default + 1, maximum)))
            for value in (*accepted, '0', '-1', str(maximum + 1),
                          'true', 'false', '1.5', '"1"', '[]', '{}'):
                with self.subTest(field=field, value=value):
                    self.file.write_text(original + f'\n[ci_requests]\n{field} = {value}\n')
                    if value in accepted:
                        self.assertEqual(dataclasses.asdict(load(self.file).ci_requests),
                                         {**expected, field: int(value)})
                    else:
                        with self.assertRaisesRegex(ConfigError, f'ci_requests.{field}'):
                            load(self.file)

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[ci_requests]\nfile = 1\n',
                     'ci_requests = 1\n' + original,
                     'ci_requests = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'ci_requests'):
                    load(self.file)
        (self.file.parent / 'evidence.toml').write_text(
            '[ci_requests]\nunknown_after_seconds = 120\nname_processing_limit = 7\nrecord_limit = 3\n')
        self.file.write_text('include = ["evidence.toml"]\n' + original)
        self.assertEqual(load(self.file).ci_requests,
                         CiRequestEvidence(unknown_after_seconds=120, name_processing_limit=7, record_limit=3))
