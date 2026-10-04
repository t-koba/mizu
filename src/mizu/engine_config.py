"""Current engine configuration and content-bound session identity."""
from pathlib import Path

from .errors import ConfigError
from .config import role_policy_text
from .fs import canonical, digest, mkdir, read_json, write_json


def resource_digest(path):
    """Bounded reviewed local content; tree hashes include names and executable bits."""
    if path.is_symlink():
        raise ConfigError('Resource symlinks are not permitted')
    if path.is_file():
        if path.stat().st_size > 64*1024*1024:
            raise ConfigError('Resource exceeds byte bound')
        return digest(path.read_bytes())
    if not path.is_dir():
        raise ConfigError('Resource must be local content')
    entries = {}
    size = 0
    scanned = 0
    for item in path.rglob('*'):
        scanned += 1
        if scanned > 4096:
            raise ConfigError('Resource tree exceeds entry bound')
        if item.is_symlink():
            raise ConfigError('Resource tree contains a symlink')
        if item.is_file():
            size += item.stat().st_size
            if size > 64*1024*1024 or len(entries) >= 4096:
                raise ConfigError('Resource tree exceeds bounds')
            entries[item.relative_to(path).as_posix()] = {'sha256': digest(item.read_bytes()), 'executable': bool(item.stat().st_mode & 0o111)}
        elif not item.is_dir():
            raise ConfigError('Resource tree contains non-regular content')
    return digest(canonical(entries))


def effective(config, role, profile):
    raw = config._raw_profile(profile)
    resources = []
    for item in raw['resources']:
        path = Path(item['path'])
        if resource_digest(path) != item['sha256']:
            raise ConfigError('Engine resource digest mismatch')
        resources.append(dict(item))
    options = dict(config.options(profile))
    owned = {
        'pi': {'cwd', 'agentDir', 'modelRuntime', 'model', 'tools', 'customTools', 'resourceLoader', 'sessionManager', 'systemPrompt'},
        'codex': {'model', 'model_provider', 'model_instructions_file', 'mcp_servers', 'cwd', 'base_instructions', 'developer_instructions'},
        'claude': {'strict_mcp_config', 'continue_conversation', 'fork_session', 'hooks', 'permission_prompt_tool_name', 'model', 'system_prompt', 'cwd', 'mcp_servers', 'can_use_tool', 'resume', 'session_id', 'permission_mode', 'tools', 'allowed_tools'},
    }[raw['engine']]
    if set(options) & owned:
        raise ConfigError('Engine options conflict with runtime-owned configuration: ' + ', '.join(sorted(set(options) & owned)))
    if raw['engine'] == 'pi':
        allowed = {'thinkingLevel', 'settings', 'codemode', 'toolSearch', 'excludeTools', 'scopedModels'}
        if set(options) - allowed:
            raise ConfigError('Unknown Pi SDK configuration field')
        settings = options.get('settings', {})
        if not isinstance(settings, dict) or settings.get('packages'):
            raise ConfigError('Use reviewed local resources; runtime package acquisition is unavailable')
    if raw['engine'] == 'claude':
        forbidden_args = {'tools', 'allowedTools', 'disallowedTools', 'mcp-config', 'strict-mcp-config',
                          'system-prompt', 'system-prompt-file', 'append-system-prompt', 'permission-mode',
                          'dangerously-skip-permissions', 'allow-dangerously-skip-permissions', 'resume',
                          'continue', 'session-id', 'setting-sources', 'no-session-persistence'}
        if not isinstance(options.get('extra_args', {}), dict):
            raise ConfigError('Claude extra_args must be a table')
        if set(options.get('extra_args', {})) & forbidden_args:
            raise ConfigError('Claude extra_args conflicts with runtime-owned configuration')
    native_content = {}
    if raw['engine'] == 'claude':
        plugins = options.get('plugins', [])
        if not isinstance(plugins, list):
            raise ConfigError('Claude plugins must be a list of local declarations')
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get('type') != 'local' or not isinstance(plugin.get('path'), str):
                raise ConfigError('Claude plugins must select local content')
            native_content[plugin['path']] = resource_digest(Path(plugin['path']))
        native_settings = options.get('settings')
        if isinstance(native_settings, str):
            native_content[native_settings] = resource_digest(Path(native_settings))
    if raw['engine'] == 'codex':
        skills = options.get('skills', {})
        if not isinstance(skills, dict) or not isinstance(skills.get('config', []), list):
            raise ConfigError('Codex skills must use native local configuration')
        for skill in skills.get('config', []):
            if not isinstance(skill, dict) or not isinstance(skill.get('path'), str):
                raise ConfigError('Codex skills must select local content')
            native_content[skill['path']] = resource_digest(Path(skill['path']))
    return {**config.model(profile), 'engine': raw['engine'], 'session': raw['session'],
            'options': options, 'resources': resources, 'mcp_servers': dict(raw['mcp_servers']),
            'engine_tools': list(role.engine_tools), 'native_content': native_content}


def adapter_root(root=None):
    return Path(root) if root is not None else Path(__file__).resolve().parents[2]


def adapter_contract(engine, root=None):
    """Schema-checked adapter contract: the single source for probe commands.

    doctor and check-cli derive entry points and required flags from here
    instead of hardcoding them, so contract.json drift fails loudly.
    """
    base = adapter_root(root) / 'adapters' / engine
    try:
        contract = read_json(base / 'contract.json')
    except (OSError, ValueError) as exc:
        raise ConfigError(f'Unreadable {engine} adapter contract: {exc}') from exc
    if not isinstance(contract, dict):
        raise ConfigError(f'Invalid {engine} adapter contract: not an object')
    for key in ('request_unit', 'completion'):
        if not isinstance(contract.get(key), str) or not contract[key]:
            raise ConfigError(f'Invalid {engine} adapter contract: {key} must be a nonempty string')
    if engine in ('pi', 'claude'):
        entry = contract.get('entrypoint')
        if not isinstance(entry, str) or not entry or '/' in entry or entry.startswith('.'):
            raise ConfigError(f'Invalid {engine} adapter contract: entrypoint must be a plain filename')
        if not (base / entry).is_file():
            raise ConfigError(f'Invalid {engine} adapter contract: missing entrypoint {entry}')
        exports = contract.get('exports')
        if not isinstance(exports, list) or not exports or not all(isinstance(name, str) and name for name in exports):
            raise ConfigError(f'Invalid {engine} adapter contract: exports must be a nonempty string list')
    elif engine == 'codex':
        flags = contract.get('required_flags')
        if not isinstance(flags, list) or not flags or not all(isinstance(flag, str) and flag for flag in flags):
            raise ConfigError('Invalid codex adapter contract: required_flags must be a nonempty string list')
        for item in contract.get('contracts', []):
            if (not isinstance(item, dict) or not isinstance(item.get('command'), list) or not item['command']
                    or not isinstance(item.get('required_flags'), list)):
                raise ConfigError('Invalid codex adapter contract: contracts entries need command and required_flags')
    else:
        raise ConfigError(f'Unknown engine adapter: {engine}')
    return contract


def adapter_digest(engine):
    root = Path(__file__).resolve().parents[2]
    paths = [Path(__file__), Path(__file__).with_name(engine+'.py'), Path(__file__).with_name('engine_channel.py')]
    paths.extend(path for path in (root/'adapters'/engine).iterdir() if path.is_file())
    return digest(canonical({str(path.relative_to(root)): digest(path.read_bytes()) for path in sorted(paths)}))


def local_settings_digest(config, engine):
    # Credentials are never part of evidence or content hashing.
    names = {'pi': ('models.json',), 'codex': (), 'claude': ()}[engine]
    result = {}
    for name in names:
        path = config.agent_dir(engine)/name
        if path.exists():
            result[name] = resource_digest(path)
    return result


def session_record(context, profile, settings):
    identity = {'settings': settings, 'policy': role_policy_text(context.role),
                'goal': context.goal_digest, 'capabilities': context.role.capabilities,
                'workspace': context.role.workspace, 'command': context.config.command(settings['engine']),
                'adapter_digest': adapter_digest(settings['engine']),
                'local_settings': local_settings_digest(context.config, settings['engine'])}
    key = digest(canonical(identity))
    persistent = settings['session'] == 'persistent' and not context.ephemeral
    directory = context.run_dir.parents[1] / 'sessions' / context.role.name / key if persistent else context.run_dir / 'session'
    mkdir(directory)
    path = directory / 'session.json'
    record = read_json(path, None)
    if path.exists():
        if (not isinstance(record, dict) or set(record) != {'id', 'usage'} or
                not isinstance(record.get('id'), str) or not 0 < len(record['id']) <= 4096 or
                not isinstance(record.get('usage'), dict) or len(canonical(record)) > 262144):
            raise ConfigError('Invalid session record; resume cannot be replaced with a new conversation')
    return path, record


def save_session(path, identifier, usage):
    if not isinstance(identifier, str) or not identifier or len(identifier) > 4096:
        raise ConfigError('Engine did not return a valid session identifier')
    write_json(path, {'id': identifier, 'usage': usage})


def mizu_server(bridge):
    import sys
    return {'command': sys.executable, 'args': ['-m', 'mizu.mcp_proxy'],
            'env': {'MIZU_BRIDGE_CONFIG': str(bridge), 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}}


def connected_servers(context, settings):
    """Run operator stdio MCP inside the existing OCI floor, never on the host.

    HTTP servers remain explicit operator endpoints. Each engine grants tools
    separately; bridge credentials are only added to the runtime-owned server.
    """
    import shlex
    import uuid
    servers = {}
    if len(settings['mcp_servers']) > 64:
        raise ConfigError('MCP server count exceeds bound')
    for name, value in settings['mcp_servers'].items():
        if not isinstance(value, dict) or not isinstance(name, str) or not name or name == 'mizu':
            raise ConfigError('Invalid MCP server configuration')
        server = dict(value)
        if 'command' in server:
            command = server['command']
            args = server.get('args', [])
            if not isinstance(command, str) or not command or not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise ConfigError('MCP command must be explicit argv')
            if server.get('env') or server.get('env_vars'):
                raise ConfigError('Container MCP environment belongs in sandbox.env')
            container = 'mizu-mcp-'+uuid.uuid4().hex
            argv = context.sandbox.argv(container, context.workspace,
                                        'exec '+shlex.join([command,*args]), writable=False)
            if not hasattr(context, 'engine_containers'):
                context.engine_containers = []
            context.engine_containers.append(container)
            # Keep stdin open; podman/docker run otherwise drops the protocol input.
            argv.insert(argv.index('run')+1, '-i')
            server.update(command=argv[0], args=argv[1:])
        servers[name] = server
    return servers


def stop_engine_containers(context):
    """Reap only this run's explicit stdio MCP containers, before finalization."""
    from .errors import Denied
    from .process import run
    from .sandbox import remove_argv, remove_leftover, runtime_env
    errors = []
    for name in getattr(context, 'engine_containers', []):
        env = runtime_env(context.config)
        argv = remove_argv(context.config, name)
        if argv is None:
            error = remove_leftover(context.config, name, env)
        else:
            result = run(argv, timeout=15, maximum=8192, env=env)
            error = result.stderr if result.exit_code else None
        if error:
            errors.append(error)
    if errors:
        raise Denied('MCP container cleanup failed; operator recovery is required: ' + '; '.join(errors)[-4000:])
    context.engine_containers = []
