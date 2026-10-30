"""Bounded Windows/3.12 prototype contracts; never execute the full suite here."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_check_offline import CHECK, CHILD, INSTALLER, ROOT


class TestIdShardTests(unittest.TestCase):
    def test_all_discovered_ids_partition_exactly_once_in_discovery_order(self):
        # Discovery only: running these thousands of cases would be a full gate.
        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / 'tests'))
        self.assertEqual(loader.errors, [])
        cases = list(CHILD.test_cases(suite))
        ids = [CHILD.stable_test_id(test) for test in cases]
        self.assertGreater(len(ids), 3800)
        self.assertNotIn('unknown_test', ids)
        self.assertEqual(len(ids), len(set(ids)))
        partitions = []
        for shard in (0, 1):
            actual = [test.id() for test in CHILD.select_shard(suite, shard)]
            expected = [identifier for identifier in ids
                        if int(hashlib.sha256(identifier.encode()).hexdigest(), 16) % 2 == shard]
            self.assertEqual(actual, expected)
            # Reversed discovery changes order, not membership.
            reversed_suite = unittest.TestSuite(reversed(cases))
            self.assertEqual([test.id() for test in CHILD.select_shard(reversed_suite, shard)],
                             list(reversed(actual)))
            partitions.append(set(actual))
        self.assertTrue(all(partitions))
        self.assertFalse(partitions[0] & partitions[1])
        self.assertEqual(partitions[0] | partitions[1], set(ids))

    def test_duplicate_unknown_ids_and_invalid_shard_fail_closed(self):
        case = unittest.FunctionTestCase(lambda: None)
        for suite in (unittest.TestSuite([case, case]), unittest.TestSuite([case])):
            with self.assertRaises(ValueError):
                CHILD.select_shard(suite, 0)
        valid = mock.Mock(spec=unittest.TestCase)
        valid.id.return_value = 'sample.Case.test_ok'
        with self.assertRaises(ValueError):
            CHILD.select_shard(unittest.TestSuite([valid, valid]), 1)
        with self.assertRaises(ValueError):
            CHILD.select_shard(unittest.TestSuite(), 2)

    def test_child_default_and_both_shards_keep_tests_fixtures_and_timings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tests').mkdir()
            (root / 'tests/test_sample.py').write_text(
                'import unittest\n'
                'class Sample(unittest.TestCase):\n'
                '    @classmethod\n'
                '    def setUpClass(cls): cls.ready = True\n'
                '    @classmethod\n'
                '    def tearDownClass(cls): assert cls.ready\n' + ''.join(
                    f'    def test_{i}(self): self.assertTrue(self.ready)\n' for i in range(8)),
                encoding='utf-8')
            observed = []
            for shard in (None, 0, 1):
                failure, timing = root / f'fail-{shard}', root / f'time-{shard}'
                command = [sys.executable, str(ROOT / 'scripts/check_unittest.py'),
                           str(failure), str(timing), '--fail-fast']
                if shard is not None:
                    command += ['--test-shard', str(shard)]
                child = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(failure.read_text(), '')
                records = [json.loads(line) for line in timing.read_text().splitlines()]
                starts = [row['test'] for row in records if row['event'] == 'start']
                stops = [row['test'] for row in records if row['event'] == 'stop']
                self.assertEqual(starts, stops)
                self.assertTrue(starts)
                observed.append(set(starts))
            self.assertEqual(len(observed[0]), 8)
            self.assertFalse(observed[1] & observed[2])
            self.assertEqual(observed[0], observed[1] | observed[2])

    def test_gate_default_is_full_and_shards_cannot_supply_reusable_receipts(self):
        # Fake subprocesses test argv forwarding and report identity, not gates.
        for shard in (None, 0, 1):
            with self.subTest(shard=shard), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report_path = root / 'report.json'
                argv = ['check.py', '--no-provision', '--report', str(report_path),
                        '--allow-not-run', INSTALLER.staging_not_run_checks(core_only=True)]
                if shard is not None:
                    argv += ['--test-shard', str(shard)]
                with mock.patch.object(CHECK, 'ROOT', root), \
                        mock.patch.object(CHECK.sys, 'argv', argv), \
                        mock.patch.object(CHECK, 'node_version', return_value='v22.0.0'), \
                        mock.patch.object(CHECK, 'spawn', side_effect=lambda *args, **kwargs: mock.Mock(
                            stdout=io.StringIO(''), wait=mock.Mock(return_value=0))) as spawn, \
                        mock.patch.object(CHECK, 'terminate_process'), \
                        mock.patch.object(CHECK.shutil, 'which', return_value='node'), \
                        mock.patch.object(CHECK, 'pi_sdk_deps_status', return_value=('absent', 'fixture')), \
                        mock.patch.object(CHECK, 'bash_available', return_value=False), \
                        mock.patch.object(CHECK.sys, 'stdout', io.StringIO()):
                    self.assertEqual(CHECK.main(), 0)
                commands = [call.args[0] for call in spawn.call_args_list]
                child = next(cmd for cmd in commands if any('check_unittest.py' in str(x) for x in cmd))
                report = json.loads(report_path.read_text())
                self.assertTrue(report['ok'])
                if shard is None:
                    self.assertNotIn('--test-shard', child)
                    self.assertEqual(report['suite'], 'offline')
                    self.assertNotIn('test_shard', report)
                else:
                    self.assertEqual(child[-2:], ['--test-shard', str(shard)])
                    self.assertEqual(report['suite'], 'offline-shard')
                    self.assertEqual(report['test_shard'], shard)
                    with self.assertRaises(INSTALLER.Denied):
                        INSTALLER.validate_reused_source_checks(report, expected_sha=report['source_sha256'])


class WindowsShardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.jobs = dict(re.findall(r'^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)',
                                   self.workflow, re.M | re.S))

    def test_only_windows_312_is_excluded_and_sharded_with_distinct_artifacts(self):
        core = self.jobs['core']
        self.assertTrue(self.workflow.startswith('name: Offline checks\n'))
        self.assertNotRegex(core, r'(?m)^    name:')  # Keep all other automatic matrix names.
        self.assertIn('        os: [ubuntu-24.04, macos-latest, windows-latest]\n', core)
        self.assertIn('        python: ["3.11", "3.12", "3.13", "3.14"]\n', core)
        matrix = core.split('      matrix:\n', 1)[1].split('    steps:\n', 1)[0]
        self.assertEqual(matrix.split('        exclude:\n')[1],
                         '          - os: windows-latest\n            python: "3.12"\n')
        self.assertNotIn('--test-shard', core)
        shards = self.jobs['windows-312-shards']
        self.assertIn('    name: windows-312-shard (${{ matrix.shard }})\n', shards)
        self.assertIn('    runs-on: windows-latest\n    timeout-minutes: 60\n', shards)
        self.assertIn('      fail-fast: false\n      matrix:\n        shard: [0, 1]\n', shards)
        self.assertIn('          python-version: "3.12"\n', shards)
        core_command = re.search(r'        run: (python scripts/check.py .+)', core).group(1)
        self.assertIn('        run: ' + core_command.replace('check.py ',
                      'check.py --test-shard ${{ matrix.shard }} ', 1) + '\n', shards)
        self.assertEqual(re.findall(r'uses: (.+)', core)[:2], re.findall(r'uses: (.+)', shards)[:2])
        self.assertEqual(re.findall(r'uses: (actions/upload-artifact@.+)', core),
                         re.findall(r'uses: (actions/upload-artifact@.+)', shards))
        for prefix in ('test-timings', 'project-import-diagnostic'):
            self.assertIn(f'          name: {prefix}-windows-latest-3.12-shard-${{{{ matrix.shard }}}}\n', shards)
        self.assertIn('            private-validation/test-timings.json.d/*.json\n', shards)
        self.assertEqual(shards.count('        if: always()\n'), 2)
        self.assertNotIn('continue-on-error', shards)
        self.assertNotRegex(shards, r'(?m)^    if:')
        self.assertEqual(self.workflow.count('--test-shard'), 1)

    def test_exact_single_old_check_name_and_fail_closed_matrix_dependency(self):
        aggregate = self.jobs['windows-312-core']
        name = '    name: core (windows-latest, 3.12)\n'
        self.assertEqual(self.workflow.count(name), 1)
        self.assertIn(name, aggregate)
        self.assertIn('    needs: [windows-312-shards]\n', aggregate)
        self.assertIn('    if: ${{ always() }}\n', aggregate)
        self.assertNotIn('timeout-minutes:', aggregate)
        self.assertIn('          SHARDS_RESULT: ${{ needs.windows-312-shards.result }}\n', aggregate)
        self.assertNotIn('continue-on-error', aggregate)
        self.assertEqual(aggregate.count('      - name:'), 1)
        command = re.search(r'        run: (.+)', aggregate).group(1)
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ['python', '-c'])
        # Execute the exact workflow predicate, not a reimplementation of it.
        for result in ('success', 'failure', 'cancelled', 'skipped', '', None):
            with self.subTest(result=result):
                env = dict(os.environ)
                env.pop('SHARDS_RESULT', None)
                if result is not None:
                    env['SHARDS_RESULT'] = result
                child = subprocess.run([sys.executable, *argv[1:]], env=env,
                                       capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0 if result == 'success' else 1)
