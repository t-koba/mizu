"""Context run-lifecycle: wall/request deadlines, wrap-up bounds and activity accounting.

These methods own one Context's run boundaries -- wall and logical-request
deadlines, wrap-up admission, drain refusal, activity/CI accounting and the
bound messages shown to a role. They are mixed back into ``Context``, so every
existing ``Context.method`` reference and ``patch.object(Context, ...)`` call
site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

import time

from . import run_activity as _run_activity
from .config import role_wrap_up_requests, role_wrap_up_tools
from .errors import Cancelled, DeadlineWrapUp, Denied, LimitExceeded, RunDeadlineExceeded
from .protocol import tool_definitions

#: Operations admitted during wrap-up (plus bridge bookkeeping). Everything
#: else is refused once a per-run bound is reached.
WRAP_UP_OPS = frozenset({"finish", "research", "attach_evidence"})


class ContextLifecycleMixin:
    def wall_expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def enter_deadline_wrap_up(self):
        if (not getattr(self, "recovery_probe", False) and self.finished is None
                and self.bound_hit is None and self.wrap_up_at is not None
                and time.monotonic() >= self.wrap_up_at):
            self.bound_hit = "deadline"
            return True
        return False

    def enter_drain_wrap_up(self):
        # Transport polling must never wait on a dispatch/join holding mutex.
        # Recheck under the admission mutex so a new request cannot race the
        # safe-boundary decision. Once latched, _budget refuses ordinary work.
        if not self.config.limits.drain_yield:
            return
        if self.ephemeral and not getattr(self, "delegated_child", False):
            return
        if not self.mutex.acquire(blocking=False):
            return
        try:
            if (self.finished is not None or self.bound_hit is not None
                    or getattr(self, "recovery_probe", False)
                    or self.request_deadlines or self.request_failure_pending or self.operation_active.is_set()
                    or not self.joins_idle.is_set()
                    or not self.project.control().get("draining")):
                return
            if getattr(self, "delegated_child", False):
                # A delegated child has no session to preserve. It stops at
                # this safe boundary and tells its caller, so a draining
                # parent is not held by a child that cannot finish.
                self.bound_hit = "drain"
                return
            from .engine_config import effective
            if effective(self.config, self.role, self.role.profile).get("session") == "persistent":
                self.bound_hit = "drain"
        finally:
            self.mutex.release()

    #: The refusal the shared handshake carries when a drain bound refuses a
    #: provider request. The durable launcher classifies this exact text, so a
    #: delegated child's stop is the drain bound on every engine.
    DRAIN_REQUEST_REFUSED = "Drain bound refused the provider request"

    def drain_refusal(self, message: str):
        """Raise the refusal the drain bound requires at this boundary.

        A delegated child has no session to wrap up: it stops at the safe
        boundary and reports the interruption to its caller, so a draining
        parent is not held by a child that cannot finish. Every engine's
        admission path uses this, so one drain rule holds for all of them.

        Bounds: one message. Trust: the project's own drain state. Failure:
        raises ``Cancelled`` for a delegated child and ``DeadlineWrapUp``
        otherwise.
        """
        if getattr(self, "delegated_child", False):
            self.drain_admission_refused = True
            # The same text the adapters classify: a refusal the launcher
            # does not recognise is a provider failure, not the drain bound.
            raise Cancelled(self.DRAIN_REQUEST_REFUSED)
        raise DeadlineWrapUp(message)

    def _activity_bucket(self) -> str:
        return _run_activity.activity_bucket(self)

    def _activity_change(self, change, *, count: str | None = None) -> None:
        return _run_activity.activity_change(self, change, count=count)

    def _publish_activity(self) -> None:
        return _run_activity.publish_activity(self)

    def activity_summary(self) -> dict:
        return _run_activity.activity_summary(self)

    def record_ci_request(self, request_id, date, workflows, covered, *, state="unresolved",
                          attempt=1, reason_code=None, possibly_duplicate_spend=False,
                          existing_only=False):
        return _run_activity.record_ci_request(
            self, request_id, date, workflows, covered, state=state,
            attempt=attempt, reason_code=reason_code,
            possibly_duplicate_spend=possibly_duplicate_spend, existing_only=existing_only)

    def ci_request_summary(self) -> dict:
        return _run_activity.ci_request_summary(self)

    def clear_request_deadlines(self):
        with self.mutex:
            if self.request_deadlines:
                reaped = len(self.request_deadlines)
                self._activity_change(self.request_deadlines.clear)
                self._activity_reaped_requests += reaped

    def start_request(self, sequence):
        key = (self.execution_attempt, sequence)
        with self.mutex:
            if key in self.request_deadlines:
                return
            deadline = time.monotonic() + self.config.limits.model_request_seconds
            self._activity_change(lambda: self.request_deadlines.setdefault(key, deadline),
                                  count="model_request")
            self._activity_max_requests = max(self._activity_max_requests,
                                               len(self.request_deadlines))

    def end_request(self, sequence):
        key = (self.execution_attempt, sequence)
        with self.mutex:
            if key not in self.request_sequences:
                raise Denied("Unknown completed request")
            if key in self.request_deadlines:
                self._activity_change(lambda: self.request_deadlines.pop(key, None))
                self.transport_idle_deadline = time.monotonic() + self.config.limits.model_request_seconds

    def check_deadlines(self, *, wrap_up=False):
        if self.host_cancelled():
            raise Cancelled("Run stopped, paused or authority revoked")
        if not self.joins_idle.is_set():
            return  # Absolute deadlines keep ticking; never renew on join.
        if self.wall_expired():
            raise RunDeadlineExceeded("Whole-unit wall deadline exhausted")
        requests = tuple(self.request_deadlines.values())
        request_deadline = (min(requests) if requests else
                            None if self.operation_active.is_set() else self.transport_idle_deadline)
        # The reserved wall transition precedes a later request/idle timeout,
        # even if polling sees both at once. Earlier request bounds still fail;
        # neither transition nor joins renew an absolute request timer.
        if wrap_up and (request_deadline is None or
                        (self.wrap_up_at is not None and self.wrap_up_at < request_deadline)):
            self.enter_deadline_wrap_up()
            if self.finished is None and self.bound_hit == "deadline" and not self.wrap_up_attempted:
                raise DeadlineWrapUp(self._bound_message())
        if requests:
            if time.monotonic() >= min(requests):
                raise LimitExceeded("Logical model request deadline exhausted")
        elif not self.operation_active.is_set() and time.monotonic() >= self.transport_idle_deadline:
            raise LimitExceeded("Engine stalled between logical requests")

        if wrap_up:
            self.enter_drain_wrap_up()
            if self.bound_hit == "drain" and not self.wrap_up_attempted and self.finished is None:
                # The transport poll is a drain crossing like any other: the
                # same refusal, so a delegated child stops here instead of
                # taking a wrap-up turn it cannot finish.
                self.drain_refusal(self._bound_message())

    def transport_deadline(self):
        self.check_deadlines(wrap_up=True)
        if not self.joins_idle.is_set():
            return None
        requests = tuple(self.request_deadlines.values())
        if not requests and self.operation_active.is_set():
            return self.deadline  # Admitted Pi tools retain their own command/adapter bounds.
        request = min(requests) if requests else self.transport_idle_deadline
        return min(self.deadline, request) if self.deadline is not None else request

    def transport_cancelled(self):
        # send_bounded accepts a predicate; receive/process polling preserves
        # the specific timeout type via check_deadlines instead.
        if self.host_cancelled():
            return True
        if not self.joins_idle.is_set():
            return False
        requests = tuple(self.request_deadlines.values())
        if not requests and self.operation_active.is_set():
            return self.wall_expired()
        request = min(requests) if requests else self.transport_idle_deadline
        return self.wall_expired() or time.monotonic() >= request

    def _delegated_wrap_up_limits(self) -> tuple[int, int]:
        """Effective wrap-up allowance for a delegated child (clamped shares).

        The child share never exceeds the spend role's effective total minus
        one, so a small configured total cannot starve the caller's own seal:
        the caller always retains at least one wrap-up request and tool.
        """
        role = self.spend_role or self.role
        total_requests = role_wrap_up_requests(self.config, role)
        total_tools = role_wrap_up_tools(self.config, role)
        return (max(0, min(self.config.limits.delegate_wrap_up_requests, total_requests - 1)),
                max(0, min(self.config.limits.delegate_wrap_up_tools, total_tools - 1)))

    def _wrap_up_used_limit(self, kind: str) -> tuple[int, int]:
        """Apply both the aggregate allowance and the child's clamped share."""
        with self._spend.lock:
            own = getattr(self._spend, "wrap_up_" + kind)
            delegated = getattr(self._spend, "delegated_wrap_up_" + kind)
            role = self.spend_role or self.role
            total = (role_wrap_up_requests if kind == "requests" else role_wrap_up_tools)(self.config, role)
            if self.delegated_child:
                share = self._delegated_wrap_up_limits()[0 if kind == "requests" else 1]
                return delegated, min(share, total - own)
            return own + delegated, total

    def _wrap_up_request_used_limit(self) -> tuple[int, int]:
        return self._wrap_up_used_limit("requests")

    def _wrap_up_remaining(self) -> tuple[int, int]:
        with self._spend.lock:
            return tuple(limit - used for used, limit in (
                self._wrap_up_used_limit("requests"), self._wrap_up_used_limit("tools")))

    def _bound_message(self) -> str:
        requests_left, tools_left = self._wrap_up_remaining()
        how = f"{max(0, requests_left)} requests and {max(0, tools_left)} wrap-up tools"
        tools = ", ".join(tool["name"] for tool in tool_definitions(
            tuple(c for c in self.role.capabilities if c in WRAP_UP_OPS), role=self.role,
            config=self.config)) or "none"
        return (f"For the rest of this work unit only: per-run {self.bound_hit} "
                f"bound reached; wrap-up only ({tools}) with {how} left")
