"""Bounded CI aggregate facts projected by the operator-owned observation driver."""
import dataclasses
import importlib.util
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import ci_observation, prompt_sections, queue_facts, summary_observation
from mizu.context_vcs_read_tools import _RECORD_ENVELOPE_KEYS
from mizu.errors import Denied
from mizu.fs import canonical, mkdir, write_json
from mizu.unit_prompts import prompt_for

DRIVER = (Path(__file__).resolve().parents[1] / "examples" / "development"
          / "drivers" / "ci_summary_driver.py")
_SPEC = importlib.util.spec_from_file_location("ci_summary_driver", DRIVER)
driver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(driver)

SHA = "a" * 40
OLDER = "b" * 40
QUEUE = "ci_summary_observations"
FACT_KEYS = {"subject", "revision", "outcome", "non_passing_checks",
             "may_be_incomplete"}


class CiSummaryObservationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(
            self.config, vcs={**self.config.vcs, "ci_branch": "main"})
        self.project.config = self.config

    def cache(self, failures=None, summaries=None):
        state = {"failures": failures or {}}
        if summaries is not None:
            state["ci_check_summaries"] = summaries
        write_json(self.project.root / ci_observation.POLL_STATE_FILE, state)

    def request(self, config=None):
        return summary_observation.summary_request(self.project, config or self.config)

    def facts(self, request=None):
        if request is None:
            request = self.request()
        return driver.observed_summary(request)["facts"] if request else []

    def observed(self, config, project, name, request):
        """Stand in for the generic host seam, invoking the real driver once."""
        self.assertEqual(name, "ci_summary")
        self.assertEqual(set(request), {"rows", "summaries", "partial"})
        self.assertTrue(all(type(row) is list and len(row) == 2
                            for row in request["rows"]))
        self.assertTrue(all(type(row) is list and len(row) == 4
                            for row in request["summaries"]))
        self.calls.append(request)
        return {"appended": 1, "observations": [{
            "driver": name, "kind": name, "summary": "observed",
            "observed_at": "2026-10-01T00:00:00+00:00",
            "detail": canonical(driver.observed_summary(request)).decode()}]}

    def facts_through_host(self, config=None):
        self.calls = []
        with patch("mizu.host_drivers.observe", side_effect=self.observed):
            rows = summary_observation.ci_summary_facts(
                self.project, config or self.config)
        self.assertEqual(len(self.calls), 1)
        return rows

    def summary(self, sha=SHA, count=1, incomplete=False):
        return {"sha": sha, "non_passing_checks": count,
                "may_be_incomplete": incomplete}

    def test_no_failure_rows_means_no_driver_call_and_no_facts(self):
        self.cache()
        self.assertEqual(self.request(), {})
        with patch("mizu.host_drivers.observe", side_effect=AssertionError("call")):
            self.assertEqual(summary_observation.ci_summary_facts(
                self.project, self.config), [])
        write_json(self.project.root / ci_observation.POLL_STATE_FILE, "[")
        self.assertEqual(self.request(), {})

    def test_each_retained_check_row_raises_the_lower_bound(self):
        self.cache({"main": [{"sha": SHA, "check": f"check-{index}"}
                             for index in range(3)]})
        request = self.request()
        self.assertEqual(request["rows"],
                         [["main", SHA] for _ in range(3)])
        self.assertEqual(request["summaries"], [])
        self.assertFalse(request["partial"])
        self.assertEqual(self.facts(request), [{
            "subject": "main", "revision": SHA, "outcome": "failure",
            "non_passing_checks": 3, "may_be_incomplete": True}])

    def test_host_valid_spaced_names_keep_aggregate_facts(self):
        """Host-valid spaces in check names or subjects must not drop facts."""
        for subject, name in (("main", "unit test"),
                              ("main", "unit\u00a0test"),
                              ("feature/foo bar", "unit")):
            with self.subTest(subject=subject, name=name):
                self.cache({subject: [{"sha": SHA, "check": name}],
                            "release": [{"sha": OLDER, "check": "unit"}]})
                request = self.request()
                # The unused check name never crosses the trust boundary.
                self.assertTrue(all(len(row) == 2 for row in request["rows"]))
                self.assertEqual(self.facts_through_host(), [
                    {"subject": subject, "revision": SHA, "outcome": "failure",
                     "non_passing_checks": 1, "may_be_incomplete": True},
                    {"subject": "release", "revision": OLDER, "outcome": "failure",
                     "non_passing_checks": 1, "may_be_incomplete": True}])

    def test_exact_summary_supplies_the_full_count(self):
        rows = [{"sha": SHA, "check": f"check-{index}"} for index in range(3)]
        self.cache({"main": rows}, {"main": [self.summary(count=4)]})
        self.assertEqual(self.facts(), [{
            "subject": "main", "revision": SHA, "outcome": "failure",
            "non_passing_checks": 4, "may_be_incomplete": False}])
        self.cache({"main": rows}, {"main": [self.summary(count=3)]})
        self.assertEqual(self.facts()[0]["non_passing_checks"], 3)
        self.assertFalse(self.facts()[0]["may_be_incomplete"])

    def test_summary_below_the_lower_bound_is_discarded(self):
        self.cache({"main": [{"sha": SHA, "check": f"check-{index}"}
                             for index in range(3)]},
                   {"main": [self.summary(count=1)]})
        self.assertEqual(self.facts(), [{
            "subject": "main", "revision": SHA, "outcome": "failure",
            "non_passing_checks": 3, "may_be_incomplete": True}])

    def test_incompleteness_combines_partial_and_summary_flags(self):
        rows = {"main": [{"sha": SHA, "check": "unit"}]}
        for partial, summary, expected in (
                (False, self.summary(incomplete=False), False),
                (True, self.summary(incomplete=False), True),
                (False, self.summary(incomplete=True), True),
                (False, None, True),
                (None, self.summary(incomplete=False), True)):
            with self.subTest(partial=partial, summary=summary):
                summaries = {"main": [summary]} if summary else None
                self.cache(rows, summaries)
                request = self.request()
                request["partial"] = partial
                self.assertEqual(self.facts(request)[0]["may_be_incomplete"], expected)

    def test_facts_are_sorted_by_subject_then_revision(self):
        self.cache({"release": [{"sha": SHA, "check": "unit"},
                                {"sha": OLDER, "check": "unit"}],
                    "main": [{"sha": SHA, "check": "unit"}]})
        facts = self.facts()
        self.assertEqual([(fact["subject"], fact["revision"]) for fact in facts],
                         [("main", SHA), ("release", SHA), ("release", OLDER)])
        self.assertTrue(all(set(fact) == FACT_KEYS for fact in facts))

    def test_max_rows_facts_at_the_retained_cache_ceiling(self):
        branches = [f"branch-{index:03d}" for index in range(128)]
        failures = {branch: [{"sha": f"{index * 16 + slot:040x}",
                              "check": f"check-{slot}"} for slot in range(16)]
                    for index, branch in enumerate(branches)}
        self.cache(failures)
        request = self.request()
        self.assertEqual(len(request["rows"]), driver.MAX_ROWS)
        facts = self.facts(request)
        self.assertEqual(len(facts), driver.MAX_ROWS)
        self.assertEqual(facts, sorted(facts,
                                       key=lambda row: (row["subject"], row["revision"])))
        self.assertTrue(all(fact["non_passing_checks"] == 1
                            and fact["may_be_incomplete"] for fact in facts))

    def test_undeclared_or_refusing_driver_returns_empty(self):
        self.cache({"main": [{"sha": SHA, "check": "unit"}]})
        with patch("mizu.host_drivers.observe", side_effect=Denied("undeclared")):
            self.assertEqual(summary_observation.ci_summary_facts(
                self.project, self.config), [])
        with patch("mizu.host_drivers.observe", side_effect=RuntimeError("failed")):
            self.assertEqual(summary_observation.ci_summary_facts(
                self.project, self.config), [])
        without = dataclasses.replace(self.config, drivers={})
        self.assertEqual(summary_observation.ci_summary_facts(self.project, without), [])

    def test_host_caller_fails_closed_on_malformed_detail(self):
        self.cache({"main": [{"sha": SHA, "check": "unit"}]})
        valid = canonical(driver.observed_summary(self.request())).decode()
        bad_details = ["[", "{}", '{"facts": [], "op": "x"}']
        for key in _RECORD_ENVELOPE_KEYS:
            bad_details.append(canonical({"facts": [], key: "reserved"}).decode())
        bad_fact = {"subject": "main", "revision": SHA, "outcome": "failure",
                    "non_passing_checks": 1, "may_be_incomplete": False}
        for mutate in (lambda row: row.update(outcome=1),
                       lambda row: row.update(extra="x"),
                       lambda row: row.pop("subject")):
            row = dict(bad_fact)
            mutate(row)
            bad_details.append(canonical({"facts": [row]}).decode())
        duplicate = dict(bad_fact, revision=OLDER)
        bad_details.append(canonical({"facts": [bad_fact, bad_fact]}).decode())
        bad_details.append(canonical({"facts": [duplicate, bad_fact]}).decode())
        bad_details.append(canonical({"facts": [bad_fact] * (driver.MAX_ROWS + 1)}).decode())
        for detail in bad_details:
            with self.subTest(detail=detail[:80]):
                with patch("mizu.host_drivers.observe", return_value={
                        "observations": [{"detail": detail}]}):
                    self.assertEqual(summary_observation.ci_summary_facts(
                        self.project, self.config), [])
        for response in (None, {}, {"observations": []},
                         {"observations": [{"detail": {}}]},
                         {"observations": [{"detail": valid}] * 2}):
            with self.subTest(response=response):
                with patch("mizu.host_drivers.observe", return_value=response):
                    self.assertEqual(summary_observation.ci_summary_facts(
                        self.project, self.config), [])
        self.assertEqual(self.facts_through_host(), [
            dict(bad_fact, non_passing_checks=1, may_be_incomplete=True)])

    def test_driver_rejects_malformed_raw_evidence(self):
        self.cache({"main": [{"sha": SHA, "check": "unit"}]},
                   {"main": [self.summary()]})
        request = self.request()
        bad = []
        for key in ("rows", "summaries", "partial"):
            broken = dict(request)
            del broken[key]
            bad.append(broken)
        bad.append({**request, "extra": 1})
        for key in ("rows", "summaries"):
            for value in (None, {}, "x", [[]], ["x"], [1]):
                bad.append({**request, key: value})
        for value in ("yes", 1):
            bad.append({**request, "partial": value})
        broken = json.loads(canonical(request))
        broken["rows"][0][1] = SHA.upper()
        bad.append(broken)
        broken = json.loads(canonical(request))
        broken["rows"][0][0] = "bad\nbranch"
        bad.append(broken)
        broken = json.loads(canonical(request))
        broken["summaries"][0] = ["main", SHA, 0, False]
        bad.append(broken)
        broken = json.loads(canonical(request))
        broken["summaries"][0] = ["main", SHA, 1, "False"]
        bad.append(broken)
        broken = json.loads(canonical(request))
        broken["summaries"].append(list(broken["summaries"][0]))
        bad.append(broken)
        broken = json.loads(canonical(request))
        broken["rows"] = [["x" * (driver.MAX_TEXT + 1), SHA]]
        bad.append(broken)
        bad.append({**request, "rows": [["main", SHA]] * (driver.MAX_ROWS + 1)})
        for value in bad:
            with self.subTest(value=canonical(value)[:100]):
                with self.assertRaises(ValueError):
                    driver.observed_summary(value)

    def test_real_driver_subprocess_projects_one_bounded_observation(self):
        self.cache({"main": [{"sha": SHA, "check": "unit"},
                             {"sha": SHA, "check": "build"}],
                    "release": [{"sha": OLDER, "check": "unit"}]},
                   {"main": [self.summary(count=3)]})
        request = self.request()
        result = subprocess.run([sys.executable, str(DRIVER)],
                                input=json.dumps(request), text=True,
                                capture_output=True, check=True)
        observation = json.loads(result.stdout)["observations"]
        self.assertEqual(len(observation), 1)
        body = observation[0]
        self.assertEqual((body["driver"], body["kind"]), ("ci_summary", "ci_summary"))
        self.assertLessEqual(len(body["summary"]), driver.SUMMARY_MAX)
        self.assertIn("+00:00", body["observed_at"])
        self.assertLessEqual(len(body["detail"].encode("utf-8")), driver.DETAIL_MAX)
        self.assertEqual(json.loads(body["detail"])["facts"], self.facts(request))
        self.assertEqual(json.loads(body["detail"])["facts"], self.facts_through_host())

    def test_real_host_path_appends_raised_cap_detail(self):
        branches = [f"branch-{index:03d}" for index in range(64)]
        failures = {branch: [{"sha": f"{index * 16 + slot:040x}",
                              "check": f"check-{slot:02d}-" + "x" * 120}
                             for slot in range(16)]
                    for index, branch in enumerate(branches)}
        self.cache(failures)
        facts = summary_observation.ci_summary_facts(self.project, self.config)
        self.assertEqual(len(facts), 1024)
        self.assertEqual(facts, sorted(facts,
                                       key=lambda row: (row["subject"], row["revision"])))
        self.assertIn("ci_summary", self.config.drivers)
        self.assertEqual(self.config.queues[QUEUE].fields["detail"].max, driver.DETAIL_MAX)
        store = self.project.kernel_queues
        self.assertEqual(self.config.queues[QUEUE].wake, ())
        self.assertEqual(self.config.queues[QUEUE].retain_last, 1)
        self.assertNotIn(QUEUE, queue_facts.waking(self.config, "worker"))
        self.assertFalse(store.due(QUEUE, "role:worker"))
        self.assertFalse(queue_facts.due(self.project, "worker"))
        page = store.read(QUEUE, "role:worker")
        self.assertEqual((page["head"], len(page["messages"])), (1, 1))
        body = page["messages"][0]["body"]
        self.assertEqual((page["messages"][0]["author"], body["driver"], body["kind"]),
                         ("host", "ci_summary", "ci_summary"))
        detail = body["detail"]
        self.assertGreater(len(detail.encode("utf-8")), 16384)
        self.assertLessEqual(len(detail.encode("utf-8")), driver.DETAIL_MAX)
        self.assertEqual(json.loads(detail)["facts"], facts)


    def test_prompt_keeps_observation_as_readable_fact_without_waking(self):
        """The opt-in prompt projection records its observation as a readable fact."""
        from mizu.runtime import Context
        self.cache({"main": [{"sha": SHA, "check": "unit"}]})
        role = dataclasses.replace(
            self.config.roles["worker"],
            context_include=("ci",),
            context_exclude=tuple(n for n in prompt_sections.SECTIONS if n != "ci"))
        config = dataclasses.replace(
            self.config, roles={**self.config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        context = Context(config, self.project, role, run,
                          self.project.snapshots.get(), self.project.workspace)
        context.queues = []
        payload = json.loads(prompt_for(context))  # real host seam appends one observation
        self.assertEqual(payload["ci_summary"], [self.facts()[0]])
        store = self.project.kernel_queues
        self.assertFalse(store.due(QUEUE, "role:worker"))
        self.assertNotIn(QUEUE, queue_facts.waking(config, "worker"))
        rows = [row for row in context.queues if row.get("queue") == QUEUE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(len(rows[0]["messages"]), 1)
        body = rows[0]["messages"][0]["body"]
        self.assertEqual((body["driver"], body["kind"]), ("ci_summary", "ci_summary"))
        self.assertEqual(json.loads(body["detail"])["facts"], payload["ci_summary"])
        queue_facts.mark_woken(self.project, "worker", context.queues)
        self.assertFalse(store.due(QUEUE, "role:worker"))

    def test_distinct_reader_gets_no_fact_on_fixed_key_or_queue_opening(self):
        """A producer's aggregate reaches the opted-in worker, not another role."""
        from mizu.runtime import Context
        self.cache({"main": [{"sha": SHA, "check": "unit"}]})
        role = dataclasses.replace(
            self.config.roles["worker"],
            context_include=("ci",),
            context_exclude=tuple(n for n in prompt_sections.SECTIONS if n != "ci"))
        config = dataclasses.replace(
            self.config, roles={**self.config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        worker_ctx = Context(config, self.project, role, run,
                             self.project.snapshots.get(), self.project.workspace)
        worker_payload = json.loads(prompt_for(worker_ctx))  # real host seam appends as host
        self.assertEqual(worker_payload["ci_summary"], [self.facts()[0]])
        worker_rows = [r.get("queue") for r in queue_facts.opening(self.project, "worker")]
        self.assertIn(QUEUE, worker_rows)

        # A distinct role without the opt-in section and without the read grant
        # gets neither the aggregate nor the raw observation through its opening.
        reviewer_ctx = super().context("reviewer")
        reviewer_payload = json.loads(prompt_for(reviewer_ctx))
        self.assertNotIn("ci", prompt_sections.included(reviewer_ctx.role))
        self.assertNotIn("ci_summary", reviewer_payload)
        reviewer_rows = [r.get("queue") for r in queue_facts.opening(self.project, "reviewer")]
        self.assertNotIn(QUEUE, reviewer_rows)


if __name__ == "__main__":
    unittest.main()
