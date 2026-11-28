"""Mizu kernel: declared message queues, observation drivers and persistent directories.

Queues carry authenticated messages, durable reader cursors, delayed delivery,
references and retention. Drivers are bounded commands whose observations are
appended to a declared queue. Persistent directories carry fact-derived
identities, ordered mount rules and referential liveness. Every
deployment-specific name comes from configuration. Specification: docs/kernel.md.
"""
from .drivers import Driver, invoke, parse_drivers, record_observations
from .persistence import (MountRule, PersistentDirectory, identity_for,
                          mount_mode, parse_persistent, unreferenced)
from .queues import Queue, QueueField, QueueStore, parse_queues

__all__ = ["Driver", "MountRule", "PersistentDirectory", "Queue", "QueueField",
           "QueueStore", "identity_for", "invoke", "mount_mode", "parse_drivers",
           "parse_persistent", "parse_queues", "record_observations", "unreferenced"]
