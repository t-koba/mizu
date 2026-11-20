"""Context insight/decision dispatch: thin wrappers over the insight lifecycle.

These seven methods own the Context side of proposal, decision and record
reads and writes. Each runs through its owning module, which derives identity
from the trusted Context. They are mixed back into ``Context`` and referenced
by its dispatch table, so every ``Context.method`` reference and
``patch.object(Context, ...)`` call site keeps resolving unchanged. No
behaviour, grant or default changes.
"""
from __future__ import annotations

from . import decision_delivery, insight_tools


class ContextInsightToolsMixin:
    def _op_decision_read(self, args: dict) -> dict:
        return decision_delivery.read(self, args)

    def _op_insights(self, args: dict) -> dict:
        return insight_tools.insights(self, args)

    def _op_decide(self, args: dict) -> dict:
        return insight_tools.decide(self, args)

    def _op_submit_insight(self, args: dict) -> dict:
        return insight_tools.submit_insight(self, args)

    def _op_revise_insight(self, args: dict) -> dict:
        return insight_tools.revise_insight(self, args)

    def _op_withdraw_insight(self, args: dict) -> dict:
        return insight_tools.withdraw_insight(self, args)

    def _op_attach_evidence(self, args: dict) -> dict:
        return insight_tools.attach_evidence(self, args)
