"""Bounded CI head facts projected by the operator-owned observation driver."""
import dataclasses
import datetime as dt
import importlib.util
import itertools
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import head_state_observation, prompt_sections, queue_facts
from mizu.context_vcs_read_tools import _RECORD_ENVELOPE_KEYS
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.ci_requests import CiRequests, request_id
from mizu.distribution import public_source_identity
from mizu.fs import mkdir, write_json
from mizu.publication import Publication
from mizu.unit_prompts import prompt_delta_for, prompt_for

DRIVER = (Path(__file__).resolve().parents[1] / "examples" / "development"
          / "drivers" / "ci_head_state_driver.py")
_SPEC = importlib.util.spec_from_file_location("ci_head_state_driver", DRIVER)
driver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(driver)

SHA = "a" * 40
OLDER = "b" * 40
LATER = "c" * 40
SOURCE = "d" * 40
KEYS = {"subject", "revision", "runs", "requests", "no_head", "no_evidence",
        "may_be_incomplete"}


class CiHeadStateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "main"})
        self.project.config = self.config
        self.ticks = itertools.count()
        self.base_time = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)

    def receipt(self, sha, *, branch="main", code_digest="e" * 64):
        stamp = (self.base_time + dt.timedelta(seconds=next(self.ticks))).isoformat()
        with patch("mizu.publication.now", return_value=stamp):
            return Publication.record_external(
                self.project.root, branch=branch, published_sha=sha,
                code_digest=code_digest, approval="approval", source_commit=SOURCE,
                projection=public_source_identity({}))

    def request(self, sha, receipt, *, dispatched_at="2026-10-20T00:00:00+00:00"):
        identity = request_id(receipt["publication_id"], sha, ["ci"])
        CiRequests(self.project.root, self.config.ci_requests).mark_dispatch(identity, {
            "request_id": identity, "publication_id": receipt["publication_id"],
            "sha": sha, "workflows": ["ci"], "dispatched_at": dispatched_at})
        return identity

    def inject_ref(self, sha, branch="main"):
        root = self.project.root / "upstream-refs"
        mkdir(root)
        (root / branch).write_text(sha + "\n")

    def cache(self, *, failures=None, successes=None):
        write_json(self.project.root / "vcs-poll.json", {
            "failures": failures or {}, "successes": successes or {}})

    def facts(self, config=None):
        request = head_state_observation.head_state_request(self.project, config or self.config)
        return driver.observed_head_state(request)["branches"]

    def observed(self, config, project, name, request):
        """Stand in for the generic host seam, invoking the real driver once."""
        self.assertEqual(name, "ci_head_state")
        self.assertEqual(set(request), {"branches"})
        self.assertTrue(all(set(row) == {
            "subject", "head", "head_unreadable", "head_page_scan_stopped",
            "exact_page", "recordings", "runs", "cache_incomplete",
            "branch_incomplete", "requests"} for row in request["branches"]))
        self.calls.append(request)
        return {"appended": 1, "observations": [{
            "driver": name, "kind": name, "summary": "observed",
            "observed_at": "2026-10-01T00:00:00+00:00",
            "detail": canonical(driver.observed_head_state(request)).decode()}]}

    def facts_through_host(self, config=None):
        self.calls = []
        with patch("mizu.host_drivers.observe", side_effect=self.observed):
            rows = head_state_observation.ci_head_state_facts(
                self.project, config or self.config)
        self.assertEqual(len(self.calls), 1)
        return rows

    def two_branches(self):
        return dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "main",
            "ci_wake_extra_branches": ["release"]})

    def matching_page(self, items, *, incomplete=False):
        return {"items": items, "incomplete": incomplete}

    def context(self, *, vcs_request=True, **role_overrides):
        role = self.config.roles["worker"]
        caps = list(role.capabilities)
        if vcs_request and "vcs_request" not in caps:
            caps.append("vcs_request")
        if not vcs_request:
            caps = [cap for cap in caps if cap != "vcs_request"]
        role = dataclasses.replace(role, capabilities=tuple(caps), **role_overrides)
        config = dataclasses.replace(self.config, roles={**self.config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        return config, role, config.roles["worker"], run, self.project.snapshots.get()

    def opening(self, *, vcs_request=True, builder=prompt_for, **role_overrides):
        from mizu.runtime import Context
        config, role, _, run, snap = self.context(vcs_request=vcs_request, **role_overrides)
        workspace = self.project.workspace
        if role.workspace != "write":
            workspace = run / "input"
            self.project.snapshots.materialize(snap, workspace)
        return json.loads(builder(Context(config, self.project, role, run, snap, workspace)))

    def test_no_publication_or_ref_is_no_head(self):
        self.assertEqual(self.facts(), [driver._row("main", "", no_head=True)])
        self.assertEqual(set(self.facts()[0]), KEYS)
        self.assertFalse(self.facts()[0]["no_evidence"])
        self.assertFalse(self.facts()[0]["may_be_incomplete"])

    def test_head_without_runs_or_requests_is_explicit_no_evidence(self):
        self.receipt(SHA)
        self.assertEqual(self.facts(), [driver._row("main", SHA)])

    def test_exact_head_request_is_an_opaque_request_identity(self):
        receipt = self.receipt(SHA)
        identity = self.request(SHA, receipt)
        self.assertEqual(self.facts(), [driver._row(
            "main", SHA, requests=[identity])])

    def test_runs_come_from_both_cache_maps_at_the_exact_head(self):
        self.receipt(SHA)
        self.cache(failures={"main": [{"sha": SHA, "check": "red", "run": "run-red"}]},
                   successes={"main": [{"sha": SHA, "check": "green", "run": "run-green"},
                                       {"sha": OLDER, "check": "other", "run": "run-old"}]})
        self.assertEqual(self.facts(), [driver._row(
            "main", SHA, runs=["run-red", "run-green"])])

    def test_injected_ref_is_the_exact_head_and_older_evidence_does_not_transfer(self):
        receipt = self.receipt(SHA)
        self.request(SHA, receipt)
        self.cache(successes={"main": [{"sha": SHA, "check": "unit", "run": "run-1"}]})
        self.inject_ref(OLDER)
        self.assertEqual(self.facts(), [driver._row("main", OLDER)])

    def test_later_receipt_and_request_do_not_cover_an_older_head(self):
        self.receipt(OLDER)
        later = self.receipt(LATER)
        self.request(LATER, later)
        self.cache(successes={"main": [{"sha": LATER, "check": "unit", "run": "run-later"}]})
        self.inject_ref(OLDER)
        self.assertEqual(self.facts(), [driver._row("main", OLDER)])

    def test_request_from_an_unreceipted_publication_is_not_attributed(self):
        self.receipt(SHA)
        self.request(SHA, {"publication_id": "f" * 64})
        self.assertEqual(self.facts(), [driver._row("main", SHA)])

    def test_reviewer_repro_a_older_release_head_does_not_inherit_main_request(self):
        self.receipt(OLDER, branch="release", code_digest="1" * 64)
        self.receipt(LATER, branch="release", code_digest="2" * 64)
        main_receipt = self.receipt(LATER, branch="main", code_digest="3" * 64)
        self.inject_ref(OLDER, branch="release")
        self.request(LATER, main_receipt)
        self.cache(failures={"release": [{"sha": LATER, "check": "unit",
                                          "run": "run-later"}]})
        rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(rows["release"], driver._row("release", OLDER))
        self.assertEqual(rows["main"], driver._row(
            "main", LATER,
            requests=[request_id(main_receipt["publication_id"], LATER, ["ci"])]))

    def test_reviewer_repro_b_identical_sha_attributes_request_by_publication(self):
        main_receipt = self.receipt(SHA, branch="main", code_digest="1" * 64)
        self.receipt(SHA, branch="release", code_digest="2" * 64)
        self.request(SHA, main_receipt)
        rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(rows["main"], driver._row(
            "main", SHA,
            requests=[request_id(main_receipt["publication_id"], SHA, ["ci"])]))
        self.assertEqual(rows["release"], driver._row("release", SHA))

    def test_publication_shared_by_two_branches_may_appear_on_both(self):
        shared = "9" * 64
        identity = request_id(shared, SHA, ["ci"])

        def receipts(project_root, *, branch, limit, published_sha=None):
            return {"publications": [{"publication_id": shared, "published_sha": SHA}],
                    "total": 1, "truncated": False, "scan_stopped": False,
                    "next_cursor": None}

        page = self.matching_page([identity])
        with patch("mizu.head_state_observation.Publication.list_receipts", side_effect=receipts), \
                patch("mizu.head_state_observation.Publication.read_receipt",
                      return_value={"recorded_at": "2026-10-01T00:00:00+00:00"}), \
                patch("mizu.head_state_observation.CiRequests.matching_many",
                      return_value={"main": page, "release": page}):
            rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        expected = driver._row("", SHA, requests=[identity])
        for branch in ("main", "release"):
            self.assertEqual(rows[branch], {**expected, "subject": branch})

    def test_two_configured_branches_use_seek_indexes_without_a_scan(self):
        main_receipt = self.receipt(SHA, branch="main", code_digest="1" * 64)
        release_receipt = self.receipt(SHA, branch="release", code_digest="2" * 64)
        main_request = self.request(SHA, main_receipt)
        release_request = self.request(SHA, release_receipt)
        with patch.object(CiRequests, "_rows", side_effect=AssertionError("shared scan used")):
            rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(rows["main"], driver._row("main", SHA, requests=[main_request]))
        self.assertEqual(rows["release"], driver._row(
            "release", SHA, requests=[release_request]))

    def test_two_legacy_publications_share_one_conservative_request_scan(self):
        main_receipt = self.receipt(SHA, branch="main", code_digest="1" * 64)
        release_receipt = self.receipt(SHA, branch="release", code_digest="2" * 64)
        main_request = self.request(SHA, main_receipt)
        release_request = self.request(SHA, release_receipt)
        ledger = self.project.root / "evidence" / "ci-requests"
        seek_dir = ledger / "seek"
        if seek_dir.is_dir():
            for path in seek_dir.glob("*.json"):
                path.unlink()
        real_rows = CiRequests._rows
        with patch.object(CiRequests, "_rows", autospec=True,
                          side_effect=lambda store, **kwargs: real_rows(store, **kwargs)) as scan:
            rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(set(scan.call_args.kwargs), {"dropped"})
        self.assertEqual(rows["main"], driver._row("main", SHA, requests=[main_request]))
        self.assertEqual(rows["release"], driver._row(
            "release", SHA, requests=[release_request]))

    def test_one_bounded_row_per_configured_branch_sorted(self):
        self.receipt(SHA)
        self.receipt(OLDER, branch="release")
        config = dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "release",
            "ci_wake_extra_branches": ["main", "main"]})
        rows = self.facts(config)
        self.assertEqual([row["subject"] for row in rows], ["main", "release"])
        self.assertTrue(all(set(row) == KEYS for row in rows))
        self.assertTrue(all(isinstance(row["runs"], list)
                            and all(isinstance(value, str) for value in row["runs"])
                            and isinstance(row["requests"], list)
                            and all(isinstance(value, str) for value in row["requests"])
                            for row in rows))

    def test_identity_arrays_are_capped_at_eight_and_flagged(self):
        self.receipt(SHA)
        rows = [f"request-{index}" for index in range(10)]
        self.cache(successes={"main": [{"sha": SHA, "check": f"check-{index}",
                                        "run": f"run-{index}"} for index in range(10)]})
        with patch("mizu.head_state_observation.CiRequests.matching_many",
                   return_value={"main": self.matching_page(rows)}):
            row = self.facts()[0]
        self.assertEqual(row["runs"], [f"run-{index}" for index in range(8)])
        self.assertEqual(row["requests"], [f"request-{index}" for index in range(8)])
        self.assertFalse(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

    def test_unreadable_or_truncated_evidence_is_incomplete_not_success(self):
        head = self.receipt(SHA)
        write_json(self.project.root / "vcs-poll.json", "[")
        row = self.facts()[0]
        self.assertEqual(row["revision"], SHA)
        self.assertTrue(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

        self.cache()
        with patch("mizu.ci_requests.CiRequests._rows", return_value=([], True)):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])
        self.inject_ref(SHA)
        with patch("mizu.head_state_observation.read_ref", side_effect=OSError("unreadable")):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])
        with patch("mizu.head_state_observation.Publication.list_receipts", return_value={
                "publications": [{"publication_id": head["publication_id"],
                                  "published_sha": SHA}],
                "total": 2, "truncated": True, "scan_stopped": False}):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])

    def test_large_history_reports_the_new_head_without_incompleteness(self):
        older = [self.receipt(OLDER, code_digest=f"{index:064x}") for index in range(101)]
        for receipt in older:
            self.request(OLDER, receipt)
        head = self.receipt(SHA)
        row = self.facts()[0]
        self.assertEqual(row, driver._row("main", SHA))
        self.assertTrue(row["no_evidence"])
        self.assertFalse(row["may_be_incomplete"])

        identity = self.request(SHA, head, dispatched_at="2026-10-01T12:00:00+00:00")
        row = self.facts()[0]
        self.assertEqual(row, driver._row("main", SHA, requests=[identity]))
        self.assertFalse(row["may_be_incomplete"])

    def test_long_history_before_the_head_window_still_reports_the_head_request(self):
        self.base_time = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        older = [self.receipt(OLDER, code_digest=f"{index:064x}") for index in range(101)]
        for receipt in older:
            self.request(OLDER, receipt, dispatched_at="2026-09-02T00:00:00+00:00")
        self.base_time = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        head = self.receipt(SHA)
        identity = self.request(SHA, head, dispatched_at="2026-10-02T00:00:00+00:00")
        row = self.facts()[0]
        self.assertEqual(row, driver._row("main", SHA, requests=[identity]))
        self.assertFalse(row["no_evidence"])
        self.assertFalse(row["may_be_incomplete"])

    def test_fresh_head_with_long_history_passes_exact_instant_and_skips_scan(self):
        self.base_time = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        older = [self.receipt(OLDER, code_digest=f"{index:064x}") for index in range(101)]
        for receipt in older:
            self.request(OLDER, receipt, dispatched_at="2026-09-02T00:00:00+00:00")
        self.base_time = dt.datetime(2036, 10, 1, tzinfo=dt.timezone.utc)
        head = self.receipt(SHA)
        recorded = dt.datetime.fromisoformat(head["recorded_at"])
        observed = {}
        real_matching = CiRequests.matching_many

        def matching(store, queries):
            observed["query"] = queries["main"]
            return real_matching(store, queries)

        with patch.object(CiRequests, "matching_many", matching), \
                patch.object(CiRequests, "_rows", side_effect=AssertionError("shared scan used")):
            rows = self.facts()
        self.assertEqual(rows, [driver._row("main", SHA)])
        self.assertEqual(observed["query"][0], [head["publication_id"]])
        self.assertIsInstance(observed["query"][1], dt.datetime)
        self.assertEqual(observed["query"][1], recorded)

    def test_stopped_or_unreadable_head_request_scan_is_incomplete(self):
        self.receipt(SHA)
        with patch("mizu.ci_requests.CiRequests._rows", return_value=([], True)):
            row = self.facts()[0]
        self.assertTrue(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])
        with patch("mizu.ci_requests.CiRequests._rows", side_effect=OSError("unreadable")):
            row = self.facts()[0]
        self.assertTrue(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

    def test_unreadable_head_dispatch_marker_is_incomplete(self):
        head = self.receipt(SHA)
        identity = self.request(SHA, head)
        CiRequests(self.project.root)._path(identity, "dispatch").write_text("{not-valid-json")
        row = self.facts()[0]
        self.assertTrue(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

    def test_invalid_run_identity_is_incomplete_and_not_a_run(self):
        self.receipt(SHA)
        self.cache(failures={"main": [{"sha": SHA, "check": "unit", "run": "bad\nrun"}]})
        self.assertEqual(self.facts(), [driver._row(
            "main", SHA, may_be_incomplete=True)])

    def test_role_gating_in_full_and_delta_prompts(self):
        self.receipt(SHA)
        self.calls = []
        with patch("mizu.host_drivers.observe", side_effect=self.observed):
            for builder in (prompt_for, prompt_delta_for):
                with self.subTest(builder=builder.__name__):
                    self.assertEqual(self.opening(vcs_request=False, builder=builder)["ci_head_state"], [])
                    offered = self.opening(vcs_request=True, builder=builder)["ci_head_state"]
                    self.assertEqual(offered, [driver._row("main", SHA)])
                    self.assertEqual(
                        self.opening(vcs_request=True, builder=builder,
                                     context_exclude=("ci",))["ci_head_state"], offered)
                    self.assertEqual(
                        self.opening(vcs_request=True, builder=builder,
                                     context_include=("ci",))["ci_head_state"], offered)
        self.assertEqual(len(self.calls), 6)
        self.assertIn("ci_head_state", prompt_sections.FIXED_KEYS)

    def test_facts_are_a_read_only_projection(self):
        self.receipt(SHA)
        def evidence_files():
            return {str(path.relative_to(self.project.root))
                    for path in (self.project.root / "evidence").rglob("*")
                    if path.is_file()} | {
                path.name for path in self.project.root.glob("vcs-*.json")}

        before = evidence_files()
        with patch("mizu.vcs.invoke") as invoke, \
                patch("mizu.vcs.request_runs_via") as dispatch, \
                patch("mizu.ci_requests.CiRequests.mark_dispatch") as mark:
            self.facts()
            self.calls = []
            with patch("mizu.host_drivers.observe", side_effect=self.observed):
                self.opening()
            invoke.assert_not_called()
            dispatch.assert_not_called()
            mark.assert_not_called()
        self.assertEqual(before, evidence_files())

    def test_host_caller_passes_only_raw_evidence_and_returns_driver_rows(self):
        receipt = self.receipt(SHA)
        identity = self.request(SHA, receipt)
        self.cache(successes={"main": [{"sha": SHA, "check": "unit", "run": "run-1"}]})
        self.assertEqual(self.facts_through_host(), [driver._row(
            "main", SHA, runs=["run-1"], requests=[identity])])
        raw = self.calls[0]["branches"][0]
        self.assertEqual(set(raw), {
            "subject", "head", "head_unreadable", "head_page_scan_stopped",
            "exact_page", "recordings", "runs", "cache_incomplete",
            "branch_incomplete", "requests"})
        self.assertFalse({"revision", "no_head", "no_evidence",
                          "may_be_incomplete"} & set(raw))
        self.assertEqual((raw["subject"], raw["head"], raw["runs"]),
                         ("main", SHA, ["run-1"]))
        self.assertEqual(raw["requests"]["items"], [identity])
        self.assertEqual(raw["exact_page"]["pairs"][0][1], SHA)
        self.assertEqual(raw["recordings"][0][0], receipt["publication_id"])
        self.assertIsInstance(raw["recordings"][0][1], str)

    def test_driver_sorts_reversed_raw_branches_without_recomputing_them(self):
        self.receipt(SHA)
        self.receipt(OLDER, branch="release")
        request = head_state_observation.head_state_request(self.project, self.two_branches())
        request["branches"].reverse()
        rows = driver.observed_head_state(request)["branches"]
        self.assertEqual([row["subject"] for row in rows], ["main", "release"])
        self.assertEqual(rows[0]["revision"], SHA)
        self.assertEqual(rows[1]["revision"], OLDER)
        self.assertTrue(all(set(row) == KEYS for row in rows))

    def test_driver_rejects_malformed_raw_evidence(self):
        self.receipt(SHA)
        request = head_state_observation.head_state_request(self.project, self.config)
        bad_requests = [
            None, {}, {"branches": []}, {"branches": [{}]},
            {**request, "extra": 1},
            {**request, "branches": request["branches"] * 2},
        ]
        broken = json.loads(canonical(request))
        broken["branches"][0]["exact_page"]["pairs"][0][0] = "bad\nid"
        bad_requests.append(broken)
        broken = json.loads(canonical(request))
        broken["branches"][0]["recordings"][0][1] = "not-a-instant"
        bad_requests.append(broken)
        broken = json.loads(canonical(request))
        broken["branches"][0]["requests"] = {"items": ["x"], "incomplete": "yes"}
        bad_requests.append(broken)
        broken = json.loads(canonical(request))
        broken["branches"][0]["runs"] = ["run\u200b"]
        bad_requests.append(broken)
        for bad in bad_requests:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                driver.observed_head_state(bad)

    def test_host_caller_fails_closed_on_refusal_and_reserved_or_invalid_detail(self):
        self.receipt(SHA)
        valid = canonical(driver.observed_head_state(
            head_state_observation.head_state_request(self.project, self.config))).decode()
        bad_details = [
            "[", "{}", '{"branches": []}', '{"branches": [], "op": "x"}',
            '{"branches": [], "trust": "external-untrusted"}',
            '{"branches": [], "evidence_id": "x"}',
            '{"branches": [], "evidence": "x"}',
            '{"branches": [], "result": {}}',
        ]
        for key in _RECORD_ENVELOPE_KEYS:
            row = driver._row("main", SHA)
            row[key] = "reserved"
            bad_details.append(canonical({"branches": [row]}).decode())
        for key in ("no_head", "no_evidence", "may_be_incomplete"):
            row = driver._row("main", SHA)
            row[key] = "yes"
            bad_details.append(canonical({"branches": [row]}).decode())
        row = driver._row("main", SHA)
        row["runs"] = ["run"] * 9
        bad_details.append(canonical({"branches": [row]}).decode())
        bad_details.append(canonical({"branches": [driver._row("other", SHA)]}).decode())
        bad_details.append(canonical({"branches": [driver._row("main", SHA)] * 2}).decode())
        bad_details.append(canonical({"branches": [driver._row("main", SHA)] * 9}).decode())
        with patch("mizu.host_drivers.observe", side_effect=Denied("undeclared")):
            self.assertEqual(head_state_observation.ci_head_state_facts(
                self.project, self.config), [])
        with patch("mizu.host_drivers.observe", side_effect=RuntimeError("failed")):
            self.assertEqual(head_state_observation.ci_head_state_facts(
                self.project, self.config), [])
        for detail in bad_details:
            with self.subTest(detail=detail):
                with patch("mizu.host_drivers.observe", return_value={
                        "observations": [{"detail": detail}]}):
                    self.assertEqual(head_state_observation.ci_head_state_facts(
                        self.project, self.config), [])
        for response in (None, {}, {"observations": []},
                         {"observations": [{"detail": {}}]},
                         {"observations": [{"detail": valid}] * 2}):
            with self.subTest(response=response):
                with patch("mizu.host_drivers.observe", return_value=response):
                    self.assertEqual(head_state_observation.ci_head_state_facts(
                        self.project, self.config), [])

    def test_every_configured_branch_is_projected_up_to_the_ceiling(self):
        receipt = self.receipt(SHA)
        identity = self.request(SHA, receipt)
        extras = [f"branch-{index}" for index in range(64)]
        config = dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "main",
            "ci_wake_extra_branches": extras})
        rows = self.facts_through_host(config)
        self.assertEqual(len(rows), 65)
        self.assertEqual([row["subject"] for row in rows], sorted(["main", *extras]))
        main = next(row for row in rows if row["subject"] == "main")
        self.assertEqual(main, driver._row("main", SHA, requests=[identity]))
        self.assertTrue(all(row["no_head"] for row in rows if row is not main))
        self.assertLessEqual(len(canonical({"branches": rows})), driver.DETAIL_MAX)

    def test_valid_run_identity_with_a_space_is_projected(self):
        self.receipt(SHA)
        self.cache(successes={"main": [
            {"sha": SHA, "check": "unit", "run": "run one"}]})
        self.assertEqual(self.facts_through_host(), [driver._row(
            "main", SHA, runs=["run one"])])

    def test_real_driver_envelope_covers_a_dense_maximum_branch_set(self):
        for character in ("x", "\u00e9", "\U0001f600"):
            with self.subTest(character=character):
                branches = []
                for index in range(driver.MAX_BRANCHES):
                    publication_id = (f"p{index:03d}" + "x" * 124)[:driver.MAX_TEXT]
                    head = f"{index + 1:040x}"
                    identity = lambda kind, slot: (
                        f"{kind}{index:03d}{slot:02d}" +
                        character * driver.MAX_TEXT)[:driver.MAX_TEXT]
                    branches.append({
                        "subject": f"branch-{index}", "head": head,
                        "head_unreadable": False, "head_page_scan_stopped": False,
                        "exact_page": {"pairs": [[publication_id, head]],
                                       "truncated": False, "scan_stopped": False},
                        "recordings": [[publication_id, "2026-10-01T00:00:00+00:00"]],
                        "runs": [identity("run", slot)
                                 for slot in range(driver.MAX_IDENTITIES)],
                        "requests": {"items": [identity("request", slot)
                                               for slot in range(driver.MAX_IDENTITIES)],
                                     "incomplete": False},
                        "cache_incomplete": False, "branch_incomplete": False})
                completed = subprocess.run(
                    [sys.executable, str(DRIVER)],
                    input=json.dumps({"branches": branches}),
                    capture_output=True, text=True, encoding="utf-8", check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                observation = json.loads(completed.stdout)["observations"][0]
                report = json.loads(observation["detail"])
                self.assertEqual(len(report["branches"]), driver.MAX_BRANCHES)
                self.assertTrue(all(
                    len(row["runs"]) == driver.MAX_IDENTITIES
                    and len(row["requests"]) == driver.MAX_IDENTITIES
                    for row in report["branches"]))
                self.assertLessEqual(
                    len(observation["detail"].encode("utf-8")), driver.DETAIL_MAX)

    def test_real_host_path_appends_dense_multibranch_detail(self):
        branches = ["main"] + [f"branch-{index}" for index in range(64)]
        config = dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "main",
            "ci_wake_extra_branches": branches[1:]})
        successes = {}
        for index, branch in enumerate(branches):
            sha = f"{index + 1:040x}"
            self.inject_ref(sha, branch)
            self.receipt(sha, branch=branch)
            successes[branch] = [{
                "sha": sha, "check": "unit",
                "run": f"{slot:02d}" + "\u00e9\U0001f600" * 63}
                for slot in range(driver.MAX_IDENTITIES)]
        self.cache(successes=successes)
        rows = head_state_observation.ci_head_state_facts(self.project, config)
        self.assertEqual(len(rows), len(branches))
        self.assertTrue(all(len(row["runs"]) == driver.MAX_IDENTITIES
                            for row in rows))
        store = self.project.kernel_queues
        self.assertTrue(store.due("ci_head_state_observations", "role:worker"))
        page = store.read("ci_head_state_observations", "role:worker")
        self.assertEqual(len(page["messages"]), 1)
        detail = json.loads(page["messages"][0]["body"]["detail"])
        self.assertEqual(len(detail["branches"]), len(branches))

    def test_prompt_consumes_the_observation_wake_edge_it_creates(self):
        from mizu.runtime import Context
        self.receipt(SHA)
        config, role, _, run, snap = self.context(vcs_request=True)
        context = Context(config, self.project, role, run, snap, self.project.workspace)
        context.queues = []
        prompt_for(context)  # real host seam: the driver appends one observation
        store = self.project.kernel_queues
        self.assertTrue(store.due("ci_head_state_observations", "role:worker"))
        self.assertEqual(context.queues[0]["queue"], "ci_head_state_observations")
        self.assertEqual(context.queues[0]["count"], 1)
        queue_facts.mark_woken(self.project, "worker", context.queues)
        self.assertFalse(store.due("ci_head_state_observations", "role:worker"))


if __name__ == "__main__":
    unittest.main()
