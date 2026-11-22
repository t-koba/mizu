"""Context-side VCS read handler and its local listing helpers.

``_op_vcs_read`` owns the adapter-backed VCS read handler: argument checks,
the workspace gate, publication-receipt binding, the trusted adapter call,
CI-wait diagnostics and lineage observation. The other three methods are the
local publication-receipt, CI-request and CI-cost listings it delegates to.
``Context._op_vcs_read_ci_costs`` also calls ``self._op_vcs_read`` back, so all
four are mixed into ``Context`` to keep the ``vcs_read`` dispatch name and that
callback resolving unchanged. No behaviour, grant, default or seam changes.
"""
from __future__ import annotations

from . import ci_waits
from . import lineage
from . import vcs as _vcs
from .errors import Denied
from .fs import write_json
from .publication import Publication
from .ci_requests import CiRequests


class ContextVcsReadToolsMixin:
    def _op_vcs_read(self, args: dict) -> dict:
        # Read-only VCS view: CI status, logs, PR comments, external
        # proposals, and CI-recorded artifact files via the trusted
        # adapter. Never publishes; mutating ops are refused here even if
        # the adapter would serve them. The adapter-backed reads require a
        # visible workspace. A
        # publication_id is a status-only locator resolved through the
        # validated durable receipt; callers cannot override its branch/SHA.
        # Branch stays required for addressed reads
        # (status/log/comments/artifacts/artifact) inside read_via;
        # proposals enumerate with an optional branch filter; acquire
        # takes a proposal id, the exact head sha, and the explicit
        # content scope (plus an optional base sha pin for full scope)
        # and needs no branch; artifact takes a branch, an artifact file
        # name, an optional sha filter and paired byte offset/limit.
        # The publications op lists the project's recorded receipts from
        # local durable evidence and
        # contacts no provider.
        if "check" in args and args["op"] != "log":
            raise Denied("check is only valid for log reads")
        if args["op"] == "publications":
            # Local evidence listing: no adapter, no network, no workspace.
            return self._op_vcs_read_publications(args)
        if args["op"] == "ci_requests":
            # Local evidence listing: no adapter, no network, no workspace.
            return self._op_vcs_read_ci_requests(args)
        if self.role.workspace == "none":
            raise Denied("This role has no workspace")
        if args["op"] == "ci_costs":
            return self._op_vcs_read_ci_costs(args)
        if "offset" in args and args["op"] != "artifact":
            raise Denied("offset is only valid for CI costs or artifact byte pages")
        if "limit" in args and args["op"] not in {"artifact", "artifacts"}:
            raise Denied("limit is only valid for CI costs or artifact reads/listings")
        if "code_digest" in args:
            # The digest filter belongs to the local publications listing
            # only; an addressed read never accepts it.
            raise Denied("code_digest is only valid for the publications listing")
        params = {}
        publication = None
        if "publication_id" in args:
            if args["op"] != "status" or set(args) != {"op", "publication_id"}:
                raise Denied("publication_id status reads take no branch, sha, or other filters")
            publication = Publication.read_receipt(self.project.root, args["publication_id"])
            params["branch"] = publication["branch"]
            params["sha"] = publication["published_sha"]
            params["publication_id"] = publication["publication_id"]
        else:
            if "branch" in args:
                params["branch"] = args["branch"]
            if "sha" in args:
                params["sha"] = args["sha"]
        if "id" in args:
            params["id"] = args["id"]
        if "cursor" in args:
            params["cursor"] = args["cursor"]
        if "scope" in args:
            params["scope"] = args["scope"]
        if "base_sha" in args:
            params["base_sha"] = args["base_sha"]
        if "check" in args:
            params["check"] = args["check"]
        if "artifact" in args:
            params["artifact"] = args["artifact"]
        if args["op"] in {"artifact", "artifacts"}:
            params.update({key: args[key] for key in ("offset", "limit") if key in args})
        diagnose_waits = (publication is not None
                          and {"vcs_read", "work_items_read"} <= set(self.role.capabilities))
        try:
            # Keep the one adapter response for diagnostics, then restore the
            # ordinary exact-SHA checks view. No extra network observation.
            data = _vcs.read_via(self.project.vcs_settings(self.config), args["op"], params,
                             cancel=self.tools_cancelled,
                             **({"preserve_status_rows": True} if diagnose_waits else {}))
        except OSError as exc:
            raise Denied(f"VCS read is unavailable: {exc}") from exc
        if diagnose_waits:
            diagnostics = ci_waits.status_diagnostics(
                self.project.work_items, self.role.name, publication, data)
            data = {**data, "checks": [row for row in data["checks"]
                                       if row["sha"] == publication["published_sha"]]}
            if diagnostics is not None:
                data["ci_waits"] = diagnostics
        if publication is not None:
            current = data.pop("lineage_observation")
            expected = self.project.lineage_target
            data["lineage"] = lineage.status(publication, current, data.pop("lineage_reason"), expected)
            data["lineage"]["receipt"] = lineage.validate(publication.get("lineage"),
                                                          publication["published_sha"])
            data["lineage"]["observation"] = current
            observed = {
                "publication_id": publication["publication_id"],
                "source_commit": publication["source_commit"],
                "code_digest": publication["code_digest"],
                "branch": publication["branch"],
                "published_sha": publication["published_sha"],
                "approval": publication["approval"],
                "run": publication["run"],
            }
            if any(key in publication for key in ("projection_schema", "projection_algorithm",
                                                  "projection_digest")):
                observed = {**observed,
                            "projection_schema": publication["projection_schema"],
                            "projection_algorithm": publication["projection_algorithm"],
                            "projection_digest": publication["projection_digest"]}
            data = {**data, "publication": observed}
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": args["op"], "branch": params.get("branch"), "trust": "external-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence, "result": data}
        write_json(self.run_dir / evidence, record)
        return record

    def _op_vcs_read_publications(self, args: dict) -> dict:
        # Local listing of the project's recorded publication receipts.
        # No provider is contacted: this reads the project's own durable
        # evidence, so no adapter call is made. Only the op, the exact
        # code_digest/branch/published_sha filters and a page cursor are accepted.
        allowed = {"op", "code_digest", "branch", "published_sha", "cursor"}
        if set(args) - allowed:
            raise Denied("publication listing takes only code_digest, branch, published_sha and cursor filters")
        # The page reports the match count, whether more matches follow it and
        # whether a scan bound stopped the search, so an absent receipt is
        # never confused with an omitted one. Follow next_cursor to page.
        listing = Publication.list_receipts(
            self.project.root, code_digest=args.get("code_digest"),
            branch=args.get("branch"), published_sha=args.get("published_sha"), limit=100,
            cursor=args.get("cursor"), policy=self.config.publication_evidence)
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": "publications", "branch": args.get("branch"),
                  "trust": "recorded-local-untrusted", "evidence_id": evidence_id,
                  "evidence": evidence, "result": listing}
        write_json(self.run_dir / evidence, record)
        # The listing is also the top-level result, like the other read ops.
        return {**record, **listing}

    def _op_vcs_read_ci_requests(self, args: dict) -> dict:
        # Local listing of the project's recorded CI requests, grouped by day.
        # No provider is contacted: this reads the project's own durable
        # evidence, so no adapter call is made. Only the op is accepted.
        if set(args) - {"op"}:
            raise Denied("CI request listing takes no filters")
        listing = CiRequests(self.project.root, self.config.ci_requests).daily(limit=100)
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": "ci_requests", "trust": "recorded-local-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence, "result": listing}
        write_json(self.run_dir / evidence, record)
        # The listing is also the top-level result, like the other read ops.
        return {**record, **listing}

    def _op_vcs_read_ci_costs(self, args: dict) -> dict:
        from .ci_costs import observed_costs
        if set(args) - {"op", "limit", "offset"}:
            raise Denied("CI costs take only limit and offset")
        page = CiRequests(self.project.root, self.config.ci_requests).page(
            limit=args.get("limit", 8), offset=args.get("offset", 0))
        listing = observed_costs(page, lambda publication: self._op_vcs_read(
            {"op": "status", "publication_id": publication}))
        evidence_id, evidence = self._new_invocation_evidence("vcs-read")
        record = {"op": "ci_costs", "trust": "external-untrusted",
                  "evidence_id": evidence_id, "evidence": evidence, "result": listing}
        write_json(self.run_dir / evidence, record)
        return {**record, **listing}
