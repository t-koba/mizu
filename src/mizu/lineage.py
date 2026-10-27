"""Bounded, adapter-observed lineage facts; never a promotion authority."""
from __future__ import annotations

import datetime as dt
import re

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPO = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
SCHEMA = 1
BOUND_SCHEMA = 2
_FIELDS = {
    "schema", "repository", "remote", "target_ref", "target_oid",
    "published_oid", "merge_bases", "target_is_ancestor", "observed_at",
}
EXPECTED_FIELDS = {
    "expected_repository", "expected_remote", "expected_target_ref",
    "expected_version",
}
_BOUND_FIELDS = _FIELDS | EXPECTED_FIELDS
TARGET_VERSION_MIN = 1
TARGET_VERSION_MAX = 1073741824
TARGET_REF_PREFIX = "refs/heads/"
TARGET_BRANCH_MAX = 256
TARGET_REF_MAX = len(TARGET_REF_PREFIX) + TARGET_BRANCH_MAX
TARGET_REMOTE_MAX = 256
_TARGET_FIELDS = {"repository", "remote", "target_ref", "version"}


def _valid_remote(value) -> bool:
    """Accept a bounded remote name, not a URL or option-like string."""
    if not isinstance(value, str) or len(value) > TARGET_REMOTE_MAX:
        return False
    if not _REMOTE.fullmatch(value):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def _valid_target_ref(value) -> bool:
    """Accept a full refs/heads name with Git's unsafe ref forms excluded."""
    if (not isinstance(value, str) or not value.startswith(TARGET_REF_PREFIX)
            or len(value) > TARGET_REF_MAX):
        return False
    branch = value[len(TARGET_REF_PREFIX):]
    if not branch or len(branch) > TARGET_BRANCH_MAX:
        return False
    if (branch == "@" or ".." in branch or "@{" in branch
            or branch.endswith(".")):
        return False
    for component in branch.split("/"):
        if not component or component.startswith(".") or component.endswith(".lock"):
            return False
    for char in branch:
        code = ord(char)
        if (code <= 0x20 or 0x7F <= code <= 0x9F
                or char in "~^:?*[\\"
                or char in "\u200b\u200c\u200d\u200e\u200f"
                "\u202a\u202b\u202c\u202d\u202e"
                "\u2066\u2067\u2068\u2069\ufeff"):
            return False
    return True


def check_target(value):
    """Return the exact typed configured target, or None if it is malformed."""
    if not isinstance(value, dict) or set(value) != _TARGET_FIELDS:
        return None
    repository = value.get("repository")
    remote = value.get("remote")
    target_ref = value.get("target_ref")
    version = value.get("version")
    if not isinstance(repository, str) or not _REPO.fullmatch(repository):
        return None
    if not _valid_remote(remote) or not _valid_target_ref(target_ref):
        return None
    if type(version) is not int or not TARGET_VERSION_MIN <= version <= TARGET_VERSION_MAX:
        return None
    return {"repository": repository, "remote": remote,
            "target_ref": target_ref, "version": version}


def _check_common(value, published_oid):
    """Validate shared immutable observation fields after their shape is known."""
    bases = value["merge_bases"]
    if (not isinstance(bases, list) or len(bases) != 1
            or not isinstance(bases[0], str) or not _OID.fullmatch(bases[0])):
        return None
    for key in ("target_oid", "published_oid"):
        if not isinstance(value[key], str) or not _OID.fullmatch(value[key]):
            return None
    if (not isinstance(published_oid, str) or not _OID.fullmatch(published_oid)
            or len(value["target_oid"]) != len(value["published_oid"])
            or len(bases[0]) != len(value["published_oid"])
            or value["published_oid"] != published_oid
            or type(value["target_is_ancestor"]) is not bool
            or value["target_is_ancestor"] != (bases[0] == value["target_oid"])):
        return None
    stamp = value["observed_at"]
    if not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
    except (ValueError, OverflowError):
        return None
    return dict(value)


def _valid_raw(value, published_oid):
    """Validate a raw adapter observation before binding it to configuration."""
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get("schema")) is not int or value["schema"] != SCHEMA):
        return None
    if (not isinstance(value.get("repository"), str)
            or not _REPO.fullmatch(value["repository"])
            or not _valid_remote(value.get("remote"))
            or not _valid_target_ref(value.get("target_ref"))):
        return None
    return _check_common(value, published_oid)


def validate(value, published_oid):
    """Return a bounded stored fact; shape validation does not attest to Git."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int:
        return None
    schema = value["schema"]
    if schema == SCHEMA:
        fact = _valid_raw(value, published_oid)
        if (fact is None or fact["remote"] != "origin"
                or fact["target_ref"] != "refs/heads/main"):
            return None
        return fact
    if schema != BOUND_SCHEMA or set(value) != _BOUND_FIELDS:
        return None
    raw = {key: value[key] for key in _FIELDS}
    raw["schema"] = SCHEMA
    fact = _valid_raw(raw, published_oid)
    if fact is None:
        return None
    target = {
        "repository": value.get("expected_repository"),
        "remote": value.get("expected_remote"),
        "target_ref": value.get("expected_target_ref"),
        "version": value.get("expected_version"),
    }
    expected = check_target(target)
    if (expected is None or fact["repository"] != expected["repository"]
            or fact["remote"] != expected["remote"]
            or fact["target_ref"] != expected["target_ref"]):
        return None
    return dict(value)


def bind(value, published_oid, expected):
    """Bind one raw adapter fact to the exact configured target descriptor."""
    target = check_target(expected)
    fact = _valid_raw(value, published_oid)
    if (target is None or fact is None
            or fact["repository"] != target["repository"]
            or fact["remote"] != target["remote"]
            or fact["target_ref"] != target["target_ref"]):
        return None
    bound = {
        **fact,
        "schema": BOUND_SCHEMA,
        "expected_repository": target["repository"],
        "expected_remote": target["remote"],
        "expected_target_ref": target["target_ref"],
        "expected_version": target["version"],
    }
    return validate(bound, published_oid)


def observe(value, published_oid, started, ended, expected=None):
    """Require a fresh adapter observation and bind it when a target is set."""
    if expected is None:
        # Without trusted configuration, accept only the old unbound shape.
        # A self-asserted schema-2 descriptor must not become trusted if a
        # matching configuration is added later.
        fact = (validate(value, published_oid)
                if isinstance(value, dict) and value.get("schema") == SCHEMA else None)
    else:
        fact = bind(value, published_oid, expected)
    if fact is None:
        return None
    observed = dt.datetime.fromisoformat(fact["observed_at"].replace("Z", "+00:00"))
    try:
        if not started <= observed <= ended:
            return None
    except (ValueError, OverflowError):
        return None
    return fact


def status(receipt, current, reason, expected=None):
    """Project exact-publication evidence without promoting or rewriting it."""
    def project(state, why, receipt_state=None, receipt_why=None):
        return {"status": state, "reason": why,
                "receipt_status": receipt_state or state,
                "receipt_reason": receipt_why or why}

    target = check_target(expected)
    if target is None:
        return project("unknown", "unconfigured")
    saved = validate(receipt.get("lineage"), receipt["published_sha"])
    if saved is None:
        return project("unknown", "receipt_missing_or_invalid")
    if saved["schema"] != BOUND_SCHEMA:
        return project("unknown", "unbound")
    binding = ("expected_repository", "expected_remote", "expected_target_ref",
               "expected_version")
    if any(saved[key] != target[field] for key, field in zip(
            binding, ("repository", "remote", "target_ref", "version"))):
        return project("unknown", "descriptor_mismatch", "stale", "descriptor_mismatch")
    if current is None:
        return project("unknown", reason)
    observed = validate(current, receipt["published_sha"])
    if observed is None or observed["schema"] != BOUND_SCHEMA:
        return project("unknown", "observation_invalid")
    if any(observed[key] != target[field] for key, field in zip(
            binding, ("repository", "remote", "target_ref", "version"))):
        return project("unknown", "observation_invalid")
    if (saved["repository"] != observed["repository"]
            or saved["remote"] != observed["remote"]
            or saved["target_ref"] != observed["target_ref"]):
        return project("unknown", "target_mismatch")
    if saved["target_oid"] != observed["target_oid"]:
        state, why = (("ready", "target_ancestor") if observed["target_is_ancestor"]
                      else ("unknown", "not_ancestor"))
        return project(state, why, "stale", "target_moved")
    if saved["target_is_ancestor"] != observed["target_is_ancestor"]:
        return project("unknown", "ancestry_conflict")
    if not observed["target_is_ancestor"]:
        return project("unknown", "not_ancestor")
    return project("ready", "target_ancestor")
