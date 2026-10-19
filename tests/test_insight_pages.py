"""Local page, migration and CLI regressions; no model/provider integration."""
import base64
import contextlib
import io
import json
import sqlite3
from unittest.mock import patch

from support import Fixture
from mizu import insight_archive
from mizu.cli import main
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest, read_json, write_json
from mizu.insights import Insights
from mizu.project import Project
from mizu.storage import backup, restore


class InsightPageTests(Fixture):
    def bulk_live(self, count=1007):
        # Reverse creation order and tied timestamps exercise all key columns,
        # not filesystem iteration or a timestamp-only continuation. These are
        # read-only fixtures, so durable atomic writes would add no coverage.
        inbox = self.project.root / 'inbox'
        inbox.mkdir(parents=True, exist_ok=True)
        for n in reversed(range(count)):
            record = self.record(n)
            (inbox / f'{record["id"]}.json').write_bytes(canonical(record))

    def record(self, n, rev=1):
        return {"id": f"{n:032x}", "rev": rev, "source": "searcher", "origin": None,
                "title": f"Title {n}", "body": f"Body {n}", "base_snapshot": None,
                "created_at": "2026-01-01T00:00:00+00:00", "run": None, "evidence": []}

    def legacy_archive(self, count=1007):
        # Exact pre-sequence schema, including its existing prefix index.
        path = self.project.root / insight_archive.NAME
        records = [self.record(n) for n in reversed(range(count))]
        for record in records:
            if record['id'] == self.record(1)['id']:
                record['base_snapshot'] = self.project.snapshots.get()['id']
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE insights (id TEXT NOT NULL, rev INTEGER NOT NULL, "
                       "record TEXT NOT NULL, decision TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY (id, rev))")
            db.execute("CREATE INDEX insight_prefix ON insights (id COLLATE NOCASE)")
            db.executemany("INSERT INTO insights VALUES (?, ?, ?, ?, ?)",
                           [(r['id'], r['rev'], canonical(r).decode(),
                             canonical({'action': 'reject', 'rev': r['rev']}).decode(),
                             canonical({'identity': r['id']}).decode())
                            for r in records])
        return path

    def archive_rows(self, root=None):
        with contextlib.closing(sqlite3.connect((root or self.project.root) / insight_archive.NAME)) as db:
            return db.execute("SELECT id, rev, record, decision, identity FROM insights ORDER BY id, rev").fetchall()

    def collect(self, scope, limit, first=None):
        page = first or self.project.insights.list_page(scope=scope, limit=limit)
        generation, expiry = page['generation'], page['cursor_expires_at']
        items = []
        while True:
            self.assertEqual(set(page), {'items', 'generation', 'next_cursor', 'has_more', 'cursor_expires_at'})
            self.assertEqual(page['generation'], generation)
            self.assertEqual(page['cursor_expires_at'], expiry)
            self.assertLessEqual(len(page['items']), limit)
            items.extend(page['items'])
            if not page['has_more']:
                self.assertIsNone(page['next_cursor'])
                return items
            self.assertTrue(page['next_cursor'])
            page = self.project.insights.list_page(scope=scope, limit=limit, cursor=page['next_cursor'])

    def test_complete_live_ties_and_replay_over_1000(self):
        self.bulk_live()
        # Same created_at/id but distinct paths: no row may disappear.
        alias = self.record(0)
        alias['title'] = 'Alias sorts first by path'
        write_json(self.project.root / 'inbox' / '000-alias.json', alias)
        page = self.project.insights.list_page(limit=1000)
        again = self.project.insights.list_page(limit=7, cursor=page['next_cursor'])
        self.assertEqual(again, self.project.insights.list_page(limit=7, cursor=page['next_cursor']))
        items = self.collect('live', 1000, page)
        self.assertEqual(len(items), 1008)
        self.assertEqual(items, self.project.insights.list(pending=False, limit=2000))
        self.assertEqual(items[0]['title'], alias['title'])
        self.assertEqual(len(self.project.insights.list(pending=False, limit=1000)), 1000)

    def test_live_staleness_binds_all_rows_bodies_evidence_and_decisions(self):
        self.bulk_live(3)
        for kind in ('body', 'evidence', 'decision', 'revision', 'append', 'remove', 'path'):
            with self.subTest(kind=kind):
                page = self.project.insights.list_page(limit=1)
                path = self.project.root / 'inbox' / f'{2:032x}.json'
                record = read_json(path)
                if kind in ('body', 'evidence', 'revision'):
                    if kind == 'body':
                        record['body'] += ' changed'
                    elif kind == 'evidence':
                        record['evidence'] = [{'text': 'same count and timestamp still binds complete text'}]
                    else:
                        record['rev'] += 1
                    write_json(path, record)
                elif kind == 'decision':
                    self.project.insights.decide(record['id'], 'defer', 'reason', 'later', 'run', expected_rev=1)
                elif kind == 'append':
                    write_json(self.project.root / 'inbox' / 'extra.json', self.record(3))
                elif kind == 'remove':
                    path.unlink()
                else:
                    # Recreate the row removed above at a different path.
                    write_json(self.project.root / 'inbox' / 'alias.json', self.record(2))
                with self.assertRaisesRegex(Denied, 'Stale insight live cursor'):
                    self.project.insights.list_page(limit=1, cursor=page['next_cursor'])

    def test_evidence_same_summary_stales_and_noop_submission_does_not(self):
        self.bulk_live(2)
        insight_id = self.record(0)['id']
        self.project.insights.attach_evidence(insight_id, source='searcher', text='first', expected_rev=1)
        page = self.project.insights.list_page(limit=1)
        self.project.insights.submit(source='searcher', title='Title 0', body='Body 0', base_snapshot=None,
                                     insight_id=insight_id)
        self.project.insights.list_page(limit=1, cursor=page['next_cursor'])
        path = self.project.root / 'inbox' / f'{insight_id}.json'
        record = read_json(path)
        record['evidence'][0]['text'] = 'edited, same summary'
        write_json(path, record)
        with self.assertRaisesRegex(Denied, 'Stale'):
            self.project.insights.list_page(limit=1, cursor=page['next_cursor'])

    def test_live_cost_does_not_scan_archive(self):
        self.bulk_live(3)
        self.legacy_archive()
        with patch('mizu.insight_archive._connect', side_effect=AssertionError('archive scan')):
            self.assertEqual(len(self.project.insights.list(pending=False)), 3)
            self.assertEqual(len(self.collect('live', 2)), 3)

    def test_archive_migration_indexed_keyset_and_append_isolation(self):
        path = self.legacy_archive()
        original = self.archive_rows()
        first = self.project.insights.list_page(scope='archive', limit=1000)
        self.assertEqual(self.archive_rows(), original)
        self.assertEqual(first['items'][0]['id'], self.record(1006)['id'])
        # Later archival, including a newer revision, must not leak into this traversal.
        insight_archive.store(self.project.root, self.record(0, rev=2), {'action': 'reject'}, {'id': 'identity'})
        insight_archive.store(self.project.root, self.record(1007), {'action': 'reject'}, {'id': 'identity'})
        second = self.project.insights.list_page(scope='archive', limit=1000, cursor=first['next_cursor'])
        self.assertEqual(second, self.project.insights.list_page(scope='archive', limit=1000, cursor=first['next_cursor']))
        items = self.collect('archive', 1000, first)
        self.assertEqual([i['id'] for i in items], [self.record(n)['id'] for n in reversed(range(1007))])
        fresh = self.collect('archive', 1000)
        self.assertEqual(len(fresh), 1009)
        self.assertNotEqual(first['generation'], self.project.insights.list_page(scope='archive')['generation'])
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT seq FROM insights ORDER BY seq').fetchall(), [(n,) for n in range(1, 1010)])
            plan = db.execute('EXPLAIN QUERY PLAN SELECT seq, record, decision FROM insights '
                              'WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?', (1000, 1007, 1001)).fetchall()
            self.assertTrue(any('SEARCH insights USING INTEGER PRIMARY KEY' in row[3] for row in plan), plan)
        self.assertEqual(self.project.insights.read(self.record(0)['id'])['rev'], 2)
        self.assertEqual(self.archive_rows()[:1][0][:2], (self.record(0)['id'], 1))

    def test_legacy_write_migration_keeps_immutable_bindings_and_monotonic_seq(self):
        path = self.legacy_archive(3)
        original = self.archive_rows()
        insight_archive.store(self.project.root, self.record(0), {'action': 'accept'}, {'changed': True})
        self.assertEqual(self.archive_rows(), original)
        with contextlib.closing(sqlite3.connect(path)) as db:
            before = db.execute('SELECT MAX(seq) FROM insights').fetchone()[0]
        insight_archive.store(self.project.root, self.record(4), {'action': 'reject'}, {'identity': 'new'})
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.assertGreater(db.execute('SELECT MAX(seq) FROM insights').fetchone()[0], before)
        changed = {**self.record(0), 'body': 'conflicting body'}
        with self.assertRaisesRegex(Denied, 'content conflict'):
            insight_archive.store(self.project.root, changed, {'action': 'reject'}, {})
        self.assertEqual(self.archive_rows()[:3], original)

    def test_legacy_id_reads_references_then_migrated_backup_restore(self):
        path = self.legacy_archive(3)
        original = self.archive_rows()
        self.assertEqual(insight_archive.read(self.project.root, self.record(0)['id']), self.record(0))
        refs = list(insight_archive.snapshot_references(self.project.root))
        self.assertIn(self.project.snapshots.get()['id'], refs)
        self.project.insights.list_page(scope='archive', limit=1)
        self.assertEqual(list(insight_archive.snapshot_references(self.project.root)), refs)
        self.assertEqual(self.archive_rows(), original)
        self.project.set_control(paused=True)
        destination = self.root / 'pages.tar.gz'
        backup(self.project, destination)
        restore(self.config, 'restored', destination)
        restored = Project(self.config, 'restored')
        self.assertEqual(self.archive_rows(restored.root), original)
        restored.insights.list_page(scope='archive', limit=1)
        insight_archive.store(restored.root, self.record(4), {'action': 'reject'}, {'id': 'identity'})
        with contextlib.closing(sqlite3.connect(restored.root / insight_archive.NAME)) as db:
            self.assertEqual(db.execute('SELECT MAX(seq) FROM insights').fetchone()[0], 4)
        self.assertEqual(list(insight_archive.snapshot_references(restored.root)), refs + [None])
        self.assertTrue(path.is_file())

    def test_migration_rolls_back_without_changing_legacy_rows(self):
        path = self.legacy_archive(3)
        original = self.archive_rows()
        connect = sqlite3.connect
        class FailingMigration(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith('DROP TABLE insights_legacy'):
                    raise sqlite3.OperationalError('injected migration failure')
                return super().execute(sql, *args)
        with patch('mizu.insight_archive.sqlite3.connect', side_effect=lambda *a, **k: connect(*a, **k, factory=FailingMigration)):
            with self.assertRaisesRegex(Denied, 'unavailable'):
                self.project.insights.list_page(scope='archive')
        self.assertEqual(self.archive_rows(), original)
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.assertNotIn('seq', [r[1] for r in db.execute('PRAGMA table_info(insights)')])
        self.assertEqual(len(self.collect('archive', 2)), 3)

    def test_expiry_absolute_not_extended_and_scope_project_mismatch(self):
        self.bulk_live(3)
        self.legacy_archive(3)
        self.project.insights.cursor_ttl_seconds = 60
        for scope in ('live', 'archive'):
            with self.subTest(scope=scope), patch('mizu.insights.time.time', return_value=2000000000):
                first = self.project.insights.list_page(scope=scope, limit=1)
            with patch('mizu.insights.time.time', return_value=2000000059):
                second = self.project.insights.list_page(scope=scope, limit=1, cursor=first['next_cursor'])
                self.assertEqual(first['cursor_expires_at'], second['cursor_expires_at'])
                with self.assertRaisesRegex(Denied, 'Foreign scope'):
                    self.project.insights.list_page(scope='archive' if scope == 'live' else 'live', cursor=first['next_cursor'])
                with self.assertRaisesRegex(Denied, 'Foreign scope or project'):
                    Insights(self.root / 'other').list_page(scope=scope, cursor=first['next_cursor'])
            with patch('mizu.insights.time.time', return_value=2000000060):
                for cursor in (first['next_cursor'], second['next_cursor']):
                    with self.assertRaisesRegex(Denied, 'Expired'):
                        self.project.insights.list_page(scope=scope, cursor=cursor)

    def test_empty_and_limit_bounds(self):
        for scope in ('live', 'archive'):
            page = self.project.insights.list_page(scope=scope, limit=1)
            self.assertEqual(page['items'], [])
            self.assertFalse(page['has_more'])
            self.assertIsNone(page['next_cursor'])
        self.bulk_live(1)
        for limit in (0, -1, 1001, True, 1.0, '1', None):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.project.insights.list_page(limit=limit)
        with self.assertRaises(Denied):
            self.project.insights.list_page(scope='all')
        self.assertEqual(len(self.project.insights.list_page(limit=1)['items']), 1)

    def test_malformed_cursor_rejected(self):
        self.bulk_live(2)
        valid = self.project.insights.list_page(limit=1)['next_cursor']
        for token in ('', 'broken', valid[:-1] + 'x', 'x' * 4097, {}, 1, '%%%.' + '0' * 64):
            with self.subTest(token=str(token)[:30]), self.assertRaisesRegex(Denied, 'Malformed'):
                self.project.insights.list_page(cursor=token)
        state = json.loads(base64.urlsafe_b64decode(valid.split('.')[0]))
        for key, value in (('v', True), ('scope', 'all'), ('position', -1), ('position', True),
                           ('high_water', 1), ('expires', 'bad'), ('generation', 'bad'), ('extra', 1)):
            body = canonical({**state, key: value})
            token = base64.urlsafe_b64encode(body).decode() + '.' + digest(body)
            with self.subTest(key=key, value=value), self.assertRaisesRegex(Denied, 'Malformed'):
                self.project.insights.list_page(cursor=token)

    def test_config_default_and_bounds(self):
        self.assertEqual(self.config.limits.insight_cursor_ttl_seconds, 86400)
        self.assertEqual(self.project.insights.cursor_ttl_seconds, 86400)
        text = self.file.read_text()
        for ttl in (60, 604800):
            self.file.write_text(text.replace('insight_cursor_ttl_seconds = 86400', f'insight_cursor_ttl_seconds = {ttl}'))
            self.assertEqual(load(self.file).limits.insight_cursor_ttl_seconds, ttl)
        for ttl in ('0', '59', '604801', 'true', '60.0'):
            self.file.write_text(text.replace('insight_cursor_ttl_seconds = 86400', f'insight_cursor_ttl_seconds = {ttl}'))
            with self.subTest(ttl=ttl), self.assertRaises(ConfigError):
                load(self.file)
        self.file.write_text('\n'.join(line for line in text.splitlines() if not line.startswith('insight_cursor_ttl_seconds')))
        self.assertEqual(load(self.file).limits.insight_cursor_ttl_seconds, 86400)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main(['--config', str(self.file), 'insight', *args])
        return result, out.getvalue(), err.getvalue()

    def test_cli_list_bytes_unchanged_explicit_pages_read_and_history(self):
        self.bulk_live(3)
        expected = self.project.insights.list(pending=False, limit=1000)
        status, output, error = self.cli('list', 'sample')
        self.assertEqual((status, error), (0, ''))
        self.assertEqual(output, json.dumps(expected, ensure_ascii=False, indent=2) + '\n')
        self.assertIsInstance(json.loads(output), list)
        # The public maximum is accepted here; the high-volume API regression
        # separately exercises a full 1,000-row page and its continuation.
        status, output, error = self.cli('page', 'sample', '--limit', '1000')
        self.assertEqual((status, error), (0, ''))
        self.assertEqual(len(json.loads(output)['items']), 3)
        status, output, error = self.cli('page', 'sample', '--limit', '2')
        self.assertEqual((status, error), (0, ''))
        first = json.loads(output)
        self.assertTrue(first['has_more'])
        status, output, error = self.cli('page', 'sample', '--cursor', first['next_cursor'], '--limit', '2')
        self.assertEqual((status, error), (0, ''))
        self.assertEqual(json.loads(output)['items'], expected[2:3])
        self.legacy_archive(2)
        status, output, error = self.cli('page', 'sample', '--scope', 'archive', '--limit', '1')
        self.assertEqual((status, error), (0, ''))
        self.assertTrue(json.loads(output)['has_more'])
        for action, expected in (('read', self.project.insights.read(self.record(0)['id'])), ('history', [])):
            status, output, error = self.cli(action, 'sample', '--id', self.record(0)['id'])
            self.assertEqual((status, error), (0, ''))
            self.assertEqual(json.loads(output), expected)
        for args in (('page', 'sample', '--limit', '1001'), ('page', 'sample', '--cursor', 'broken'),
                     ('list', 'sample', '--limit', '1')):
            status, output, error = self.cli(*args)
            self.assertNotEqual(status, 0)
            self.assertEqual(output, '')
            self.assertIn('error', json.loads(error))

    def test_archive_page_is_summary_and_read_keeps_full_body(self):
        body = "Secret archived body marker 7f3a"
        evidence_text = "Secret archived evidence marker 9c1e"
        record = {**self.record(0), "body": body,
                  "updated_at": "2026-01-02T00:00:00+00:00",
                  "evidence": [{"author": "searcher", "run": None,
                                "created_at": "2026-01-02T00:00:00+00:00",
                                "text": evidence_text}]}
        decision = {"action": "reject", "reason": "done", "revisit": "never",
                    "run": "run-1", "created_at": "2026-01-03T00:00:00+00:00", "rev": 1}
        insight_archive.store(self.project.root, record, decision, {"id": "identity"})
        page = self.project.insights.list_page(scope="archive", limit=30)
        self.assertEqual(len(page["items"]), 1)
        item = page["items"][0]
        blob = json.dumps(item, ensure_ascii=False)
        self.assertNotIn(body, blob)
        self.assertNotIn(evidence_text, blob)
        self.assertNotIn("evidence", [k for k in item if k == "evidence"])
        for key in ("id", "rev", "source", "title", "created_at", "decision", "evidence_count"):
            self.assertIn(key, item)
        self.assertEqual(item["evidence_count"], 1)
        self.assertEqual(item["evidence_updated_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(item["updated_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(item["decision"]["action"], "reject")
        full = self.project.insights.read(record["id"])
        self.assertEqual(full["body"], body)
        self.assertEqual(full["evidence"][0]["text"], evidence_text)
