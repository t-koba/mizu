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
        'pi-durable': {'cwd', 'agentDir', 'modelRuntime', 'model', 'tools', 'customTools', 'resourceLoader', 'sessionManager', 'systemPrompt'},
        'codex': {'model', 'model_provider', 'model_instructions_file', 'mcp_servers', 'cwd', 'base_instructions', 'developer_instructions'},
        'claude': {'verbatim_prompts', 'strict_mcp_config', 'continue_conversation', 'fork_session', 'hooks', 'permission_prompt_tool_name', 'model', 'system_prompt', 'cwd', 'mcp_servers', 'can_use_tool', 'resume', 'session_id', 'permission_mode', 'tools', 'allowed_tools'},
    }[raw['engine']]
    if set(options) & owned:
        raise ConfigError('Engine options conflict with runtime-owned configuration: ' + ', '.join(sorted(set(options) & owned)))
    if raw['engine'] == 'pi':
        allowed = {'thinkingLevel', 'settings', 'codemode', 'toolSearch', 'excludeTools', 'scopedModels', 'cacheRetention'}
        if set(options) - allowed:
            raise ConfigError('Unknown Pi SDK configuration field')
        if 'cacheRetention' in options and options['cacheRetention'] not in ('none', 'short', 'long'):
            raise ConfigError("cacheRetention must be one of 'none', 'short', or 'long'")
    if raw['engine'] == 'pi-durable':
        # The durable Harness owns its own generation loop: Pi SDK knobs are
        # refused here instead of silently ignored, and pi profiles cannot
        # carry durable policy that the pi engine never enforces.
        allowed = {'thinkingLevel', 'cacheRetention', 'durable_backend', 'durable_resume',
                   'durable_retention_days',
                   'durable_compaction_reserve_tokens',
                   'durable_compaction_keep_recent_tokens',
                   'durable_compaction_background_tokens'}
        if set(options) - allowed:
            raise ConfigError('Unknown pi-durable configuration field')
        if 'cacheRetention' in options and options['cacheRetention'] not in ('none', 'short', 'long'):
            raise ConfigError("cacheRetention must be one of 'none', 'short', or 'long'")
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
    if engine in ('pi', 'pi-durable', 'claude'):
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
    module = {'pi-durable': 'pi_durable'}.get(engine, engine)
    paths = [Path(__file__), Path(__file__).with_name(module+'.py'), Path(__file__).with_name('engine_channel.py')]
    if engine == 'pi':
        paths.append(Path(__file__).with_name('pi_reselection.py'))
    paths.extend(path for path in (root/'adapters'/engine).iterdir() if path.is_file())
    return digest(canonical({str(path.relative_to(root)): digest(path.read_bytes()) for path in sorted(paths)}))


def local_settings_digest(config, engine):
    # Credentials are never part of evidence or content hashing.
    names = {'pi': ('models.json',), 'pi-durable': ('models.json',), 'codex': (), 'claude': ()}[engine]
    result = {}
    for name in names:
        path = config.agent_dir(engine)/name
        if path.exists():
            result[name] = resource_digest(path)
    return result


#: Keys applied live to a resumed conversation instead of forking it. A
#: profile switch that changes only these keys continues the role's
#: conversation with the current settings, and the durable resume grant
#: excludes them as well, so an effort-only retune resumes instead of
#: refusing. Other effective options still fork or refuse resume. Ordinary Pi
#: additionally applies policy, charter and tool grants live (see its identity
#: projection below); non-Pi identity remains content-bound.
#: Names stay native per engine; Mizu imposes no common effort
#: enum. Durable compaction thresholds ride along because the Harness reads
#: its policy at every resolution: bounding live context must not throw the
#: conversation away, while the per-run grant still binds those values.
CONVERSATION_LIVE_OPTIONS = {
    'pi': frozenset({'thinkingLevel', 'cacheRetention'}),
    'codex': frozenset({'model_reasoning_effort'}),
    'claude': frozenset({'effort'}),
}


def conversation_settings(settings):
    """Effective settings minus live keys for session identity.

    Schema: settings dict from ``effective()``. Bounds: drops at most the
    named live keys. Trust: configuration. Failure: never raises.
    Drivers still receive the full settings, so the current effort and
    durable compaction thresholds apply per dispatch to the shared
    conversation.
    """
    try:
        options = dict(settings.get('options', {}))
    except AttributeError:
        return settings
    for key in CONVERSATION_LIVE_OPTIONS.get(settings.get('engine'), frozenset()):
        options.pop(key, None)
    return {**settings, 'options': options}


def durable_session_identity(settings) -> dict:
    """Session identity for pi-durable: only what makes a conversation incompatible.

    Schema: ``{'engine', 'backend', 'store_compat'}``. Policy text,
    capabilities and tools, goal, model, and settings ride the live
    conversation (the launcher applies them per submission through
    positional system entries and ``configure()``), so they stay out of
    the key. ``store_compat`` is the integer store format the adapter
    declares in ``adapters/pi-durable/contract.json``: launcher changes
    that keep it continue the same store, and only a declared
    store-incompatible change rotates. Bounds: small dict, never raises
    for missing options (backend defaults to sqlite). Trust: operator
    configuration plus the vendored adapter declaration. Failure:
    ``ConfigError`` when the declaration is missing or malformed, so an
    unreadable contract never silently forks or merges conversations.
    """
    try:
        options = settings.get('options', {}) or {}
    except AttributeError:
        options = {}
    backend = options.get('durable_backend', 'sqlite')
    contract_path = Path(__file__).resolve().parents[2] / 'adapters' / 'pi-durable' / 'contract.json'
    try:
        contract = read_json(contract_path, None)
        compat = contract['store_compat']
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError('Unreadable pi-durable store_compat declaration') from exc
    if type(compat) is not int or compat < 1:
        raise ConfigError('Unreadable pi-durable store_compat declaration')
    return {'engine': 'pi-durable', 'backend': backend, 'store_compat': compat}


def conversation_identity(context, settings):
    if settings.get('engine') == 'pi-durable':
        identity = durable_session_identity(settings)
    else:
        projected = conversation_settings(settings)
        # Ordinary Pi persists authority/tool deltas through its current SDK
        # session path. Keep every other compatibility boundary, especially the
        # adapter digest: this projection deliberately rekeys once, never scans
        # or adopts old content-bound directories. Durable/non-Pi are unchanged.
        if settings.get('engine') == 'pi':
            projected = {k: v for k, v in projected.items() if k != 'engine_tools'}
        identity = {'settings': projected,
                    'workspace': context.role.workspace, 'command': context.config.command(settings['engine']),
                    'adapter_digest': adapter_digest(settings['engine']),
                    'local_settings': local_settings_digest(context.config, settings['engine'])}
        if settings.get('engine') != 'pi':
            capabilities = getattr(context, 'session_identity_capabilities', None)
            if capabilities is None:
                capabilities = context.role.capabilities
            identity.update(policy=role_policy_text(context.role), goal=context.goal_digest,
                            capabilities=capabilities)
    return identity


def session_record(context, profile, settings):
    identity = conversation_identity(context, settings)
    key = digest(canonical(identity))
    persistent = settings['session'] == 'persistent' and not context.ephemeral
    directory = context.run_dir.parents[1] / 'sessions' / context.role.name / key if persistent else context.run_dir / 'session'
    from .pi_reselection import family_directory
    directory = family_directory(context, profile, settings, identity, directory)
    mkdir(directory)
    path = directory / 'session.json'
    record = read_json(path, None)
    if path.exists():
        if (not isinstance(record, dict) or set(record) not in ({'id', 'usage'}, {'id', 'usage', 'created_at'}) or
                not isinstance(record.get('id'), str) or not 0 < len(record['id']) <= 4096 or
                not isinstance(record.get('usage'), dict) or len(canonical(record)) > 262144 or
                ('created_at' in record and _session_created_at(record) is None)):
            raise ConfigError('Invalid session record; resume cannot be replaced with a new conversation')
    # A dispatch observer only: peeks have no active record. It must never
    # affect identity, rotation, resume validation, or model execution.
    from .dispatch_evidence import selected_session
    selected_session(context, profile, settings, path)
    return path, record


def reset_role_sessions(config, project, role_name: str) -> dict:
    """Delete one role's persistent sessions and durable stores (explicit reset).

    Schema: receipt ``{"project", "role", "removed_sessions", "removed_stores"}``
    naming deleted session keys and store directories. Session identity
    deliberately almost never rotates for pi-durable, so this is the
    operator's explicit fresh start. Bounds: one role's directories only;
    nothing outside ``sessions/<role>`` and ``durable/<project>/<role>`` is
    touched. Trust: operator CLI invocation. Failure: ``ConfigError`` for an
    unknown role, ``Denied`` while the role has an active run (reset after
    it finishes); filesystem errors propagate loudly, never half-silently.
    """
    from .fs import ID
    from .errors import Denied
    import shutil
    if not isinstance(role_name, str) or not ID.fullmatch(role_name):
        raise ConfigError("Invalid role name for session reset")
    if role_name not in config.roles:
        raise ConfigError(f"Session reset role is not configured: {role_name}")
    if (project.root / "active" / f"{role_name}.json").exists():
        raise Denied(f"Session reset refused: role {role_name} has an active run")
    sessions_dir = project.root / "sessions" / role_name
    removed_sessions = sorted(d.name for d in sessions_dir.iterdir()) if sessions_dir.is_dir() else []
    if sessions_dir.is_dir():
        shutil.rmtree(sessions_dir)
    from .pi_durable_store import _safe_name
    stores_dir = (config.data / "durable" / _safe_name(project.name, "project")
                  / _safe_name(role_name, "role"))
    removed_stores = sorted(d.name for d in stores_dir.iterdir() if d.is_dir()) if stores_dir.is_dir() else []
    if stores_dir.is_dir():
        shutil.rmtree(stores_dir)
    return {"project": project.name, "role": role_name,
            "removed_sessions": removed_sessions, "removed_stores": removed_stores}


def session_generation(session_dir) -> int:
    """Conversation generation of a persistent session key directory.

    Schema: ``generation.json`` holds ``{"generation": N}``; a missing
    file is generation zero (pre-rotation sessions keep their existing
    store). Trust: local operator state, written atomically by rotation
    alongside ``session.json``. Failure: malformed records raise
    ``ConfigError`` rather than silently resuming an older generation.
    """
    try:
        record = read_json(Path(session_dir) / "generation.json", None)
    except (OSError, ValueError) as exc:
        raise ConfigError("Invalid session generation; "
                          "resume cannot be replaced with a new conversation") from exc
    if record is None:
        return 0
    if (not isinstance(record, dict) or set(record) != {"generation"}
            or type(record.get("generation")) is not int
            or not 0 <= record["generation"] <= 2 ** 31):
        raise ConfigError("Invalid session generation; "
                          "resume cannot be replaced with a new conversation")
    return record["generation"]


#: Persistent-session rotation: usage totals are read from the saved
#: per-session record plus existing per-run evidence; no new counters.
MAX_SESSION_TOKENS = 2**63 - 1


def _as_int(value):
    return value if type(value) is int and value >= 0 else None


#: Native per-model token keys saved by each driver: Codex saves its flat
#: TokenUsage.total (codex.TokenUsage.FIELDS); Claude saves native cumulative
#: per-model usage (claude.TOKEN_FIELDS); Pi saves ``{"tokens": N}``.
#: engine_config must not import the drivers (they import this module), so the
#: native names are repeated here and pinned by regression tests.
_TOKEN_COMPONENTS = ("inputTokens", "input_tokens", "input",
                     "outputTokens", "output_tokens", "output",
                     "cachedInputTokens", "cachedTokens", "cache_read_tokens", "cacheRead",
                     "cacheReadInputTokens",
                     "cacheCreationInputTokens", "cache_creation_tokens", "cacheWrite",
                     "reasoningOutputTokens", "reasoningTokens", "reasoning_tokens")
_COST_KEYS = ("costUSD", "cost_estimate_usd", "cost")


def _entry_tokens(entry):
    """Token count for one usage entry: an aggregate wins over components."""
    aggregate = _as_int(entry.get("totalTokens"))
    if aggregate is not None:
        return aggregate
    subtotal = 0
    for key in _TOKEN_COMPONENTS:
        part = _as_int(entry.get(key))
        if part is not None:
            subtotal = min(subtotal + part, MAX_SESSION_TOKENS)
    return subtotal


def _entry_cost(entry):
    """Cost for one usage entry: same quantity under several names counts once."""
    import math
    found = []
    for key in _COST_KEYS:
        value = entry.get(key)
        if type(value) in (int, float) and value >= 0 and math.isfinite(value):
            found.append(float(value))
    return max(found) if found else None


def session_token_total(saved, engine="unknown"):
    """Cumulative model tokens recorded for a saved persistent session.

    Schema: ``saved`` is a ``session_record()`` record (``{'id', 'usage', 'created_at?'}``).
    Bounds: usage payloads stay under the 256 KiB session-record bound; at
    most 4097 entries are scanned. Trust: local operator state only.
    Failure: unknown shapes count 0 (rotation stays age-driven) rather than
    raising. An entry carrying ``totalTokens`` counts the aggregate only, so
    aggregate-plus-component records never double-count.
    """
    try:
        usage = saved.get("usage", {}) if isinstance(saved, dict) else {}
        if not isinstance(usage, dict):
            return 0
        direct = _as_int(usage.get("tokens"))
        if direct is not None:
            return min(direct, MAX_SESSION_TOKENS)
        if _as_int(usage.get("totalTokens")) is not None:
            return min(usage["totalTokens"], MAX_SESSION_TOKENS)
        total = _entry_tokens(usage)
        for value in list(usage.values())[:4096]:
            if isinstance(value, dict):
                total = min(total + _entry_tokens(value), MAX_SESSION_TOKENS)
        return total
    except Exception:
        return 0


def session_cost_total(saved):
    """Cumulative estimated USD recorded for a saved persistent session."""
    try:
        usage = saved.get("usage", {}) if isinstance(saved, dict) else {}
        if not isinstance(usage, dict):
            return 0.0
        import math
        top = _entry_cost(usage)
        if top is not None:
            return top if math.isfinite(top) and top >= 0 else 0.0
        total = 0.0
        for value in list(usage.values())[:4096]:
            if isinstance(value, dict):
                piece = _entry_cost(value)
                if piece is not None:
                    total += piece
        return total if math.isfinite(total) and total >= 0 else 0.0
    except Exception:
        return 0.0


def _session_created_at(saved):
    """Stable conversation creation time, or None when absent/invalid.

    Schema: ``session.json`` ``created_at`` epoch seconds set at first save
    and preserved across same-id saves; legacy records lack it. Bounds:
    finite non-negative number only. Trust: local operator state. Failure:
    never raises (invalid reads as None so callers fall back to mtime or
    treat the record as invalid at validation).
    """
    import math
    try:
        if not isinstance(saved, dict):
            return None
        value = saved.get("created_at")
        if type(value) not in (int, float) or isinstance(value, bool):
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return number
    except Exception:
        return None


def session_age_seconds(path, saved=None):
    """Wall-clock age of a saved session conversation, or None when unknown.

    Schema: prefers the stable ``created_at`` surviving same-id saves so a
    frequently resumed conversation still reaches its lifetime bound;
    legacy records without it fall back to file mtime. Bounds: one stat
    plus pure arithmetic. Trust: local operator state only. Failure:
    ``None`` when the file is missing.
    """
    import time
    try:
        created = _session_created_at(saved)
        if created is not None:
            return max(0.0, time.time() - created)
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def rotation_due(limits, path, saved, engine="unknown"):
    """Decide whether a resumed persistent session must restart.

    Schema: reads ``[limits] session_max_tokens``, ``session_max_cost_usd``
    and ``session_max_age_seconds`` (0 disables each). Bounds: token/cost
    totals come from the bounded saved record; age from the stable
    conversation creation time (legacy fallback to file mtime). Trust:
    local operator state only. Failure: never raises; unknown usage counts
    as zero so age still rotates Pi sessions that report no cumulative
    tokens. Returns ``(due, reason)`` with an empty reason when fresh.
    """
    if saved is None:
        return False, ""
    try:
        maximum = getattr(limits, "session_max_tokens", 0) or 0
        if maximum:
            total = session_token_total(saved, engine)
            if total >= maximum:
                return True, f"tokens {total}>={maximum}"
        cost_max = getattr(limits, "session_max_cost_usd", 0) or 0
        if cost_max:
            cost = session_cost_total(saved)
            if cost >= float(cost_max):
                return True, f"cost {cost}>={cost_max}"
        age_max = getattr(limits, "session_max_age_seconds", 0) or 0
        if age_max:
            age = session_age_seconds(path, saved)
            if age is not None and age >= age_max:
                return True, f"age {age:.0f}s>={age_max}s"
    except Exception:
        return False, ""
    return False, ""


def rotate_session(path, run_dir, reason):
    """Restart a persistent session, keeping the published snapshot.

    Schema: bumps the session ``generation.json`` before removing
    ``session.json`` so the next dispatch mints a fresh provider session
    under the same content-bound key directory; writes ``rotation.json``
    evidence into the current run directory. The published snapshot and
    composed policy reload on the next unit (callers reuse the exact
    published snapshot id). Retry/cancellation: the generation bump
    lands first, so any crash or failure resolves toward the fresh
    conversation, never a silent resume of the prior one: a failed bump
    changes nothing (the error propagates and the next dispatch retries
    the due rotation), while a failure after the bump already points the
    next dispatch at the new generation; the rotation evidence is
    written last. Failure: missing session files are tolerated; other
    I/O errors propagate.
    """
    from .fs import now, write_json
    key = path.parent.name if hasattr(path, "parent") else ""
    if hasattr(path, "parent"):
        generation = session_generation(path.parent)
        write_json(path.parent / "generation.json", {"generation": generation + 1})
    else:
        generation = None
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    record = {"rotated": True, "reason": reason, "session_key": key, "created_at": now()}
    if generation is not None:
        record["generation"] = generation + 1
    write_json(run_dir / "rotation.json", record)
    return record


def read_prompt_state(directory):
    """Last dispatched snapshot/inbox generation for a session directory."""
    from .fs import read_json
    try:
        record = read_json(directory / "prompt_state.json", None)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def write_prompt_state(directory, *, snapshot, inbox_generation):
    """Record what a persistent session has already received (delta)."""
    from .fs import now, write_json
    write_json(directory / "prompt_state.json",
               {"snapshot": snapshot, "inbox_generation": inbox_generation, "created_at": now()})


def save_session(path, identifier, usage):
    """Persist a provider session identifier with a stable creation time.

    Schema: writes ``{'id', 'usage', 'created_at'}`` where ``created_at`` is
    epoch seconds. Same-id saves preserve the existing creation time so
    ``session_max_age_seconds`` bounds conversation lifetime, not file
    inactivity; legacy same-id records without it keep their mtime as
    creation. A new id (or missing file) mints now. Rotation deletes the
    file so the next conversation starts fresh. Bounds: one read plus one
    atomic write. Trust: local operator state. Failure: invalid
    identifiers raise; unreadable prior state starts fresh without raising.
    """
    if not isinstance(identifier, str) or not identifier or len(identifier) > 4096:
        raise ConfigError('Engine did not return a valid session identifier')
    import time
    now_ts = time.time()
    created = now_ts
    try:
        existing = read_json(path, None)
        if isinstance(existing, dict) and existing.get('id') == identifier:
            kept = _session_created_at(existing)
            if kept is not None:
                created = kept
            else:
                try:
                    created = path.stat().st_mtime
                except OSError:
                    created = now_ts
    except Exception:
        pass
    try:
        import math
        created = float(created)
        if not math.isfinite(created) or created < 0:
            created = now_ts
    except Exception:
        created = now_ts
    write_json(path, {'id': identifier, 'usage': usage, 'created_at': created})


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
    from .process import run, stop_note
    from .sandbox import remove_argv, remove_leftover, runtime_env
    errors = []
    for name in getattr(context, 'engine_containers', []):
        env = runtime_env(context.config)
        argv = remove_argv(context.config, name)
        if argv is None:
            error = remove_leftover(context.config, name, env)
        else:
            result = run(argv, timeout=15, maximum=8192, env=env)
            error = stop_note(result) or (result.stderr if result.exit_code else None)
        if error:
            errors.append(error)
    if errors:
        raise Denied('MCP container cleanup failed; operator recovery is required: ' + '; '.join(errors)[-4000:])
    context.engine_containers = []
