"""Official Python Agent SDK driver; one admission measures one query."""
import contextlib
import math
import time
import shutil
from pathlib import Path

from .bridge import Bridge
from .drivers import admit_invocation
from .engine_channel import Channel
from .engine_config import effective, connected_servers, mizu_server, session_record, save_session
from .errors import ProtocolError, ModelFailure
from .fs import mkdir, write_json
from .process import environment
from .protocol import tool_definitions

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FIELDS = ('inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens')


def model_delta(current, baseline):
    """Model totals already include subagents; do not sum message/child usage."""
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        raise ProtocolError('Invalid Claude model usage')
    result = []
    for model, usage in current.items():
        if not isinstance(usage, dict):
            raise ProtocolError('Invalid Claude model usage entry')
        item = {'model': model}
        for field in TOKEN_FIELDS:
            number = usage.get(field, 0)
            before = baseline.get(model, {}).get(field, 0)
            if type(number) is not int or type(before) is not int or not 0 <= before <= number <= 2**63-1:
                raise ProtocolError('Claude cumulative usage decreased or is invalid')
            item[field] = number-before
        cost = usage.get('costUSD')
        previous_cost = baseline.get(model, {}).get('costUSD', 0)
        if cost is not None:
            if type(cost) not in (int, float) or type(previous_cost) not in (int, float) or not math.isfinite(cost) or not math.isfinite(previous_cost) or not 0 <= previous_cost <= cost:
                raise ProtocolError('Invalid cumulative model cost estimate')
            item['cost_estimate_usd'] = cost-previous_cost
        result.append(item)
    return result


def terminal_result(event):
    if event.get('terminal_reason') != 'completed' or event.get('is_error') or event.get('subtype') != 'success':
        if isinstance(event.get('terminal_reason'), str) and event.get('terminal_reason') not in ('aborted', 'cancelled', 'interrupted', 'aborted_tools', 'max_turns', 'max_budget_usd'):
            raise ModelFailure('claude', kind=event['terminal_reason'], code=event.get('subtype'),
                               message=event.get('errors') or event.get('subtype', ''))
        raise ProtocolError('Claude query failed: '+str(event.get('terminal_reason'))+' '+str(event.get('errors') or event.get('subtype'))[:4000])


class ClaudeDriver:
    requires_sandbox = True

    def __init__(self, config):
        self.config = config

    def execute(self, context, prompt, *, profile=None):
        profile = profile or context.role.profile
        settings = effective(self.config, context.role, profile)
        path, saved = session_record(context, profile, settings)
        started = time.monotonic()
        context.model_evidence = {'profile': profile, **self.config.model(profile), 'engine': 'claude',
                                  'requests': 0, 'request_unit': 'query', 'usage': [], 'usage_known': False}
        cwd = (path.parent if settings['session'] == 'persistent' and not context.ephemeral else context.run_dir)/'controller'
        mkdir(cwd)
        with Bridge(context.handle, tool_definitions(context.role.capabilities), timeout=self.config.limits.run_seconds,
                    on_close=context.cancel_operations) as bridge:
            servers = connected_servers(context, settings)
            servers['mizu'] = mizu_server(bridge.config_file)
            options = dict(settings['options'])
            plugins = list(options.get('plugins', []))
            for resource in settings['resources']:
                if resource['kind'] == 'plugin':
                    plugins.append({'type': 'local', 'path': resource['path']})
                elif resource['kind'] == 'skill':
                    source = Path(resource['path'])
                    target = cwd/'.claude/skills'/source.stem
                    if target.exists():
                        from .errors import ConfigError
                        raise ConfigError('Duplicate local skill name')
                    mkdir(target.parent)
                    if source.is_dir():
                        shutil.copytree(source, target)
                    else:
                        mkdir(target)
                        shutil.copy2(source, target/'SKILL.md')
                else:
                    from .errors import ConfigError
                    raise ConfigError('Unsupported Claude resource kind')
            if plugins:
                options['plugins'] = plugins

            if options.get('setting_sources'):
                from .errors import ConfigError
                raise ConfigError('Select local resources explicitly; settings discovery is runtime-owned')
            options['setting_sources'] = []
            native_env = dict(options.get('env', {}))
            if 'CLAUDE_CONFIG_DIR' in native_env:
                from .errors import ConfigError
                raise ConfigError('CLAUDE_CONFIG_DIR is owned by engines.claude.directory')
            native_env['CLAUDE_CONFIG_DIR'] = str(self.config.agent_dir('claude'))
            options['env'] = native_env
            options['strict_mcp_config'] = True
            if settings['session'] == 'ephemeral' or context.ephemeral:
                options['extra_args'] = {**options.get('extra_args', {}), 'no-session-persistence': None}
            file = context.run_dir/'claude-effective.json'
            write_json(file, {**settings, 'options': options, 'mcp_servers': servers, 'cwd': str(cwd),
                             'systemPrompt': context.role.policy.read_text(encoding='utf-8'),
                             'resume': saved['id'] if saved else None})
            channel = Channel(context, [*self.config.command('claude'), str(ROOT/'adapters/claude/launcher.py'), str(file)],
                              environment(extra={'MIZU_BRIDGE_CONFIG': str(bridge.config_file)}), cwd, 'claude')
            result = None
            try:
                ready = channel.receive(until=min(context.deadline, time.monotonic()+30))
                if ready.get('type') != 'ready' or not context.hello.is_set():
                    raise ProtocolError('Claude SDK handshake failed: '+str(ready)[:4000])
                admit_invocation(context, unit='query')
                channel.send({'type': 'query', 'prompt': prompt})
                while True:
                    event = channel.receive()
                    if event.get('type') == 'error':
                        if event.get('stage') == 'query':
                            raise ModelFailure('claude', kind=event.get('error_type'), code=event.get('code'),
                                               message=event.get('error', ''), retry_at=event.get('retry_at'))
                        raise ProtocolError(str(event.get('error'))[:4000])
                    if event.get('type') == 'result':
                        result = event
                        current = event.get('model_usage')
                        if current is not None:
                            delta = model_delta(current, saved.get('usage', {}) if saved else {})
                            context.model_evidence.update(usage=delta, usage_known=True, observed_models=list(current),
                                usage_observations=[{'provider': None, 'model': item['model'], 'usage': item} for item in delta],
                                cost_estimate_usd=sum(item['cost_estimate_usd'] for item in delta) if all('cost_estimate_usd' in item for item in delta) else None,
                                cost_scope='Provider-reported model usage only; unreported auxiliary requests are unknown')
                        terminal_result(event)
                        if context.finished is None:
                            raise ProtocolError('Claude query completed without mizu_finish')
                        save_session(path, event['session_id'], current or {})
                        break
            finally:
                with contextlib.suppress(Exception):
                    channel.send({'type': 'interrupt'})
                channel.close()
                context.model_evidence['requests'] = context.request_count
        return {**context.model_evidence, 'session_id': result['session_id'], 'seconds': round(time.monotonic()-started, 3)}
