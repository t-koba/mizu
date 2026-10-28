"""Operator catalog control path; configured child boundary, never a model run."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Fixture
from mizu.cli import parser, execute
from mizu.errors import ConfigError
from mizu.model_catalog import (inspect, MAXIMUM, OBSERVATIONS, OBSERVATION_BYTES,
                                OBSERVATION_COUNT)


class ModelCatalogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config.engines['pi']['directory'] = self.root / 'engine'

    def catalog(self, *, refresh=False, at='2026-01-01T00:00:00+00:00',
                models=None, error=None, source='registered_native_provider'):
        rows = [{'id': 'native-only-model', 'resolvable': True}] if models is None else models
        report = {'source': source, 'status': 'error' if error else 'available',
                  'models': rows, 'error': error,
                  'refresh': ('failed' if error else 'completed') if refresh else 'not_requested'}
        with patch('mizu.model_catalog.run', return_value=SimpleNamespace(
                reason='exited', exit_code=0, stdout=json.dumps(report))) as child, \
                patch('mizu.model_catalog.now', return_value=at):
            try:
                return inspect(self.config, 'primary', refresh=refresh)
            finally:
                self.assertEqual(child.call_count, 1, 'never retry the provider')

    def test_success_then_cache_age_is_source_bound_and_read_only(self):
        before = self.catalog()
        self.assertIsNone(before['last_successful_observation'])
        self.assertIsNone(before['observation_age_seconds'])
        self.assertFalse((self.config.data / OBSERVATIONS).exists())
        report = self.catalog(refresh=True)
        self.assertEqual(report['last_successful_observation'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(report['observation_age_seconds'], 0)
        self.assertEqual(report['freshness'], 'observed')
        self.assertEqual(report['completeness'], 'unknown')
        path = self.config.data / OBSERVATIONS
        stored = path.read_bytes()
        self.assertLess(len(stored), OBSERVATION_BYTES)
        self.assertNotIn(str(self.root).encode(), stored)
        self.assertNotIn(b'test-provider', stored)
        self.assertNotIn(b'native-only-model', stored)
        cached = self.catalog(at='2026-01-02T00:00:30+00:00')
        self.assertEqual(cached['last_successful_observation'], report['last_successful_observation'])
        self.assertEqual(cached['observation_age_seconds'], 86430, 'no implicit stale threshold')
        self.assertEqual(cached['freshness'], 'observed')
        self.assertIsNone(cached['error'])
        self.assertEqual(path.read_bytes(), stored, 'cache reads do not rewrite observations')
        earlier = self.catalog(at='2025-12-31T23:59:59+00:00')
        self.assertIsNone(earlier['observation_age_seconds'])
        self.assertEqual(earlier['freshness'], 'unknown',
                         'a future observation timestamp proves nothing about freshness')
        self.assertEqual(earlier['last_successful_observation'],
                         report['last_successful_observation'])

    def test_listing_and_runtime_identity_must_match_without_cross_provider_leakage(self):
        from mizu.engine_config import resource_digest
        success = self.catalog(refresh=True)
        changed_rows = [[], [{'id': 'changed-model', 'resolvable': True}],
                        [{'id': 'native-only-model', 'resolvable': False}]]
        for models in changed_rows:
            report = self.catalog(models=models, error='offline')
            self.assertIsNone(report['last_successful_observation'])
            self.assertIsNone(report['observation_age_seconds'])
            self.assertEqual(report['freshness'], 'unknown')
        self.catalog(refresh=True, error='auth_failed')
        original = copy.deepcopy(self.config.profiles['primary'])
        for provider in ('other-provider', 'test-provider'):
            self.config.profiles['primary']['provider'] = provider
            if provider == 'other-provider':
                report = self.catalog()
                self.assertIsNone(report['last_successful_observation'])
                self.assertIsNone(report['last_refresh_error'])
                self.catalog(refresh=True, error='offline')
            else:
                self.assertEqual(self.catalog()['last_refresh_error']['code'], 'auth_failed')
        self.config.profiles['primary']['options']['thinkingLevel'] = 'high'
        self.assertIsNone(self.catalog()['last_successful_observation'])
        self.config.profiles['primary'] = copy.deepcopy(original)
        extension = self.root / 'native.mjs'
        extension.write_text('export default () => {};')
        self.config.profiles['primary']['resources'] = [
            {'kind': 'extension', 'path': str(extension), 'sha256': resource_digest(extension)}]
        self.assertIsNone(self.catalog()['last_successful_observation'])
        self.config.profiles['primary'] = copy.deepcopy(original)
        self.config.profiles['primary']['model'] = 'explicit-new-selection'
        self.assertEqual(self.catalog()['last_successful_observation'], success['last_successful_observation'],
                         'selection is independent of discovery')
        directory = self.config.engines['pi']['directory']
        self.config.engines['pi']['directory'] = self.root / 'other-engine'
        self.assertIsNone(self.catalog()['last_successful_observation'])
        self.config.engines['pi']['directory'] = directory
        with patch.object(type(self.config), 'command', return_value=('different-runtime',)):
            self.assertIsNone(self.catalog()['last_successful_observation'])
        unsupported = self.catalog(source='unsupported', models=[])
        self.assertIsNone(unsupported['last_successful_observation'])

    def test_refresh_errors_preserve_success_but_not_current_invocation_error(self):
        success = self.catalog(refresh=True)
        for code in ('offline', 'auth_failed', 'refresh_failed', 'aborted'):
            failed = self.catalog(refresh=True, at='2026-01-01T00:01:00+00:00', error=code)
            expected = {'code': code, 'at': '2026-01-01T00:01:00+00:00'}
            self.assertEqual(failed['error'], code)
            self.assertEqual(failed['last_refresh_error'], expected)
            self.assertEqual(failed['last_successful_observation'], success['last_successful_observation'])
            cached = self.catalog(at='2026-01-01T00:02:00+00:00')
            self.assertIsNone(cached['error'])
            self.assertEqual(cached['last_refresh_error'], expected)
            self.assertEqual(cached['observation_age_seconds'], 120)
        refreshed = self.catalog(refresh=True, at='2026-01-01T00:03:00+00:00')
        self.assertIsNone(refreshed['last_refresh_error'])
        self.assertEqual(refreshed['last_successful_observation'], '2026-01-01T00:03:00+00:00')

    def test_failed_process_and_invalid_listing_never_replace_success(self):
        success = self.catalog(refresh=True)
        for result, code in ((SimpleNamespace(reason='timeout', exit_code=None), 'catalog_process_failed'),
                             (OSError('unsafe fixture detail'), 'engine_unavailable')):
            with patch('mizu.model_catalog.run', **({'side_effect': result} if isinstance(result, OSError)
                                                   else {'return_value': result})):
                failed = inspect(self.config, 'primary', refresh=True)
            self.assertIsNone(failed['last_successful_observation'], 'no listing to match')
            self.assertEqual(failed['last_refresh_error']['code'], code)
            self.assertNotIn('unsafe fixture detail', json.dumps(failed))
        invalid = self.catalog(refresh=True, error='invalid_native_listing', models=[])
        self.assertIsNone(invalid['last_successful_observation'])
        self.assertEqual(self.catalog()['last_successful_observation'], success['last_successful_observation'])
        with patch('mizu.model_catalog.run', return_value=SimpleNamespace(
                reason='exited', exit_code=0, stdout='unsafe non-JSON fixture')):
            with self.assertRaisesRegex(ConfigError, 'Invalid model catalog response'):
                inspect(self.config, 'primary', refresh=True)
        self.assertEqual(self.catalog()['last_refresh_error']['code'], 'invalid_catalog_response')
        self.assertEqual(self.catalog()['last_successful_observation'], success['last_successful_observation'])

    def test_corrupt_metadata_fails_before_provider_and_credentials(self):
        from mizu.fs import canonical, digest
        self.catalog(refresh=True)
        path = self.config.data / OBSERVATIONS
        valid = json.loads(path.read_bytes())
        corruptions = [b'not JSON', b'{' * 2000, b'x' * (OBSERVATION_BYTES + 1),
                       b'{"version":1,"version":1}',
                       json.dumps({**valid, 'checksum': '0' * 64}).encode()]
        key = next(iter(valid['observations']))
        for change in ({'at': 'not-a-timestamp'}, {'listing_digest': 'bad-digest'},
                       {'at': '2026-01-01T00:00:00'}, {'at': '2026-01-01T00:00:00+01:00'}):
            altered = copy.deepcopy(valid)
            altered['observations'][key]['success'].update(change)
            altered['checksum'] = digest(canonical({k: v for k, v in altered.items() if k != 'checksum'}))
            corruptions.append(canonical(altered))
        altered = copy.deepcopy(valid)
        altered['observations'][key]['refresh_error'] = {'code': 'unsafe provider detail',
                                                       'at': '2026-01-01T00:00:00+00:00'}
        altered['checksum'] = digest(canonical({k: v for k, v in altered.items() if k != 'checksum'}))
        corruptions.append(canonical(altered))
        for raw in corruptions:
            path.write_bytes(raw)
            for refresh in (False, True):
                with patch('mizu.model_catalog.run') as child, patch('mizu.model_catalog.credentials') as credentials:
                    with self.assertRaisesRegex(ConfigError, '^Invalid or unavailable model catalog observation store$'):
                        inspect(self.config, 'primary', refresh=refresh)
                child.assert_not_called()
                credentials.assert_not_called()
                self.assertEqual(path.read_bytes(), raw, 'never silently repair corrupted evidence')

    def test_credential_errors_keep_their_identity(self):
        from mizu.errors import ConfigError as _ConfigError
        with patch('mizu.model_catalog.credentials',
                   side_effect=_ConfigError('fixture credentials failure')):
            with patch('mizu.model_catalog.run') as child:
                with self.assertRaisesRegex(_ConfigError, 'fixture credentials failure'):
                    inspect(self.config, 'primary', refresh=True)
            child.assert_not_called()
        with patch('mizu.model_catalog.credentials', side_effect=OSError('fixture os failure')):
            with patch('mizu.model_catalog.run') as child:
                with self.assertRaises(OSError):
                    inspect(self.config, 'primary', refresh=True)
            child.assert_not_called()

    def test_linked_sidecar_is_refused_before_provider(self):
        import os
        self.catalog(refresh=True)
        path = self.config.data / OBSERVATIONS
        other = self.config.data / 'linked-observation.json'
        os.link(path, other)
        with patch('mizu.model_catalog.run') as child:
            with self.assertRaisesRegex(ConfigError, 'observation store'):
                inspect(self.config, 'primary', refresh=True)
        child.assert_not_called()
        other.unlink()
        path.unlink()
        try:
            path.symlink_to(other)
        except OSError:
            return  # Windows without symlink privilege; hardlink case ran.
        with patch('mizu.model_catalog.run') as child:
            with self.assertRaisesRegex(ConfigError, 'observation store'):
                inspect(self.config, 'primary')
        child.assert_not_called()

    def test_failed_sidecar_write_preserves_prior_record_without_retry(self):
        from mizu.fs import safe_write as real_safe_write
        self.catalog(refresh=True)
        path = self.config.data / OBSERVATIONS
        stored = path.read_bytes()
        def interrupted(root, name, data):
            if name == OBSERVATIONS:
                raise OSError('unsafe fixture diagnostics')
            return real_safe_write(root, name, data)
        with patch('mizu.model_catalog.safe_write', side_effect=interrupted):
            with self.assertRaisesRegex(ConfigError, '^Invalid or unavailable model catalog observation store$'):
                self.catalog(refresh=True, at='2026-01-02T00:00:00+00:00')
        self.assertEqual(path.read_bytes(), stored)
        self.assertEqual(list(self.config.data.glob('.new-*')), [])
        self.assertEqual(self.catalog()['last_successful_observation'], '2026-01-01T00:00:00+00:00')

    def test_store_bound_evicts_oldest_for_new_refresh(self):
        from mizu.fs import canonical, digest
        from mizu.model_catalog import _observation_key
        from mizu.config import Role
        from mizu.engine_config import effective
        self.catalog(refresh=True)
        path = self.config.data / OBSERVATIONS
        old_entry = {'success': {'listing_digest': '0' * 64, 'at': '2025-01-01T00:00:00+00:00'},
                     'refresh_error': None}
        entries = {f'{number:064x}': copy.deepcopy(old_entry) for number in range(OBSERVATION_COUNT)}
        payload = {'version': 1, 'observations': entries}
        path.write_bytes(canonical({**payload, 'checksum': digest(canonical(payload))}))
        settings = effective(self.config, Role('catalog', 'primary', (), 'none', ()), 'primary')
        current = _observation_key(self.config, settings, self.config.agent_dir('pi'))
        self.assertNotIn(current, entries, 'fixture uses a fresh identity')
        refreshed = self.catalog(refresh=True, at='2026-01-02T00:00:00+00:00')
        self.assertEqual(refreshed['last_successful_observation'], '2026-01-02T00:00:00+00:00')
        self.assertEqual(refreshed['freshness'], 'observed')
        stored = json.loads(path.read_bytes())['observations']
        self.assertEqual(len(stored), OBSERVATION_COUNT, 'bounded eviction keeps the count bound')
        self.assertIn(current, stored, 'the new explicit observation is retained')
        self.assertNotIn(f'{(0):064x}', stored, 'the oldest entry is evicted first')
        self.assertEqual(stored[current]['success']['at'], '2026-01-02T00:00:00+00:00')
        self.assertNotIn(b'native-only-model', path.read_bytes())
        # Updating the same identity replaces in place without further eviction.
        again = self.catalog(refresh=True, at='2026-01-03T00:00:00+00:00')
        self.assertEqual(again['last_successful_observation'], '2026-01-03T00:00:00+00:00')
        restored = json.loads(path.read_bytes())['observations']
        self.assertEqual(len(restored), OBSERVATION_COUNT)
        self.assertIn(current, restored)
        self.assertEqual(restored[current]['success']['at'], '2026-01-03T00:00:00+00:00')

    def test_store_bound_evicts_oldest_for_new_error(self):
        from mizu.fs import canonical, digest
        from mizu.model_catalog import _observation_key
        from mizu.config import Role
        from mizu.engine_config import effective
        self.catalog(refresh=True)
        path = self.config.data / OBSERVATIONS
        old_entry = {'success': None,
                     'refresh_error': {'code': 'offline', 'at': '2025-01-01T00:00:00+00:00'}}
        entries = {f'{number:064x}': copy.deepcopy(old_entry) for number in range(OBSERVATION_COUNT)}
        entries[f'{0:064x}'] = {
            'success': {'listing_digest': '0' * 64, 'at': '2024-01-01T00:00:00+00:00'},
            'refresh_error': {'code': 'offline', 'at': '2025-12-01T00:00:00+00:00'}}
        payload = {'version': 1, 'observations': entries}
        path.write_bytes(canonical({**payload, 'checksum': digest(canonical(payload))}))
        settings = effective(self.config, Role('catalog', 'primary', (), 'none', ()), 'primary')
        current = _observation_key(self.config, settings, self.config.agent_dir('pi'))
        self.assertNotIn(current, entries, 'fixture uses a fresh identity')
        failed = self.catalog(refresh=True, at='2026-01-02T00:00:00+00:00', error='offline')
        self.assertEqual(failed['last_refresh_error'],
                         {'code': 'offline', 'at': '2026-01-02T00:00:00+00:00'})
        stored = json.loads(path.read_bytes())['observations']
        self.assertEqual(len(stored), OBSERVATION_COUNT)
        self.assertIn(current, stored)
        self.assertIn(f'{(0):064x}', stored, 'recent error retains an entry with old success')
        self.assertNotIn(f'{(1):064x}', stored, 'eviction uses the latest event time')

    def test_provider_runs_without_global_store_lock(self):
        import os
        import subprocess
        import sys
        from support import ROOT
        def child(*args, **kwargs):
            code = """
from pathlib import Path
import sys
from mizu.fs import lock
from mizu.errors import Busy
try:
    with lock(Path(sys.argv[1]), blocking=False):
        print('unlocked')
except Busy:
    raise SystemExit('provider holds the store lock')
"""
            result = subprocess.run([sys.executable, '-c', code, str(self.config.data / '.model-catalog.lock')],
                                    env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'unlocked')
            return SimpleNamespace(reason='exited', exit_code=0,
                                   stdout='{"source":"registered_native_provider",'
                                          '"status":"available",'
                                          '"models":[{"id":"native-only-model","resolvable":true}],'
                                          '"refresh":"completed","error":null,'
                                          '"last_successful_observation":null,'
                                          '"freshness":"unknown","completeness":"unknown"}')
        with patch('mizu.model_catalog.run', side_effect=child):
            report = inspect(self.config, 'primary', refresh=True)
        self.assertEqual(report['freshness'], 'observed')

    def test_sidecar_write_holds_global_store_lock(self):
        import os
        import subprocess
        import sys
        from support import ROOT
        from mizu.fs import safe_write as real_safe_write
        def checked_write(root, name, data):
            if name != OBSERVATIONS:
                return real_safe_write(root, name, data)
            code = """
from pathlib import Path
import sys
from mizu.fs import lock
from mizu.errors import Busy
try:
    with lock(Path(sys.argv[1]), blocking=False):
        raise SystemExit('unexpected lock acquisition')
except Busy:
    print('locked')
"""
            result = subprocess.run([sys.executable, '-c', code, str(self.config.data / '.model-catalog.lock')],
                                    env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'locked')
            return real_safe_write(root, name, data)
        with patch('mizu.model_catalog.safe_write', side_effect=checked_write):
            report = self.catalog(refresh=True)
        self.assertEqual(report['freshness'], 'observed')

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
        cached = command()
        self.assertEqual(cached['models'], report['models'])
        self.assertIsNotNone(report['last_successful_observation'])
        self.assertEqual(cached['last_successful_observation'], report['last_successful_observation'])
        self.assertEqual(cached['freshness'], 'observed')
        self.assertIsInstance(cached['observation_age_seconds'], int)
        self.assertEqual(self.file.read_bytes(), config_bytes)
        extension.write_text('export default () => {};')
        with patch('mizu.model_catalog.run') as child:
            with self.assertRaisesRegex(ConfigError, 'resource digest mismatch'):
                execute(parser().parse_args(['--config', str(self.file), 'model-catalog', '--profile', 'primary']))
        child.assert_not_called()
