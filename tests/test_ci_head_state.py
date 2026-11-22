"""Bounded, read-only CI head evidence for roles that can request CI."""
import dataclasses
import datetime as dt
import itertools
import json
import unittest
import uuid
from unittest.mock import patch

from support import Fixture
from mizu import ci_head_state, prompt_sections
from mizu.ci_requests import CiRequests, request_id
from mizu.distribution import public_source_identity
from mizu.fs import mkdir, write_json
from mizu.publication import Publication
from mizu.unit_prompts import prompt_delta_for, prompt_for

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

    def request(self, sha, receipt):
        identity = request_id(receipt["publication_id"], sha, ["ci"])
        CiRequests(self.project.root, self.config.ci_requests).mark_dispatch(identity, {
            "request_id": identity, "publication_id": receipt["publication_id"],
            "sha": sha, "workflows": ["ci"],
            "dispatched_at": "2026-10-20T00:00:00+00:00"})
        return identity

    def inject_ref(self, sha, branch="main"):
        root = self.project.root / "upstream-refs"
        mkdir(root)
        (root / branch).write_text(sha + "\n")

    def cache(self, *, failures=None, successes=None):
        write_json(self.project.root / "vcs-poll.json", {
            "failures": failures or {}, "successes": successes or {}})

    def facts(self, config=None):
        return ci_head_state.ci_head_state_facts(self.project, config or self.config)

    def two_branches(self):
        return dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "main",
            "ci_wake_extra_branches": ["release"]})

    def daily_page(self, items, *, truncated=False):
        return {"days": [{"date": "2026-10-20", "requests": len(items),
                          "covered_items": 0, "items": items}],
                "total": len(items), "truncated": truncated, "scan_stopped": False}

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
        self.assertEqual(self.facts(), [ci_head_state._row("main", "", no_head=True)])
        self.assertEqual(set(self.facts()[0]), KEYS)
        self.assertFalse(self.facts()[0]["no_evidence"])
        self.assertFalse(self.facts()[0]["may_be_incomplete"])

    def test_head_without_runs_or_requests_is_explicit_no_evidence(self):
        self.receipt(SHA)
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA)])

    def test_exact_head_request_is_an_opaque_request_identity(self):
        receipt = self.receipt(SHA)
        identity = self.request(SHA, receipt)
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", SHA, requests=[identity])])

    def test_runs_come_from_both_cache_maps_at_the_exact_head(self):
        self.receipt(SHA)
        self.cache(failures={"main": [{"sha": SHA, "check": "red", "run": "run-red"}]},
                   successes={"main": [{"sha": SHA, "check": "green", "run": "run-green"},
                                       {"sha": OLDER, "check": "other", "run": "run-old"}]})
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", SHA, runs=["run-red", "run-green"])])

    def test_injected_ref_is_the_exact_head_and_older_evidence_does_not_transfer(self):
        receipt = self.receipt(SHA)
        self.request(SHA, receipt)
        self.cache(successes={"main": [{"sha": SHA, "check": "unit", "run": "run-1"}]})
        self.inject_ref(OLDER)
        self.assertEqual(self.facts(), [ci_head_state._row("main", OLDER)])

    def test_later_receipt_and_request_do_not_cover_an_older_head(self):
        self.receipt(OLDER)
        later = self.receipt(LATER)
        self.request(LATER, later)
        self.cache(successes={"main": [{"sha": LATER, "check": "unit", "run": "run-later"}]})
        self.inject_ref(OLDER)
        self.assertEqual(self.facts(), [ci_head_state._row("main", OLDER)])

    def test_request_from_an_unreceipted_publication_is_not_attributed(self):
        self.receipt(SHA)
        self.request(SHA, {"publication_id": "f" * 64})
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA)])

    def test_reviewer_repro_a_older_release_head_does_not_inherit_main_request(self):
        self.receipt(OLDER, branch="release", code_digest="1" * 64)
        self.receipt(LATER, branch="release", code_digest="2" * 64)
        main_receipt = self.receipt(LATER, branch="main", code_digest="3" * 64)
        self.inject_ref(OLDER, branch="release")
        self.request(LATER, main_receipt)
        self.cache(failures={"release": [{"sha": LATER, "check": "unit",
                                          "run": "run-later"}]})
        rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(rows["release"], ci_head_state._row("release", OLDER))
        self.assertEqual(rows["main"], ci_head_state._row(
            "main", LATER,
            requests=[request_id(main_receipt["publication_id"], LATER, ["ci"])]))

    def test_reviewer_repro_b_identical_sha_attributes_request_by_publication(self):
        main_receipt = self.receipt(SHA, branch="main", code_digest="1" * 64)
        self.receipt(SHA, branch="release", code_digest="2" * 64)
        self.request(SHA, main_receipt)
        rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        self.assertEqual(rows["main"], ci_head_state._row(
            "main", SHA,
            requests=[request_id(main_receipt["publication_id"], SHA, ["ci"])]))
        self.assertEqual(rows["release"], ci_head_state._row("release", SHA))

    def test_publication_shared_by_two_branches_may_appear_on_both(self):
        shared = "9" * 64
        identity = request_id(shared, SHA, ["ci"])

        def receipts(project_root, *, branch, limit):
            return {"publications": [{"publication_id": shared, "published_sha": SHA}],
                    "total": 1, "truncated": False, "scan_stopped": False,
                    "next_cursor": None}

        page = self.daily_page([{"request_id": identity, "publication_id": shared,
                                 "sha": SHA}])
        with patch("mizu.ci_head_state.Publication.list_receipts", side_effect=receipts), \
                patch("mizu.ci_head_state.CiRequests.daily", return_value=page):
            rows = {row["subject"]: row for row in self.facts(self.two_branches())}
        expected = ci_head_state._row("", SHA, requests=[identity])
        for branch in ("main", "release"):
            self.assertEqual(rows[branch], {**expected, "subject": branch})

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
        receipt = self.receipt(SHA)
        rows = [{"request_id": f"request-{index}",
                 "publication_id": receipt["publication_id"], "sha": SHA}
                for index in range(10)]
        self.cache(successes={"main": [{"sha": SHA, "check": f"check-{index}",
                                        "run": f"run-{index}"} for index in range(10)]})
        with patch("mizu.ci_head_state.CiRequests.daily",
                   return_value=self.daily_page(rows)):
            row = self.facts()[0]
        self.assertEqual(row["runs"], [f"run-{index}" for index in range(8)])
        self.assertEqual(row["requests"], [f"request-{index}" for index in range(8)])
        self.assertFalse(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

    def test_unreadable_or_truncated_evidence_is_incomplete_not_success(self):
        self.receipt(SHA)
        write_json(self.project.root / "vcs-poll.json", "[")
        row = self.facts()[0]
        self.assertEqual(row["revision"], SHA)
        self.assertTrue(row["no_evidence"])
        self.assertTrue(row["may_be_incomplete"])

        self.cache()
        with patch("mizu.ci_head_state.CiRequests.daily",
                   return_value=self.daily_page([], truncated=True)):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])
        self.inject_ref(SHA)
        with patch("mizu.ci_head_state.read_ref", side_effect=OSError("unreadable")):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])
        with patch("mizu.ci_head_state.Publication.list_receipts", return_value={
                "publications": [{"publication_id": "1" * 64, "published_sha": SHA}],
                "total": 2, "truncated": True, "scan_stopped": False}):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])

    def test_invalid_run_identity_is_incomplete_and_not_a_run(self):
        self.receipt(SHA)
        self.cache(failures={"main": [{"sha": SHA, "check": "unit", "run": "bad\nrun"}]})
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", SHA, may_be_incomplete=True)])

    def test_role_gating_in_full_and_delta_prompts(self):
        self.receipt(SHA)
        for builder in (prompt_for, prompt_delta_for):
            with self.subTest(builder=builder.__name__):
                self.assertEqual(self.opening(vcs_request=False, builder=builder)["ci_head_state"], [])
                offered = self.opening(vcs_request=True, builder=builder)["ci_head_state"]
                self.assertEqual(offered, [ci_head_state._row("main", SHA)])
                self.assertEqual(
                    self.opening(vcs_request=True, builder=builder,
                                 context_exclude=("ci",))["ci_head_state"], offered)
                self.assertEqual(
                    self.opening(vcs_request=True, builder=builder,
                                 context_include=("ci",))["ci_head_state"], offered)
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
            self.opening()
            invoke.assert_not_called()
            dispatch.assert_not_called()
            mark.assert_not_called()
        self.assertEqual(before, evidence_files())


if __name__ == "__main__":
    unittest.main()
