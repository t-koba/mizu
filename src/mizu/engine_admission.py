"""Revalidate run authority and acquire Engine admission locks."""
from __future__ import annotations

import contextlib
import shutil

from . import dispatch_evidence, lock_diagnostics, safe_point
from .admission import admission, role_exclusion
from .budget import Budget
from .config import MEBIBYTE_BYTES, Role
from .delegation_jobs import DelegationJobs
from .errors import Busy, Denied, InfraExceeded
from .fs import ID
from .project import Project


class EngineAdmissionMixin:
    def _check_admission(self, project: Project) -> None:
        """Fresh authority and spend guards; neither selects nor consumes a request."""
        if self.stop.is_set():
            raise Busy("Run stopped before execution admission")
        control = project.control()
        if not control.get("armed") or control.get("paused"):
            raise Denied("Project is unarmed or paused")
        if control.get("draining"):
            raise Busy("Project is draining; no new runs admitted")
        usage = Budget(self.config.data / "budget", self.config.limits.daily_requests,
                       self.config.limits.retention_days,
                       self.config.limits.shared_daily_requests, self.config.timezone,
                       day_file_bytes=self.config.limits.budget_day_file_bytes).usage(project.name)
        if usage["limit"] <= usage["used"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request budget unavailable for project '{project.name}'")
        if usage["shared_limit"] > 0 and usage["shared_used"] >= usage["shared_limit"]:
            raise InfraExceeded(f"{self.config.timezone} daily model-request shared budget unavailable")
        if shutil.disk_usage(project.root).free < self.config.limits.free_disk_mb * MEBIBYTE_BYTES:
            raise InfraExceeded("Free disk space is below the configured reserve")

    def _admit(self, project: Project, role_name: str, role: Role, stack: contextlib.ExitStack,
               *, wait: bool, enqueue: bool = False, observation_sink=None, identity_sink=None) -> float:
        """Hold dispatch locks after FIFO admission and return its elapsed time."""
        resources = [role_exclusion(project.root, role_name)]
        if role.workspace == "write":
            resources.append(project.root / "locks" / "workspace.lock")
        # Only fixed configuration is known before admission. A selector's
        # eventual dispatch profile is not a hint about this queue transaction.
        hints = {"profile": None, "engine": None,
                 "profile_engine_reason": "selector_not_resolved_at_admission" if role.selector
                                          else "configured_hint_unavailable"}
        if observation_sink is not None and not role.selector:
            with contextlib.suppress(Exception):
                if isinstance(role.profile, str) and ID.fullmatch(role.profile):
                    hints = {"profile": role.profile, "engine": self.config.engine(role.profile),
                             "profile_engine_reason": None}
        def observe(row):
            observation_sink({**row, **hints})
        admission_kwargs = {
            "stop": self.stop,
            "check": lambda: self._check_admission(project),
        }
        # Ticket metadata is needed only if this request queues or a contended
        # transaction records its alternatives. Defer identity work until then.
        admission_kwargs["evidence_source_factory"] = lambda: dispatch_evidence.AdmissionSource(
            self.config, project, role, ephemeral=self.ephemeral)
        admission_kwargs["capture_choices"] = self.config.limits.queue_observations > 0
        if identity_sink is not None:
            admission_kwargs["identity_sink"] = identity_sink
        if observation_sink is not None:
            admission_kwargs["observation_sink"] = observe
        elapsed = stack.enter_context(admission(self.config, resources, wait=wait, enqueue=enqueue,
                                                **admission_kwargs))
        lock_diagnostics.phase("post-admission")
        # Trusted starts hold workspace.lock too. Recheck after admission has
        # acquired it, before safe-point inspection or any live-tree work.
        DelegationJobs.guard(project.root)
        if role.workspace == "write":
            safe_point.guard(project.snapshots, role_name)
        return elapsed
