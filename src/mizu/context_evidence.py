"""Context evidence-record dispatch: thin wrappers over evidence_records.

These five methods own the Context side of retained-evidence reads and of the
invocation/mutation evidence attached to tool calls. The two dispatch handlers
route through the shared _read_retained_evidence helper; all five are mixed
back into ``Context`` and the handlers referenced by its dispatch table, so
every ``Context.method`` reference and ``patch.object(Context, ...)`` call site
keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import evidence_records


class ContextEvidenceMixin:
    def _read_retained_evidence(self, kind: str, args: dict) -> dict:
        return evidence_records.read_retained(self, kind, args)

    def _new_invocation_evidence(self, kind: str) -> tuple[str, str]:
        return evidence_records.new_invocation(kind)

    def _mutation_evidence(self, kind: str, op: str, parameters: dict):
        return evidence_records.mutation(self, kind, op, parameters)

    def _op_command_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("commands", args)

    def _op_experiment_evidence_read(self, args: dict) -> dict:
        return self._read_retained_evidence("experiments", args)
