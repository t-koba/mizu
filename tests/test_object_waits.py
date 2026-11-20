"""Object wait authority: current observation, immutable replay, holder-only reopen."""
import copy
from unittest.mock import patch

from support import Fixture, bind_delivery, history_write_json as write_json
from mizu.errors import Denied
from mizu.fs import canonical, digest
from mizu import object_waits, work_item_resolution as resolution
from mizu.work_item_waits import liveness


class ObjectWaitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items

    def create(self, key):
        return self.store.create(actor='worker', idempotency_key=key, title=key)['record']['work_item']

    def block(self, item, subject):
        return self.store.transition(item['id'], expected_rev=item['rev'], status='blocked',
            actor='worker', wait={'kind': 'object_resolved', 'subject': subject})

    def state(self, item):
        return liveness(self.store, item['wait'], None, item=item)['state']

    def reconcile(self, role='worker'):
        resolution.reconcile(self.project, role, limit=20)

    def subject(self, item, kind='work_item'):
        return {'type': kind, 'id': item['id']}

    def test_missing_and_corrupt_objects_never_authorize_reopen(self):
        with self.assertRaises(Denied):
            self.block(self.create('missing'), {'type': 'insight', 'id': 'missing'})
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

    def test_object_proof_fits_existing_resolution_allowance(self):
        # Authenticated answer IDs are operator/<project identifier>/<event identifier>.
        maximum = {'wait_rev': 255, 'wait_digest': 'f' * 64, 'kind': 'due',
                   'resolved_at': '9999-12-31T23:59:59+00:00',
                   'evidence': {'event': 'operator/' + 'p' * 63 + '/' + 'e' * 63,
                                'digest': 'f' * 64, 'rev': 'f' * 64, 'state': 'answered'}}
        growth = len(canonical({'a': 1, 'wait_resolution': maximum})) - len(canonical({'a': 1}))
        self.assertLessEqual(growth, resolution.MAX_RESOLUTION_BYTES)

