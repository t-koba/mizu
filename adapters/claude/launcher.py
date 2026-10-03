"""Official SDK adapter. Dependencies belong to this interpreter, not Mizu core."""
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from mizu.mcp_proxy import load_bridge, forward


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


async def main():
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow, PermissionResultDeny, ResultMessage, AgentDefinition
    if '--check-contract' in sys.argv:
        assert callable(ClaudeSDKClient.interrupt) and callable(ClaudeSDKClient.get_mcp_status)
        fields = {f.name for f in dataclasses.fields(ResultMessage)}
        assert {'terminal_reason', 'model_usage'} <= fields
        emit({'contract': 'official-python-agent-sdk',
              'exports': ['ClaudeSDKClient.interrupt', 'ClaudeSDKClient.get_mcp_status',
                          'ResultMessage.terminal_reason', 'ResultMessage.model_usage'],
              'inference': 'not_run'})
        return
    cfg = json.loads(Path(sys.argv[1]).read_text())
    bridge = load_bridge()
    mizu_tools = {'mcp__mizu__'+tool['name'] for tool in bridge['tools']}
    grants = set(cfg['engine_tools'])
    forbidden = {'Bash', 'Edit', 'Write', 'NotebookEdit', 'Computer', 'Read', 'Glob', 'Grep'}
    if forbidden & grants:
        raise ValueError('Native host tools bypass the OCI bridge')

    async def gate(name):
        if name in mizu_tools:
            # The actual operation always passes through the bridge, including nested calls.
            return True
        try:
            await asyncio.to_thread(forward, bridge, '_engine_tool', {'name': name})
            return True
        except Exception:
            return False

    async def permission(name, inputs, context):
        if await gate(name):
            return PermissionResultAllow(updated_input=inputs)
        return PermissionResultDeny(message='Tool has no Mizu grant or the run is sealed', interrupt=True)

    async def before_tool(inputs, tool_use_id, context):
        name = inputs.get('tool_name', '')
        if await gate(name):
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow'}}
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': 'Tool has no Mizu grant or run is sealed'}}

    options = dict(cfg['options'])
    # Operator hook declarations remain native settings; the grant hook cannot be replaced.
    if 'hooks' in options:
        raise ValueError('SDK hook callbacks are runtime-owned; configure reviewed native hooks in settings')
    if options.get('continue_conversation') or options.get('fork_session'):
        raise ValueError('Session identity and resume belong to Mizu')
    if 'agents' in options:
        agents = {}
        for name, spec in options['agents'].items():
            child = set(spec.get('tools') or (mizu_tools | grants))
            if not child <= mizu_tools | grants:
                raise ValueError('Subagent tools exceed parent grants')
            agents[name] = AgentDefinition(**{**spec, 'tools': sorted(child)})
        options['agents'] = agents
    tools = sorted(grants - {name for name in grants if name.startswith('mcp__')})
    opts = ClaudeAgentOptions(**options, model=cfg['model'], cwd=cfg['cwd'],
                             system_prompt=cfg['systemPrompt'], resume=cfg.get('resume'),
                             tools=tools, allowed_tools=[], permission_mode='default',
                             mcp_servers=cfg['mcp_servers'], can_use_tool=permission,
                             hooks={'PreToolUse': [HookMatcher(hooks=[before_tool])]})
    async with ClaudeSDKClient(options=opts) as client:
        deadline = time.monotonic()+30
        while True:
            status = await client.get_mcp_status()
            server = next((item for item in status['mcpServers'] if item['name'] == 'mizu'), None)
            if server and server['status'] == 'connected':
                break
            if not server or server['status'] != 'pending' or time.monotonic() >= deadline:
                raise RuntimeError('Required mizu MCP connection failed before query')
            await asyncio.sleep(.05)
        await asyncio.to_thread(forward, bridge, '_hello', {})
        emit({'type': 'ready', 'mcp': 'connected'})
        line = await asyncio.to_thread(sys.stdin.readline)
        record = json.loads(line)
        if record['type'] != 'query':
            raise ValueError('Expected query input')
        await client.query(record['prompt'])

        async def controls():
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    await client.interrupt()
                    return
                if json.loads(line).get('type') == 'interrupt':
                    await client.interrupt()
                    return
        task = asyncio.create_task(controls())
        try:
            async for message in client.receive_response():
                value = dataclasses.asdict(message)
                value['type'] = 'result' if isinstance(message, ResultMessage) else type(message).__name__
                emit(value)
        finally:
            task.cancel()
            await client.interrupt()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        emit({'type': 'error', 'error': str(exc)[:4000]})
        raise SystemExit(1)
