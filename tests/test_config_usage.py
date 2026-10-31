"""Usage-report policy defaults, strict ranges and include loading."""
import dataclasses

from support import Fixture
from mizu.config import Usage, load
from mizu.errors import ConfigError


class UsageConfigTests(Fixture):
    def test_defaults_and_ranges(self):
        expected = dict(runs_scanned=5000, recent_entries=200, cache_affinity_runs=200,
                        cache_affinity_rows=200, unknown_keys=20)
        self.assertEqual(dataclasses.asdict(self.config.usage), expected)
        self.assertEqual(dataclasses.asdict(Usage()), expected)
        original = self.file.read_text()
        for field, maximum in expected.items():
            minimum = 1 if field in ("runs_scanned", "recent_entries") else 0
            for value in (str(minimum), str(maximum), str(minimum - 1), str(maximum + 1),
                          'true', 'false', '1.5', '"1"', '[]', '{}'):
                with self.subTest(field=field, value=value):
                    self.file.write_text(original + f'\n[usage]\n{field} = {value}\n')
                    if value in (str(minimum), str(maximum)):
                        actual = dataclasses.asdict(load(self.file).usage)
                        self.assertEqual(actual, {**expected, field: int(value)})
                    else:
                        with self.assertRaisesRegex(ConfigError, f'usage.{field}'):
                            load(self.file)

    def test_rejects_unknown_keys_and_non_table(self):
        original = self.file.read_text()
        for text in (original + '\n[usage]\nruns_scaned = 1\n',
                     'usage = 1\n' + original, 'usage = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'usage'):
                    load(self.file)

    def test_usage_in_included_fragment(self):
        original = self.file.read_text()
        (self.file.parent / 'usage.toml').write_text('[usage]\nruns_scanned = 3\nunknown_keys = 0\n')
        self.file.write_text('include = ["usage.toml"]\n' + original)
        policy = load(self.file).usage
        self.assertEqual(policy, Usage(runs_scanned=3, unknown_keys=0))
