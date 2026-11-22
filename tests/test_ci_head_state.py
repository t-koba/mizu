"""Bounded, read-only CI head facts for roles that can request CI."""
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
KEYS = {"subject", "revision", "state", "covering_revision", "may_be_incomplete"}


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

    def inject_ref(self, sha, branch="main"):
        root = self.project.root / "upstream-refs"
        mkdir(root)
        (root / branch).write_text(sha + "\n")

    def cache(self, *, failures=None, successes=None):
        write_json(self.project.root / "vcs-poll.json", {
            "failures": failures or {}, "successes": successes or {}})

    def facts(self):
        return ci_head_state.ci_head_state_facts(self.project, self.config)

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
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", "", "no_head")])
        self.assertEqual(set(self.facts()[0]), KEYS)
        self.assertFalse(self.facts()[0]["may_be_incomplete"])

    def test_current_head_without_request_or_run_is_explicitly_not_requested(self):
        self.receipt(SHA)
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA, "not_requested")])

    def test_exact_head_request_is_requested(self):
        receipt = self.receipt(SHA)
        self.request(SHA, receipt)
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA, "requested")])

    def test_exact_head_success_and_failure(self):
        self.receipt(SHA)
        self.cache(successes={"main": [{"sha": SHA, "check": "unit"}]})
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA, "passing")])
        self.cache(failures={"main": [{"sha": SHA, "check": "unit"}]},
                   successes={"main": [{"sha": SHA, "check": "unit"}]})
        self.assertEqual(self.facts(), [ci_head_state._row("main", SHA, "failing")])

    def test_injected_ref_wins_over_publication_head(self):
        self.receipt(SHA)
        self.inject_ref(OLDER)
        self.assertEqual(self.facts()[0]["revision"], OLDER)

    def test_later_head_request_or_success_covers_stale_head(self):
        self.receipt(OLDER)
        later = self.receipt(LATER)
        self.inject_ref(OLDER)
        self.request(LATER, later)
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", OLDER, "covered_by_later", LATER)])
        self.cache(successes={"main": [{"sha": LATER, "check": "unit"}]})
        self.assertEqual(self.facts(), [ci_head_state._row(
            "main", OLDER, "covered_by_later", LATER)])

    def test_exact_head_status_wins_over_later_coverage(self):
        self.receipt(OLDER)
        later = self.receipt(LATER)
        self.inject_ref(OLDER)
        self.request(LATER, later)
        self.cache(failures={"main": [{"sha": OLDER, "check": "unit"}]})
        self.assertEqual(self.facts(), [ci_head_state._row("main", OLDER, "failing")])

    def test_unordered_injected_ref_cannot_claim_older_receipt_as_later(self):
        self.receipt(OLDER)
        self.inject_ref(LATER)
        self.request(OLDER, {"publication_id": "e" * 64})
        self.assertEqual(self.facts(), [ci_head_state._row("main", LATER, "not_requested")])

    def test_only_receipted_later_shas_can_cover(self):
        self.receipt(OLDER)
        self.inject_ref(OLDER)
        self.request(LATER, {"publication_id": "f" * 64})
        self.assertEqual(self.facts(), [ci_head_state._row("main", OLDER, "not_requested")])

    def test_one_bounded_row_per_configured_branch_sorted(self):
        self.receipt(SHA)
        self.receipt(OLDER, branch="release")
        config = dataclasses.replace(self.config, vcs={
            **self.config.vcs, "ci_branch": "release",
            "ci_wake_extra_branches": ["main", "main"]})
        rows = ci_head_state.ci_head_state_facts(self.project, config)
        self.assertEqual([row["subject"] for row in rows], ["main", "release"])
        self.assertTrue(all(set(row) == KEYS for row in rows))

    def test_unreadable_or_truncated_evidence_is_incomplete_not_success(self):
        self.receipt(SHA)
        write_json(self.project.root / "vcs-poll.json", "[")
        row = self.facts()[0]
        self.assertEqual(row["state"], "not_requested")
        self.assertTrue(row["may_be_incomplete"])
        self.cache()
        with patch("mizu.ci_head_state.CiRequests.daily",
                   return_value={"days": [], "total": 100, "truncated": True,
                                 "scan_stopped": False}):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])
        self.inject_ref(SHA)
        with patch("mizu.ci_head_state.read_ref", side_effect=OSError("unreadable")):
            self.assertTrue(self.facts()[0]["may_be_incomplete"])

    def test_role_gating_in_full_and_delta_prompts(self):
        self.receipt(SHA)
        for builder in (prompt_for, prompt_delta_for):
            with self.subTest(builder=builder.__name__):
                self.assertEqual(self.opening(vcs_request=False, builder=builder)["ci_head_state"], [])
                offered = self.opening(vcs_request=True, builder=builder)["ci_head_state"]
                self.assertEqual(offered, [ci_head_state._row("main", SHA, "not_requested")])
                self.assertEqual(
                    self.opening(vcs_request=True, builder=builder,
                                 context_exclude=("ci",))["ci_head_state"], offered)
                self.assertEqual(
                    self.opening(vcs_request=False, builder=builder,
                                 context_include=("ci",))["ci_head_state"], [])
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
