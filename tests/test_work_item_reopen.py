"""Audited completion challenges: opt-in authority, immutable replay and projections."""
import dataclasses

from support import Fixture, forget_ledger, history_write_json
from mizu import object_waits, storage, work_item_events
from mizu.completion_evidence import HOLDER_ATTESTATION, VERIFIED_SNAPSHOT
from mizu.config import CompletionEvidence, completion_evidence_policy, CAPABILITIES
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest
from mizu.project import Project
from mizu.protocol import DEFINITIONS, validate
from mizu.work_item_waits import liveness


class CompletionReopenTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.serial = 0

    def configure(self, policy=VERIFIED_SNAPSHOT, *, allowed=(), capability=False):
        roles = dict(self.config.roles)
        caps = tuple(c for c in roles['worker'].capabilities if c != 'work_items_reopen')
        roles['worker'] = dataclasses.replace(roles['worker'],
            capabilities=caps + (('work_items_reopen',) if capability else ()))
        self.config = dataclasses.replace(self.config, roles=roles,
            completion_evidence=CompletionEvidence(default=policy, reopen_roles=allowed))
        self.project = Project(self.config, self.project.name)
        self.store = self.project.work_items

    def seed(self):
        self.serial += 1
        return self.store.create(actor='worker', idempotency_key=f'reopen-{self.serial}',
            title='Audited completion', description='Retained description', scope='Accepted scope')['record']['work_item']

    def change(self, item, status, **kwargs):
        return self.store.transition(item['id'], expected_rev=item['rev'], status=status,
                                     actor='worker', **kwargs)

    def snapshot(self, label):
        snapshots = self.project.snapshots
        (self.project.workspace / 'result.txt').write_text(label)
        files = snapshots.capture_files(self.project.workspace)
        snapshot = snapshots.create(files, goal='Goal', state='State', run='verified', outcome='done',
            summary=label, verification={'passed': True, 'code_digest': files['code_digest'], 'run': 'verified'})
        snapshots.publish(snapshot)
        return snapshot

    def close(self, item, policy, label='verified'):
        doing = self.change(item, 'doing')
        if policy == HOLDER_ATTESTATION:
            return self.change(doing, 'done', evidence={'statement': label})
        snapshot = self.snapshot(label)
        bound = self.change(doing, 'doing', snapshot_id=snapshot['id'])
        return self.change(bound, 'done', snapshot_id=snapshot['id'])

    def reopen(self, item, **kwargs):
        return self.store.reopen(item['id'], expected_rev=kwargs.pop('expected_rev', item['rev']),
                                 reason=kwargs.pop('reason', 'Challenge the result'), actor='worker', **kwargs)

    def test_default_disabled_and_both_authority_gates(self):
        from mizu.prompt_sections import WORK_CAPABILITIES
        from mizu.runtime import CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN
        for grants in (CAPABILITIES, WORK_CAPABILITIES, CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN):
            self.assertIn('work_items_reopen', grants)
        self.assertEqual(self.config.completion_evidence.reopen_roles, ())
        self.assertTrue(all('work_items_reopen' not in role.capabilities for role in self.config.roles.values()))
        self.configure(HOLDER_ATTESTATION)
        done = self.close(self.seed(), HOLDER_ATTESTATION)
        args = {'id': done['id'], 'expected_rev': done['rev'], 'reason': 'Incorrect result'}
        for allowed, cap, error in (((), False, 'capability'), (('worker',), False, 'capability'),
                                    ((), True, 'reopen_roles')):
            self.configure(HOLDER_ATTESTATION, allowed=allowed, capability=cap)
            with self.subTest(allowed=allowed, cap=cap), self.assertRaisesRegex(Denied, error):
                self.context().handle('work_items_reopen', args)
            self.assertEqual(self.store.current(done['id']), done)
        self.configure(HOLDER_ATTESTATION, allowed=('worker',), capability=True)
        opened = self.context().handle('work_items_reopen', args)
        self.assertEqual(opened['actor'], 'worker')
        self.assertEqual(opened['status'], 'open')

    def test_config_and_schema_fail_closed(self):
        roles = self.config.roles
        self.assertEqual(completion_evidence_policy({'reopen_roles': ['worker']}, roles).reopen_roles,
                         ('worker',))
        for raw in ({'unexpected': []}, {'reopen_roles': 'worker'}, {'reopen_roles': {}},
                    {'reopen_roles': ['missing']}, {'reopen_roles': [False]},
                    {'reopen_roles': ['worker', 'worker']}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                completion_evidence_policy(raw, roles)
        schema = DEFINITIONS['work_items_reopen'][1]
        args = {'id': 'wi_' + 'a' * 32, 'expected_rev': 1, 'reason': 'Challenge'}
        validate(args, schema)
        for fields in ({'actor': 'reviewer'}, {'policy': HOLDER_ATTESTATION}, {'expected_rev': 0},
                       {'expected_rev': True}, {'reason': ''}, {'reason': 'x' * 4001}, {'id': 'short'}):
            with self.subTest(fields=fields), self.assertRaises(Denied):
                validate({**args, **fields}, schema)

    def test_full_cycles_preserve_history_but_inherit_no_proof(self):
        for policy in (VERIFIED_SNAPSHOT, HOLDER_ATTESTATION):
            self.configure(policy)
            done = self.close(self.seed(), policy)
            path = self.store.root / 'work-items' / done['id'] / f"{done['rev']}.json"
            if policy == VERIFIED_SNAPSHOT:
                # Earlier strict done records had no versioned policy fields.
                done = {key: value for key, value in done.items()
                        if key not in ('completion_policy', 'completion_evidence')}
                history_write_json(path, done)
            original = path.read_bytes()
            opened = self.reopen(done, reason='  Challenge 日本語  ')
            for field in ('id', 'title', 'origin', 'description', 'created_at', 'scope', 'delivery_schema'):
                self.assertEqual(opened[field], done[field])
            for field in ('holder', 'wait', 'blocked_since', 'snapshot_id', 'verification',
                          'delivered_tree', 'verification_scope', 'delivery'):
                self.assertIsNone(opened[field])
            self.assertNotIn('completion_policy', opened)
            self.assertNotIn('completion_evidence', opened)
            self.assertEqual(opened['reopen'], {'challenged_rev': done['rev'], 'challenger': 'worker',
                'reason_digest': digest(canonical(opened['reason']))})
            doing = self.change(opened, 'doing')
            self.assertNotIn('reopen', doing)
            if policy == VERIFIED_SNAPSHOT:
                with self.assertRaisesRegex(Denied, 'prior exact snapshot'):
                    self.change(doing, 'done', snapshot_id=done['snapshot_id'])
                snapshot = self.snapshot('fresh proof')
                doing = self.change(doing, 'doing', snapshot_id=snapshot['id'])
                fresh = self.change(doing, 'done', snapshot_id=snapshot['id'])
            else:
                with self.assertRaisesRegex(Denied, 'evidence object is required'):
                    self.change(doing, 'done')
                fresh = self.change(doing, 'done', evidence={'statement': 'New checks'})
                self.assertIsNone(fresh['verification'])
            self.assertEqual(path.read_bytes(), original)
            # Reload under a different policy and without reopening authority.
            self.configure(HOLDER_ATTESTATION if policy == VERIFIED_SNAPSHOT else VERIFIED_SNAPSHOT)
            forget_ledger(self.store.root)
            self.assertEqual(self.store.get(done['id']), fresh)
            self.assertIn(done, list(self.store.history(done['id'])))

    def test_invalid_requests_do_not_append_or_change_index(self):
        self.configure(HOLDER_ATTESTATION)
        done = self.close(self.seed(), HOLDER_ATTESTATION)
        directory = self.store.root / 'work-items' / done['id']
        files = {p.name: p.read_bytes() for p in directory.iterdir()}
        for kwargs in ({'expected_rev': 1}, {'expected_rev': True}, {'reason': ''}, {'reason': '  '},
                       {'reason': None}, {'reason': 'x' * 4001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Denied):
                self.reopen(done, **kwargs)
            self.assertEqual(self.store.current(done['id']), done)
            self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, files)
        for status in ('open', 'doing', 'blocked', 'dropped'):
            item = self.seed()
            if status != 'open':
                item = self.change(item, status, **({'wait': {'kind': 'code_change'}} if status == 'blocked'
                                                   else {'reason': 'Retire'} if status == 'dropped' else {}))
            with self.subTest(status=status), self.assertRaisesRegex(Denied, 'requires a done'):
                self.reopen(item)
            self.assertEqual(self.store.current(item['id']), item)

    def test_real_revision_boundaries_preserve_existing_ceiling(self):
        for source_rev in (252, 253, 254, 255, 256):
            seed = self.seed()
            snapshot = self.snapshot(f'Boundary {source_rev}')
            template = self.change(self.change(seed, 'doing'), 'doing', snapshot_id=snapshot['id'])
            previous = template
            directory = self.store.root / 'work-items' / seed['id']
            # Bulk-write valid ordinary fixture history; the boundary closures,
            # reopening, binding and refusal still exercise real CAS/index writes.
            for rev in range(4, source_rev):
                status = 'open' if rev % 2 == 0 else 'doing'
                previous = {**template, 'rev': rev, 'status': status,
                            'holder': 'worker' if status == 'doing' else None,
                            'transition': {'from': previous['status'], 'to': status}}
                with (directory / f'{rev}.json').open('xb') as stream:
                    stream.write(canonical(previous))
            forget_ledger(self.store.root)
            done = self.change(previous, 'done', snapshot_id=snapshot['id'])
            if source_rev > 252:
                files = {p.name: p.read_bytes() for p in directory.iterdir()}
                with self.assertRaisesRegex(Denied, 'headroom'):
                    self.reopen(done)
                self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, files)
                self.assertEqual(self.store.current(seed['id']), done)
                continue
            opened = self.reopen(done)
            bound = self.change(self.change(opened, 'doing'), 'doing', snapshot_id=snapshot['id'])
            self.assertEqual(self.change(bound, 'done', snapshot_id=snapshot['id'])['rev'], 256)

    def test_replay_rejects_tampered_marker_and_uncleared_state(self):
        self.configure(HOLDER_ATTESTATION)
        done = self.close(self.seed(), HOLDER_ATTESTATION)
        for status in ('open', 'doing', 'blocked', 'done', 'dropped'):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.change(done, status, reason='Cannot bypass reopening')
        opened = self.reopen(done)
        path = self.store.root / 'work-items' / done['id'] / f"{opened['rev']}.json"
        variants = []
        for key, value in (('challenged_rev', done['rev'] - 1), ('challenged_rev', True),
                           ('challenger', 'reviewer'), ('reason_digest', '0' * 64), ('extra', 'x')):
            variants.append({**opened, 'reopen': {**opened['reopen'], key: value}})
        variants.extend(({**opened, 'reopen': None}, {k: v for k, v in opened.items() if k != 'reopen'},
                         {**opened, 'scope': 'Changed scope'}, {**opened, 'reason': 'Tampered'},
                         {**opened, 'holder': 'worker'}, {**opened, 'snapshot_id': '0' * 64},
                         {**opened, 'completion_policy': HOLDER_ATTESTATION, 'completion_evidence': {'statement': 'old'}}))
        for bad in variants:
            with self.subTest(bad=bad):
                history_write_json(path, bad)
                with self.assertRaises(Denied):
                    list(self.store.history(done['id']))
        history_write_json(path, opened)
        doing = self.change(opened, 'doing')
        with self.assertRaisesRegex(Denied, 'reopen marker'):
            self.store._validate_transition(opened, {**doing, 'reopen': opened['reopen']})

    def test_pages_wait_proofs_and_acknowledgements_follow_new_head(self):
        self.configure(HOLDER_ATTESTATION)
        target = self.seed()
        wait = {'kind': 'object_resolved', 'subject': {'type': 'work_item', 'id': target['id']}}
        waiter = self.change(self.seed(), 'blocked', wait=wait)
        done = self.close(target, HOLDER_ATTESTATION)
        self.assertEqual(liveness(self.store, waiter['wait'], None, item=waiter)['state'], 'due')
        proof = object_waits.observe(self.store, waiter['wait'], waiter)['evidence']
        resolved_waiter = self.change(self.seed(), 'blocked', wait=wait)
        resolved = self.store.transition(resolved_waiter['id'], expected_rev=resolved_waiter['rev'],
            status='open', actor='worker', reason='Exact historical resolution', _resolve_wait=True)
        page = work_item_events.pending(self.store, 'reviewer', ('done',), sources='any')
        old_event = work_item_events.delivery('reviewer', page)['events'][0]
        raw = work_item_events.acknowledgement_payload(self.store, 'reviewer', old_event)
        work_item_events.acknowledge(self.store, 'reviewer', [raw])
        self.assertEqual(work_item_events.pending(self.store, 'reviewer', ('done',), sources='any')['pending'], 0)
        opened = self.reopen(done)
        self.assertNotIn(target['id'], [i['id'] for i in self.store.page_closed_readonly()['items']])
        for page in (self.store.page_open(), self.store.page_ready(role_name='worker', writer_roles=('worker',))):
            row = next(i for i in page['items'] if i['id'] == target['id'])
            self.assertEqual(row['reason_preview'], opened['reason'])
        self.assertEqual(liveness(self.store, waiter['wait'], None, item=waiter)['state'], 'pending')
        object_waits.validate(self.store, waiter['wait'], waiter['holder'], proof)
        self.assertEqual(list(self.store.history(resolved['id']))[-1], resolved)
        with self.assertRaisesRegex(Denied, 'differs from terminal'):
            work_item_events.acknowledgement_payload(self.store, 'reviewer', old_event)
        fresh = self.close(opened, HOLDER_ATTESTATION, 'New checks')
        page = work_item_events.pending(self.store, 'reviewer', ('done',), sources='any')
        self.assertEqual(page['pending'], 1)
        self.assertEqual(page['events'][0]['rev'], fresh['rev'])
        self.assertIsNone(page['events'][0]['verification'])
        with self.assertRaises(Denied):
            work_item_events.acknowledgement_payload(self.store, 'reviewer', old_event)

    def test_storage_audit_retains_historical_binding_after_reopen(self):
        done = self.close(self.seed(), VERIFIED_SNAPSHOT)
        self.reopen(done)
        # Move publication history away from the bound manifest.
        self.snapshot('new publication')
        inventory = storage.audit(self.project)
        self.assertEqual(inventory['work_item_events_skipped'], 0)
        self.assertEqual(inventory['references']['work_item_snapshots'], 1)
        self.assertEqual(list(self.store.history(done['id']))[-2]['delivery'], done['delivery'])
