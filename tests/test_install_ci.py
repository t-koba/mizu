"""Synthetic retained CI proof; no provider, full suite, npm or live smoke."""
import argparse
import copy
import dataclasses
import errno
import hashlib
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
        (self.source / 'adapters/pi-durable').mkdir(parents=True)
        (self.source / 'adapters/pi-durable/package-lock.json').write_bytes(b'{}\n')
        for args in (['init', '-q'], ['add', '.'],
                     ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      '-c', 'commit.gpgsign=false',
                      'commit', '-qm', 'Create synthetic source']):
            subprocess.run(['git', *args], cwd=self.source, check=True,
                           capture_output=True, timeout=15)
        self.commit = source_commit(self.source, timeout=self.config.publication.source_commit_timeout_seconds)
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
        self.last_result = result
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
        expected_provenance = {
            'evidence_kind': 'retained-ci-acceptance',
            'receipt_schema': 2,
            'receipt_id': self.receipt_id,
            'publication_id': self.publication['publication_id'],
            'run': '123',
            'attempt': 1,
            'current_attempt_at_ingestion': True,
        }
        self.assertEqual(installed['ci_acceptance_provenance'], expected_provenance)
        self.assertEqual(self.last_result['ci_acceptance_provenance'], expected_provenance)
        self.assertEqual(installed['checks'], self.receipt['evidence']['checks'])
        self.assertEqual(source_manifest(release), self.manifest)
        INSTALLER.verify_release(release)
        # Existing release metadata is immutable; the invocation still displays
        # the exact CI receipt selected for this reuse request.
        self.assertEqual(self.stage(), release)
        self.assertEqual(self.last_result['ci_acceptance_provenance'], expected_provenance)
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
        self.project.config = dataclasses.replace(self.project.config,
            publication=dataclasses.replace(self.config.publication, source_commit_timeout_seconds=13))
        with patch('mizu.publication.source_commit', return_value='f' * 40) as local_head:
            with self.assertRaisesRegex(Denied, 'Git HEAD'):
                self.stage()
        local_head.assert_called_once_with(self.source, timeout=13)
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
        def record(argv, **kwargs):
            calls.append((list(map(str, argv)), str(kwargs.get('cwd'))))
        with patch.object(INSTALLER.shutil, 'which', side_effect=lambda name: name), \
                patch.object(INSTALLER.subprocess, 'check_output', side_effect=['24.0.0', '1.5.0']):
            release = self.stage(self.args(core_only=False), command=record)
        self.assertEqual(len(calls), 4)  # Neither source suite nor other commands.
        self.assertEqual(calls[0][0], ['npm', 'ci', '--engine-strict', '--no-audit', '--no-fund', '--ignore-scripts'])
        self.assertEqual(Path(calls[0][1]).parts[-2:], ('adapters', 'pi'))
        self.assertIn('--check-contract', calls[1][0])
        self.assertEqual(Path(calls[1][1]).parts[-2:], ('adapters', 'pi'))
        self.assertEqual(calls[2][0], ['npm', 'ci', '--engine-strict', '--no-audit', '--no-fund', '--ignore-scripts'])
        self.assertEqual(Path(calls[2][1]).parts[-2:], ('adapters', 'pi-durable'))
        self.assertIn('--check-contract', calls[3][0])
        self.assertEqual(Path(calls[3][1]).parts[-2:], ('adapters', 'pi-durable'))
        receipt = json.loads((release / 'installation.json').read_text())
        self.assertEqual(receipt['pi_lock_sha256'], digest(b'{}\n'))
        self.assertEqual(receipt['pi_durable_lock_sha256'], digest(b'{}\n'))
        self.assertEqual(receipt['checks_provenance'], 'retained-ci-acceptance')

    def test_modes_are_explicit_and_mutually_exclusive(self):
        with self.assertRaisesRegex(Denied, 'mutually exclusive'):
            self.stage(self.args(reuse_source_checks=self.root / 'untrusted.json'))
        with patch.object(sys, 'argv', ['install.py', '--reuse-ci-checks', 'config', 'sample',
                self.snapshot['id'], '--reuse-source-checks', 'report']), \
                patch('sys.stderr'), self.assertRaises(SystemExit) as exit:
            INSTALLER.main()
        self.assertEqual(exit.exception.code, 2)


def _absolute_bash():
    """Resolve an absolute shell; a PATH entry can be a shim, not bash."""
    system = '/bin/bash'
    if os.path.isfile(system) and os.access(system, os.X_OK):
        return system
    found = shutil.which('bash')
    return os.path.abspath(found) if found else None


@unittest.skipUnless(os.name == 'posix' and _absolute_bash(), 'POSIX Bash wrapper')
class InstallWrapperCITests(unittest.TestCase):
    @staticmethod
    def _hermetic_wrapper_env():
        # Non-interactive bash sources BASH_ENV before the script body.
        return {k: v for k, v in os.environ.items() if k not in ('BASH_ENV', 'ENV')}

    def _bash_control(self, env=None):
        if env is None:
            env = self._hermetic_wrapper_env()
        try:
            probe = subprocess.run([_absolute_bash(), '-c', 'printf mizu-control'],
                capture_output=True, timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return False, False
        return (probe.returncode == 0 and probe.stdout == b'mizu-control',
                len(probe.stderr) == 0)

    def read_wrapper_trace(self, root, mode_index, returncode, *, stdout='', stderr=''):
        trace = root / 'trace.jsonl'
        try:
            return trace.read_text()
        except OSError as error:
            # Only fixed, relative identities may leave this disposable fixture.
            stage_paths = {'prefix': 'prefix with spaces', 'bin': 'bin with spaces',
                           'config': 'private configuration/config.toml'}
            try:
                observed = json.loads((root / 'setup.invoked.json').read_text())
            except (OSError, ValueError):
                observed = None
            wrapper_path = root / 'scripts' / 'test-install.sh'
            try:
                wrapper_bytes = wrapper_path.stat().st_size if wrapper_path.is_file() else None
            except OSError:
                wrapper_bytes = None
            try:
                with open(wrapper_path, 'rb') as handle:
                    file_digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1048576), b''):
                        file_digest.update(chunk)
                    wrapper_sha256 = file_digest.hexdigest()
            except OSError:
                wrapper_sha256 = None
            control_ok, control_stderr_empty = self._bash_control()
            stderr_bytes = stderr.encode('utf-8')
            error.add_note('wrapper trace diagnostic: ' + json.dumps({
                'mode_index': mode_index, 'wrapper_returncode': returncode,
                'root_exists': root.exists(), 'trace_exists': trace.exists(),
                'setup_invoked': (root / 'setup.invoked.json').exists(),
                'cli_invoked': (root / 'cli.invoked').exists(),
                'fixture_paths': {'root': '.', 'trace': 'trace.jsonl',
                    'wrapper': 'scripts/test-install.sh',
                    'setup': 'scripts/fake-setup.py', 'cli': 'scripts/fake-cli.py'},
                'stage_paths': stage_paths if observed == stage_paths else None,
                'errno': error.errno,
                'wrapper_stdout_empty': stdout == '',
                'wrapper_stderr_empty': stderr == '',
                'wrapper_bytes': wrapper_bytes,
                'wrapper_sha256': wrapper_sha256,
                'wrapper_stderr_bytes': len(stderr_bytes),
                'wrapper_stderr_sha256': hashlib.sha256(stderr_bytes).hexdigest(),
                'bash_control_ok': control_ok,
                'bash_control_stderr_empty': control_stderr_empty,
            }, sort_keys=True))
            raise

    def test_missing_trace_diagnostic_is_bounded_and_private(self):
        with tempfile.TemporaryDirectory(prefix='mizu-wrapper-') as temporary:
            root = Path(temporary)
            sentinel = 'SYNTHETIC-CREDENTIAL-DO-NOT-EMIT'
            stage_paths = {'prefix': 'prefix with spaces', 'bin': 'bin with spaces',
                           'config': 'private configuration/config.toml'}
            (root / 'setup.invoked.json').write_text(json.dumps(stage_paths))
            (root / 'cli.invoked').touch()
            (root / 'config.toml').write_text(sentinel)
            for observed in (stage_paths, {'config': str(root / sentinel)}):
                with self.subTest(valid_stage_paths=observed == stage_paths):
                    (root / 'setup.invoked.json').write_text(json.dumps(observed))
                    with patch.object(InstallWrapperCITests, '_bash_control',
                                      return_value=(True, True)):
                        with self.assertRaises(FileNotFoundError) as caught:
                            self.read_wrapper_trace(root, 2, 0)
                    error = caught.exception
                    self.assertEqual(error.errno, errno.ENOENT)
                    self.assertEqual(error.filename, str(root / 'trace.jsonl'))
                    note, = error.__notes__
                    details = json.loads(note.removeprefix('wrapper trace diagnostic: '))
                    self.assertEqual(details, {
                        'mode_index': 2, 'wrapper_returncode': 0,
                        'root_exists': True, 'trace_exists': False,
                        'setup_invoked': True, 'cli_invoked': True,
                        'fixture_paths': {'root': '.', 'trace': 'trace.jsonl',
                            'wrapper': 'scripts/test-install.sh',
                            'setup': 'scripts/fake-setup.py', 'cli': 'scripts/fake-cli.py'},
                        'stage_paths': stage_paths if observed == stage_paths else None,
                        'errno': errno.ENOENT,
                        'wrapper_stdout_empty': True,
                        'wrapper_stderr_empty': True,
                        'wrapper_bytes': None,
                        'wrapper_sha256': None,
                        'wrapper_stderr_bytes': 0,
                        'wrapper_stderr_sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
                        'bash_control_ok': True,
                        'bash_control_stderr_empty': True,
                    })
                    for private in (temporary, sentinel, str(error)):
                        self.assertNotIn(private, note)

    def test_wrapper_sha256_covers_whole_file_beyond_single_chunk(self):
        with tempfile.TemporaryDirectory(prefix='mizu-wrapper-hash-') as temporary:
            root = Path(temporary)
            scripts = root / 'scripts'
            scripts.mkdir()
            payload = bytes(range(256)) * 5000
            self.assertGreater(len(payload), 1048576)
            (scripts / 'test-install.sh').write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            with patch.object(InstallWrapperCITests, '_bash_control',
                              return_value=(True, True)):
                with self.assertRaises(FileNotFoundError) as caught:
                    self.read_wrapper_trace(root, 0, 0)
            note, = caught.exception.__notes__
            details = json.loads(note.removeprefix('wrapper trace diagnostic: '))
            self.assertEqual(details['wrapper_bytes'], len(payload))
            self.assertEqual(details['wrapper_sha256'], expected)

    def test_all_modes_reach_same_bounded_smoke_flow_with_mock_setup_and_cli(self):
        # This proves wrapper control flow, not real installation or live Pi.
        with tempfile.TemporaryDirectory(prefix='mizu-wrapper-') as temporary:
            root = Path(temporary)
            scripts = root / 'scripts'
            scripts.mkdir()
            shutil.copy2(ROOT / 'scripts/test-install.sh', scripts / 'test-install.sh')
            trace = root / 'trace.jsonl'
            cli = scripts / 'fake-cli.py'
            cli_marker = root / 'cli.invoked'
            setup_marker = root / 'setup.invoked.json'
            cli.write_text('import json, pathlib, sys\n'
                f'pathlib.Path({str(cli_marker)!r}).touch()\n'
                f'with open({str(trace)!r}, "a") as log: log.write(json.dumps(["cli", *sys.argv[1:]]) + "\\n")\n'
                'print(json.dumps({"armed": False, "control": {"paused": True}}))\n')
            launcher = '#!/usr/bin/env bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(cli)) + ' \"$@\"\n'
            config_text = 'data_dir = \"~/.local/state/mizu\"\n'
            setup = scripts / 'fake-setup.py'
            setup.write_text('import json, pathlib, sys\n'
                'args = sys.argv[1:]\n'
                'def option(name): return pathlib.Path(args[args.index(name) + 1])\n'
                'stage = option("--prefix").parent\n'
                f'pathlib.Path({str(setup_marker)!r}).write_text(json.dumps({{\n'
                '    key: option(flag).relative_to(stage).as_posix() for key, flag in\n'
                '    [("prefix", "--prefix"), ("bin", "--bin-dir"), ("config", "--config")]}))\n'
                f'with open({str(trace)!r}, "a") as log: log.write(json.dumps(["setup", *args]) + "\\n")\n'
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
            for mode_index, selection in enumerate(selections):
                with self.subTest(selection=selection):
                    trace.unlink(missing_ok=True)
                    (root / 'setup.invoked.json').unlink(missing_ok=True)
                    (root / 'cli.invoked').unlink(missing_ok=True)
                    result = subprocess.run([_absolute_bash(), str(scripts / 'test-install.sh'), *selection],
                        capture_output=True, text=True, timeout=30,
                        env=self._hermetic_wrapper_env())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [json.loads(line) for line in
                             self.read_wrapper_trace(root, mode_index, result.returncode,
                                stdout=result.stdout, stderr=result.stderr).splitlines()]
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

    def test_wrapper_ignores_hostile_bash_env_when_hermetic(self):
        with tempfile.TemporaryDirectory(prefix='mizu-wrapper-bash-env-') as temporary:
            root = Path(temporary)
            scripts = root / 'scripts'
            scripts.mkdir()
            shutil.copy2(ROOT / 'scripts/test-install.sh', scripts / 'test-install.sh')
            marker = root / 'setup.invoked'
            setup = scripts / 'setup.sh'
            setup.write_text('#!/usr/bin/env bash\ntouch ' + shlex.quote(str(marker)) + '\nexit 0\n')
            setup.chmod(0o755)
            bash_env = root / 'bash_env'
            bash_env.write_text('exit 0\n')
            hostile = dict(os.environ)
            hostile['BASH_ENV'] = str(bash_env)
            silent = subprocess.run([_absolute_bash(), str(scripts / 'test-install.sh')],
                capture_output=True, text=True, timeout=30, env=hostile)
            # Ambient BASH_ENV ending in exit 0 makes bash exit before the body.
            self.assertEqual(silent.returncode, 0)
            self.assertEqual(silent.stdout, '')
            self.assertEqual(silent.stderr, '')
            self.assertFalse(marker.exists())
            hostile_ok, hostile_stderr_empty = self._bash_control(env=hostile)
            self.assertFalse(hostile_ok)
            with patch.dict(os.environ, {'BASH_ENV': str(bash_env)}):
                hermetic = subprocess.run([_absolute_bash(), str(scripts / 'test-install.sh')],
                    capture_output=True, text=True, timeout=30,
                    env=self._hermetic_wrapper_env())
            self.assertTrue(marker.exists())
            self.assertNotEqual(hermetic.returncode, 0)
            hermetic_ok, hermetic_stderr_empty = self._bash_control(
                env=self._hermetic_wrapper_env())
            self.assertTrue(hermetic_ok)
            self.assertTrue(hermetic_stderr_empty)
            self.assertTrue(hostile_stderr_empty)
