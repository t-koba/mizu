"""Declared policy and fake host publisher route; no actual publisher integration."""
import dataclasses
import json
from unittest.mock import patch

from test_ci_waits import CiWaitFixture
from mizu import ci_waits, host_records
from mizu.cli import parser, execute
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import PREVIEW_BYTES, canonical, now
from mizu.project import Project
from mizu.runtime import should_run
from mizu.storage import audit, reclaim

DECLARATION = '''
[record_kinds."publisher.outcome"]
writer = "publisher.cli"
route = "publication_wait"
wake = true
retention = "permanent"
acknowledgement = "explicit"
subject = "snapshot"
maximum = 8192
[record_kinds."publisher.outcome".variants.refused.reason]
type = "text"
maximum = 1024
[record_kinds."publisher.outcome".variants.superseded.replacement]
type = "newer_snapshot"
'''
KIND = 'publisher.outcome'
ROLE_DECLARATION = '''
[record_kinds."probe.note"]
writer = "role.worker"
route = "none"
wake = false
retention = "permanent"
acknowledgement = "explicit"
subject = "object"
maximum = 1024
'''


class HostRecordTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        self.file.write_text(self.file.read_text() + DECLARATION)
        loaded = load(self.file)
        self.config = dataclasses.replace(self.config, record_kinds=loaded.record_kinds,
                                          vcs={**self.config.vcs, 'ci_branch': 'published'})
        self.project = Project(self.config, 'sample')
        self.store = self.project.work_items
        self.snapshot = self.project.snapshots.get()
        self.subject = {key: self.snapshot['id' if key == 'snapshot_id' else key]
                        for key in ('snapshot_id', 'code_digest')}
        self.wait = {'kind': 'publication_ci', 'snapshot_id': self.snapshot['id'], 'checks': ['unit']}
        self.refused = {'outcome': 'refused', 'reason': 'Publication approval was refused'}

    def write(self, payload=None, subject=None):
        return host_records.write_publisher(self.project, kind=KIND,
            subject=self.subject if subject is None else subject,
            payload=self.refused if payload is None else payload)

    def shared(self):
        return host_records.store(self.project.root, self.project.snapshots, self.config.record_kinds)

    def newer(self, publish=True):
        snap = self.project.snapshots.create(self.project.snapshots.capture_files(self.project.workspace),
            goal='Goal', state='New candidate', run=None, outcome='wait', summary='Candidate')
        if publish:
            self.project.snapshots.publish(snap)
        return {'snapshot_id': snap['id'], 'code_digest': snap['code_digest']}

    def test_configuration_is_bounded_declarative_and_closed_by_default(self):
        self.assertEqual(host_records.configuration(), {})
        policy = self.config.record_kinds[KIND]
        invalid = [None, [], {'extra': True}]
        for field, value in [('writer', 'role.worker'), ('route', 'addressee'),
                             ('wake', 1), ('retention', 'expire'), ('acknowledgement', 'automatic'),
                             ('subject', 'branch'), ('maximum', True), ('maximum', 0),
                             ('variants', {'refused': {'reason': {'type': 'callable'}}}),
                             ('variants', {'refused': {'reason': {'type': 'text', 'maximum': -1}}}),
                             ('variants', {'refused': {'reason': {'type': 'text', 'maximum': PREVIEW_BYTES + 1}},
                                           'superseded': {'replacement': {'type': 'newer_snapshot'}}})]:
            invalid.append({**policy, field: value})
        invalid.append({**policy, 'variants': {'unknown': {}}})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                host_records.configuration({KIND: value})
        with self.assertRaises(ConfigError):
            host_records.configuration({KIND: policy, 'other': policy})
        with patch.object(self.project, 'config', dataclasses.replace(self.config, record_kinds={})), self.assertRaises(Denied):
            self.write()

    def test_host_cli_binds_identity_and_exact_replay(self):
        # Preserve the configured fake VCS hook; this route performs no VCS calls.
        args = parser().parse_args(['--config', str(self.file), 'publisher-record', 'sample',
            '--kind', KIND, '--subject', json.dumps(self.subject), '--payload', json.dumps(self.refused)])
        result = execute(args)
        self.assertEqual(result, self.write())
        self.assertEqual(result['record']['writer'], host_records.PUBLISHER)
        self.assertEqual(result, self.write())
        with self.assertRaises(Denied):
            self.write({**self.refused, 'reason': 'Conflicting refusal'})
        with self.assertRaises(TypeError):
            host_records.write_publisher(self.project, kind=KIND, subject=self.subject,
                                         payload=self.refused, writer='role.worker')
        forged = {**result['record'], 'writer': 'role.worker'}
        with self.assertRaises(Denied):
            self.shared().put(forged, writer='role.worker')
        with self.assertRaises(Denied):
            self.shared().put(result['record'], writer='role.worker')
        with self.assertRaises(Denied):
            self.context().handle('record_write', {})

    def test_snapshot_identity_and_payload_bounds(self):
        for subject in ({}, {'snapshot_id': self.subject['snapshot_id']},
                        {**self.subject, 'code_digest': 'a' * 64},
                        {**self.subject, 'snapshot_id': 'f' * 64}):
            with self.subTest(subject=subject), self.assertRaises(Denied):
                self.write(subject=subject)
        for payload in ([], {}, {'outcome': 'unknown'}, {'outcome': 'refused'},
                        {**self.refused, 'reason': ' '}, {**self.refused, 'reason': 'x' * 1025},
                        {**self.refused, 'extra': True}):
            with self.subTest(payload=payload), self.assertRaises(Denied):
                self.write(payload)
        # Exact retained objects remain valid even after the rolling list moves.
        with patch.object(self.project.snapshots, '_history_ids', return_value=[]):
            self.assertEqual(self.write()['record']['subject'], self.subject)

    def test_refusal_wakes_only_holder_and_explicit_ack_never_completes(self):
        item = self.block()
        snap = {**self.snapshot, 'outcome': 'done', 'wake_generation': self.project.control()['wake_generation']}
        self.assertFalse(should_run(self.project, snap, role_name='worker'))
        result = self.write()
        with patch('mizu.vcs.invoke', side_effect=AssertionError('No provider call')):
            self.assertTrue(should_run(self.project, snap, role_name='worker'))
            self.assertEqual(ci_waits.pending(self.store, 'reviewer'), [])
            context = self.delivery_context()
        event = context.wait_events[0]
        self.assertEqual(event['outcome'], self.refused)
        self.assertEqual(event['source_digest'], result['record_digest'])
        self.assertEqual(event['snapshot_id'], self.snapshot['id'])
        ci_waits.complete(context, {'outcome': 'wait'})
        self.assertEqual(self.delivery_context().wait_events, context.wait_events)
        foreign = self.delivery_context('reviewer')
        with self.assertRaises(Denied):
            ci_waits.validate_selection(foreign, [event['ack_token']])
        self.acknowledge_delivery(context)
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        self.assertTrue(should_run(self.project, snap, role_name='worker'))
        ready = self.store.page_ready(role_name='worker')
        self.assertEqual(ready['items'][0]['wait_liveness']['outcome'], 'refused')
        self.assertEqual(self.store.page_ready(role_name='reviewer')['total'], 0)
        self.assertEqual(self.store.get(item['id']), item)
        second = self.block('another-wait')
        self.assertEqual(ci_waits.pending(self.store, 'worker')[0]['work_item'], second['id'])

    def test_exact_supersession_accepts_retained_unpublished_replacement(self):
        item = self.block()
        unpublished = self.newer(publish=False)
        for replacement in (self.subject, {}, {**unpublished, 'code_digest': 'f' * 64},
                            {**unpublished, 'snapshot_id': 'f' * 64}):
            with self.subTest(replacement=replacement), self.assertRaises(Denied):
                self.write({'outcome': 'superseded', 'replacement': replacement})
        sibling = self.newer(publish=False)
        subject = {key: unpublished[key] for key in ('snapshot_id', 'code_digest')}
        with self.assertRaises(Denied):
            self.write({'outcome': 'superseded', 'replacement': sibling}, subject=subject)
        payload = {'outcome': 'superseded', 'replacement': unpublished}
        # The blocked wait pins a snapshot that may have left rolling history;
        # the publisher can still name an exact retained newer candidate.
        with patch.object(self.project.snapshots, '_history_ids', return_value=[]):
            result = self.write(payload)
            self.assertEqual(self.write(payload), result)
            event = self.delivery_context().wait_events[0]
            self.assertEqual(event['outcome'], payload)
            self.assertEqual(ci_waits.pending(self.store, 'worker')[0]['outcome'], payload)
        self.assertEqual(self.store.get(item['id']), item)
        report = audit(self.project)
        self.assertEqual(report['reference_records_skipped'], 0)
        self.assertEqual(report['references']['host_record_snapshots'], 2)
        path = self.project.root / 'snapshots' / (unpublished['snapshot_id'] + '.json')
        original = path.read_bytes()
        path.unlink()
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        path.write_bytes(original)

    def test_host_reference_scan_budget_is_incomplete_and_blocks_reclaim(self):
        (self.project.workspace / "host-record-one.txt").write_text("first")
        legacy_candidate = self.newer(publish=False)
        first = self.write(subject=legacy_candidate)
        legacy_name = self.shared()._legacy_record_name(first["record"]["id"])
        canonical_path = self.project.root / self.shared()._record_name(first["record"]["id"])
        legacy_path = self.project.root / legacy_name
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.replace(legacy_path)
        (self.project.workspace / "host-record-two.txt").write_text("second")
        referenced_candidate = self.newer(publish=False)
        self.write(subject=referenced_candidate)
        (self.project.workspace / "host-record-orphan.txt").write_text("orphan")
        unreferenced = self.newer(publish=False)
        orphan_object = self.project.root / "objects" / ("f" * 64)
        orphan_object.write_bytes(b"orphan")
        self.project.set_control(paused=True)

        for budget, should_be_incomplete in ((4, False), (3, True), (2, True)):
            limits = dataclasses.replace(
                self.config.limits, host_record_reference_scan_entries=budget)
            config = dataclasses.replace(self.config, limits=limits)
            with self.subTest(budget=budget), patch.object(self.project, "config", config):
                report = audit(self.project)
                if should_be_incomplete:
                    self.assertGreater(report["reference_records_skipped"], 0)
                    preview = reclaim(self.project)
                    self.assertGreater(preview["reference_records_skipped"], 0)
                    with self.assertRaisesRegex(Denied, "Skipped reference records"):
                        reclaim(self.project, apply=True)
                    self.assertTrue((self.project.root / "snapshots" /
                                     (referenced_candidate["snapshot_id"] + ".json")).is_file())
                    self.assertTrue((self.project.root / "snapshots" /
                                     (unreferenced["snapshot_id"] + ".json")).is_file())
                    self.assertTrue(orphan_object.is_file())
                else:
                    self.assertEqual(report["reference_records_skipped"], 0)
                    self.assertEqual(report["references"]["host_record_snapshots"], 2)
                    complete = reclaim(self.project)["snapshots"]
                    self.assertNotIn(f"snapshots/{legacy_candidate['snapshot_id']}.json", complete)
                    self.assertNotIn(f"snapshots/{referenced_candidate['snapshot_id']}.json", complete)
                    self.assertIn(f"snapshots/{unreferenced['snapshot_id']}.json", complete)

    def test_missing_unknown_mismatched_and_corrupt_source_remains_pending(self):
        self.block()
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        # A different valid candidate with the same code tree is not this waiter.
        newer = self.newer()
        self.write(subject=newer)
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        result = self.write()
        path = self.project.root / self.shared()._record_name(result['record']['id'])
        original = path.read_bytes()
        for change in ({'subject': newer}, {'writer': 'unknown.host'},
                       {'payload': {'outcome': 'unknown'}}, {'payload': {}}):
            path.write_bytes(canonical({**result['record'], **change}))
            self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        path.write_bytes(b'{')
        self.assertEqual(audit(self.project)['reference_records_skipped'], 1)
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
        path.write_bytes(original)
        self.assertEqual(len(ci_waits.pending(self.store, 'worker')), 1)
        legacy = self.project.root / self.shared()._legacy_record_name(result['record']['id'])
        legacy.write_bytes(b'{}')
        self.assertEqual(ci_waits.pending(self.store, 'worker'), [])

    def test_foreign_kind_record_in_host_partition_is_an_incomplete_reference(self):
        # A canonically stored record of another declared family can sit in the
        # host partition. Its subject names no snapshot, so the scan must report
        # an incomplete reference source instead of aborting maintenance.
        self.file.write_text(self.file.read_text() + ROLE_DECLARATION)
        loaded = load(self.file)
        config = dataclasses.replace(self.config, record_kinds=loaded.record_kinds)
        shared = host_records.store(self.project.root, self.project.snapshots, config.record_kinds)
        shared.put(dict(schema=1, id='host/probe/x', kind='probe.note', writer='role.worker',
                        created_at=now(), subject={}, payload={}), writer='role.worker')
        self.project.set_control(paused=True)
        with patch.object(self.project, 'config', config):
            report = audit(self.project)
            self.assertGreater(report['reference_records_skipped'], 0)
            self.assertEqual(report['references']['host_record_snapshots'], 0)
            preview = reclaim(self.project)
            self.assertGreater(preview['reference_records_skipped'], 0)
            with self.assertRaisesRegex(Denied, 'Skipped reference records'):
                reclaim(self.project, apply=True)

    def test_disabled_wake_and_stale_ack_remain_pending(self):
        item = self.block()
        self.write()
        kinds = {KIND: {**self.config.record_kinds[KIND], 'wake': False}}
        with patch.object(self.project, 'config', dataclasses.replace(self.config, record_kinds=kinds)), \
                patch.object(self.store, 'record_kinds', kinds), \
                patch('mizu.host_records.os.utime') as wake_hint:
            self.assertEqual(ci_waits.pending(self.store, 'worker'), [])
            self.assertEqual(self.store.page_ready(role_name='worker')['total'], 0)
            self.write()
            wake_hint.assert_not_called()
        context = self.delivery_context()
        self.store.transition(item['id'], expected_rev=item['rev'], actor='worker',
                              status='open', reason='Reconsider wait')
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)

    def test_source_is_revalidated_for_ack_and_conflicting_ci_stays_pending(self):
        self.block()
        self.write()
        context = self.delivery_context()
        event = context.wait_events[0]
        path = self.project.root / self.shared()._record_name(event['source_record'])
        original = path.read_bytes()
        path.write_bytes(b'{}')
        with self.assertRaises(Denied):
            self.acknowledge_delivery(context)
        path.write_bytes(original)
        # Existing source evidence is never silently replaced by a publisher outcome.
        with patch('mizu.ci_waits._read', return_value={'kind': 'ci'}):
            self.assertEqual(ci_waits.pending(self.store, 'worker'), [])

    def test_unknown_or_incomplete_publisher_contract_is_rejected_at_config_load(self):
        for variants in ({'unknown': {}}, {'refused': {}}):
            kinds = {KIND: {**self.config.record_kinds[KIND], 'variants': variants}}
            with self.subTest(variants=variants), self.assertRaises(ConfigError):
                host_records.configuration(kinds)

    def test_interrupted_source_sync_replays_exact_record_before_delivery(self):
        self.block()
        with patch('mizu.fs.sync_dir', side_effect=OSError('synthetic sync failure')), \
                patch('mizu.host_records.sync_dir', side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):
                self.write()
            self.assertEqual(self.delivery_context().wait_events, [])
        result = self.write()
        self.assertEqual(self.write(), result)
        self.assertEqual(self.delivery_context().wait_events[0]['source_digest'], result['record_digest'])
