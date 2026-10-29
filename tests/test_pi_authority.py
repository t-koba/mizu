"""Ordinary Pi authority is live; every other compatibility boundary remains."""
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu.config import MAX_ROLE_POLICY_BYTES, MAX_ROLE_POLICY_TOTAL, role_policy_text
from mizu.engine_config import (conversation_identity, conversation_settings, effective,
                                save_session, session_record)
from mizu.errors import ConfigError, Denied
from mizu.fs import canonical, digest, mkdir
from mizu.pi import authority_sections, authority_projection


class PiAuthorityTests(Fixture):
    def test_exact_ordered_sections_and_path_free_projection(self):
        role = self.config.roles['worker']
        sources = (self.root / 'first.md', self.root / 'second.md')
        texts = ['  first λ\r\n\n', '\nsecond\n\n']
        for path, text in zip(sources, texts):
            path.write_bytes(text.encode('utf-8'))
        role = dataclasses.replace(role, policy=sources)
        goal = '  pinned charter\r\n雪\n\n'
        sections = authority_sections(role, goal)
        self.assertEqual(list(sections), ['mizu_authority_policy_0', 'mizu_authority_policy_1',
                                         'mizu_authority_charter'])
        self.assertEqual(list(sections.values()), [*texts, goal])
        self.assertEqual(role_policy_text(role), '\n'.join(t.rstrip('\n') for t in texts) + '\n')
        projection = authority_projection(sections)
        self.assertNotIn(str(self.root), projection)
        for text in [*texts, goal]:
            self.assertIn(text, projection)
        smaller = authority_sections(dataclasses.replace(role, policy=sources[:1]), goal)
        self.assertNotIn('mizu_authority_policy_1', smaller)

    def test_shared_byte_bounds_and_fail_closed_reads(self):
        role = self.config.roles['worker']
        path = self.root / 'policy.md'
        role = dataclasses.replace(role, policy=(path,))
        # Bounds count UTF-8 bytes, not code points; exact boundary is allowed.
        path.write_bytes(('λ' * (MAX_ROLE_POLICY_BYTES // 2)).encode())
        self.assertEqual(len(authority_sections(role, '')['mizu_authority_policy_0'].encode()),
                         MAX_ROLE_POLICY_BYTES)
        for raw in (b'x' * (MAX_ROLE_POLICY_BYTES + 1), b'\xff'):
            path.write_bytes(raw)
            for read in (role_policy_text, lambda r: authority_sections(r, 'charter')):
                with self.assertRaises(ConfigError):
                    read(role)
        path.unlink()
        with self.assertRaises(ConfigError):
            authority_sections(role, 'charter')
        with self.assertRaises(ConfigError):
            authority_sections(dataclasses.replace(role, policy=()), 'charter')
        path.write_bytes(b'x' * MAX_ROLE_POLICY_BYTES)
        count = MAX_ROLE_POLICY_TOTAL // MAX_ROLE_POLICY_BYTES
        self.assertEqual(len(authority_sections(dataclasses.replace(role, policy=(path,) * count), '')), count + 1)
        with self.assertRaises(ConfigError):
            authority_sections(dataclasses.replace(role, policy=(path,) * (count + 1)), 'charter')

    def test_only_ordinary_pi_has_live_authority_projection(self):
        for engine in ('pi', 'codex', 'claude', 'pi-durable'):
            with self.subTest(engine=engine):
                ctx = self.context('consult')
                settings = {**effective(self.config, ctx.role, ctx.role.profile), 'engine': engine}
                original = conversation_identity(ctx, settings)
                for kind in ('goal', 'policy', 'capabilities', 'tools'):
                    with self.subTest(kind=kind):
                        saved_goal, saved_role = ctx.goal_digest, ctx.role
                        saved_policy = ctx.role.policy[0].read_bytes()
                        current = settings
                        if kind == 'goal':
                            ctx.goal_digest = digest(b'changed charter')
                        elif kind == 'policy':
                            ctx.role.policy[0].write_text('changed authority\n')
                        elif kind == 'capabilities':
                            ctx.role = dataclasses.replace(ctx.role, capabilities=('finish',))
                        else:
                            current = {**settings, 'engine_tools': ['new-tool']}
                        changed = conversation_identity(ctx, current)
                        if engine in ('pi', 'pi-durable'):
                            self.assertEqual(original, changed)
                        else:
                            self.assertNotEqual(original, changed)
                        ctx.goal_digest, ctx.role = saved_goal, saved_role
                        ctx.role.policy[0].write_bytes(saved_policy)
                if engine == 'pi':
                    self.assertNotIn('engine_tools', original['settings'])
                    for key in ('policy', 'goal', 'capabilities'):
                        self.assertNotIn(key, original)

    def test_ordinary_pi_preserves_all_other_identity_boundaries(self):
        ctx = self.context('consult')
        settings = effective(self.config, ctx.role, ctx.role.profile)
        original = conversation_identity(ctx, settings)
        for key, value in (('model', 'other'), ('provider', 'other'), ('session', 'persistent'),
                           ('resources', [{'sha256': 'a' * 64}]), ('mcp_servers', {'server': {}}),
                           ('native_content', {'local': 'b' * 64}),
                           ('options', {**settings['options'], 'codemode': True})):
            with self.subTest(key=key):
                self.assertNotEqual(original, conversation_identity(ctx, {**settings, key: value}))
        ctx.role = dataclasses.replace(ctx.role, workspace='write')
        self.assertNotEqual(original, conversation_identity(ctx, settings))
        ctx.role = self.config.roles['consult']
        with patch.object(type(ctx.config), 'command', return_value=['other-command']):
            self.assertNotEqual(original, conversation_identity(ctx, settings))
        for helper, value in (('adapter_digest', 'c' * 64), ('local_settings_digest', {'models.json': 'd' * 64})):
            with patch('mizu.engine_config.' + helper, return_value=value):
                self.assertNotEqual(original, conversation_identity(ctx, settings))

    def test_one_time_rekey_never_scans_or_modifies_old_directory(self):
        ctx = self.context('consult')
        settings = {**effective(self.config, ctx.role, ctx.role.profile), 'session': 'persistent'}
        identity = conversation_identity(ctx, settings)
        # Reconstruct the old content-bound shape, even with today's adapter
        # digest: the new projection alone rekeys. No migration/adoption scan.
        old = {**identity, 'settings': conversation_settings(settings),
               'policy': role_policy_text(ctx.role), 'goal': ctx.goal_digest,
               'capabilities': ctx.role.capabilities}
        root = ctx.run_dir.parents[1] / 'sessions' / ctx.role.name
        old_path = root / digest(canonical(old)) / 'session.json'
        mkdir(old_path.parent)
        save_session(old_path, 'old-history', {'tokens': 17})
        before = old_path.read_bytes()
        with patch('pathlib.Path.iterdir', side_effect=AssertionError('must not scan')), \
                patch('mizu.engine_config.adapter_digest', return_value=identity['adapter_digest']):
            path, saved = session_record(ctx, ctx.role.profile, settings)
        self.assertNotEqual(path, old_path)
        self.assertIsNone(saved)
        save_session(path, 'new-history', {})
        ctx.goal_digest = digest(b'new goal')
        ctx.role = dataclasses.replace(ctx.role, capabilities=('finish',), engine_tools=())
        resumed, saved = session_record(ctx, ctx.role.profile, {**settings, 'engine_tools': []})
        self.assertEqual(path, resumed)
        self.assertEqual(saved['id'], 'new-history')
        self.assertEqual(old_path.read_bytes(), before)

    def test_historical_grants_do_not_admit_current_host_operations(self):
        ctx = self.context('worker')
        ctx.role = dataclasses.replace(ctx.role, capabilities=('finish',), engine_tools=())
        with self.assertRaises(Denied):
            ctx.handle('files', {})
        with self.assertRaises(Denied):
            ctx.handle('_engine_tool', {'name': 'old-tool', 'invocation_id': 'from-history'})
