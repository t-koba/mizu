"""Dashboard projection policy: recent-decision count and reason truncation are operator knobs."""
import dataclasses
from support import Fixture
from mizu.config import ConfigError, load
from mizu.dashboard import collect


class DashboardPolicyTests(Fixture):
    def _decide(self, reason):
        snap = self.project.snapshots.get()["id"]
        item = self.project.insights.submit(source="operator", title="T", body="B",
                                            base_snapshot=snap)
        self.project.insights.decide(item["id"], "accept", reason, "", "test", expected_rev=1)
        return item

    def test_defaults_match_previous_fixed_bounds(self):
        self.assertEqual(self.config.limits.dashboard_decisions, 10)
        self.assertEqual(self.config.limits.dashboard_reason_chars, 500)
        self._decide("R" * 600)
        core = collect(self.project)
        self.assertEqual(len(core["recent_decisions"]), 1)
        entry = core["recent_decisions"][0]["reason"]
        self.assertTrue(entry["truncated"])
        self.assertEqual(len(entry["text"]), 500)

    def test_operator_values_control_projection(self):
        for _ in range(3):
            self._decide("ok")
        limited = dataclasses.replace(self.config,
                                      limits=dataclasses.replace(self.config.limits,
                                                                 dashboard_decisions=2,
                                                                 dashboard_reason_chars=100))
        self.project.config = limited
        core = collect(self.project)
        self.assertEqual(len(core["recent_decisions"]), 2)
        self.project.config = dataclasses.replace(
            self.config,
            limits=dataclasses.replace(self.config.limits, dashboard_reason_chars=100))
        self._decide("R" * 150)
        core = collect(self.project)
        by_len = {len(d["reason"]["text"]) for d in core["recent_decisions"]}
        self.assertIn(100, by_len)
        flagged = [d["reason"] for d in core["recent_decisions"]
                   if d["reason"]["text"] == "R" * 100]
        self.assertTrue(flagged and all(f["truncated"] for f in flagged))

    def test_ranges_reject_out_of_bounds(self):
        text = self.file.read_text().replace("dashboard_decisions = 10",
                                              "dashboard_decisions = 0")
        self.file.write_text(text)
        with self.assertRaises(ConfigError):
            load(self.file)
