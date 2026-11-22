"""Context-side VCS proposal disposition handler.

``_op_vcs_dispose`` owns the close/merge grant and approval gates, the
conditional close one-shot reservation, mutation evidence, the trusted
adapter calls and the returned records with their observations. It is mixed
back into ``Context`` so the ``vcs_dispose`` dispatch name and grant checks
are unchanged; call-side patch targets follow the handler's new owner. No
behaviour, grant or default changes.
"""
from __future__ import annotations

from . import vcs as _vcs
from .errors import Denied


class ContextVcsDisposeMixin:
    def _op_vcs_dispose(self, args: dict) -> dict:
        # Proposal disposition: close/merge an external proposal at the
        # exact assessed head, base, and target behind the matching
        # per-action grant; close_if_adopted is a separately pinned,
        # adapter-conditional close that retains even a refusal receipt.
        # Close is routine terminal reconciliation
        # behind close_grant alone. Merge promotes the integrated tree:
        # it additionally requires a recorded human GO approval for the
        # tree, checked before the adapter spawns, so main/release
        # promotion without a matching approval is refused and approval
        # is never inferred from a role or CI. The adapter must act only
        # while the proposal is still open at all assessed endpoints and
        # echo them with the terminal state. Never automatic; a failed
        # call implies nothing about the remote proposal.
        # Integration authority is the capability plus the configured
        # grant (and approval for merges), never source-write access:
        # read-workspace integrators dispose from their materialized
        # input, whose digest binds a merge approval exactly like a
        # writable workspace capture. The approval additionally binds the
        # assessed destination and proposal endpoints, checked here before
        # the adapter spawns: a GO for another target or other proposal
        # content never authorizes this merge.
        if self.role.workspace == "none":
            raise Denied("Proposal disposition requires a visible workspace")
        op = args.get("op")
        if op == "close_if_adopted":
            expected = {"op", *_vcs.CLOSE_IF_ADOPTED_PINS}
            if set(args) != expected:
                raise Denied("Conditional close requires op and exactly the six observed pins")
            pins = {key: args[key] for key in _vcs.CLOSE_IF_ADOPTED_PINS}

            before_invoke, observe = self._mutation_evidence("vcs-dispose", op, pins)

            def reserve_conditional_close():
                # Called only after local grant/pin validation and immediately
                # before adapter dispatch. Failed local input can be corrected;
                # a dispatched attempt can never overwrite or retry its receipt.
                if self.conditional_close_attempted:
                    raise Denied("Conditional proposal close was already attempted in this work unit")
                self.conditional_close_attempted = True
                before_invoke()

            try:
                data = _vcs.close_if_adopted_via(
                    self.config.vcs, pins, cancel=self.tools_cancelled,
                    before_invoke=reserve_conditional_close)
            except OSError as exc:
                raise Denied(f"Conditional proposal close is unavailable: {exc}") from exc
            record = {"disposed": data["closed"], "op": data["op"],
                      **pins, "predicate": data["predicate"],
                      "closed": data["closed"],
                      "trust": "external-untrusted",
                      "adapter_receipt": data["adapter_receipt"]}
            # A valid refusal is durable evidence too: do not refetch,
            # retry, fall through to merge, or treat false as an exception.
            return observe(record)
        if op not in ("close", "merge") or set(args) != {
                "op", "id", "sha", "base", "target", "branch"}:
            raise Denied("Invalid proposal disposition arguments")
        approval = None
        code_digest = None
        if op == "merge":
            self.close_exec()
            captured = self.project.snapshots.capture_files(self.workspace)
            if captured.get("skipped"):
                raise Denied("Proposal disposition requires a representable snapshot")
            code_digest = captured["code_digest"]
            approval = _vcs.require_go_approval(
                self.project, args["branch"], code_digest,
                endpoints={"target": args.get("target"), "id": args.get("id"),
                           "sha": args.get("sha"), "base": args.get("base")},
                queue=self.config.vcs.get("approval_queue"))
        parameters = {key: value for key, value in args.items() if key != "op"}
        if approval is not None:
            parameters.update(code_digest=code_digest, approval=approval["approval"])
        before_invoke, observe = self._mutation_evidence("vcs-dispose", op, parameters)
        try:
            data = _vcs.dispose_via(self.config.vcs, args["op"], args.get("id"),
                                    args.get("sha"), args.get("base"),
                                    args.get("target"), cancel=self.tools_cancelled,
                                    before_invoke=before_invoke)
        except OSError as exc:
            raise Denied(f"Proposal disposition is unavailable: {exc}") from exc
        record = {"disposed": True, "op": args["op"], "id": data["id"],
                  "sha": data["sha"], "base": data["base"],
                  "target": data["target"], "state": data["state"],
                  "branch": args["branch"],
                  "trust": "external-untrusted",
                  "result": data}
        if approval is not None:
            record["code_digest"] = code_digest
            record["approval"] = approval["approval"]
        return observe(record)
