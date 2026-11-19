"""Shared validation for durable one-way authority markers.

A marker is a create-only canonical JSON file naming the selected authority
(``shared`` or ``queues``) and pinning the exact retained plan and report bytes.
Absence means legacy; a marker that is malformed, noncanonical or whose report
no longer matches its digest refuses instead of falling back. The meaning of
the pinned report body and the marker's kind are owned by each delivery module.
"""
from __future__ import annotations

from .errors import Denied
from .fs import DIGEST, canonical, digest, loads_finite, safe_read

MARKER_BYTES = 4096
FIELDS = {"schema", "kind", "authority", "plan_digest", "report_digest"}
AUTHORITIES = ("shared", "queues")


def read(root, *, file, plan_file, report_file, kind, label, report_bytes,
         marker_bytes=MARKER_BYTES, loads=loads_finite):
    """Return the validated marker, or None when no marker exists."""
    try:
        (root / file).lstat()
    except FileNotFoundError:
        return None
    raw = safe_read(root, file, marker_bytes)
    try:
        marker = loads(raw)
        if (type(marker) is not dict or set(marker) != FIELDS
                or type(marker["schema"]) is not int or marker["schema"] != 1
                or marker["kind"] != kind
                or marker["authority"] not in AUTHORITIES
                or any(not isinstance(marker[key], str) or not DIGEST.fullmatch(marker[key])
                       for key in ("plan_digest", "report_digest"))
                or canonical(marker) != raw):
            raise ValueError("marker schema")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Denied(f"Invalid {label} authority marker; reconciliation required") from exc
    report(root, marker, file=report_file, label=label, report_bytes=report_bytes)
    return marker


def authority(root, **kwargs):
    """Return the selected authority value: None, ``shared`` or ``queues``."""
    marker = read(root, **kwargs)
    return None if marker is None else marker["authority"]


def report(root, marker, *, file, label, report_bytes):
    """Read the exact report bytes pinned by the marker (never its body)."""
    raw = safe_read(root, file, report_bytes)
    if digest(raw) != marker["report_digest"]:
        raise Denied(f"{label.capitalize()} authority report digest mismatch")
    return raw


def plan(root, marker, *, file, label, report_bytes):
    """Read the exact plan bytes pinned by the marker where a caller needs them."""
    raw = safe_read(root, file, report_bytes)
    if digest(raw) != marker["plan_digest"]:
        raise Denied(f"{label.capitalize()} authority plan digest mismatch")
    return raw
