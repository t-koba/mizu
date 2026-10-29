"""Bounded synthetic cost observations; no external adapter or CI reruns."""
import dataclasses
import unittest
from unittest.mock import Mock, patch

from support import Fixture
from mizu import vcs
from mizu.ci_costs import observed_costs
from mizu.ci_requests import CiRequests, request_id
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import read_json
from mizu.publication import Publication
from mizu.runtime import Context

SHA = "a" * 40
PUB = "b" * 64
DAY = "2026-07-20"


def check(**changes):
    return {"check": "core", "state": "success", "sha": SHA, "run": "22",
            "repository": "owner/project", "workflow_run_id": "11", "job_id": "22",
            "run_attempt": 1, "started_at": "2026-07-21T00:00:00+00:00",
            "completed_at": "2026-07-21T00:01:00+00:00", **changes}


def request(**changes):
    return {"date": DAY, "request_id": "c" * 64, "publication_id": PUB, "sha": SHA,
            "workflows": ["ci"], "runs": [{"workflow": "ci", "run_id": "11", "head_sha": SHA}],
            "covered": [], "state": "resolved", **changes}


def page(*rows):
    return {"items": list(rows), "offset": 0, "limit": 8, "total": len(rows),
            "truncated": False, "scan_stopped": False, "next_offset": None}


def status(*rows, publication=PUB, sha=SHA):
    return {"evidence_id": "observation", "evidence": "vcs-read/observation.json",
            "result": {"checks": vcs.parse_status_checks({"checks": list(rows)}),
                       "publication": {"publication_id": publication, "published_sha": sha}}}


class IdentityTests(unittest.TestCase):
    def test_typed_bundle_and_legacy_stay_distinct(self):
        row = vcs.parse_status_checks({"checks": [check()]})[0]
        self.assertEqual(row["job_seconds"], 60)
        self.assertEqual(row["job_id"], row["run"])
        legacy = {"check": "core", "state": "success", "sha": SHA, "run": "opaque"}
        self.assertNotIn("repository", vcs.parse_status_checks({"checks": [legacy]})[0])
        del legacy["run"]
        self.assertNotIn("workflow_run_id", vcs.parse_status_checks({"checks": [legacy]})[0])

    def test_every_partial_or_malformed_bundle_denies_whole_response(self):
        bad = []
        for key in ("workflow_run_id", "job_id", "run_attempt", "repository", "run"):
            row = check()
            del row[key]
            bad.append(row)
        for key, values in {
            "workflow_run_id": [1, "", "run-1", "１", "1" * 129, None],
            "job_id": [22, "23", "22\n", "1" * 129],
            "run_attempt": [True, 0, -1, 1.0, "1", 1073741825, None],
            "repository": ["owner", "/project", "owner/", "owner/project/other",
                           "owner/..", "owner/a b", "owner/a\n", "o/" + "r" * 255, None],
        }.items():
            bad.extend(check(**{key: value}) for value in values)
        for row in bad:
            with self.subTest(row=row), self.assertRaises(Denied):
                vcs.parse_status_checks({"checks": [check(), row]})


class ProjectionTests(unittest.TestCase):
    def test_exact_join_dedups_polls_but_counts_attempts_and_jobs(self):
        read = Mock(return_value=status(check(), check(), check(run_attempt=2),
                                        check(run="23", job_id="23")))
        out = observed_costs(page(request(), request(request_id="d" * 64)), read)
        read.assert_called_once_with(PUB)
        self.assertEqual(out["observed_wall_seconds"], 180)
        self.assertEqual(out["days"][0]["timed_executions"], 3)
        self.assertEqual(out["days"][0]["date"], DAY)  # completion is next day
        self.assertEqual(out["coverage"], "partial")
        self.assertEqual(out["days"][0]["requests"][0]["status_evidence"]["evidence_id"], "observation")
        # Re-reading observations is not a persistent accumulator.
        self.assertEqual(observed_costs(page(request()), read)["observed_wall_seconds"], 180)

    def test_distinct_receipt_observations_share_only_exact_execution_tuples(self):
        other = "f" * 64
        observations = {PUB: status(check()),
                        other: status(check(), check(run_attempt=2), publication=other)}
        read = Mock(side_effect=observations.__getitem__)
        out = observed_costs(page(request(), request(publication_id=other)), read)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(out["observed_wall_seconds"], 120)
        self.assertEqual(out["days"][0]["timed_executions"], 2)

    def test_sha_workflow_and_publication_must_all_match(self):
        for row in (check(sha="d" * 40), check(workflow_run_id="99")):
            out = observed_costs(page(request()), lambda _: status(row))
            self.assertIsNone(out["observed_wall_seconds"])
            self.assertEqual(out["days"][0]["requests"][0]["unmatched_rows"], 1)
        for observation in (status(check(), sha="e" * 40), status(check(), publication="f" * 64)):
            out = observed_costs(page(request()), lambda _: observation)
            self.assertIsNone(out["observed_wall_seconds"])
            self.assertEqual(out["days"][0]["requests"][0]["status"], "publication_mismatch")

    def test_conflicting_repository_namespace_cannot_join(self):
        out = observed_costs(page(request()), lambda _: status(check(), check(repository="other/project")))
        self.assertTrue(out["repository_conflict"])
        self.assertIsNone(out["observed_wall_seconds"])
        self.assertEqual(out["days"][0]["requests"][0]["unmatched_rows"], 2)

    def test_legacy_partial_missing_running_and_unresolved_are_visible(self):
        legacy = {"check": "legacy", "state": "success", "sha": SHA, "run": "11"}
        running = check(run="23", job_id="23", state="in_progress")
        del running["completed_at"]
        no_times = check(run="24", job_id="24")
        del no_times["completed_at"], no_times["started_at"]
        out = observed_costs(page(request(), request(request_id="d" * 64, runs=[], state="unresolved")),
                             lambda _: status(legacy, running, no_times, check()))
        self.assertEqual(out["observed_wall_seconds"], 60)
        first, unresolved = out["days"][0]["requests"]
        self.assertEqual((first["identityless_rows"], first["running_rows"], first["missing_duration_rows"]),
                         (1, 1, 2))
        self.assertEqual(unresolved["state"], "unresolved")
        self.assertEqual(unresolved["matched_rows"], 0)
        self.assertEqual(unresolved["unmatched_rows"], 3)

    def test_absent_coverage_is_null_not_zero(self):
        for observation in (status(), status({"check": "legacy", "state": "success", "sha": SHA})):
            out = observed_costs(page(request()), lambda _: observation)
            self.assertIsNone(out["observed_wall_seconds"])
        read = Mock(side_effect=Denied("unavailable"))
        out = observed_costs(page(request(), request(request_id="d" * 64)), read)
        read.assert_called_once_with(PUB)
        self.assertIsNone(out["observed_wall_seconds"])
        self.assertEqual(out["days"][0]["requests"][0]["status"], "unavailable")
        self.assertIsNone(observed_costs(page(), read)["observed_wall_seconds"])
        zero = check(completed_at=check()["started_at"])
        self.assertEqual(observed_costs(page(request()), lambda _: status(zero))["observed_wall_seconds"], 0)

    def test_running_and_missing_duration_states_do_not_become_cost(self):
        terminal_without_end = check(state="success")
        del terminal_without_end["completed_at"]
        running_with_interval = check(state="in_progress", run="23", job_id="23")
        out = observed_costs(page(request()),
                             lambda _: status(terminal_without_end, running_with_interval))
        report = out["days"][0]["requests"][0]
        self.assertIsNone(out["observed_wall_seconds"])
        self.assertEqual(report["running_rows"], 1)
        self.assertEqual(report["missing_duration_rows"], 1)
        self.assertEqual(report["state_timing_conflicts"], 1)

    def test_conflicting_duration_or_day_is_not_arbitrarily_selected(self):
        for requests, rows in (([request()], [check(), check(completed_at="2026-07-21T00:02:00Z")]),
                               ([request(), request(date="2026-07-21")], [check()])):
            out = observed_costs(page(*requests), lambda _: status(*rows))
            self.assertIsNone(out["observed_wall_seconds"])
            self.assertGreater(out["conflicting_execution_day_observations"], 0)


class CostReadTests(Fixture):
    def setUp(self):
        super().setUp()
        publication = Publication(self.project.root, "e" * 40, "c" * 64, "published", "push",
                                  projection=public_source_identity({}))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        self.receipt = publication.complete(intent, SHA)
        self.store = CiRequests(self.project.root)
        role = dataclasses.replace(self.config.roles["worker"], capabilities=("vcs_read",))
        run = self.project.root / "runs" / "cost-observation"
        run.mkdir(parents=True)
        self.ctx = Context(self.config, self.project, role, run,
                           self.project.snapshots.get(), self.project.workspace)

    def dispatch(self, workflows=("ci",), publication=None, resolved=True):
        pub = publication or self.receipt["publication_id"]
        identity = request_id(pub, SHA, list(workflows))
        base = {"request_id": identity, "publication_id": pub, "sha": SHA, "workflows": list(workflows)}
        self.store.mark_dispatch(identity, {**base, "dispatched_at": "2026-07-21T08:59:00+09:00"})
        if resolved:
            self.store.record_outcome(identity, {**base, "requested_at": "2026-07-21T00:05:00Z",
                                                "runs": [{"workflow": w, "run_id": "11", "head_sha": SHA}
                                                         for w in workflows]})
        return identity

    def test_real_receipt_path_status_evidence_and_dispatch_utc_day(self):
        self.dispatch()
        self.dispatch(("ci", "lint"))
        with patch("mizu.vcs.invoke", return_value={"checks": [check()]}) as provider:
            out = self.ctx.handle("vcs_read", {"op": "ci_costs"})
        self.assertEqual(provider.call_count, 1)
        params = provider.call_args.args[1]
        self.assertEqual((params["branch"], params["sha"]), ("published", SHA))
        self.assertEqual(out["observed_wall_seconds"], 60)
        self.assertEqual(out["days"][0]["date"], DAY)
        self.assertEqual(out["total"], 2)
        evidence = out["days"][0]["requests"][0]["status_evidence"]["evidence"]
        retained = read_json(self.ctx.run_dir / evidence)
        self.assertEqual(retained["result"]["publication"]["publication_id"], self.receipt["publication_id"])
        self.assertEqual(read_json(self.ctx.run_dir / out["evidence"])["result"], out["result"])

    def test_no_provider_calls_from_count_listing_or_invalid_receipt(self):
        self.dispatch(publication=PUB, resolved=False)
        with patch("mizu.vcs.invoke", side_effect=AssertionError("no provider")):
            counts = self.ctx.handle("vcs_read", {"op": "ci_requests"})
            out = self.ctx.handle("vcs_read", {"op": "ci_costs"})
        self.assertEqual(counts["total"], 1)
        self.assertIsNone(out["observed_wall_seconds"])
        self.assertEqual(out["days"][0]["requests"][0]["state"], "unresolved")

    def test_malformed_status_is_unavailable_without_erasing_dispatch(self):
        self.dispatch()
        with patch("mizu.vcs.invoke", return_value={"checks": [check(run_attempt=0)]}):
            out = self.ctx.handle("vcs_read", {"op": "ci_costs"})
        self.assertEqual(out["total"], 1)
        self.assertIsNone(out["observed_wall_seconds"])
        self.assertEqual(out["days"][0]["requests"][0]["status"], "unavailable")

    def test_pagination_and_bounded_status_calls(self):
        for i in range(10):
            self.dispatch((f"workflow-{i}",))
        with patch("mizu.vcs.invoke", return_value={"checks": [check()]}) as provider:
            first = self.ctx.handle("vcs_read", {"op": "ci_costs"})
            second = self.ctx.handle("vcs_read", {"op": "ci_costs", "limit": 2, "offset": 8})
        self.assertEqual(provider.call_count, 2)  # one receipt per page
        self.assertEqual(len(first["days"][0]["requests"]), 8)
        self.assertEqual(len(second["days"][0]["requests"]), 2)
        self.assertEqual(first["next_offset"], 8)
        self.assertIsNone(second["next_offset"])
        self.assertTrue(first["truncated"] and second["truncated"])
        first_ids = {r["request_id"] for r in first["days"][0]["requests"]}
        self.assertFalse(first_ids & {r["request_id"] for r in second["days"][0]["requests"]})
        with patch("mizu.ci_requests.REQUEST_RECORD_LIMIT", 3):
            out = self.store.page()
        self.assertTrue(out["scan_stopped"])
        self.assertEqual(out["total"], 3)
        with patch("mizu.ci_requests.REQUEST_SCAN_LIMIT", 2):
            self.assertTrue(self.store.page()["scan_stopped"])

    def test_eight_distinct_receipts_never_exceed_eight_status_calls(self):
        for i in range(9):
            self.dispatch(publication=f"{i:064x}")
        with patch.object(self.ctx, "_op_vcs_read", wraps=self.ctx._op_vcs_read) as read:
            self.ctx.handle("vcs_read", {"op": "ci_costs"})
        self.assertEqual(sum(call.args[0]["op"] == "status" for call in read.call_args_list), 8)

    def test_bounds_and_op_specific_arguments_fail_closed(self):
        with patch("mizu.vcs.invoke", side_effect=AssertionError("no provider")):
            for args in ({"limit": 0}, {"limit": 9}, {"limit": True}, {"offset": -1},
                         {"offset": 1000}, {"offset": 1.0}, {"branch": "published"}):
                with self.subTest(args=args), self.assertRaises(Denied):
                    self.ctx.handle("vcs_read", {"op": "ci_costs", **args})
            for op in ("ci_requests", "status", "publications"):
                with self.assertRaises(Denied):
                    self.ctx.handle("vcs_read", {"op": op, "limit": 1})
        self.assertEqual(self.store.page(offset=999)["items"], [])
