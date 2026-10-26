"""Host-only exact receipts for structured insight waits and obligations.

CI waits share a prompt list, not this protocol. Tokens grant no authority:
finish must also require membership in the role's frozen current delivery.
"""
import datetime as dt
import json

from . import insight_archive
from .errors import Denied, LimitExceeded
from .work_item_waits import WAIT_KINDS
from .fs import DIGEST, canonical, digest, identifier, mkdir, now, safe_read, write_json

WAIT = "insight_wait"
OBLIGATION = "dependency_obligation"
MAX_ROW_BYTES = 1024 * 1024
RECEIPT_BYTES = 4096


def token(role, source, event):
    identifier(role)
    if source not in (WAIT, OBLIGATION):
        raise Denied("Unknown structured wait source")
    return digest(canonical({"role": role, "source": source,
                             "payload": {k: v for k, v in event.items() if k != "ack_token"}}))


def delivery(role, source, event):
    row = {**event, "ack_token": token(role, source, event)}
    if len(canonical(row)) > MAX_ROW_BYTES:
        raise LimitExceeded("Structured wait exceeds row byte bound")
    return row


def validate(role, source, event):
    if (not isinstance(event, dict)
            or (source == WAIT and event.get("kind") not in WAIT_KINDS)
            or not isinstance(event.get("ack_token"), str)
            or not DIGEST.fullmatch(event["ack_token"])
            or event["ack_token"] != token(role, source, event)):
        raise Denied("Structured wait acknowledgement binding changed")


def _identity(role, event):
    identifier(role)
    identifier(event["insight"])
    return {"role": role, "source": OBLIGATION, "insight": event["insight"],
            "registration_id": event["registration_id"]}


def _path(root, role, event):
    return root / "obligation-receipts" / role / f"{digest(canonical(_identity(role, event)))}.json"


def received(root, role, event):
    """Absent is pending; unsafe or corrupt receipts fail closed, never repair.

    Each read is at most 4 KiB, with no symlink/hardlink following. Receipts
    are exact per-role registrations, not cursors or scheduling authority.
    """
    path = _path(root, role, event)
    if (not insight_archive._directory(path.parent.parent)
            or not insight_archive._directory(path.parent)):
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    raw = safe_read(root, path.relative_to(root).as_posix(), RECEIPT_BYTES)
    try:
        receipt = json.loads(raw)
        expected = {**_identity(role, event), "ack_token": event["ack_token"]}
        if (not isinstance(receipt, dict)
                or set(receipt) != set(expected) | {"acknowledged_at"}
                or any(receipt[k] != v for k, v in expected.items())):
            raise ValueError
        stamp = dt.datetime.fromisoformat(receipt["acknowledged_at"])
        if stamp.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise Denied("Corrupt obligation receipt") from exc
    return True


def acknowledge_obligations(root, role, events):
    """After durable success only; atomic create per identity, replay-safe.

    An interrupted batch leaves only written selections suppressed. Failure
    never overwrites corrupt evidence or acknowledges later/unselected rows.
    """
    if not isinstance(events, list) or len(events) > 100:
        raise Denied("Invalid obligation acknowledgement batch")
    for event in events:
        validate(role, OBLIGATION, event)
    for event in events:
        if received(root, role, event):
            continue
        path = _path(root, role, event)
        mkdir(path.parent)
        receipt = {**_identity(role, event), "ack_token": event["ack_token"],
                   "acknowledged_at": now()}
        try:
            write_json(path, receipt, exclusive=True)
        except (FileExistsError, Denied):
            if not received(root, role, event):
                raise
