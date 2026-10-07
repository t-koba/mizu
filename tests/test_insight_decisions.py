"""Decisions bind to the revision actually read; stale revisions are rejected."""
import unittest

from support import Fixture
from mizu.errors import Denied


class DecisionRevisionFixture(Fixture):
    def submit(self, source="worker"):
        snap = self.project.snapshots.get()
        return self.project.insights.submit(source=source, title="Proposal",
                                            body="original claims", base_snapshot=snap["id"])

    def revise(self, proposal):
        snap = self.project.snapshots.get()
        return self.project.insights.revise(proposal["id"], source="worker",
                                            title="Proposal", body="revised claims",
                                            base_snapshot=snap["id"], expected_rev=1)

    def decided(self, insight_id):
        return (self.project.root / "decisions" / f"{insight_id}.json").exists()


class DecisionRevisionTests(DecisionRevisionFixture):
    def test_matching_rev_records(self):
        proposal = self.submit()
        record = self.project.insights.decide(proposal["id"], "accept", "good", "", "run-1",
                                              expected_rev=1)
        self.assertEqual(record["rev"], 1)

    def test_stale_rev_rejected_without_side_effects(self):
        proposal = self.submit()
        self.assertEqual(self.revise(proposal)["rev"], 2)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "read rev1 rationale",
                                         "", "run-1", expected_rev=1)
        self.assertFalse(self.decided(proposal["id"]))
        # The current revision stays actionable for reassessment.
        record = self.project.insights.decide(proposal["id"], "accept", "read rev2 rationale",
                                              "", "run-1", expected_rev=2)
        self.assertEqual(record["rev"], 2)

    def test_absent_rev_denied(self):
        proposal = self.submit()
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "accept", "current", "", "run-1")
        self.assertFalse(self.decided(proposal["id"]))
        ctx = self.context("worker")
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "no rev echoed"})
        self.assertFalse(self.decided(proposal["id"]))

    def test_malformed_rev_denied(self):
        proposal = self.submit()
        for bad in (0, -1, True, "1", 1.0):
            with self.assertRaises(Denied, msg=f"rev={bad!r}"):
                self.project.insights.decide(proposal["id"], "accept", "x", "", "run-1",
                                             expected_rev=bad)
        self.assertFalse(self.decided(proposal["id"]))

    def test_stale_defer_registers_no_wait(self):
        proposal = self.submit()
        self.revise(proposal)
        with self.assertRaises(Denied):
            self.project.insights.decide(proposal["id"], "defer", "later", "when calm", "run-1",
                                         wait={"kind": "code_change"}, expected_rev=1)
        self.assertFalse(self.decided(proposal["id"]))
        self.assertFalse((self.project.root / "waits" / f"{proposal['id']}.json").exists())


class DecideToolRaceTests(DecisionRevisionFixture):
    def test_read_revise_decide_race_through_tool_path(self):
        ctx = self.context("worker")
        proposal = self.submit()
        read = ctx.handle("insights", {"id": proposal["id"]})
        self.assertEqual(read["rev"], 1)
        # An edit lands between the read and the decision.
        self.revise(proposal)
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "rev1 rationale", "rev": 1})
        self.assertFalse(self.decided(proposal["id"]))
        # Re-reading the current revision makes the decision land on it.
        reread = ctx.handle("insights", {"id": proposal["id"]})
        record = ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                       "reason": "rev2 rationale", "rev": reread["rev"]})
        self.assertEqual(record["rev"], 2)

    def test_tool_rejects_out_of_range_rev(self):
        ctx = self.context("worker")
        proposal = self.submit()
        with self.assertRaises(Denied):
            ctx.handle("decide", {"id": proposal["id"], "action": "accept",
                                  "reason": "x", "rev": 0})
        self.assertFalse(self.decided(proposal["id"]))


if __name__ == "__main__":
    unittest.main()
