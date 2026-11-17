"""Versioned inline work-completion evidence format, independent of deployment policy."""
import math

from .errors import Denied
from .fs import canonical

VERIFIED_SNAPSHOT = "verified_snapshot_v1"
HOLDER_ATTESTATION = "holder_attestation_v1"
POLICIES = (VERIFIED_SNAPSHOT, HOLDER_ATTESTATION)
# Durable format ceilings, not deployment budgets. Replay never consults config.
MAX_EVIDENCE_BYTES = 65536
MAX_EVIDENCE_DEPTH = 32


def validate_attestation(evidence, maximum_bytes=MAX_EVIDENCE_BYTES):
    """Validate finite JSON without coercing or dropping any attachment fields."""
    def visit(value, depth):
        if depth > MAX_EVIDENCE_DEPTH:
            raise ValueError
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError
                visit(child, depth + 1)
        elif type(value) is list:
            for child in value:
                visit(child, depth + 1)
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError
        elif value is not None and type(value) not in (str, int, bool):
            raise ValueError

    if not isinstance(evidence, dict):
        raise Denied(f"{HOLDER_ATTESTATION}: evidence object is required")
    try:
        visit(evidence, 0)
        size = len(canonical(evidence))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise Denied(f"{HOLDER_ATTESTATION}: evidence must be finite JSON within format depth") from None
    if size > maximum_bytes:
        raise Denied(f"{HOLDER_ATTESTATION}: evidence exceeds maximum_evidence_bytes")
    statement = evidence.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise Denied(f"{HOLDER_ATTESTATION}: evidence.statement is required")


def recorded_policy(record):
    policy = record.get("completion_policy", VERIFIED_SNAPSHOT)
    if policy not in POLICIES:
        raise Denied("completion_policy: unknown completion contract")
    return policy
