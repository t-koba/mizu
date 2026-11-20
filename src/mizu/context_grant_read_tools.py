"""Context grant-checked read dispatch: ready work, work order and report.

These three methods own the Context side of the grant-gated read tools. Each
checks its operation grant before reaching its owning module with the trusted
Context. They are mixed back into ``Context`` and referenced by its dispatch
table, so every ``Context.method`` reference and ``patch.object(Context, ...)``
call site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from .errors import Denied
from .protocol import operation_allowed
from .report import previous as _previous_report
from .unit_prompts import _ready_work_projection


class ContextGrantReadToolsMixin:
    def _op_ready_work_read(self, args: dict) -> dict:
        if not operation_allowed("ready_work_read", self.role.capabilities):
            raise Denied("Ready-work read requires a work capability")
        page = self.project.work_items.page_ready(
            limit=args.get("limit", 25), role_name=self.role.name)
        # A tool read is not opening-page delivery: do not retain signatures,
        # consume resolution tokens or advance the prompt high-water receipt.
        return _ready_work_projection(page)

    def _op_work_order_read(self, args: dict) -> dict:
        if not operation_allowed("work_order_read", self.role.capabilities):
            raise Denied("Work-order read requires the work_order grant")
        from . import work_order
        return work_order.projection_for(self)

    def _op_report_read(self, args: dict) -> dict:
        if not operation_allowed("report_read", self.role.capabilities):
            raise Denied("Report read requires the report grant")
        return {"previous_report": _previous_report(self.project)}
