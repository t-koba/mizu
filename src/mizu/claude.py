"""Official Python Agent SDK driver; one admission measures one query."""
import contextlib
import math
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from .bridge import Bridge
from .drivers import admit_invocation
from .engine_channel import Channel
from .config import system_context_text
from .engine_config import effective, connected_servers, mizu_server, resource_digest, session_record, save_session
from .errors import ConfigError, ProtocolError, ModelFailure
from .fs import mkdir, write_json
from .process import environment
from .protocol import tool_definitions
from .startup import RUNTIME_ENDPOINT, record_startup, startup_bound

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


def _skill_target_name(source: Path) -> str:
    """Runtime-owned skill directory name for one configured source."""
    name = source.stem
    if not name or name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name:
        raise ConfigError('Invalid local skill name')
    return name


def _verify_staged_skill(source: Path, target: Path, expected: str) -> None:
    """Accept only a verified identical stage; never repair staged content.

    A pre-existing staged path is reused only when it holds exactly the
    current reviewed source: a real directory whose tree matches (a
    single-file source is staged as exactly SKILL.md). Symlinks, extra
    entries and changed bytes are refused instead of overwritten, so an
    untrusted path never becomes trusted by repair.
    """
    if target.is_symlink():
        raise ConfigError('Untrusted staged skill content')
    if source.is_symlink():
        raise ConfigError('Resource symlinks are not permitted')
    if resource_digest(source) != expected:
        raise ConfigError('Engine resource digest mismatch')
    if source.is_dir():
        if not target.is_dir():
            raise ConfigError('Staged skill content mismatch')
        try:
            staged = resource_digest(target)
        except ConfigError as exc:
            raise ConfigError('Untrusted staged skill content') from exc
        if staged != expected:
            raise ConfigError('Staged skill content mismatch')
        return
    if not source.is_file():
        raise ConfigError('Resource must be local content')
    if not target.is_dir():
        raise ConfigError('Staged skill content mismatch')
    try:
        entries = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ConfigError('Untrusted staged skill content') from exc
    for entry in entries:
        if entry.is_symlink():
            raise ConfigError('Untrusted staged skill content')
    if len(entries) != 1 or entries[0].name != 'SKILL.md' or not entries[0].is_file():
        raise ConfigError('Staged skill content mismatch')
    if entries[0].read_bytes() != source.read_bytes():
        raise ConfigError('Staged skill content mismatch')


def _skill_stage_directory(path: Path) -> None:
    """Create or reuse a runtime-owned directory, never a symlink or file."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mkdir(path)
        return
    if not stat.S_ISDIR(mode):
        raise ConfigError('Untrusted skill staging directory')


def _stage_skill_source(source: Path, target: Path, expected: str) -> None:
    """Publish one verified skill stage atomically, or reuse an identical one.

    New content is assembled in a temporary sibling and renamed only after it
    verifies identical, so an interrupted copy never leaves a trusted partial
    target behind; a later dispatch retries from the same reviewed source.
    """
    if os.path.lexists(target):
        _verify_staged_skill(source, target, expected)
        return
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=target.parent))
    try:
        if source.is_symlink():
            raise ConfigError('Resource symlinks are not permitted')
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=True, dirs_exist_ok=True)
        elif source.is_file():
            if resource_digest(source) != expected:
                raise ConfigError('Engine resource digest mismatch')
            shutil.copy2(source, staging / 'SKILL.md')
        else:
            raise ConfigError('Resource must be local content')
        # The assembled copy verifies before it becomes the trusted target,
        # so a source that changed mid-copy (a new symlink, for example) is
        # refused while the published path is still untouched.
        _verify_staged_skill(source, staging, expected)
        if os.path.lexists(target):
            # A sequential dispatch published first; reuse it when identical.
            _verify_staged_skill(source, target, expected)
        else:
            os.rename(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_claude_skills(root: Path, skills) -> None:
    """Stage reviewed skill sources under the runtime-owned skills directory.

    Repeated dispatches in one persistent session reuse the verified
    identical stage instead of failing; duplicate configured names, changed
    content and untrusted pre-existing paths are refused before any launch.
    """
    names = [_skill_target_name(Path(item['path'])) for item in skills]
    if len(set(names)) != len(names):
        raise ConfigError('Duplicate local skill name')
    if names:
        for directory in (root, root / '.claude', root / '.claude/skills'):
            _skill_stage_directory(directory)
    for item, name in zip(skills, names):
        _stage_skill_source(Path(item['path']), root/'.claude/skills'/name, item['sha256'])


class ClaudeDriver:
    requires_sandbox = True

    def __init__(self, config):
        self.config = config

    def execute(self, context, prompt, *, profile=None):
        context.begin_driver('claude', 'query')
        profile = profile or context.role.profile
        settings = effective(self.config, context.role, profile)
        path, saved = session_record(context, profile, settings)
        started = time.monotonic()
        context.model_evidence = {'profile': profile, **self.config.model(profile), 'engine': 'claude',
                                  'requests': 0, 'request_unit': 'query', 'usage': [], 'usage_known': False}
        cwd = (path.parent if settings['session'] == 'persistent' and not context.ephemeral else context.run_dir)/'controller'
        _skill_stage_directory(cwd)
        with Bridge(context.handle, tool_definitions(context.driver_capabilities), timeout=self.config.limits.model_request_seconds,
                    on_close=context.cancel_operations) as bridge:
            servers = connected_servers(context, settings)
            servers['mizu'] = mizu_server(bridge.config_file)
            options = dict(settings['options'])
            plugins = list(options.get('plugins', []))
            for resource in settings['resources']:
                if resource['kind'] == 'plugin':
                    plugins.append({'type': 'local', 'path': resource['path']})
                elif resource['kind'] != 'skill':
                    raise ConfigError('Unsupported Claude resource kind')
            _stage_claude_skills(cwd, [item for item in settings['resources'] if item['kind'] == 'skill'])
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
                             'systemPrompt': system_context_text(context.role, context.goal),
                             'resume': saved['id'] if saved else None})
            bound = startup_bound(self.config, 'claude')
            spawned = time.monotonic()
            channel = Channel(context, [*self.config.command('claude'), str(ROOT/'adapters/claude/launcher.py'), str(file)],
                              environment(extra={'MIZU_BRIDGE_CONFIG': str(bridge.config_file)}), cwd, 'claude')
            result = None
            try:
                # The runtime handshake uses this host's per-engine bound,
                # derived from its largest recorded interval. See startup.py
                # for how probe process-exit and runtime first-answer samples
                # contribute distinct measurements to that record.
                ready = channel.receive(until=spawned+bound, bound=bound)
                # Runtime startup is measured through this first protocol
                # response, including refusals. Probe paths measure through
                # process exit; the shared record keeps the larger interval.
                record_startup(self.config, 'claude', time.monotonic() - spawned, RUNTIME_ENDPOINT)
                if ready.get('type') != 'ready' or not context.hello.is_set():
                    raise ProtocolError('Claude SDK handshake failed: '+str(ready)[:4000])
                admit_invocation(context, unit='query')
                channel.send({'type': 'query', 'prompt': prompt})
                while True:
                    event = channel.receive()
                    if event.get('type') == 'error':
                        if event.get('stage') == 'query':
                            context.end_request(0)
                            raise ModelFailure('claude', kind=event.get('error_type'), code=event.get('code'),
                                               message=event.get('error', ''), retry_at=event.get('retry_at'))
                        raise ProtocolError(str(event.get('error'))[:4000])
                    if event.get('type') == 'result':
                        context.end_request(0)
                        result = event
                        current = event.get('model_usage')
                        if current is not None:
                            delta = model_delta(current, saved.get('usage', {}) if saved else {})
                            context.model_evidence.update(usage=delta, usage_known=True, observed_models=list(current),
                                usage_observations=[{'provider': None, 'model': item['model'], 'usage': item} for item in delta],
                                cost_estimate_usd=sum(item['cost_estimate_usd'] for item in delta) if all('cost_estimate_usd' in item for item in delta) else None,
                                cost_scope='Provider-reported model usage only; unreported auxiliary requests are unknown')
                        terminal_result(event)
                        save_session(path, event['session_id'], current or {})
                        # A normal result without a seal is handled centrally by
                        # Engine._execute; retain the session for its follow-up.
                        break
            finally:
                with contextlib.suppress(Exception):
                    channel.send({'type': 'interrupt'})
                channel.close()
                context.model_evidence['requests'] = context.request_count
        if channel.quiescent:
            context.complete_driver_checkpoint('claude', 'query')
        if getattr(context, 'recovery_probe', False):
            context.model_evidence['settled'] = True
        return {**context.model_evidence, 'session_id': result['session_id'], 'seconds': round(time.monotonic()-started, 3)}
