"""Owner of shared-record authority selection for role-admission reads."""
from __future__ import annotations

from . import operator_records, queue_record_delivery


def _record_delivery(project):
    """Select the shared record authority; a marker is checked on every use."""
    root = getattr(project, "insights", None)
    if root is not None and queue_record_delivery.authority_kind(root.root) == "queues":
        return queue_record_delivery
    return operator_records

