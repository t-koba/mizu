"""Delegation request resolution, role gating and retry admission for Engine.

These methods own the caller-authoritative child-role and request resolution
boundary, plus the capability gate that keeps verification and publication in
the caller. They are mixed back into ``Engine``, so every existing
``Engine.method`` reference and ``patch.object(Engine, ...)`` call site keeps
resolving unchanged. No behaviour, grant or default changes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import time
from typing import TYPE_CHECKING

from .config import ConfigError
from .delegation_jobs import DelegationJobs, _WorkspaceLease
from .errors import Cancelled, Denied, RunDeadlineExceeded
from .fs import canonical, loads_finite

if TYPE_CHECKING:
    from .config import Config, Role
    from .project import Project
    from .runtime import Context


#: Capabilities a delegation child must never hold. The child works in the
#: caller's live workspace, so queue-write/vcs-mutating grants
#: and redelegation stay with the caller: verification and publication
#: never leave it. Implementation grants (files/read/exec/search/fetch and
#: the child's own profile/selector/policy) stay operator choice. finish
#: stays allowed: every configured role carries it so the child can seal,
#: and delegation ensures it (idempotent when already present).
DELEGATE_FORBIDDEN = frozenset({"delegate", "delegate_join",
                                "vcs_publish", "vcs_retire", "vcs_dispose", "vcs_request",
                                "work_items_create", "work_items_transition", "work_items_reopen",
                                "work_order", "queues", "review_verdict"})


def check_delegate_role(role_name: str, role) -> None:
    """Single property-based delegation gate (fail closed before any model call).

    Schema: role has ``capabilities``. Bounds: none beyond the capability
    set. Trust: configuration, not model output. Retry/cancellation: n/a
    (pre-model validation). Evidence: ConfigError names the role. Failure:
    raises ConfigError before any model call or budget use.
    """
    if set(role.capabilities) & DELEGATE_FORBIDDEN:
        raise ConfigError(
            f"Delegation role {role_name} must not hold "
            f"{sorted(DELEGATE_FORBIDDEN)}")


#: Capabilities that let a consultation role change shared state or execute
#: code. Consultation must stay read-only and advisory: workspace "read" plus
#: none of these. Read-only grants (files/read/diff/fetch/probe/search)
#: stay operator choice, plus the required finish.
CONSULT_FORBIDDEN = frozenset({"exec", "experiment", "verify", "consult", "report", "sync",
                               "vcs_publish", "vcs_retire", "vcs_dispose", "vcs_request",
                               "research", "materialize", "work_items_create", "work_items_transition", "work_items_reopen",
                               "work_order", "queues", "review_verdict"})


def check_consult_role(role_name: str, role) -> None:
    """Single property-based consultation gate (runtime + smoke share this).

    Schema: role has ``workspace`` and ``capabilities``. Bounds: none beyond
    the capability sets. Trust: configuration, not model output.
    Retry/cancellation: n/a (pre-model validation). Evidence: ConfigError names
    the role. Failure: raises ConfigError before any model call or budget use.
    """
    if role.workspace != "read" or set(role.capabilities) & CONSULT_FORBIDDEN:
        raise ConfigError(
            f"Consultation role {role_name} must be read-only without "
            f"{sorted(CONSULT_FORBIDDEN)}")


class EngineDelegationMixin:
    def _delegate_role(self, parent_role, role_name):
        """Shared post-delegation role: caller tree, child grants, no owned jobs."""
        role_name = role_name or getattr(self.config, "delegate_role", "")
        if not role_name:
            raise Denied("Delegation needs an explicit role or a configured delegate_role")
        if role_name == parent_role.name:
            raise Denied("A role cannot delegate to itself")
        if role_name not in self.config.roles:
            raise ConfigError(f"Delegation role is not configured: {role_name}")
        role = self.config.roles[role_name]
        check_delegate_role(role_name, role)
        return dataclasses.replace(
            role, workspace=parent_role.workspace,
            capabilities=tuple(c for c in dict.fromkeys([*role.capabilities, "finish"])
                               if c not in DELEGATE_FORBIDDEN and c != "check_job"))

    def _delegate_profile(self, project, role, snapshot):
        if role.selector:
            # Same trusted, config-only preview as synchronous delegation;
            # never consume routing or guess a profile when selection waits.
            from .classification import prepare
            decision = prepare(self, project, role, snapshot, None, preview=True)
            profile = decision.get("profile") or ""
            if not profile:
                raise Denied("Delegation selector has no selectable profile: "
                             + str(decision.get("reason") or "waiting"))
            role = dataclasses.replace(role, profile=profile, selector="")
        return role

    def _delegation_request(self, parent, request):
        return self._resolve_delegation_request(
            parent.project, parent.role.name, request, parent=parent)

    def _resolve_delegation_request(self, project, owner, request, *, parent=None):
        """Read-only authority resolution, not permission to claim or launch.

        Recovery supplies the trusted job owner and immutable resolved request;
        legacy/unbound requests refuse. Admission supplies its live parent and
        an unresolved request, and must reproduce the same accounting authority.
        No Context, lease, current snapshot pointer or saved hash is recovery
        authority. Only the final recomputed request leaves this resolver.
        """
        from .config import role_policy_text
        from .delegation_jobs import _request
        from .engine_config import (adapter_digest, child_authority_digest,
                                    effective, local_settings_digest)
        _request(request)
        bound = "authority_digest" in request
        if bound != (parent is None):
            raise Denied("Expected a resolved recovery or unresolved admission request")
        if owner not in project.roles or owner not in self.config.roles:
            raise Denied("Delegation owner is not enabled")
        spend_role = self.config.roles[owner]
        if spend_role.workspace != "write":
            raise Denied("Delegation owner requires the project workspace")
        if "delegate" not in spend_role.capabilities:
            raise Denied("Delegation owner lacks the delegation grant")
        snapshot = project.snapshots.get(request["source_snapshot"])
        role = self._delegate_profile(
            project, self._delegate_role(spend_role, request["role"]), snapshot)
        if request["profile"] is not None and request["profile"] != role.profile:
            raise Denied("Delegation profile differs from resolved role")
        settings = effective(self.config, role, role.profile)
        policy = role_policy_text(role)
        adapter = adapter_digest(settings["engine"])
        local = local_settings_digest(self.config, settings["engine"])

        def authority(spend, goal, workspace):
            return child_authority_digest(
                self.config, role, spend_role=spend, settings=settings, policy=policy,
                goal_digest=goal, workspace=workspace, adapter_identity=adapter,
                local_settings=local)

        resolved = {**request, "role": role.name, "profile": role.profile,
                    "authority_digest": authority(spend_role, snapshot["goal_digest"], project.workspace)}
        if parent is not None:
            if resolved["authority_digest"] != authority(
                    parent.spend_role or parent.role, parent.goal_digest, parent.workspace):
                raise Denied("Parent authority cannot be reconstructed from owner and source")
        elif resolved != request:
            raise Denied("Delegation authority changed")
        return resolved

    def _reserve_delegation(self, parent: Context, key: str, request: dict):
        """Trusted admitted-parent reservation groundwork only; never launch.

        Hold the dispatch mutex through start AND store refresh. A failed read
        leaves an unknown (deny-all) gate, not an inferred absence. The original
        run ExitStack retains its admission lock through the durable commit.
        """
        with parent.mutex:
            lease = parent._workspace_lease
            if type(lease) is not _WorkspaceLease:
                raise Denied("Missing admitted workspace lease")
            with lease.start_scope(parent.project.root, parent.role.name,
                                   self, parent, key, request) as pinned_request:
                parent._reservation_view = {}  # Fail closed on uncertain I/O.
                try:
                    DelegationJobs(parent.project.root).start(parent.role.name, key, pinned_request)
                finally:
                    parent._reservation_view = DelegationJobs.reservation_projection(parent.project.root)
                return loads_finite(canonical(parent._reservation_view))

    def _selection_retry_wait(self, context: Context, project: Project, stack: contextlib.ExitStack,
                              selection_role: Role, role_name: str, wait_seconds: float) -> None:
        """Bounded retry wait holding no execution slot and spending nothing.

        Mechanism: releases dispatch admission (slot plus role/workspace
        locks), polls at idle_seconds with stop wake, revalidates
        authority/budget without consuming anything, then fail-fast
        re-admits. Waiting never renews deadlines, consumes budget or
        requests, or holds a slot. Budget, stop, wall/deadline or
        re-admission failure abandons the retry; the caller then drops the
        unit through the existing path for the raised error (original
        failure deferred, or the infra/authority/bound error itself).
        """
        stack.close()
        context._slot_lease = False
        context._admission_id = None
        deadline = time.monotonic() + max(0.0, wait_seconds)
        idle = self.config.limits.idle_seconds
        while True:
            if self.stop.is_set() or context.host_cancelled():
                raise Cancelled("Run stopped before selection retry")
            if context.wall_expired():
                raise RunDeadlineExceeded("Whole-unit wall deadline exhausted before selection retry")
            self._check_admission(project)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.stop.wait(min(idle, remaining))
        self._admit(project, role_name, selection_role, stack, wait=False,
                    observation_sink=getattr(context, "_admission_observation_sink", None),
                    identity_sink=lambda identity: setattr(context, "_admission_id", identity))
        context._slot_lease = True
        if context.role.workspace == "write":
            context._workspace_lease = _WorkspaceLease(self, context, stack)
