"""Declared persistent directories: identities from facts, mounts and liveness.

A declaration names a directory and the facts that form its identity. The
identity is either a digest of exactly those established facts or an explicit
configured literal used only where facts cannot be established. Mount rules
are ordered, generic equalities over facts; a rule grants read-only or
writable use. Writable use is serialized at mount time by the caller's lock;
this module only decides which mode matches.

An instance is a (declaration name, identity) pair. Liveness is referential:
an instance remains while a declaration or execution references its identity
and is eligible for removal only when nothing references it. The kernel knows
no meaning of a fact, a name or a directory beyond these rules.

Declarations are validated as a whole: one bad declaration refuses the set.
Runtime facts and references are bounded and fail closed.
"""
from __future__ import annotations

import dataclasses
import unicodedata
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..errors import ConfigError, Denied
from ..fs import canonical, digest
from .queues import NAME, _name

#: Mechanism bounds, following kernel/queues.py: declarations, the fact names
#: forming an identity, and strings in facts or configured identities.
MAX_DECLARATIONS = 64
MAX_IDENTITY_KEYS = 32
MAX_STRING = 255
MAX_MOUNT_RULES = 64
MODES = ("ro", "rw")
_DECLARATION_KEYS = frozenset({"identity_from", "identity_value", "mounts"})
_MOUNT_KEYS = frozenset({"mode", "when"})


def _string(value: Any, where: str, error: type[Exception]) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_STRING
            or any(unicodedata.category(char) == "Cc" for char in value)):
        raise error(f"{where} must be a nonempty single-line string of at most "
                    f"{MAX_STRING} characters")
    return value


def _config_string(value: Any, where: str) -> str:
    return _string(value, where, ConfigError)


def _runtime_string(value: Any, where: str) -> str:
    return _string(value, where, Denied)


def _table(raw: Any, allowed: frozenset[str], where: str) -> None:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown keys in {where}: {', '.join(sorted(map(repr, unknown)))}")


@dataclasses.dataclass(frozen=True)
class MountRule:
    """One mode granted when every named fact equals its configured value."""
    mode: str
    when: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class PersistentDirectory:
    """A declared directory with a fact-derived identity and ordered mounts."""
    name: str
    identity_from: tuple[str, ...]
    identity_value: str | None
    mounts: tuple[MountRule, ...]


def _mount(raw: Any, where: str) -> MountRule:
    _table(raw, _MOUNT_KEYS, where)
    mode = raw.get("mode")
    if mode not in MODES:
        raise ConfigError(f"{where}.mode must be 'ro' or 'rw'")
    when = raw.get("when")
    if not isinstance(when, Mapping) or len(when) > MAX_IDENTITY_KEYS:
        raise ConfigError(f"{where}.when must be a table of at most "
                          f"{MAX_IDENTITY_KEYS} fact equalities")
    equalities = {_config_string(key, f"{where}.when"):
                  _config_string(value, f"{where}.when[{key!r}]")
                  for key, value in when.items()}
    return MountRule(mode=mode, when=MappingProxyType(equalities))


def parse_persistent(raw: Any) -> Mapping[str, PersistentDirectory]:
    """Validate all declared persistent directories; refuse the set on error."""
    if not isinstance(raw, Mapping):
        raise ConfigError("persistent must be a table")
    if len(raw) > MAX_DECLARATIONS:
        raise ConfigError(f"persistent has more than {MAX_DECLARATIONS} declarations")
    result = {}
    for name, spec in raw.items():
        where = f"persistent.{name}"
        _name(name, where)
        _table(spec, _DECLARATION_KEYS, where)
        raw_identity = spec.get("identity_from")
        if (not isinstance(raw_identity, list) or not raw_identity
                or len(raw_identity) > MAX_IDENTITY_KEYS):
            raise ConfigError(f"{where}.identity_from must be a nonempty list of "
                              f"at most {MAX_IDENTITY_KEYS} fact names")
        identity_from = tuple(_config_string(key, f"{where}.identity_from")
                             for key in raw_identity)
        if len(set(identity_from)) != len(identity_from):
            raise ConfigError(f"{where}.identity_from has duplicates")
        identity_value = None
        if "identity_value" in spec:
            identity_value = _config_string(spec["identity_value"], f"{where}.identity_value")
        raw_mounts = spec.get("mounts")
        if not isinstance(raw_mounts, list) or len(raw_mounts) > MAX_MOUNT_RULES:
            raise ConfigError(f"{where}.mounts must be a list of at most {MAX_MOUNT_RULES} rules")
        mounts = tuple(_mount(rule, f"{where}.mounts[{index}]")
                       for index, rule in enumerate(raw_mounts))
        result[name] = PersistentDirectory(name=name, identity_from=identity_from,
                                           identity_value=identity_value, mounts=mounts)
    return MappingProxyType(result)


def identity_for(declaration: PersistentDirectory, facts: Mapping[str, str]) -> str:
    """Digest exactly the declared identity facts, or return the configured literal."""
    if not isinstance(declaration, PersistentDirectory):
        raise Denied("Invalid persistent directory declaration")
    if declaration.identity_value is not None:
        return _runtime_string(declaration.identity_value, "identity_value")
    if not isinstance(facts, Mapping):
        raise Denied("facts must be a table")
    if len(declaration.identity_from) > MAX_IDENTITY_KEYS:
        raise Denied("identity_from exceeds its bound")
    selected = {}
    for key in declaration.identity_from:
        key = _runtime_string(key, "identity_from")
        if key not in facts:
            raise Denied(f"identity fact {key!r} is missing")
        selected[key] = _runtime_string(facts[key], f"identity fact {key!r}")
    return digest(canonical(selected))


def mount_mode(declaration: PersistentDirectory, execution_facts: Mapping[str, str]) -> str | None:
    """Mode of the first mount rule whose fact equalities all match, else None."""
    if not isinstance(declaration, PersistentDirectory):
        raise Denied("Invalid persistent directory declaration")
    if not isinstance(execution_facts, Mapping):
        raise Denied("facts must be a table")
    if len(declaration.mounts) > MAX_MOUNT_RULES:
        raise Denied("mounts exceed their bound")
    for rule in declaration.mounts:
        if not isinstance(rule, MountRule) or rule.mode not in MODES:
            raise Denied("Invalid mount rule")
        when = rule.when
        if not isinstance(when, Mapping) or len(when) > MAX_IDENTITY_KEYS:
            raise Denied("Invalid mount equalities")
        matched = True
        for key, expected in when.items():
            key = _runtime_string(key, "mount fact")
            expected = _runtime_string(expected, f"mount fact {key!r}")
            if execution_facts.get(key) != expected:
                matched = False
                break
        if matched:
            return rule.mode
    return None


def _pair(item: Any, where: str) -> tuple[str, str]:
    if not isinstance(item, (tuple, list)) or len(item) != 2:
        raise Denied(f"{where} must be a (name, identity) pair")
    name = _runtime_string(item[0], f"{where} name")
    if not NAME.fullmatch(name):
        raise Denied(f"{where} name must be a lowercase identifier")
    return (name, _runtime_string(item[1], f"{where} identity"))


def _pairs(items: Iterable[Any], where: str) -> set[tuple[str, str]]:
    # Liveness answers for any number of instances: callers pass the iterables
    # they already hold, so per-call work is bounded by what the caller can list.
    # Each pair is still validated; declarations, keys and strings stay bounded.
    try:
        iterator = iter(items)
    except TypeError:
        raise Denied(f"{where} must be iterable") from None
    return {_pair(item, where) for item in iterator}


def unreferenced(instances: Iterable[tuple[str, str]],
                 referenced: Iterable[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    """Instances with no reference, irrespective of who holds the references."""
    live = _pairs(referenced, "reference")
    return frozenset(pair for pair in _pairs(instances, "instance") if pair not in live)
