"""Exercise the launcher against a fake SDK, not a provider or installed CLI."""
import asyncio
import dataclasses
import importlib.util
import io
import json
import sys
import types
from unittest.mock import patch

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
