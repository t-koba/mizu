"""Context-side VCS read listings: publication receipts and CI records.

These three methods own the Context handlers for the project's recorded
publication receipts, CI request days and CI cost pages. The first two are
purely local evidence listings; the CI-cost page reads local request records
and resolves each distinct receipt through ``Context._op_vcs_read`` via
``self``. ``Context._op_vcs_read`` calls these helpers internally, so they are
mixed back into ``Context`` to keep that call site resolving unchanged. No
behaviour, grant, default or seam changes.
"""
from __future__ import annotations

from .errors import Denied
from .fs import write_json
from .publication import Publication
from .ci_requests import CiRequests


class ContextVcsReadToolsMixin:
    def _op_vcs_read_publications(self, args: dict) -> dict:
        # Local listing of the project's recorded publication receipts.
        # No provider is contacted: this reads the project's own durable
        # evidence, so no adapter call is made. Only the op and the exact
        # code_digest/branch filters are accepted.
        if set(args) - {"op", "code_digest", "branch", "published_sha"}:
            raise Denied("publication listing takes only code_digest, branch and published_sha filters")
        # The page reports the match count, whether the page dropped matches
        # and whether a scan bound stopped the search, so an absent receipt is
        # never confused with an omitted one.
        listing = Publication.list_receipts(
            self.project.root, code_digest=args.get("code_digest"),
            branch=args.get("branch"), published_sha=args.get("published_sha"), limit=100,
            policy=self.config.publication_evidence)
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
