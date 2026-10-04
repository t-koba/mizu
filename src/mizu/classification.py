"""Optional attribute inference through existing drivers, with no recursive routing."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .config import Role, role_policy_text
from .engine_config import effective, adapter_digest, local_settings_digest
from .errors import Cancelled, ConfigError, Denied, ProtocolError, ModelFailure
from .fs import canonical, digest, mkdir, now, read_json, write_json
from .selection import bounded, validate_labels


class ClassificationOutputError(ValueError):
    """A model completed, but did not return the declared attribute object."""


def classify(engine, project, role, snapshot, facts, *, preview=False):
    definition = engine.config.selectors[role.selector].get('classifier')
    if not definition:
        return {}, None
    config = engine.config
    classifier_role = Role(role.name, definition['profile'], (Path(definition['policy']),), 'none', ('finish',))
    settings = effective(config, classifier_role, classifier_role.profile)
    policy = role_policy_text(classifier_role)
    inputs = {'task': facts['task'], 'attributes': facts['attributes'], 'role': role.name,
              'project': project.name, 'output_attributes': definition['attributes']}
    key = digest(canonical({'inputs': inputs, 'definition': definition, 'policy': policy,
                            'settings': settings, 'command': config.command(settings['engine']),
                            'adapter': adapter_digest(settings['engine']),
                            'local_settings': local_settings_digest(config, settings['engine'])}))
    path = project.root / 'selection' / (role.name + '-classification.json')
    cached = None
    if path.exists():
        try:
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError('invalid cache file')
            cached = read_json(path)
            bounded(cached, 65536)
            if not isinstance(cached, dict) or cached.get('status') not in ('completed', 'failed'):
                raise ValueError('invalid cache')
            if not isinstance(cached.get('key'), str) or not isinstance(cached.get('run'), str):
                raise ValueError('invalid cache identity')
            if cached['status'] == 'completed':
                validate_labels(cached['attributes'], definition['attributes']) if cached['key'] == key else None
            elif type(cached.get('retry_at')) not in (int, float):
                raise ValueError('invalid retry time')
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ConfigError('Unreadable classification cache') from exc
    at = time.time()
    if cached and cached['key'] == key:
        if cached['status'] == 'completed':
            return cached['attributes'], {'status': 'cached', 'run': cached['run'], 'key': key}
        if cached['retry_at'] > at:
            return {}, {**cached, 'waiting': definition['on_failure'] == 'wait'}
    if preview:
        return {}, {'status': 'not_run', 'key': key, 'provisional': True}
    if engine.stop.is_set():
        raise Cancelled('Classification cancelled')
    from .runtime import Context
    run = uuid.uuid4().hex
    directory = project.root / 'runs' / run
    mkdir(directory)
    workspace = directory / 'input'
    mkdir(workspace)
    context = Context(config, project, classifier_role, directory, snapshot, workspace,
                      stop=engine.stop, goal=inputs['task']['goal'])
    context.ephemeral = True
    write_json(directory / 'started.json', {'run': run, 'role': role.name, 'kind': 'classification', 'started_at': now(), 'key': key})
    model = None
    try:
        model = engine.resolve(classifier_role.profile).execute(context, canonical(inputs).decode())
        if context.cancelled():
            raise Cancelled('Classification cancelled')
        if context.finished is None:
            raise Denied('Classifier did not seal its work unit')
        try:
            labels = json.loads(context.finished['summary'], parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))
            validate_labels(labels, definition['attributes'])
        except (ValueError, TypeError, ConfigError) as exc:
            raise ClassificationOutputError('Classifier summary is not valid declared attribute JSON') from exc
        result = {'run': run, 'role': role.name, 'kind': 'classification', 'status': 'completed',
                  'finished_at': now(), 'model': model, 'attributes': labels, 'key': key}
        write_json(directory / 'result.json', result)
        write_json(path, {k: result[k] for k in ('run', 'status', 'attributes', 'key')})
        return labels, {'status': 'completed', 'run': run, 'key': key}
    except Exception as exc:
        write_json(directory / 'error.json', {'run': run, 'role': role.name, 'kind': 'classification',
                   'status': 'interrupted', 'finished_at': now(), 'error': str(exc),
                   'model': model or getattr(context, 'model_evidence', {})})
        # Runtime invariants and operator stop requests cannot become a fallback.
        if context.cancelled():
            raise Cancelled('Classification cancelled') from exc
        if context.admission_error:
            raise ProtocolError('Local classification admission failed: ' + context.admission_error) from exc
        if not isinstance(exc, (ModelFailure, ClassificationOutputError)):
            raise
        cached = {'run': run, 'key': key, 'status': 'failed', 'error': str(exc)[:4000],
                  'retry_at': time.time() + definition['retry_seconds']}
        write_json(path, cached)
        return {}, {**cached, 'waiting': definition['on_failure'] == 'wait'}
    finally:
        context.closed = True
        context.cancel_operations()


def prepare(engine, project, role, snapshot, explicit=None, *, preview=False):
    from .selection import base_facts, rule_attributes, select
    facts, sources = base_facts(project, role, snapshot, explicit)
    spec = engine.config.selectors[role.selector]
    rule_attributes(spec, facts, sources)
    labels, classification = classify(engine, project, role, snapshot, facts, preview=preview)
    for key, value in labels.items():
        if key not in facts['attributes']:
            facts['attributes'][key], sources[key] = value, 'inference'
    from .selection import attributes
    attributes(facts["attributes"])
    decision = select(engine.config, role, facts, sources, classification=classification)
    if classification and classification.get('waiting'):
        decision.update(profile=None, reason='classification_wait',
                        next_evaluation_at=min(decision['next_evaluation_at'], classification['retry_at']))
    if classification and classification.get('provisional'):
        decision['provisional'] = True
    return decision
