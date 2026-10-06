"""Declarative selection: bounded facts and predicates, no provider policy or IO callbacks."""
from __future__ import annotations

import math
import time

from .errors import ConfigError, ModelFailure
from .fs import ID, canonical, digest, lock, read_json, write_json

MAX_BYTES = 1024 * 1024
MAX_ITEMS = 64
UNKNOWN = object()

#: Default bounds for classifier work inputs (operator policy may override
#: per classifier through its `inputs` table; 0 disables that input).
DEFAULT_CLASSIFIER_INPUTS = {'max_proposals': 8, 'max_decisions': 8, 'max_text_bytes': 2048}
#: Upper bound on the assembled work-input document itself.
MAX_WORK_BYTES = 131072
#: Decision actions that count as review evidence (withdrawals never inform routing).
EVIDENCE_ACTIONS = ('accept', 'modify', 'defer', 'reject')


def table(value, allowed, where):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError(f'Invalid {where} fields')


def bounded(value, maximum=MAX_BYTES):
    try:
        if len(canonical(value)) > maximum:
            raise ValueError('too large')
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConfigError('Selection data must be bounded finite JSON') from exc
    return value


def items(value, where, *, nonempty=False):
    if not isinstance(value, list) or not int(nonempty) <= len(value) <= MAX_ITEMS:
        raise ConfigError(f'{where} must contain {int(nonempty)}..{MAX_ITEMS} entries')
    return value


def name(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ConfigError('Invalid selection identifier')
    return value


def scalar(value):
    return (type(value) in (bool, int, float, str) and
            (not isinstance(value, str) or len(value) <= 4096 and '\x00' not in value) and
            (type(value) not in (int, float) or abs(value) <= 2**53 and math.isfinite(value)))


def attributes(value):
    if not isinstance(value, dict) or len(value) > MAX_ITEMS:
        raise ConfigError('Attributes must be a table of at most 64 scalar values')
    for key, val in value.items():
        name(key)
        if not scalar(val):
            raise ConfigError('Attribute values must be bounded strings, booleans or finite numbers')
    bounded(value, 16384)
    return value


def seconds(value):
    if type(value) is not int or not 1 <= value <= 31536000:
        raise ConfigError('Selection intervals must be 1..31536000 seconds')
    return value


def timestamp(value):
    if type(value) not in (int, float) or not 0 <= value <= 253402300799 or not math.isfinite(value):
        raise ConfigError('Selection times must be finite Unix seconds')
    return value


def validate_condition(cond, depth=0):
    if depth > 12 or not isinstance(cond, dict):
        raise ConfigError('Invalid selection condition or nesting exceeds 12')
    bounded(cond, 16384)
    if not cond:
        return
    for op in ('all', 'any', 'not'):
        if op in cond:
            if set(cond) != {op}:
                raise ConfigError('Logical condition must have one operator')
            children = [cond[op]] if op == 'not' else items(cond[op], op, nonempty=True)
            for child in children:
                validate_condition(child, depth+1)
            return
    table(cond, {'path', 'op', 'value'}, 'condition')
    path, op = cond.get('path'), cond.get('op')
    if not isinstance(path, str) or len(path) > 512 or any(not part for part in path.split('.')):
        raise ConfigError('Condition requires a dotted path')
    if op not in ('eq', 'in', 'lt', 'lte', 'gt', 'gte', 'exists', 'contains') or 'value' not in cond:
        raise ConfigError('Condition requires a supported operator and value')
    value = cond['value']
    if op == 'in':
        if any(not scalar(v) for v in items(value, 'in', nonempty=True)):
            raise ConfigError('Membership values must be scalars')
    elif op == 'exists':
        if type(value) is not bool:
            raise ConfigError('exists requires a boolean')
    elif not scalar(value):
        raise ConfigError('Condition value must be a scalar')
    if op in ('lt', 'lte', 'gt', 'gte') and type(value) not in (int, float):
        raise ConfigError('Ordering requires a number')
    if op == 'contains' and not isinstance(value, str):
        raise ConfigError('contains requires a string')


def lookup(facts, path):
    value = facts
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            return UNKNOWN
        value = value[key]
    return UNKNOWN if value is None else value


def equal(left, right):
    return type(left) is type(right) and left == right or (
        type(left) in (int, float) and type(right) in (int, float) and left == right)


def evaluate(cond, facts):
    """Three-valued logic: missing is never made true by negation."""
    if not cond:
        return True
    if 'not' in cond:
        result = evaluate(cond['not'], facts)
        return UNKNOWN if result is UNKNOWN else not result
    for op in ('all', 'any'):
        if op in cond:
            results = [evaluate(c, facts) for c in cond[op]]
            if op == 'all' and any(r is False for r in results):
                return False
            if op == 'any' and any(r is True for r in results):
                return True
            if any(r is UNKNOWN for r in results):
                return UNKNOWN
            return op == 'all'
    value, op, expected = lookup(facts, cond['path']), cond['op'], cond['value']
    if op == 'exists':
        return (value is not UNKNOWN) == expected
    if value is UNKNOWN:
        return UNKNOWN
    if op == 'eq':
        return equal(value, expected)
    if op == 'in':
        return any(equal(value, v) for v in expected)
    if op == 'contains':
        return isinstance(value, str) and expected in value
    if type(value) not in (int, float):
        return False
    return {'lt': value < expected, 'lte': value <= expected,
            'gt': value > expected, 'gte': value >= expected}[op]


def validate_selectors(value, profiles, base):
    if not isinstance(value, dict) or len(value) > MAX_ITEMS:
        raise ConfigError('selectors must contain at most 64 definitions')
    bounded(value)
    for key, spec in value.items():
        name(key)
        table(spec, {'rules', 'classify_rules', 'classifier', 'on_error', 'retry_seconds'}, 'selector')
        seconds(spec.get('retry_seconds'))
        for rule in items(spec.get('rules'), 'rules', nonempty=True):
            table(rule, {'when', 'candidates'}, 'selection rule')
            validate_condition(rule.get('when', {}))
            seen = set()
            for candidate in items(rule.get('candidates'), 'candidates', nonempty=True):
                table(candidate, {'profile', 'group', 'when', 'recover_when'}, 'candidate')
                profile = candidate.get('profile')
                if not isinstance(profile, str) or profile not in profiles or profile in seen:
                    raise ConfigError('Candidates require distinct configured profiles')
                seen.add(profile)
                name(candidate.get('group', profile))
                validate_condition(candidate.get('when', {}))
                if 'recover_when' in candidate:
                    validate_condition(candidate['recover_when'])
        for rule in items(spec.get('classify_rules', []), 'classify_rules'):
            table(rule, {'when', 'attributes'}, 'classification rule')
            validate_condition(rule.get('when', {}))
            attributes(rule.get('attributes'))
        for rule in items(spec.get('on_error', []), 'on_error'):
            table(rule, {'when', 'scope', 'seconds', 'until'}, 'error rule')
            validate_condition(rule.get('when', {}))
            if rule.get('scope') not in ('profile', 'group'):
                raise ConfigError('Error scope must be profile or group')
            if ('seconds' in rule) == ('until' in rule):
                raise ConfigError('Error rule needs seconds or until')
            if 'seconds' in rule:
                seconds(rule['seconds'])
            elif rule['until'] != 'error.retry_at':
                raise ConfigError('until must reference error.retry_at')
        if 'classifier' in spec:
            classifier = spec['classifier']
            table(classifier, {'profile', 'policy', 'attributes', 'on_failure', 'retry_seconds', 'inputs'}, 'classifier')
            if not isinstance(classifier.get('profile'), str) or classifier['profile'] not in profiles:
                raise ConfigError('Classifier requires a fixed configured profile')
            if classifier.get('on_failure') not in ('continue', 'wait'):
                raise ConfigError('Classifier on_failure must be continue or wait')
            seconds(classifier.get('retry_seconds'))
            from .config import path_value
            policy = classifier.get('policy')
            if not isinstance(policy, str) or not policy:
                raise ConfigError('Classifier requires a policy path')
            path = path_value(policy, base)
            if not path.is_file() or path.stat().st_size > 65536:
                raise ConfigError('Classifier policy must be a file of at most 64 KiB')
            classifier['policy'] = str(path)
            raw_inputs = classifier.get('inputs', {})
            if not isinstance(raw_inputs, dict) or set(raw_inputs) - set(DEFAULT_CLASSIFIER_INPUTS):
                raise ConfigError('Classifier inputs must name known bounds')
            merged = {**DEFAULT_CLASSIFIER_INPUTS, **raw_inputs}
            for key in ('max_proposals', 'max_decisions'):
                if type(merged[key]) is not int or not 0 <= merged[key] <= MAX_ITEMS:
                    raise ConfigError('Classifier item bounds must be integers in 0..64')
            if type(merged['max_text_bytes']) is not int or not 256 <= merged['max_text_bytes'] <= 65536:
                raise ConfigError('Classifier text bound must be an integer in 256..65536')
            classifier['inputs'] = merged
            fields = classifier.get('attributes')
            if not isinstance(fields, dict) or not 1 <= len(fields) <= MAX_ITEMS:
                raise ConfigError('Classifier requires 1..64 attribute definitions')
            for field, definition in fields.items():
                name(field)
                table(definition, {'type', 'values'}, 'attribute definition')
                if definition.get('type') not in ('string', 'number', 'boolean'):
                    raise ConfigError('Unknown classifier attribute type')
                if 'values' in definition:
                    for item in items(definition['values'], 'values', nonempty=True):
                        validate_labels({field: item}, {field: definition})
    return value


def validate_labels(value, fields):
    attributes(value)
    if set(value) - set(fields):
        raise ConfigError('Classifier returned undeclared attributes')
    for key, val in value.items():
        definition = fields[key]
        types = {'string': (str,), 'number': (int, float), 'boolean': (bool,)}
        if type(val) not in types[definition['type']] or (
            'values' in definition and not any(equal(val, v) for v in definition['values'])):
            raise ConfigError('Classifier attribute does not match its declaration')
    return value


def role_profiles(config, role):
    if not role.selector:
        return [role.profile]
    spec = config.selectors[role.selector]
    names = {c['profile'] for r in spec['rules'] for c in r['candidates']}
    if 'classifier' in spec:
        names.add(spec['classifier']['profile'])
    return sorted(names)


def groups(config):
    return {c.get('group', c['profile']) for s in config.selectors.values()
            for r in s['rules'] for c in r['candidates']}


class State:
    """Operator state only. One lock/atomic file; reads (including preview) do not write."""
    def __init__(self, config):
        self.config = config
        self.root = config.data / 'selection'
        self.path = self.root / 'state.json'

    def read(self):
        empty = {'version': 1, 'observations': {}, 'blocks': {}, 'history': {}}
        if not self.path.exists():
            return empty
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                raise ValueError('unsafe or oversized file')
            state = read_json(self.path)
            bounded(state)
            if set(state) != set(empty) or type(state['version']) is not int or state['version'] != 1:
                raise ValueError('invalid version/fields')
            for field in ('observations', 'blocks', 'history'):
                if not isinstance(state[field], dict) or len(state[field]) > 8192:
                    raise ValueError('invalid state table')
            for group, observation in state['observations'].items():
                self.validate_observation({'group': group, **observation}, configured=False)
            for key, block in state['blocks'].items():
                table(block, {'created_at', 'until', 'run'}, 'saved block')
                timestamp(block['created_at']); timestamp(block['until'])
                if not isinstance(block['run'], str) or len(block['run']) > 128:
                    raise ValueError('invalid run')
                if not key.startswith(('profile:', 'group:')):
                    raise ValueError('invalid block scope')
                name(key.split(':', 1)[1])
            for profile, history in state['history'].items():
                name(profile)
                table(history, {'status', 'at', 'run', 'error'}, 'history')
                timestamp(history['at'])
                if history['status'] not in ('completed', 'failed') or not isinstance(history['run'], str):
                    raise ValueError('invalid history')
                if 'error' in history and not isinstance(history['error'], dict):
                    raise ValueError('invalid history error')
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ConfigError('Unreadable or corrupt selection state') from exc

    def save(self, state):
        if any(len(state[key]) > 8192 for key in ('observations', 'blocks', 'history')):
            raise ConfigError('Selection state table exceeds 8192 entries')
        write_json(self.path, bounded(state))

    def validate_observation(self, observation, *, configured=True):
        table(observation, {'group', 'observed_at', 'expires_at', 'facts'}, 'observation')
        group = name(observation.get('group'))
        if configured and group not in groups(self.config):
            raise ConfigError('Observation group is not configured')
        timestamp(observation.get('observed_at')); timestamp(observation.get('expires_at'))
        if observation['expires_at'] <= observation['observed_at']:
            raise ConfigError('Observation expiry must follow observation time')
        attributes(observation.get('facts'))

    def observe(self, observation):
        self.validate_observation(observation)
        if observation['observed_at'] > time.time():
            raise ConfigError('Observation time is in the future')
        group = observation['group']
        with lock(self.root / 'state.lock'):
            state = self.read()
            old = state['observations'].get(group)
            body = {k: v for k, v in observation.items() if k != 'group'}
            if old and (old['observed_at'] > body['observed_at'] or
                        old['observed_at'] == body['observed_at'] and old != body):
                raise ConfigError('Observation is older than or conflicts with saved evidence')
            state['observations'][group] = body
            self.save(state)
        return observation

    def delete(self, group):
        name(group)
        with lock(self.root / 'state.lock'):
            state = self.read()
            state['observations'].pop(group, None)
            self.save(state)
        return {'deleted': group}

    def result(self, decision, *, run, error=None, at=None):
        at = time.time() if at is None else at
        profile = decision['profile']
        spec = self.config.selectors[decision['selector']]
        matched = None
        if isinstance(error, ModelFailure):
            facts = {**decision['facts'], 'error': error.evidence}
            for index, rule in enumerate(spec.get('on_error', [])):
                if evaluate(rule.get('when', {}), facts) is not True:
                    continue
                until = at + rule['seconds'] if 'seconds' in rule else error.evidence.get('retry_at')
                if type(until) not in (int, float) or until <= at:
                    continue
                matched = {'rule': index, 'until': until, 'scope': rule['scope']}
                break
        with lock(self.root / 'state.lock'):
            state = self.read()
            previous = state['history'].get(profile, {})
            if previous.get('at', 0) <= at:
                state['history'][profile] = {'status': 'failed' if error else 'completed', 'at': at, 'run': run,
                                            **({'error': error.evidence} if isinstance(error, ModelFailure) else {})}
            if matched:
                target = profile if matched['scope'] == 'profile' else decision['group']
                key = matched['scope'] + ':' + target
                old = state['blocks'].get(key, {})
                state['blocks'][key] = {'created_at': max(at, old.get('created_at', 0)),
                                       'until': max(matched['until'], old.get('until', 0)), 'run': run}
            self.save(state)
        return matched


def base_facts(project, role, snapshot, explicit=None):
    sources, attrs = {}, {}
    for source, values in (('project', project.settings.get('attributes', {})),
                           ('role', role.attributes), ('run', explicit or {})):
        for key, value in attributes(values).items():
            attrs[key], sources[key] = value, source
    attributes(attrs)
    return {'project': project.name, 'role': role.name, 'attributes': attrs,
            'task': {'goal': project.goal, 'state': snapshot.get('state', '')}}, sources


def _clip(text, maximum):
    if not isinstance(text, str):
        return ''
    return text if len(text) <= maximum else text[:maximum]


def work_inputs(project, role, snapshot, bounds):
    """Bounded current-work inputs for inference, not for rule predicates.

    Returns ``{'proposals': [...], 'evidence': {...}}``: pending (actionable)
    proposals with their current revisions, plus the latest snapshot
    verification and unacknowledged decisions routed to this role. Counts and
    text lengths follow the operator's classifier ``inputs`` bounds; unreadable
    stores read as absent rather than failing selection. History beyond these
    windows is never replayed.
    """
    cap = bounds['max_text_bytes']

    def _at(value):
        return value if isinstance(value, str) else ''

    try:
        pending = project.insights.list(pending=True, limit=bounds['max_proposals'])
    except Exception:
        pending = []
    proposals = [{'id': item.get('id'), 'rev': item.get('rev'), 'source': item.get('source'),
                  'title': _clip(item.get('title'), cap), 'at': _at(item.get('created_at'))}
                 for item in pending if isinstance(item, dict)][:bounds['max_proposals']]
    try:
        events = project.insights.decision_events(role.name, EVIDENCE_ACTIONS,
                                                  limit=bounds['max_decisions'])
    except Exception:
        events = []
    decisions = [{'insight': event.get('insight'), 'rev': event.get('rev'),
                  'action': event.get('action'), 'reason': _clip(event.get('reason'), cap),
                  'at': _at(event.get('decided_at'))}
                 for event in events if isinstance(event, dict)][:bounds['max_decisions']]
    # Both lists arrive oldest-first. Any validated bound combination must fit
    # the work-input budget, so over-budget documents shed oldest items first
    # (ties shed decisions before proposals, the actionable work) instead of
    # failing at runtime. Retention is deterministic in the recorded inputs.
    while (proposals or decisions) and len(canonical({'proposals': proposals,
                                                      'decisions': decisions})) > MAX_WORK_BYTES:
        if proposals and (not decisions or proposals[0]['at'] < decisions[0]['at']):
            proposals.pop(0)
        elif decisions:
            decisions.pop(0)
        else:
            proposals.pop(0)
    verification = snapshot.get('verification')
    # Only the verification verdict is material: snapshot ids and outcomes
    # turn over on every publication and must not invalidate the cache.
    evidence = {'snapshot': {'verified': isinstance(verification, dict)
                             and verification.get('passed') is True},
                'decisions': decisions}
    work = {'proposals': proposals, 'evidence': evidence}
    return bounded(work, MAX_WORK_BYTES)


def rule_attributes(spec, facts, sources):
    for index, rule in enumerate(spec.get('classify_rules', [])):
        if evaluate(rule.get('when', {}), facts) is True:
            for key, value in rule['attributes'].items():
                if key not in facts['attributes']:
                    facts['attributes'][key], sources[key] = value, f'rule:{index}'
    attributes(facts['attributes'])


def select(config, role, facts, sources, *, at=None, classification=None):
    at = time.time() if at is None else at
    spec = config.selectors[role.selector]
    state = State(config).read()
    observations = {}
    for group in sorted(groups(config)):
        obs = state['observations'].get(group)
        observations[group] = {'fresh': bool(obs and obs['observed_at'] <= at < obs['expires_at'])}
        if obs:
            observations[group].update({k: obs[k] for k in ('observed_at', 'expires_at')})
            if observations[group]['fresh']:
                observations[group]['facts'] = obs['facts']
    facts = {**facts, 'observations': observations, 'history': state['history']}
    # Text is an input to classification, not duplicated in every decision record.
    evidence_facts = {k: v for k, v in facts.items() if k != 'task'}
    decision = {'selector': role.selector, 'definition_sha256': digest(canonical(spec)),
                'at': at, 'task_sha256': digest(canonical(facts['task'])),
                'profile': None, 'rule': None, 'excluded': [],
                'facts': evidence_facts, 'attribute_sources': sources,
                'classification': classification, 'next_evaluation_at': at + spec['retry_seconds']}
    times = [decision['next_evaluation_at']]
    for obs in observations.values():
        if obs.get('expires_at', 0) > at:
            times.append(obs['expires_at'])
    for index, rule in enumerate(spec['rules']):
        if evaluate(rule.get('when', {}), facts) is not True:
            continue
        decision['rule'] = index
        for candidate in rule['candidates']:
            profile, group = candidate['profile'], candidate.get('group', candidate['profile'])
            candidate_facts = {**facts, 'candidate': {'profile': profile, 'group': group,
                                'history': state['history'].get(profile, {})}}
            reasons = []
            if evaluate(candidate.get('when', {}), candidate_facts) is not True:
                reasons.append('condition_false_or_unknown')
            for key in ('profile:'+profile, 'group:'+group):
                block = state['blocks'].get(key)
                if block and block['until'] > at:
                    obs = observations[group]
                    recovered = ('recover_when' in candidate and obs['fresh'] and
                                 obs['observed_at'] > block['created_at'] and
                                 evaluate(candidate['recover_when'], candidate_facts) is True)
                    if not recovered:
                        reasons.append(key)
                        times.append(block['until'])
            if reasons:
                decision['excluded'].append({'profile': profile, 'reasons': reasons})
            else:
                decision.update(profile=profile, group=group, facts={k: v for k, v in candidate_facts.items() if k != 'task'})
                break
        break
    decision['next_evaluation_at'] = min(times)
    return bounded(decision, 262144)
