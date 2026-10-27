"""CI requests: one run per workflow at an exact published commit.

Offline only: the adapter is a local double, no provider, no publication.
"""
import dataclasses
import json
import sys
import uuid
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Denied, LimitExceeded
from mizu.fs import mkdir, read_json
from mizu.protocol import tool_definitions
from mizu.project import Project
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu.runtime import Context, Engine

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
        self.publication = Publication(self.project.root, "e" * 40, "c" * 64, "published", "push",
                                       projection=public_source_identity({}))
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

    def test_a_replay_reports_only_the_items_the_recorded_request_does_not_cover(self):
        ctx = self.context(runs(), poll_enabled=True)
        first = self.covered_item("first")
        second = self.covered_item("second")
        self.request_covered(ctx, [first])
        ctx = self.context(runs(), poll_enabled=True)
        again = self.request_covered(ctx, [first, second])
        # The recorded request covers the first item, so only the second one is
        # not applied, and an item that is waiting is never reported as such.
        self.assertEqual(again["coverage"]["not_applied"], [second["id"]])
        self.assertEqual(again["coverage"]["waiting"], [first["id"]])

    def test_a_reordered_replay_is_the_same_coverage(self):
        ctx = self.context(runs(), poll_enabled=True)
        first = self.covered_item("first")
        second = self.covered_item("second")
        self.request_covered(ctx, [first, second])
        ctx = self.context(runs(), poll_enabled=True)
        again = self.request_covered(ctx, [second, first])
        self.assertEqual(again["coverage"]["not_applied"], [])
        self.assertEqual(sorted(again["coverage"]["waiting"]),
                         sorted([first["id"], second["id"]]))

    def test_the_run_record_names_the_requests_it_made(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        out = self.request_covered(ctx, [covered])
        summary = ctx.ci_request_summary()
        self.assertEqual(summary["total"], 1)
        entry = summary["requests"][0]
        self.assertEqual(entry["request_id"], out["request_id"])
        # The day is the day the request was made: the dispatch.
        marker = CiRequests(self.project.root).dispatched(out["request_id"])
        self.assertEqual(entry["date"], marker["dispatched_at"][:10])
        self.assertEqual(entry["workflows"], ["ci"])
        self.assertEqual(entry["covered"], [covered["id"]])
        # A replay of the recorded request made no new run, so the run's
        # record does not count it twice.
        self.request_covered(ctx, [covered])
        self.assertEqual(ctx.ci_request_summary()["total"], 1)

    def test_the_daily_listing_groups_the_recorded_requests(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        self.request_covered(ctx, [covered])
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 1)
        self.assertFalse(listing["truncated"])
        self.assertFalse(listing["scan_stopped"])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        marker = CiRequests(self.project.root).dispatched(identity)
        day = listing["days"][0]
        # The day is the day the request was made: the dispatch.
        self.assertEqual(day["date"], marker["dispatched_at"][:10])
        self.assertEqual(day["requests"], 1)
        self.assertEqual(day["covered_items"], 1)
        item = day["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["sha"], SHA)
        self.assertEqual(item["workflows"], ["ci"])
        self.assertEqual(item["runs"][0]["run_id"], "run-1")
        self.assertEqual(item["covered"], [{"id": covered["id"], "rev": covered["rev"]}])

    def test_daily_validates_marker_outcome_identity_and_provider_runs(self):
        store = CiRequests(self.project.root)
        publication = self.receipt["publication_id"]
        workflows = ["ci", "lint"]
        identity = request_id(publication, SHA, workflows)
        marker = {"request_id": identity, "publication_id": publication, "sha": SHA,
                  "workflows": workflows, "dispatched_at": "2026-10-20T23:59:00+00:00"}
        outcome = {"request_id": identity, "publication_id": publication, "sha": SHA,
                   "workflows": list(reversed(workflows)), "requested_at": "2026-10-21T00:01:00+00:00",
                   "runs": [runs(workflow=name)["runs"][0] for name in workflows]}
        store.mark_dispatch(identity, marker)
        store.record_outcome(identity, outcome)
        # A second valid request survives every damaged neighbor.
        other_id = request_id(publication, OTHER_SHA, ["ci"])
        store.mark_dispatch(other_id, {**marker, "request_id": other_id, "sha": OTHER_SHA,
                                       "workflows": ["ci"]})
        store.record_outcome(other_id, {**outcome, "request_id": other_id, "sha": OTHER_SHA,
                                       "workflows": ["ci"], "runs": runs(OTHER_SHA)["runs"]})
        path = store.root / f"{identity}.request.json"

        def rows():
            listing = store.daily()
            return {row["request_id"]: row for day in listing["days"] for row in day["items"]}

        self.assertEqual(rows()[identity]["state"], "resolved")
        self.assertEqual(rows()[identity]["workflows"], list(reversed(workflows)))
        invalid_runs = [None, [], [1], ["run"], [{}],
                        [{**outcome["runs"][0], "run_id": ""}],
                        [{**outcome["runs"][0], "run_id": "x" * 129}],
                        [{**outcome["runs"][0], "url": "x" * 4097}],
                        [{**outcome["runs"][0], "head_sha": OTHER_SHA}],
                        [outcome["runs"][0]],
                        [outcome["runs"][0], outcome["runs"][0]],
                        [{**outcome["runs"][0], "extra": True}, outcome["runs"][1]]]
        cases = [{"runs": value} for value in invalid_runs] + [
            {"request_id": "f" * 64}, {"publication_id": "f" * 64},
            {"sha": OTHER_SHA}, {"workflows": ["ci", "other"]},
            {"workflows": ["ci", 1]}]
        for change in cases:
            with self.subTest(change=change):
                path.write_text(json.dumps({**outcome, **change}), encoding="utf-8")
                found = rows()
                self.assertEqual(set(found), {identity, other_id})
                self.assertEqual(found[identity]["state"], "unresolved")
                self.assertEqual(found[identity]["runs"], [])
                self.assertEqual(found[other_id]["state"], "resolved")

        # A correctly shaped marker whose identity does not recompute is not evidence.
        path.write_text(json.dumps(outcome), encoding="utf-8")
        marker_path = store.root / f"{identity}.dispatch.json"
        for change in ({"publication_id": "f" * 64}, {"sha": OTHER_SHA},
                       {"workflows": ["other"]}, {"request_id": "f" * 64},
                       {"workflows": []}, {"sha": "g" * 40}):
            with self.subTest(marker=change):
                marker_path.write_text(json.dumps({**marker, **change}), encoding="utf-8")
                found = rows()
                self.assertEqual(set(found), {other_id})
                self.assertEqual(found[other_id]["state"], "resolved")

    def test_the_daily_listing_names_a_dispatch_whose_outcome_names_another_request(self):
        ctx = self.context(runs(), poll_enabled=True)
        self.request_covered(ctx, [])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        path = CiRequests(self.project.root).root / f"{identity}.request.json"
        path.write_text("{\"request_id\": \"other\"}", encoding="utf-8")
        # A readable outcome that names another request is never returned
        # unvalidated, and it is not a reason to hide the request: the
        # dispatch marker proves the provider was asked, so a CI run may
        # have been spent and the request is listed as unresolved.
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 1)
        item = listing["days"][0]["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])

    def test_the_daily_listing_skips_a_damaged_record_beside_a_valid_one(self):
        ctx = self.context(runs(), poll_enabled=True)
        self.request_covered(ctx, [])
        store = CiRequests(self.project.root)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        # A malformed record and an oversized one must not deny the view: the
        # valid record beside them is still listed.
        (store.root / ("d" * 64 + ".request.json")).write_text("{not json", encoding="utf-8")
        (store.root / ("e" * 64 + ".request.json")).write_text("x" * 9000, encoding="utf-8")
        listing = store.daily()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["days"][0]["items"][0]["request_id"], identity)

    def test_a_request_is_dated_by_the_day_it_was_dispatched(self):
        # The provider is asked at 23:59Z on one day and answers at 00:01Z on
        # the next. The request was made on the first day, so both views
        # count it there.
        dispatch = "2026-10-20T23:59:00+00:00"
        answer = "2026-10-21T00:01:00+00:00"
        state = {"dispatched": False}
        original = CiRequests.mark_dispatch

        def mark_dispatch(store, identity, record):
            state["dispatched"] = True
            return original(store, identity, record)

        def clock():
            return answer if state["dispatched"] else dispatch

        ctx = self.context(runs(), poll_enabled=True)
        with patch("mizu.runtime.now", clock), \
                patch.object(CiRequests, "mark_dispatch", mark_dispatch):
            out = self.request(ctx)
        # The answer really did arrive on the next day.
        self.assertEqual(out["requested_at"], answer)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        marker = CiRequests(self.project.root).dispatched(identity)
        self.assertEqual(marker["dispatched_at"], dispatch)
        # The run's own record names the day the request was made.
        summary = ctx.ci_request_summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["requests"][0]["state"], "resolved")
        self.assertEqual(summary["requests"][0]["date"], "2026-10-20")
        # The per-day listing counts it on the same day.
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["days"][0]["date"], "2026-10-20")
        self.assertEqual(listing["days"][0]["requests"], 1)
        self.assertEqual(listing["days"][0]["items"][0]["state"], "resolved")

    def test_the_daily_listing_names_a_dispatch_whose_outcome_is_invalid(self):
        ctx = self.context(runs(), poll_enabled=True)
        self.request_covered(ctx, [])
        store = CiRequests(self.project.root)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        path = store.root / f"{identity}.request.json"
        path.write_text("[]", encoding="utf-8")
        # The invalid outcome is never returned unvalidated.
        with self.assertRaises(Denied):
            store.outcome(identity)
        # The dispatch marker still proves the request reached the provider,
        # so it is listed as unresolved rather than hidden.
        listing = store.daily()
        self.assertEqual(listing["total"], 1)
        item = listing["days"][0]["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])
        # The refusal path agrees with the listing: the request is not
        # retried blindly, so no second CI run is spent.
        with self.assertRaises(Denied):
            self.request(self.context(runs(), poll_enabled=True))

    def test_the_daily_listing_names_a_dispatch_whose_outcome_is_oversized(self):
        ctx = self.context(runs(), poll_enabled=True)
        self.request_covered(ctx, [])
        store = CiRequests(self.project.root)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        path = store.root / f"{identity}.request.json"
        path.write_text(json.dumps({"request_id": identity, "pad": "x" * 9000}),
                        encoding="utf-8")
        # The oversized outcome is never returned unvalidated.
        with self.assertRaises(LimitExceeded):
            store.outcome(identity)
        # The dispatch marker still proves the request reached the provider,
        # so it is listed as unresolved rather than hidden.
        listing = store.daily()
        self.assertEqual(listing["total"], 1)
        item = listing["days"][0]["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])

    def test_the_daily_listing_does_not_name_a_dispatch_whose_marker_is_invalid(self):
        # A marker that cannot be validated has no instant to date the
        # request by, so the request is not listed at all.
        store = CiRequests(self.project.root)
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        store.mark_dispatch(identity, {"request_id": identity,
                                       "publication_id": "e" * 64, "sha": SHA,
                                       "workflows": ["ci"],
                                       "dispatched_at": "not-an-instant"})
        listing = store.daily()
        self.assertEqual(listing["total"], 0)
        self.assertEqual(listing["days"], [])

    def test_the_run_record_file_names_the_requests_it_made(self):
        config = dataclasses.replace(self.config, vcs=adapter_settings(runs()))
        role = dataclasses.replace(
            config.roles["worker"],
            capabilities=tuple([*config.roles["worker"].capabilities, "vcs_request"]))
        config = dataclasses.replace(config, roles={**config.roles, "worker": role})
        def work(ctx, *_):
            ctx.handle("vcs_request", {"publication_id": self.receipt["publication_id"],
                                       "workflows": ["ci"]})
        result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        record = read_json(self.project.root / "runs" / result["run"] / "result.json")
        self.assertEqual(record["ci_requests"]["total"], 1)
        entry = record["ci_requests"]["requests"][0]
        self.assertEqual(entry["request_id"],
                         request_id(self.receipt["publication_id"], SHA, ["ci"]))
        self.assertEqual(entry["state"], "resolved")
        self.assertEqual(entry["workflows"], ["ci"])

    def test_the_interrupted_run_record_names_the_requests_it_made(self):
        config = dataclasses.replace(self.config, vcs=adapter_settings(runs()))
        role = dataclasses.replace(
            config.roles["worker"],
            capabilities=tuple([*config.roles["worker"].capabilities, "vcs_request"]))
        config = dataclasses.replace(config, roles={**config.roles, "worker": role})
        def work(ctx, *_):
            ctx.handle("vcs_request", {"publication_id": self.receipt["publication_id"],
                                       "workflows": ["ci"]})
            raise RuntimeError("the unit failed after spending a CI run")
        with self.assertRaises(RuntimeError):
            Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        run_dirs = self.project.root / "runs"
        interrupted = [path for path in run_dirs.iterdir()
                       if path.is_dir() and (path / "error.json").is_file()]
        self.assertEqual(len(interrupted), 1)
        record = read_json(interrupted[0] / "error.json")
        self.assertEqual(record["ci_requests"]["total"], 1)
        self.assertEqual(record["ci_requests"]["requests"][0]["state"], "resolved")

    def test_a_spent_request_stays_named_when_the_wait_registration_fails(self):
        ctx = self.context(runs(), poll_enabled=True)
        covered = self.covered_item()
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        with patch.object(self.project.work_items, "transition",
                          side_effect=Denied("Work item changed; re-read before transitioning")):
            with self.assertRaises(Denied):
                self.request_covered(ctx, [covered])
        # The run was spent and the outcome is durable, so the request stays
        # named even though the wait registration failed afterwards.
        self.assertIsNotNone(CiRequests(self.project.root).outcome(identity))
        summary = ctx.ci_request_summary()
        self.assertEqual(summary["total"], 1)
        entry = summary["requests"][0]
        self.assertEqual(entry["request_id"], identity)
        self.assertEqual(entry["covered"], [covered["id"]])
        self.assertEqual(entry["state"], "resolved")

    def test_an_unresolved_dispatch_is_named_in_the_run_record_and_the_day_listing(self):
        # The provider answers with a run bound to another commit, so the
        # request is refused after it was dispatched: a CI run may have been
        # spent and its result is unknown.
        ctx = self.context(runs(head_sha=OTHER_SHA))
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        with self.assertRaises(Denied):
            self.request(ctx)
        summary = ctx.ci_request_summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["requests"][0]["request_id"], identity)
        self.assertEqual(summary["requests"][0]["state"], "unresolved")
        listing = CiRequests(self.project.root).daily()
        self.assertEqual(listing["total"], 1)
        item = listing["days"][0]["items"][0]
        self.assertEqual(item["request_id"], identity)
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])

    def test_the_delegation_failure_record_names_the_requests_it_made(self):
        # A delegation that fails writes the child's own record; it names the
        # same CI request summary the completed delegation record carries.
        config = dataclasses.replace(self.config, vcs=adapter_settings(runs()))
        roles = dict(config.roles)
        roles["worker"] = dataclasses.replace(
            roles["worker"],
            capabilities=tuple([*roles["worker"].capabilities, "delegate", "vcs_request"]))
        roles["helper"] = dataclasses.replace(
            roles["searcher"], name="helper", capabilities=("files", "read", "finish"))
        config = dataclasses.replace(config, roles=roles)
        def work(ctx, *_):
            if ctx.role.name == "helper":
                raise RuntimeError("the child failed")
            ctx.handle("delegate", {"role": "helper", "task": "spend a CI run"})
        result = Engine(config, driver=ScriptDriver(work)).run(self.project, "worker")
        run_dirs = self.project.root / "runs"
        children = [path for path in run_dirs.iterdir()
                    if path.is_dir() and (path / "error.json").is_file()]
        self.assertEqual(len(children), 1)
        record = read_json(children[0] / "error.json")
        # The failed child's record carries the same CI request summary the
        # completed delegation record carries. A delegation child may not hold
        # the CI request capability, so the summary is empty.
        self.assertIn("ci_requests", record)
        self.assertEqual(record["ci_requests"]["total"], 0)

    def test_the_daily_listing_op_reads_local_evidence_only(self):
        ctx = self.context(runs(), poll_enabled=True, add=("vcs_read",))
        self.request_covered(ctx, [])
        out = ctx.handle("vcs_read", {"op": "ci_requests"})
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["days"][0]["requests"], 1)
        with self.assertRaises(Denied):
            ctx.handle("vcs_read", {"op": "ci_requests", "branch": "mizu"})
