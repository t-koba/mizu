"""Trusted host-only activation of queue-backed record delivery.

No model tool reaches this module. The operator supplies the exact reports
produced by ``convert_legacy.apply`` and ``stage2_verify.verify`` for this
project; both are rechecked against the declared queues before a create-only
marker is published last. A marker exists only after a clean gate, and an exact
retry returns the retained marker. A failure leaves legacy record delivery in
force without rewriting plan, report or marker bytes.
"""
from __future__ import annotations

from . import authority_marker
from .config import MAX_DECISION_RECONCILIATION_BYTES
from .errors import Denied, LimitExceeded
from .fs import atomic_write, canonical, digest, lock, safe_read

REPORT_BYTES = MAX_DECISION_RECONCILIATION_BYTES
COUNTS = {"proposals": ("proposals",), "decisions": ("decisions", "revisits"),
          "questions": ("questions",), "answers": ("answers",),
          "notices": ("notices",), "receipts": ("receipts",),
          "record-receipts": ("record_receipts",)}


def _validate_gate(project, conversion, verification):
    from .queue_decision_delivery import CONVERSION_QUEUES
    from .queue_record_delivery import RECORD_RECEIPTS, _require_queues
    queues = (*CONVERSION_QUEUES, RECORD_RECEIPTS)
    if (type(conversion) is not dict or conversion.get("project") != project.name
            or conversion.get("applied") is not True
            or type(conversion.get("unbound_receipts")) is not list
            or conversion["unbound_receipts"]
            or any(type(conversion.get(field)) is not list for fields in COUNTS.values()
                   for field in fields)):
        raise Denied("Stage-2 conversion has not been applied cleanly")
    if (type(verification) is not dict or verification.get("project") != project.name
            or verification.get("ok") is not True or verification.get("differences") != []
            or type(verification.get("checked")) is not dict
            or set(verification["checked"]) != set(queues)):
        raise Denied("Stage-2 verify did not pass for record delivery")
    for queue in queues:
        row = verification["checked"][queue]
        if (type(row) is not dict or set(row) != {"planned", "stored"}
                or type(row["planned"]) is not int or type(row["stored"]) is not int
                or row["planned"] != row["stored"]):
            raise Denied("Stage-2 verify did not pass for record delivery")
        count = sum(len(conversion[field]) for field in COUNTS[queue])
        if count != row["planned"] or queue not in project.config.queues:
            raise Denied("Stage-2 conversion and verify reports disagree")
    _require_queues(project)
    return canonical(conversion), canonical(verification)


def activate_queue_record_delivery(project, *, conversion, verification):
    """Publish durable queue record authority from the operator's gate reports."""
    from .queue_record_delivery import (AUTHORITY_FILE, KIND, PLAN_FILE, REPORT_FILE, authority)
    plan_raw, report_raw = _validate_gate(project, conversion, verification)
    root = project.insights.root
    with lock(project.root / "locks" / "record-authority.lock"):
        marker = authority(root)
        if marker is not None:
            if marker["authority"] != "queues":
                raise Denied("Record delivery is shared; queue activation is closed")
            if authority_marker.plan(root, marker, file=PLAN_FILE, label="record",
                                     report_bytes=REPORT_BYTES) != plan_raw:
                raise Denied("Record activation plan changed before retry")
            authority_marker.report(root, marker, file=REPORT_FILE, label="record",
                                    report_bytes=REPORT_BYTES)
            return marker
        if len(plan_raw) > REPORT_BYTES or len(report_raw) > REPORT_BYTES:
            raise LimitExceeded("Record activation proof exceeds configured byte bound")
        for name, raw in ((PLAN_FILE, plan_raw), (REPORT_FILE, report_raw)):
            try:
                (root / name).lstat()
            except FileNotFoundError:
                atomic_write(root / name, raw, exclusive=True)
            else:
                if safe_read(root, name, REPORT_BYTES) != raw:
                    raise Denied("Record activation proof changed before marker")
        marker = {"schema": 1, "kind": KIND, "authority": "queues",
                  "plan_digest": digest(plan_raw), "report_digest": digest(report_raw)}
        atomic_write(root / AUTHORITY_FILE, canonical(marker), exclusive=True)
        return authority(root)
