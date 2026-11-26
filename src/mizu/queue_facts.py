"""Queue facts for one role: what wakes it and what its unit opening states.

The facts are counts and the newest messages of each declared queue the role
may read; nothing here interprets a message or names a queue. Delivery facts
are the only reader state: a due message wakes a role until it is delivered,
and a dispatched unit records delivery for every message its serialized
opening actually showed.
Readiness admits a role when a queue that wakes it has a due message that has
not been delivered to it yet.
"""
from __future__ import annotations

from . import prompt_sections
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
    the queues it may both write and read, where replies to its messages arrive.
    A role whose prompt omits the ``queues`` section cannot drain a queue wake
    through its opening, so no queue wakes it: eligibility follows handover."""
    role = getattr(config, "roles", {}).get(role_name)
    if role is not None and "queues" not in prompt_sections.included(role):
        return []
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


def deliver_opening(project, role_name: str, rows: list[dict], unit: str) -> None:
    """Record delivery for every message the dispatched unit opening showed.

    ``rows`` is the queues projection actually serialized into the dispatched
    opening, including referred bodies embedded by ``_with_thread``; a role
    whose prompt omits that section passes no rows, so no unseen message is
    credited. A racing retention removal
    or a corrupt fact cannot fail dispatch: such a message stays undelivered
    and can still wake a later unit.
    """
    store = project.kernel_queues
    actor = principal(role_name)

    def credit(message: dict, default_queue) -> None:
        # A serialized message is credited by its own queue and sequence. A
        # referred body (see _with_thread) is serialized into the same opening
        # and must be credited too, or the next opening shows it again even
        # though the reader already received it. Only due bodies are embedded
        # there; a future-dated body stays withheld and uncredited. Bodies
        # that were not serialized are absent here and stay undelivered.
        if not isinstance(message, dict) or type(message.get("seq")) is not int:
            return
        queue = message.get("queue", default_queue)
        if not isinstance(queue, str):
            return
        try:
            # The handoff establishes the fact: this body was serialized into
            # the dispatched opening, so a deadline crossing between the
            # projection and this credit cannot make the reader unsee it.
            # Eligibility paths still honour the deadline.
            store.record_delivery(queue, message["seq"], actor, unit, handed=True)
        except (Denied, OSError, ValueError):
            pass
        refers = message.get("refers")
        if isinstance(refers, list):
            for referred in refers:
                credit(referred, queue)

    for row in rows:
        if not isinstance(row.get("queue"), str) or not isinstance(row.get("messages"), list):
            continue
        for message in row["messages"]:
            credit(message, row["queue"])


def opening(project, role_name: str) -> list[dict]:
    """Per readable queue with due messages: count, ``more`` and the oldest few.

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
    """A reply arrives with the due messages it refers to that the reader may read.

    ``store.message`` gates each body by ``deliver_at``, so a future-dated
    reference is not serialized into this opening and earns no delivery fact.
    """
    refers = []
    for ref in message.get("refs") or ():
        try:
            refers.append(store.message(ref, actor))
        except (Denied, OSError, ValueError):
            continue
    return {**message, "refers": refers} if refers else message
