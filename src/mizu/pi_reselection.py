"""Opt-in Pi family identity and host-only, two-phase model authorization."""
import dataclasses
import secrets
import threading
import time

from .errors import ConfigError, ProtocolError
from .fs import DIGEST, ID, canonical, digest, lock, mkdir, read_json, write_json


def policy(context):
    role = getattr(context, 'reselection_role', None)
    if role is None:
        role = context.config.roles.get(context.role.name)
    spec = context.config.selectors.get(role.selector, {}) if role else {}
    return role, spec


def compatible(context, role, spec):
    from .engine_config import conversation_identity, effective
    if context.ephemeral:
        raise ConfigError('Pi re-selection requires a non-ephemeral dispatch')
    identities, settings = {}, {}
    common = None
    for rule in spec['rules']:
        for candidate in rule['candidates']:
            profile = candidate['profile']
            value = effective(context.config, role, profile)
            if value['engine'] != 'pi' or value['session'] != 'persistent':
                raise ConfigError('Pi re-selection requires persistent Pi candidates')
            identity = conversation_identity(context, value)
            shared = {**identity, 'settings': {k: v for k, v in identity['settings'].items()
                                             if k not in ('provider', 'model')}}
            if common is not None and common != shared:
                raise ConfigError('Incompatible Pi re-selection conversations')
            common = shared
            identities[profile], settings[profile] = identity, value
    return common, identities, settings


def safe_path(path, *, file=False):
    # The configured data root is trusted; reject links within the family state.
    if any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent)):
        raise ConfigError('Unsafe Pi family path')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1) and file:
        raise ConfigError('Unsafe Pi family reference')


def _family_definition(spec):
    """Session lineage is independent of polling/retry cadence policy."""
    return {key: value for key, value in spec.items()
            if key not in ('reselection_interval_seconds', 'retry_seconds',
                           'recommendation_validity')}


def _family_record(path, *, target_name=None):
    safe_path(path, file=True)
    if not path.exists():
        return None
    record = read_json(path, max_bytes=4096)
    if (not isinstance(record, dict)
            or set(record) != {'schema', 'binding', 'entry', 'target'}
            or type(record.get('schema')) is not int or record['schema'] != 1
            or not isinstance(record.get('binding'), str) or not DIGEST.fullmatch(record['binding'])
            or not isinstance(record.get('entry'), str) or not ID.fullmatch(record['entry'])
            or not isinstance(record.get('target'), str) or not DIGEST.fullmatch(record['target'])
            or target_name is not None and record['target'] != target_name):
        raise ConfigError('Invalid Pi family ownership record')
    return record


def _family_owner(target, binding, identities):
    record = _family_record(target / 'family-owner.json', target_name=target.name)
    if record is None:
        return None
    if (record['binding'] != binding or record['entry'] not in identities
            or target.name != digest(canonical(identities[record['entry']]))):
        raise ConfigError('Invalid or conflicting Pi family ownership')
    return record


# Authority-delta identities have a new family-reference namespace as well as
# new session keys. Never read/adopt old content-bound pointers on this one-time
# rekey; leave those records untouched. Current ownership still fails closed.
def _family_reference(root):
    return _family_record(root / 'families-v2' / 'family.json')


def family_directory(context, profile, settings, identity, ordinary):
    role, spec = policy(context)
    if not spec.get('reselection_interval_seconds', 0):
        if (settings.get('engine') != 'pi' or settings.get('session') != 'persistent'
                or context.ephemeral):
            return ordinary
        root = ordinary.parent
        safe_path(root)
        owner = _family_record(ordinary / 'family-owner.json', target_name=ordinary.name)
        refs = root / 'families-v2'
        safe_path(refs)
        if refs.exists() and not refs.is_dir():
            raise ConfigError('Invalid Pi family reference directory')
        pointer = _family_reference(root) if refs.is_dir() else None
        if pointer is not None and pointer['target'] != ordinary.name:
            pointer = None
        if owner is not None and pointer is not None and owner != pointer:
            raise ConfigError('Conflicting Pi family ownership records')
        record = owner or pointer
        if record is None:
            return ordinary
        # Opting out never appends to history that may contain another
        # candidate. Start a stable isolated profile session; re-enabling the
        # same family still resolves its unchanged target reference.
        key = digest(canonical({'disabled_selector': role.selector if role else '',
                                'role': role.name if role else context.role.name,
                                'family_binding': record['binding'],
                                'identity': identity}))
        isolated = root / key
        safe_path(isolated)
        if isolated.exists() and not isolated.is_dir():
            raise ConfigError('Invalid isolated Pi session directory')
        return isolated
    common, identities, _ = compatible(context, role, spec)
    decision = getattr(context, 'reselection_decision', None)
    # Ordinary Pi identity already excludes current grants/native tools, so a
    # finish-only continuation needs no identity rewrite. The dispatch selector,
    # model compatibility and family ownership checks below still apply; the
    # current registration and Bridge, never historical declarations, admit tools.
    if (not isinstance(decision, dict) or decision.get('selector') != role.selector
            or decision.get('definition_sha256') != digest(canonical(spec))
            or decision.get('profile') != profile or profile not in identities
            or identity != identities[profile]):
        raise ConfigError('Pi family requires the authoritative dispatch selection')
    binding = digest(canonical({'selector': role.selector,
                                'definition': _family_definition(spec),
                                'project': context.project.name, 'role': role.name,
                                'conversation': common}))
    root = ordinary.parent
    safe_path(root)
    if root.exists() and not root.is_dir():
        raise ConfigError('Invalid Pi session root')
    refs = root / 'families-v2'
    # One active pointer per role bounds family state across config revisions;
    # its binding makes selector edits fail closed.
    reference = refs / 'family.json'
    safe_path(reference, file=True)
    mkdir(refs)
    with lock(refs / 'lock'):
        if reference.exists():
            record = _family_record(reference)
            if (record['binding'] != binding or record['entry'] not in identities
                    or record['target'] != digest(canonical(identities[record['entry']]))):
                raise ConfigError('Invalid Pi family reference')
            target = root / record['target']
            safe_path(target)
            if not target.is_dir() or _family_owner(target, binding, identities) != record:
                raise ConfigError('Dangling or unowned Pi family reference')
            return target

        # A target directory owns one family lineage. Search only this
        # selector's bounded candidate set, so a crash after the ownership
        # claim but before the reference write recovers the same target even
        # if a different candidate is selected on restart. A conflicting
        # family never adopts or aliases that mutable history.
        owners = []
        for candidate, candidate_identity in identities.items():
            candidate_dir = root / digest(canonical(candidate_identity))
            safe_path(candidate_dir)
            if candidate_dir.exists() and not candidate_dir.is_dir():
                raise ConfigError('Invalid Pi candidate session directory')
            if candidate_dir.is_dir():
                owner = _family_owner(candidate_dir, binding, identities)
                if owner is not None:
                    owners.append(owner)
        if owners:
            record = owners[0]
            if any(owner != record for owner in owners):
                raise ConfigError('Conflicting Pi family ownership records')
            target = root / record['target']
            safe_path(target)
            if not target.is_dir():
                raise ConfigError('Dangling Pi family ownership record')
            write_json(reference, record, exclusive=True)
            return target

        # First use adopts only the authoritative entry candidate's exact
        # same-source directory, or initializes that directory. No other
        # profile history is searched or merged.
        safe_path(ordinary)
        if ordinary.exists() and not ordinary.is_dir():
            raise ConfigError('Invalid Pi candidate session directory')
        mkdir(ordinary)
        record = {'schema': 1, 'binding': binding,
                  'entry': profile, 'target': ordinary.name}
        write_json(ordinary / 'family-owner.json', record, exclusive=True)
        write_json(reference, record, exclusive=True)
        return ordinary


class Reselection:
    """One driver connection; no model tools. Failed/uncertain exchanges poison admission.

    _pi_reselect prepares one exact candidate; _pi_reselect_applied commits only
    after the adapter verifies AgentSession and transcript projection. No retry.
    Existing _budget remains the sole request admission/spend path.
    """
    def __init__(self, context, *, clock=time.monotonic):
        self.context, self.clock = context, clock
        self.role, self.spec = policy(context)
        self.enabled = bool(self.spec.get('reselection_interval_seconds', 0))
        self.pending = None
        self.failed = False
        self.mutex = threading.Lock()
        self.checked = clock()
        self.session_path = self.session_file = self.saved_session = None
        if self.enabled:
            self.common, self.identities, self.settings = compatible(context, self.role, self.spec)
            self.decision = context.reselection_decision
            self.definition = digest(canonical(self.spec))
            if (not isinstance(self.decision, dict)
                    or self.decision.get('selector') != self.role.selector
                    or self.decision.get('definition_sha256') != self.definition):
                raise ConfigError('Pi re-selection definition changed')

    def bind_session(self, path, session_file, saved):
        if not self.enabled:
            return
        if not isinstance(session_file, str) or not session_file or len(session_file) > 4096:
            raise ProtocolError('Pi family session file is unavailable')
        self.session_path, self.session_file, self.saved_session = path, session_file, saved

    def checkpoint(self):
        if not self.enabled or self.session_file is None:
            return
        from .engine_config import save_session
        from .pi import _cumulative_tokens
        save_session(self.session_path, self.session_file,
                     {'tokens': _cumulative_tokens(self.saved_session, self.context)})

    def handle(self, operation, arguments):
        if not self.enabled or operation not in ('_pi_reselect', '_pi_reselect_applied', '_budget'):
            return self.context.handle(operation, arguments)
        with self.mutex:
            if operation == '_budget' and not self.failed and self.pending is None:
                # Preserve existing budget refusals and their exception identity.
                return self.context.handle(operation, arguments)
            try:
                if self.failed or self.pending is not None and operation != '_pi_reselect_applied':
                    raise ProtocolError('Pi model authorization is uncertain')
                if operation == '_pi_reselect_applied':
                    if self.pending is None or arguments != {**self.pending['model'], 'authorization': self.pending['authorization']}:
                        raise ProtocolError('Pi model application mismatch')
                    decision = self.pending['decision']
                    write_json(self.context.run_dir / 'selection.json', decision)
                    self.decision.clear()
                    self.decision.update(decision)
                    self.context.role = dataclasses.replace(self.context.role, profile=decision['profile'])
                    self.context.model_evidence.update(profile=decision['profile'], **self.pending['model'])
                    self.pending = None
                    return {'applied': True}
                if arguments != {}:
                    raise ProtocolError('Pi re-selection accepts no candidate input')
                # Persist the SDK's already-completed turn before this boundary
                # can authorize another model or fail. This binds the native
                # session file ID to the family for a later dispatch.
                self.checkpoint()
                if digest(canonical(self.spec)) != self.definition:
                    raise ProtocolError('Pi re-selection definition changed')
                if getattr(self.context, 'turn_continuation', False):
                    return {'switch': None}
                now = self.clock()
                if now - self.checked < self.spec['reselection_interval_seconds']:
                    return {'switch': None}
                self.checked = now
                common, identities, settings = compatible(self.context, self.role, self.spec)
                if (common != self.common or identities != self.identities or settings != self.settings):
                    raise ProtocolError('Pi candidate configuration changed during dispatch')
                from .selection import select
                candidate = select(self.context.config, self.role, self.decision['reselection_facts'],
                                   self.decision['attribute_sources'], classification=self.decision['classification'])
                current = self.decision['profile']
                def rank(decision):
                    rule = decision['rule']
                    return (rule, [c['profile'] for c in self.spec['rules'][rule]['candidates']].index(decision['profile']))
                if not candidate['profile'] or rank(candidate) >= rank(self.decision) or candidate['profile'] == current:
                    return {'switch': None}
                model = {k: settings[candidate['profile']][k] for k in ('provider', 'model')}
                authorization = secrets.token_hex(16)
                self.pending = {'decision': candidate, 'model': model, 'authorization': authorization}
                return {'switch': {**model, 'authorization': authorization,
                                   'options': settings[candidate['profile']]['options']}}
            except Exception:
                self.failed = True
                self.context.admission_error = 'Pi model authorization failed'
                raise
