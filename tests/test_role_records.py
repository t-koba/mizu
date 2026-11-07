"""Runtime grant/identity and declared data records; no source migration."""
import copy
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu import host_records
from mizu.config import load
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, digest
from mizu.project import Project
from mizu.runtime import check_consult_role, check_delegate_role

DECLARATION = '''
[record_kinds."role.note"]
writer = "role.worker"
route = "none"
wake = false
retention = "permanent"
acknowledgement = "explicit"
subject = "object"
maximum = 8192
'''
ARGS = {'kind': 'role.note', 'id': 'note1',
        'subject': '{"topic":"fixture"}', 'payload': '{"body":"A note","n":1}'}


class RoleRecordTests(Fixture):
    def setUp(self):
        super().setUp()
        text = self.file.read_text()
        start = text.index('[roles.worker]')
        index = text.index('capabilities = [', start) + len('capabilities = [')
        self.file.write_text(text[:index] + '"record_write", "record_read", ' + text[index:] + DECLARATION)
        self.config = load(self.file)
        self.project = Project(self.config, 'sample')

    def shared(self):
        return host_records.store(self.project.root, self.project.snapshots, self.config.record_kinds)

    def test_authenticated_create_canonical_replay_conflict_and_explicit_ack(self):
        context = self.context()
        result = context.handle('record_write', ARGS)
        record, computed = result['record'], result['record_digest']
        self.assertEqual(record['writer'], 'role.worker')
        self.assertEqual(record['id'], 'role/worker/role.note/note1')
        self.assertEqual(computed, digest(canonical(record)))
        shared = self.shared()
        path = self.project.root / shared._record_name(record['id'])
        self.assertEqual(path.read_bytes(), canonical(record))
        replay = {**ARGS, 'subject': '{ "topic": "fixture" }',
                  'payload': '{"n":1,"body":"A note"}'}
        with patch('mizu.shared_records.atomic_write') as write:
            self.assertEqual(context.handle('record_write', replay), result)
            write.assert_not_called()
        for changed in ({'subject': '{}'}, {'payload': '{"body":"different"}'}):
            with self.subTest(changed=changed), self.assertRaises(Denied):
                context.handle('record_write', {**ARGS, **changed})
        self.assertEqual(path.read_bytes(), canonical(record))
        self.assertFalse(shared.received('worker', record['id'], computed))
        # Host-only manifests still require exact, explicit identity-bound selection.
        manifest = shared.freeze('worker', [record['id']])
        selected = dataclasses.asdict(manifest[0])
        shared.acknowledge('worker', manifest=manifest)
        self.assertFalse(shared.received('worker', record['id'], computed))
        with self.assertRaises(Denied):
            shared.acknowledge('reviewer', [selected], manifest=manifest)
        shared.acknowledge('worker', [selected], manifest=manifest)
        self.assertTrue(shared.received('worker', record['id'], computed))
        self.assertEqual(context.handle('record_write', ARGS), result)

    def test_role_kind_id_boundary_cannot_alias(self):
        context = self.context()
        args = {**ARGS, 'id': 'foo/bar'}
        written = context.handle('record_write', args)
        read = context.handle('record_read', {'kind': 'role.note', 'id': 'foo/bar'})
        self.assertEqual(written['record']['id'], 'role/worker/role.note/foo/bar')
        self.assertEqual(read['record_id'], written['record']['id'])
        self.assertEqual(read['record_digest'], written['record_digest'])
        self.assertEqual(read['text'], canonical(written['record']).decode('utf-8'))

        policy = self.config.record_kinds['role.note']
        with self.assertRaises(ConfigError):
            host_records.configuration({'role.note': policy, 'role.note/foo': policy})

    def test_missing_grant_policy_wrong_role_and_spoofing_refuse(self):
        ungranted = self.context('reviewer')
        with self.assertRaisesRegex(Denied, 'no capability'):
            ungranted.handle('record_write', ARGS)
        with self.assertRaisesRegex(Denied, 'record_write grant'):
            ungranted.dispatch('record_write', ARGS)
        ungranted.role = dataclasses.replace(ungranted.role,
            capabilities=(*ungranted.role.capabilities, 'record_write'))
        with self.assertRaisesRegex(Denied, 'undeclared'):
            ungranted.handle('record_write', ARGS)
        context = self.context()
        with patch.object(context, 'config', dataclasses.replace(self.config, record_kinds={})), \
                self.assertRaisesRegex(Denied, 'undeclared'):
            context.handle('record_write', ARGS)
        for extra in ({'kind': 'undeclared'}, {'kind': 'answer'}, {'kind': 'publisher.outcome'},
                      {'writer': 'operator.cli'}, {'writer': 'publisher.cli'},
                      {'writer': 'role.reviewer'}, {'created_at': '2026-01-01T00:00:00Z'}):
            with self.subTest(extra=extra), self.assertRaises(Denied):
                context.handle('record_write', {**ARGS, **extra})
        self.assertFalse((self.project.root / 'shared-records' / 'role').exists())
        for check in (check_consult_role, check_delegate_role):
            with self.assertRaises(ConfigError):
                check('worker', dataclasses.replace(context.role, workspace='read',
                      capabilities=('record_write', 'finish')))

    def test_host_principals_remain_separate_with_both_declarations(self):
        from test_host_records import DECLARATION as publisher_declaration
        from mizu import operator_records
        self.file.write_text(self.file.read_text() + publisher_declaration)
        self.config = load(self.file)
        self.project = Project(self.config, 'sample')
        context = self.context()
        with self.assertRaises(Denied):
            context.handle('record_write', {**ARGS, 'kind': 'publisher.outcome'})
        subject = {'snapshot_id': context.snapshot['id'], 'code_digest': context.snapshot['code_digest']}
        published = host_records.write_publisher(self.project, kind='publisher.outcome',
            subject=subject, payload={'outcome': 'refused', 'reason': 'Host fixture'})
        self.assertEqual(published['record']['writer'], 'publisher.cli')
        notice = operator_records.write(self.project, kind='operator.notice', event_id='host1',
            body='Host notice', addressee='worker')
        self.assertEqual(notice['record']['writer'], 'operator.cli')
        self.assertEqual(context.handle('record_write', ARGS)['record']['writer'], 'role.worker')

    def test_data_and_id_bounds_fail_closed(self):
        context = self.context()
        for changed in ({'subject': '[]'}, {'payload': '{"bad":NaN}'},
                        {'payload': '{"bad":1e9999}'}, {'subject': '{'},
                        {'payload': '{"body":"' + 'x' * 8192 + '"}'},
                        {'payload': 'x' * (PREVIEW_BYTES + 1)},
                        {'id': '../operator/forged'}, {'id': 'x' * 256}):
            with self.subTest(changed=str(changed)[:80]), self.assertRaises((Denied, LimitExceeded)):
                context.handle('record_write', {**ARGS, **changed})
        self.assertEqual(list((self.project.root / 'shared-records').rglob('*.json')), [])

    def test_interrupted_sync_replays_original_identity(self):
        context = self.context()
        with patch('mizu.fs.sync_dir', side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):
                context.handle('record_write', ARGS)
        result = context.handle('record_write', ARGS)
        with patch('mizu.host_records.sync_dir', side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):
                context.handle('record_write', ARGS)
        self.assertEqual(self.context().handle('record_write', ARGS), result)

    def test_policy_is_data_only_and_default_closed(self):
        self.assertEqual(host_records.configuration(), {})
        policy = self.config.record_kinds['role.note']
        invalid = [('writer', 'operator.cli'), ('writer', 'publisher.cli'), ('writer', 'worker'),
                   ('writer', 'role.'), ('writer', 'role.worker/other'), ('route', 'question_source'),
                   ('wake', True), ('wake', 0), ('retention', 'expire'), ('subject', 'snapshot'),
                   ('acknowledgement', 'automatic'), ('maximum', True), ('maximum', 0),
                   ('maximum', PREVIEW_BYTES + 1), ('variants', {}), ('validate', 'callable')]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                host_records.configuration({'role.note': {**policy, field: value}})
        for field in policy:
            row = copy.deepcopy(policy)
            del row[field]
            with self.subTest(missing=field), self.assertRaises(ConfigError):
                host_records.configuration({'role.note': row})

    def test_write_then_later_read_succeeds_with_paging(self):
        context = self.context()
        written = context.handle('record_write', ARGS)
        record, computed = written['record'], written['record_digest']
        immediate = context.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        self.assertEqual(immediate['record_id'], record['id'])
        self.assertEqual(immediate['record_digest'], computed)
        self.assertEqual(immediate['offset'], 0)
        full = canonical(record).decode('utf-8')
        self.assertEqual(immediate['total_chars'], len(full))
        self.assertEqual(immediate['text'], full)
        self.assertIsNone(immediate['next_offset'])
        # Later unit with the same authenticated role rereads the same bytes.
        later = self.context()
        reread = later.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        self.assertEqual(reread, immediate)
        first = context.handle('record_read', {'kind': 'role.note', 'id': 'note1', 'offset': 0, 'limit': 10})
        self.assertEqual(first['total_chars'], len(full))
        self.assertEqual(first['text'], full[:10])
        self.assertEqual(first['next_offset'], 10)
        second = context.handle('record_read', {'kind': 'role.note', 'id': 'note1', 'offset': 10, 'limit': 8192})
        self.assertEqual(second['text'], full[10:])
        self.assertIsNone(second['next_offset'])
        # Reading selects no receipt and leaves the store untouched.
        self.assertFalse(self.shared().received('worker', record['id'], computed))
        self.assertEqual(context.handle('record_write', ARGS), written)
        self.assertEqual(self.context().handle('record_read', {'kind': 'role.note', 'id': 'note1'}), immediate)

    def test_role_read_refuses_cross_role_missing_grant_and_policy(self):
        context = self.context()
        written = context.handle('record_write', ARGS)
        reviewer = self.context('reviewer')
        with self.assertRaisesRegex(Denied, 'no capability'):
            reviewer.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        with self.assertRaisesRegex(Denied, 'record_read grant'):
            reviewer.dispatch('record_read', {'kind': 'role.note', 'id': 'note1'})
        reviewer_role = dataclasses.replace(reviewer.role,
            capabilities=(*reviewer.role.capabilities, 'record_read'))
        reviewer.role = reviewer_role
        reviewer.config = dataclasses.replace(reviewer.config,
            roles={**reviewer.config.roles, 'reviewer': reviewer_role})
        with self.assertRaisesRegex(Denied, 'undeclared'):
            reviewer.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        with self.assertRaisesRegex(Denied, 'undeclared'):
            reviewer.dispatch('record_read', {'kind': 'role.note', 'id': 'note1'})
        with patch.object(context, 'config', dataclasses.replace(self.config, record_kinds={})):
            with self.assertRaisesRegex(Denied, 'undeclared'):
                context.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        for bad in ({'kind': 'undeclared', 'id': 'note1'}, {'kind': 'answer', 'id': 'note1'},
                    {'kind': 'publisher.outcome', 'id': 'x'}, {'kind': 'role.note', 'id': '../operator/forged'},
                    {'kind': 'role.note', 'id': 'x' * 256}, {'kind': 'role.note', 'id': 'missing'}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                context.handle('record_read', bad)
        with self.assertRaises(Denied):
            context.handle('record_read', {'kind': 'role.note', 'id': 'note1', 'ack_token': '0' * 64})
        with self.assertRaises(Denied):
            context.handle('record_read', {'ack_token': '0' * 64, 'kind': 'role.note', 'id': 'note1'})
        with self.assertRaises(Denied):
            context.handle('record_read', {})
        for paging in ({'kind': 'role.note', 'id': 'note1', 'offset': -1},
                       {'kind': 'role.note', 'id': 'note1', 'limit': 0},
                       {'kind': 'role.note', 'id': 'note1', 'limit': 8193},
                       {'kind': 'role.note', 'id': 'note1', 'offset': 10 ** 9}):
            with self.subTest(paging=paging), self.assertRaises(Denied):
                context.handle('record_read', paging)
        # Exact retry still succeeds for the author after all refusals.
        self.assertEqual(context.handle('record_read', {'kind': 'role.note', 'id': 'note1'})['record_digest'],
                         written['record_digest'])

    def test_manifest_auto_grant_does_not_authorize_own_record_read(self):
        from mizu import operator_records
        context = self.context()
        written = context.handle('record_write', ARGS)
        configured = context.config.roles['worker']
        no_read = dataclasses.replace(configured, capabilities=tuple(
            cap for cap in configured.capabilities if cap != 'record_read'))
        context.config = dataclasses.replace(context.config,
            roles={**context.config.roles, 'worker': no_read})
        context.role = no_read
        notice = operator_records.write(self.project, kind='operator.notice', event_id='auto-grant',
            body='Host notice', addressee='worker')
        operator_records.bind(context)
        self.assertIn('record_read', context.role.capabilities)  # Existing manifest-driven grant.
        with self.assertRaisesRegex(Denied, 'explicit record_read grant'):
            context.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        token = next(entry.ack_token for entry in context.operator_record_manifest
                     if entry.record_id == notice['record']['id'])
        legacy = context.handle('record_read', {'ack_token': token})
        self.assertEqual(legacy['record_digest'], notice['record_digest'])
        self.assertEqual(written['record_digest'], self.shared().get(written['record']['id'])[1])

    def test_record_read_modes_are_isolated_and_operator_unchanged(self):
        from mizu import operator_records
        context = self.context()
        written = context.handle('record_write', ARGS)
        role_read = context.handle('record_read', {'kind': 'role.note', 'id': 'note1'})
        self.assertEqual(role_read['record_digest'], written['record_digest'])
        notice = operator_records.write(self.project, kind='operator.notice', event_id='host1',
            body='Host notice', addressee='worker')
        fresh = self.context()
        operator_records.bind(fresh)
        token = next(entry.ack_token for entry in fresh.operator_record_manifest
                     if entry.record_id == notice['record']['id'])
        legacy = fresh.handle('record_read', {'ack_token': token, 'limit': 32})
        self.assertEqual(legacy['record_digest'], notice['record_digest'])
        self.assertIsNotNone(legacy['next_offset'])
        self.assertEqual(fresh.handle('record_read', {'kind': 'role.note', 'id': 'note1'})['record_digest'],
                         written['record_digest'])
        with self.assertRaises(Denied):
            fresh.handle('record_read', {'ack_token': token, 'kind': 'role.note', 'id': 'note1'})
        self.assertFalse(self.shared().received('worker', written['record']['id'], written['record_digest']))
        self.assertFalse(operator_records.store(self.project).received('worker', notice['record']['id'], notice['record_digest']))
        # Role reads are not consult/delegate writes; reads stay allowed while writes stay forbidden.
        for check in (check_consult_role, check_delegate_role):
            with self.assertRaises(ConfigError):
                check('worker', dataclasses.replace(context.role, workspace='read',
                      capabilities=('record_write', 'finish')))
            check('worker', dataclasses.replace(context.role, workspace='read',
                  capabilities=('record_read', 'finish')))
