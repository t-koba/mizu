"""Repeatable runtime-owned Claude skill staging; no provider or network."""
import dataclasses
import os
import shutil
import stat
import unittest.mock
from pathlib import Path

from support import Fixture
from mizu.claude import ClaudeDriver
from mizu.engine_config import resource_digest, session_record
from mizu.errors import ConfigError, ProtocolError


class _FakeBridge:
    """Stand in for the MCP bridge; the model peer never runs here."""

    def __init__(self, handle, *args, **kwargs):
        self.handle = handle
        self.config_file = str(Path.cwd() / 'synthetic-bridge.json')

    def __enter__(self):
        self.handle('_hello', {})
        return self

    def __exit__(self, *args):
        return False


class _FakeChannel:
    """Answer the handshake and one successful turn with fixed usage."""

    def __init__(self, *args, **kwargs):
        self.quiescent = True

    def receive(self, until=None, bound=None):
        if not getattr(self, '_ready', False):
            self._ready = True
            return {'type': 'ready'}
        return {'type': 'result', 'terminal_reason': 'completed', 'is_error': False,
                'subtype': 'success', 'session_id': 'session',
                'model_usage': {'synthetic': {'inputTokens': 1, 'outputTokens': 1}}}

    def send(self, message):
        pass

    def close(self):
        pass


class ClaudeSkillStagingTests(Fixture):
    def config_with_skills(self, ctx, sources, session='persistent'):
        profile = ctx.role.profile
        resources = [{'kind': 'skill', 'path': str(item), 'sha256': resource_digest(item)}
                     for item in sources]
        raw = {**self.config.profiles[profile], 'engine': 'claude',
               'options': {}, 'session': session, 'resources': resources}
        config = dataclasses.replace(self.config, profiles={**self.config.profiles, profile: raw})
        ctx.config = config
        return config

    def execute(self, ctx, config, channel=_FakeChannel):
        with unittest.mock.patch('mizu.claude.Bridge', _FakeBridge), \
             unittest.mock.patch('mizu.claude.Channel', channel), \
             unittest.mock.patch('mizu.claude.connected_servers', return_value={}):
            return ClaudeDriver(config).execute(ctx, 'synthetic prompt')

    def session_controller(self, ctx, config):
        settings = config.profiles[ctx.role.profile]
        # Settings carry the reviewed digests; effective() revalidates them.
        from mizu.engine_config import effective
        live = effective(config, ctx.role, ctx.role.profile)
        path, _ = session_record(ctx, ctx.role.profile, live)
        self.assertEqual(settings['session'], 'persistent')
        return path.parent / 'controller'

    def write_file_skill(self, name='reviewed.md'):
        source = self.root / name
        source.write_bytes(b'# reviewed skill\n')
        return source

    def write_tree_skill(self, name='reviewed-skill'):
        source = self.root / name
        source.mkdir()
        (source / 'SKILL.md').write_bytes(b'# reviewed tree skill\n')
        (source / 'helper.txt').write_bytes(b'helper\n')
        return source

    def test_file_skill_stages_as_skill_md(self):
        ctx = self.context('consult')
        source = self.write_file_skill()
        source.chmod(0o640)
        os.utime(source, (1_600_000_000, 1_600_000_000))
        config = self.config_with_skills(ctx, [source])
        result = self.execute(ctx, config)
        self.assertEqual(result['requests'], 1)
        staged = self.session_controller(ctx, config) / '.claude/skills' / source.stem / 'SKILL.md'
        self.assertEqual(staged.read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), stat.S_IMODE(source.stat().st_mode))
        self.assertEqual(staged.stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_directory_skill_stages_tree(self):
        ctx = self.context('consult')
        source = self.write_tree_skill()
        config = self.config_with_skills(ctx, [source])
        self.execute(ctx, config)
        target = self.session_controller(ctx, config) / '.claude/skills' / source.stem
        self.assertEqual(resource_digest(target), resource_digest(source))
        self.assertEqual((target / 'helper.txt').read_bytes(), b'helper\n')

    def test_repeated_persistent_dispatch_reuses_identical_stage(self):
        ctx = self.context('consult')
        source = self.write_file_skill()
        config = self.config_with_skills(ctx, [source])
        first = self.execute(ctx, config)
        staged = self.session_controller(ctx, config) / '.claude/skills' / source.stem / 'SKILL.md'
        before = staged.read_bytes()
        followup = self.context('consult')
        followup.config = config
        second = self.execute(followup, config)
        self.assertEqual((first['requests'], second['requests']), (1, 1))
        self.assertEqual(staged.read_bytes(), before)

    def test_repeated_directory_dispatch_reuses_identical_tree(self):
        ctx = self.context('consult')
        source = self.write_tree_skill()
        config = self.config_with_skills(ctx, [source])
        self.execute(ctx, config)
        followup = self.context('consult')
        followup.config = config
        self.execute(followup, config)
        target = self.session_controller(followup, config) / '.claude/skills' / source.stem
        self.assertEqual(resource_digest(target), resource_digest(source))

    def test_duplicate_configured_skill_names_refused_before_launch(self):
        ctx = self.context('consult')
        first = self.root / 'one' / 'skill.md'
        first.parent.mkdir()
        first.write_bytes(b'# first\n')
        second = self.root / 'two' / 'skill.md'
        second.parent.mkdir()
        second.write_bytes(b'# second\n')
        config = self.config_with_skills(ctx, [first, second])
        channel = unittest.mock.MagicMock()
        with self.assertRaisesRegex(ConfigError, 'Duplicate local skill name'):
            self.execute(ctx, config, channel=channel)
        channel.assert_not_called()
        self.assertEqual(ctx.request_count, 0)
        skills = self.session_controller(ctx, config) / '.claude/skills'
        self.assertFalse(skills.exists())

    def test_changed_staged_content_refused_without_overwrite(self):
        ctx = self.context('consult')
        source = self.write_file_skill()
        config = self.config_with_skills(ctx, [source])
        self.execute(ctx, config)
        staged = self.session_controller(ctx, config) / '.claude/skills' / source.stem / 'SKILL.md'
        staged.write_bytes(b'# tampered by the model side\n')
        followup = self.context('consult')
        followup.config = config
        channel = unittest.mock.MagicMock()
        with unittest.mock.patch('mizu.claude.Bridge', _FakeBridge), \
             unittest.mock.patch('mizu.claude.Channel', channel), \
             unittest.mock.patch('mizu.claude.connected_servers', return_value={}):
            with self.assertRaisesRegex(ConfigError, 'Staged skill content mismatch'):
                ClaudeDriver(config).execute(followup, 'synthetic prompt')
        channel.assert_not_called()
        self.assertEqual(staged.read_bytes(), b'# tampered by the model side\n')

    def test_changed_source_refused_without_overwrite(self):
        # A source that changed without a reviewed digest never reaches
        # staging: the configured digest still pins the reviewed content.
        ctx = self.context('consult')
        source = self.write_file_skill()
        config = self.config_with_skills(ctx, [source])
        self.execute(ctx, config)
        staged = self.session_controller(ctx, config) / '.claude/skills' / source.stem / 'SKILL.md'
        before = staged.read_bytes()
        source.write_bytes(b'# unreviewed changed content\n')
        followup = self.context('consult')
        followup.config = config
        channel = unittest.mock.MagicMock()
        with unittest.mock.patch('mizu.claude.Bridge', _FakeBridge), \
             unittest.mock.patch('mizu.claude.Channel', channel), \
             unittest.mock.patch('mizu.claude.connected_servers', return_value={}):
            with self.assertRaisesRegex(ConfigError, 'Engine resource digest mismatch'):
                ClaudeDriver(config).execute(followup, 'synthetic prompt')
        channel.assert_not_called()
        self.assertEqual(staged.read_bytes(), before)

    def test_symlink_stage_refused_without_repair(self):
        ctx = self.context('consult')
        source = self.write_file_skill()
        config = self.config_with_skills(ctx, [source])
        controller = self.session_controller(ctx, config)
        target = controller / '.claude/skills' / source.stem
        target.parent.mkdir(parents=True)
        outside = self.root / 'outside.md'
        outside.write_bytes(b'# outside staged content\n')
        target.symlink_to(outside)
        channel = unittest.mock.MagicMock()
        with self.assertRaisesRegex(ConfigError, 'Untrusted staged skill content'):
            self.execute(ctx, config, channel=channel)
        channel.assert_not_called()
        self.assertTrue(target.is_symlink())

    def test_symlinked_staging_directories_refused_before_launch(self):
        for component in ('.', '.claude', '.claude/skills'):
            for reuse in (False, True):
                with self.subTest(component=component, reuse=reuse):
                    ctx = self.context('consult')
                    source = self.write_file_skill(name=f'parent-{component.count("/")}-{reuse}.md')
                    config = self.config_with_skills(ctx, [source])
                    controller = self.session_controller(ctx, config)
                    parent = controller / component
                    parent.parent.mkdir(parents=True, exist_ok=True)
                    outside = self.root / 'outside'
                    outside.mkdir()
                    (outside / 'marker').write_bytes(b'untouched outside content')
                    if reuse:
                        relative = {'.': '.claude/skills', '.claude': 'skills',
                                    '.claude/skills': '.'}[component]
                        staged = outside / relative / source.stem
                        staged.mkdir(parents=True)
                        (staged / 'SKILL.md').write_bytes(source.read_bytes())
                    before = {str(item.relative_to(outside)): item.read_bytes() if item.is_file() else None
                              for item in outside.rglob('*')}
                    try:
                        parent.symlink_to(outside, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        self.skipTest('Directory symlinks unavailable')
                    try:
                        channel = unittest.mock.MagicMock()
                        with self.assertRaisesRegex(ConfigError, 'Untrusted skill staging directory'):
                            self.execute(ctx, config, channel=channel)
                        channel.assert_not_called()
                        self.assertEqual(ctx.request_count, 0)
                        self.assertTrue(parent.is_symlink())
                        self.assertEqual({str(item.relative_to(outside)): item.read_bytes() if item.is_file() else None
                                          for item in outside.rglob('*')}, before)
                        self.assertFalse(any(outside.rglob('.staging-*')))
                    finally:
                        parent.unlink()
                        shutil.rmtree(outside)

    def test_non_directory_staging_components_refused_before_launch(self):
        for component in ('.', '.claude', '.claude/skills'):
            with self.subTest(component=component):
                ctx = self.context('consult')
                source = self.write_file_skill(name=f'file-parent-{component.count("/")}.md')
                config = self.config_with_skills(ctx, [source])
                parent = self.session_controller(ctx, config) / component
                parent.parent.mkdir(parents=True, exist_ok=True)
                parent.write_bytes(b'not a directory')
                try:
                    channel = unittest.mock.MagicMock()
                    with self.assertRaisesRegex(ConfigError, 'Untrusted skill staging directory'):
                        self.execute(ctx, config, channel=channel)
                    channel.assert_not_called()
                    self.assertEqual(parent.read_bytes(), b'not a directory')
                finally:
                    parent.unlink()

    def test_preexisting_path_variants_refused_without_overwrite(self):
        variants = ('regular-file', 'extra-entry', 'missing-skill-md')
        for variant in variants:
            with self.subTest(variant=variant):
                ctx = self.context('consult')
                source = self.write_file_skill(name=f'{variant}-reviewed.md')
                config = self.config_with_skills(ctx, [source])
                controller = self.session_controller(ctx, config)
                target = controller / '.claude/skills' / source.stem
                if variant == 'regular-file':
                    target.parent.mkdir(parents=True)
                    target.write_bytes(b'# planted regular file\n')
                    marker = target.read_bytes()
                else:
                    target.mkdir(parents=True)
                    if variant == 'extra-entry':
                        (target / 'SKILL.md').write_bytes(source.read_bytes())
                        (target / 'planted.txt').write_bytes(b'# planted\n')
                    else:
                        (target / 'other.md').write_bytes(b'# wrong name\n')
                    marker = sorted(item.name for item in target.iterdir())
                channel = unittest.mock.MagicMock()
                with self.assertRaisesRegex(ConfigError, 'Staged skill content mismatch'):
                    self.execute(ctx, config, channel=channel)
                channel.assert_not_called()
                if variant == 'regular-file':
                    self.assertEqual(target.read_bytes(), marker)
                else:
                    self.assertEqual(sorted(item.name for item in target.iterdir()), marker)

    def test_interrupted_copy_leaves_no_trusted_target(self):
        ctx = self.context('consult')
        source = self.write_tree_skill()
        config = self.config_with_skills(ctx, [source])
        controller = self.session_controller(ctx, config)
        target = controller / '.claude/skills' / source.stem
        with unittest.mock.patch('mizu.claude.shutil.copytree',
                                 side_effect=OSError('synthetic copy interruption')):
            with self.assertRaises(OSError):
                self.execute(ctx, config)
        self.assertFalse(os.path.lexists(target))
        leftovers = [item for item in target.parent.iterdir()] if target.parent.exists() else []
        self.assertEqual(leftovers, [])

    def test_retry_after_launch_failure_reuses_verified_stage(self):
        ctx = self.context('consult')
        source = self.write_file_skill()
        config = self.config_with_skills(ctx, [source])
        failing = unittest.mock.MagicMock(side_effect=ProtocolError('synthetic launch failure'))
        with self.assertRaises(ProtocolError):
            self.execute(ctx, config, channel=failing)
        staged = self.session_controller(ctx, config) / '.claude/skills' / source.stem / 'SKILL.md'
        self.assertEqual(staged.read_bytes(), source.read_bytes())
        followup = self.context('consult')
        followup.config = config
        result = self.execute(followup, config)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(staged.read_bytes(), source.read_bytes())
