"""Context-side VCS branch retirement handler.

``_op_vcs_retire`` owns the adapter-backed branch retirement: the workspace
gate, the live-proposal reference check, mutation evidence, the trusted
adapter call and the returned record with its observation. No behaviour,
grant, default or seam changes.
"""
from __future__ import annotations

from . import vcs as _vcs
from .errors import Denied


class ContextVcsRetireMixin:
    def _op_vcs_retire(self, args: dict) -> dict:
        # Branch retirement: delete an owned temporary integration branch
        # at an exact expected sha behind the configured retire grant.
        # Classification is enforced inside retire_via (owned only);
        # protected, external, tracking, and other refs are preserved.
        # A failed call implies nothing about the remote ref: reconcile by
        # re-observing, never by assuming deletion. Retirement is tied to
        # terminal disposition twice: recorded observations refuse early
        # before dispatch (they may be stale), and the provider must
        # affirm atomically with the delete that no live proposal
        # references the branch, naming blockers in live_refs.
        # Integration authority is the capability plus the configured
        # grant, never source-write access: read-workspace integrators
        # retire without touching the shared workspace.
        if self.role.workspace == "none":
            raise Denied("Branch retirement requires a visible workspace")
        blocking = _vcs.live_proposal_refs(self.project, args.get("branch"))
        if blocking:
            raise Denied("Branch retirement is blocked by open proposal "
                         f"reference(s) {', '.join(blocking)}; dispose the "
                         "proposal to a terminal state and re-observe before "
                         "retiring")
        before_invoke, observe = self._mutation_evidence(
            "vcs-retire", "retire",
            {"branch": args.get("branch"), "expected_sha": args.get("expected_sha")})
        try:
            data = _vcs.retire_via(self.config.vcs, args.get("branch"),
                                   args.get("expected_sha"), cancel=self.tools_cancelled,
                                   before_invoke=before_invoke)
        except OSError as exc:
            raise Denied(f"Branch retirement is unavailable: {exc}") from exc
        record = {"retired": True, "branch": data["branch"], "sha": data["sha"],
                  "classification": data["classification"],
                  "live_refs": data["live_refs"],
                  "trust": "external-untrusted",
                  "result": data}
        return observe(record)
