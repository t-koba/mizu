"""Synthetic retained CI proof; no provider, full suite, npm or live smoke."""
import argparse
import copy
import dataclasses
import importlib.util
import json
import os
import shlex
import tempfile
import unittest
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from support import Fixture, ROOT
from mizu import acceptance, ci_acceptance
from mizu.distribution import public_source_identity, source_manifest
from mizu.errors import Denied
from mizu.fs import canonical, digest, write_json
from mizu.publication import Publication, source_commit

spec = importlib.util.spec_from_file_location('install_ci', ROOT / 'scripts/install.py')
INSTALLER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(INSTALLER)


class InstallCITests(Fixture):
    def setUp(self):
        super().setUp()
        if not shutil.which('git'):
            self.skipTest('local Git required for source-commit binding')
        self.project.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True),
            vcs={**self.config.vcs, 'command': ['synthetic-adapter-never-invoked']})
        self.source = self.project.workspace
        (self.source / 'src').mkdir()
        (self.source / 'src/probe.py').write_bytes(b'VALUE = 1\n')
        (self.source / 'adapters/pi').mkdir(parents=True)
        (self.source / 'adapters/pi/package-lock.json').write_bytes(b'{}\n')
        for args in (['init', '-q'], ['add', '.'],
                     ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      '-c', 'commit.gpgsign=false',
                      'commit', '-qm', 'Create synthetic source']):
            subprocess.run(['git', *args], cwd=self.source, check=True,
                           capture_output=True, timeout=15)
        self.commit = source_commit(self.source)
        self.capture = self.project.snapshots.capture_files(self.source)
        self.snapshot = self.project.snapshots.create(self.capture, goal=self.project.goal,
            state='', run=None, outcome='wait', summary='Synthetic source')
        self.project.snapshots.publish(self.snapshot)
        self.manifest = source_manifest(self.source)
        self.local = acceptance.scope(self.project)
        self.project.settings['ci_acceptance'] = {
            'schema': 1, 'version': 'reviewed-v1', 'repository': 'example/project',
            'workflow': '.github/workflows/offline.yml', 'workflow_digest': 'a' * 64,
            'acceptance_scope': acceptance._scope(self.local),
            'checks': [{'check': name, 'commands': [0], 'coverage': ['complete-offline'],
                        'environment': {'os': os_name, 'python': '3.14'}}
                       for name, os_name in [('core-linux', 'linux'), ('core-windows', 'windows')]]}
        self.publication = Publication.record_external(self.project.root, branch='mizu',
            published_sha='c' * 40, code_digest=self.capture['code_digest'],
            approval='synthetic-review', source_commit=self.commit,
            projection=public_source_identity(self.capture['files']))
        policy = self.project.settings['ci_acceptance']
        self.receipt = {
            'schema': 2, 'code_digest': self.capture['code_digest'],
            'scope': ci_acceptance.bound(self.project, self.snapshot, self.local),
            'publication_id': self.publication['publication_id'], 'passed': True,
            'evidence': {'schema': 1, **{key: policy[key] for key in
                ('repository', 'workflow', 'workflow_digest')},
                'sha': 'c' * 40, 'code_digest': self.capture['code_digest'],
                'run': '123', 'attempt': 1, 'current_attempt': True, 'complete': True,
                'checks': [{**copy.deepcopy(row), 'check_id': str(index), 'conclusion': 'success'}
                           for index, row in enumerate(policy['checks'])]}}
        self.receipt_id = acceptance._retain(self.project.root, self.receipt)
        self.index = self.project.root / acceptance._index(self.project.root,
            self.capture['code_digest'], self.receipt['scope'])
        for target in ('mizu.vcs.invoke', 'mizu.ci_acceptance.retain', 'mizu.pi.credentials',
                       'mizu.acceptance.effective'):
            guard = patch(target, side_effect=AssertionError('no provider/credentials/fallback'))
            guard.start()
            self.addCleanup(guard.stop)
        root_patch = patch.object(INSTALLER, 'ROOT', self.source)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def validate(self):
        return INSTALLER.validate_ci_source_checks(self.project, self.snapshot['id'], self.manifest)

    def args(self, **overrides):
        values = dict(core_only=True, reuse_source_checks=None,
            reuse_ci_checks=[str(self.file), 'sample', self.snapshot['id']],
            prefix=self.root / 'prefix', bin_dir=self.root / 'bin',
            config=self.root / 'installed-config.toml', allow_install_scripts=False,
            stage_only=True, activate=False, allow_mode_change=False)
        return argparse.Namespace(**(values | overrides))

    def stage(self, args=None, command=None):
        with patch.object(INSTALLER._platform, 'is_root', return_value=False), \
                patch.object(INSTALLER, 'source_manifest', side_effect=lambda root=self.source: source_manifest(root)), \
                patch.object(INSTALLER, 'load', return_value=self.project.config), \
                patch('mizu.project.Project', return_value=self.project) as project, \
                patch.object(INSTALLER, 'command', side_effect=command or AssertionError('no source suite')):
            result = INSTALLER.stage(args or self.args())
            project.assert_called_once_with(self.project.config, 'sample', read_only=True)
        return Path(result['release'])

    def test_exact_match_skips_only_source_gate_and_records_exact_receipt(self):
        with patch.object(self.project.snapshots, 'capture_files',
                          wraps=self.project.snapshots.capture_files) as capture:
            self.assertEqual(self.validate(), (self.receipt_id, self.receipt))
            capture.assert_called_once_with(self.source, persist=False)
        release = self.stage()
        installed = json.loads((release / 'installation.json').read_text())
        self.assertEqual(installed['checks_provenance'], 'retained-ci-acceptance')
        self.assertEqual(installed['ci_acceptance_receipt_id'], self.receipt_id)
        self.assertEqual(installed['checks'], self.receipt['evidence']['checks'])
        self.assertEqual(source_manifest(release), self.manifest)
        INSTALLER.verify_release(release)
        # Even an existing candidate cannot bypass newly absent proof.
        self.index.unlink()
        with self.assertRaises(Denied):
            self.stage()

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX symlink support')
    def test_existing_release_verification_allows_symlinked_prefix_ancestor(self):
        release = self.stage()
        alias = self.root / 'prefix-alias'
        alias.symlink_to(release.parent.parent, target_is_directory=True)
        via_alias = alias / release.parent.name / release.name
        INSTALLER.verify_release(via_alias)

    def test_existing_release_source_tampering_is_rejected_before_ci_reuse(self):
        release = self.stage()
        probe = release / 'src/probe.py'
        probe.write_bytes(b'TAMPERED = 1\n')
        with self.assertRaisesRegex(Denied, 'source files do not match'):
            self.stage()

    def test_operator_config_project_and_receipt_load_without_adapter_calls(self):
        text = self.file.read_text().replace('check_reuse = false', 'check_reuse = true')
        text = text.replace('[vcs]\ncommand = []', '[vcs]\ncommand = ["synthetic-adapter-never-invoked"]')
        self.file.write_text(text)
        policy = self.project.settings['ci_acceptance']
        lines = ['\n[ci_acceptance]']
        for key in ('schema', 'version', 'repository', 'workflow', 'workflow_digest', 'acceptance_scope'):
            lines.append(f'{key} = {json.dumps(policy[key])}')
        for row in policy['checks']:
            lines.append('[[ci_acceptance.checks]]')
            for key in ('check', 'commands', 'coverage'):
                lines.append(f'{key} = {json.dumps(row[key])}')
            lines.append('[ci_acceptance.checks.environment]')
            lines.extend(f'{key} = {json.dumps(value)}' for key, value in row['environment'].items())
        with (self.project.root / 'project.toml').open('a') as stream:
            stream.write('\n'.join(lines) + '\n')
        with patch.object(INSTALLER._platform, 'is_root', return_value=False), \
                patch.object(INSTALLER, 'source_manifest', side_effect=lambda root=self.source: source_manifest(root)), \
                patch.object(INSTALLER, 'command', side_effect=AssertionError('no source suite')):
            result = INSTALLER.stage(self.args())
        receipt = json.loads((Path(result['release']) / 'installation.json').read_text())
        self.assertEqual(receipt['ci_acceptance_receipt_id'], self.receipt_id)

    def test_absent_or_local_only_proof_does_not_fall_back(self):
        self.index.unlink()
        local_receipt = {'schema': 1, 'code_digest': self.capture['code_digest'],
            'scope': self.local, 'producer': {'kind': 'verify', 'id': 'd' * 32, 'run': 'd' * 32},
            'passed': True, 'commands': [{'id': 'e' * 32, 'exit_code': 0, 'reason': 'exited'}]}
        acceptance._retain(self.project.root, local_receipt)
        with self.assertRaises(Denied):
            self.stage()

    def test_changed_current_policy_and_acceptance_scope_refuse(self):
        original = copy.deepcopy(self.project.settings['ci_acceptance'])
        for key, value in [('version', 'reviewed-v2'), ('workflow_digest', 'f' * 64),
                           ('acceptance_scope', 'f' * 64), ('checks', original['checks'][:1])]:
            with self.subTest(key=key):
                self.project.settings['ci_acceptance'] = {**original, key: value}
                with self.assertRaises(Denied):
                    self.stage()
        self.project.settings['ci_acceptance'] = original
        self.project.verify = ('different-command',)
        with self.assertRaises(Denied):
            self.stage()

    def test_same_tree_different_snapshot_cannot_select_original_proof(self):
        snapshot = self.project.snapshots.create(self.capture, goal=self.project.goal,
            state='different snapshot', run=None, outcome='wait', summary='Same tree')
        self.project.snapshots.publish(snapshot)
        with self.assertRaises(Denied):
            INSTALLER.validate_ci_source_checks(self.project, snapshot['id'], self.manifest)
        # The explicitly named older published snapshot remains eligible.
        self.assertEqual(self.validate()[0], self.receipt_id)

    def test_tampered_retained_evidence_refuses_at_install(self):
        mutations = [lambda r: r['evidence'].update(current_attempt=False),
                     lambda r: r['evidence'].update(complete=False),
                     lambda r: r['evidence'].update(checks=r['evidence']['checks'][:1]),
                     lambda r: r['evidence']['checks'][0].update(conclusion='failure'),
                     lambda r: r['evidence']['checks'][0].update(check='wrong-check'),
                     lambda r: r['evidence']['checks'][0].update(coverage=['partial']),
                     lambda r: r['evidence'].update(sha='f' * 40),
                     lambda r: r.update(code_digest='f' * 64),
                     lambda r: r['evidence'].update(code_digest='f' * 64)]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                bad = copy.deepcopy(self.receipt)
                mutate(bad)
                identity = digest(canonical(bad))
                # Simulated disk tamper, deliberately bypassing the trusted writer.
                write_json(self.project.root / f'evidence/acceptance/objects/{identity}.json', bad)
                write_json(self.index, {'schema': 1, 'receipt_id': identity})
                with self.assertRaises(Denied):
                    self.stage()

    def test_source_digest_head_and_public_manifest_are_independent_bindings(self):
        with patch('mizu.publication.source_commit', return_value='f' * 40):
            with self.assertRaisesRegex(Denied, 'Git HEAD'):
                self.stage()
        for field, value in [('code_digest', 'f' * 64), ('skipped', ['large-file']),
                             ('reserved', ['refs/remotes/upstream/main'])]:
            with self.subTest(field=field), patch.object(self.project.snapshots, 'capture_files',
                    return_value={**self.capture, field: value}):
                with self.assertRaises(Denied):
                    self.stage()
        self.manifest = {}
        with self.assertRaisesRegex(Denied, 'distribution manifest'):
            self.validate()

    def test_actual_source_mutation_refuses_and_copy_equality_remains_mandatory(self):
        (self.source / 'src/probe.py').write_bytes(b'VALUE = 9\n')
        with self.assertRaises(Denied):
            self.stage()
        (self.source / 'src/probe.py').write_bytes(b'VALUE = 1\n')
        original_copy = shutil.copy2
        def changed_copy(source, target):
            original_copy(source, target)
            if Path(target).name == 'probe.py':
                Path(target).write_bytes(b'CHANGED = True\n')
        with patch.object(INSTALLER.shutil, 'copy2', side_effect=changed_copy):
            with self.assertRaisesRegex(Denied, 'Source changed while staging'):
                self.stage()
        self.assertFalse(list((self.root / 'prefix/releases').glob('.stage-*')))

    def test_unpublished_snapshot_and_unconfigured_policy_refuse(self):
        with patch.object(self.project.snapshots, '_history_ids', return_value=[]):
            with self.assertRaises(Denied):
                self.validate()
        self.project.settings.pop('ci_acceptance')
        with self.assertRaises(Denied):
            self.stage()

    def test_publication_projection_identity_is_checked(self):
        wrong = Publication.record_external(self.project.root, branch='mizu',
            published_sha='c' * 40, code_digest=self.capture['code_digest'],
            approval='synthetic-review', source_commit=self.commit,
            projection={**public_source_identity(self.capture['files']), 'digest': 'f' * 64})
        receipt = {**self.receipt, 'publication_id': wrong['publication_id']}
        identity = digest(canonical(receipt))
        write_json(self.project.root / f'evidence/acceptance/objects/{identity}.json', receipt)
        write_json(self.index, {'schema': 1, 'receipt_id': identity})
        with self.assertRaisesRegex(Denied, 'projection mismatch'):
            self.stage()

    def test_ci_mode_still_provisions_pinned_pi_and_runs_contract(self):
        calls = []
        with patch.object(INSTALLER.shutil, 'which', side_effect=lambda name: name), \
                patch.object(INSTALLER.subprocess, 'check_output', side_effect=['24.0.0', '1.3.0']):
            release = self.stage(self.args(core_only=False),
                                 command=lambda argv, **kwargs: calls.append(list(map(str, argv))))
        self.assertEqual(calls[0], ['npm', 'ci', '--engine-strict', '--no-audit', '--no-fund', '--ignore-scripts'])
        self.assertIn('--check-contract', calls[1])
        self.assertEqual(len(calls), 2)  # Neither source suite nor other commands.
        receipt = json.loads((release / 'installation.json').read_text())
        self.assertEqual(receipt['pi_lock_sha256'], digest(b'{}\n'))
        self.assertEqual(receipt['checks_provenance'], 'retained-ci-acceptance')

    def test_modes_are_explicit_and_mutually_exclusive(self):
        with self.assertRaisesRegex(Denied, 'mutually exclusive'):
            self.stage(self.args(reuse_source_checks=self.root / 'untrusted.json'))
        with patch.object(sys, 'argv', ['install.py', '--reuse-ci-checks', 'config', 'sample',
                self.snapshot['id'], '--reuse-source-checks', 'report']), \
                patch('sys.stderr'), self.assertRaises(SystemExit) as exit:
            INSTALLER.main()
        self.assertEqual(exit.exception.code, 2)


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'POSIX Bash wrapper')
class InstallWrapperCITests(unittest.TestCase):
    def test_all_modes_reach_same_bounded_smoke_flow_with_mock_setup_and_cli(self):
        # This proves wrapper control flow, not real installation or live Pi.
        with tempfile.TemporaryDirectory(prefix='mizu-wrapper-') as temporary:
            root = Path(temporary)
            scripts = root / 'scripts'
            scripts.mkdir()
            shutil.copy2(ROOT / 'scripts/test-install.sh', scripts / 'test-install.sh')
            trace = root / 'trace.jsonl'
            cli = scripts / 'fake-cli.py'
            cli.write_text('import json, sys\n'
                f'with open({str(trace)!r}, "a") as log: log.write(json.dumps(["cli", *sys.argv[1:]]) + "\\n")\n'
                'print(json.dumps({"armed": False, "control": {"paused": True}}))\n')
            launcher = '#!/usr/bin/env bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(cli)) + ' \"$@\"\n'
            config_text = 'data_dir = \"~/.local/state/mizu\"\n'
            setup = scripts / 'fake-setup.py'
            setup.write_text('import json, pathlib, sys\n'
                'args = sys.argv[1:]\n'
                f'with open({str(trace)!r}, "a") as log: log.write(json.dumps(["setup", *args]) + "\\n")\n'
                'def option(name): return pathlib.Path(args[args.index(name) + 1])\n'
                'config = option("--config"); config.parent.mkdir(parents=True, exist_ok=True)\n'
                f'if not config.exists(): config.write_text({config_text!r})\n'
                'bin_dir = option("--bin-dir"); bin_dir.mkdir(exist_ok=True)\n'
                f'launcher = {launcher!r}\n'
                '(bin_dir / "mizu").write_text(launcher); (bin_dir / "mizu").chmod(0o755)\n'
                'release = option("--prefix") / "current"; release.mkdir(parents=True, exist_ok=True)\n'
                'provenance = ("retained-ci-acceptance" if "--reuse-ci-checks" in args else\n'
                '              "reused-source-gate" if "--reuse-source-checks" in args else "staged-gate")\n'
                '(release / "installation.json").write_text(json.dumps({"pi_version": None,\n'
                '    "validation": "local-install-checks-passed", "checks_provenance": provenance}))\n'
                'print("{}")\n')
            (scripts / 'setup.sh').write_text('#!/usr/bin/env bash\nexec ' +
                shlex.quote(sys.executable) + ' ' + shlex.quote(str(setup)) + ' "$@"\n')
            (scripts / 'setup.sh').chmod(0o755)
            selections = [[], ['source report.json'],
                          ['--reuse-ci-checks', 'operator config.toml', 'sample', 'a' * 64]]
            for selection in selections:
                with self.subTest(selection=selection):
                    trace.unlink(missing_ok=True)
                    result = subprocess.run(['bash', str(scripts / 'test-install.sh'), *selection],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [json.loads(line) for line in trace.read_text().splitlines()]
                    installs = [row for row in calls if row[0] == 'setup']
                    self.assertEqual(len(installs), 2)
                    forwarded = (['--reuse-source-checks', *selection] if len(selection) == 1 else selection)
                    for call in installs:
                        if forwarded:
                            self.assertEqual(call[-len(forwarded):], forwarded)
                        else:
                            self.assertNotIn('--reuse-ci-checks', call)
                            self.assertNotIn('--reuse-source-checks', call)
                    commands = [row[3] if row[1] == '--config' else row[1]
                                for row in calls if row[0] == 'cli']
                    self.assertEqual(commands, ['--version', 'init', 'report', 'editor', 'backup', 'restore', 'status'])
                    self.assertIn('PASS: offline installation', result.stdout)
