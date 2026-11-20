"""Context role/work dispatch: thin wrappers over their owners.

These four methods own the Context side of role-research reads and updates,
work-order recording and input materialization. Each runs through its owning
module with the trusted Context. They are mixed back into ``Context`` and
referenced by its dispatch table, so every ``Context.method`` reference and
``patch.object(Context, ...)`` call site keeps resolving unchanged. No
behaviour, grant or default changes.
"""
from __future__ import annotations

from . import materialize as _materialize
from . import role_state_ops, work_order_ops


class ContextRoleToolsMixin:
    def _op_research_read(self, args: dict) -> dict:
        return role_state_ops.research_read(self, args)

    def _op_research(self, args: dict) -> dict:
        return role_state_ops.research(self, args)

    def _op_work_order(self, args: dict) -> dict:
        return work_order_ops.record(self, args)

    def _op_materialize(self, args: dict) -> dict:
        return _materialize.op_materialize(self, args)
