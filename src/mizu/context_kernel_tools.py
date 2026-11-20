"""Context kernel-tool dispatch: thin wrappers over the kernel tool modules.

These eleven methods own the Context side of object, event, queue and message
tool calls. Each validates and runs through the kernel tool module, which takes
the acting role from the trusted Context and gates before any kernel I/O. They
are mixed back into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import kernel_queue_tools, kernel_tools


class ContextKernelToolsMixin:
    def _op_objects_create(self, args: dict) -> dict:
        return kernel_tools.objects_create(self, args)

    def _op_objects_transition(self, args: dict) -> dict:
        return kernel_tools.objects_transition(self, args)

    def _op_objects_read(self, args: dict) -> dict:
        return kernel_tools.objects_read(self, args)

    def _op_objects_list(self, args: dict) -> dict:
        return kernel_tools.objects_list(self, args)

    def _op_events_read(self, args: dict) -> dict:
        return kernel_tools.events_read(self, args)

    def _op_events_ack(self, args: dict) -> dict:
        return kernel_tools.events_ack(self, args)

    def _op_queue_write(self, args: dict) -> dict:
        return kernel_queue_tools.queue_write(self, args)

    def _op_queue_read(self, args: dict) -> dict:
        return kernel_queue_tools.queue_read(self, args)

    def _op_queue_ack(self, args: dict) -> dict:
        return kernel_queue_tools.queue_ack(self, args)

    def _op_message_read(self, args: dict) -> dict:
        return kernel_queue_tools.message_read(self, args)

    def _op_message_refs(self, args: dict) -> dict:
        return kernel_queue_tools.message_refs(self, args)
