"""Context queue-tool dispatch: thin wrappers over the kernel queue tools.

These five methods own the Context side of queue and message tool calls. Each
validates and runs through the kernel queue tool module, which takes
the acting role from the trusted Context and gates before any kernel I/O. They
are mixed back into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import kernel_queue_tools


class ContextKernelToolsMixin:
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
