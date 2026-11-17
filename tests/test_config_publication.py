"""Publication-owned local Git HEAD timeout policy and fragment loading."""
import dataclasses
import json

from support import Fixture
from mizu.config import PublicationPolicy, TOML_INTEGER_MAX, load
from mizu.errors import ConfigError


class PublicationConfigTests(Fixture):
    def test_default_and_range(self):
        expected = {'source_commit_timeout_seconds': 5, 'reviewer_role': ''}
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

    def test_review_defaults_and_legacy_policy_confer_capability(self):
        self.assertEqual(self.config.publication.reviewer_role, "")
        self.assertFalse(any("review_verdict" in role.capabilities
                             for role in self.config.roles.values()))
        original = self.file.read_text()
        for name in ("reviewer", "searcher"):
            with self.subTest(name=name):
                self.file.write_text(original + f'\n[publication]\nreviewer_role = "{name}"\n')
                config = load(self.file)
                self.assertEqual(config.roles[name].capabilities,
                                 (*self.config.roles[name].capabilities, "review_verdict"))
                self.assertEqual([role.name for role in config.roles.values()
                                  if "review_verdict" in role.capabilities], [name])
        self.file.write_text(original + '\n[publication]\nreviewer_role = "absent"\n')
        with self.assertRaisesRegex(ConfigError, 'publication.reviewer_role.*review_verdict'):
            load(self.file)

    def test_explicit_review_holders_require_independent_shape(self):
        original = self.file.read_text()
        for workspace, caps in (
                ("read", ["finish", "review_verdict"]),
                ("write", ["finish", "review_verdict"]),
                ("read", ["review_verdict"]),
                ("read", ["finish", "review_verdict", "delegate"]),
                ("read", ["finish", "review_verdict", "vcs_publish"])):
            with self.subTest(workspace=workspace, capabilities=caps):
                self.file.write_text(original + '\n[roles.auditor]\nprofile = "alternate"\npolicy = "policies/shared.md"\n'
                    + f'workspace = "{workspace}"\ncapabilities = {json.dumps(caps)}\n')
                if workspace == "read" and caps == ["finish", "review_verdict"]:
                    config = load(self.file)
                    self.assertIn("review_verdict", config.roles["auditor"].capabilities)
                    self.assertEqual(config.publication.reviewer_role, "")
                    # Explicitly selecting an existing holder is idempotent.
                    self.file.write_text(self.file.read_text() +
                        '\n[publication]\nreviewer_role = "auditor"\n')
                    self.assertEqual(load(self.file).roles["auditor"], config.roles["auditor"])
                else:
                    with self.assertRaisesRegex(ConfigError, 'review_verdict'):
                        load(self.file)

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

    def test_ci_wait_descendants_is_a_strict_default_true_boolean(self):
        self.assertIs(self.config.vcs['ci_wait_descendants'], True)
        original = self.file.read_text()
        for value in ('true', 'false', '0', '1', '"false"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original.replace('\n[vcs]\n', '\n[vcs]\nci_wait_descendants = ' + value + '\n'))
                if value in ('true', 'false'):
                    self.assertIs(load(self.file).vcs['ci_wait_descendants'], value == 'true')
                else:
                    with self.assertRaisesRegex(ConfigError, 'vcs.ci_wait_descendants'):
                        load(self.file)
