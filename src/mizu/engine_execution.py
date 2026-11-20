"""Engine model execution: provider dispatch, bounded turns and checkpoint seals.

These methods own one Engine's inference boundary -- driver resolution, one
host-verified finish-only seal, optional safe-point handoff and the bounded
missing-finish/deadline follow-ups. They are mixed back into ``Engine``, so
every existing ``Engine.method`` reference and ``patch.object(Engine, ...)``
call site keeps resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from . import dispatch_evidence, safe_point
from .admission import has_live_demand
from .config import ConfigError
from .context_lifecycle import WRAP_UP_OPS
from .drivers import DriverCheckpoint, driver_for
from .errors import (Cancelled, DeadlineWrapUp, Denied, DrainYieldExceeded,
                     MissingFinish, MizuError, ModelFailure, ProtocolError,
                     RequestYieldExceeded, RunDeadlineExceeded, ToolYieldExceeded)
from .fs import now, write_json

if TYPE_CHECKING:
    from .runtime import Context


class EngineExecutionMixin:
    def resolve(self, profile: str):
        """Return the driver for a profile's engine (registry) or the injected test driver."""
        if self.driver is not None:
            return self.driver
        return driver_for(self.config, profile, self._drivers)

    def _dispatch(self, context: Context, driver, prompt: str, **kwargs) -> dict:
        # Retain host exception identity across an adapter's error event. Model
        # text can neither fabricate a local bound nor change its accounting.
        context.admission_error = None
        context.admission_exception = None
        context.admission_wait = False
        context.tool_admission_exception = None
        try:
            result = dispatch_evidence.execute(context, driver, prompt, **kwargs)
            if (context.finished is None and isinstance(context.tool_admission_exception,
                    (ToolYieldExceeded, RequestYieldExceeded, RunDeadlineExceeded, DrainYieldExceeded))):
                # A normally settled but unsealed exhausted reserve is still
                # bounded, not a reason to start missing-finish follow-ups.
                raise context.tool_admission_exception
            return result
        except ModelFailure as exc:
            if isinstance(context.admission_exception, MizuError):
                raise context.admission_exception from exc
            if context.tool_admission_exception is not None:
                raise context.tool_admission_exception from exc
            raise

    def _seal_driver_checkpoint(self, context: Context, checkpoint: DriverCheckpoint, *, profile=None,
                                safe_point_yield=False) -> dict:
        """Consume a host-verified boundary for exactly one finish-only query/turn.

        The caller must own the scheduling decision. Never use _execute here:
        its missing-finish and deadline follow-ups would authorize more than one
        model unit.
        The caller retains its workspace/slot ownership and handles publication.
        A seal's outcome is untouched; any failure propagates without retry.
        """
        with context.mutex:
            context.check_deadlines(wrap_up=True)
            profile = profile or context.role.profile
            authority = context.turn_authority_role or context.role
            if (checkpoint is None or context.config.engine(profile) != checkpoint.engine
                    or context.role.workspace != "write" or authority.workspace != "write"
                    or context.delegated_child or context.ephemeral
                    or context.role.engine_tools or authority.engine_tools
                    or "finish" not in context.role.capabilities):
                raise Denied("Checkpoint seal requires a supported driver and finish grant")
            if safe_point_yield:
                safe_point.begin(context, profile)
                # The durable fence precedes the one-shot checkpoint claim. If
                # stop, authority or a deadline wins that race, retire only
                # this exact not-yet-sealed marker and preserve the old path.
                context.safe_point_yield_requested = True
            try:
                context.claim_driver_checkpoint(checkpoint)
            except BaseException:
                if safe_point_yield:
                    safe_point.abandon(context)
                    context.safe_point_yield_requested = False
                raise
            previous_continuation = context.turn_continuation
            context.turn_continuation = True
            context.execution_attempt += 1
            context.hello.clear()
            context.operations_stopped.clear()
            context._checkpoint_launch_pending = True
        try:
            driver = driver_for(context.config, profile, self._drivers)
            result = self._dispatch(context, driver,
                "This is a finish-only seal phase for the completed work unit. "
                "Call mizu_finish exactly once with an accurate outcome, summary and retained state. "
                "Do no further work; no operation may follow the seal.", profile=profile)
            context.check_deadlines(wrap_up=True)
            if context._checkpoint_seal_error is not None:
                raise context._checkpoint_seal_error
            if context.finished is None:
                raise ProtocolError("Checkpoint seal unit ended without finish")
            if not context._checkpoint_settled:
                raise ProtocolError("Checkpoint seal unit did not prove quiescent teardown")
            return result
        except Exception:
            context.check_deadlines(wrap_up=True)
            if context._checkpoint_seal_error is not None:
                raise context._checkpoint_seal_error
            raise
        finally:
            with context.mutex:
                context._checkpoint_launch_pending = False
                context._checkpoint_driver_started = False
                context.operations_stopped.set()
                context.turn_continuation = previous_continuation

    def _maybe_safe_point_yield(self, context: Context, *, profile=None) -> dict | None:
        """Seal once only at a configured unit quantum with live FIFO demand.

        The observation grants no admission or priority: the next invocation
        still uses ordinary FIFO admission. Invalid/contended queue evidence is
        no demand. Drain, deadline, stop and authority paths keep their existing
        transitions and never enter this optional path.
        """
        quantum = context.config.limits.safe_point_yield_quantum
        if (quantum == 0 or context.finished is not None or context.bound_hit is not None
                or context.wrap_up_attempted or context.safe_point_yield_requested
                or context.driver_checkpoint is None
                or context.completed_driver_units % quantum != 0
                or context.host_cancelled() or context.wall_expired()):
            return None
        # Drain remains a separate opt-in transition even when drain_yield is
        # disabled for this role/session. A safe-point yield must not mask it.
        if context.project.control().get("draining"):
            return None
        profile = profile or context.role.profile
        checkpoint = context.driver_checkpoint
        try:
            if context.config.engine(profile) != checkpoint.engine:
                return None
        except (ConfigError, KeyError, TypeError, ValueError):
            return None
        if "finish" not in context.role.capabilities or not has_live_demand(context.config):
            return None
        return self._seal_driver_checkpoint(context, checkpoint, profile=profile,
                                            safe_point_yield=True)

    def _execute(self, context: Context, driver, prompt: str, **kwargs) -> dict:
        """Preserve a durable host seal and recover only normal unsealed returns.

        A normally settled turn without a seal gets the configured continuation
        allowance. With no prior tool calls it retains the work grant; after
        tool activity, follow-ups are finish-only. Provider/protocol faults and
        explicit cancellation keep their existing paths. Deadline wrap-up remains
        separate and bounded.
        Final record, integrity, and publication failures still fail closed.
        A request attempted after the seal short-circuits without calling
        the driver and settles with the sealed outcome (failed settlement
        evidence; the run still publishes the seal).
        """
        try:
            if context.finished is not None:
                raise Denied("No inference after the work unit is sealed")
            context.clear_request_deadlines()
            context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
            try:
                model_result = self._dispatch(context, driver, prompt, **kwargs)
                if context.finished is None:
                    context.enter_drain_wrap_up()
                    if context.bound_hit == "drain" and not context.wrap_up_attempted:
                        context.drain_refusal(context._bound_message())
                yielded = self._maybe_safe_point_yield(context, profile=kwargs.get("profile"))
                if yielded is not None:
                    model_result = yielded
            except DeadlineWrapUp as exc:
                if context.finished is not None:
                    raise
                if context.safe_point_yield_requested:
                    # The one permitted finish-only seal already consumed the
                    # checkpoint. Do not turn its deadline failure into a
                    # second provider call; preserve the workspace fence and
                    # record the ordinary bounded deadline result.
                    raise RunDeadlineExceeded("Safe-point seal reached the deadline without finish") from exc
                if context.host_cancelled():
                    raise Cancelled("Run stopped before wrap-up")
                if context.wall_expired():
                    raise RunDeadlineExceeded("Whole-unit wall deadline exhausted")
                context.wrap_up_attempted = True
                previous_authority = context.turn_authority_role
                previous_continuation = context.turn_continuation
                authority = previous_authority or context.role
                context.turn_authority_role = authority
                context.turn_continuation = True
                previous_session_capabilities = context.session_identity_capabilities
                if context.bound_hit in ("drain", "requests"):
                    context.session_identity_capabilities = authority.capabilities
                try:
                    context.role = dataclasses.replace(authority,
                        capabilities=tuple(c for c in authority.capabilities if c in WRAP_UP_OPS),
                        engine_tools=(), selector="")
                    context.operations_stopped.clear()
                    context.admission_error = None
                    context.admission_exception = None
                    context.clear_request_deadlines()  # The failed transport was reaped, not renewed.
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    context.execution_attempt += 1
                    model_result = self._dispatch(context, driver, context._bound_message() + "\n\n" + prompt, **kwargs)
                finally:
                    context.turn_authority_role = previous_authority
                    context.turn_continuation = previous_continuation
                    context.session_identity_capabilities = previous_session_capabilities

            if context.finished is None:
                if context.host_cancelled():
                    raise Cancelled("Run stopped before finish")
                if context.bound_hit == "drain" and context.request_deadlines:
                    raise ProtocolError("Terminal drain wrap-up left an active model-request deadline")
                # The reserved deadline transition (or an already-used wrap-up)
                # is authoritative; a missing finish there follows its existing
                # bounded/failure behavior, not this separate policy.
                if context.wrap_up_attempted and context.bound_hit == "requests":
                    raise RequestYieldExceeded("Request wrap-up ended without a seal")
                if context.wrap_up_attempted or context.bound_hit == "deadline" or context.wall_expired():
                    if context.wall_expired() or context.bound_hit == "deadline":
                        context.missing_finish_evidence = {
                            "configured_followups": context.config.limits.missing_finish_followups,
                            "used_followups": 0, "exhausted": False,
                            "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    return model_result
                if context.request_deadlines:
                    raise ProtocolError("Terminal turn left an active model-request deadline")

                configured = context.config.limits.missing_finish_followups
                used = 0
                base_role = context.role
                authority = context.turn_authority_role or base_role
                while context.finished is None and used < configured:
                    if context.host_cancelled():
                        raise Cancelled("Run stopped before missing-finish follow-up")
                    if context.wall_expired():
                        context.missing_finish_evidence = {
                            "configured_followups": configured, "used_followups": used,
                            "exhausted": False, "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                        return model_result
                    if context.request_deadlines:
                        raise ProtocolError("Terminal turn left an active model-request deadline")
                    context.check_deadlines()
                    used += 1
                    did_tool_work = bool(context.tool_count or context.engine_tool_invocations)
                    if did_tool_work:
                        followup_prompt = (
                            "The prior turn returned normally without calling mizu_finish. "
                            "This work unit remains open. For the rest of this work unit only: "
                            "do not repeat the work or call any other tool; "
                            "call mizu_finish now with an accurate outcome and summary. No action may follow the seal.")
                        followup_role = dataclasses.replace(base_role, capabilities=("finish",),
                                                            engine_tools=(), selector="")
                    else:
                        followup_prompt = (
                            "The prior turn returned normally without calling mizu_finish or any tool. "
                            "This work unit has not started. Begin the requested work using the original "
                            "goal and state in this session, use your granted tools as needed, and then "
                            "call mizu_finish with an accurate outcome and summary. No action may follow the seal.")
                        followup_role = base_role
                    context.missing_finish_evidence = {
                        "configured_followups": configured, "used_followups": used,
                        "exhausted": False, "sealed": False,
                        "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    context.execution_attempt += 1
                    previous_authority = context.turn_authority_role
                    previous_continuation = context.turn_continuation
                    context.turn_authority_role = authority
                    context.turn_continuation = True
                    context.role = followup_role
                    previous_session_capabilities = context.session_identity_capabilities
                    context.session_identity_capabilities = base_role.capabilities
                    context.operations_stopped.clear()
                    context.transport_idle_deadline = time.monotonic() + context.config.limits.model_request_seconds
                    try:
                        model_result = self._dispatch(context, driver, followup_prompt, **kwargs)
                        yielded = self._maybe_safe_point_yield(context, profile=kwargs.get("profile"))
                        if yielded is not None:
                            model_result = yielded
                    finally:
                        context.role = base_role
                        context.turn_authority_role = previous_authority
                        context.turn_continuation = previous_continuation
                        context.session_identity_capabilities = previous_session_capabilities
                    if context.finished is None and context.request_deadlines:
                        raise ProtocolError("Finish follow-up left an active model-request deadline")
                    if context.finished is None and (context.bound_hit == "deadline" or context.wall_expired()):
                        context.missing_finish_evidence = {
                            "configured_followups": configured, "used_followups": used,
                            "exhausted": False, "stopped_by": "run_deadline",
                            "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                        return model_result

                if context.finished is None and context.host_cancelled():
                    raise Cancelled("Run stopped before missing-finish exhaustion")
                if context.finished is None:
                    context.missing_finish_evidence = {
                        "configured_followups": configured, "used_followups": used,
                        "exhausted": True,
                        "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                    raise MissingFinish(configured=configured, used=used,
                                        counts_as_failure=context.config.limits.missing_finish_counts_as_failure)

                context.missing_finish_evidence = {
                    "configured_followups": configured, "used_followups": used,
                    "exhausted": False, "sealed": True,
                    "counts_as_failure": context.config.limits.missing_finish_counts_as_failure}
                context.model_evidence = {**getattr(context, "model_evidence", {}),
                                          "missing_finish": context.missing_finish_evidence}
                if isinstance(model_result, dict):
                    model_result = {**model_result, "missing_finish": context.missing_finish_evidence}

            return model_result
        except Exception as exc:
            context.wait_for_joins()
            if context.finished is None:
                raise
            if context.host_cancelled():
                raise Cancelled("Run stopped after finish") from exc
            settlement = {"status": "failed", "execution_attempt": context.execution_attempt,
                          "error_type": type(exc).__name__[:128], "error": str(exc)[:4000],
                          "recorded_at": now()}
        # No synthetic session or durable-Pi result: only evidence already
        # observed by the driver, explicitly marked as not settled.
        context.model_evidence = {**getattr(context, "model_evidence", {}),
                                  "settled": False, "settlement": settlement}
        write_json(context.run_dir / "adapter-settlement.json", settlement)
        return context.model_evidence
