"""Bounded external-check facts share the wake row observation, not admission."""
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu import ci_observation, ci_wake
from mizu.ci_observation import ci_cache_limits, wake_ci_failures
from mizu.ci_polling import poll_ci
from mizu.ci_wake import _ci_due_for, write_ci_wake
from mizu.fs import write_json


SHA_A = "a" * 40
SHA_B = "b" * 64


class CiWakeFactTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "ci_branch": "main",
            "ci_wake_extra_branches": ("release",)})
        self.project.config = self.config

    def poll(self, state, rows, *, branches=("main",), now=1000):
        event = poll_ci(self.config, self.project, state, now=now, interval=15,
                        branches=list(branches), reader=lambda *args: {"checks": rows})
        write_json(self.project.root / "vcs-poll.json", state)
        return event

    def fact(self, sha=SHA_A, count=1, incomplete=False, subject="main"):
        return {"subject": subject, "revision": sha, "outcome": "failure",
                "non_passing_checks": count, "may_be_incomplete": incomplete}

    def test_mixed_states_and_revisions_are_counted_before_retention(self):
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "ci_cache_max_checks_per_branch": 2})
        states = ("success", "failure", "pending", "cancelled", "skipped", "neutral")
        rows = [{"sha": SHA_B, "check": state, "state": state} for state in states]
        rows += [{"sha": SHA_A, "check": f"matrix-{i}", "state": "failure"}
                 for i in range(3)]
        state = {}
        self.assertTrue(self.poll(state, rows)["ok"])
        # Counts cover the full observation, not just the two retained rows.
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config),
                         [self.fact(SHA_A, 3, True), self.fact(SHA_B, 5, True)])

    def test_exact_mixed_count_and_revision_separation(self):
        rows = [{"sha": sha, "check": state, "state": state}
                for sha in (SHA_B, SHA_A)
                for state in ("success", "failure", "pending", "cancelled", "skipped", "neutral")]
        state = {}
        self.assertTrue(self.poll(state, rows)["ok"])
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config),
                         [self.fact(SHA_A, 5), self.fact(SHA_B, 5)])

    def test_legacy_and_malformed_summary_fall_back_without_leaking_details(self):
        rows = [{"sha": SHA_A, "check": "unit", "run": "attempt-1"},
                {"sha": SHA_A, "check": "lint"}, {"sha": "bad", "check": "bad"},
                {"sha": SHA_B, "check": "unit"}]
        state = {"failures": {"main": rows},
                 "successes": {"release": [{"sha": SHA_A, "check": "unit"}]}}
        expected = [self.fact(SHA_A, 2, True), self.fact(SHA_B, 1, True)]
        for summaries in (None, [], {"main": [{"sha": SHA_A,
                "non_passing_checks": True, "may_be_incomplete": False}]},
                {"main": [{"sha": SHA_A, "non_passing_checks": 0,
                           "may_be_incomplete": False}]},
                {"main": [{"sha": SHA_A, "non_passing_checks": 1,
                           "may_be_incomplete": False}]},
                {"main": [{"sha": SHA_A, "non_passing_checks": 2,
                           "may_be_incomplete": "false"}]},
                {"main": [{"sha": SHA_A, "non_passing_checks": 1025,
                           "may_be_incomplete": False}]},
                {"main": [{"sha": SHA_A, "non_passing_checks": 2,
                           "may_be_incomplete": False}] * 2}):
            with self.subTest(summaries=summaries):
                state["ci_check_summaries"] = summaries
                write_json(self.project.root / "vcs-poll.json", state)
                self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config), expected)

    def test_dispatch_uses_one_cache_read_and_keeps_rerun_suppression(self):
        state = {}
        rows = [{"sha": SHA_A, "check": "unit", "state": "failure", "run": "first"}]
        self.poll(state, rows)
        due = wake_ci_failures(self.project, self.config)
        facts = [self.fact()]
        with patch("mizu.ci_observation.read_json", side_effect=[state, {}]) as read:
            self.assertEqual(ci_wake._ci_due_for_with_facts(
                self.project, self.config, "worker", "head", 1000), (due, facts))
            read.assert_called_once()
        self.assertEqual(_ci_due_for(self.project, self.config, "worker", "head", 1000), due)
        write_ci_wake(self.project, "worker", ("main", "release"), due, "head", 1000)
        for role, head, now, expected in (("worker", "head", 1001, ([], [])),
                ("worker", "new-head", 1001, (due, facts)),
                ("worker", "head", 2800, (due, facts)),
                ("worker", None, None, (due, facts)),
                ("reporter", "head", 1001, ([], []))):
            with self.subTest(role=role, head=head, now=now):
                self.assertEqual(ci_wake._ci_due_for_with_facts(
                    self.project, self.config, role, head, now), expected)
                self.assertEqual(_ci_due_for(
                    self.project, self.config, role, head, now), expected[0])
        rows[0]["run"] = "rerun"
        self.poll(state, rows, now=1020)
        rerun, frozen = ci_wake._ci_due_for_with_facts(self.project, self.config, "worker", "head", 1020)
        self.assertEqual(rerun[0]["run"], "rerun")
        self.assertEqual(frozen, facts)

    def test_summary_replaced_on_refresh_and_unreadable_checks_are_not_exact(self):
        state = {}
        rows = [{"sha": SHA_A, "check": "unit", "state": "failure"}]
        self.poll(state, rows)
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config), [self.fact()])
        self.assertFalse(self.poll(state, None, now=1020)["ok"])
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config), [self.fact(incomplete=True)])
        self.poll(state, [{"sha": SHA_B, "check": "unit", "state": "failure"}], now=1040)
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config), [self.fact(SHA_B)])
        self.poll(state, [], now=1060)
        self.assertEqual(state["ci_check_summaries"], {})
        self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config), [])

    def test_malformed_status_never_contributes_to_summary(self):
        state = {}
        self.poll(state, [{"sha": SHA_A, "check": "unit", "state": "failure"}])
        for row in (None, {}, {"sha": SHA_B, "check": "other", "state": ""},
                    {"sha": "bad", "check": "other", "state": "failure"}):
            with self.subTest(row=row):
                event = self.poll(state, [row], now=state["last_poll"] + 20)
                self.assertFalse(event["ok"])
                self.assertEqual(ci_observation.wake_ci_facts(self.project, self.config),
                                 [self.fact(incomplete=True)])

    def test_summary_bounds_wake_priority_and_live_branch_pruning(self):
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "ci_cache_max_branches": 2, "ci_cache_max_checks_per_branch": 2})
        summary = {"sha": SHA_A, "non_passing_checks": 1, "may_be_incomplete": False}
        stored = {name: [summary] for name in ("deleted", "unrelated", "main", "release")}
        self.assertEqual(list(ci_observation._clean_ci_check_summaries(stored, self.config)),
                         ["main", "release"])
        state = {"ci_check_summaries": stored,
                 "failures": {name: [{"sha": SHA_A, "check": "unit"}]
                              for name in stored}}
        # Automatic target selection gets a complete live-ref inventory; stale
        # summaries disappear with the failure observation that referenced them.
        with patch("mizu.vcs.list_refs", return_value=["main", "release"]):
            event = poll_ci(self.config, self.project, state, now=1000, interval=15,
                            reader=lambda *args: {"checks": [
                                {"sha": SHA_B, "check": "new", "state": "failure"}]})
        self.assertTrue(event["ok"])
        self.assertEqual(set(state["ci_check_summaries"]), {"main", "release"})
        for summaries in state["ci_check_summaries"].values():
            self.assertEqual([row["sha"] for row in summaries], [SHA_B])
        max_branches, max_checks = ci_cache_limits(self.config)
        self.assertLessEqual(len(state["ci_check_summaries"]), max_branches)
        self.assertTrue(all(len(rows) <= max_checks
                            for rows in state["ci_check_summaries"].values()))
