"""Pure configuration binding only; no async runner or containment claim."""
import copy
import dataclasses
import io
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.config import role_policy_text
from mizu.engine_config import child_authority_digest, effective, resource_digest
from mizu.errors import ConfigError
from mizu.fs import digest


class ChildAuthorityTests(Fixture):
    def setUp(self):
        super().setUp()
        self.role = self.config.roles['worker']
        self.inputs = dict(
            spend_role=self.role,
            settings=effective(self.config, self.role, self.role.profile),
            policy=role_policy_text(self.role),
            goal_digest=digest(self.project.goal.encode()),
            workspace=self.project.workspace,
            adapter_identity='a' * 64, local_settings={},
        )

    def identity(self, config=None, role=None, **overrides):
        return child_authority_digest(config or self.config, role or self.role,
                                      **{**self.inputs, **overrides})

    def test_stable_pure_bounded_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.inputs)
        # Resolution is outside this function; it must never discover current
        # policy, resources, credentials, or adapter content on its own.
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('I/O')), \
                patch.object(Path, 'write_bytes', side_effect=AssertionError('I/O')):
            value = self.identity()
            self.assertEqual(value, self.identity(**copy.deepcopy(self.inputs)))
        self.assertRegex(value, r'\A[0-9a-f]{64}\Z')
        self.assertEqual(before, self.inputs)
        reordered = dict(reversed(list(self.inputs['settings'].items())))
        self.assertEqual(value, self.identity(settings=reordered))

    def test_role_workspace_grants_policy_goal_and_adapter_mutations(self):
        baseline = self.identity()
        for field, value in (
                ('workspace', 'none'), ('capabilities', ('finish',)),
                ('name', 'other-child'), ('persistent_exec', not self.role.persistent_exec)):
            with self.subTest(field=field):
                self.assertNotEqual(baseline, self.identity(role=dataclasses.replace(self.role, **{field: value})))
        for field, value in (
                ('workspace', self.root / 'other-workspace'),
                ('policy', self.inputs['policy'] + '\nChanged policy\n'),
                ('goal_digest', 'b' * 64), ('adapter_identity', 'c' * 64),
                ('local_settings', {'models.json': 'd' * 64})):
            with self.subTest(field=field):
                self.assertNotEqual(baseline, self.identity(**{field: value}))
        for field, value in (('command', ('other-runtime',)), ('directory', self.root / 'other-agent')):
            engines = copy.deepcopy(self.config.engines)
            engines[self.inputs['settings']['engine']][field] = value
            self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, engines=engines)))

    def test_every_effective_category_and_live_pi_grants_are_bound(self):
        for engine in ('pi', 'pi-durable', 'codex', 'claude'):
            profile = {**self.config.profiles[self.role.profile], 'engine': engine, 'options': {}}
            self.config.profiles['authority-test'] = profile
            base = effective(self.config, self.role, 'authority-test')
            baseline = self.identity(settings=base)
            changes = {
                'provider': 'other-provider', 'model': 'other-model',
                'session': 'ephemeral',
                'options': {'thinkingLevel': 'high'},
                'resources': [{'path': '/reviewed/resource', 'sha256': 'e' * 64}],
                'mcp_servers': {'reviewed': {'url': 'https://example.invalid/mcp'}},
                'native_content': {'/reviewed/plugin': 'f' * 64},
            }
            for field, value in changes.items():
                with self.subTest(engine=engine, field=field):
                    self.assertNotEqual(baseline, self.identity(settings={**base, field: value}))
            tools = list(self.role.engine_tools) + ['reviewed-tool']
            role = dataclasses.replace(self.role, engine_tools=tuple(tools))
            self.assertNotEqual(baseline, self.identity(role=role, settings={**base, 'engine_tools': tools}))
            # Pi session projections omit these, child authority must not.
            role = dataclasses.replace(self.role, capabilities=('finish',))
            self.assertNotEqual(baseline, self.identity(role=role, settings=base))
        self.assertNotEqual(self.identity(settings={**base, 'engine': 'pi'}),
                            self.identity(settings={**base, 'engine': 'pi-durable'}))

    def test_effective_content_changes_at_same_paths_are_bound(self):
        resource = self.root / 'reviewed-resource'
        plugin = self.root / 'reviewed-plugin'
        resource.write_text('reviewed resource version one')
        plugin.mkdir()
        entry = plugin / 'entry.txt'
        entry.write_text('reviewed plugin version one')
        profile = {**self.config.profiles[self.role.profile], 'engine': 'claude',
                   'options': {'plugins': [{'type': 'local', 'path': str(plugin)}]},
                   'resources': [{'path': str(resource), 'sha256': resource_digest(resource)}]}
        self.config.profiles['content-test'] = profile
        settings = effective(self.config, self.role, 'content-test')
        baseline = self.identity(settings=settings)
        entry.write_text('reviewed plugin version two')
        changed = effective(self.config, self.role, 'content-test')
        self.assertNotEqual(baseline, self.identity(settings=changed))
        resource.write_text('reviewed resource version two')
        with self.assertRaises(ConfigError):
            effective(self.config, self.role, 'content-test')
        profile['resources'][0]['sha256'] = resource_digest(resource)
        self.assertNotEqual(self.identity(settings=changed),
                            self.identity(settings=effective(self.config, self.role, 'content-test')))

    def test_parent_effective_allowances_and_child_deadline_are_bound(self):
        baseline = self.identity()
        for field in ('requests_per_run', 'tools_per_run', 'wrap_up_requests', 'wrap_up_tools'):
            with self.subTest(field=field):
                current = getattr(self.config.limits, field)
                spend_role = dataclasses.replace(self.role, **{field: current + 1})
                self.assertNotEqual(baseline, self.identity(spend_role=spend_role))
        child = dataclasses.replace(self.role, run_seconds=123)
        self.assertNotEqual(baseline, self.identity(role=child))
        for field in ('delegate_wrap_up_requests', 'delegate_wrap_up_tools',
                      'daily_requests', 'shared_daily_requests', 'command_seconds',
                      'model_request_seconds', 'output_bytes', 'file_bytes'):
            limits = dataclasses.replace(self.config.limits, **{field: getattr(self.config.limits, field) + 1})
            self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, limits=limits)))

    def test_sandbox_and_tool_policy_are_bound(self):
        baseline = self.identity()
        for field, value in (
                ('image', 'example.invalid/image@sha256:' + 'a' * 64),
                ('mode', 'namespace'), ('executable', 'other-runtime'),
                ('network', 'managed'), ('memory_mb', 3000),
                ('mounts', ({'source': '/reviewed/data', 'target': '/reviewed'},)),
                ('env', {'PUBLIC_SETTING': 'test-only'}),
                ('namespace_helper', ('reviewed-helper',)),
                ('cgroup_parent', 'isolated.scope')):
            with self.subTest(field=field):
                sandbox = dataclasses.replace(self.config.sandbox, **{field: value})
                self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, sandbox=sandbox)))
        for field in ('web', 'vcs', 'materialize', 'insight_decisions', 'operator_records'):
            value = {**getattr(self.config, field), 'test-only-policy': True}
            self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, **{field: value})))
        self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, exclude=('ignored',))))
        for field, value in (('data', self.root / 'other-data'),
                             ('file', self.root / 'other-config.toml'),
                             ('timezone', 'local')):
            self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, **{field: value})))
        cache = dataclasses.replace(self.config.workspace_cache, roots=('build',))
        self.assertNotEqual(baseline, self.identity(config=dataclasses.replace(self.config, workspace_cache=cache)))

    def test_synthetic_secret_values_are_only_in_memory_and_affect_digest(self):
        # Never print sentinels, even in an assertion's failure diff.
        option = 'synthetic-option-' + 'sentinel'
        mcp_env = 'synthetic-mcp-' + 'sentinel'
        self.config.profiles['secret-test'] = {
            **self.config.profiles[self.role.profile], 'engine': 'claude',
            'options': {'env': {'TEST_ONLY': option}},
            'mcp_servers': {'test': {'url': 'https://example.invalid', 'env': {'TEST_ONLY': mcp_env}}},
        }
        settings = effective(self.config, self.role, 'secret-test')
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), \
                patch('logging.Logger._log') as log:
            value = self.identity(settings=settings)
            changed = copy.deepcopy(settings)
            changed['options']['env']['TEST_ONLY'] += '-changed'
            self.assertTrue(value != self.identity(settings=changed))
            changed = copy.deepcopy(settings)
            changed['mcp_servers']['test']['env']['TEST_ONLY'] += '-changed'
            self.assertTrue(value != self.identity(settings=changed))
            log.assert_not_called()
        self.assertTrue(all(s not in value + output.getvalue() for s in (option, mcp_env)))
        self.assertEqual('', output.getvalue())

    def test_incomplete_invalid_or_oversized_inputs_fail_without_values(self):
        sentinel = 'synthetic-invalid-' + 'sentinel'
        base = self.inputs['settings']
        invalid = [dict(base, unknown=sentinel),
                   {k: v for k, v in base.items() if k != 'native_content'},
                   dict(base, engine_tools=['mismatched']),
                   dict(base, options={sentinel: object()}),
                   dict(base, options={sentinel: float('nan')}),
                   dict(base, options={sentinel: 'x' * 1048576})]
        for settings in invalid:
            with self.assertRaises(ConfigError) as caught:
                self.identity(settings=settings)
            self.assertTrue(sentinel not in str(caught.exception))
            self.assertTrue(sentinel not in ''.join(traceback.format_exception(caught.exception)))
            self.assertEqual('Invalid or incomplete child authority inputs', str(caught.exception))
        for field, value in (('goal_digest', sentinel), ('adapter_identity', ''), ('policy', ''), ('workspace', '')):
            with self.assertRaises(ConfigError):
                self.identity(**{field: value})


if __name__ == '__main__':
    unittest.main()
