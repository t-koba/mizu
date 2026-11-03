"""Doctor-owned host command wall bounds and their configured range."""
import dataclasses

from support import Fixture
from mizu.config import DOCTOR_TIMEOUT_MAX_SECONDS, Doctor, TOML_INTEGER_MAX, load
from mizu.errors import ConfigError


class DoctorConfigTests(Fixture):
    def test_default_and_range(self):
        expected = {'runtime_version_seconds': 20,
                    'namespace_helper_seconds': 10,
                    'image_inspect_seconds': 15,
                    'node_capability_seconds': 10,
                    'sandbox_smoke_seconds': 30}
        self.assertEqual(dataclasses.asdict(self.config.doctor), expected)
        self.assertEqual(dataclasses.asdict(Doctor()), expected)
        original = self.file.read_text()
        accepted = (1, 10, 20, DOCTOR_TIMEOUT_MAX_SECONDS)
        for name in expected:
            for value in (*accepted, 0, -1, DOCTOR_TIMEOUT_MAX_SECONDS + 1,
                          TOML_INTEGER_MAX + 1):
                with self.subTest(name=name, value=value):
                    self.file.write_text(original + '\n[doctor]\n'
                                         f'{name} = {value}\n')
                    if value in accepted:
                        configured = load(self.file).doctor
                        self.assertEqual(getattr(configured, name), value)
                    else:
                        with self.assertRaisesRegex(ConfigError, f'doctor.{name}'):
                            load(self.file)
        for value in ('true', 'false', '1.5', '"1"', '[]', '{}'):
            with self.subTest(value=value):
                self.file.write_text(original + '\n[doctor]\n'
                                     f'node_capability_seconds = {value}\n')
                with self.assertRaisesRegex(ConfigError, 'doctor.node_capability_seconds'):
                    load(self.file)
        self.file.write_text(original + '\n[doctor]\n')
        self.assertEqual(load(self.file).doctor, Doctor())

    def test_unknown_keys_non_table_and_include(self):
        original = self.file.read_text()
        for text in (original + '\n[doctor]\ntimeout_seconds = 1\n',
                     'doctor = 1\n' + original,
                     'doctor = []\n' + original):
            with self.subTest(text=text[-40:]):
                self.file.write_text(text)
                with self.assertRaisesRegex(ConfigError, 'doctor'):
                    load(self.file)
        (self.file.parent / 'doctor.toml').write_text(
            '[doctor]\nruntime_version_seconds = 7\nnode_capability_seconds = 11\n')
        self.file.write_text('include = ["doctor.toml"]\n' + original)
        self.assertEqual(load(self.file).doctor,
                         Doctor(runtime_version_seconds=7, node_capability_seconds=11))
