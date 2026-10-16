"""Owner-only compact work-item status on exact insight reads."""
from support import Fixture
from mizu.errors import Denied
from mizu.fs import write_json


class InsightWorkItemStatusTests(Fixture):
    def accepted(self, *, source="worker", action="accept"):
        proposal = self.project.insights.submit(
            source=source, title="Follow-up", body="Proposal body remains on the insight",
            base_snapshot=None)
        decision = self.project.insights.decide(
            proposal["id"], action, "Reviewed", "", "status-test", expected_rev=1)
        return proposal, decision["work_item"]

    def read(self, role, insight_id):
        return self.context(role).handle("insights", {"id": insight_id})

    def test_current_decision_work_item_id_is_opt_in(self):
        proposal, item = self.accepted()

        self.assertEqual(self.project.work_items._current_decision(proposal["id"]),
                         {"rev": 1, "action": "accept"})
        self.assertEqual(self.project.work_items._current_decision(
            proposal["id"], include_work_item=True),
            {"rev": 1, "action": "accept", "work_item_id": item["id"]})

    def test_owner_gets_latest_compact_status_and_other_role_does_not(self):
        proposal, item = self.accepted()
        worker = self.context("worker")
        changed = worker.handle("work_items_transition", {
            "id": item["id"], "expected_rev": 1, "status": "blocked",
            "reason": "Investigate the dependency", "wait": {"kind": "code_change"}})

        owner = self.read("worker", proposal["id"])
        other = self.read("reporter", proposal["id"])

        status = owner["work_item_status"]
        self.assertEqual(status, {
            "id": item["id"], "status": "blocked", "rev": 2, "holder": "worker",
            "updated_at": changed["updated_at"], "current_origin_rev": 1,
            "stale": False, "reason_preview": "Investigate the dependency",
            "reason_truncated": False, "wait": changed["wait"]})
        self.assertNotIn("work_item_status", other)
        self.assertEqual(owner["body"], "Proposal body remains on the insight")

    def test_non_accept_modify_decision_has_no_status(self):
        proposal = self.project.insights.submit(
            source="worker", title="Deferred", body="No work item", base_snapshot=None)
        self.assertNotIn("work_item_status", self.read("worker", proposal["id"]))
        self.project.insights.decide(proposal["id"], "defer", "Need more evidence",
                                     "later", "status-test", expected_rev=1)

        record = self.read("worker", proposal["id"])

        self.assertNotIn("work_item_status", record)

    def test_drop_only_legacy_reconciliation_has_no_status(self):
        proposal = self.project.insights.submit(
            source="worker", title="Legacy drop", body="No fabricated item", base_snapshot=None)
        event_id = "a" * 32
        event = {"id": proposal["id"], "action": "accept", "reason": "Legacy accept",
                 "revisit": "", "run": "legacy-run",
                 "created_at": "2024-01-01T00:00:00+00:00", "rev": 1}
        write_json(self.project.root / "decision-history" / f"{event_id}.json", event)
        write_json(self.project.root / "decisions" / f"{proposal['id']}.json", event)
        self.project.work_items.reconcile(
            event_id, expected_action="accept", expected_rev=1, outcome="drop",
            reason="No longer actionable", actor="worker")

        record = self.read("worker", proposal["id"])

        self.assertNotIn("work_item_status", record)
        with self.assertRaises(Denied):
            self.project.work_items.get(f"wi_{event_id}")

    def test_unresolved_legacy_decision_has_no_inferred_status(self):
        proposal = self.project.insights.submit(
            source="worker", title="Legacy unresolved", body="No sidecar", base_snapshot=None)
        event_id = "c" * 32
        event = {"id": proposal["id"], "action": "accept", "reason": "Legacy accept",
                 "revisit": "", "run": "legacy-run",
                 "created_at": "2024-01-01T00:00:00+00:00", "rev": 1}
        write_json(self.project.root / "decision-history" / f"{event_id}.json", event)
        write_json(self.project.root / "decisions" / f"{proposal['id']}.json", event)

        record = self.read("worker", proposal["id"])

        self.assertNotIn("work_item_status", record)
        self.assertFalse((self.project.root / "work-items" / f"wi_{event_id}" / "legacy.json").exists())

    def test_decided_listing_does_not_add_submitter_status(self):
        proposal, _ = self.accepted()

        response = self.context("worker").handle(
            "insights", {"action": "accept", "offset": 0, "limit": 1000})

        listed = next(item for item in response["insights"] if item["id"] == proposal["id"])
        self.assertNotIn("work_item_status", listed)

    def test_valid_open_legacy_reconciliation_is_projected(self):
        proposal = self.project.insights.submit(
            source="worker", title="Legacy open", body="Validated sidecar", base_snapshot=None)
        event_id = "b" * 32
        event = {"id": proposal["id"], "action": "modify", "reason": "Legacy modify",
                 "revisit": "", "run": "legacy-run",
                 "created_at": "2024-01-01T00:00:00+00:00", "rev": 1}
        write_json(self.project.root / "decision-history" / f"{event_id}.json", event)
        write_json(self.project.root / "decisions" / f"{proposal['id']}.json", event)
        self.project.work_items.reconcile(
            event_id, expected_action="modify", expected_rev=1, outcome="open",
            reason="Keep this bounded", title="Legacy follow-up", actor="worker")

        status = self.read("worker", proposal["id"])["work_item_status"]

        self.assertEqual(status["id"], f"wi_{event_id}")
        self.assertEqual(status["status"], "open")
        self.assertEqual(status["rev"], 1)
        self.assertEqual(status["current_origin_rev"], 1)
        self.assertFalse(status["stale"])

    def test_stale_accepted_origin_still_surfaces_item_and_staleness(self):
        proposal, item = self.accepted()
        self.project.insights.revise(
            proposal["id"], source="worker", title="Revised follow-up",
            body="Current proposal", base_snapshot=None, expected_rev=1)

        status = self.read("worker", proposal["id"])["work_item_status"]

        self.assertEqual(status["id"], item["id"])
        self.assertEqual(status["status"], "open")
        self.assertEqual(status["current_origin_rev"], 2)
        self.assertTrue(status["stale"])


if __name__ == "__main__":
    import unittest
    unittest.main()
