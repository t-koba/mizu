"""Publication-owned local Git HEAD timeout policy and fragment loading."""
import dataclasses

from support import Fixture
from mizu.config import PublicationPolicy, TOML_INTEGER_MAX, load
from mizu.errors import ConfigError


class PublicationConfigTests(Fixture):
    def test_default_and_range(self):
        expected = {'source_commit_timeout_seconds': 5}
        self.assertEqual(dataclasses.asdict(self.config.publication), expected)
        self.assertEqual(dataclasses.asdict(PublicationPolicy()), expected)
        original = self.file.read_text()
        accepted = tuple(map(str, (1, 5, 6, TOML_INTEGER_MAX)))
        for value in (*accepted, '0', '-1', str(TOML_INTEGER_MAX + 1),
                      'true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original + '\n[publication]\n'
                                     f'source_commit_timeout_seconds = {value}\n')
                if value in accepted:
                    self.assertEqual(load(self.file).publication,
                                     PublicationPolicy(source_commit_timeout_seconds=int(value)))
                else:
                    with self.assertRaisesRegex(ConfigError, 'publication.source_commit_timeout_seconds'):
                        load(self.file)
        self.file.write_text(original + '\n[publication]\n')
        self.assertEqual(load(self.file).publication, PublicationPolicy())

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[publication]\ntimeout_seconds = 1\n',
                     'publication = 1\n' + original,
                     'publication = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'publication'):
                    load(self.file)
        (self.file.parent / 'publication.toml').write_text(
            '[publication]\nsource_commit_timeout_seconds = 13\n')
        self.file.write_text('include = ["publication.toml"]\n' + original)
        self.assertEqual(load(self.file).publication,
                         PublicationPolicy(source_commit_timeout_seconds=13))
