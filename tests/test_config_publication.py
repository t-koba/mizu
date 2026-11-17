"""Publication-owned local Git HEAD timeout policy and fragment loading."""
import dataclasses
import json

from support import Fixture
from mizu.config import PublicationPolicy, TOML_INTEGER_MAX, load, publication_review_requirement
from mizu.errors import ConfigError


class PublicationConfigTests(Fixture):
    def test_default_and_range(self):
        expected = {'source_commit_timeout_seconds': 5, 'reviewer_role': '',
                    'reviewers': (), 'required_approvals': 0}
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
        self.assertEqual(publication_review_requirement(self.config.publication), ((), 0))
        self.assertFalse(any("review_verdict" in role.capabilities
                             for role in self.config.roles.values()))
        original = self.file.read_text()
        for name in ("reviewer", "searcher"):
            with self.subTest(name=name):
                self.file.write_text(original + f'\n[publication]\nreviewer_role = "{name}"\n')
                config = load(self.file)
                self.assertEqual(publication_review_requirement(config.publication), ((name,), 1))
                self.assertEqual(config.roles[name].capabilities,
                                 (*self.config.roles[name].capabilities, "review_verdict"))
                self.assertEqual([role.name for role in config.roles.values()
                                  if "review_verdict" in role.capabilities], [name])
        self.file.write_text(original + '\n[publication]\nreviewer_role = "absent"\n')
        with self.assertRaisesRegex(ConfigError, 'publication.reviewer_role.*review_verdict'):
            load(self.file)

    def test_plural_reviewers_parse_and_include_without_implicit_grants(self):
        original = self.file.read_text()
        holders = """
[roles.auditor]
profile = "alternate"
policy = "policies/shared.md"
workspace = "read"
capabilities = ["finish", "review_verdict"]
[roles.inspector]
profile = "alternate"
policy = "policies/shared.md"
workspace = "read"
capabilities = ["finish", "review_verdict"]
"""
        policy = ('[publication]\nreviewers = ["inspector", "auditor"]\n'
                  'required_approvals = 2\n')
        for included in (False, True):
            with self.subTest(included=included):
                (self.file.parent / 'publication.toml').write_text(policy)
                self.file.write_text(('include = ["publication.toml"]\n' if included else '')
                                     + original + holders + ('' if included else policy))
                config = load(self.file)
                self.assertEqual(config.publication, PublicationPolicy(
                    reviewers=("inspector", "auditor"), required_approvals=2))
                self.assertEqual(publication_review_requirement(config.publication),
                                 (("inspector", "auditor"), 2))
                self.assertEqual([name for name, role in config.roles.items()
                                  if "review_verdict" in role.capabilities], ["auditor", "inspector"])

    def test_plural_review_requirement_rejects_invalid_selections_and_thresholds(self):
        original = self.file.read_text()
        holder = ('\n[roles.auditor]\nprofile = "alternate"\npolicy = "policies/shared.md"\n'
                  'workspace = "read"\ncapabilities = ["finish", "review_verdict"]\n')
        invalid = [
            'reviewers = "auditor"\nrequired_approvals = 1',
            'reviewers = [1]\nrequired_approvals = 1',
            'reviewers = [""]\nrequired_approvals = 1',
            'reviewers = ["auditor", "auditor"]\nrequired_approvals = 1',
            'reviewers = ["absent"]\nrequired_approvals = 1',
            'reviewers = ["reviewer"]\nrequired_approvals = 1',  # no implicit grant
            'reviewers = ["auditor"]\nrequired_approvals = 1\nreviewer_role = "reviewer"',
            'reviewers = ["auditor"]',  # default zero is not a quorum
        ]
        invalid += [f'reviewers = ["auditor"]\nrequired_approvals = {value}'
                    for value in ('-1', '0', '2', 'true', 'false', '1.0', '"1"', '[]', '{}')]
        invalid += [f'required_approvals = {value}' for value in ('1', '-1')]
        for policy in invalid:
            with self.subTest(policy=policy):
                self.file.write_text(original + holder + '\n[publication]\n' + policy + '\n')
                with self.assertRaisesRegex(ConfigError, r'publication\.'):
                    load(self.file)
        for workspace, caps in (
                ("write", ["finish", "review_verdict"]),
                ("read", ["review_verdict"]),
                ("read", ["finish", "review_verdict", "delegate"]),
                ("read", ["finish", "review_verdict", "vcs_publish"])):
            with self.subTest(workspace=workspace, caps=caps):
                bad_holder = holder.replace('workspace = "read"', f'workspace = "{workspace}"').replace(
                    '["finish", "review_verdict"]', json.dumps(caps))
                self.file.write_text(original + bad_holder + '\n[publication]\n'
                                     'reviewers = ["auditor"]\nrequired_approvals = 1\n')
                with self.assertRaisesRegex(ConfigError, r'publication\.'):
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
