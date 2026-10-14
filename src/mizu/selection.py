"""Declarative selection: bounded facts and predicates, no provider policy or IO callbacks."""
from __future__ import annotations

import copy
import math
import time

from .errors import ConfigError, ModelFailure
from .fs import DIGEST, ID, canonical, digest, lock, read_json, text_preview, write_json

MAX_BYTES = 1024 * 1024
MAX_ITEMS = 64
MAX_STATE_ITEMS = 8192
MAX_TERMINAL_CONTRIBUTIONS = 256
MAX_PROBE_EVENTS = 8
INTENT_RESERVE_BYTES = 512
INTENT_TERMINAL_RESERVE_BYTES = 128
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
        table(spec, {'rules', 'classify_rules', 'classifier', 'on_error', 'retry_seconds',
                     'recommendation_validity'}, 'selector')
        seconds(spec.get('retry_seconds'))
        validity = spec.get('recommendation_validity', 'once')
        if validity not in ('once', 'sticky'):
            raise ConfigError('recommendation_validity must be once or sticky')
        spec['recommendation_validity'] = validity
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
            table(rule, {'when', 'scope', 'seconds', 'until', 'probe_seconds'}, 'error rule')
            validate_condition(rule.get('when', {}))
            if rule.get('scope') not in ('profile', 'group'):
                raise ConfigError('Error scope must be profile or group')
            if 'probe_seconds' in rule:
                seconds(rule['probe_seconds'])
                if 'until' in rule:
                    raise ConfigError('Probe rules cannot use until')
                if 'seconds' in rule:
                    seconds(rule['seconds'])
                continue
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
    """Selection evidence. One lock/atomic file; reads (including preview) do not write."""
    def __init__(self, config):
        self.config = config
        self.root = config.data / 'selection'
        self.path = self.root / 'state.json'

    def read(self):
        empty = {'version': 2, 'observations': {}, 'blocks': {}, 'history': {},
                 'contributions': {}, 'receipts': {}, 'events': {}, 'exhausted': False}
        if not self.path.exists():
            return empty
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                raise ValueError('unsafe or oversized file')
            state = read_json(self.path)
            bounded(state)
            if state.get('version') == 1 and type(state['version']) is int:
                if set(state) != {'version', 'observations', 'blocks', 'history'}:
                    raise ValueError('invalid legacy fields')
                state = {**empty, **state, 'version': 2}
                try:
                    bounded(state)
                except ConfigError:
                    # A full legacy file cannot accommodate new ledger fields.
                    # Retain it unchanged and expose a read-time capacity latch.
                    state['exhausted'] = True
            if set(state) != set(empty) or type(state['version']) is not int or state['version'] != 2:
                raise ValueError('invalid version/fields')
            for field in ('observations', 'blocks', 'history', 'contributions', 'receipts', 'events'):
                if not isinstance(state[field], dict) or len(state[field]) > MAX_STATE_ITEMS:
                    raise ValueError('invalid state table')
            for group, observation in state['observations'].items():
                self.validate_observation({'group': group, **observation}, configured=False)
            for key, block in state['blocks'].items():
                table(block, {'created_at', 'until', 'run', 'selector', 'rule', 'error_rule', 'sponsor', 'role'}, 'saved block')
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
            if type(state['exhausted']) is not bool:
                raise ValueError('invalid capacity latch')
            for identity, receipt in state['receipts'].items():
                if (not DIGEST.fullmatch(identity) or set(receipt) != {'payload', 'action'}
                        or not DIGEST.fullmatch(receipt['payload']) or not isinstance(receipt['action'], dict)):
                    raise ValueError('invalid probe receipt')
                action = receipt['action']
                fields = {'rule', 'until', 'scope', 'probe_seconds', 'contribution'}
                if (set(action) != fields or action['scope'] not in ('profile', 'group')
                        or type(action['rule']) is not int or not 0 <= action['rule'] < MAX_ITEMS):
                    raise ValueError('invalid probe receipt action fields')
                seconds(action['probe_seconds'])
                if action['contribution'] != identity or identity not in state['contributions']:
                    raise ValueError('invalid receipt contribution')
                if action['until'] is not None:
                    timestamp(action['until'])
            for identity, contribution in state['contributions'].items():
                validate_contribution(identity, contribution)
                if identity not in state['receipts']:
                    raise ValueError('missing contribution receipt')
            for identity, event in state['events'].items():
                validate_event(identity, event)
            for identity, c in state['contributions'].items():
                for field in ('claim', 'last_event'):
                    if c[field] is not None and (c[field] not in state['events']
                            or state['events'][c[field]]['contribution'] != identity):
                        raise ValueError('missing/mismatched event reference')
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ConfigError('Unreadable or corrupt selection state') from exc

    def save(self, state):
        prune_terminal_contributions(state)
        if any(len(state[key]) > MAX_STATE_ITEMS for key in
               ('observations', 'blocks', 'history', 'contributions', 'receipts', 'events')):
            raise ConfigError('Selection state table exceeds 8192 entries')
        # Reserve room for a capacity latch even when the next write cannot fit.
        bounded({**state, 'exhausted': False})
        write_json(self.path, bounded(state))

    def save_or_close(self, state, previous):
        try:
            self.save(state)
        except ConfigError:
            previous['exhausted'] = True
            self.save(previous)
            raise ConfigError('Selection ledger capacity exhausted; active obligations retained, selection closed')

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

    def result(self, decision, *, run, attempt=0, error=None, at=None):
        """Persist an idempotent receipt only for a matched probe failure.

        Time is deliberately excluded from the payload; while retained, the
        first accounting time owns the probe action. Conflicting reports fail closed.
        """
        if not isinstance(run, str) or not 1 <= len(run) <= 128 or type(attempt) is not int or not 0 <= attempt <= 64:
            raise ConfigError('Invalid selection attempt identity')
        at = time.time() if at is None else timestamp(at)
        profile = decision['profile']
        identity = digest(canonical({'project': decision['facts'].get('project'),
                                    'role': decision['facts'].get('role'), 'run': run, 'attempt': attempt}))
        payload = digest(canonical({'decision': decision, 'error': error.evidence if isinstance(error, ModelFailure)
                                    else ('local_error' if error else None)}))
        with lock(self.root / 'state.lock'):
            previous_state = self.read()
            state = copy.deepcopy(previous_state)
            receipt = state['receipts'].get(identity)
            if receipt:
                if receipt['payload'] != payload:
                    raise ConfigError('Conflicting selection attempt replay')
                return receipt['action']
            if state['exhausted']:
                raise ConfigError('Selection ledger capacity exhausted')
            spec = self.config.selectors[decision['selector']]
            matched = None
            if isinstance(error, ModelFailure):
                facts = {**decision['facts'], 'error': error.evidence}
                for index, rule in enumerate(spec.get('on_error', [])):
                    if evaluate(rule.get('when', {}), facts) is not True:
                        continue
                    if 'probe_seconds' in rule:
                        until = at + rule['seconds'] if 'seconds' in rule else None
                        matched = {'rule': index, 'until': until, 'scope': rule['scope'],
                                   'probe_seconds': rule['probe_seconds'], 'contribution': identity}
                    else:
                        until = at + rule['seconds'] if 'seconds' in rule else error.evidence.get('retry_at')
                        if type(until) not in (int, float) or until <= at:
                            continue
                        matched = {'rule': index, 'until': until, 'scope': rule['scope']}
                    break
            previous = state['history'].get(profile, {})
            if previous.get('at', 0) <= at:
                state['history'][profile] = {'status': 'failed' if error else 'completed', 'at': at, 'run': run,
                                            **({'error': error.evidence} if isinstance(error, ModelFailure) else {})}
            if matched:
                target = profile if matched['scope'] == 'profile' else decision['group']
                key = matched['scope'] + ':' + target
                if 'probe_seconds' in matched:
                    from .probes import target_binding, sponsor_binding
                    sponsor, role = decision['facts']['project'], decision['facts']['role']
                    binding = decision.get('probe_target') or target_binding(self.config, profile)
                    state['contributions'][identity] = {
                        'key': key, 'profile': profile, 'sponsor': sponsor, 'role': role,
                        'sponsor_binding': decision.get('probe_sponsor') or sponsor_binding(self.config, sponsor, role),
                        'target': binding, 'selector': decision['selector'], 'rule': decision['rule'],
                        'error_rule': matched['rule'], 'created_at': at, 'until': matched['until'],
                        'probe_seconds': matched['probe_seconds'], 'next_probe_at': at + matched['probe_seconds'],
                        'status': 'active', 'revision': 1, 'claim': None, 'last_event': None, 'reason': None}
                else:
                    old = state['blocks'].get(key, {})
                    state['blocks'][key] = {'created_at': max(at, old.get('created_at', 0)),
                                           'until': max(matched['until'], old.get('until', 0)), 'run': run,
                                           'selector': decision['selector'], 'rule': decision['rule'],
                                           'error_rule': matched['rule'], 'sponsor': decision['facts'].get('project'),
                                           'role': decision['facts'].get('role')}
            if matched and 'probe_seconds' in matched:
                state['receipts'][identity] = {'payload': payload, 'action': matched}
            self.save_or_close(state, previous_state)
        return matched

    def probe_progress(self, identity, *, revision, claim, requests, model=None):
        """Persist admission/usage on the intent too, so crashes retain spend evidence."""
        with lock(self.root / 'state.lock'):
            previous = self.read()
            state = copy.deepcopy(previous)
            c = state['contributions'].get(identity)
            e = state['events'].get(claim)
            if not c or c['revision'] != revision or c['claim'] != claim or not e or e['status'] != 'intent':
                raise ConfigError('Recovery claim lost during admission')
            state['events'][claim] = consume_intent_reservation(
                e, {'requests': requests, 'admitted': requests > 0, 'model': model})
            e = state['events'][claim]
            validate_event(claim, e)
            self.save_or_close(state, previous)

    def probe_update(self, identity, *, revision, claim, event=None, recover=False, at=None, reason=None):
        """Fence a terminal event/recovery against its exact contribution and claim.

        Terminal evidence is a separate durable write *before* recovery. No
        caller may turn an intent/unknown/failure into recovery.
        """
        at = time.time() if at is None else timestamp(at)
        with lock(self.root / 'state.lock'):
            previous = self.read()
            state = copy.deepcopy(previous)
            c = state['contributions'].get(identity)
            if not c or c['revision'] != revision or c['claim'] != claim or c['status'] != 'active':
                return False
            if event is not None:
                validate_event(claim, event)
                old = state['events'].get(claim)
                if not old or old['status'] != 'intent' or event['contribution'] != identity or event['revision'] != revision:
                    return False
                if len(canonical(event)) > len(canonical(old)):
                    raise ConfigError('Probe terminal evidence exceeds its intent reservation')
                state['events'][claim] = event
                c['last_event'] = claim
                prune_probe_events(state, identity)
                self.save_or_close(state, previous)
                return True
            e = state['events'].get(claim)
            if (not e or e['status'] == 'intent' or e['contribution'] != identity
                    or e['revision'] != revision or e['target'] != c['target']):
                return False
            if recover:
                if (e['status'] != 'success' or not e['admitted'] or e['requests'] < 1 or not e['settled'] or not e['challenge_ok']
                        or e['target'] != c['target'] or not active(c, at)):
                    return False
                c['status'] = 'recovered'
            elif c['until'] is not None and c['until'] <= at:
                c['status'] = 'expired'
            c.update(claim=None, last_event=claim, revision=revision + 1,
                     next_probe_at=at + c['probe_seconds'], reason=reason)
            prune_probe_events(state, identity)
            self.save_or_close(state, previous)
            return True


CONTRIBUTION_FIELDS = {'key', 'profile', 'sponsor', 'role', 'sponsor_binding', 'target', 'selector', 'rule',
                       'error_rule', 'created_at', 'until', 'probe_seconds', 'next_probe_at', 'status',
                       'revision', 'claim', 'last_event', 'reason'}
EVENT_FIELDS = {'contribution', 'revision', 'target', 'started_at', 'finished_at', 'status',
                'admitted', 'settled', 'challenge_ok', 'requests', 'model', 'reason'}


def validate_contribution(identity, c):
    if not DIGEST.fullmatch(identity) or set(c) != CONTRIBUTION_FIELDS:
        raise ConfigError('Invalid probe contribution')
    for field in ('profile', 'sponsor', 'role', 'selector'):
        name(c[field])
    if not c['key'].startswith(('profile:', 'group:')):
        raise ConfigError('Invalid probe scope')
    name(c['key'].split(':', 1)[1])
    for field in ('target', 'sponsor_binding'):
        if not isinstance(c[field], str) or not DIGEST.fullmatch(c[field]):
            raise ConfigError('Invalid probe binding')
    for field in ('created_at', 'next_probe_at'):
        timestamp(c[field])
    if c['until'] is not None:
        timestamp(c['until'])
    seconds(c['probe_seconds'])
    if c['status'] not in ('active', 'recovered', 'expired') or type(c['revision']) is not int or c['revision'] < 1:
        raise ConfigError('Invalid contribution revision/status')
    for field in ('rule', 'error_rule'):
        if type(c[field]) is not int or not 0 <= c[field] < MAX_ITEMS:
            raise ConfigError('Invalid probe rule')
    for field in ('claim', 'last_event'):
        if c[field] is not None and (not isinstance(c[field], str) or not DIGEST.fullmatch(c[field])):
            raise ConfigError('Invalid probe event reference')
    if c['reason'] is not None:
        name(c['reason'])


def validate_event(identity, e):
    fields = EVENT_FIELDS | ({'reserve'} if isinstance(e, dict) and e.get('status') == 'intent' else set())
    if not DIGEST.fullmatch(identity) or not isinstance(e, dict) or set(e) != fields:
        raise ConfigError('Invalid probe event')
    if ('reserve' in e and (not isinstance(e['reserve'], str)
                            or not INTENT_TERMINAL_RESERVE_BYTES <= len(e['reserve']) <= INTENT_RESERVE_BYTES
                            or e['reserve'].strip())):
        raise ConfigError('Invalid probe intent reservation')
    bounded(e, 4096)
    for field in ('contribution', 'target'):
        if not isinstance(e[field], str) or not DIGEST.fullmatch(e[field]):
            raise ConfigError('Invalid probe event binding')
    if type(e['revision']) is not int or e['revision'] < 1:
        raise ConfigError('Invalid probe event revision')
    timestamp(e['started_at'])
    if e['finished_at'] is not None:
        timestamp(e['finished_at'])
    if e['status'] not in ('intent', 'success', 'failure', 'cancelled', 'unknown', 'deferred'):
        raise ConfigError('Invalid probe event status')
    for field in ('admitted', 'settled', 'challenge_ok'):
        if type(e[field]) is not bool:
            raise ConfigError('Invalid probe event verdict')
    if type(e['requests']) is not int or not 0 <= e['requests'] <= 4:
        raise ConfigError('Invalid probe admissions')
    if e['reason'] is not None:
        name(e['reason'])
    if e['model'] is not None:
        model = e['model']
        if (not isinstance(model, dict) or set(model) != {'usage_known', 'tokens'}
                or type(model['usage_known']) is not bool or not isinstance(model['tokens'], dict)
                or set(model['tokens']) - {'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'other_tokens'}
                or any(type(v) not in (int, float) or not 0 <= v <= 2**53 for v in model['tokens'].values())):
            raise ConfigError('Invalid probe model evidence')


def consume_intent_reservation(event, updates):
    """Replace reserved intent bytes with progress evidence without growing the record."""
    if event.get('status') != 'intent' or not isinstance(event.get('reserve'), str):
        raise ConfigError('Recovery intent reservation is unavailable')
    updated = {**event, **updates}
    growth = len(canonical(updated)) - len(canonical(event))
    reserve = event['reserve']
    if growth > len(reserve) - INTENT_TERMINAL_RESERVE_BYTES:
        raise ConfigError('Recovery progress exceeds its intent reservation')
    if growth > 0:
        updated['reserve'] = reserve[:-growth]
    return updated


def terminal_intent(event, updates):
    """Remove intent padding and require terminal evidence to fit the saved claim."""
    updated = {**event, **updates}
    updated.pop('reserve', None)
    if len(canonical(updated)) > len(canonical(event)):
        raise ConfigError('Probe terminal evidence exceeds its intent reservation')
    return updated


def prune_probe_events(state, identity):
    """Keep the latest eight events, without deleting the active or visible result."""
    contribution = state['contributions'].get(identity)
    if not contribution:
        return
    events = [(key, event) for key, event in state['events'].items()
              if event['contribution'] == identity]
    protected = {key for key in (contribution['claim'], contribution['last_event']) if key}
    protected.update(key for key, event in events if event['status'] == 'intent')
    removable = sorted((event['started_at'], event['revision'], key) for key, event in events
                       if event['status'] != 'intent' and key not in protected)
    excess = max(0, len(events) - MAX_PROBE_EVENTS)
    for _, _, key in removable[:excess]:
        state['events'].pop(key, None)


def prune_terminal_contributions(state):
    """Retain only the 256 newest terminal probe obligations and their evidence."""
    terminal = sorted((c['created_at'], identity) for identity, c in state['contributions'].items()
                      if c['status'] in ('recovered', 'expired'))
    for _, identity in terminal[:-MAX_TERMINAL_CONTRIBUTIONS]:
        state['contributions'].pop(identity, None)
        state['receipts'].pop(identity, None)
        for event_id in [key for key, event in state['events'].items()
                         if event['contribution'] == identity]:
            state['events'].pop(event_id, None)


def active(contribution, at):
    return contribution['status'] == 'active' and (contribution['until'] is None or contribution['until'] > at)


def blocks_for(state, key, at):
    fixed = state['blocks'].get(key)
    return ([fixed] if fixed and fixed['until'] > at else []) + [
        c for c in state['contributions'].values() if c['key'] == key and active(c, at)]


def project_status(config, project, *, at=None):
    """Only relevant block metadata; never shared history/errors or task facts."""
    at = time.time() if at is None else at
    state = State(config).read()
    keys = set()
    for role_name in project.roles:
        role = config.roles[role_name]
        if role.selector:
            for rule in config.selectors[role.selector]['rules']:
                for c in rule['candidates']:
                    keys.update(('profile:' + c['profile'], 'group:' + c.get('group', c['profile'])))
    fixed = [{'key': k, **v} for k, v in state['blocks'].items() if k in keys and v['until'] > at]
    # Local display-only caches. Admission always performs uncached checks.
    from .probes import sponsor_authorization, target_authorization
    sponsors, targets = {}, {}
    probes = []
    for identity, c in state['contributions'].items():
        if not active(c, at) or not (c['key'] in keys or c['sponsor'] == project.name):
            continue
        e = state['events'].get(c['claim'] or c['last_event'])
        sponsor_key = digest(canonical([c['sponsor'], c['role'], c['sponsor_binding']]))
        target_key = digest(canonical([c['profile'], c['target']]))
        if sponsor_key not in sponsors:
            sponsors[sponsor_key] = sponsor_authorization(config, c)[0]
        reason = sponsors[sponsor_key]
        if reason is None:
            if target_key not in targets:
                targets[target_key] = target_authorization(config, c)
            reason = targets[target_key]
        if e is not None:
            e = {key: value for key, value in e.items() if key != 'reserve'}
        probes.append({'contribution': identity, **c, 'last_result': e, 'authorization_reason': reason})
    return {'exhausted': state['exhausted'], 'fixed_blocks': fixed, 'probe_blocks': probes,
            'next_probe_at': min((c['next_probe_at'] for c in probes), default=None),
            'ledger_entries': len(state['receipts']), 'event_entries': len(state['events'])}


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
    preview, _ = text_preview(text, maximum)
    return preview


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
    verification = snapshot.get('verification')
    # Only the verification verdict is material: snapshot ids and outcomes
    # turn over on every publication and must not invalidate the cache.
    snapshot_evidence = {'verified': isinstance(verification, dict)
                         and verification.get('passed') is True}
    # Both lists arrive oldest-first. Any validated bound combination must fit
    # the work-input budget, so over-budget documents shed oldest items first
    # (ties shed decisions before proposals, the actionable work) instead of
    # failing at runtime. The guard measures the exact returned shape, wrapper
    # included. Retention is deterministic in the recorded inputs.
    work = {'proposals': proposals, 'evidence': {'snapshot': snapshot_evidence,
                                                 'decisions': decisions}}
    while (proposals or decisions) and len(canonical(work)) > MAX_WORK_BYTES:
        if proposals and (not decisions or proposals[0]['at'] < decisions[0]['at']):
            proposals.pop(0)
        elif decisions:
            decisions.pop(0)
        else:
            proposals.pop(0)
        work = {'proposals': proposals, 'evidence': {'snapshot': snapshot_evidence,
                                                     'decisions': decisions}}
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
            if state['exhausted']:
                reasons.append('selection_capacity_exhausted')
            for key in ('profile:'+profile, 'group:'+group):
                for block in blocks_for(state, key, at):
                    obs = observations[group]
                    recovered = ('recover_when' in candidate and obs['fresh'] and
                                 obs['observed_at'] > block['created_at'] and
                                 evaluate(candidate['recover_when'], candidate_facts) is True)
                    if not recovered:
                        if key not in reasons:
                            reasons.append(key)
                        if block['until'] is not None:
                            times.append(block['until'])
                        if block.get('next_probe_at', 0) > at:
                            times.append(block['next_probe_at'])
            if reasons:
                decision['excluded'].append({'profile': profile, 'reasons': reasons})
            else:
                decision.update(profile=profile, group=group, facts={k: v for k, v in candidate_facts.items() if k != 'task'})
                break
        break
    decision['next_evaluation_at'] = min(times)
    if decision['profile'] and any('probe_seconds' in r for r in spec.get('on_error', [])):
        from .probes import target_binding, sponsor_binding
        # Capture the authority BEFORE execution, not after mutable native files
        # may have changed during the failed request. Preview reads only.
        decision['probe_target'] = target_binding(config, decision['profile'])
        decision['probe_sponsor'] = sponsor_binding(config, facts['project'], facts['role'])
    return bounded(decision, 262144)


#: Pre-execution wait coalescing: unchanged selection waits share one audit
#: run instead of one directory per idle poll. Time-varying fields only.
WAIT_EPOCH_FIELDS = ('at', 'next_evaluation_at')


def wait_stable(decision):
    """Stable wait reason: a decision without its time-varying fields."""
    if not isinstance(decision, dict):
        return {}
    return {key: value for key, value in decision.items() if key not in WAIT_EPOCH_FIELDS}


def wait_fingerprint(decision):
    """Digest of the stable wait reason; time slides never change it."""
    return digest(canonical(wait_stable(decision)))


def wait_path(project, role_name):
    """Per-project, per-role last-wait record (alongside classification caches)."""
    return project.root / 'selection' / (role_name + '-wait.json')


def _audit_run_intact(project, run):
    """True when the named audit run still carries its wait evidence."""
    try:
        run_dir = project.root / 'runs' / run
        selection = run_dir / 'selection.json'
        result = run_dir / 'result.json'
        for path in (selection, result):
            if path.is_symlink() or not path.is_file():
                return False
        saved = read_json(result, None)
        if not isinstance(saved, dict):
            return False
        if saved.get('run') != run or saved.get('status') != 'waiting':
            return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def read_wait(project, role_name):
    """Last recorded wait whose audit run is still intact, else None.

    Never raises: a missing/unreadable record, a dangling pointer (audit
    run removed by retention/GC/restore/cleanup), or a record naming a
    non-waiting run all read as "no coalescible wait" so the caller
    audits a fresh run instead of reusing a stale pointer.
    """
    try:
        record = read_json(wait_path(project, role_name), None)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(record, dict):
        return None
    if not isinstance(record.get('fingerprint'), str) or not DIGEST.fullmatch(record['fingerprint']):
        return None
    if not isinstance(record.get('run'), str) or not record['run']:
        return None
    try:
        if not _audit_run_intact(project, record['run']):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return record


def write_wait(project, role_name, fingerprint, run, next_evaluation_at):
    """Record the audited wait; overwrites any prior record."""
    from pathlib import Path as _Path
    path = wait_path(project, role_name)
    directory = path.parent
    if not isinstance(directory, _Path):
        raise ConfigError('Invalid wait record path')
    import os as _os
    try:
        _os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f'Cannot record selection wait: {exc}') from exc
    record = {'fingerprint': fingerprint, 'run': run,
              'next_evaluation_at': next_evaluation_at, 'version': 1}
    write_json(path, bounded(record, 262144))


def clear_wait(project, role_name):
    """Forget the last wait after a dispatch; raises on failure.

    Callers must fall back to :func:`invalidate_wait` so a failed clear
    never leaves a stale pointer that falsely coalesces the next wait.
    """
    wait_path(project, role_name).unlink(missing_ok=True)


def invalidate_wait(project, role_name):
    """Poison the wait record so the next wait re-audits; never raises."""
    try:
        write_wait(project, role_name, digest(canonical({})), '', 0)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
