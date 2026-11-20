"""Context work-item dispatch: thin wrappers over work_item_operations.

These four methods own the Context side of work-item reads, creation,
transitions and reopening. Each runs through its owning module with the trusted
Context. They are mixed back into ``Context`` and referenced by its dispatch
table, so every ``Context.method`` reference and ``patch.object(Context, ...)``
call site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import work_item_operations


class ContextWorkToolsMixin:
    def _op_work_items_read(self, args: dict) -> dict:
        return work_item_operations.read(self, args)

    def _op_work_items_create(self, args: dict) -> dict:
        return work_item_operations.create(self, args)

    def _op_work_items_transition(self, args: dict) -> dict:
        return work_item_operations.transition(self, args)

    def _op_work_items_reopen(self, args: dict) -> dict:
        return work_item_operations.reopen(self, args)
