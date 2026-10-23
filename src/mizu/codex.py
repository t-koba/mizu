"""Managed Codex app-server stdio adapter. One admission measures one turn."""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from .bridge import Bridge
from .drivers import admit_invocation
from .engine_channel import Channel
from .config import system_context_text
from .engine_config import effective, connected_servers, mizu_server, session_record, save_session
from .errors import ModelFailure, ConfigError, ProtocolError, Denied
from .fs import mkdir, write_json
from .process import environment
from .pi import credentials
from .protocol import tool_definitions
from .startup import RUNTIME_ENDPOINT, record_startup, startup_bound


def auth_source():
    """Selected operator auth locator only; never read credential content."""
    return Path(os.environ.get('CODEX_AUTH_FILE', str(Path.home()/'.codex/auth.json'))).expanduser()


class TokenUsage:
    """Monotone cumulative observations; repeated/reordered notifications add nothing."""
    FIELDS = ('inputTokens', 'outputTokens', 'cachedInputTokens', 'reasoningOutputTokens', 'totalTokens')

    def __init__(self, baseline=None):
        self.baseline = self.validate(baseline or {})
        self.total = dict(self.baseline)
        self.known = False

    @classmethod
    def validate(cls, value):
        if not isinstance(value, dict):
            raise ProtocolError('Invalid cumulative token usage')
        result = {}
        for key in cls.FIELDS:
            number = value.get(key, 0)
            if type(number) is not int or not 0 <= number <= 2**63-1:
                raise ProtocolError('Invalid cumulative token count')
            result[key] = number
        return result

    def observe(self, value):
        value = self.validate(value)
        self.total = {key: max(self.total[key], value[key]) for key in self.FIELDS}
        self.known = True

    def delta(self):
        return {key: self.total[key]-self.baseline[key] for key in self.FIELDS}


class CodexDriver:
    requires_sandbox = True

    def __init__(self, config):
        self.config = config

    def execute(self, context, prompt, *, profile=None):
        profile = profile or context.role.profile
        settings = effective(self.config, context.role, profile)
        if context.role.engine_tools:
            # item/started is post-dispatch, not an admission boundary. Do not
            # even launch app-server (or its extra MCP servers) under this grant.
            raise ConfigError('Codex engine_tools require an unavailable pre-send adapter')
        path, saved = session_record(context, profile, settings)
        usage = TokenUsage(saved.get('usage') if saved else None)
        identifier = turn = None
        response = {}
        sequence = 0
        postponed = []
        started = time.monotonic()
        context.model_evidence = {'profile': profile, 'engine': 'codex', **self.config.model(profile),
                                  'request_unit': 'turn', 'requests': 0, 'usage': [], 'usage_known': False}
        home = path.parent / 'home'
        mkdir(home)
        cwd = context.run_dir / 'controller'
        mkdir(cwd)
        # Authentication is operator-owned; this pointer never enters model evidence.
        source = auth_source()
        link = home/'auth.json'
        if source.is_file() and not link.exists():
            os.symlink(source, link)
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.model_request_seconds,
                    on_close=context.cancel_operations) as bridge:
            options = dict(settings['options'])
            turn_options = options.pop('turn', {})
            if not isinstance(turn_options, dict) or set(turn_options) & {'threadId', 'input', 'model', 'cwd', 'sandboxPolicy', 'approvalPolicy', 'approvalsReviewer'}:
                raise ConfigError('Codex turn options conflict with runtime-owned fields')
            skills = dict(options.get('skills', {}))
            configured_skills = list(skills.get('config', []))
            for resource in settings['resources']:
                if resource['kind'] != 'skill':
                    raise ConfigError('Codex resources must be local skills; use native options for other local configuration')
                configured_skills.append({'path': resource['path'], 'enabled': True})
            if configured_skills:
                skills['config'] = configured_skills
                options['skills'] = skills
            features = dict(options.get('features', {}))
            for name in ('shell_tool', 'unified_exec'):
                if features.get(name):
                    raise ConfigError('Host command tools cannot be granted; use mizu_exec through OCI')
                features[name] = False
            if features.get('multi_agent') and 'spawn_agent' not in context.role.engine_tools:
                raise ConfigError('Native subagents require a spawn_agent engine_tools grant')
            options['features'] = features
            servers = connected_servers(context, settings)
            for name, server in servers.items():
                prefix = 'mcp__'+name+'__'
                allowed = [tool[len(prefix):] for tool in context.role.engine_tools if tool.startswith(prefix)]
                if 'enabled_tools' in server and set(server['enabled_tools']) - set(allowed):
                    raise ConfigError('MCP enabled_tools exceeds role grants')
                server['enabled_tools'] = allowed
            servers['mizu'] = {**mizu_server(bridge.config_file), 'required': True,
                               'enabled_tools': ['mizu_'+name for name in context.role.capabilities],
                               'default_tools_approval_mode': 'approve'}
            options['mcp_servers'] = servers
            options.setdefault('approval_policy', 'on-request')
            options.setdefault('sandbox_mode', 'read-only')
            if options['sandbox_mode'] != 'read-only':
                raise ConfigError('Native workspace writes bypass publication; use mizu operations')
            if options.get('web_search', 'disabled') != 'disabled' and 'web_search' not in context.role.engine_tools:
                raise ConfigError('web_search requires an engine_tools grant')
            options.setdefault('web_search', 'disabled')
            write_json(context.run_dir/'codex-effective.json', options)
            native_env = {'CODEX_HOME': str(home)}
            provider_config = options.get('model_providers', {}).get(settings['provider'], {})
            env_names = [provider_config['env_key']] if provider_config.get('env_key') else []
            env_names.extend(provider_config.get('env_http_headers', {}).values())
            credential_values = credentials(self.config.file.parent/'credentials.env') if env_names else {}
            for name in env_names:
                if not isinstance(name, str) or name in {'HOME', 'PATH', 'NODE_OPTIONS', 'PYTHONPATH', 'LD_PRELOAD'}:
                    raise ConfigError('Invalid native provider credential variable')
                value = os.environ.get(name) or credential_values.get(name)
                if not value:
                    raise ConfigError('Configured provider environment variable is unset: '+name)
                native_env[name] = value
            # The initial initialize response uses this host's per-engine
            # bound. Probe process-exit and runtime first-response samples are
            # distinct intervals; startup.py keeps the larger observed value.
            startup_bound_seconds = startup_bound(self.config, 'codex')
            spawned = time.monotonic()
            channel = Channel(context, [*self.config.command('codex'), 'app-server', '--listen', 'stdio://'],
                              environment(extra=native_env), cwd, 'codex')

            def server_request(event):
                method, request_id = event.get('method'), event['id']
                # Commands/file mutation never execute on the controller host.
                if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    channel.send({'id': request_id, 'result': {'decision': 'decline'}})
                elif method == 'item/permissions/requestApproval':
                    channel.send({'id': request_id, 'result': {'permissions': {}, 'scope': 'turn'}})
                elif method == 'mcpServer/elicitation/request':
                    channel.send({'id': request_id, 'result': {'action': 'decline', 'content': None}})
                    raise ProtocolError('Unattended MCP elicitation requires operator input')
                else:
                    channel.send({'id': request_id, 'error': {'code': -32601, 'message': 'Unattended request cannot be answered'}})
                    raise ProtocolError('Unattended engine request cannot be answered: '+str(method))

            def request(method, params):
                nonlocal sequence
                sequence += 1
                wanted = sequence
                channel.send({'id': wanted, 'method': method, 'params': params})
                # The host-relative bound applies to process startup only;
                # later app-server RPCs keep their existing 30 s response cap.
                request_bound = startup_bound_seconds if method == 'initialize' else 30
                handshake_deadline = (spawned+request_bound if method == 'initialize'
                                      else time.monotonic()+request_bound)
                while True:
                    event = channel.receive(until=handshake_deadline,
                                            bound=startup_bound_seconds if method == 'initialize' else None)
                    if 'method' in event and 'id' in event:
                        server_request(event)
                    elif event.get('id') == wanted:
                        if method == 'initialize':
                            # Runtime startup is measured through this first
                            # initialize response, including refusals. Probe
                            # paths measure through process exit; the shared
                            # record keeps the larger interval.
                            record_startup(self.config, 'codex', time.monotonic() - spawned, RUNTIME_ENDPOINT)
                        if 'error' in event:
                            error = event['error']
                            if method == 'turn/start' and isinstance(error, dict):
                                raise ModelFailure('codex', kind='turn/start', code=error.get('code'),
                                                   message=error.get('message', ''), retry_at=error.get('retry_at'))
                            raise ProtocolError('Codex '+method+' failed: '+str(error)[:4000])
                        return event.get('result', {})
                    else:
                        postponed.append(event)
                        if len(postponed) > 128:
                            raise ProtocolError('Too many notifications during handshake')

            try:
                request('initialize', {'clientInfo': {'name': 'mizu', 'version': '0.1.0'},
                                       'capabilities': {'experimentalApi': bool(options.pop('experimentalApi', False))}})
                channel.send({'method': 'initialized'})
                params = {'model': settings['model'], 'modelProvider': settings['provider'], 'cwd': str(cwd),
                          'baseInstructions': system_context_text(context.role, context.goal), 'config': options}
                if saved:
                    params['threadId'] = saved['id']
                    params['excludeTurns'] = True
                    response = request('thread/resume', params)
                else:
                    params['ephemeral'] = settings['session'] == 'ephemeral' or context.ephemeral
                    response = request('thread/start', params)
                identifier = response['thread']['id']
                observed_model = response.get('model')
                context.model_evidence.update(observed_model=observed_model, observed_reasoning_effort=response.get('reasoningEffort'))
                if response.get('model') != settings['model'] or response.get('modelProvider') != settings['provider']:
                    raise ProtocolError('Codex selected an unexpected model/provider')
                statuses = request('mcpServerStatus/list', {'threadId': identifier, 'serverName': 'mizu', 'detail': 'toolsAndAuthOnly'})
                mizu = next((item for item in statuses.get('data', []) if item.get('name') == 'mizu'), None)
                tools = mizu.get('tools', {}) if mizu else {}
                if not mizu or not all('mizu_'+cap in tools for cap in context.role.capabilities):
                    raise ProtocolError('Required mizu MCP tools are not connected')
                context.handle('_hello', {})
                # Resume notifications establish the pre-turn baseline, never count restored totals.
                for event in postponed:
                    if event.get('method') == 'thread/tokenUsage/updated' and event.get('params', {}).get('threadId') == identifier:
                        total = event['params']['tokenUsage']['total']
                        usage = TokenUsage(total)
                postponed.clear()
                admit_invocation(context, unit='turn')
                response = request('turn/start', {**turn_options, 'threadId': identifier,
                                   'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]})
                turn = response['turn']['id']
                while True:
                    event = postponed.pop(0) if postponed else channel.receive()
                    if 'method' in event and 'id' in event:
                        server_request(event)
                        continue
                    params = event.get('params', {})
                    if params.get('threadId') != identifier:
                        continue
                    if event.get('method') == 'item/started':
                        item = params.get('item', {})
                        if (item.get('type') in ('webSearch', 'collabAgentToolCall',
                                                 'commandExecution', 'fileChange', 'dynamicToolCall')
                                or (item.get('type') == 'mcpToolCall' and item.get('server') != 'mizu')):
                            # Evidence of a protocol/capability violation, never
                            # successful admission or a retroactive tool count.
                            raise Denied('Codex extra tool dispatched without a pre-send boundary')
                    if event.get('method') == 'thread/tokenUsage/updated':
                        usage.observe(params['tokenUsage']['total'])
                    if event.get('method') == 'turn/completed' and params.get('turn', {}).get('id') == turn:
                        context.end_request(0)
                        terminal = params['turn']
                        if terminal.get('status') == 'failed' and isinstance(terminal.get('error'), dict):
                            failure = terminal['error']
                            raise ModelFailure('codex', kind='failed',
                                               details={'codex_error_info': failure.get('codexErrorInfo')},
                                               code=failure.get('code'), message=failure.get('message', ''),
                                               retry_at=failure.get('retry_at'))
                        if terminal.get('status') != 'completed':
                            raise ProtocolError('Codex turn '+str(terminal.get('status'))+': '+str(terminal.get('error'))[:4000])
                        # Completed turns without a seal are handed to the
                        # central bounded follow-up policy. Persist this thread
                        # first so the follow-up can continue its context.
                        break
                save_session(path, identifier, usage.total)
            finally:
                if turn and identifier:
                    with contextlib.suppress(Exception):
                        channel.send({'id': 'cleanup', 'method': 'turn/interrupt', 'params': {'threadId': identifier, 'turnId': turn}})
                delta = usage.delta()
                context.model_evidence.update(requests=context.request_count, usage=[{
                    'input_tokens': delta['inputTokens'], 'output_tokens': delta['outputTokens'],
                    'cached_input_tokens': delta['cachedInputTokens'], 'reasoning_output_tokens': delta['reasoningOutputTokens']}]
                    if usage.known else [], usage_known=usage.known)
                context.model_evidence['usage_observations'] = [
                    {'provider': None, 'model': None, 'usage': value}
                    for value in context.model_evidence['usage']]
                try:
                    channel.close()
                finally:
                    with contextlib.suppress(OSError):
                        link.unlink()
        if getattr(context, 'recovery_probe', False):
            context.model_evidence['settled'] = True
        return {**context.model_evidence, 'conversation_id': identifier, 'seconds': round(time.monotonic()-started, 3)}
