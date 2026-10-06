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
from .selection import (DEFAULT_CLASSIFIER_INPUTS, bounded, validate_labels, work_inputs)


class ClassificationOutputError(ValueError):
    """A model completed, but did not return the declared attribute object."""


def _model_summary(model):
    """Expose what the driver reported: identity, requests, known token totals.

    Token usage arrives as per-provider fragments; totals are summed only when
    every fragment is well-formed, otherwise the counts stay unknown rather
    than guessed. Anything oversized or misshapen reads as absent.
    """
    if not isinstance(model, dict):
        return None
    summary = {}
    for key in ('profile', 'engine', 'provider', 'model', 'request_unit'):
        value = model.get(key)
        if isinstance(value, str) and value and len(value) <= 256:
            summary[key] = value
    for key in ('requests',):
        value = model.get(key)
        if type(value) is int and 0 <= value <= 2**53:
            summary[key] = value
    if model.get('usage_known') is True:
        summary['usage_known'] = True
        usage = model.get('usage')
        try:
            if not isinstance(usage, list) or not usage:
                raise ValueError('no fragments')
            totals = {'input': 0, 'output': 0}
            for fragment in usage:
                detail = fragment['usage']
                for kind in totals:
                    count = detail[kind]
                    if type(count) not in (int, float) or not 0 <= count <= 2**53:
                        raise ValueError('bad count')
                    totals[kind] += count
            summary['input_tokens'] = totals['input']
            summary['output_tokens'] = totals['output']
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    try:
        bounded(summary, 4096)
    except Exception:
        return None
    return summary or None


def classify(engine, project, role, snapshot, facts, *, preview=False):
    definition = engine.config.selectors[role.selector].get('classifier')
    if not definition:
        return {}, None
    config = engine.config
    classifier_role = Role(role.name, definition['profile'], (Path(definition['policy']),), 'none', ('finish',))
    settings = effective(config, classifier_role, classifier_role.profile)
    policy = role_policy_text(classifier_role)
    bounds = definition.get('inputs') or dict(DEFAULT_CLASSIFIER_INPUTS)
    cap = bounds['max_text_bytes']
    work = work_inputs(project, role, snapshot, bounds)
    task = facts['task']
    excerpt = {'goal': task.get('goal') if isinstance(task.get('goal'), str) else '',
               'state': task.get('state') if isinstance(task.get('state'), str) else ''}
    excerpt = {key: value[:cap] for key, value in excerpt.items()}
    inputs = {'task': excerpt, 'proposals': work['proposals'], 'evidence': work['evidence'],
              'attributes': facts['attributes'], 'role': role.name,
              'project': project.name, 'output_attributes': definition['attributes']}
    # Cache identity covers material inputs and policy, never raw state text:
    # bookkeeping state churn must not invalidate, while new proposals,
    # fresh evidence, goal edits, label changes, or policy/settings changes do.
    key = digest(canonical({'goal': task.get('goal'), 'proposals': work['proposals'],
                            'evidence': work['evidence'], 'attributes': facts['attributes'],
                            'definition': definition, 'policy': policy,
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
            return cached['attributes'], {'status': 'cached', 'run': cached['run'], 'key': key,
                                           'source': 'cache',
                                           'reason': 'material inputs unchanged since ' + cached['run'],
                                           'model': cached.get('model')}
        if cached['retry_at'] > at:
            return {}, {**cached, 'waiting': definition['on_failure'] == 'wait'}
    if preview:
        return {}, {'status': 'not_run', 'key': key, 'provisional': True, 'source': 'none',
                   'reason': 'preview never runs inference'}
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
        summary = _model_summary(model)
        result = {'run': run, 'role': role.name, 'kind': 'classification', 'status': 'completed',
                  'finished_at': now(), 'model': model, 'attributes': labels, 'key': key}
        write_json(directory / 'result.json', result)
        cached = {'run': run, 'status': 'completed', 'attributes': labels, 'key': key}
        if summary is not None:
            cached['model'] = summary
        write_json(path, cached)
        return labels, {'status': 'completed', 'run': run, 'key': key, 'source': 'inference',
                        'reason': 'material inputs differed from cache', 'model': summary}
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
                  'retry_at': time.time() + definition['retry_seconds'],
                  'source': 'inference', 'reason': 'inference failed; fallback applies'}
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
    definition = spec.get('classifier')
    labels, classification = {}, None
    if definition:
        declared = definition['attributes']
        covered = {key: facts['attributes'][key] for key in declared if key in facts['attributes']}
        explicit = len(covered) == len(declared)
        if explicit:
            try:
                validate_labels(covered, declared)
            except (ConfigError, ValueError, TypeError):
                explicit = False
        if explicit:
            classification = {'status': 'explicit', 'source': 'explicit',
                              'reason': 'all declared attributes already labelled',
                              'attributes': covered}
        else:
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
