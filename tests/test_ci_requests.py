"""CI requests: one run per workflow at an exact published commit.

Offline only: the adapter is a local double, no provider, no publication.
"""
import contextlib
import dataclasses
import datetime as dt
import json
import shutil
import stat
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from support import Fixture, ScriptDriver
from mizu import ci_requests
from mizu.ci_requests import (ACTIVITY, CI_DIAGNOSTIC_RECORD_BYTES, RECORD_BYTES,
                              CiRequests, _summary, _unresolved_summary, request_id)
from mizu.config import CiRequestEvidence
from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest, mkdir, read_json, write_json
from mizu.protocol import tool_definitions
from mizu.project import Project
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu import vcs
from mizu.process import adapter_text_diagnostic
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

    def request(self, ctx, workflows=("ci",), attempt=None):
        args = {"publication_id": self.receipt["publication_id"],
                "workflows": list(workflows)}
        if attempt is not None:
            args["attempt"] = attempt
        return ctx.handle("vcs_request", args)

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

    def pending_marker(self, **fields):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"], fields.get("attempt", 1))
        store = CiRequests(self.project.root, self.config.ci_requests)
        marker = {"request_id": identity, "publication_id": self.receipt["publication_id"],
                  "sha": SHA, "workflows": ["ci"],
                  "dispatched_at": "2026-10-20T00:00:00+00:00", **fields}
        store.mark_dispatch(identity, marker)
        return store, identity, marker

    def legacy_unknown(self, store, identity, marker):
        record = {key: value for key, value in marker.items() if key != "dispatched_at"}
        record.update(outcome="unknown", reason_code="dispatch_outcome_timeout", runs=[],
                      requested_at="2026-10-21T00:00:00+00:00", unknown_after_seconds=86400,
                      dispatch_digest=digest(canonical(marker)))
        store.record_outcome(identity, record)
        return record

    def test_reconciliation_binds_runs_without_dispatch_and_replays(self):
        store, identity, marker = self.pending_marker()
        ctx = self.context({**runs(), "request_id": identity, "attempt": 1})
        with patch("mizu.runtime._vcs.request_runs_via") as dispatch, \
                patch("mizu.vcs.invoke", wraps=vcs.invoke) as invoke:
            result = self.request(ctx)
        dispatch.assert_not_called()
        self.assertEqual(invoke.call_args.args[1], {
            "op": "reconcile_runs", "request_id": identity, "attempt": 1,
            "sha": SHA, "workflows": ["ci"]})
        self.assertEqual(result["runs"], runs()["runs"])
        self.assertEqual(store.dispatched(identity), marker)
        self.assertEqual(store.daily()["days"][0]["items"][0]["state"], "resolved")
        with patch("mizu.vcs.invoke") as invoke:
            self.assertEqual(self.request(ctx), result)
            with self.assertRaisesRegex(Denied, "preceding"):
                self.request(ctx, attempt=2)
        invoke.assert_not_called()

    def test_reconciliation_unknown_malformed_or_lost_never_authorizes_dispatch(self):
        store, identity, marker = self.pending_marker()
        good = {**runs(), "request_id": identity, "attempt": 1}
        bad = [
            {"outcome": "unknown", "request_id": identity, "attempt": 1},
            {"outcome": "unknown"},
            {"outcome": "unknown", "request_id": identity, "attempt": 1, "runs": []},
            {}, runs(), {**good, "request_id": "0" * 64}, {**good, "attempt": 2},
            {**good, "attempt": True}, {**good, "extra": 1},
            {**good, **runs(head_sha=OTHER_SHA)}, {**good, **runs(workflow="other")},
            {**good, "runs": []},
            {"outcome": "not_dispatched", "request_id": identity, "attempt": 2,
             "reason_code": "provider_rejected"},
            {"outcome": "not_dispatched", "request_id": identity, "attempt": 1,
             "reason_code": "unbounded arbitrary reason"},
            Denied("adapter call lost")]
        for payload in bad:
            with self.subTest(payload=payload), \
                    patch("mizu.runtime._vcs.request_runs_via") as dispatch, \
                    patch("mizu.vcs.invoke", **({"side_effect": payload} if isinstance(payload, Exception)
                                              else {"return_value": payload})):
                ctx = self.context({})
                with patch("mizu.context_vcs_request_tools.now", return_value="2036-10-21T00:00:00+00:00"):
                    with self.assertRaises(Denied):
                        self.request(ctx)
                    with self.assertRaisesRegex(Denied, "preceding"):
                        self.request(ctx, attempt=2)
                dispatch.assert_not_called()
                self.assertIsNone(store.outcome(identity))
                self.assertEqual(store.dispatched(identity), marker)
                self.assertFalse(store._path(identity, "request").exists())
                self.assertFalse(store._path(identity, "resolution").exists())
                self.assertIsNone(store.dispatched(request_id(self.receipt["publication_id"],
                                                             SHA, ["ci"], 2)))
                self.assertEqual(store.daily()["days"][0]["items"][0]["state"], "unresolved")

    def test_reconciled_no_dispatch_alone_authorizes_next_attempt(self):
        store, identity, _ = self.pending_marker()
        ctx = self.context({"outcome": "not_dispatched", "request_id": identity,
                            "attempt": 1, "reason_code": "provider_rejected"})
        with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
            result = self.request(ctx)
        dispatch.assert_not_called()
        self.assertEqual(result["reason_code"], "provider_rejected")
        self.assertEqual(store.outcome(identity)["outcome"], "not_dispatched")
        second = self.request(self.context(runs()), attempt=2)
        self.assertNotIn("possibly_duplicate_spend", second)
        self.assertNotIn("possibly_duplicate_spend", store.dispatched(second["request_id"]))
        self.assertTrue(self.request(self.context({}), attempt=2)["replayed"])

    def test_unknown_diagnostic_is_returned_but_does_not_settle_the_attempt(self):
        store, identity, marker = self.pending_marker()
        payload = {"outcome": "unknown", "request_id": identity, "attempt": 1,
                   "diagnostic": "abcdefgh"}
        ctx = self.context(payload, diagnostic_bytes=4)
        with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
            with self.assertRaisesRegex(Denied, "tail capped at 4 bytes") as caught:
                self.request(ctx)
        dispatch.assert_not_called()
        self.assertIn("efgh", str(caught.exception))
        self.assertEqual(store.dispatched(identity), marker)
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))
        self.assertFalse(store._path(identity, "request").exists())

    def test_unknown_query_neither_registers_nor_completes_covered_waits(self):
        ctx = self.context({"malformed": True}, poll_enabled=True)
        covered = self.covered_item()
        before = self.project.work_items.get(covered["id"])
        with self.assertRaises(Denied):
            self.request_covered(ctx, [covered])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        ctx = self.context({"outcome": "unknown", "request_id": identity, "attempt": 1},
                           poll_enabled=True)
        with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
            with self.assertRaisesRegex(Denied, "unresolved"):
                self.request_covered(ctx, [covered])
        dispatch.assert_not_called()
        self.assertEqual(self.project.work_items.get(covered["id"]), before)
        self.assertIsNone(CiRequests(self.project.root).outcome(identity))
        ctx = self.context({"outcome": "not_dispatched", "request_id": identity,
                            "attempt": 1, "reason_code": "provider_rejected"}, poll_enabled=True)
        result = self.request_covered(ctx, [covered])
        self.assertEqual(result["coverage"]["not_applied"], [covered["id"]])
        self.assertEqual(self.project.work_items.get(covered["id"]), before)

    def test_reconciled_runs_apply_only_the_recorded_coverage(self):
        ctx = self.context({}, poll_enabled=True)
        covered = self.covered_item()
        other = self.covered_item("other")
        with self.assertRaises(Denied):
            self.request_covered(ctx, [covered])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        ctx = self.context({**runs(), "request_id": identity, "attempt": 1}, poll_enabled=True)
        with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
            result = self.request_covered(ctx, [other])
        dispatch.assert_not_called()
        self.assertEqual(result["coverage"]["not_applied"], [other["id"]])
        item = self.project.work_items.get(covered["id"])
        self.assertEqual((item["status"], item["rev"], item["holder"]), ("blocked", 2, "worker"))
        self.assertEqual(item["wait"]["sha"], SHA)
        self.assertEqual(self.project.work_items.get(other["id"])["status"], "open")

    def test_reconciliation_requires_the_request_grant_before_querying(self):
        _, identity, _ = self.pending_marker()
        with patch("mizu.vcs.invoke") as invoke:
            with self.assertRaises(Denied):
                vcs.reconcile_runs_via({"request_grant": False}, SHA, ["ci"], request_id=identity)
        invoke.assert_not_called()

    def test_exact_adapter_outcomes_are_never_replaced(self):
        for workflow, expected in (("ci", None), ("rejected", "not_dispatched")):
            with self.subTest(outcome=expected):
                identity = request_id(self.receipt["publication_id"], SHA, [workflow])
                payload = runs() if expected is None else {
                    "outcome": expected, "request_id": identity, "attempt": 1,
                    "reason_code": "provider_rejected"}
                ctx = self.context(payload)
                result = self.request(ctx, workflows=[workflow])
                with patch("mizu.vcs.invoke") as invoke:
                    replay = self.request(ctx, workflows=[workflow])
                invoke.assert_not_called()
                self.assertEqual(replay, {**result, "replayed": True})
                self.assertNotIn("possibly_duplicate_spend", replay)

    def test_invalid_marker_or_existing_outcome_cannot_be_reconciled(self):
        store, identity, marker = self.pending_marker()
        marker_path = store._path(identity, "dispatch")
        for changes in ({"dispatched_at": "2026-10-20T00:00:00"},
                        {"request_id": "0" * 64}, {"attempt": True},
                        {"possibly_duplicate_spend": True}, {"extra": 1}):
            with self.subTest(changes=changes), patch("mizu.vcs.invoke") as invoke:
                marker_path.write_text(json.dumps({**marker, **changes}))
                with self.assertRaises(Denied):
                    self.request(self.context(runs()))
                invoke.assert_not_called()
        marker_path.write_text(json.dumps(marker))
        outcome_path = store._path(identity, "request")
        for raw in ('{', json.dumps({"outcome": "unknown"})):
            outcome_path.write_text(raw)
            with patch("mizu.vcs.invoke") as invoke:
                with self.assertRaises(Denied):
                    self.request(self.context(runs()))
                invoke.assert_not_called()
            self.assertEqual(outcome_path.read_text(), raw)

    def test_legacy_unknown_validation_binds_marker_bound_reason_time_and_duplicate_flag(self):
        store, identity, marker = self.pending_marker()
        result = self.legacy_unknown(store, identity, marker)
        outcome_path = store._path(identity, "request")
        for changes in ({"dispatch_digest": "0" * 64}, {"unknown_after_seconds": True},
                        {"unknown_after_seconds": 86401}, {"unknown_after_seconds": 0},
                        {"requested_at": "2026-10-20T23:59:59+00:00"},
                        {"requested_at": "2026-10-21T00:00:00"},
                        {"reason_code": "provider_rejected"}, {"runs": runs()["runs"]},
                        {"possibly_duplicate_spend": True}):
            with self.subTest(changes=changes):
                outcome_path.write_text(json.dumps({**result, **changes}))
                with self.assertRaises(Denied):
                    store.outcome(identity)
                self.assertEqual(store.daily()["days"][0]["items"][0]["state"], "unresolved")
        outcome_path.write_text(json.dumps(result))
        store._path(identity, "dispatch").write_text(json.dumps(
            {**marker, "dispatched_at": "2026-10-19T00:00:00+00:00"}))
        with self.assertRaises(Denied):
            store.outcome(identity)

    def test_legacy_unknown_resolution_is_immutable_and_bound_to_both_records(self):
        for attempt, outcome in ((1, "runs"), (2, "not_dispatched")):
            with self.subTest(outcome=outcome):
                store, identity, marker = self.pending_marker(
                    attempt=attempt, **({"possibly_duplicate_spend": True} if attempt == 2 else {}))
                legacy = self.legacy_unknown(store, identity, marker)
                original = store._path(identity, "request").read_bytes()
                self.assertIsNone(store.outcome(identity))
                row = next(row for row in store.page()["items"] if row["request_id"] == identity)
                self.assertEqual(row["state"], "unresolved")
                unknown = {"outcome": "unknown", "request_id": identity, "attempt": attempt}
                with self.assertRaises(Denied):
                    self.request(self.context(unknown), attempt=attempt)
                self.assertFalse(store._path(identity, "resolution").exists())
                with self.assertRaisesRegex(Denied, "preceding"):
                    self.request(self.context(runs()), attempt=attempt + 1)
                payload = {"request_id": identity, "attempt": attempt}
                payload.update(runs() if outcome == "runs" else
                               {"outcome": outcome, "reason_code": "pre_dispatch_refusal"})
                with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
                    result = self.request(self.context(payload), attempt=attempt)
                dispatch.assert_not_called()
                self.assertEqual(store._path(identity, "request").read_bytes(), original)
                self.assertEqual(store.dispatched(identity), marker)
                effective = store.outcome(identity)
                self.assertEqual(effective["runs"], result["runs"])
                sidecar_path = store._path(identity, "resolution")
                sidecar = read_json(sidecar_path)
                self.assertEqual(sidecar["dispatch_digest"], digest(canonical(marker)))
                self.assertEqual(sidecar["legacy_outcome_digest"], digest(canonical(legacy)))
                with self.assertRaisesRegex(Denied, "Immutable object conflict"):
                    store._write(identity, "resolution", {**sidecar, "dispatch_digest": "0" * 64})
                expected = "resolved" if outcome == "runs" else "not_dispatched"
                day_row = next(row for day in store.daily()["days"] for row in day["items"]
                               if row["request_id"] == identity)
                self.assertEqual(day_row["state"], expected)
                row = next(row for row in store.page()["items"] if row["request_id"] == identity)
                self.assertEqual(row["state"], expected)
                self.assertEqual(row["possibly_duplicate_spend"], attempt == 2)
                for changes in ({"dispatch_digest": "0" * 64},
                                {"legacy_outcome_digest": "0" * 64},
                                {"outcome": legacy}, {"extra": 1},
                                {"outcome": {**effective, "sha": OTHER_SHA}},
                                {"outcome": {**effective, "possibly_duplicate_spend": 1}},
                                {"outcome": {**effective, "possibly_duplicate_spend": not (attempt == 2)}}):
                    sidecar_path.write_text(json.dumps({**sidecar, **changes}))
                    with self.assertRaises(Denied):
                        store.outcome(identity)
                    row = next(row for row in store.page()["items"] if row["request_id"] == identity)
                    self.assertEqual(row["state"], "unresolved")
                sidecar_path.write_text(json.dumps(sidecar))
                with patch("mizu.vcs.invoke") as invoke:
                    self.assertTrue(self.request(self.context({}), attempt=attempt)["replayed"])
                invoke.assert_not_called()
                if attempt == 2:
                    next_result = self.request(self.context(runs()), attempt=3)
                    self.assertTrue(next_result["possibly_duplicate_spend"])

    def test_legacy_chain_retains_risk_until_exact_prior_no_spend_reconciliation(self):
        # A legacy retry could follow timeout-unknown. The immediate predecessor
        # permits a retry, but cannot establish that the whole chain spent no runs.
        for resolution in ("not_dispatched", "runs"):
            with self.subTest(resolution=resolution):
                store, first_id, first = self.pending_marker()
                self.legacy_unknown(store, first_id, first)
                _, second_id, second = self.pending_marker(attempt=2, possibly_duplicate_spend=True)
                no_spend = {key: value for key, value in second.items() if key != "dispatched_at"}
                no_spend.update(outcome="not_dispatched", reason_code="provider_rejected",
                                runs=[], requested_at="2026-10-21T00:00:00+00:00")
                store.record_outcome(second_id, no_spend)
                # Seek indexes are rewritten with each dispatch; markers and
                # outcomes stay immutable.
                historical = {path: path.read_bytes() for path in store.root.iterdir()
                              if path.name != ci_requests.SEEK_DIR}

                third_id = request_id(self.receipt["publication_id"], SHA, ["ci"], 3)
                payload = {"outcome": "not_dispatched", "request_id": third_id,
                           "attempt": 3, "reason_code": "provider_rejected"}
                ctx = self.context(payload)
                result = self.request(ctx, attempt=3)
                self.assertTrue(result["possibly_duplicate_spend"])
                self.assertTrue(store.dispatched(third_id)["possibly_duplicate_spend"])
                self.assertTrue(store.outcome(third_id)["possibly_duplicate_spend"])
                activity = ctx.ci_request_summary()["requests"][0]
                self.assertEqual((activity["state"], activity["possibly_duplicate_spend"]),
                                 ("not_dispatched", True))
                evidence = list(ctx.run_dir.glob("vcs-request/*.json"))
                self.assertEqual(len(evidence), 1)
                self.assertTrue(read_json(evidence[0])["possibly_duplicate_spend"])
                row = next(row for row in store.page()["items"] if row["request_id"] == third_id)
                self.assertTrue(row["possibly_duplicate_spend"])
                for path, raw in historical.items():
                    self.assertEqual(path.read_bytes(), raw)

                payload = {"request_id": first_id, "attempt": 1}
                payload.update(runs() if resolution == "runs" else
                               {"outcome": resolution, "reason_code": "provider_rejected"})
                with patch("mizu.runtime._vcs.request_runs_via") as dispatch:
                    self.request(self.context(payload))
                dispatch.assert_not_called()
                for path, raw in historical.items():
                    self.assertEqual(path.read_bytes(), raw)
                ctx = self.context(runs())
                fourth = self.request(ctx, attempt=4)
                fourth_id = fourth["request_id"]
                evidence = list(ctx.run_dir.glob("vcs-request/*.json"))
                self.assertEqual(len(evidence), 1)
                for record in (fourth, store.dispatched(fourth_id), store.outcome(fourth_id),
                               ctx.ci_request_summary()["requests"][0], read_json(evidence[0])):
                    self.assertEqual(record.get("possibly_duplicate_spend", False), resolution == "runs")
                # Later knowledge never rewrites the risk recorded at dispatch time.
                self.assertTrue(store.outcome(third_id)["possibly_duplicate_spend"])
                for path in store.root.iterdir():
                    if path.is_dir():
                        for child in path.iterdir():
                            child.unlink()
                        path.rmdir()
                    else:
                        path.unlink()

    def test_incomplete_unreadable_or_bounded_prior_history_never_clears_risk(self):
        store, first_id, first = self.pending_marker()
        _, second_id, second = self.pending_marker(attempt=2, possibly_duplicate_spend=True)
        no_spend = {key: value for key, value in second.items() if key != "dispatched_at"}
        no_spend.update(outcome="not_dispatched", reason_code="provider_rejected",
                        runs=[], requested_at="2026-10-21T00:00:00+00:00")
        store.record_outcome(second_id, no_spend)
        first_no_spend = {**no_spend, "request_id": first_id, "attempt": 1}
        first_no_spend.pop("possibly_duplicate_spend")
        for problem in ("missing", "unreadable", "invalid", "bounded"):
            with self.subTest(problem=problem):
                path = store._path(first_id, "request")
                if problem == "missing":
                    store._path(first_id, "dispatch").unlink()
                else:
                    store._path(first_id, "dispatch").write_text(json.dumps(first))
                    path.write_text("invalid json" if problem == "unreadable" else json.dumps(
                        {**first_no_spend, **({"sha": OTHER_SHA} if problem == "invalid" else {})}))
                ctx = self.context(runs())
                if problem == "bounded":
                    ctx.config = dataclasses.replace(ctx.config, ci_requests=dataclasses.replace(
                        ctx.config.ci_requests, record_limit=1))
                result = self.request(ctx, attempt=3)
                self.assertTrue(result["possibly_duplicate_spend"])
                self.assertTrue(ctx.ci_request_summary()["requests"][0]["possibly_duplicate_spend"])
                store._path(result["request_id"], "dispatch").unlink()
                store._path(result["request_id"], "request").unlink()

    def test_risky_dispatch_failure_is_visible_in_activity_before_outcome(self):
        store, first_id, first = self.pending_marker()
        self.legacy_unknown(store, first_id, first)
        # Also recover risk if an older writer already lost the predecessor's flag.
        _, second_id, marker = self.pending_marker(attempt=2)
        record = {key: value for key, value in marker.items() if key != "dispatched_at"}
        record.update(outcome="not_dispatched", reason_code="provider_rejected",
                      runs=[], requested_at="2026-10-21T00:00:00+00:00")
        store.record_outcome(second_id, record)
        ctx = self.context({"malformed": True})
        with self.assertRaises(Denied):
            self.request(ctx, attempt=3)
        activity = ctx.ci_request_summary()["requests"][0]
        self.assertEqual((activity["state"], activity["possibly_duplicate_spend"]),
                         ("unresolved", True))
        self.assertTrue(store.dispatched(activity["request_id"])["possibly_duplicate_spend"])
        self.assertIsNone(store.outcome(activity["request_id"]))

    def test_adapter_request_and_no_dispatch_echo_bind_the_explicit_attempt(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"], 2)
        payload = {"outcome": "not_dispatched", "request_id": identity, "attempt": 2,
                   "reason_code": "provider_rejected"}
        with patch("mizu.vcs.invoke", return_value=payload) as invoke:
            result = vcs.request_runs_via({"request_grant": True}, SHA, ["ci"],
                                          request_id=identity, attempt=2)
        self.assertEqual(result["request_id"], identity)
        self.assertEqual(result["attempt"], 2)
        request = invoke.call_args.args[1]
        self.assertEqual((request["request_id"], request["attempt"]), (identity, 2))
        for bad in ({**payload, "request_id": "0" * 64},
                    {**payload, "attempt": 1},
                    {**payload, "reason_code": ["provider_rejected"]},
                    {**payload, "runs": []}):
            with self.subTest(response=bad), patch("mizu.vcs.invoke", return_value=bad):
                with self.assertRaises(Denied):
                    vcs.request_runs_via({"request_grant": True}, SHA, ["ci"],
                                         request_id=identity, attempt=2)

    def test_typed_ci_diagnostic_is_bounded_configurable_and_untrusted(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        payload = {"outcome": "not_dispatched", "request_id": identity, "attempt": 1,
                   "reason_code": "provider_rejected", "diagnostic": "abcdefgh"}
        with patch("mizu.vcs.invoke", return_value=payload):
            result = vcs.request_runs_via(
                {"request_grant": True, "diagnostic_bytes": 4}, SHA, ["ci"],
                request_id=identity)
        self.assertEqual(result["diagnostic"],
                         "\nAdapter diagnostic (external-untrusted; VCS; tail capped at 4 bytes):\nefgh")
        with patch("mizu.vcs.invoke", return_value=payload):
            hidden = vcs.request_runs_via(
                {"request_grant": True, "diagnostic_bytes": 0}, SHA, ["ci"],
                request_id=identity)
        self.assertNotIn("diagnostic", hidden)
        with patch("mizu.vcs.invoke", return_value={**payload, "diagnostic": ["bad"]}):
            with self.assertRaisesRegex(Denied, "diagnostic"):
                vcs.request_runs_via(
                    {"request_grant": True, "diagnostic_bytes": 8}, SHA, ["ci"],
                    request_id=identity)

    def test_no_dispatch_diagnostic_persists_for_exact_replay(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        payload = {"outcome": "not_dispatched", "request_id": identity, "attempt": 1,
                   "reason_code": "provider_rejected", "diagnostic": "abcdefgh"}
        first = self.request(self.context(payload, diagnostic_bytes=4))
        expected = "\nAdapter diagnostic (external-untrusted; VCS; tail capped at 4 bytes):\nefgh"
        self.assertEqual(first["diagnostic"], expected)
        self.assertEqual(CiRequests(self.project.root).outcome(identity)["diagnostic"], expected)
        with patch("mizu.runtime._vcs.request_runs_via",
                   side_effect=AssertionError("terminal replay cannot call the adapter")):
            replay = self.request(self.context(runs()))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["diagnostic"], expected)

    def test_maximum_control_diagnostic_roundtrips_with_worst_case_escaping(self):
        store, identity, marker = self.pending_marker()
        record = {key: value for key, value in marker.items() if key != "dispatched_at"}
        diagnostic = adapter_text_diagnostic("\x01" * 8192, source="VCS", maximum_bytes=8192)
        record.update(outcome="not_dispatched", reason_code="pre_dispatch_refusal", runs=[],
                      requested_at="2026-10-21T00:00:00+00:00", diagnostic=diagnostic)
        store.record_outcome(identity, record)
        stored = store.outcome(identity)
        self.assertEqual(stored["diagnostic"], diagnostic)
        self.assertGreater(store._path(identity, "request").stat().st_size, RECORD_BYTES)
        self.assertLessEqual(store._path(identity, "request").stat().st_size,
                             CI_DIAGNOSTIC_RECORD_BYTES)

    def test_maximum_control_diagnostic_roundtrips_in_legacy_resolution_sidecar(self):
        store, identity, marker = self.pending_marker()
        self.legacy_unknown(store, identity, marker)
        record = {key: value for key, value in marker.items() if key != "dispatched_at"}
        diagnostic = adapter_text_diagnostic("\x01" * 8192, source="VCS", maximum_bytes=8192)
        record.update(outcome="not_dispatched", reason_code="provider_rejected", runs=[],
                      requested_at="2026-10-21T00:00:00+00:00", diagnostic=diagnostic)
        store.resolve(identity, record)
        stored = store.outcome(identity)
        self.assertEqual(stored["diagnostic"], diagnostic)
        self.assertGreater(store._path(identity, "resolution").stat().st_size, RECORD_BYTES)
        self.assertLessEqual(store._path(identity, "resolution").stat().st_size,
                             CI_DIAGNOSTIC_RECORD_BYTES)

    def test_terminal_no_dispatch_is_durable_and_replays_without_a_provider_call(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        payload = {"outcome": "not_dispatched", "request_id": identity, "attempt": 1,
                   "reason_code": "pre_dispatch_refusal"}
        ctx = self.context(payload)
        first = self.request(ctx)
        self.assertEqual(first["outcome"], "not_dispatched")
        self.assertEqual(first["reason_code"], "pre_dispatch_refusal")
        self.assertEqual(first["runs"], [])
        stored = CiRequests(self.project.root).outcome(identity)
        self.assertEqual(stored["outcome"], "not_dispatched")
        row = CiRequests(self.project.root).daily()["days"][0]["items"][0]
        self.assertEqual((row["state"], row["attempt"], row["reason_code"]),
                         ("not_dispatched", 1, "pre_dispatch_refusal"))
        activity = ctx.ci_request_summary()["requests"][0]
        self.assertEqual((activity["state"], activity["attempt"], activity["reason_code"]),
                         ("not_dispatched", 1, "pre_dispatch_refusal"))
        with patch("mizu.runtime._vcs.request_runs_via",
                   side_effect=AssertionError("terminal replay cannot call the adapter")):
            again = self.request(self.context(runs()))
        self.assertTrue(again["replayed"])
        self.assertEqual(again["request_id"], identity)

    def test_malformed_no_dispatch_echo_stays_unresolved_and_blocks_later_attempts(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        bad = {"outcome": "not_dispatched", "request_id": "0" * 64, "attempt": 1,
               "reason_code": "provider_rejected"}
        with self.assertRaises(Denied):
            self.request(self.context(bad))
        self.assertEqual(CiRequests(self.project.root).daily()["days"][0]["items"][0]["state"],
                         "unresolved")
        with self.assertRaises(Denied):
            self.request(self.context(runs()), attempt=2)
        self.assertIsNotNone(CiRequests(self.project.root).dispatched(identity))
        self.assertIsNone(CiRequests(self.project.root).dispatched(
            request_id(self.receipt["publication_id"], SHA, ["ci"], 2)))

    def test_next_attempt_requires_exact_previous_no_dispatch_and_is_independently_idempotent(self):
        with self.assertRaises(Denied):
            self.request(self.context(runs()), attempt=2)
        no_spend_id = request_id(self.receipt["publication_id"], SHA, ["ci"])
        self.assertIsNone(CiRequests(self.project.root).dispatched(no_spend_id))
        self.request(self.context({"outcome": "not_dispatched", "request_id": no_spend_id,
                                  "attempt": 1, "reason_code": "provider_rejected"}))
        second_id = request_id(self.receipt["publication_id"], SHA, ["ci"], 2)
        second = self.request(self.context(runs()), attempt=2)
        self.assertEqual(second["request_id"], second_id)
        self.assertEqual(second["attempt"], 2)
        rows = [row for day in CiRequests(self.project.root).daily()["days"]
                for row in day["items"]]
        self.assertEqual({(row["attempt"], row["state"]) for row in rows},
                         {(1, "not_dispatched"), (2, "resolved")})
        self.assertTrue(self.request(self.context(runs()), attempt=2)["replayed"])
        with self.assertRaises(Denied):
            self.request(self.context(runs()), attempt=3)

    def test_no_dispatch_leaves_covered_items_unchanged(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        item = self.covered_item()
        ctx = self.context({"outcome": "not_dispatched", "request_id": identity,
                            "attempt": 1, "reason_code": "pre_dispatch_refusal"},
                           poll_enabled=True)
        out = self.request_covered(ctx, [item])
        self.assertEqual(out["coverage"]["not_applied"], [item["id"]])
        self.assertEqual(self.project.work_items.get(item["id"])["status"], "open")

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

    def test_replay_rejects_a_stored_run_bound_to_another_sha_without_redispatch(self):
        self.request(self.context(runs()))
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        store = CiRequests(self.project.root)
        path = store.root / f"{identity}.request.json"
        record = read_json(path)
        record["runs"][0]["head_sha"] = OTHER_SHA
        path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaises(Denied):
            store.outcome(identity)
        item = store.daily()["days"][0]["items"][0]
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])
        with patch("mizu.runtime._vcs.request_runs_via",
                   side_effect=AssertionError("an existing dispatch must not be retried")):
            with self.assertRaises(Denied):
                self.request(self.context(runs()))

    def test_replay_rejects_coverage_not_bound_by_its_dispatch_marker(self):
        ctx = self.context(runs(), poll_enabled=True)
        recorded_item = self.covered_item("recorded")
        other_item = self.covered_item("other")
        self.request_covered(ctx, [recorded_item])
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        store = CiRequests(self.project.root)
        path = store.root / f"{identity}.request.json"
        record = read_json(path)
        record["covered"][0]["id"] = other_item["id"]
        path.write_text(json.dumps(record), encoding="utf-8")

        with patch("mizu.runtime._vcs.request_runs_via",
                   side_effect=AssertionError("an existing dispatch must not be retried")):
            with self.assertRaises(Denied):
                self.request_covered(self.context(runs(), poll_enabled=True), [other_item])
        self.assertEqual(self.project.work_items.get(other_item["id"])["status"], "open")
        item = store.daily()["days"][0]["items"][0]
        self.assertEqual(item["state"], "unresolved")
        self.assertEqual(item["runs"], [])

    def test_an_unresolved_dispatch_is_never_retried_blindly(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root).mark_dispatch(
            identity, {"request_id": identity, "sha": SHA, "workflows": ["ci"]})
        ctx = self.context(runs())
        with self.assertRaises(Denied):
            self.request(ctx)
        with self.assertRaises(Denied):
            CiRequests(self.project.root).outcome(identity)

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

    def test_daily_and_cost_pages_accept_one_and_reject_zero(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        identities = []
        for sha, instant in ((SHA, "2026-10-20T23:59:00+00:00"),
                             (OTHER_SHA, "2026-10-21T00:01:00+00:00")):
            identity = request_id(publication_id, sha, ["ci"])
            identities.append(identity)
            store.mark_dispatch(identity, {
                "request_id": identity, "publication_id": publication_id,
                "sha": sha, "workflows": ["ci"], "dispatched_at": instant,
            })

        daily = store.daily(limit=1)
        self.assertEqual(daily["total"], 2)
        self.assertTrue(daily["truncated"])
        self.assertEqual(daily["days"][0]["requests"], 1)
        self.assertEqual(daily["days"][0]["items"][0]["request_id"], identities[0])
        page = store.page(limit=1, offset=0)
        self.assertEqual(page["items"][0]["request_id"], identities[0])
        self.assertEqual(page["next_offset"], 1)
        for read in (lambda: store.daily(limit=0), lambda: store.page(limit=0)):
            with self.subTest(read=read), self.assertRaises(Denied):
                read()

    def test_seek_index_is_created_updated_and_read_without_a_shared_scan(self):
        store = CiRequests(self.project.root, self.config.ci_requests)
        publication = self.receipt["publication_id"]
        first = request_id(publication, SHA, ["ci"])
        second = request_id(publication, SHA, ["ci"], 2)
        for identity, day, attempt in ((first, "2026-10-20", 1), (second, "2026-10-21", 2)):
            store.mark_dispatch(identity, {
                "request_id": identity, "publication_id": publication, "sha": SHA,
                "workflows": ["ci"], "dispatched_at": f"{day}T00:00:00+00:00",
                "attempt": attempt, "possibly_duplicate_spend": attempt == 2})
        index = read_json(store._seek_path(publication))
        self.assertEqual(index["publication_id"], publication)
        self.assertEqual(index["entries"], [
            {"request_id": first, "date": "2026-10-20"},
            {"request_id": second, "date": "2026-10-21"}])
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            self.assertEqual(store.matching([publication], not_before=dt.date(2026, 10, 20)),
                             {"items": [second, first], "incomplete": False})

    def test_seek_matching_at_default_cap_with_older_unrelated_markers(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.assertEqual(store.policy.record_limit, 1000)
        mkdir(store.root)
        other = "f" * 64
        for number in range(1001):
            sha = f"{number:040x}"
            identity = request_id(other, sha, ["ci"])
            write_json(store.root / f"{identity}.dispatch.json", {
                "request_id": identity, "publication_id": other, "sha": sha,
                "workflows": ["ci"], "dispatched_at": "2020-01-01T00:00:00+00:00"})
        publication = self.receipt["publication_id"]
        fresh = request_id(publication, SHA, ["ci"])
        store.mark_dispatch(fresh, {
            "request_id": fresh, "publication_id": publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        # The shared scan really is capped in this fixture, but the seek path
        # never touches it and so never reports an incomplete page.
        self.assertTrue(store._rows()[1])
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            pages = store.matching_many({"head": ([publication], dt.date(2026, 10, 1))})
        self.assertEqual(pages, {"head": {"items": [fresh], "incomplete": False}})

    def write_activity(self):
        path = self.project.root / ACTIVITY
        mkdir(path.parent)
        write_json(path, uuid.uuid4().hex)
        return path

    def fresh_receipt(self, recorded_at="2036-10-22T12:00:00+00:00"):
        with patch("mizu.publication.now", return_value=recorded_at):
            return Publication.record_external(
                self.project.root, branch="main", published_sha=SHA,
                code_digest="1" * 64, approval="approval", source_commit="d" * 40,
                projection=public_source_identity({}))

    def unrelated_marker(self, store, sha, publication="f" * 64):
        identity = request_id(publication, sha, ["ci"])
        mkdir(store.root)
        write_json(store.root / f"{identity}.dispatch.json", {
            "request_id": identity, "publication_id": publication, "sha": sha,
            "workflows": ["ci"], "dispatched_at": "2026-01-01T00:00:00+00:00"})
        return identity

    def activity_stat_patch(self, *, mode=None, mtime_ns=None, ctime_ns=None, error=None,
                            content=None):
        """Control the single pinned read that ``_fresh_activity`` observes."""
        real_read = ci_requests.safe_read_info

        class Info:
            st_mtime_ns = mtime_ns if mtime_ns is not None else 0
            st_ctime_ns = ctime_ns if ctime_ns is not None else 0

        def read(root, name, maximum):
            if name != ACTIVITY:
                return real_read(root, name, maximum)
            if error is not None:
                raise error
            if mode is not None and not stat.S_ISREG(mode):
                raise Denied("Only regular, non-hardlinked files are readable")
            raw = real_read(root, name, maximum)[0] if content is None else content
            return raw, Info()

        return patch.object(ci_requests, "safe_read_info", read)

    def test_fresh_publication_without_index_needs_no_shared_scan(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        receipt = self.fresh_receipt()
        publication = receipt["publication_id"]
        self.assertFalse(store._seek_path(publication).exists())
        for number in range(3):
            self.unrelated_marker(store, f"{number:040x}")
        # A controlled over-cap name list proves the shared reader would be
        # stopped and capped here without fabricating 1000 real markers.
        names = [f"{request_id('f' * 64, f'{number:040x}', ['ci'])}.dispatch.json"
                 for number in range(1001)]
        with patch("mizu.ci_requests.os.listdir", return_value=names):
            self.assertTrue(store._rows()[1])
        instant = dt.datetime.fromisoformat(receipt["recorded_at"])
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")) as scan, \
                patch.object(store, "_read", side_effect=AssertionError("dispatch read used")) as read:
            pages = store.matching_many({"head": ([publication], instant)})
        self.assertEqual(pages, {"head": {"items": [], "incomplete": False}})
        scan.assert_not_called()
        read.assert_not_called()
        # Date-based callers use midnight UTC; a non-UTC instant converts to it.
        for window in (instant.date(), instant.astimezone(dt.timezone(dt.timedelta(hours=9)))):
            with self.subTest(window=window), \
                    patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
                self.assertEqual(store.matching([publication], not_before=window),
                                 {"items": [], "incomplete": False})

    def test_first_dispatch_is_found_from_its_index_without_a_scan(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        receipt = self.fresh_receipt()
        publication = receipt["publication_id"]
        identity = request_id(publication, SHA, ["ci"])
        store.mark_dispatch(identity, {
            "request_id": identity, "publication_id": publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": receipt["recorded_at"]})
        self.assertTrue(store._seek_path(publication).exists())
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            pages = store.matching_many({
                "head": ([publication], dt.datetime.fromisoformat(receipt["recorded_at"]))})
        self.assertEqual(pages, {"head": {"items": [identity], "incomplete": False}})

    def test_unrelated_dispatch_does_not_revoke_known_empty(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        mkdir(store.root)
        publication = self.fresh_receipt()["publication_id"]
        instant = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        stamp = dt.datetime(2026, 5, 31, tzinfo=dt.timezone.utc)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        delta = stamp - epoch
        stamp_ns = ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
                    + delta.microseconds * 1000)
        with self.activity_stat_patch(mtime_ns=stamp_ns, ctime_ns=stamp_ns):
            with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
                pages = store.matching_many({"head": ([publication], instant)})
            self.assertEqual(pages, {"head": {"items": [], "incomplete": False}})
            self.assertTrue(store._seek_path(publication).exists())
            self.assertEqual(store._seek_read(publication), ([], False))
        # A valid dispatch for a different publication rotates the global
        # token, so freshness alone can no longer prove this publication quiet.
        other = "e" * 64
        identity = request_id(other, SHA, ["ci"])
        store.mark_dispatch(identity, {
            "request_id": identity, "publication_id": other, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-06-02T00:00:00+00:00"})
        self.assertFalse(store._fresh_activity(instant))
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            pages = store.matching_many({"head": ([publication], instant)})
        self.assertEqual(pages, {"head": {"items": [], "incomplete": False}})

    def test_established_empty_index_grows_on_first_dispatch(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        mkdir(store.root)
        publication = self.fresh_receipt()["publication_id"]
        instant = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        stamp = dt.datetime(2026, 5, 31, tzinfo=dt.timezone.utc)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        delta = stamp - epoch
        stamp_ns = ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
                    + delta.microseconds * 1000)
        with self.activity_stat_patch(mtime_ns=stamp_ns, ctime_ns=stamp_ns):
            with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
                pages = store.matching_many({"head": ([publication], instant)})
        self.assertEqual(pages, {"head": {"items": [], "incomplete": False}})
        self.assertEqual(store._seek_read(publication), ([], False))
        identity = request_id(publication, SHA, ["ci"])
        store.mark_dispatch(identity, {
            "request_id": identity, "publication_id": publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-06-02T00:00:00+00:00"})
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            pages = store.matching_many({"head": ([publication], instant)})
        self.assertEqual(pages, {"head": {"items": [identity], "incomplete": False}})
        self.assertEqual(store._seek_read(publication), ([(identity, "2026-06-02")], False))

    def test_activity_boundary_uses_the_exact_instant_not_its_date(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        publication = self.fresh_receipt()["publication_id"]
        stamp = dt.datetime(2036, 10, 22, 12, tzinfo=dt.timezone.utc)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        delta = stamp - epoch
        stamp_ns = ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
                    + delta.microseconds * 1000)
        with self.activity_stat_patch(mtime_ns=stamp_ns, ctime_ns=stamp_ns):
            with patch.object(store, "_rows", return_value=([], True)) as scan:
                page = store.matching([publication], not_before=stamp)
            self.assertTrue(page["incomplete"])
            self.assertEqual(scan.call_count, 1)
            with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
                self.assertEqual(store.matching(
                    [publication], not_before=stamp + dt.timedelta(microseconds=1)),
                    {"items": [], "incomplete": False})

    def test_uncertain_activity_stays_on_the_conservative_scan(self):
        policy = dataclasses.replace(CiRequestEvidence(), record_limit=1)
        store = CiRequests(self.project.root, policy)
        publication = "f" * 64
        for sha in (SHA, OTHER_SHA):
            self.unrelated_marker(store, sha, publication)
        token = self.write_activity()
        past = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        future = dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc)
        cutoff_ns = 1_577_836_800_000_000_000
        for case in ("newer", "equal", "only-change-equal", "missing",
                     "unreadable", "nonregular"):
            with self.subTest(case=case), contextlib.ExitStack() as stack:
                window = past if case in ("newer", "equal", "only-change-equal") else future
                if case == "equal":
                    stack.enter_context(self.activity_stat_patch(
                        mtime_ns=cutoff_ns, ctime_ns=cutoff_ns))
                elif case == "only-change-equal":
                    stack.enter_context(self.activity_stat_patch(
                        mtime_ns=cutoff_ns - 1, ctime_ns=cutoff_ns))
                elif case == "unreadable":
                    stack.enter_context(self.activity_stat_patch(error=OSError("unreadable")))
                elif case == "nonregular":
                    stack.enter_context(self.activity_stat_patch(mode=stat.S_IFDIR | 0o700))
                if case == "missing":
                    token.unlink()
                with patch.object(store, "_rows", wraps=store._rows) as scan:
                    page = store.matching([publication], not_before=window)
                self.assertEqual(scan.call_count, 1)
                self.assertTrue(page["incomplete"])
                if case == "missing":
                    self.write_activity()

    def test_malformed_activity_token_cannot_prove_quiet_and_hide_a_marker(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        publication = "f" * 64
        marker = self.unrelated_marker(store, SHA, publication)
        self.write_activity()
        # The marker is dated 2026-01-01; query from that day so a real scan
        # returns it. Old token timestamps alone used to prove "known empty"
        # and skip the scan, hiding the marker as a complete-empty page.
        window = dt.date(2026, 1, 1)
        valid = json.dumps(uuid.uuid4().hex).encode("utf-8")
        for case, content in (("valid", valid), ("not-json", b"not JSON"),
                              ("wrong-hex", b'"' + b"z" * 32 + b'"'),
                              ("integer", b"123"), ("null", b"null")):
            with self.subTest(case=case), \
                    self.activity_stat_patch(content=content, mtime_ns=0, ctime_ns=0):
                with patch.object(store, "_rows", wraps=store._rows) as scan:
                    page = store.matching([publication], not_before=window)
                if case == "valid":
                    scan.assert_not_called()
                    self.assertEqual(page, {"items": [], "incomplete": False})
                else:
                    self.assertEqual(scan.call_count, 1)
                    self.assertIn(marker, page["items"])

    def test_activity_file_on_disk_discriminates_on_content_not_ctime(self):
        # POSIX ctime cannot be backdated with os.utime, so a real file made to
        # look old fails the quiet proof on ctime alone and never tests its
        # content. Read real bytes from disk but pin only the stat observation
        # to old timestamps: the on-disk content becomes the sole discriminator.
        # Each case uses its own publication so a valid token's durable empty
        # seek index cannot mask the malformed cases that follow.
        cases = (("valid", "1" * 64, json.dumps(uuid.uuid4().hex).encode("utf-8")),
                 ("not-json", "2" * 64, b"not JSON"),
                 ("wrong-hex", "3" * 64, b'"' + b"z" * 32 + b'"'))
        for case, publication, content in cases:
            with self.subTest(case=case):
                store = CiRequests(self.project.root, CiRequestEvidence())
                marker = self.unrelated_marker(store, SHA, publication)
                token = self.write_activity()
                token.write_bytes(content)
                # content=None keeps the real safe_read_info byte read; only
                # mtime/ctime come from the patched stat observation.
                with self.activity_stat_patch(mtime_ns=0, ctime_ns=0), \
                        patch.object(store, "_rows", wraps=store._rows) as scan:
                    page = store.matching([publication], not_before=dt.date(2026, 1, 1))
                if case == "valid":
                    scan.assert_not_called()
                    self.assertEqual(page, {"items": [], "incomplete": False})
                else:
                    self.assertEqual(scan.call_count, 1)
                    self.assertIn(marker, page["items"])

    def test_known_empty_page_is_not_marked_incomplete_by_an_unrelated_scan(self):
        store = CiRequests(self.project.root, CiRequestEvidence())
        self.write_activity()
        fresh = self.fresh_receipt()["publication_id"]
        legacy = "not-a-publication-id"  # No index or freshness proof is possible.
        window = dt.datetime(2036, 1, 1, tzinfo=dt.timezone.utc)
        rows = [{"date": "2036-01-02", "request_id": "legacy-request",
                 "publication_id": legacy}]
        with patch.object(store, "_rows", return_value=(rows, True)) as scan:
            pages = store.matching_many({
                "fresh": ([fresh], window), "legacy": ([legacy], window),
                "both": ([fresh, legacy], window)})
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(pages["fresh"], {"items": [], "incomplete": False})
        self.assertEqual(pages["legacy"], {"items": ["legacy-request"],
                                           "incomplete": True})
        self.assertEqual(pages["both"], {"items": ["legacy-request"],
                                         "incomplete": True})

    def test_malformed_index_or_candidate_fails_closed(self):
        store = CiRequests(self.project.root, self.config.ci_requests)
        publication = self.receipt["publication_id"]
        identity = request_id(publication, SHA, ["ci"])
        marker = {"request_id": identity, "publication_id": publication, "sha": SHA,
                  "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"}
        store.mark_dispatch(identity, marker)
        path = store._seek_path(publication)
        good = read_json(path)
        query = {"head": ([publication], dt.date(2026, 10, 1))}
        path.write_text("{not-json", encoding="utf-8")
        self.assertTrue(store.matching_many(query)["head"]["incomplete"])
        write_json(path, {**good, "entries": good["entries"] * 2})
        self.assertTrue(store.matching_many(query)["head"]["incomplete"])
        # A well-formed index whose candidate no longer agrees with its marker
        # is stale; it must not be returned as a complete absence or identity.
        write_json(path, {**good, "entries": [{**good["entries"][0], "date": "2026-10-21"}]})
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            self.assertEqual(store.matching_many(query),
                             {"head": {"items": [], "incomplete": True}})
        write_json(path, good)
        store._path(identity, "dispatch").unlink()
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            self.assertEqual(store.matching_many(query),
                             {"head": {"items": [], "incomplete": True}})

    def test_malformed_index_marks_only_affected_pages_incomplete(self):
        store = CiRequests(self.project.root, self.config.ci_requests)
        first, second = self.receipt["publication_id"], "f" * 64
        for publication, sha in ((first, SHA), (second, OTHER_SHA)):
            identity = request_id(publication, sha, ["ci"])
            store.mark_dispatch(identity, {
                "request_id": identity, "publication_id": publication, "sha": sha,
                "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        store._seek_path(first).write_text("{not-json", encoding="utf-8")
        window = dt.date(2026, 10, 1)
        pages = store.matching_many({"one": ([first], window), "two": ([second], window),
                                     "both": ([first, second], window)})
        self.assertTrue(pages["one"]["incomplete"])
        self.assertTrue(pages["both"]["incomplete"])
        self.assertFalse(pages["two"]["incomplete"])

    def test_many_seek_indexes_do_not_crowd_legacy_marker(self):
        policy = dataclasses.replace(self.config.ci_requests,
                                     name_processing_limit=100, record_limit=100)
        store = CiRequests(self.project.root, policy)
        target = self.receipt["publication_id"]
        legacy_identity = request_id(target, OTHER_SHA, ["ci"])
        mkdir(store.root)
        write_json(store.root / f"{legacy_identity}.dispatch.json", {
            "request_id": legacy_identity, "publication_id": target,
            "sha": OTHER_SHA, "workflows": ["ci"],
            "dispatched_at": "2026-09-01T00:00:00+00:00"})
        for number in range(20):
            other = f"{number:064x}"
            if other == target:
                continue
            identity = request_id(other, SHA, ["ci"])
            store.mark_dispatch(identity, {
                "request_id": identity, "publication_id": other, "sha": SHA,
                "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        self.assertTrue((store.root / ci_requests.SEEK_DIR).is_dir())
        self.assertEqual(store._seek_path(target).parent.name, ci_requests.SEEK_DIR)
        ledger_names = sorted(path.name for path in store.root.iterdir())
        self.assertIn(ci_requests.SEEK_DIR, ledger_names)
        self.assertFalse(any(name.startswith("seek-") for name in ledger_names))
        self.assertFalse(store._seek_certifiable(target))
        fresh = request_id(target, SHA, ["ci"])
        store.mark_dispatch(fresh, {
            "request_id": fresh, "publication_id": target, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-21T00:00:00+00:00"})
        self.assertFalse(store._seek_path(target).exists())
        found = store.matching([target], not_before=dt.date(2026, 9, 1))
        self.assertIn(legacy_identity, found["items"])
        self.assertIn(fresh, found["items"])
        rows, _ = store._rows(strict=True)
        self.assertIn(legacy_identity, {row["request_id"] for row in rows})

    def test_malformed_index_does_not_force_indexed_pages_onto_capped_scan(self):
        policy = dataclasses.replace(self.config.ci_requests,
                                     name_processing_limit=100, record_limit=5)
        store = CiRequests(self.project.root, policy)
        good, bad, legacy_pub = self.receipt["publication_id"], "f" * 64, "e" * 64
        good_identity = request_id(good, SHA, ["ci"])
        store.mark_dispatch(good_identity, {
            "request_id": good_identity, "publication_id": good, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        bad_identity = request_id(bad, OTHER_SHA, ["ci"])
        store.mark_dispatch(bad_identity, {
            "request_id": bad_identity, "publication_id": bad, "sha": OTHER_SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        store._seek_path(bad).write_text("{not-json", encoding="utf-8")
        for number in range(6):
            sha = f"{number:040x}"
            identity = request_id(legacy_pub, sha, ["ci"])
            write_json(store.root / f"{identity}.dispatch.json", {
                "request_id": identity, "publication_id": legacy_pub, "sha": sha,
                "workflows": ["ci"], "dispatched_at": "2020-01-01T00:00:00+00:00"})
        self.assertTrue(store._rows()[1])
        window = dt.date(2026, 10, 1)
        with patch.object(store, "_rows", side_effect=AssertionError("shared scan used")):
            good_only = store.matching_many({"good": ([good], window)})["good"]
        self.assertEqual(good_only, {"items": [good_identity], "incomplete": False})
        pages = store.matching_many({
            "good": ([good], window),
            "bad": ([bad], window),
            "mixed": ([good, legacy_pub], window)})
        self.assertEqual(pages["good"], {"items": [good_identity], "incomplete": False})
        self.assertTrue(pages["bad"]["incomplete"])
        self.assertIn(good_identity, pages["mixed"]["items"])
        self.assertTrue(pages["mixed"]["incomplete"])

    def test_legacy_publication_without_index_stays_on_the_conservative_scan(self):
        policy = dataclasses.replace(self.config.ci_requests, record_limit=1)
        store = CiRequests(self.project.root, policy)
        publication = self.receipt["publication_id"]
        mkdir(store.root)
        for sha in (SHA, OTHER_SHA):
            identity = request_id(publication, sha, ["ci"])
            write_json(store.root / f"{identity}.dispatch.json", {
                "request_id": identity, "publication_id": publication, "sha": sha,
                "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        self.assertFalse(store._seek_path(publication).exists())
        with patch.object(store, "_rows", wraps=store._rows) as scan:
            found = store.matching([publication], not_before=dt.date(2026, 10, 1))
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(set(scan.call_args.kwargs), {"dropped"})
        self.assertTrue(found["incomplete"])
        third = request_id(publication, "c" * 40, ["ci"])
        store.mark_dispatch(third, {
            "request_id": third, "publication_id": publication, "sha": "c" * 40,
            "workflows": ["ci"], "dispatched_at": "2026-10-21T00:00:00+00:00"})
        self.assertFalse(store._seek_path(publication).exists())

    def test_quiet_certification_accepts_a_never_created_ledger(self):
        store = CiRequests(self.project.root / "never-dispatched", self.config.ci_requests)
        self.assertFalse(store.root.exists())
        self.assertEqual(store.quiet_activity(), store.activity())

    def test_idle_ledger_cleans_superseded_flat_seek_for_quiet_certification(self):
        store, identity, marker = self.pending_marker()
        no_spend = {key: value for key, value in marker.items() if key != "dispatched_at"}
        no_spend.update(outcome="not_dispatched", reason_code="provider_rejected",
                        runs=[], requested_at="2026-10-21T00:00:00+00:00")
        store.record_outcome(identity, no_spend)
        for path in (store.root / "seek").glob("*.json"):
            path.unlink()
        publication = self.receipt["publication_id"]
        legacy_path = store.root / f"seek-{publication}.json"
        write_json(legacy_path, {"version": 1, "publication_id": publication,
                                 "entries": [{"request_id": identity,
                                              "date": "2026-10-20"}]})
        first = store.quiet_activity()
        self.assertIsNotNone(first)
        self.assertFalse(legacy_path.exists())
        self.assertIsNotNone(store.quiet_activity())

    def test_superseded_flat_seek_index_is_removed_before_new_seek(self):
        store = CiRequests(self.project.root, self.config.ci_requests)
        old_publication = self.receipt["publication_id"]
        old_identity = request_id(old_publication, SHA, ["ci"])
        mkdir(store.root)
        write_json(store.root / f"{old_identity}.dispatch.json", {
            "request_id": old_identity, "publication_id": old_publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        legacy_path = store.root / f"seek-{old_publication}.json"
        write_json(legacy_path, {"version": 1, "publication_id": old_publication,
                                 "entries": [{"request_id": old_identity,
                                              "date": "2026-10-20"}]})
        fresh_publication = "f" * 64
        fresh = request_id(fresh_publication, SHA, ["ci"])
        store.mark_dispatch(fresh, {
            "request_id": fresh, "publication_id": fresh_publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-21T00:00:00+00:00"})
        self.assertFalse(legacy_path.exists())
        self.assertTrue(store._seek_path(fresh_publication).exists())
        rows, stopped = store._rows(strict=True)
        self.assertFalse(stopped)
        self.assertIn(old_identity, {row["request_id"] for row in rows})
        self.assertEqual(store.matching([fresh_publication],
                                        not_before=dt.date(2026, 10, 1)),
                         {"items": [fresh], "incomplete": False})

    def test_dispatch_does_not_proceed_when_its_index_cannot_be_written(self):
        store = CiRequests(self.project.root, self.config.ci_requests)
        publication = self.receipt["publication_id"]
        first = request_id(publication, SHA, ["ci"])
        store.mark_dispatch(first, {
            "request_id": first, "publication_id": publication, "sha": SHA,
            "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        index = read_json(store._seek_path(publication))
        second = request_id(publication, SHA, ["ci"], 2)
        marker = {"request_id": second, "publication_id": publication, "sha": SHA,
                  "workflows": ["ci"], "dispatched_at": "2026-10-21T00:00:00+00:00",
                  "attempt": 2, "possibly_duplicate_spend": True}
        original = ci_requests.write_json

        def fail_index(path, value, **kwargs):
            if path == store._seek_path(publication):
                raise OSError("index is not writable")
            return original(path, value, **kwargs)

        with patch("mizu.ci_requests.write_json", fail_index):
            with self.assertRaises(OSError):
                store.mark_dispatch(second, marker)
        self.assertIsNone(store.dispatched(second))
        self.assertEqual(read_json(store._seek_path(publication)), index)

    def test_matching_scans_newest_first_and_stops_at_the_window(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        other = "f" * 64
        rows = [
            {"date": "2026-09-30", "request_id": "older-match", "publication_id": publication_id},
            {"date": "2026-10-01", "request_id": "window-match", "publication_id": publication_id},
            {"date": "2026-10-02", "request_id": "newer-match", "publication_id": publication_id},
            {"date": "2026-10-03", "request_id": "newer-other", "publication_id": other},
        ]
        with patch.object(store, "_rows", return_value=(rows, False)):
            found = store.matching([publication_id], not_before=dt.date(2026, 10, 1))
        self.assertEqual(found, {"items": ["newer-match", "window-match"],
                                 "incomplete": False})

    def test_matching_many_filters_distinct_windows_in_one_scan(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        other = "f" * 64
        rows = [
            {"date": "2026-09-30", "request_id": "older-match", "publication_id": publication_id},
            {"date": "2026-10-01", "request_id": "window-match", "publication_id": publication_id},
            {"date": "2026-10-02", "request_id": "other-match", "publication_id": other},
            {"date": "2026-10-03", "request_id": "newer-other", "publication_id": other},
        ]
        queries = {"head": ([publication_id], dt.date(2026, 10, 1)),
                   "later": ([other], dt.date(2026, 10, 3))}
        with patch.object(store, "_rows", return_value=(rows, False)) as scan:
            pages = store.matching_many(queries)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(pages, {
            "head": {"items": ["window-match"], "incomplete": False},
            "later": {"items": ["newer-other"], "incomplete": False}})
        with patch.object(store, "_rows", side_effect=OSError("unreadable")):
            self.assertEqual(store.matching_many(queries), {
                key: {"items": [], "incomplete": True} for key in queries})

    def test_matching_fails_closed_on_stopped_or_unreadable_scan(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        rows = [{"date": "2026-10-02", "request_id": "match",
                 "publication_id": publication_id}]
        with patch.object(store, "_rows", return_value=(rows, True)):
            self.assertEqual(store.matching([publication_id], not_before=dt.date(2026, 10, 1)),
                             {"items": ["match"], "incomplete": True})
        with patch.object(store, "_rows", side_effect=OSError("unreadable")):
            self.assertEqual(store.matching([publication_id], not_before=dt.date(2026, 10, 1)),
                             {"items": [], "incomplete": True})
        with self.assertRaises(Denied):
            store.matching([publication_id], not_before=dt.datetime(2026, 10, 1))
        # A filter value that cannot name an index is not malformed evidence.
        opaque = "not-a-publication-id"
        with patch.object(store, "_rows", return_value=(
                [{"date": "2026-10-02", "request_id": "match", "publication_id": opaque}],
                False)):
            self.assertEqual(store.matching([opaque], not_before=dt.date(2026, 10, 1)),
                             {"items": ["match"], "incomplete": False})

    def test_matching_distinguishes_missing_ledger_from_unreadable_path(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        window = dt.date(2026, 10, 1)
        missing = CiRequests(self.project.root / "absent-project")
        self.assertEqual(missing.matching([publication_id], not_before=window),
                         {"items": [], "incomplete": False})
        mkdir(store.root.parent)
        if store.root.exists():
            shutil.rmtree(store.root)
        store.root.write_text("not a directory")
        self.assertEqual(store.matching([publication_id], not_before=window),
                         {"items": [], "incomplete": True})

    def test_matching_fails_closed_for_unreadable_dispatch_marker(self):
        store = CiRequests(self.project.root)
        publication_id = self.receipt["publication_id"]
        identity = request_id(publication_id, SHA, ["ci"])
        mkdir(store.root)
        store._path(identity, "dispatch").write_text("{not-valid-json")
        found = store.matching([publication_id], not_before=dt.date(2026, 10, 1))
        self.assertEqual(found, {"items": [], "incomplete": True})

    def test_persisted_covered_revisions_require_positive_integer(self):
        publication_id = self.receipt["publication_id"]
        timestamp = "2026-10-20T23:59:00+00:00"
        for revision, accepted in ((1, True), (0, False)):
            with self.subTest(revision=revision):
                identity = request_id(publication_id, SHA, ["ci"])
                covered = [{"id": "wi_" + "1" * 32, "rev": revision}]
                outcome = {
                    "request_id": identity, "publication_id": publication_id,
                    "sha": SHA, "workflows": ["ci"], "requested_at": timestamp,
                    "runs": runs()["runs"], "covered": covered,
                }
                marker = {"date": "2026-10-20", "publication_id": publication_id,
                          "sha": SHA, "workflows": ["ci"]}
                summary = _summary(outcome, identity, marker)
                self.assertEqual(summary is not None, accepted)

                dispatch = {
                    "request_id": identity, "publication_id": publication_id,
                    "sha": SHA, "workflows": ["ci"], "dispatched_at": timestamp,
                    "covered": covered,
                }
                unresolved = _unresolved_summary(dispatch, identity)
                self.assertEqual(unresolved is not None, accepted)

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
        # dispatch marker shows the adapter attempt began, so a CI run may
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
        with patch("mizu.context_vcs_request_tools.now", clock), \
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

    def test_daily_uses_utc_dispatch_day_in_both_resolution_states(self):
        store = CiRequests(self.project.root)
        publication = self.receipt["publication_id"]
        identity = request_id(publication, SHA, ["ci"])
        marker = {"request_id": identity, "publication_id": publication,
                  "sha": SHA, "workflows": ["ci"],
                  "dispatched_at": "2026-10-21T00:30:00+09:00"}
        store.mark_dispatch(identity, marker)
        before = store.daily()
        self.assertEqual(before["days"][0]["date"], "2026-10-20")
        self.assertEqual(before["days"][0]["items"][0]["state"], "unresolved")
        store.record_outcome(identity, {
            "request_id": identity, "publication_id": publication,
            "sha": SHA, "workflows": ["ci"], "runs": runs()["runs"],
            "requested_at": "2026-10-22T00:00:00+00:00"})
        after = store.daily()
        self.assertEqual(after["days"][0]["date"], "2026-10-20")
        self.assertEqual(after["days"][0]["requests"], 1)
        self.assertEqual(after["days"][0]["items"][0]["state"], "resolved")

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
        # The dispatch marker still proves an adapter attempt began and may
        # have spent a run, so it is listed as unresolved rather than hidden.
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
        path.write_text(json.dumps({"request_id": identity, "pad": "x" * (RECORD_BYTES + 1)}),
                        encoding="utf-8")
        # The oversized outcome is never returned unvalidated.
        with self.assertRaises(LimitExceeded):
            store.outcome(identity)
        # The dispatch marker still proves an adapter attempt began and may
        # have spent a run, so it is listed as unresolved rather than hidden.
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

    def test_an_undatable_dispatch_marker_does_not_hide_valid_requests(self):
        # A timezone-aware instant at the edge of the representable range can
        # parse yet have no representable UTC date. Such a marker is invalid
        # and is skipped, without denying the valid requests beside it and
        # without aborting either listing path.
        store = CiRequests(self.project.root)
        publication = "b" * 64
        for sha, timestamp in (
                ("a" * 40, "0001-01-01T00:00:00+01:00"),
                ("c" * 40, "2026-10-20T00:00:00+00:00")):
            identity = request_id(publication, sha, ["ci"])
            store.mark_dispatch(identity, {
                "request_id": identity, "publication_id": publication,
                "sha": sha, "workflows": ["ci"], "dispatched_at": timestamp})
        undatable = request_id(publication, "a" * 40, ["ci"])
        self.assertIsNone(_unresolved_summary(store.dispatched(undatable), undatable))
        daily = store.daily()
        self.assertEqual(daily["total"], 1)
        self.assertEqual(daily["days"][0]["date"], "2026-10-20")
        self.assertEqual(store.page()["total"], 1)

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
