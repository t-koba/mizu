"""Operator-only provider inspection; uses the existing trusted engine boundary.

One bounded child, no bridge/session/inference, no retries. Credentials are only
passed to the configured engine command, never workspace/container tools. Like
ordinary Pi, reviewed extensions execute inside that trusted runtime boundary.
"""
import json
import tempfile
from pathlib import Path

from .config import Role
from .engine_config import effective
from .errors import ConfigError
from .fs import mkdir, write_json
from .pi import credentials
from .process import environment, run

ROOT = Path(__file__).resolve().parents[2]
MAXIMUM = 1024 * 1024
_REPORT_KEYS = frozenset({"engine", "provider", "selected_model", "inference", "source",
                          "status", "models", "refresh", "error",
                          "last_successful_observation", "freshness", "completeness"})
_REPORT_ERRORS = frozenset({"offline", "auth_failed", "refresh_failed", "aborted",
                            "invalid_native_listing", "provider_setup_failed"})


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
        return {**base, 'status': 'unsupported', 'source': 'unsupported', 'models': [],
                'refresh': 'unsupported' if refresh else 'not_requested',
                'error': ('durable_native_provider_source_mismatch' if engine == 'pi-durable'
                          else 'engine_unsupported'),
                'last_successful_observation': None, 'freshness': 'unknown', 'completeness': 'unknown'}
    agent_dir = config.agent_dir(engine)
    mkdir(agent_dir)
    mkdir(config.data)
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
