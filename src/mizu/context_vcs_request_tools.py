"""Context-side VCS CI request handler and its private coverage helpers.

``_op_vcs_request`` owns the exact-commit CI request lifecycle: workflow
sorting and attempt identity, the recorded/reconciled replay paths, the retry
gate and prior-spend check, the dispatch marker and durable evidence, and the
covered work-item waits. The other three methods are its private coverage
validation, state and record-rebuild helpers. All four are mixed back into
``Context`` so the ``vcs_request`` dispatch name and its checks are unchanged;
call-side patch targets follow the handler's new owner. No behaviour, grant,
default or seam changes.
"""
from __future__ import annotations

from . import vcs as _vcs
from .ci_requests import CiRequests, request_id as ci_request_id
from .errors import Denied
from .fs import canonical, now, write_json
from .publication import Publication

#: Bounded serialized size of the work items a CI request covers.
MAX_COVERED_BYTES = 2048


class ContextVcsRequestToolsMixin:
    def _op_vcs_request(self, args: dict) -> dict:
        # Ask the provider for one CI run per workflow at an exact published
        # commit. The target is a completed publication receipt, never a
        # branch head: the product verifies every returned run's head sha
        # against that commit and fails closed on any other binding. A
        # recorded request replays, and an unresolved dispatch is never
        # retried blindly, because a second request would spend a second run
        # for the same commit.
        receipt = Publication.read_receipt(self.project.root, args["publication_id"])
        sha = receipt["published_sha"]
        # One request per set of workflows: a reordered retry is the same
        # request and replays its recorded outcome.
        workflows = sorted(str(name) for name in args["workflows"])
        attempt = args.get("attempt", 1)
        requests = CiRequests(self.project.root, self.config.ci_requests)
        identity = ci_request_id(receipt["publication_id"], sha, workflows, attempt)
        # A request may cover work items: each covered item waits for the
        # request's result through a receipt-pinned CI wait, and its holder
        # keeps the completion decision.
        covered = self._covered_waits(args.get("covered"), receipt)

        def retain_response(marker, response):
            not_dispatched = isinstance(response, dict)
            record = {key: value for key, value in marker.items() if key != "dispatched_at"}
            record.update(runs=[] if not_dispatched else response, requested_at=now())
            if not_dispatched:
                record.update(outcome="not_dispatched", reason_code=response["reason_code"])
                if "diagnostic" in response:
                    record["diagnostic"] = response["diagnostic"]
            requests.resolve(identity, record)
            self.record_ci_request(
                identity, marker["dispatched_at"][:10], marker["workflows"],
                [entry["id"] for entry in marker.get("covered", [])],
                state="not_dispatched" if not_dispatched else "resolved",
                attempt=marker.get("attempt", 1), reason_code=record.get("reason_code"),
                possibly_duplicate_spend=marker.get("possibly_duplicate_spend", False))
            evidence_id, evidence = self._new_invocation_evidence("vcs-request")
            write_json(self.run_dir / evidence,
                       {**record, "trust": "external-untrusted", "evidence_id": evidence_id})
            return record

        recorded = requests.outcome(identity)
        marker = requests.dispatched(identity)
        if recorded is None and marker is not None:
            # Re-observe this exact attempt; this operation cannot dispatch.
            response = _vcs.reconcile_runs_via(
                self.config.vcs, marker["sha"], marker["workflows"],
                request_id=identity, attempt=marker.get("attempt", 1),
                cancel=self.tools_cancelled)
            recorded = retain_response(marker, response)
        if recorded is not None:
            if recorded.get("outcome") == "not_dispatched":
                coverage = {"registered": [], "waiting": [], "moved": [], "refused": [],
                            "not_applied": [entry["id"] for entry in covered]}
            else:
                coverage = self._coverage_state(self._recorded_coverage(recorded), identity,
                                                register=True)
                recorded_ids = {entry["id"] for entry in recorded.get("covered", [])}
                coverage["not_applied"] = [entry["id"] for entry in covered
                                           if entry["id"] not in recorded_ids]
            return {**recorded, "replayed": True, "coverage": coverage}
        if attempt > 1:
            previous_id = ci_request_id(receipt["publication_id"], sha, workflows, attempt - 1)
            previous = requests.outcome(previous_id)
            if previous is None or previous.get("outcome") != "not_dispatched":
                raise Denied("A new CI attempt requires the preceding attempt's durable "
                             "no-dispatch outcome")
        # Retry permission above is independent of spend in the rest of a
        # legacy chain. Only a fully validated no-spend history clears risk.
        possibly_duplicate_spend = requests.prior_spend_possible(
            receipt["publication_id"], sha, workflows, attempt)
        # The covered items are checked before the provider is called, so a
        # coverage that cannot take the wait never spends a run.
        self._coverage_state(covered, identity, register=False)

        dispatched_at = now()
        covered_records = [entry["record"] for entry in covered]
        covered_ids = [entry["id"] for entry in covered]

        marker = {"request_id": identity, "publication_id": receipt["publication_id"],
                  "sha": sha, "workflows": workflows, "dispatched_at": dispatched_at,
                  "attempt": attempt, "covered": covered_records}
        if possibly_duplicate_spend:
            marker["possibly_duplicate_spend"] = True

        def before_invoke():
            # The dispatch marker is the point of no return: from here the run
            # may have spent a CI run, so the run's own record names the
            # request now, and no later failure can drop it.
            requests.mark_dispatch(identity, marker)
            self.record_ci_request(identity, dispatched_at[:10], workflows,
                                   covered_ids, state="unresolved", attempt=attempt,
                                   possibly_duplicate_spend=possibly_duplicate_spend)

        response = _vcs.request_runs_via(
            self.config.vcs, sha, workflows, request_id=identity, attempt=attempt,
            cancel=self.tools_cancelled, before_invoke=before_invoke)
        not_dispatched = isinstance(response, dict)
        record = retain_response(marker, response)
        if not_dispatched:
            coverage = {"registered": [], "waiting": [], "moved": [], "refused": [],
                        "not_applied": covered_ids}
        else:
            coverage = self._coverage_state(covered, identity, register=True)
        return {**record, "replayed": False, "coverage": coverage}

    def _covered_waits(self, raw, receipt):
        """Validate the covered work items and the CI waits they carry.

        Each covered item names the revision it is covered at and the checks
        whose result settles it. The wait is pinned to the published receipt,
        so it observes exactly that commit, and the holder of the item keeps
        the completion decision. Bounds: at most 16 items, at most 64 checks
        each, and at most 2048 UTF-8 bytes of covered records.
        """
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 16:
            raise Denied("A CI request covers at most 16 work items")
        if raw and "work_items_transition" not in set(self.role.capabilities):
            raise Denied("Covering work items requires the work-item capability")
        if raw and not (self.config.vcs.get("poll_enabled") and self.config.vcs.get("command")):
            raise Denied("Covering work items requires automatic VCS polling and an adapter")
        from .ci_waits import normalize as normalize_ci_wait
        waits = []
        seen = set()
        for entry in raw:
            item_id = entry["id"]
            if item_id in seen:
                raise Denied("Duplicate covered work item")
            seen.add(item_id)
            request_wait = {"kind": "ci", "publication_id": receipt["publication_id"],
                            "checks": entry["checks"]}
            wait = normalize_ci_wait(self.project.root, request_wait)
            waits.append({"id": item_id, "rev": entry["rev"], "wait": wait,
                          "input": request_wait,
                          "record": {"id": item_id, "rev": entry["rev"], **wait}})
        if len(canonical([entry["record"] for entry in waits])) > MAX_COVERED_BYTES:
            raise Denied("The covered work-item set exceeds 2048 UTF-8 bytes")
        return waits

    def _coverage_state(self, waits, identity, *, register):
        """Report the covered items' state and register the pending waits.

        A covered item that cannot take the CI wait is refused before the
        provider is called, so a bad coverage never spends a run. Once the
        run is spent the state is only reported: a holder that resumed an
        item keeps that decision, and the recorded outcome is still returned.
        """
        registered, waiting, moved, refused = [], [], [], []
        for entry in waits:
            item = self.project.work_items.get(entry["id"])
            if item["status"] == "blocked" and item.get("wait") == entry["wait"]:
                # Already registered: the item's revision moved with the wait.
                waiting.append(entry["id"])
                continue
            if item["status"] in ("done", "dropped") or item["status"] == "blocked":
                # A finished item takes no wait, and a blocked item carries
                # another wait that only its holder may replace.
                refused.append(entry["id"])
                if not register:
                    raise Denied(f"Covered work item {entry['id']} cannot take the CI wait")
                continue
            if item["rev"] != entry["rev"]:
                moved.append(entry["id"])
                if not register:
                    raise Denied(f"Covered work item {entry['id']} changed; "
                                 "re-read it before registering the wait")
                continue
            if register:
                self.project.work_items.transition(
                    entry["id"], expected_rev=entry["rev"], status="blocked",
                    actor=self.role.name, wait=entry["input"],
                    reason=f"Waits for the CI request {identity}")
                registered.append(entry["id"])
        return {"registered": registered, "waiting": waiting,
                "moved": moved, "refused": refused, "not_applied": []}

    def _recorded_coverage(self, recorded):
        """The waits a recorded request covered, rebuilt from its own record."""
        waits = []
        for entry in recorded.get("covered", []):
            wait = {key: entry[key]
                    for key in ("kind", "publication_id", "branch", "sha", "checks")}
            waits.append({"id": entry["id"], "rev": entry["rev"], "wait": wait,
                          "input": {"kind": "ci", "publication_id": wait["publication_id"],
                                    "checks": wait["checks"]}})
        return waits
