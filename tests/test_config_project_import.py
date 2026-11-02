"""Project-import-owned local Git command bounds and fragment loading."""
import dataclasses

from support import Fixture
from mizu.config import ProjectImport, TOML_INTEGER_MAX, load
from mizu.errors import ConfigError


class ProjectImportConfigTests(Fixture):
    def test_default_and_range(self):
        expected = {'status_timeout_seconds': 30,
                    'clone_timeout_seconds': 300,
                    'local_timeout_seconds': 15}
        self.assertEqual(dataclasses.asdict(self.config.project_import), expected)
        self.assertEqual(dataclasses.asdict(ProjectImport()), expected)
        original = self.file.read_text()
        accepted = (1, 15, 30, 300, TOML_INTEGER_MAX)
        for name in expected:
            for value in (*accepted, 0, -1, TOML_INTEGER_MAX + 1):
                with self.subTest(name=name, value=value):
                    self.file.write_text(original + '\n[project_import]\n'
                                         f'{name} = {value}\n')
                    if value in accepted:
                        configured = load(self.file).project_import
                        self.assertEqual(getattr(configured, name), value)
                    else:
                        with self.assertRaisesRegex(ConfigError, f'project_import.{name}'):
                            load(self.file)
        for value in ('true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original + '\n[project_import]\n'
                                     f'status_timeout_seconds = {value}\n')
                with self.assertRaisesRegex(ConfigError, 'project_import.status_timeout_seconds'):
                    load(self.file)
        self.file.write_text(original + '\n[project_import]\n')
        self.assertEqual(load(self.file).project_import, ProjectImport())

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[project_import]\ntimeout_seconds = 1\n',
                     'project_import = 1\n' + original,
                     'project_import = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'project_import'):
                    load(self.file)
        (self.file.parent / 'project_import.toml').write_text(
            '[project_import]\nstatus_timeout_seconds = 7\nclone_timeout_seconds = 11\n'
            'local_timeout_seconds = 3\n')
        self.file.write_text('include = ["project_import.toml"]\n' + original)
        self.assertEqual(load(self.file).project_import,
                         ProjectImport(status_timeout_seconds=7, clone_timeout_seconds=11,
                                       local_timeout_seconds=3))
