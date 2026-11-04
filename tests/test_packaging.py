"""Strict release lock validation; no npm execution or network."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('mizu_package', ROOT / 'scripts/package.py')
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)
REQUIRED = {'pi': ('@earendil-works/pi-coding-agent', '@earendil-works/pi-ai'),
            'pi-durable': ('@earendil-works/chord', '@earendil-works/pi-ai',
                           '@earendil-works/pi-coding-agent', '@earendil-works/pi-durable')}


def package(version='1.5.0', integrity='sha512-test'):
    return {'version': version, 'integrity': integrity}


def packages(version='1.5.0'):
    return {'node_modules/' + name: package(version)
            for name in {name for names in REQUIRED.values() for name in names}}


class PackageLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {'pi': self.root / 'pi-lock.json',
                      'pi-durable': self.root / 'pi-durable-lock.json'}

    def write(self, adapter, value):
        self.paths[adapter].write_text(json.dumps(value), encoding='utf-8')

    def valid(self, version='1.5.0'):
        for adapter in self.paths:
            self.write(adapter, {'lockfileVersion': 3,
                                 'packages': {'': {}, **packages(version)}})

    def test_accepts_two_complete_locks_on_one_version(self):
        self.valid()
        self.assertEqual(PACKAGE.validate_locks(self.paths, REQUIRED),
                         {'pi': '1.5.0', 'pi-durable': '1.5.0'})

    def test_nested_non_pi_scoped_package_does_not_invalidate_lock(self):
        self.valid()
        value = json.loads(self.paths['pi'].read_text())
        value['packages']['node_modules/@earendil-works/pi-coding-agent/node_modules/@silvia-odwyer/photon-node'] = {
            'version': '0.3.4'}
        self.write('pi', value)
        self.assertEqual(PACKAGE.validate_locks(self.paths, REQUIRED),
                         {'pi': '1.5.0', 'pi-durable': '1.5.0'})

    def test_missing_lock_refuses(self):
        self.valid()
        self.paths['pi-durable'].unlink()
        with self.assertRaisesRegex(ValueError, 'requires reviewed adapters/pi-durable'):
            PACKAGE.validate_locks(self.paths, REQUIRED)

    def test_missing_integrity_refuses(self):
        self.valid()
        value = json.loads(self.paths['pi'].read_text())
        value['packages']['node_modules/@earendil-works/pi-ai'].pop('integrity')
        self.write('pi', value)
        with self.assertRaisesRegex(ValueError, 'registry integrity'):
            PACKAGE.validate_locks(self.paths, REQUIRED)

    def test_mixed_versions_refuse(self):
        self.valid()
        value = json.loads(self.paths['pi-durable'].read_text())
        value['packages']['node_modules/@earendil-works/pi-ai'] = package('1.4.0')
        self.write('pi-durable', value)
        with self.assertRaisesRegex(ValueError, 'mixes'):
            PACKAGE.validate_locks(self.paths, REQUIRED)

    def test_cross_lock_version_mismatch_refuses(self):
        self.valid()
        value = json.loads(self.paths['pi-durable'].read_text())
        for name in REQUIRED['pi-durable']:
            value['packages']['node_modules/' + name] = package('1.4.0')
        self.write('pi-durable', value)
        with self.assertRaisesRegex(ValueError, 'same @earendil-works version'):
            PACKAGE.validate_locks(self.paths, REQUIRED)

    def test_incomplete_lock_refuses(self):
        self.valid()
        self.write('pi', {'lockfileVersion': 1, 'packages': packages()})
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            PACKAGE.validate_locks(self.paths, REQUIRED)

    def test_unreadable_lock_refuses(self):
        self.valid()
        self.paths['pi'].write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unreadable'):
            PACKAGE.validate_locks(self.paths, REQUIRED)


if __name__ == '__main__':
    unittest.main()
