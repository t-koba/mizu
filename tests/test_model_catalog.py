"""Operator catalog control path; configured child boundary, never a model run."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture
from mizu.cli import parser, execute
from mizu.errors import ConfigError
from mizu.model_catalog import inspect, MAXIMUM


class ModelCatalogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config.engines['pi']['directory'] = self.root / 'engine'

    def test_cli_requires_profile_and_refresh_is_explicit(self):
        args = parser().parse_args(['model-catalog', '--profile', 'primary'])
        self.assertFalse(args.refresh)
        with patch('mizu.model_catalog.inspect', return_value={'status': 'available'}) as call, patch('mizu.cli.load', return_value=self.config):
            self.assertEqual(execute(args), {'status': 'available'})
        call.assert_called_once_with(self.config, 'primary', refresh=False)
        self.assertTrue(parser().parse_args(['model-catalog', '--profile', 'primary', '--refresh']).refresh)

    def test_uses_configured_engine_boundary_no_bridge_and_no_selection_write(self):
        profiles = copy.deepcopy(self.config.profiles)
        config_bytes = self.file.read_bytes()
        def child(argv, **kwargs):
            self.assertEqual(argv[:len(self.config.command('pi'))], list(self.config.command('pi')))
            self.assertEqual(argv[-2], '--model-catalog')
            effective = json.loads(Path(argv[-1]).read_text())
            self.assertTrue(effective['refresh'])
            self.assertEqual(effective['provider'], 'test-provider')
            self.assertEqual(effective['model'], 'test-model')
            self.assertEqual(effective['agentDir'], str(self.config.agent_dir('pi')))
            self.assertEqual(kwargs['maximum'], MAXIMUM)
            self.assertEqual(kwargs['timeout'], self.config.limits.model_request_seconds)
            self.assertNotIn('MIZU_BRIDGE_CONFIG', kwargs['env'])
            self.assertEqual(kwargs['env']['FIXTURE_API_KEY'], 'fixture-value')
            return SimpleNamespace(reason='exited', exit_code=0,
                                   stdout='{"inference":"not_run","status":"unsupported"}')
        with patch('mizu.model_catalog.run', side_effect=child) as run, \
                patch('mizu.model_catalog.credentials', return_value={'FIXTURE_API_KEY': 'fixture-value'}):
            result = inspect(self.config, 'primary', refresh=True)
        self.assertEqual(run.call_count, 1, 'never retry a possibly completed refresh')
        self.assertEqual(result['selected_model'], 'test-model')
        self.assertEqual(self.config.profiles, profiles)
        self.assertEqual(self.file.read_bytes(), config_bytes)
        self.assertEqual(list(self.config.data.glob('catalog-*')), [], 'ephemeral input removed')

    def test_durable_is_explicit_source_mismatch_without_launch_or_credentials(self):
        self.config.profiles['primary']['engine'] = 'pi-durable'
        for refresh in (False, True):
            with patch('mizu.model_catalog.run') as run, patch('mizu.model_catalog.credentials') as credentials:
                report = inspect(self.config, 'primary', refresh=refresh)
            run.assert_not_called()
            credentials.assert_not_called()
            self.assertEqual(report['status'], 'unsupported')
            self.assertEqual(report['error'], 'durable_native_provider_source_mismatch')
            self.assertEqual(report['models'], [])
            self.assertEqual(report['selected_model'], 'test-model')

    def test_failure_is_bounded_safe_and_never_retried(self):
        for reason, code in [('timeout', None), ('output_limit', None), ('exited', 1)]:
            with patch('mizu.model_catalog.run', return_value=SimpleNamespace(
                    reason=reason, exit_code=code, stdout='unsafe fixture output', stderr='unsafe fixture diagnostics')) as run:
                report = inspect(self.config, 'primary', refresh=True)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(report['error'], 'catalog_process_failed')
            self.assertNotIn('unsafe fixture', json.dumps(report))
        malformed = (
            '{"status":"unsupported","error":"unsafe provider detail"}',
            '{"status":"unsupported","error":null,"private":"must not escape"}',
            '{"status":"unsupported","error":null,"error":"unsafe"}',
            '{"status":"available","source":"registered_native_provider","error":null,'
            '"models":[{"id":"fixture-model","resolvable":true,"private":"unsafe"}]}',
        )
        for stdout in ('not JSON', *malformed):
            with patch('mizu.model_catalog.run', return_value=SimpleNamespace(
                    reason='exited', exit_code=0, stdout=stdout)):
                with self.assertRaisesRegex(ConfigError, 'Invalid model catalog response'):
                    inspect(self.config, 'primary')

    def test_cli_to_reviewed_native_extension_without_inference(self):
        import contextlib
        import io
        import shutil
        from support import ROOT
        from mizu.cli import main
        from mizu.engine_config import resource_digest
        if not shutil.which('node') or not (ROOT / 'adapters/pi/node_modules/@earendil-works/pi-coding-agent/dist/index.js').is_file():
            self.skipTest('operator-provisioned Pi SDK and Node required; real SDK gate covers this path')
        extension = self.root / 'native.mjs'
        extension.write_text('''
export default pi => {
  const model = { id: 'native-only-cli-model', name: 'Native', provider: 'test-provider',
    api: 'openai-completions', baseUrl: 'https://fixture.invalid', input: ['text'],
    reasoning: false, contextWindow: 8192, maxTokens: 1024,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
  let models = [];
  pi.registerProvider({ id: model.provider, name: 'Native', getModels: () => models,
    auth: { apiKey: { name: 'Fixture', resolve: async () => ({ auth: { apiKey: 'fixture' } }) } },
    async refreshModels(ctx) {
      const rows = ctx.allowNetwork ? [model] : ctx.stored?.models ?? [];
      await ctx.publish({ ...(ctx.allowNetwork ? { persist: { models: rows } } : {}),
        update: () => { models = rows; } });
    }, stream() { throw new Error('no inference'); }, streamSimple() { throw new Error('no inference'); }
  });
};
''')
        text = self.file.read_text().replace('directory = "pi"', 'directory = ' + json.dumps(str(self.root / 'engine')))
        resource = ('resources = [{ kind = "extension", path = ' + json.dumps(str(extension))
                    + ', sha256 = ' + json.dumps(resource_digest(extension)) + ' }]\n')
        self.file.write_text(text.replace('[profiles.primary]\n', '[profiles.primary]\n' + resource))
        config_bytes = self.file.read_bytes()
        def command(*flags):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['--config', str(self.file), 'model-catalog', '--profile', 'primary', *flags])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())
        self.assertEqual(command()['models'], [])
        report = command('--refresh')
        self.assertEqual(report['source'], 'registered_native_provider')
        self.assertEqual(report['models'], [{'id': 'native-only-cli-model', 'resolvable': True}])
        self.assertEqual(report['selected_model'], 'test-model')
        self.assertEqual(command()['models'], report['models'])
        self.assertEqual(self.file.read_bytes(), config_bytes)
        extension.write_text('export default () => {};')
        with patch('mizu.model_catalog.run') as child:
            with self.assertRaisesRegex(ConfigError, 'resource digest mismatch'):
                execute(parser().parse_args(['--config', str(self.file), 'model-catalog', '--profile', 'primary']))
        child.assert_not_called()
