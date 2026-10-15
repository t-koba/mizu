"""Offline continuation, byte-integrity and OCI argv tests, not live isolation."""
import json
import os
import shutil
from unittest.mock import patch
from support import Fixture
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest, read_json, write_json
from mizu.process import Result
from mizu.protocol import DEFINITIONS, validate
from mizu.web import decode_fetch
from test_probe import FakeResponse

RAW = b'<p>caf\xc3\xa9\r\ninvalid:\xff</p><script>hidden</script>\r\nend'
URL = 'https://example.org/document'


class SourceContinuationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.web = self.ctx.web
        self.web.settings = {**self.config.web, 'hosts': ['example.org'],
                             'probe_hosts': ['example.org'], 'probe_methods': ['GET', 'POST'],
                             'feeds': [], 'search_command': ['fake-search'], 'cache_seconds': 0}
        self.requests = []
        requests = self.requests

        class Connection:
            def __init__(self, *args): pass
            def request(self, method, path, body=None, headers=None):
                requests.append((method, path, body))
            def getresponse(self):
                return FakeResponse(200, [('Content-Type', 'text/html')], RAW)
            def close(self): pass

        self.enterContext(patch('mizu.web.PinnedHTTPS', Connection))
        self.enterContext(patch('mizu.web.public_addresses', return_value=['fake-address']))

    def dispatch(self, config, run_dir, operation, value, cancel, role):
        return getattr(self.web, operation)(value)

    def test_fetch_paging_uses_first_raw_response_only(self):
        with patch('mizu.runtime.bounded', side_effect=self.dispatch) as bounded:
            first = self.ctx._op_fetch({'url': URL, 'limit': 3})
            identity = first['receipt_id']
            text, offset = first['text'], first['next_offset']
            self.web.settings['hosts'] = []  # Replay does not recheck destinations.
            while offset is not None:
                result = self.ctx._op_fetch({'receipt_id': identity, 'offset': offset, 'limit': 3})
                text += result['text']
                offset = result['next_offset']
            self.assertEqual(bounded.call_count, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(text, decode_fetch(RAW, 'text/html'))
        self.assertIn('caf\u00e9\r\ninvalid:\ufffd', text)
        self.assertNotIn('hidden', text)
        directory = self.web.receipts / identity
        body = directory / 'payload' / 'body'
        self.assertEqual(body.read_bytes(), RAW)
        record = read_json(directory / 'receipt.json')
        self.assertNotIn('text', record['result'])
        self.assertEqual(record['body'], {'representation': 'raw-response-body',
                          'byte_length': len(RAW), 'sha256': digest(RAW)})
        self.assertEqual(self.web.raw_source(identity)[1],
                         {'receipt_id': identity, **record['body']})

    def test_plain_bytes_decode_same_on_replay(self):
        record = self.web.retain('fetch', {'text': RAW.decode('utf-8', 'replace'),
                                 'content_type': 'text/plain', 'sha256': digest(RAW)}, RAW)
        replay = self.web.replay(record['receipt_id'], 'fetch')
        self.assertEqual(replay, record)
        self.assertIn('\r\n', replay['text'])

    def test_post_probe_continuation_never_repeats_post(self):
        with patch('mizu.runtime.bounded', side_effect=self.dispatch) as bounded:
            first = self.ctx._op_probe({'url': URL, 'method': 'POST', 'body': '{"x":1}', 'limit': 2})
            second = self.ctx._op_probe({'receipt_id': first['receipt_id'], 'offset': 2, 'limit': 7})
            self.assertEqual(second['text'], RAW.decode('utf-8', 'replace')[2:9])
            self.assertEqual(second['status'], 200)
            self.assertEqual(second['headers'], first['headers'])
            self.assertEqual(bounded.call_count, 1)
            new = self.ctx._op_probe({'url': URL, 'method': 'POST'})
            self.assertNotEqual(first['receipt_id'], new['receipt_id'])
        self.assertEqual([r[0] for r in self.requests], ['POST', 'POST'])

    def test_search_continuation_never_reruns_adapter(self):
        rows = [{'title': str(n), 'url': URL + str(n), 'summary': 'summary'} for n in range(4)]
        with patch('mizu.web.run', return_value=Result(0, json.dumps({'results': rows}), '', 'exited', 0)) as adapter, \
             patch('mizu.runtime.bounded', side_effect=self.dispatch) as bounded:
            first = self.ctx._op_search({'query': 'original', 'limit': 2})
            second = self.ctx._op_search({'result_id': first['result_id'], 'offset': 2, 'limit': 2})
            self.assertEqual([r['title'] for r in first['results'] + second['results']], ['0', '1', '2', '3'])
            self.assertEqual(second['result_id'], first['result_id'])
            self.assertEqual(bounded.call_count, 1)
            self.assertEqual(adapter.call_count, 1)
        self.assertEqual(self.requests, [])

    def test_missing_malformed_and_ambiguous_calls_never_retrieve(self):
        for operation, key, initial in (('fetch', 'receipt_id', {'url': URL}),
                ('probe', 'receipt_id', {'url': URL, 'method': 'POST'}),
                ('search', 'result_id', {'query': 'query'})):
            call = getattr(self.ctx, '_op_' + operation)
            for args in ({}, {key: '0' * 64}, {key: '../body'}, {key: ''}, {key: '0' * 64, **initial}):
                with self.subTest(operation=operation, args=args), patch('mizu.runtime.bounded') as bounded:
                    with self.assertRaises(Denied): call(args)
                    bounded.assert_not_called()

    def test_corrupt_and_other_run_receipts_never_retrieve(self):
        records = [('fetch', self.web.fetch(URL)),
                   ('probe', self.web.probe({'url': URL, 'method': 'GET'})),
                   ('search', self.web.retain('search', {'results': []}))]
        other = self.context()
        for kind, first in records:
            key = 'result_id' if kind == 'search' else 'receipt_id'
            identity = first[key]
            name = f'search-{identity}.json' if kind == 'search' else f'{identity}/receipt.json'
            path, target = self.web.receipts / name, other.web.receipts / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)  # Even copied records cannot cross runs.
            with patch('mizu.runtime.bounded') as bounded:
                with self.assertRaises(Denied): getattr(other, '_op_' + kind)({key: identity})
                path.write_bytes(b'{broken')
                with self.assertRaises(Denied): getattr(self.ctx, '_op_' + kind)({key: identity})
                bounded.assert_not_called()

    def test_text_only_cache_hit_is_pageable_but_not_raw_input(self):
        self.web.settings['cache_seconds'] = 1800
        self.web.fetch(URL)
        cached = self.web.fetch(URL)
        self.assertTrue(cached['cached'])
        self.assertFalse(cached['raw_available'])
        identity = cached['receipt_id']
        self.assertFalse((self.web.receipts / identity / 'payload' / 'body').exists())
        self.assertEqual(self.web.replay(identity, 'fetch'), cached)
        with self.assertRaisesRegex(Denied, 'no retained raw body'): self.web.raw_source(identity)
        self.assertEqual(len(self.requests), 1)

    def test_raw_integrity_bounds_and_unsafe_files(self):
        identity = self.web.fetch(URL)['receipt_id']
        body = self.web.receipts / identity / 'payload' / 'body'
        body.chmod(0o644)  # Simulate tampering by the owning host process.
        for damaged in (RAW[:-1], RAW + b'!', b'X' * len(RAW)):
            body.write_bytes(damaged)
            with self.assertRaises(Denied): self.web.raw_source(identity)
            with self.assertRaises(Denied): self.web.replay(identity, 'fetch')
        body.write_bytes(b'x' * (self.web.settings['max_bytes'] + 1))
        with self.assertRaises(LimitExceeded): self.web.raw_source(identity)
        body.unlink()
        with self.assertRaises(Denied): self.web.raw_source(identity)
        external = self.root / 'external'
        external.write_bytes(RAW)
        try: body.symlink_to(external)
        except (OSError, NotImplementedError): return  # Windows may lack this grant.
        with self.assertRaises(Denied): self.web.raw_source(identity)
        body.unlink()
        os.link(external, body)
        with self.assertRaises(Denied): self.web.raw_source(identity)

    def test_receipt_hash_and_kind_are_verified(self):
        identity = self.web.fetch(URL)['receipt_id']
        path = self.web.receipts / identity / 'receipt.json'
        record = read_json(path)
        record['result']['url'] = 'https://changed.example/'
        write_json(path, record)
        with self.assertRaises(Denied): self.web.raw_source(identity)
        probe = self.web.probe({'url': URL, 'method': 'GET'})
        with self.assertRaises(Denied): self.web.raw_source(probe['receipt_id'])

    def test_schema_accepts_continuation_and_source_not_model_paths(self):
        for operation, args in [('fetch', {'receipt_id': 'a' * 64}), ('probe', {'receipt_id': 'a' * 64}),
                ('search', {'result_id': 'a' * 64}), ('experiment', {'script': 'true', 'source_receipt': 'a' * 64})]:
            validate(args, DEFINITIONS[operation][1])
        with self.assertRaises(Denied):
            validate({'script': 'true', 'source_path': '/host/body'}, DEFINITIONS['experiment'][1])

    def test_experiment_mount_is_exact_readonly_file_and_evidence_bound(self):
        identity = self.web.fetch(URL)['receipt_id']
        body = self.web.receipts / identity / 'payload' / 'body'
        payload_dir = body.parent
        with patch('mizu.sandbox.assert_no_checkpoint'), patch('mizu.sandbox.remove_argv', return_value=None), patch('mizu.sandbox.remove_leftover', return_value=''), \
             patch('mizu.sandbox.run', return_value=Result(0, 'measured', '', 'exited', 0)) as launch, \
             patch('mizu.runtime.bounded') as bounded:
            result = self.ctx._op_experiment({'script': 'wc -c /work/input/body', 'source_receipt': identity})
            bounded.assert_not_called()
        self.assertEqual(launch.call_count, 1)
        argv = launch.call_args.args[0]
        mounts = [arg for arg in argv if str(payload_dir) in arg]
        self.assertEqual(len(mounts), 1)
        self.assertIn('/work/input', mounts[0])
        self.assertTrue(mounts[0].endswith(':ro') or mounts[0].endswith(':ro,z') or 'readonly' in mounts[0])
        self.assertIn('--network=' + self.config.sandbox.network, argv)
        self.assertEqual(body.read_bytes(), RAW)
        evidence = read_json(self.ctx.run_dir / result['evidence'])
        self.assertEqual(evidence['source'], {'receipt_id': identity, 'representation': 'raw-response-body',
                                               'byte_length': len(RAW), 'sha256': digest(RAW)})
        self.assertEqual(evidence['source_trust'], 'external-untrusted')
        self.assertNotIn(str(body), canonical(evidence).decode())

    def test_experiment_rejects_invalid_sources_without_launch(self):
        identity = self.web.fetch(URL)['receipt_id']
        other = self.context()
        cached = self.web.retain('fetch', {'text': 'cache only', 'sha256': digest(b'cache only')})
        probe = self.web.probe({'url': URL, 'method': 'GET'})
        with patch('mizu.sandbox.assert_no_checkpoint') as checkpoint, \
             patch('mizu.sandbox.remove_argv', return_value=None), patch('mizu.sandbox.remove_leftover', return_value=''), \
             patch('mizu.sandbox.run') as launch:
            for invalid in ('../body', '0' * 64, cached['receipt_id'], probe['receipt_id']):
                with self.subTest(identity=invalid), self.assertRaises(Denied):
                    self.ctx._op_experiment({'script': 'true', 'source_receipt': invalid})
            with self.assertRaises(Denied): other._op_experiment({'script': 'true', 'source_receipt': identity})
            damaged = self.web.receipts / identity / 'payload' / 'body'
            damaged.chmod(0o644)
            damaged.write_bytes(b'x' * len(RAW))
            with self.assertRaises(Denied): self.ctx._op_experiment({'script': 'true', 'source_receipt': identity})
            launch.assert_not_called()
            checkpoint.assert_not_called()
        self.assertEqual(list((self.ctx.run_dir / 'commands').glob('*.started.json')), [])
