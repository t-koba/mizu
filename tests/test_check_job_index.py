"""Check-job index: rebuild equality, crash repair, eviction, audit and indexed readers."""
import contextlib
import dataclasses
import sqlite3
import threading
import unittest
from unittest.mock import patch

from support import Fixture, forget_ledger
from test_check_jobs import FakeSandbox
from mizu import acceptance, check_jobs, ledger
from mizu.fs import lock, write_json


def rows(root):
    with contextlib.closing(sqlite3.connect(root / ledger.NAME)) as db, db:
        return (db.execute("SELECT * FROM check_jobs ORDER BY id").fetchall(),
                db.execute("SELECT key FROM intents WHERE domain='check_jobs'").fetchall())


class CheckJobIndexTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config, roles={k: dataclasses.replace(v, capabilities=(*v.capabilities, 'check_job'))
                                for k, v in self.config.roles.items()})
        self.project.config = self.config
        FakeSandbox.calls = []

    def queue(self, script='one', role='worker'):
        return check_jobs.submit(self.context(role), script, kind='experiment')

    def run_all(self):
        while check_jobs.run_one(self.project, threading.Event(), sandbox_factory=FakeSandbox)['status'] \
                not in ('idle', 'deferred'):
            pass

    def build(self):
        with self.project.insights.ledger.read('check_jobs', locked=True):
            pass
        return rows(self.project.root)

    def test_rebuild_equals_incremental_rows_through_a_job_lifecycle(self):
        first, second = self.queue('one'), self.queue('two', 'reviewer')
        self.project.set_control(paused=False)
        self.run_all()
        check_jobs.acknowledge(self.project, 'worker', [first['id']])
        expected = self.build()
        self.assertEqual(expected[1], [])
        forget_ledger(self.project.root)
        self.assertEqual(self.build(), expected)
        self.assertEqual(len(expected[0]), 2)
        self.assertTrue(self.project.work_items.audit()['check_jobs']['consistent'])

    def test_crash_between_file_and_row_is_repaired_by_the_next_locked_use(self):
        job = self.queue()
        self.build()
        original, calls = ledger.Ledger._repair, []

        def crash_on_replay(self, db, names, *, recover):
            calls.append(names)
            if len(calls) == 2:  # The first is the preparation before the intent exists.
                raise RuntimeError('crash')
            return original(self, db, names, recover=recover)

        with patch.object(ledger.Ledger, '_repair', crash_on_replay):
            with self.assertRaises(RuntimeError):
                with lock(self.project.root / 'locks/check-jobs.lock'):
                    acceptance.write_job(self.project, job['id'], {**check_jobs.raw_read(self.project, job['id']),
                                                                   'acknowledged': True})
        self.assertEqual(len(rows(self.project.root)[1]), 1)
        # A reader already sees the committed file through a private replay.
        self.assertTrue(acceptance.discover(self.project)[0][job['id']]['acknowledged'])
        self.build()
        self.assertEqual(rows(self.project.root)[1], [])

    def test_eviction_removes_the_row_with_the_directory(self):
        job = self.queue()
        self.build()
        record = check_jobs.raw_read(self.project, job['id'])
        with lock(self.project.root / 'locks/check-jobs.lock'):
            acceptance.write_job(self.project, job['id'], {**record, 'status': 'cancelled', 'acknowledged': True})
            self.project.set_control(paused=False)
            with self.capped(1):
                check_jobs._make_room(self.project)
        self.assertEqual(rows(self.project.root)[0], [])

    def test_invalid_records_are_rows_the_audit_reports(self):
        job = self.queue()
        self.build()
        path = check_jobs.root(self.project) / job['id'] / 'job.json'
        original = path.read_bytes()
        path.write_text('{')
        result = self.project.work_items.audit()['check_jobs']
        self.assertEqual((result['consistent'], result['divergent']), (False, [job['id']]))
        forget_ledger(self.project.root)
        self.assertEqual(acceptance.discover(self.project)[1], {job['id']: 'job_json'})
        self.assertEqual(self.project.work_items.audit()['check_jobs']['corrupt'][0]['error'], 'job_json')
        path.write_bytes(original)
        self.assertTrue(self.project.work_items.audit(repair=True)['check_jobs']['repaired'] or
                        self.project.work_items.audit()['check_jobs']['consistent'])

    def test_wake_readiness_reads_columns_not_files(self):
        job = self.queue()
        self.project.set_control(paused=False)
        self.run_all()
        self.build()
        with patch('mizu.acceptance.read_job', side_effect=AssertionError('record read')):
            self.assertTrue(check_jobs.wake_ready(self.project, 'worker'))
            self.assertFalse(check_jobs.wake_ready(self.project, 'reviewer'))
        check_jobs.acknowledge(self.project, 'worker', [job['id']])
        self.assertFalse(check_jobs.wake_ready(self.project, 'worker'))


if __name__ == '__main__':
    unittest.main()
