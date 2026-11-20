"""Context execution lifecycle: exec session, driver checkpoint and cancellation state.

These methods own one Context's execution boundary -- the persistent exec
session, the driver-checkpoint seal lifecycle, and the cancellation/wake state
that decides whether work may continue. They are mixed back into ``Context``,
so every existing ``Context.method`` reference and ``patch.object(Context, ...)``
call site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

from .drivers import DriverCheckpoint
from .errors import Denied


class ContextDriverMixin:
    def close_exec(self):
        # Keep a poisoned session attached: cleanup failure must not let a
        # subsequent call launch another container or publish a snapshot.
        if self.exec_session is not None:
            self.exec_session.close()
            self.exec_session = None

    def persistent_execute(self, script):
        from .persistent_exec import ExecSession
        if not self.role.persistent_exec or "exec" not in self.role.capabilities:
            raise Denied("Persistent exec requires explicit role opt-in and exec grant")
        if self.exec_session is not None and self.exec_session.closed:
            self.close_exec()
        if self.exec_session is None:
            self.exec_session = ExecSession(self.sandbox, self.workspace,
                                            writable=self.role.workspace == "write")
        return self.exec_session.execute(script)

    @property
    def driver_capabilities(self):
        """Expose no ordinary operation in a claimed checkpoint seal phase."""
        return tuple(c for c in self.role.capabilities if not self.checkpoint_seal_only or c == "finish")

    def begin_driver(self, engine: str, unit: str):
        """Invalidate a prior boundary before launch; a claimed one is final."""
        with self.mutex:
            if self.checkpoint_seal_only:
                self.check_deadlines(wrap_up=True)
                authority = self.turn_authority_role or self.role
                if (not self._checkpoint_launch_pending or self.role.engine_tools or authority.engine_tools
                        or (engine, unit) != (self._checkpoint_engine, self._checkpoint_unit)):
                    raise Denied("No inference in the checkpoint seal phase")
                self._checkpoint_launch_pending = False
                self._checkpoint_driver_started = True
            self.driver_checkpoint = None

    def complete_driver_checkpoint(self, engine: str, unit: str):
        """Called only after successful terminal completion AND bridge teardown.

        Query/turn completion is not a Pi model_request boundary. Engine-tool
        admission has no completion acknowledgment, so those paths fail closed.
        This records a candidate; only the configured host scheduler may consume it.
        """
        with self.mutex:
            self.driver_checkpoint = None
            authority = self.turn_authority_role or self.role
            if self.checkpoint_seal_only:
                self._checkpoint_settled = (
                    self._checkpoint_driver_started and (engine, unit) == (self._checkpoint_engine, self._checkpoint_unit)
                    and (engine, unit) in (("claude", "query"), ("codex", "turn"))
                    and self.role.workspace == "write" and authority.workspace == "write"
                    and not self.delegated_child and not self.ephemeral
                    and not self.role.engine_tools and not authority.engine_tools and self.operations_stopped.is_set()
                    and not self.request_deadlines and not self.request_failure_pending
                    and self.admission_error is None and self.admission_exception is None
                    and not self.operation_active.is_set() and self.joins_idle.is_set())
                return
            if ((engine, unit) not in (("claude", "query"), ("codex", "turn"))
                    or self.role.workspace != "write" or authority.workspace != "write"
                    or self.delegated_child or self.ephemeral
                    or self.role.engine_tools or authority.engine_tools or self.finished is not None
                    or self.checkpoint_seal_only or not self.operations_stopped.is_set()
                    or self.request_deadlines or self.request_failure_pending
                    or self.admission_error is not None or self.admission_exception is not None
                    or self.operation_active.is_set() or not self.joins_idle.is_set() or not self.request_count):
                return
            self.driver_checkpoint = DriverCheckpoint(engine, unit, self.execution_attempt, self.request_count)
            if self.config.limits.safe_point_yield_quantum:
                self.completed_driver_units += 1

    def claim_driver_checkpoint(self, checkpoint: DriverCheckpoint):
        """Host-only, one-shot transition to finish-only, not a finish or yield.

        The caller must already own any scheduling decision. No ordinary tools
        or new inference may start after this latch, except the one host-armed
        finish-only driver unit with normal grant, budget and seal checks.
        Tokens cannot survive another dispatch, operation, attempt or restart.
        """
        with self.mutex:
            self.check_deadlines(wrap_up=True)  # stop/authority and bounds win
            if self.bound_hit is not None:
                raise Denied(self._bound_message())
            authority = self.turn_authority_role or self.role
            if (checkpoint is None or checkpoint is not self.driver_checkpoint
                    or (checkpoint.engine, checkpoint.unit) not in (("claude", "query"), ("codex", "turn"))
                    or checkpoint.execution_attempt != self.execution_attempt
                    or checkpoint.requests != self.request_count
                    or self.role.workspace != "write" or authority.workspace != "write"
                    or self.delegated_child or self.ephemeral
                    or self.role.engine_tools or authority.engine_tools or self.finished is not None
                    or self.checkpoint_seal_only or not self.operations_stopped.is_set()
                    or self.request_deadlines or self.request_failure_pending
                    or self.admission_error is not None or self.admission_exception is not None
                    or self.operation_active.is_set() or not self.joins_idle.is_set()):
                raise Denied("No quiescent driver checkpoint")
            self.checkpoint_seal_only = True
            self._checkpoint_engine = checkpoint.engine
            self._checkpoint_unit = checkpoint.unit
            self.driver_checkpoint = None

    def wait_for_joins(self):
        # Explicit stop is shared with children; still drain their cleanup
        # before releasing the parent's workspace lock.
        self.joins_idle.wait()

    def cancel_operations(self):
        # Even an exited/failed parent transport must drain admitted children
        # before releasing the writer lock or cleaning up their shared tree.
        self.wait_for_joins()
        self.operations_stopped.set()
        self.close_exec()

    def tools_cancelled(self):
        return self.operations_stopped.is_set() or self.cancelled()

    def host_cancelled(self) -> bool:
        """Explicit host cancellation, distinct from the adapter run deadline."""
        control = self.project.control()
        return self.stop.is_set() or self.closed or not control.get("armed") or control.get("paused")

    def cancelled(self) -> bool:
        return self.host_cancelled() or (self.joins_idle.is_set() and self.wall_expired())
