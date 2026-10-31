"""Synthetic authenticated-adapter facts only; no build, CI or network requests."""
import copy
import dataclasses
import json
import sys
from unittest.mock import patch

from support import Fixture, bind_delivery
from mizu import acceptance, check_jobs, ci_acceptance, vcs
from mizu.cli import execute, parser
from mizu.distribution import public_source_identity
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest, write_json
from mizu.project import Project, check_project_settings
from mizu.publication import Publication


class CIAcceptanceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config,
            sandbox=dataclasses.replace(self.config.sandbox, check_reuse=True))
        self.project.config = self.config
        self.snapshot = self.project.snapshots.get()
        self.local = acceptance.scope(self.project)
        self.spec = {
            'schema': 1, 'version': 'reviewed-v1', 'repository': 'example/project',
            'workflow': '.github/workflows/offline.yml', 'workflow_digest': 'a' * 64,
            'acceptance_scope': digest(canonical(self.local)),
            'checks': [{'check': 'core-linux', 'commands': [0],
                        'coverage': ['complete-offline-scope'],
                        'environment': {'os': 'linux', 'python': '3.14', 'image': 'sha256:' + 'b' * 64}}]}
        self.project.settings['ci_acceptance'] = copy.deepcopy(self.spec)
        self.publication = Publication.record_external(
            self.project.root, branch='mizu', published_sha='c' * 40,
            code_digest=self.snapshot['code_digest'], approval='operator-review',
            projection=public_source_identity(self.snapshot['files']))
        self.response = {
            'schema': 1, 'repository': self.spec['repository'], 'workflow': self.spec['workflow'],
            'workflow_digest': self.spec['workflow_digest'], 'sha': 'c' * 40,
            'code_digest': self.snapshot['code_digest'], 'run': '123', 'attempt': 1,
            'current_attempt': True, 'complete': True,
            'checks': [{**copy.deepcopy(self.spec['checks'][0]), 'check_id': '456', 'conclusion': 'success'}]}
        # Real subprocess, synthetic adapter. The adapter must receive only
        # immutable selectors, never model-provided proof or policy verdicts.
        self.reply = self.root / 'reply.json'
        self.adapter = self.root / 'adapter.py'
        self.adapter.write_text(
            'import json, sys\n'
            'request = json.load(sys.stdin)\n'
            'assert set(request) == {"op", "repository", "workflow", "sha", "run", "attempt"}\n'
            'assert request["op"] == "acceptance"\n'
            'print(open(sys.argv[1]).read())\n')
        self.project.config = dataclasses.replace(self.config, vcs={
            **self.config.vcs, 'command': [sys.executable, str(self.adapter), str(self.reply)]})
        self.selectors = {'snapshot_id': self.snapshot['id'],
                          'publication_id': self.publication['publication_id'], 'run': '123', 'attempt': 1}

    def retain(self):
        write_json(self.reply, self.response)
        return ci_acceptance.retain(self.project, **self.selectors)['receipt_id']

    def test_adapter_to_exact_delivery_and_immutable_done_record(self):
        manifest = self.project.root / 'snapshots' / (self.snapshot['id'] + '.json')
        before = manifest.read_bytes()
        item = self.project.work_items.create(actor='worker', idempotency_key='ci-proof',
            title='Complete delivered tree', scope='Exact CI acceptance')['record']['work_item']
        with self.assertRaises(Denied):
            self.project.work_items.transition(item['id'], expected_rev=1, status='done',
                actor='worker', snapshot_id=self.snapshot['id'])
        binding = bind_delivery(self.project, item['id'], self.snapshot['id'])
        timeout_path = self.project.root / 'runs' / ('d' * 32) / 'verification.json'
        timeout_record = {'code_digest': self.snapshot['code_digest'], 'passed': False,
                          'reason': 'local verification timed out'}
        write_json(timeout_path, timeout_record)
        timeout_bytes = timeout_path.read_bytes()
        failed_snapshot = {**self.snapshot, 'verification': {
            'passed': False, 'code_digest': self.snapshot['code_digest'], 'reason': 'timeout'}}
        receipt_id = self.retain()
        proof = acceptance.effective(self.project, failed_snapshot)
        self.assertEqual(proof['receipt_id'], receipt_id)
        with patch.object(self.project.snapshots, 'get', return_value=failed_snapshot):
            record = self.project.work_items.transition(item['id'], expected_rev=binding['rev'],
                status='done', actor='worker', snapshot_id=self.snapshot['id'])
        self.assertEqual(record['verification'], proof)
        self.assertEqual(timeout_path.read_bytes(), timeout_bytes)
        self.assertIs(failed_snapshot['verification']['passed'], False)
        self.assertEqual(self.project.work_items.get(item['id']), record)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertIsNone(self.snapshot['verification'])
        self.assertEqual(acceptance.audit(self.project.root), 1)
        # Policy removal stops new selection, not historical exact proof reads.
        self.project.settings.pop('ci_acceptance')
        self.assertIsNone(acceptance.effective(self.project, self.snapshot))
        self.assertEqual(self.project.work_items.get(item['id']), record)

    def test_idempotent_replay_does_not_requery_or_substitute_attempt(self):
        receipt_id = self.retain()
        with patch.object(vcs, 'invoke', side_effect=AssertionError('no repeat query')):
            self.assertEqual(ci_acceptance.retain(self.project, **self.selectors)['receipt_id'], receipt_id)
            with self.assertRaises(Denied):
                ci_acceptance.retain(self.project, **{**self.selectors, 'attempt': 2})
            # A later selector cannot supersede the immutable first-writer
            # proof; installation continues to select that exact receipt only.
            self.assertEqual(ci_acceptance.selected(self.project, self.snapshot['id'])[0], receipt_id)
        value = acceptance.get(self.project.root, receipt_id)
        self.assertEqual(value['publication_id'], self.selectors['publication_id'])
        self.assertEqual(value['scope']['ci']['snapshot_id'], self.snapshot['id'])

    def test_crash_window_recovery_keeps_exact_receipt(self):
        receipt_id = self.retain()
        for path in (self.project.root / 'evidence/acceptance/index').glob('*/*.json'):
            path.unlink()  # Synthetic crash after durable object, before index.
        self.assertIsNone(acceptance.effective(self.project, self.snapshot))
        acceptance.recover(self.project.root)
        self.assertEqual(acceptance.effective(self.project, self.snapshot)['receipt_id'], receipt_id)

    def test_policy_and_adapter_scope_changes_do_not_reuse_proof(self):
        self.retain()
        original = copy.deepcopy(self.project.settings)
        for key, value in [('version', 'reviewed-v2'), ('workflow_digest', 'e' * 64)]:
            with self.subTest(key=key):
                self.project.settings['ci_acceptance'][key] = value
                self.assertIsNone(acceptance.effective(self.project, self.snapshot))
                self.project.settings = copy.deepcopy(original)
        self.project.verify = ('another command',)
        self.assertIsNone(acceptance.effective(self.project, self.snapshot))
        self.project.verify = tuple(self.local['commands'])
        self.project.config = dataclasses.replace(self.project.config,
            vcs={**self.project.config.vcs, 'timeout_seconds': 19})
        self.assertIsNone(acceptance.effective(self.project, self.snapshot))

    def test_disabled_unconfigured_skipped_or_unpublished_refuses_without_query(self):
        original = copy.deepcopy(self.project.settings)
        with patch.object(vcs, 'invoke', side_effect=AssertionError('must not query')):
            self.project.settings.pop('ci_acceptance')
            with self.assertRaises(Denied):
                self.retain()
            self.project.settings = original
            config = self.project.config
            self.project.config = dataclasses.replace(config, vcs={**config.vcs, 'command': []})
            with self.assertRaises(Denied):
                self.retain()
            self.project.config = config
            failed_snapshot = {**self.snapshot, 'verification': {'passed': False}}
            self.assertIsNotNone(ci_acceptance.bound(self.project, failed_snapshot, self.local))
            self.assertIsNone(ci_acceptance.bound(
                self.project, {**self.snapshot, 'skipped': ['unrepresentable']}, self.local))
            with patch.object(self.project.snapshots, '_history_ids', return_value=[]):
                with self.assertRaises(Denied):
                    self.retain()

    def test_local_failures_remain_untouched_and_do_not_veto_ci_proof(self):
        records = []
        timeout_path = self.project.root / 'runs' / ('d' * 32) / 'verification.json'
        write_json(timeout_path, {'code_digest': self.snapshot['code_digest'],
                                  'passed': False, 'reason': 'timeout'})
        records.append((timeout_path, timeout_path.read_bytes()))
        # A real queued local verify plus its ordinary role wait sidecar must
        # not be mistaken for an invalid CI proof or an unreadable producer tree.
        context = self.context()
        job = check_jobs.submit(context)
        check_jobs.set_wait(self.project, 'worker', job['id'])
        job_path = self.project.root / 'check-jobs' / job['id'] / 'job.json'
        wait_path = self.project.root / 'check-jobs' / 'worker.wait.json'
        records.extend((path, path.read_bytes()) for path in (job_path, wait_path))
        receipt_id = self.retain()
        proof = acceptance.effective(self.project, self.snapshot)
        self.assertEqual(proof['receipt_id'], receipt_id)
        self.assertEqual(ci_acceptance.retain(self.project, **self.selectors)['receipt_id'], receipt_id)
        for path, before in records:
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(acceptance.get(self.project.root, receipt_id)['schema'], 2)

    def test_wrong_snapshot_receipt_cannot_be_used_for_done(self):
        receipt_id = self.retain()
        proof = acceptance.effective(self.project, self.snapshot)
        with self.assertRaises(Denied):
            self.project.work_items._retained_verification(proof, self.snapshot['code_digest'],
                digest(canonical(acceptance.get(self.project.root, receipt_id)['scope'])), 'f' * 64)
        self.assertIsNone(acceptance.effective(self.project, {**self.snapshot, 'id': 'f' * 64}))

    def test_provenance_and_partial_scope_fail_closed(self):
        mutations = [
            ('green only', lambda e: e.clear()),
            ('wrong sha', lambda e: e.update(sha='d' * 40)),
            ('wrong source', lambda e: e.update(code_digest='d' * 64)),
            ('wrong repo', lambda e: e.update(repository='other/project')),
            ('wrong workflow', lambda e: e.update(workflow='different.yml')),
            ('wrong workflow digest', lambda e: e.update(workflow_digest='d' * 64)),
            ('wrong run', lambda e: e.update(run='124')),
            ('wrong attempt', lambda e: e.update(attempt=2)),
            ('stale', lambda e: e.update(current_attempt=False)),
            ('partial', lambda e: e.update(complete=False)),
            ('missing', lambda e: e.update(checks=[])),
            ('duplicate', lambda e: e['checks'].append(copy.deepcopy(e['checks'][0]))),
            ('unknown check', lambda e: e['checks'][0].update(check='other')),
            ('unknown scope', lambda e: e['checks'][0].update(coverage=['partial'])),
            ('missing command', lambda e: e['checks'][0].update(commands=[])),
            ('wrong env', lambda e: e['checks'][0].update(environment={'os': 'unknown'})),
            ('unknown fields', lambda e: e.update(trust='trusted')),
        ]
        mutations += [(state, lambda e, state=state: e['checks'][0].update(conclusion=state))
                      for state in ('failure', 'cancelled', 'skipped', 'pending', 'neutral')]
        original = copy.deepcopy(self.response)
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.response = copy.deepcopy(original)
                mutate(self.response)
                with self.assertRaises(Denied):
                    self.retain()
                self.assertIsNone(acceptance.effective(self.project, self.snapshot))

    def test_policy_parser_requires_complete_explicit_bounded_mapping(self):
        check_project_settings(self.project.settings)
        for modify in (lambda p: p.pop('version'),
                       lambda p: p.update(schema=2),
                       lambda p: p.update(acceptance_scope='unknown'),
                       lambda p: p.update(checks=[]),
                       lambda p: p['checks'].append(copy.deepcopy(p['checks'][0])),
                       lambda p: p['checks'][0].update(commands=[]),
                       lambda p: p['checks'][0].update(commands=[True]),
                       lambda p: p['checks'][0].update(environment={}),
                       lambda p: p['checks'][0].update(coverage=[])):
            spec = copy.deepcopy(self.spec)
            modify(spec)
            with self.subTest(spec=spec), self.assertRaises(ConfigError):
                check_project_settings({**self.project.settings, 'ci_acceptance': spec})
        cli = parser().parse_args(['ci-acceptance', 'sample', '--snapshot', self.snapshot['id'],
            '--publication-id', self.selectors['publication_id'], '--run', '123', '--attempt', '1'])
        self.assertEqual(cli.attempt, 1)
        with self.assertRaises(Denied):
            vcs.read_via(self.project.config.vcs, 'acceptance', {})

    def test_multiple_checks_have_complete_unique_identity_and_stable_order(self):
        second = {**copy.deepcopy(self.spec['checks'][0]), 'check': 'core-macos',
                  'environment': {'os': 'macos', 'python': '3.14'}}
        self.project.settings['ci_acceptance']['checks'].append(second)
        self.response['checks'].append({**copy.deepcopy(second), 'check_id': '789', 'conclusion': 'success'})
        # Both required checks cover the command, with distinct environments.
        for mutation in ('duplicate-name', 'duplicate-id', 'bool-index', 'cross-attempt'):
            bad = copy.deepcopy(self.response)
            if mutation == 'duplicate-name':
                bad['checks'][1] = copy.deepcopy(bad['checks'][0])
            elif mutation == 'duplicate-id':
                bad['checks'][1]['check_id'] = bad['checks'][0]['check_id']
            elif mutation == 'bool-index':
                bad['checks'][1]['commands'] = [False]
            else:
                bad['checks'][1]['attempt'] = 2
            with self.subTest(mutation=mutation), patch.object(vcs, 'invoke', return_value=bad):
                with self.assertRaises(Denied):
                    self.retain()
        self.response['checks'].reverse()
        receipt_id = self.retain()
        receipt = acceptance.get(self.project.root, receipt_id)
        self.assertEqual([r['check'] for r in receipt['evidence']['checks']], ['core-linux', 'core-macos'])
        # Repeat after an object-before-index crash with a different provider order.
        for path in (self.project.root / 'evidence/acceptance/index').glob('*/*.json'):
            path.unlink()
        self.response['checks'].reverse()
        self.assertEqual(self.retain(), receipt_id)

    def test_publication_source_or_retained_publication_corruption_refuses(self):
        other = Publication.record_external(self.project.root, branch='other', published_sha='e' * 40,
            code_digest='d' * 64, approval='operator-review', projection=public_source_identity({}))
        with patch.object(vcs, 'invoke', side_effect=AssertionError('must not query')):
            with self.assertRaises(Denied):
                ci_acceptance.retain(self.project, **{**self.selectors,
                    'publication_id': other['publication_id']})
        receipt_id = self.retain()
        # Publication authority is revalidated, not copied into an independent
        # unverifiable pass claim inside the CI object.
        for path in (self.project.root / 'evidence/publications').glob('*'):
            if self.selectors['publication_id'] in path.name and path.name.endswith('.external.json'):
                path.write_bytes(b'{}')
        with self.assertRaises(Denied):
            acceptance.get(self.project.root, receipt_id)

    def test_transport_failure_and_oversized_response_never_mint_proof(self):
        self.adapter.write_text('raise SystemExit(1)\n')
        with self.assertRaises(Denied):
            self.retain()
        self.adapter.write_text('print("x" * 10000)\n')
        self.project.config = dataclasses.replace(self.project.config,
            vcs={**self.project.config.vcs, 'max_bytes': 4096})
        with self.assertRaises(Denied):
            self.retain()
        self.assertFalse((self.project.root / 'evidence/acceptance/objects').exists())

    def test_private_project_toml_and_cli_execute_use_the_same_authority(self):
        spec = copy.deepcopy(self.spec)
        env = spec['checks'][0]['environment']
        toml = '\n[ci_acceptance]\n' + '\n'.join(
            f'{key} = {json.dumps(value)}' for key, value in spec.items() if key != 'checks')
        toml += ('\n[[ci_acceptance.checks]]\ncheck = "core-linux"\ncommands = [0]\n'
                 'coverage = ["complete-offline-scope"]\n[ci_acceptance.checks.environment]\n')
        toml += '\n'.join(f'{key} = {json.dumps(value)}' for key, value in env.items()) + '\n'
        settings = self.project.root / 'project.toml'
        settings.write_text(settings.read_text() + toml)
        reopened = Project(self.project.config, self.project.name, read_only=True)
        self.assertEqual(reopened.settings['ci_acceptance'], spec)
        args = parser().parse_args(['ci-acceptance', 'sample', '--snapshot', self.snapshot['id'],
            '--publication-id', self.selectors['publication_id'], '--run', '123', '--attempt', '1'])
        write_json(self.reply, self.response)
        with patch('mizu.cli.load', return_value=self.project.config):
            result = execute(args)
        self.assertEqual(acceptance.effective(reopened, self.snapshot)['receipt_id'], result['receipt_id'])

    def test_rehashed_invalid_receipt_is_not_authority(self):
        receipt_id = self.retain()
        original = acceptance.get(self.project.root, receipt_id)
        for alter in (lambda r: r['evidence'].update(complete=False),
                      lambda r: r['scope']['ci']['policy'].update(acceptance_scope='e' * 64),
                      lambda r: r['evidence']['checks'][0].update(coverage=['unknown'])):
            value = copy.deepcopy(original)
            alter(value)
            changed_id = digest(canonical(value))
            path = self.project.root / f'evidence/acceptance/objects/{changed_id}.json'
            write_json(path, value)
            with self.assertRaises(Denied):
                acceptance.get(self.project.root, changed_id)
            path.unlink()

    def test_retention_corruption_never_repairs_or_accepts(self):
        receipt_id = self.retain()
        path = self.project.root / f'evidence/acceptance/objects/{receipt_id}.json'
        value = json.loads(path.read_bytes())
        value['evidence']['checks'][0]['conclusion'] = 'failure'
        write_json(path, value)
        for read in (lambda: acceptance.get(self.project.root, receipt_id),
                     lambda: acceptance.effective(self.project, self.snapshot),
                     lambda: acceptance.audit(self.project.root), self.retain):
            with self.assertRaises(Denied):
                read()
        self.assertEqual(json.loads(path.read_bytes()), value)
