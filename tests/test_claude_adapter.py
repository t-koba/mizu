"""Exercise the launcher against a fake SDK, not a provider or installed CLI."""
import asyncio
import dataclasses
import importlib.util
import io
import json
import sys
import types
from unittest.mock import Mock, patch

from support import Fixture, ROOT


class ClaudePreSendTests(Fixture):
    def launch(self, ctx, *, order='permission-first', identity='sdk-use-id', name='mcp__fake__search',
               bridge_result=None, seal_before_tool=False):
        spec = importlib.util.spec_from_file_location('synthetic_claude_launcher', ROOT/'adapters/claude/launcher.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        trace = []
        admissions = []
        test = self
        inputs = {'invocation_id': 'model-forged-id', 'tool_use_id': 'model-forged-id'}

        class Allow(types.SimpleNamespace):
            pass

        class Deny(types.SimpleNamespace):
            pass

        class FakeClient:
            def __init__(self, options):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get_mcp_status(self):
                return {'mcpServers': [{'name': 'mizu', 'status': 'connected'}]}

            async def query(self, prompt):
                if seal_before_tool:
                    ctx.handle('finish', {'outcome': 'wait', 'summary': 'sealed'})
                hook = self.options.hooks['PreToolUse'][0].hooks[0]

                async def permission():
                    trace.append('permission')
                    return await self.options.can_use_tool(name, inputs, {})

                async def before():
                    trace.append('pre-tool')
                    return await hook({'tool_name': name, 'tool_input': inputs}, identity, {})

                if order == 'permission-first':
                    permission_result = await permission()
                    hook_result = await before()
                else:
                    hook_result = await before()
                    permission_result = await permission()
                if (isinstance(permission_result, Allow)
                        and hook_result['hookSpecificOutput']['permissionDecision'] == 'allow'):
                    if name != 'mcp__mizu__mizu_finish':
                        test.assertEqual(ctx.tool_count, 1)
                        test.assertEqual(admissions, [{'name': name, 'invocation_id': identity}])
                    trace.append('fake-tool-server')

            async def receive_response(self):
                for message in ():
                    yield message

            async def interrupt(self):
                pass

        sdk = types.SimpleNamespace(ClaudeAgentOptions=types.SimpleNamespace, ClaudeSDKClient=FakeClient,
            HookMatcher=types.SimpleNamespace, PermissionResultAllow=Allow, PermissionResultDeny=Deny,
            ResultMessage=types.SimpleNamespace, AgentDefinition=types.SimpleNamespace)
        cfg = self.root/'synthetic-claude.json'
        cfg.write_text(json.dumps({'engine_tools': list(ctx.role.engine_tools), 'options': {}, 'model': 'synthetic',
                                  'cwd': '.', 'systemPrompt': 'synthetic', 'mcp_servers': {}}))

        def forward(_bridge, operation, args):
            if operation == '_engine_tool':
                trace.append('admission')
                admissions.append(args)
                if bridge_result is not None:
                    return bridge_result
            return ctx.handle(operation, args)

        with patch.dict(sys.modules, {'claude_agent_sdk': sdk}), \
             patch.object(sys, 'argv', ['launcher.py', str(cfg)]), \
             patch.object(sys, 'stdin', io.StringIO('{"type":"query","prompt":"synthetic"}\n')), \
             patch.object(launcher, 'load_bridge', return_value={'tools': [{'name': 'mizu_finish'}]}), \
             patch.object(launcher, 'forward', side_effect=forward), patch.object(launcher, 'emit'):
            asyncio.run(launcher.main())
        return trace, admissions

    def context_with_grant(self):
        ctx = self.context('consult')
        ctx.role = dataclasses.replace(ctx.role, engine_tools=('mcp__fake__search',))
        return ctx

    def test_permission_and_pretool_charge_exactly_once_before_fake_server_in_either_order(self):
        for order in ('permission-first', 'pre-first'):
            with self.subTest(order=order):
                ctx = self.context_with_grant()
                trace, admissions = self.launch(ctx, order=order)
                self.assertEqual(ctx.tool_count, 1)
                self.assertEqual(admissions, [{'name': 'mcp__fake__search', 'invocation_id': 'sdk-use-id'}])
                self.assertLess(trace.index('admission'), trace.index('fake-tool-server'))
                self.assertEqual(trace.count('admission'), 1)

    def test_missing_id_ungranted_or_host_denial_blocks_fake_server(self):
        for failure in ('missing-id', 'ungranted', 'malformed-name', 'bound', 'sealed', 'not-allowed'):
            with self.subTest(failure=failure):
                ctx = self.context_with_grant()
                kwargs = {}
                if failure == 'missing-id':
                    kwargs['identity'] = None
                elif failure == 'ungranted':
                    kwargs['name'] = 'mcp__fake__ungranted'
                elif failure == 'malformed-name':
                    kwargs['name'] = {'tool': 'mcp__fake__search'}
                elif failure == 'bound':
                    ctx.bound_hit = 'tools'
                elif failure == 'sealed':
                    kwargs['seal_before_tool'] = True
                else:
                    kwargs['bridge_result'] = {'allowed': False}
                count = ctx.tool_count
                trace, admissions = self.launch(ctx, **kwargs)
                self.assertNotIn('fake-tool-server', trace)
                self.assertEqual(ctx.tool_count, count + (1 if failure == 'sealed' else 0))
                self.assertEqual(len(admissions), 0 if failure in ('ungranted', 'malformed-name') else 1)

    def test_mizu_tools_remain_on_the_existing_bridge_path(self):
        ctx = self.context_with_grant()
        trace, admissions = self.launch(ctx, name='mcp__mizu__mizu_finish')
        self.assertIn('fake-tool-server', trace)
        self.assertEqual(admissions, [])
        self.assertEqual(ctx.tool_count, 0)


class ClaudePromptTransportTests(Fixture):
    def launcher(self):
        spec = importlib.util.spec_from_file_location('synthetic_claude_launcher', ROOT/'adapters/claude/launcher.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        return launcher

    def test_literal_prompt_and_runtime_options_reach_fake_sdk_unchanged(self):
        launcher = self.launcher()
        captured = {}

        class FakeClient:
            def __init__(self, options):
                captured['options'] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get_mcp_status(self):
                return {'mcpServers': [{'name': 'mizu', 'status': 'connected'}]}

            async def query(self, prompt):
                captured['prompt'] = prompt

            async def receive_response(self):
                for message in ():
                    yield message

            async def interrupt(self):
                pass

        sdk = types.SimpleNamespace(ClaudeAgentOptions=types.SimpleNamespace, ClaudeSDKClient=FakeClient,
            HookMatcher=types.SimpleNamespace, PermissionResultAllow=types.SimpleNamespace,
            PermissionResultDeny=types.SimpleNamespace, ResultMessage=types.SimpleNamespace,
            AgentDefinition=types.SimpleNamespace)
        plugins = [{'type': 'local', 'path': str(self.root/'reviewed-plugin')}]
        servers = {'mizu': {'command': 'synthetic-bridge'}, 'fake': {'command': 'synthetic-server'}}
        cfg = self.root/'synthetic-claude.json'
        cfg.write_text(json.dumps({'engine_tools': ['WebSearch', 'mcp__fake__search'],
            'options': {'plugins': plugins, 'setting_sources': [], 'strict_mcp_config': True},
            'model': 'exact-model', 'cwd': str(self.root), 'systemPrompt': 'system\r\n指示',
            'resume': 'saved-session', 'mcp_servers': servers}))
        # No referenced prompt files are read: these are not live CLI probes.
        for prompt in ('@/synthetic/marker.txt', '/synthetic-command @relative.txt',
                       '/synthetic-command\r\n@/synthetic/marker.txt\r\n日本語 🦊'):
            with self.subTest(prompt=prompt), \
                 patch.dict(sys.modules, {'claude_agent_sdk': sdk}), \
                 patch.object(sys, 'argv', ['launcher.py', str(cfg)]), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps({'type': 'query', 'prompt': prompt})+'\n')), \
                 patch.object(launcher, 'load_bridge', return_value={'tools': [{'name': 'mizu_finish'}]}), \
                 patch.object(launcher, 'forward', return_value={'allowed': True}) as forward, \
                 patch.object(launcher, 'emit'):
                asyncio.run(launcher.main())
                self.assertEqual(captured['prompt'], prompt)
                options = captured['options']
                self.assertIs(options.verbatim_prompts, True)
                self.assertEqual((options.model, options.cwd, options.system_prompt, options.resume),
                                 ('exact-model', str(self.root), 'system\r\n指示', 'saved-session'))
                self.assertEqual(options.mcp_servers, servers)
                self.assertEqual(options.plugins, plugins)
                self.assertEqual(options.tools, ['WebSearch'])
                self.assertEqual(options.allowed_tools, [])
                self.assertEqual(options.permission_mode, 'default')
                self.assertEqual(options.setting_sources, [])
                self.assertIs(options.strict_mcp_config, True)
                self.assertTrue(callable(options.can_use_tool))
                hook = options.hooks['PreToolUse'][0].hooks[0]
                self.assertTrue(callable(hook))
                allowed = asyncio.run(options.can_use_tool('mcp__fake__search', {}, {}))
                self.assertEqual(allowed.updated_input, {})
                admitted = asyncio.run(hook({'tool_name': 'mcp__fake__search'}, 'sdk-use-id', {}))
                self.assertEqual(admitted['hookSpecificOutput']['permissionDecision'], 'allow')
                forward.assert_any_call({'tools': [{'name': 'mizu_finish'}]}, '_engine_tool',
                                        {'name': 'mcp__fake__search', 'invocation_id': 'sdk-use-id'})

    def test_direct_config_cannot_supply_verbatim_prompts_before_client(self):
        launcher = self.launcher()
        cfg = self.root/'synthetic-claude.json'
        # Imports exist but no SDK constructor should run for invalid config.
        sdk = types.SimpleNamespace(**{name: Mock() for name in (
            'ClaudeAgentOptions', 'ClaudeSDKClient', 'HookMatcher', 'PermissionResultAllow',
            'PermissionResultDeny', 'ResultMessage', 'AgentDefinition')})
        for value in (True, False, 'true', None, 1, {}):
            cfg.write_text(json.dumps({'engine_tools': [], 'options': {'verbatim_prompts': value}}))
            with self.subTest(value=value), patch.dict(sys.modules, {'claude_agent_sdk': sdk}), \
                 patch.object(sys, 'argv', ['launcher.py', str(cfg)]), \
                 patch.object(launcher, 'load_bridge', return_value={'tools': []}):
                with self.assertRaisesRegex(ValueError, 'verbatim_prompts is runtime-owned'):
                    asyncio.run(launcher.main())
            sdk.ClaudeAgentOptions.assert_not_called()
            sdk.ClaudeSDKClient.assert_not_called()

    def test_contract_requires_verbatim_dataclass_field(self):
        launcher = self.launcher()
        from mizu.engine_config import adapter_contract
        for supported in (False, True):
            fields = [('verbatim_prompts', bool)] if supported else []
            sdk = types.SimpleNamespace(
                ClaudeAgentOptions=dataclasses.make_dataclass('Options', fields),
                ResultMessage=dataclasses.make_dataclass('Result', [('terminal_reason', str), ('model_usage', dict)]),
                ClaudeSDKClient=types.SimpleNamespace(interrupt=lambda: None, get_mcp_status=lambda: None),
                HookMatcher=None, PermissionResultAllow=None, PermissionResultDeny=None, AgentDefinition=None)
            with self.subTest(supported=supported), patch.dict(sys.modules, {'claude_agent_sdk': sdk}), \
                 patch.object(sys, 'argv', ['launcher.py', '--check-contract']), \
                 patch.object(launcher, 'load_bridge') as bridge, patch.object(launcher, 'emit') as emit:
                if supported:
                    asyncio.run(launcher.main())
                    result = emit.call_args.args[0]
                    self.assertEqual(result['exports'], adapter_contract('claude')['exports'])
                    self.assertEqual(result['inference'], 'not_run')
                else:
                    with self.assertRaises(AssertionError):
                        asyncio.run(launcher.main())
                    emit.assert_not_called()
                bridge.assert_not_called()
