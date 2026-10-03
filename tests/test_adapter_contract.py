"""Adapter contract regression tests: contract.json is the single source.

doctor and check-cli must derive entry points and required flags from
adapters/<engine>/contract.json instead of hardcoding them, so drift
fails loudly via ConfigError or a missing-exports refusal.
"""
import copy
import json
import unittest

from support import ROOT
from mizu.engine_config import adapter_contract
from mizu.errors import ConfigError


class AdapterContractTests(unittest.TestCase):
    def test_contracts_load_with_expected_units(self):
        self.assertEqual(adapter_contract('pi')['request_unit'], 'model_request')
        self.assertEqual(adapter_contract('codex')['request_unit'], 'turn')
        self.assertEqual(adapter_contract('claude')['request_unit'], 'query')

    def test_entrypoints_exist_and_exports_declared(self):
        for engine in ('pi', 'claude'):
            contract = adapter_contract(engine)
            self.assertTrue((ROOT / 'adapters' / engine / contract['entrypoint']).is_file())
            self.assertTrue(contract['exports'])

    def test_codex_flags_declared(self):
        contract = adapter_contract('codex')
        flags = list(contract['required_flags'])
        for item in contract.get('contracts', []):
            flags.extend(item['required_flags'])
        self.assertIn('--listen', flags)

    def test_tampered_contract_refused(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for engine in ('pi', 'codex', 'claude'):
                target = root / 'adapters' / engine
                target.mkdir(parents=True)
                contract = copy.deepcopy(adapter_contract(engine))
                (target / 'contract.json').write_text(json.dumps(contract))
            pi = json.loads((root / 'adapters/pi/contract.json').read_text())
            pi['exports'] = []
            (root / 'adapters/pi/contract.json').write_text(json.dumps(pi))
            with self.assertRaises(ConfigError):
                adapter_contract('pi', root=root)
            codex = json.loads((root / 'adapters/codex/contract.json').read_text())
            codex['required_flags'] = 'listen'
            (root / 'adapters/codex/contract.json').write_text(json.dumps(codex))
            with self.assertRaises(ConfigError):
                adapter_contract('codex', root=root)

    def test_probes_derive_from_contract(self):
        import sys
        sys.path.insert(0, str(ROOT / 'scripts'))
        import importlib.util
        spec = importlib.util.spec_from_file_location('check_cli', ROOT / 'scripts/check-cli.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv, dependency, (_, flags) = module.probe('codex', ['codex'])
        self.assertEqual(argv, ['codex', 'app-server', '--help'])
        self.assertIn('--listen', flags)
        argv, _, (_, exports) = module.probe('pi', ['node'])
        self.assertTrue(argv[-2].endswith('launcher.mjs'))
        self.assertIn('ModelRuntime', exports)

    def test_no_hardcoded_probe_names(self):
        doctor = (ROOT / 'src/mizu/doctor.py').read_text()
        self.assertNotIn('launcher.mjs', doctor)
        self.assertNotIn('launcher.py', doctor)
        self.assertNotIn("'--listen'", doctor)


if __name__ == '__main__':
    unittest.main()
