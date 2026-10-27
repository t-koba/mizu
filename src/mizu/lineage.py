"""Bounded, adapter-observed lineage facts; never a promotion authority."""
from __future__ import annotations

import datetime as dt
import re

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPO = re.compile(r"[0-9a-f]{64}\Z")
SCHEMA = 1
_FIELDS = {"schema", "repository", "remote", "target_ref", "target_oid", "published_oid",
           "merge_bases", "target_is_ancestor", "observed_at"}


def validate(value, published_oid):
    """Return a bounded fact or None; shape validation does not attest to Git."""
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get("schema")) is not int or value["schema"] != SCHEMA):
        return None
    if (not isinstance(value["repository"], str) or not _REPO.fullmatch(value["repository"])
            or value["remote"] != 'origin' or value["target_ref"] != 'refs/heads/main'):
        return None
    bases = value["merge_bases"]
    if not isinstance(bases, list) or len(bases) != 1 or not isinstance(bases[0], str) or not _OID.fullmatch(bases[0]):
        return None
    for key in ('target_oid', 'published_oid'):
        if not isinstance(value[key], str) or not _OID.fullmatch(value[key]):
            return None
    if (len(value["target_oid"]) != len(value["published_oid"])
            or len(bases[0]) != len(value["published_oid"])
            or value["published_oid"] != published_oid
            or type(value["target_is_ancestor"]) is not bool
            or value["target_is_ancestor"] != (bases[0] == value["target_oid"])):
        return None
    stamp = value["observed_at"]
    if not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        parsed = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
    except (ValueError, OverflowError):
        return None
    return dict(value)


def observe(value, published_oid, started, ended):
    """Require the adapter's observation time to fall within this invocation."""
    fact = validate(value, published_oid)
    if fact is None:
        return None
    observed = dt.datetime.fromisoformat(fact["observed_at"].replace('Z', '+00:00'))
    try:
        if not started <= observed <= ended:
            return None
    except (ValueError, OverflowError):
        return None
    return fact


def status(receipt, current, reason):
    """Project exact-receipt evidence, with a closed set of bounded reasons."""
    def project(state, why, receipt_state=None, receipt_why=None):
        return {'status': state, 'reason': why,
                'receipt_status': receipt_state or state,
                'receipt_reason': receipt_why or why}

    saved = validate(receipt.get('lineage'), receipt['published_sha'])
    if saved is None:
        return project('unknown', 'receipt_missing_or_invalid')
    if current is None:
        return project('unknown', reason)
    current = validate(current, receipt['published_sha'])
    if current is None:
        return project('unknown', 'observation_invalid')
    if saved['repository'] != current['repository']:
        return project('unknown', 'repository_mismatch')
    if saved['target_oid'] != current['target_oid']:
        state, why = (('ready', 'target_ancestor') if current['target_is_ancestor']
                      else ('unknown', 'not_ancestor'))
        return project(state, why, 'stale', 'target_moved')
    if saved['target_is_ancestor'] != current['target_is_ancestor']:
        return project('unknown', 'ancestry_conflict')
    if not current['target_is_ancestor']:
        return project('unknown', 'not_ancestor')
    return project('ready', 'target_ancestor')
