"""CI requests: one run per workflow at an exact published commit.

Offline only: the adapter is a local double, no provider, no publication.
"""
import dataclasses
import json
import sys
import uuid

from support import Fixture
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Denied
from mizu.fs import mkdir, read_json
from mizu.protocol import tool_definitions
from mizu.project import Project
from mizu.publication import Publication
from mizu.runtime import Context

SHA = "a" * 40
OTHER_SHA = "b" * 40


def adapter_settings(payload, **extra):
    code = ("import sys,json; req=json.load(sys.stdin); "
            "print(json.dumps(%s))" % json.dumps(payload))
    settings = {"command": [sys.executable, "-c", code], "timeout_seconds": 5,
                "max_bytes": 524288, "request_grant": True}
    settings.update(extra)
    return settings


def runs(head_sha=SHA, workflow="ci", run_id="run-1"):
    return {"runs": [{"workflow": workflow, "run_id": run_id,
                      "head_sha": head_sha, "url": "https://ci.invalid/1"}]}


class CiRequestTests(Fixture):
    def setUp(self):
        super().setUp()
        self.publication = Publication(self.project.root, "e" * 40, "c" * 64, "published", "push")
        intent, _ = self.publication.prepare("approval", "run")
        self.publication.mark_dispatch()
        self.receipt = self.publication.complete(intent, SHA)

    def context(self, payload, **extra):
        config = dataclasses.replace(self.config, vcs=adapter_settings(payload, **extra))
        drop = extra.pop("drop", ())
        add = extra.pop("add", ())
        capabilities = [name for name in config.roles["worker"].capabilities
                         if name not in drop] + ["vcs_request"] + list(add)
        role = dataclasses.replace(config.roles["worker"], capabilities=tuple(capabilities))
        config = dataclasses.replace(config, roles={**config.roles, "worker": role})
        # The work-item store carries the same VCS settings as the runtime, so
        # a CI wait is registrable exactly when the runtime says it is.
        self.project = Project(config, "sample")
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        return Context(config, self.project, role, run, self.project.snapshots.get(),
                       self.project.workspace)

    def request(self, ctx, workflows=("ci",)):
        return ctx.handle("vcs_request", {"publication_id": self.receipt["publication_id"],
                                           "workflows": list(workflows)})

    def test_request_runs_at_the_published_commit_and_records_it(self):
        ctx = self.context(runs())
        out = self.request(ctx)
        self.assertEqual(out["sha"], SHA)
        self.assertEqual(out["publication_id"], self.receipt["publication_id"])
        self.assertEqual(out["runs"][0]["run_id"], "run-1")
        self.assertFalse(out["replayed"])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        stored = CiRequests(self.project.root).outcome(identity)
        self.assertEqual(stored["runs"][0]["head_sha"], SHA)
        self.assertIsNotNone(CiRequests(self.project.root).dispatched(identity))

    def test_a_run_at_another_commit_is_refused_and_never_recorded(self):
        ctx = self.context(runs(head_sha=OTHER_SHA))
        with self.assertRaises(Denied):
            self.request(ctx)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))
        # The dispatch marker survives: the outcome is uncertain, so a second
        # request is refused instead of spending a second run.
        self.assertIsNotNone(CiRequests(self.project.root).dispatched(identity))

    def test_a_recorded_request_replays_instead_of_spending_a_second_run(self):
        ctx = self.context(runs())
        first = self.request(ctx)
        second = self.request(ctx)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["runs"], first["runs"])

    def test_an_unresolved_dispatch_is_never_retried_blindly(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root).mark_dispatch(
            identity, {"request_id": identity, "sha": SHA, "workflows": ["ci"]})
        ctx = self.context(runs())
        with self.assertRaises(Denied):
            self.request(ctx)
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))

    def test_the_provider_grant_is_required(self):
        ctx = self.context(runs(), request_grant=False)
        with self.assertRaises(Denied):
            self.request(ctx)

    def test_an_unpublished_target_is_refused(self):
        ctx = self.context(runs())
        with self.assertRaises(Denied):
            ctx.handle("vcs_request", {"publication_id": "0" * 64, "workflows": ["ci"]})

    def test_a_response_that_omits_a_workflow_is_refused(self):
        ctx = self.context(runs(workflow="other"))
        with self.assertRaises(Denied):
            self.request(ctx)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))

    def test_a_reordered_request_replays_the_recorded_outcome(self):
        ctx = self.context(runs())
        first = self.request(ctx, workflows=("ci",))
        ctx = self.context(runs())
        again = self.request(ctx, workflows=("ci",))
        self.assertTrue(again["replayed"])
        self.assertEqual(again["request_id"], first["request_id"])
        reordered = self.request(ctx, workflows=("ci",))
        self.assertTrue(reordered["replayed"])

    def test_the_capability_is_off_by_default(self):
        self.assertNotIn("vcs_request", self.config.roles["worker"].capabilities)
        self.assertNotIn("mizu_vcs_request",
                         [tool["name"] for tool in tool_definitions(self.config.roles["worker"].capabilities)])
        granted = dataclasses.replace(
            self.config.roles["worker"],
            capabilities=tuple(list(self.config.roles["worker"].capabilities) + ["vcs_request"]))
        self.assertIn("mizu_vcs_request",
                      [tool["name"] for tool in tool_definitions(granted.capabilities)])

    def covered_item(self, key="covered", checks=("unit", "lint")):
        seed = self.project.work_items.create(actor="worker", idempotency_key=key,
                                              title="Covered item")["record"]["work_item"]
        return {"id": seed["id"], "rev": seed["rev"], "checks": list(checks)}

    def request_covered(self, ctx, covered):
        return ctx.handle("vcs_request", {"publication_id": self.receipt["publication_id"],
                                          "workflows": ["ci"], "covered": covered})

    def test_a_covered_item_waits_for_the_requested_checks(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        out = self.request_covered(ctx, [covered])
        self.assertEqual(out["covered"][0]["id"], covered["id"])
        self.assertEqual(out["covered"][0]["checks"], ["lint", "unit"])
        item = self.project.work_items.get(covered["id"])
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(item["wait"]["kind"], "ci")
        self.assertEqual(item["wait"]["publication_id"], self.receipt["publication_id"])
        self.assertEqual(item["wait"]["checks"], ["lint", "unit"])
        self.assertEqual(item["wait"]["sha"], SHA)
        # The holder keeps the completion decision.
        self.assertEqual(item["holder"], "worker")

    def test_an_unknown_covered_item_is_refused_before_a_run_is_spent(self):
        ctx = self.context(runs(), poll_enabled=True)
        with self.assertRaises(Denied):
            self.request_covered(ctx, [{"id": "wi_" + "0" * 32, "rev": 1, "checks": ["unit"]}])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(identity))
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))

    def test_a_stale_covered_revision_is_refused_before_a_run_is_spent(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        covered["rev"] = covered["rev"] + 1
        with self.assertRaises(Denied):
            self.request_covered(ctx, [covered])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(identity))

    def test_covering_work_items_requires_the_work_item_capability(self):
        ctx = self.context(runs(), poll_enabled=True, drop=("work_items_transition",))
        covered = self.covered_item()
        with self.assertRaises(Denied):
            self.request_covered(ctx, [covered])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(identity))

    def test_covering_work_items_requires_automatic_ci_polling(self):
        ctx = self.context(runs())
        covered = self.covered_item()
        with self.assertRaises(Denied):
            self.request_covered(ctx, [covered])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(identity))

    def test_a_replay_completes_the_coverage_without_a_second_run(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        self.request_covered(ctx, [covered])
        ctx = self.context(runs(), poll_enabled=True)
        again = self.request_covered(ctx, [covered])
        self.assertTrue(again["replayed"])
        item = self.project.work_items.get(covered["id"])
        self.assertEqual(item["status"], "blocked")
        # One registration only: the replay does not wait a second time.
        self.assertEqual(item["rev"], 2)

    def test_a_replay_that_covers_other_items_reports_them_as_not_applied(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        first = self.request_covered(ctx, [covered])
        other = self.covered_item("other")
        ctx = self.context(runs(), poll_enabled=True)
        again = self.request_covered(ctx, [other])
        # The recorded run is returned; the requested coverage is reported,
        # never silently applied to a run the record does not name.
        self.assertTrue(again["replayed"])
        self.assertEqual(again["runs"], first["runs"])
        self.assertEqual(again["coverage"]["not_applied"], [other["id"]])
        self.assertEqual(self.project.work_items.get(other["id"])["status"], "open")

    def test_a_covered_item_that_cannot_take_the_wait_is_refused_before_a_run_is_spent(self):
        dropped = self.covered_item("dropped")
        self.project.work_items.transition(dropped["id"], expected_rev=dropped["rev"],
                                           status="dropped", actor="worker", reason="not needed")
        blocked = self.covered_item("blocked")
        self.project.work_items.transition(blocked["id"], expected_rev=blocked["rev"],
                                           status="blocked", actor="worker",
                                           wait={"kind": "code_change"})
        ctx = self.context(runs(), poll_enabled=True)
        for entry in (dropped, blocked):
            with self.assertRaises(Denied):
                self.request_covered(ctx, [entry])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(identity))
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))

    def test_a_replay_returns_the_recorded_outcome_after_the_holder_resumes_the_item(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        first = self.request_covered(ctx, [covered])
        item = self.project.work_items.get(covered["id"])
        # The holder takes the item back: its decision outranks the coverage.
        self.project.work_items.transition(covered["id"], expected_rev=item["rev"],
                                           status="doing", actor="worker")
        ctx = self.context(runs(), poll_enabled=True)
        again = self.request_covered(ctx, [covered])
        self.assertTrue(again["replayed"])
        self.assertEqual(again["runs"], first["runs"])
        self.assertEqual(again["coverage"]["moved"], [covered["id"]])
        self.assertEqual(self.project.work_items.get(covered["id"])["status"], "doing")

    def test_the_daily_listing_groups_the_recorded_requests(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        self.request_covered(ctx, [covered])
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 1)
        self.assertFalse(listing["truncated"])
        self.assertFalse(listing["scan_stopped"])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        record = CiRequests(self.project.root).outcome(identity)
        day = listing["days"][0]
        self.assertEqual(day["date"], record["requested_at"][:10])
        self.assertEqual(day["requests"], 1)
        self.assertEqual(day["covered_items"], 1)
        item = day["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["sha"], SHA)
        self.assertEqual(item["workflows"], ["ci"])
        self.assertEqual(item["runs"][0]["run_id"], "run-1")
        self.assertEqual(item["covered"], [{"id": covered["id"], "rev": covered["rev"]}])

    def test_the_daily_listing_skips_an_invalid_record(self):
        ctx = self.context(runs(), poll_enabled=True)
        self.request_covered(ctx, [])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        path = CiRequests(self.project.root).root / f"{identity}.request.json"
        path.write_text("{\"request_id\": \"other\"}", encoding="utf-8")
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 0)
        self.assertEqual(listing["days"], [])

    def test_the_daily_listing_op_reads_local_evidence_only(self):
        ctx = self.context(runs(), poll_enabled=True, add=("vcs_read",))
        self.request_covered(ctx, [])
        out = ctx.handle("vcs_read", {"op": "ci_requests"})
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["days"][0]["requests"], 1)
        with self.assertRaises(Denied):
            ctx.handle("vcs_read", {"op": "ci_requests", "branch": "mizu"})
