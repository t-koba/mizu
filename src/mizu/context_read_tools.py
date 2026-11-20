"""Context workspace/execution dispatch: thin wrappers over their owners.

These six methods own the Context side of result/diff/files reads and of exec
and experiment tool calls, including the shared exec/experiment helper. They
are mixed back into ``Context`` and referenced by its dispatch table, so every
``Context.method`` reference and ``patch.object(Context, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from . import sandbox_operations, tool_results, workspace_reads


class ContextReadToolsMixin:
    def _op_result_read(self, args: dict) -> dict:
        return tool_results.read(self, args)

    def _op_diff(self, args: dict) -> dict:
        return workspace_reads.op_diff(self, args)

    def _op_files(self, args: dict) -> dict:
        return workspace_reads.op_files(self, args)

    def _op_exec_experiment(self, op: str, args: dict) -> dict:
        return sandbox_operations.exec_experiment(self, op, args)

    def _op_exec(self, args: dict) -> dict:
        return self._op_exec_experiment("exec", args)

    def _op_experiment(self, args: dict) -> dict:
        return self._op_exec_experiment("experiment", args)
