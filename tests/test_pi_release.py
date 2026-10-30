"""Offline host CLI release signals; no live GitHub or npm requests."""
import contextlib
import copy
import io
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory as RealTemporaryDirectory
from unittest.mock import patch

from support import Fixture, ROOT
from mizu.cli import execute, parser
from mizu.errors import Denied
from mizu.fs import digest
from mizu import operator_records as records
from mizu.pi_release import SOURCE, RELEASES, Web, pinned_version
from mizu.protocol import DEFINITIONS
from mizu.config import CAPABILITIES


class PiReleaseTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / 'release-source'
        for adapter in ('pi', 'pi-durable'):
            target = self.source / 'adapters' / adapter
            target.mkdir(parents=True)
            for name in ('package.json', 'package-lock.json'):
                shutil.copyfile(ROOT / 'adapters' / adapter / name, target / name)
        # Configure the host CLI through the actual private configuration.
        text = self.file.read_text().replace('hosts = []', 'hosts = ["api.github.com"]', 1)
        self.file.write_text(text)
        self.root_patch = patch('mizu.cli.ROOT', self.source)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def release(self, tag='v1.4.0'):
        return dict(tag_name=tag, draft=False, prerelease=False,
                    published_at='2026-10-21T22:49:48Z', html_url=RELEASES + tag,
                    body='UNTRUSTED RELEASE PROSE MUST NOT BE COPIED')

    def receipt(self, release):
        return dict(url=SOURCE, final_url=SOURCE, content_type='application/json',
                    trust='external-untrusted', text=json.dumps(release), receipt_id='a' * 64)

    def cli(self, receipt=None):
        with patch.object(Web, 'fetch', return_value=receipt or self.receipt(self.release())) as fetch:
            result = execute(parser().parse_args(['--config', str(self.file),
                'pi-release-check', 'sample', '--role', 'worker']))
            fetch.assert_called_once_with(SOURCE)
            return result

    def pending(self):
        return records.pending(self.project, self.config.roles['worker'])[0]['events']

    def test_newer_notice_and_exact_replay_after_acknowledgement(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*.json')}
        result = self.cli()
        self.assertTrue(result['newer'])
        self.assertEqual(result['baseline'], '1.3.0')
        notice = result['notice']['record']
        self.assertEqual(notice['id'], 'operator/sample/pi-release-' + digest(b'v1.4.0')[:48])
        self.assertEqual(notice['writer'], 'operator.cli')
        self.assertEqual(notice['payload']['addressee'], 'worker')
        for text in ('1.3.0', 'v1.4.0', '2026-10-21T22:49:48Z', SOURCE,
                     'external-untrusted; review only; no package availability or compatibility claim'):
            self.assertIn(text, notice['payload']['body'])
        self.assertNotIn('UNTRUSTED RELEASE PROSE', notice['payload']['body'])
        self.assertEqual(self.cli(), result)
        self.assertEqual(len(self.pending()), 1)
        context = self.context('worker')
        records.bind(context)
        records.acknowledge(context, [context.operator_record_manifest[0].ack_token])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.cli(), result)
        self.assertEqual(self.pending(), [])
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes()
                                  for p in self.source.rglob('*.json')})
        self.assertNotIn('pi-release-check', CAPABILITIES | DEFINITIONS.keys())

    def test_no_newer_and_numeric_semver_order(self):
        for tag, newer in (('v1.3.0', False), ('v1.2.99', False), ('v0.99.99', False),
                           ('v1.10.0', True), ('v2.0.0', True)):
            with self.subTest(tag=tag):
                result = self.cli(self.receipt(self.release(tag)))
                self.assertEqual(result['newer'], newer)
                self.assertEqual('notice' in result, newer)
        self.assertEqual(len(self.pending()), 2)

    def test_invalid_release_metadata_never_writes(self):
        bad = [None, [], {}, 'not an object']
        release = self.release()
        for key in ('tag_name', 'draft', 'prerelease', 'published_at', 'html_url'):
            row = copy.deepcopy(release)
            del row[key]
            bad.append(row)
        for key, value in (('draft', True), ('prerelease', True), ('draft', 0),
                           ('prerelease', None), ('published_at', '2026-02-30T00:00:00Z'),
                           ('published_at', '2026-10-21'), ('html_url', 'https://example.com/')):
            bad.append({**release, key: value})
        for tag in ('1.4.0', 'v1.4.0-rc.1', 'v1.4.0+build', 'v01.4.0', 'v1.4', 'v1.4.0\n', 14):
            bad.append({**release, 'tag_name': tag})
        for row in bad:
            with self.subTest(row=row), self.assertRaises(Denied):
                self.cli(self.receipt(row))
        for text in ('{', '{"tag_name":"v1.4.0","tag_name":"v1.5.0"}',
                     json.dumps(release)[:-1] + ',"extra":NaN}'):
            with self.subTest(text=text), self.assertRaises(Denied):
                self.cli({**self.receipt(release), 'text': text})
        for key, value in (('url', 'https://example.com/'), ('final_url', RELEASES + 'v1.4.0'),
                           ('content_type', 'text/html'), ('trust', 'trusted')):
            with self.subTest(key=key), self.assertRaises(Denied):
                self.cli({**self.receipt(release), key: value})
        self.assertEqual(self.pending(), [])

    def test_inconsistent_or_missing_pins_fail_before_fetch(self):
        cases = [('pi', 'package.json', lambda x: x['dependencies'].update(
                    {'@earendil-works/pi-ai': '1.4.0'})),
                 ('pi-durable', 'package-lock.json', lambda x: x['packages'][
                    'node_modules/@earendil-works/pi-durable'].update(version='1.4.0')),
                 ('pi', 'package-lock.json', lambda x: x['packages'].update({
                    'node_modules/other/node_modules/@earendil-works/pi-ai': {'version': '1.4.0'}})),
                 ('pi', 'package-lock.json', lambda x: x['packages']['']['dependencies'].clear()),
                 ('pi', 'package-lock.json', lambda x: x['packages'].pop(
                    'node_modules/@earendil-works/pi-ai')),
                 ('pi', 'package.json', lambda x: x['dependencies'].pop('@earendil-works/pi-ai'))]
        for adapter, name, mutate in cases:
            path = self.source / 'adapters' / adapter / name
            original = path.read_bytes()
            row = json.loads(original)
            mutate(row)
            path.write_text(json.dumps(row))
            with self.subTest(adapter=adapter, name=name), patch.object(Web, 'fetch') as fetch:
                with self.assertRaises(Denied):
                    execute(parser().parse_args(['--config', str(self.file),
                        'pi-release-check', 'sample', '--role', 'worker']))
                fetch.assert_not_called()
            path.write_bytes(original)
        path.unlink()
        with self.assertRaises(Denied):
            pinned_version(self.source)
        self.assertEqual(self.pending(), [])

    def test_policy_role_allowlist_and_fetch_failure(self):
        original = self.file.read_text()
        for text in (original.replace('hosts = ["api.github.com"]', 'hosts = []', 1),
                     original.replace('[operator_records."operator.notice"]\nenabled = true',
                                      '[operator_records."operator.notice"]\nenabled = false')):
            self.file.write_text(text)
            with patch.object(Web, 'fetch') as fetch, self.assertRaises(Denied):
                execute(parser().parse_args(['--config', str(self.file),
                    'pi-release-check', 'sample', '--role', 'worker']))
            fetch.assert_not_called()
        self.file.write_text(original)
        with patch.object(Web, 'fetch') as fetch, self.assertRaises(Denied):
            execute(parser().parse_args(['--config', str(self.file),
                'pi-release-check', 'sample', '--role', 'unknown']))
        fetch.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['pi-release-check', 'sample'])
        with patch.object(Web, 'fetch', side_effect=Denied('offline failure')), self.assertRaises(Denied):
            execute(parser().parse_args(['--config', str(self.file),
                'pi-release-check', 'sample', '--role', 'worker']))
        self.assertEqual(self.pending(), [])

    def test_conflicting_replay_is_refused_not_duplicated(self):
        first = self.cli()
        changed = self.release()
        changed['published_at'] = '2026-10-22T22:49:48Z'
        with self.assertRaises(Denied):
            self.cli(self.receipt(changed))
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.cli()['notice'], first['notice'])

    def test_configured_web_transport_and_temporary_receipt_offline(self):
        raw = json.dumps(self.release()).encode()
        receipt_dirs = []

        @contextlib.contextmanager
        def tracked_receipts(*args, **kwargs):
            with RealTemporaryDirectory(*args, **kwargs) as name:
                receipt_dirs.append(Path(name))
                yield name

        with patch('mizu.pi_release.tempfile.TemporaryDirectory', new=tracked_receipts), \
                patch('mizu.web.PinnedHTTPS') as connection, patch('mizu.web.public_addresses',
                return_value=['192.0.2.1']):
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.side_effect = lambda key, default=None: {
                'Content-Type': 'application/json', 'Content-Encoding': 'identity'}.get(key, default)
            response.read.return_value = raw
            args = parser().parse_args(['--config', str(self.file),
                'pi-release-check', 'sample', '--role', 'worker'])
            first = execute(args)
            # Same configured cache, same durable notice; no second transport.
            second = execute(args)
            self.assertEqual(first['notice'], second['notice'])
            self.assertEqual(connection.call_count, 1)
            connection.return_value.request.assert_called_once()
            self.assertNotIn('receipt_id', first)
            self.assertFalse((self.project.root / 'pi-release-sources').exists())
            self.assertEqual(len(receipt_dirs), 2)
            self.assertTrue(all(not path.exists() for path in receipt_dirs))
            self.assertEqual(len(self.pending()), 1)
