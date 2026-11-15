"""Object wait authority: current observation, immutable replay, holder-only reopen."""
import copy
from unittest.mock import patch

from support import Fixture, bind_delivery, history_write_json as write_json
from mizu.errors import Denied
from mizu.fs import canonical, digest
from mizu import object_waits, work_item_resolution as resolution
from mizu.work_item_waits import liveness, stored_wait
from mizu.protocol import WORK_ITEM_WAIT_SCHEMA, validate


class ObjectWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.insights = self.project.insights

    def create(self, key):
        return self.store.create(actor='worker', idempotency_key=key, title=key)['record']['work_item']

    def block(self, item, subject):
        return self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
            actor='worker', wait={'kind': 'object_resolved', 'subject': subject})

    def state(self, item):
        return liveness(self.store, item['wait'], None, item=item)['state']

    def reconcile(self, role='worker'):
        resolution.reconcile(self.project, role, limit=20)

    def insight(self):
        return self.insights.submit(source='worker', title='Question', body='Choose a route', base_snapshot=None)

    def decide(self, item, action):
        return self.insights.decide(item['id'], action, 'Reviewed',
            'Later' if action == 'defer' else '', 'run', expected_rev=item.get('rev', 1))

    def subject(self, item, kind='work_item'):
        return {'type': kind, 'id': item['id']}

    def test_insight_outcomes_and_immutable_replay_after_revision(self):
        for action in ('accept', 'modify', 'reject', 'defer', 'withdraw'):
            with self.subTest(action=action):
                insight = self.insight()
                blocked = self.block(self.create(action), self.subject(insight, 'insight'))
                self.assertEqual(self.state(blocked), 'pending')
                if action == 'withdraw':
                    self.insights.withdraw(insight['id'], source='worker', reason='No longer needed')
                else:
                    self.decide(insight, action)
                self.assertEqual(self.state(blocked), 'impossible' if action == 'withdraw' else
                                 'pending' if action == 'defer' else 'due')
                before = canonical(self.store._optional_record(f"decisions/{insight['id']}.json"))
                self.reconcile('reviewer')
                self.assertEqual(self.store.get(blocked['id']), blocked)
                self.reconcile()
                opened = self.store.get(blocked['id'])
                if action in ('defer', 'withdraw'):
                    self.assertEqual(opened, blocked)
                    continue
                self.assertEqual((opened['status'], opened['holder']), ('open', 'worker'))
                self.assertEqual(opened['wait_resolution']['evidence']['state'], action)
                self.assertEqual(before, canonical(self.store._optional_record(f"decisions/{insight['id']}.json")))
                self.insights.revise(insight['id'], source='worker', title='Revised', body='New question', base_snapshot=None, expected_rev=1)
                self.assertEqual(self.state(blocked), 'pending')
                resolution.validate(self.store, blocked, opened)
                self.assertEqual(list(self.store.history(blocked['id']))[-1], opened)

    def test_missing_and_corrupt_objects_never_authorize_reopen(self):
        missing = self.block(self.create('missing'), {'type': 'insight', 'id': 'missing'})
        self.assertEqual(self.state(missing), 'impossible')
        write_json(self.store.root / 'insight-ids' / 'missing.json', {'id': 'missing'})
        self.assertEqual(self.state(missing), 'unknown')
        item = self.create('target')
        blocked = self.block(self.create('waiter'), self.subject(item))
        fake = {**blocked, 'wait': {'kind': 'object_resolved', 'subject': {'type': 'work_item', 'id': 'wi_' + 'f' * 32}}}
        self.assertEqual(self.state(fake), 'impossible')
        path = self.store.root / 'work-items' / item['id'] / 'create.json'
        path.write_text('{')
        self.assertEqual(self.state(blocked), 'unknown')
        self.reconcile()
        self.assertEqual(self.store.get(blocked['id']), blocked)

    def test_work_item_terminal_proofs_and_unchanged_target(self):
        for state in ('done', 'dropped'):
            with self.subTest(state=state):
                target = self.create(state)
                if state == 'done':
                    snapshots = self.project.snapshots
                    files = snapshots.capture_files(self.project.workspace)
                    snapshot = snapshots.create(files, goal='Goal', state='State', run='run', outcome='done',
                        summary='Verified', verification={'passed': True, 'code_digest': files['code_digest'], 'run': 'run'})
                    snapshots.publish(snapshot)
                    target = bind_delivery(self.project, target['id'], snapshot['id'])
                waiter = self.block(self.create('wait-' + state), self.subject(target))
                self.assertEqual(self.state(waiter), 'pending')
                terminal = self.store.transition(target['id'], expected_rev=target['rev'], status=state,
                    actor='worker', reason='Finished', **({'snapshot_id': snapshot['id']} if state == 'done' else {}))
                self.assertEqual(self.state(waiter), 'due')
                proof = resolution.observe(self.store, waiter)
                self.reconcile()
                opened = self.store.get(waiter['id'])
                self.assertEqual(opened['wait_resolution']['evidence'], proof['evidence'])
                self.assertEqual(self.store.get(target['id']), terminal)
                self.assertEqual(opened['wait_resolution']['wait_digest'], digest(canonical(waiter['wait'])))
                resolution.validate(self.store, waiter, opened)
                if state == 'done':
                    from mizu.storage import audit
                    self.assertGreaterEqual(audit(self.project)['references']['work_item_snapshots'], 1)
                for field, bad in (('rev', terminal['rev'] + 1), ('state', 'doing'),
                                   ('event', waiter['id']), ('digest', '0' * 64)):
                    forged = copy.deepcopy(opened)
                    forged['wait_resolution']['evidence'][field] = bad
                    with self.subTest(field=field), self.assertRaises(Denied):
                        resolution.validate(self.store, waiter, forged)
                with self.assertRaises(Denied):
                    self.store.transition(waiter['id'], expected_rev=opened['rev'], status='doing', actor='reviewer')

    def test_dropped_to_done_correction_preserves_old_proof_and_retirement_resolves(self):
        target = self.create('corrected')
        snapshots = self.project.snapshots
        files = snapshots.capture_files(self.project.workspace)
        snapshot = snapshots.create(files, goal='Goal', state='State', run='run', outcome='done',
            summary='Verified', verification={'passed': True, 'code_digest': files['code_digest'], 'run': 'run'})
        snapshots.publish(snapshot)
        target = bind_delivery(self.project, target['id'], snapshot['id'])
        waiter = self.block(self.create('correction-wait'), self.subject(target))
        dropped = self.store.transition(target['id'], expected_rev=target['rev'], actor='worker',
                                        status='dropped', reason='Dropped before acceptance')
        self.reconcile()
        opened = self.store.get(waiter['id'])
        from mizu import acceptance
        bound = {'commands': ['check'], 'execution': '1' * 64, 'environment': '2' * 64,
                 'image': 'sha256:' + '3' * 64}
        acceptance._retain(self.project.root, {
            'schema': 1, 'code_digest': snapshot['code_digest'], 'scope': bound,
            'producer': {'kind': 'verify', 'id': '4' * 32, 'run': '4' * 32}, 'passed': True,
            'commands': [{'id': '5' * 32, 'exit_code': 0, 'reason': 'exited'}]})
        with patch('mizu.acceptance._current_scope', return_value=bound):
            corrected = self.store.transition(target['id'], expected_rev=dropped['rev'], actor='worker',
                status='done', snapshot_id=snapshot['id'])
        self.assertEqual(corrected['status'], 'done')
        resolution.validate(self.store, waiter, opened)
        self.assertEqual(opened['wait_resolution']['evidence']['state'], 'dropped')

        exhausted = self.create('exhausted')
        waiter = self.block(self.create('retirement-wait'), self.subject(exhausted))
        exhausted = self.store.transition(exhausted['id'], expected_rev=1, actor='worker', status='doing')
        for rev in range(3, 257):
            exhausted = {**exhausted, 'rev': rev, 'scope': f'Scope {rev}', 'reason': 'Refine scope',
                         'transition': {'from': 'doing', 'to': 'doing'}}
            write_json(self.store.root / 'work-items' / exhausted['id'] / f'{rev}.json', exhausted)
        self.store.transition(exhausted['id'], expected_rev=256, actor='worker', status='dropped', reason='Retired')
        self.reconcile()
        opened = self.store.get(waiter['id'])
        self.assertEqual(opened['wait_resolution']['evidence']['rev'], 256)
        resolution.validate(self.store, waiter, opened)

    def test_self_cycles_and_uncertain_traversal_write_nothing(self):
        a, b, c = [self.create(key) for key in ('a', 'b', 'c')]
        with self.assertRaises(Denied):
            self.block(a, self.subject(a))
        a = self.block(a, self.subject(b))
        b = self.block(b, self.subject(c))
        with self.assertRaises(Denied):
            self.block(c, self.subject(a))
        with patch.object(object_waits, 'target', side_effect=Denied('Unavailable')):
            with self.assertRaises(Denied):
                self.block(c, self.subject(a))
        self.assertEqual(list(self.store.history(c['id'])), [c])
        with self.assertRaises(Denied):
            self.block(c, {'type': 'work_item', 'id': 'wi_' + 'f' * 32})

    def test_resolution_rechecks_cas_and_current_insight_before_append(self):
        insight = self.insight()
        waiter = self.block(self.create('race'), self.subject(insight, 'insight'))
        self.decide(insight, 'reject')
        self.assertIsNotNone(resolution.observe(self.store, waiter))
        with patch('mizu.work_item_waits.liveness', return_value={'state': 'due'}), \
                patch.object(self.store, '_optional_record', return_value=None):
            self.assertIsNone(resolution.observe(self.store, waiter))
        self.insights.revise(insight['id'], source='worker', title='Revision', body='Again', base_snapshot=None, expected_rev=1)
        with self.assertRaises(Denied):
            self.store.transition(waiter['id'], expected_rev=waiter['rev'], status='open',
                                  actor='worker', _resolve_wait=True)
        self.assertEqual(self.store.get(waiter['id']), waiter)
        self.decide({**insight, 'rev': 2}, 'reject')
        self.reconcile()
        with self.assertRaises(Denied):
            self.store.transition(waiter['id'], expected_rev=waiter['rev'], status='open',
                                  actor='worker', _resolve_wait=True)
        self.assertEqual(len(list(self.store.history(waiter['id']))), 3)

    def test_object_proof_fits_existing_resolution_allowance(self):
        # Authenticated answer IDs are operator/<project identifier>/<event identifier>.
        maximum = {'wait_rev': 255, 'wait_digest': 'f' * 64, 'kind': 'due',
                   'resolved_at': '9999-12-31T23:59:59+00:00',
                   'evidence': {'event': 'operator/' + 'p' * 63 + '/' + 'e' * 63,
                                'digest': 'f' * 64, 'rev': 'f' * 64, 'state': 'answered'}}
        growth = len(canonical({'a': 1, 'wait_resolution': maximum})) - len(canonical({'a': 1}))
        self.assertLessEqual(growth, resolution.MAX_RESOLUTION_BYTES)

    def test_manual_policy_historical_alias_and_public_subjects(self):
        insight = self.insight()
        waiter = self.create('alias')
        waiter = self.store.transition(waiter['id'], expected_rev=1, actor='worker', status='blocked',
            wait={'kind': 'insight_decided', 'insight': insight['id']})
        self.assertEqual(waiter['wait']['kind'], 'object_resolved')
        # Immutable legacy event fixture: replay must not rewrite it.
        waiter['wait'] = {'kind': 'insight_decided', 'target': insight['id']}
        write_json(self.store.root / 'work-items' / waiter['id'] / '2.json', waiter)
        self.decide(insight, 'reject')
        with patch.dict(self.project.settings, work_item_wait_resolution='manual'):
            self.reconcile()
        self.assertEqual(self.store.get(waiter['id']), waiter)
        self.reconcile()
        opened = self.store.get(waiter['id'])
        resolution.validate(self.store, waiter, opened)
        self.assertEqual(list(self.store.history(waiter['id']))[1], waiter)
        for kind, target in (('insight', insight), ('work_item', waiter)):
            wait = {'kind': 'object_resolved', 'subject': self.subject(target, kind)}
            validate(wait, WORK_ITEM_WAIT_SCHEMA)
            self.assertEqual(stored_wait(wait), wait)
        for bad in ({'type': 'snapshot', 'id': 'x'}, {'type': 'insight', 'id': 'x', 'rev': 1},
                    {'type': 'work_item', 'id': 'prefix'}, {'type': 'insight'}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                stored_wait({'kind': 'object_resolved', 'subject': bad})
