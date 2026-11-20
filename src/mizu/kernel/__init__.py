"""Mizu kernel: declared message queues.

Queues carry authenticated messages, durable reader cursors, delayed delivery,
references and retention. Specification: docs/kernel.md. Nothing in this
package names a role, record kind, state, event, queue or workflow; every such
name comes from configuration.
"""
from .queues import Queue, QueueField, QueueStore, parse_queues

__all__ = ["Queue", "QueueField", "QueueStore", "parse_queues"]
