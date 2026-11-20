"""Context web-tool dispatch: thin wrappers over the web tool modules.

These five methods own the Context side of fetch, probe, search and feed-update
tool calls, including the shared bounded-operation helper. Each runs through
its owning module, which derives identity from the trusted Context. They are
mixed back into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import feed_updates, web_operations
from .web_worker import bounded


class ContextWebToolsMixin:
    def _bounded_web_operation(self, *args):
        return bounded(*args)

    def _op_fetch(self, args: dict) -> dict:
        return web_operations.fetch(self, args)

    def _op_probe(self, args: dict) -> dict:
        return web_operations.probe(self, args)

    def _op_feed_updates(self, args: dict) -> dict:
        return feed_updates.read(self)

    def _op_search(self, args: dict) -> dict:
        return web_operations.search(self, args)
