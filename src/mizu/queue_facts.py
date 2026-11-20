"""Queue facts for one role: what wakes it and what its unit opening states.

The facts are counts and the newest messages of each declared queue the role
may read; nothing here interprets a message, names a queue or acknowledges
anything. Readiness admits a role when a queue that wakes it has a due
unacknowledged message for it.
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
    """Declared queues whose new or due messages wake the role."""
    actor = principal(role_name)
    return [name for name, queue in config.queues.items() if actor in queue.wake]


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
            rows.append(facts)
    return rows
