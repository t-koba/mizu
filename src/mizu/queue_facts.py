"""Queue facts for one role: what wakes it and what its unit opening states.

The facts are counts and the newest messages of each declared queue the role
may read; nothing here interprets a message, names a queue or acknowledges
anything. Readiness admits a role when a queue that wakes it has a due
message that has not woken it yet; a finished unit records that its opening's
messages have woken the role.
"""
from __future__ import annotations

from .errors import Denied
from .kernel.queues import allows


def principal(role_name: str) -> str:
    return f"role:{role_name}"


def readable(config, role_name: str) -> list[str]:
    """Declared queues the role may read, in declaration order."""
    actor = principal(role_name)
    return [name for name, queue in config.queues.items() if allows(queue.read, actor)]


def waking(config, role_name: str) -> list[str]:
    """Declared queues whose messages can wake the role: its ``wake`` queues, and
    the queues it may both write and read, where replies to its messages arrive."""
    actor = principal(role_name)
    return [name for name, queue in config.queues.items()
            if actor in queue.wake or (allows(queue.write, actor) and allows(queue.read, actor))]


def due(project, role_name: str) -> bool:
    """Whether a queue that wakes the role holds a due unacknowledged message for it.

    An unreadable queue counts as due: a fact that cannot be read must not
    leave its reader asleep.
    """
    store = project.kernel_queues
    for name in waking(project.config, role_name):
        try:
            if store.due(name, principal(role_name)):
                return True
        except (Denied, OSError, ValueError):
            return True
    return False


def next_delivery_at(project, role_name: str) -> float | None:
    """Epoch seconds of the earliest pending delayed message across the queues
    that wake ``role_name``, or None when none of them has one.

    Read-only and takes no acknowledgement: a future delivery still wakes the
    role through the queue store, so this only bounds how long a caller sleeps.
    A queue that cannot be read contributes no deadline; callers keep whatever
    fallback wake they already had.
    """
    store = project.kernel_queues
    actor = principal(role_name)
    deadlines = []
    for name in waking(project.config, role_name):
        try:
            delivery = store.next_delivery(name, actor)
        except (Denied, OSError, ValueError):
            continue
        if delivery is not None:
            deadlines.append(delivery.timestamp())
    return min(deadlines) if deadlines else None


def mark_woken(project, role_name: str, rows: list[dict]) -> None:
    """Record that the opening's due messages have woken the role."""
    store = project.kernel_queues
    for row in rows:
        if type(row.get("through")) is int and "queue" in row:
            store.mark_woken(row["queue"], principal(role_name), row["through"])


def opening(project, role_name: str) -> list[dict]:
    """Per readable queue with due messages: count, ``more`` and the newest few.

    The number of newest messages is ``[limits] queue_opening_messages``. A
    queue that cannot be read is listed with its error instead of a count.
    """
    store = project.kernel_queues
    newest = project.config.limits.queue_opening_messages
    rows = []
    for name in readable(project.config, role_name):
        try:
            facts = store.pending(name, principal(role_name), newest=newest)
        except (Denied, OSError, ValueError) as exc:
            rows.append({"queue": name, "error": str(exc)[:512]})
            continue
        if facts["count"]:
            facts["messages"] = [_with_thread(store, message, principal(role_name))
                                 for message in facts["messages"]]
            rows.append(facts)
    return rows


def _with_thread(store, message: dict, actor: str) -> dict:
    """A reply arrives with the messages it refers to that the reader may read."""
    refers = []
    for ref in message.get("refs") or ():
        try:
            refers.append(store.message(ref, actor))
        except (Denied, OSError, ValueError):
            continue
    return {**message, "refers": refers} if refers else message
