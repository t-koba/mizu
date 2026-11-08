"""Exported editor search policy, fail-closed settings and MCP-sized results."""
import dataclasses
import io
import json
import os
import types
import unittest
from unittest.mock import patch

from support import Fixture
from mizu.cli import _cmd_editor_export
from mizu.config import (Editor, MAX_EDITOR_SEARCH_MATCH_CHARS, TOML_INTEGER_MAX, load)
from mizu.editor import (Capsule, EDITOR_SEARCH_RESPONSE_BYTES, EDITOR_SEARCH_SETTINGS_BYTES,
                         export, serve)
from mizu.errors import Busy, ConfigError, Denied, LimitExceeded
from mizu.fs import MAX_FRAME, PREVIEW_BYTES, READ_PAGE_CHARS, canonical, digest, lock, read_json, write_json


class EditorSearchTests(Fixture):
    def test_config_defaults_overrides_and_invalid_values(self):
        defaults = {"search_max_bytes": 8 * 1024 * 1024,
                    "search_max_matches": 50, "search_match_chars": 1000}
        self.assertEqual(dataclasses.asdict(self.config.editor), defaults)
        self.assertEqual(dataclasses.asdict(Editor()), defaults)
        self.assertEqual(MAX_EDITOR_SEARCH_MATCH_CHARS, READ_PAGE_CHARS)
        original = self.file.read_text()
        for name in defaults:
            maximum = MAX_EDITOR_SEARCH_MATCH_CHARS if name == "search_match_chars" else TOML_INTEGER_MAX
            for value in (1, defaults[name], maximum):
                with self.subTest(name=name, value=value):
                    self.file.write_text(original + f"\n[editor]\n{name} = {value}\n")
                    self.assertEqual(getattr(load(self.file).editor, name), value)
            for value in (0, -1, maximum + 1, 'true', '1.5', '"1"', '[]', '{}'):
                with self.subTest(name=name, invalid=value):
                    self.file.write_text(original + f"\n[editor]\n{name} = {value}\n")
                    with self.assertRaises(ConfigError):
                        load(self.file)
        self.file.write_text(original + '\n[editor]\nunknown = 1\n')
        with self.assertRaisesRegex(ConfigError, 'editor'):
            load(self.file)
        for value in ('1', '[]'):
            self.file.write_text(f'editor = {value}\n' + original)
            with self.assertRaisesRegex(ConfigError, 'editor'):
                load(self.file)

    def test_cli_export_round_trip_legacy_and_single_writer(self):
        policy = Editor(search_max_bytes=9, search_max_matches=2, search_match_chars=3)
        cfg = dataclasses.replace(self.config, editor=policy)
        bundle = self.root / 'bundle'
        args = types.SimpleNamespace(destination=bundle)
        with lock(self.project.root / 'locks/publication.lock', blocking=False):
            with self.assertRaises(Busy):
                _cmd_editor_export(cfg, self.project, args)
        self.assertFalse(bundle.exists())
        _cmd_editor_export(cfg, self.project, args)
        self.assertEqual(read_json(bundle / 'editor-search.json'), dataclasses.asdict(policy))
        capsule = Capsule(bundle, self.root / 'outbox')
        self.assertEqual(capsule.editor, policy)
        self.assertEqual(capsule.call('search_files', {'query': 'VALUE'}), {
            'matches': [], 'truncated': True, 'skipped': ['app.py']})
        # The existing export remains on its own settings, not today's config.
        cfg = dataclasses.replace(cfg, editor=Editor())
        with self.assertRaises(Denied):
            _cmd_editor_export(cfg, self.project, args)
        self.assertEqual(Capsule(bundle, self.root / 'outbox').editor, policy)
        (bundle / 'editor-search.json').unlink()
        self.assertEqual(Capsule(bundle, self.root / 'outbox').editor, Editor())
        self.assertEqual(capsule.editor, policy)
        failed = self.root / 'failed'
        with patch.object(self.project.snapshots, 'materialize', side_effect=Denied('fixture failure')):
            with self.assertRaises(Denied):
                export(self.project, failed, policy)
        self.assertFalse(failed.exists())
        self.assertEqual(list(self.root.glob('.export-*')), [])

    def test_present_settings_fail_closed(self):
        bundle = self.root / 'bundle'
        export(self.project, bundle)
        settings = bundle / 'editor-search.json'
        defaults = dataclasses.asdict(Editor())
        invalid = [b'{broken', b'[]', b'{}', b'null', b'\xff',
                   canonical({'search_max_bytes': 1, 'search_max_matches': 1}),
                   b'{"search_max_bytes":NaN,"search_max_matches":1,"search_match_chars":1}',
                   canonical({**defaults, 'extra': 1}),
                   b'{"search_max_bytes":1,"search_max_bytes":2,"search_max_matches":1,"search_match_chars":1}',
                   b' ' * (EDITOR_SEARCH_SETTINGS_BYTES + 1)]
        for name in defaults:
            maximum = MAX_EDITOR_SEARCH_MATCH_CHARS if name == 'search_match_chars' else TOML_INTEGER_MAX
            invalid.extend(canonical({**defaults, name: value})
                           for value in (True, 0, -1, 1.5, '1', maximum + 1))
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                settings.write_bytes(raw)
                with self.assertRaises((Denied, LimitExceeded)):
                    Capsule(bundle, self.root / 'outbox')
        settings.unlink()
        settings.mkdir()
        with self.assertRaises(Denied):
            Capsule(bundle, self.root / 'outbox')
        settings.rmdir()
        target = self.root / 'settings.json'
        target.write_bytes(canonical(defaults))
        # Hardlinks are not valid immutable-bundle settings either.
        os.link(target, settings)
        with self.assertRaises(Denied):
            Capsule(bundle, self.root / 'outbox')
        settings.unlink()
        try:
            settings.symlink_to(target)
        except OSError:
            pass  # Windows may not grant symlink creation; hardlink refusal ran above.
        else:
            with self.assertRaises(Denied):
                Capsule(bundle, self.root / 'outbox')
            settings.unlink()
            settings.symlink_to(self.root / 'missing')
            with self.assertRaises(Denied):
                Capsule(bundle, self.root / 'outbox')

    def capsule(self, files, policy):
        """Small byte-exact bundle, avoiding full Engine runs for search data."""
        bundle = self.root / 'search-bundle'
        code = bundle / 'code'
        code.mkdir(parents=True)
        entries = {}
        for name, value in files.items():
            entries[name] = {'bytes': len(value), 'sha256': digest(value)}
            (code / name).write_bytes(value)
        snapshot = {'files': entries}
        snapshot['id'] = digest(canonical(snapshot))
        write_json(bundle / 'snapshot.json', snapshot)
        write_json(bundle / 'editor-search.json', dataclasses.asdict(policy))
        return Capsule(bundle, self.root / 'outbox')

    def test_policy_controls_scan_matches_excerpt_and_query_is_unchanged(self):
        capsule = self.capsule({'a': b'needle-long\nneedle-two\n', 'b': b'needle\n',
                                'c': b'needle\n' + b'x' * PREVIEW_BYTES},
                               Editor(search_max_bytes=23, search_max_matches=1, search_match_chars=4))
        self.assertEqual(capsule.call('search_files', {'query': 'needle'}), {
            'matches': [{'path': 'a', 'line': 1, 'text': 'need'}], 'truncated': True, 'skipped': []})
        capsule.editor = dataclasses.replace(capsule.editor, search_max_matches=10)
        result = capsule.call('search_files', {'query': 'needle'})
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual(result['skipped'], ['b', 'c'])
        self.assertTrue(result['truncated'])
        capsule.editor = dataclasses.replace(capsule.editor, search_max_bytes=TOML_INTEGER_MAX)
        result = capsule.call('search_files', {'query': 'needle'})
        self.assertEqual(len(result['matches']), 3)
        self.assertEqual(result['skipped'], ['c'])
        capsule.snapshot['files'].pop('c')
        self.assertFalse(capsule.call('search_files', {'query': 'absent'})['truncated'])
        for query in ('', '  ', 'x' * 1001):
            with self.assertRaises(Denied):
                capsule.call('search_files', {'query': query})
        self.assertEqual(capsule.call('search_files', {'query': 'x' * 1000})['matches'], [])

    def test_full_skipped_diagnostic_reserve_does_not_hide_later_matches(self):
        files = {f'a-skip-{index:04}-' + 'x' * 1024: {'bytes': PREVIEW_BYTES + 1}
                 for index in range(100)}
        files['z-match'] = {'bytes': 7}
        capsule = self.capsule({'a-skip-0000': b'x'}, Editor())
        capsule.snapshot['files'] = files
        with patch.object(capsule, 'read', return_value='needle'):
            result = capsule.call('search_files', {'query': 'needle'})
        self.assertEqual(result['matches'], [{'path': 'z-match', 'line': 1, 'text': 'needle'}])
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['skipped']), len(files) - 1)

    def test_response_budget_includes_matches_skipped_and_real_mcp_envelope(self):
        policy = Editor(TOML_INTEGER_MAX, TOML_INTEGER_MAX, MAX_EDITOR_SEARCH_MATCH_CHARS)
        self.assertEqual(EDITOR_SEARCH_RESPONSE_BYTES, MAX_FRAME // 4)
        capsule = self.capsule({'a': b'needle\n'}, policy)
        # Control-heavy and multibyte payloads exercise JSON escaping; metadata
        # names are synthetic (no host path-length or filename assumptions).
        for mode in ('matches', 'skipped', 'mixed'):
            with self.subTest(mode=mode):
                names = [f'{i:04}-' + '\x00"\\雪' * 60 for i in range(1500)]
                capsule.snapshot['files'] = {
                    name: {'bytes': PREVIEW_BYTES + 1 if mode == 'skipped' or (mode == 'mixed' and i % 2 == 0) else 1}
                    for i, name in enumerate(names)}
                text = 'needle' + '\x00"\\雪' * 2046
                with patch.object(capsule, 'read', return_value=text) as read:
                    result = capsule.call('search_files', {'query': 'needle'})
                self.assertTrue(result['truncated'])
                self.assertLessEqual(len(canonical(result)), EDITOR_SEARCH_RESPONSE_BYTES)
                rows = result['skipped'] if mode == 'skipped' else result['matches']
                self.assertGreater(len(rows), 0)
                self.assertLess(len(rows), len(names))
                if mode == 'mixed':
                    self.assertTrue(result['skipped'])
                self.assertLessEqual(read.call_count, len(result['matches']) + 1)
                # Exercise the actual duplicated text + structured MCP response,
                # with the maximum string request ID, not just a payload estimate.
                messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                            {'jsonrpc': '2.0', 'id': '\x00' * 256, 'method': 'tools/call',
                             'params': {'name': 'search_files', 'arguments': {'query': 'needle'}}},
                            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}]
                sink = io.BytesIO()
                with patch('mizu.editor.Capsule', return_value=capsule), patch.object(capsule, 'read', return_value=text):
                    serve(capsule.bundle, capsule.outbox, input_stream=io.BytesIO(b''.join(map(canonical, messages))),
                          output_stream=sink)
                frames = sink.getvalue().splitlines(keepends=True)
                self.assertTrue(all(len(frame) < MAX_FRAME for frame in frames))
                response = json.loads(frames[1])['result']
                self.assertFalse(response['isError'])
                self.assertEqual(response['structuredContent'], result)
                tool = next(t for t in json.loads(frames[2])['result']['tools'] if t['name'] == 'search_files')
                for setting in dataclasses.asdict(policy).values():
                    self.assertIn(str(setting), tool['description'])
                self.assertEqual(tool['inputSchema']['properties']['query']['maxLength'], 1000)


if __name__ == '__main__':
    unittest.main()
