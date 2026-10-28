"""Operator-only provider inspection; uses the existing trusted engine boundary.

One bounded child, no bridge/session/inference, no retries. Credentials are only
passed to the configured engine command, never workspace/container tools. Like
ordinary Pi, reviewed extensions execute inside that trusted runtime boundary.
"""
import datetime as dt
import json
import stat
import tempfile
from pathlib import Path

from .config import Role
from .engine_config import effective, resource_digest
from .errors import ConfigError, Denied, LimitExceeded
from .fs import DIGEST, canonical, digest, lock, mkdir, now, safe_read, safe_write, write_json
from .pi import credentials
from .process import environment, run

ROOT = Path(__file__).resolve().parents[2]
MAXIMUM = 1024 * 1024
_REPORT_KEYS = frozenset({"engine", "provider", "selected_model", "inference", "source",
                          "status", "models", "refresh", "error",
                          "last_successful_observation", "freshness", "completeness"})
_REPORT_ERRORS = frozenset({"offline", "auth_failed", "refresh_failed", "aborted",
                            "invalid_native_listing", "provider_setup_failed"})

# One bounded store, not an index of SDK caches. Only digests and safe times/codes.
OBSERVATIONS = 'model-catalog-observations.json'
OBSERVATION_BYTES = 64 * 1024
OBSERVATION_COUNT = 128
_REFRESH_ERRORS = _REPORT_ERRORS | {'engine_unavailable', 'catalog_process_failed',
                                    'invalid_catalog_response'}


def _timestamp(text):
    if not isinstance(text, str) or len(text) != 25:
        raise ValueError('invalid observation time')
    value = dt.datetime.fromisoformat(text)
    if value.utcoffset() != dt.timedelta(0) or value.isoformat(timespec='seconds') != text:
        raise ValueError('invalid observation time')
    return value


def _local_read(data, name, maximum):
    # lstat distinguishes absence from dangling links; safe_read pins regular,
    # single-link bytes and refuses oversized files before parsing.
    try:
        (data / name).lstat()
    except FileNotFoundError:
        return None
    return safe_read(data, name, maximum)


def _read_observations(data):
    raw = _local_read(data, OBSERVATIONS, OBSERVATION_BYTES)
    if raw is None:
        return {}
    store = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(store, dict) or set(store) != {'version', 'observations', 'checksum'}
            or type(store['version']) is not int or store['version'] != 1):
        raise ValueError('invalid observation store')
    entries = store['observations']
    if not isinstance(entries, dict) or len(entries) > OBSERVATION_COUNT:
        raise ValueError('invalid observations')
    if store['checksum'] != digest(canonical({'version': 1, 'observations': entries})):
        raise ValueError('observation checksum mismatch')
    for key, entry in entries.items():
        if not DIGEST.fullmatch(key) or not isinstance(entry, dict) or set(entry) != {'success', 'refresh_error'}:
            raise ValueError('invalid observation')
        success, error = entry['success'], entry['refresh_error']
        if success is not None:
            if (not isinstance(success, dict) or set(success) != {'listing_digest', 'at'}
                    or not isinstance(success['listing_digest'], str)
                    or not DIGEST.fullmatch(success['listing_digest'])):
                raise ValueError('invalid success')
            _timestamp(success['at'])
        if error is not None:
            if (not isinstance(error, dict) or set(error) != {'code', 'at'}
                    or not isinstance(error['code'], str) or error['code'] not in _REFRESH_ERRORS):
                raise ValueError('invalid refresh error')
            _timestamp(error['at'])
        if success is None and error is None:
            raise ValueError('empty observation')
    return entries


def _write_observations(data, entries):
    payload = {'version': 1, 'observations': entries}
    store = {**payload, 'checksum': digest(canonical(payload))}
    raw = canonical(store)
    if len(entries) > OBSERVATION_COUNT or len(raw) > OBSERVATION_BYTES:
        raise ValueError('observations exceed bound')
    safe_write(data, OBSERVATIONS, raw)


def _entry_time(entry):
    # Oldest retained instant for bounded eviction; entries without a time sort first.
    times = []
    success = entry.get('success')
    error = entry.get('refresh_error')
    if isinstance(success, dict) and isinstance(success.get('at'), str):
        times.append(success['at'])
    if isinstance(error, dict) and isinstance(error.get('at'), str):
        times.append(error['at'])
    return max(times) if times else ''


def _oldest_key(entries, exclude):
    candidates = [key for key in entries if key != exclude]
    return min(candidates, key=lambda key: (_entry_time(entries[key]), key))


def _updated_entry(old, report, refresh, moment):
    valid = (report['source'] == 'registered_native_provider'
             and report['error'] not in {'invalid_native_listing', 'provider_setup_failed'})
    listing = digest(canonical(report['models'])) if valid else None
    entry = old
    if refresh and report['error'] is not None:
        entry = {**entry, 'refresh_error': {'code': report['error'], 'at': moment}}
    elif (refresh and valid and report['refresh'] == 'completed'
          and report['status'] in {'available', 'incomplete'}):
        entry = {'success': {'listing_digest': listing, 'at': moment}, 'refresh_error': None}
    return entry, listing


def _decorate(report, entry, listing, moment):
    success = entry['success']
    matches = success is not None and listing == success['listing_digest']
    if not matches:
        return {**report, 'last_successful_observation': None,
                'observation_age_seconds': None,
                'freshness': 'unknown',
                'last_refresh_error': entry['refresh_error']}
    current = _timestamp(moment)
    observed = _timestamp(success['at'])
    if current < observed:
        # Clock moved backward; a future timestamp proves nothing about freshness.
        return {**report, 'last_successful_observation': success['at'],
                'observation_age_seconds': None,
                'freshness': 'unknown',
                'last_refresh_error': entry['refresh_error']}
    return {**report, 'last_successful_observation': success['at'],
            'observation_age_seconds': int((current - observed).total_seconds()),
            'freshness': 'observed',
            'last_refresh_error': entry['refresh_error']}


def _present(entries, key, report):
    # Lock-free cache-only presentation from atomic safe reads; never persists.
    old = entries.get(key, {'success': None, 'refresh_error': None})
    moment = now()
    valid = (report['source'] == 'registered_native_provider'
             and report['error'] not in {'invalid_native_listing', 'provider_setup_failed'})
    listing = digest(canonical(report['models'])) if valid else None
    return _decorate(report, old, listing, moment)


def _observation_key(config, settings, agent_dir):
    # Configured runtime identity, NOT an attestation of executable behavior.
    # Neither credentials nor raw paths are persisted or returned.
    return digest(canonical({
        'engine': settings['engine'], 'provider': settings['provider'],
        'command': list(config.command(settings['engine'])), 'agent_dir': str(agent_dir.resolve()),
        'options': settings['options'], 'resources': settings['resources'],
        'adapter': {name: resource_digest(ROOT / 'adapters/pi' / name)
                    for name in ('launcher.mjs', 'catalog.mjs', 'package-lock.json')},
    }))


def _observe(data, entries, key, report, refresh):
    # Caller holds the cross-process lock around this short read/modify/write.
    old = entries.get(key, {'success': None, 'refresh_error': None})
    moment = now()
    entry, listing = _updated_entry(old, report, refresh, moment)
    if entry != old:
        if key not in entries:
            while len(entries) >= OBSERVATION_COUNT:
                del entries[_oldest_key(entries, key)]
        entries[key] = entry
        while True:
            try:
                _write_observations(data, entries)
                break
            except ValueError:
                candidates = [other for other in entries if other != key]
                if not candidates:
                    raise
                del entries[_oldest_key(entries, key)]
    return _decorate(report, entry, listing, moment)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_report(report, base, selected):
    if not isinstance(report, dict) or set(report) - _REPORT_KEYS:
        raise ValueError("unexpected report field")
    result = {**base, **report}
    if set(result) != _REPORT_KEYS:
        raise ValueError("missing report field")
    if (result["engine"] != "pi" or result["provider"] != selected["provider"]
            or result["selected_model"] != selected["model"] or result["inference"] != "not_run"):
        raise ValueError("report selection mismatch")
    if result["source"] not in {"unsupported", "registered_native_provider"}:
        raise ValueError("invalid source")
    if result["status"] not in {"available", "incomplete", "unsupported", "error"}:
        raise ValueError("invalid status")
    if result["refresh"] not in {"not_requested", "unsupported", "completed", "failed", "not_started", "unknown"}:
        raise ValueError("invalid refresh status")
    if result["error"] is not None and result["error"] not in _REPORT_ERRORS:
        raise ValueError("unsafe error")
    if (result["last_successful_observation"] is not None
            or result["freshness"] != "unknown" or result["completeness"] != "unknown"):
        raise ValueError("unsupported freshness metadata")
    rows = result["models"]
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("invalid model rows")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "resolvable"}:
            raise ValueError("invalid model row")
        model_id = row["id"]
        if not isinstance(model_id, str) or not model_id or type(row["resolvable"]) is not bool:
            raise ValueError("invalid model row")
        if (any(ord(char) < 0x20 or ord(char) == 0x7f for char in model_id)
                or len(model_id.encode("utf-16-le")) // 2 > 512):
            raise ValueError("invalid model id")
        model_id.encode("utf-8")
        if model_id in ids:
            raise ValueError("duplicate model id")
        ids.add(model_id)
    if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 768 * 1024:
        raise ValueError("model rows exceed output bound")
    if result["source"] == "unsupported" and rows:
        raise ValueError("unsupported source has models")
    if result["status"] == "available" and (result["error"] is not None or any(not row["resolvable"] for row in rows)):
        raise ValueError("inconsistent available status")
    if result["status"] == "incomplete" and (result["error"] is not None or not any(not row["resolvable"] for row in rows)):
        raise ValueError("inconsistent incomplete status")
    if result["status"] == "error" and result["error"] is None:
        raise ValueError("error status without error code")
    if (result["status"] == "unsupported" and result["source"] == "registered_native_provider"
            and result["error"] != "invalid_native_listing"):
        raise ValueError("inconsistent unsupported status")
    return result


def _validate_observation_root(data):
    info = data.lstat()
    if (not stat.S_ISDIR(info.st_mode) or data.is_symlink()
            or getattr(info, 'st_reparse_tag', 0)):
        raise ValueError('unsafe observation directory')


def inspect(config, profile: str, *, refresh: bool = False) -> dict:
    settings = effective(config, Role('catalog', profile, (), 'none', ()), profile)
    engine = settings['engine']
    selected = config.model(profile)
    base = {'engine': engine, 'provider': selected['provider'],
            'selected_model': selected['model'], 'inference': 'not_run',
            'source': 'unsupported', 'status': 'unsupported', 'models': [],
            'refresh': 'not_started' if refresh else 'not_requested', 'error': None,
            'last_successful_observation': None, 'freshness': 'unknown', 'completeness': 'unknown'}
    if engine != 'pi':
        if engine == 'pi-durable':
            error = ('extension_provided_model_not_supported_by_engine'
                     if any(item['kind'] == 'extension' for item in settings['resources'])
                     else None)
        else:
            error = 'engine_unsupported'
        return {**base, 'status': 'unsupported', 'source': 'unsupported', 'models': [],
                'refresh': 'unsupported' if refresh else 'not_requested', 'error': error,
                'last_successful_observation': None, 'freshness': 'unknown', 'completeness': 'unknown',
                'observation_age_seconds': None, 'last_refresh_error': None}
    agent_dir = config.agent_dir(engine)
    mkdir(agent_dir)
    mkdir(config.data)
    # Validate the retained sidecar before touching credentials or provider code.
    # Atomic safe reads only; no lock is held across the potentially long child.
    try:
        _validate_observation_root(config.data)
        _local_read(config.data, '.model-catalog.lock', 0)
        _read_observations(config.data)
    except (OSError, ValueError, TypeError, RecursionError, Denied, LimitExceeded):
        raise ConfigError('Invalid or unavailable model catalog observation store') from None
    # Provider/runtime identity errors keep their own message, never a store error.
    key = _observation_key(config, settings, agent_dir)
    # Credentials and the provider child run without the global store lock so
    # unrelated inspections never block for the provider timeout.
    try:
        report = _invoke(config, settings, selected, base, agent_dir, refresh)
    except ConfigError as exc:
        # Only a malformed provider listing is recorded as invalid_catalog_response;
        # credential or other config errors keep their own identity and never touch the store.
        if str(exc) != 'Invalid model catalog response':
            raise
        invalid = {**base, 'error': 'invalid_catalog_response'}
        try:
            with lock(config.data / '.model-catalog.lock'):
                entries = _read_observations(config.data)
                _observe(config.data, entries, key, invalid, refresh)
        except ConfigError:
            raise
        except (OSError, ValueError, TypeError, RecursionError, Denied, LimitExceeded):
            raise ConfigError('Invalid or unavailable model catalog observation store') from None
        raise
    if not refresh:
        # Cache-only presentation from atomic safe reads; no store lock needed.
        try:
            entries = _read_observations(config.data)
        except (OSError, ValueError, TypeError, RecursionError, Denied, LimitExceeded):
            raise ConfigError('Invalid or unavailable model catalog observation store') from None
        return _present(entries, key, report)
    # Short post-invocation read/modify/atomic-write under the cross-process lock.
    try:
        with lock(config.data / '.model-catalog.lock'):
            entries = _read_observations(config.data)
            return _observe(config.data, entries, key, report, refresh)
    except ConfigError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, Denied, LimitExceeded):
        raise ConfigError('Invalid or unavailable model catalog observation store') from None


def _invoke(config, settings, selected, base, agent_dir, refresh):
    engine = settings['engine']
    env = environment()
    env.update(credentials(config.file.parent / 'credentials.env'))
    with tempfile.TemporaryDirectory(prefix='catalog-', dir=config.data) as directory:
        directory = Path(directory)
        path = directory / 'effective.json'
        write_json(path, {**settings, **selected, 'agentDir': str(agent_dir),
                          'cwd': str(directory), 'refresh': refresh})
        try:
            result = run([*config.command(engine), str(ROOT / 'adapters/pi/launcher.mjs'),
                          '--model-catalog', str(path)], cwd=directory, env=env,
                         timeout=config.limits.model_request_seconds, maximum=MAXIMUM)
        except OSError:
            return {**base, 'status': 'error', 'error': 'engine_unavailable', 'refresh': 'not_started' if refresh else 'not_requested'}
    if result.reason != 'exited' or result.exit_code != 0:
        return {**base, 'status': 'error', 'error': 'catalog_process_failed', 'refresh': 'unknown' if refresh else 'not_requested'}
    try:
        report = json.loads(result.stdout, object_pairs_hook=_unique_object)
        return _validate_report(report, base, selected)
    except (ValueError, TypeError, UnicodeError):
        raise ConfigError('Invalid model catalog response') from None
