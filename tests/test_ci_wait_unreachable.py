"""Unreachable CI waits are holder-actionable liveness, never auto-resolution."""
from unittest.mock import patch

from mizu import ci_waits, vcs
from mizu.ci_observation import POLL_STATE_FILE
from mizu.ci_requests import CiRequests, request_id
from mizu.errors import Denied
from mizu.fs import write_json
from mizu.project import Project
from test_ci_waits import CiWaitFixture, SHA

OTHER = "c" * 40


class UnreachableCiWaitTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        vcs.inject_refs(self.project.root, {"published": OTHER})
        write_json(self.project.root / POLL_STATE_FILE, {"failures": {}, "successes": {}})
        self.item = self.block()

    def live(self):
        return self.store.blocked_liveness_readonly()["items"][0]

    def assert_pending(self):
        self.assertEqual(self.live()["state"], "pending")
        self.assertEqual(self.store.page_ready(role_name="worker")["total"], 0)

    def test_impossible_is_actionable_in_status_and_ready_without_a_transition(self):
        row = self.live()
        self.assertEqual((row["state"], row["reason"], row["wait_kind"]),
                         ("impossible", "ci_commit_unreachable", "ci"))
        page = self.store.page_ready(role_name="worker")
        live = page["items"][0]["wait_liveness"]
        self.assertEqual((live["state"], live["reason"]), ("impossible", "ci_commit_unreachable"))
        current = self.store.get(self.item["id"])
        self.assertEqual((current["status"], current["rev"]), ("blocked", 2))

    def test_holder_reopens_with_a_reason_and_cannot_renew_without_it(self):
        with self.assertRaisesRegex(Denied, "Reopening blocked work requires a reason"):
            self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker")
        opened = self.store.transition(self.item["id"], expected_rev=2, status="open", actor="worker",
                                       reason="Pinned commit is no longer a branch head")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(self.store.blocked_liveness_readonly()["total"], 0)

    def test_current_branch_head_stays_pending(self):
        vcs.inject_refs(self.project.root, {"published": SHA})
        self.assert_pending()

    def test_current_evidence_blocks_proof_and_incomplete_ledger_stays_pending(self):
        with patch("mizu.ci_waits._read", return_value={}):
            self.assertIsNone(ci_waits.unreachable(self.store, self.item))
        with patch.object(CiRequests, "_rows", return_value=([], True)):
            self.assert_pending()

    def test_recorded_request_stays_pending(self):
        identity = request_id(self.receipt["publication_id"], SHA, ["ci"])
        CiRequests(self.project.root).mark_dispatch(identity, {
            "request_id": identity, "publication_id": self.receipt["publication_id"],
            "sha": SHA, "workflows": ["ci"], "dispatched_at": "2026-10-20T00:00:00+00:00"})
        self.assert_pending()

    def test_recorded_poll_run_stays_pending(self):
        write_json(self.project.root / POLL_STATE_FILE,
                   {"failures": {"published": [{"sha": SHA, "check": "unit"}]}, "successes": {}})
        self.assert_pending()

    def test_empty_or_unreadable_ref_inventory_stays_pending(self):
        vcs.inject_refs(self.project.root, {})
        self.assertEqual(self.live()["state"], "pending")
        with patch("mizu.vcs.list_refs", side_effect=OSError("unreadable")):
            self.assertEqual(self.live()["state"], "pending")
        with patch("mizu.vcs.list_refs", return_value=["../escape"]):
            self.assertEqual(self.live()["state"], "pending")

    def test_absent_or_malformed_poll_inventory_stays_pending(self):
        (self.project.root / POLL_STATE_FILE).unlink()
        self.assertEqual(self.live()["state"], "pending")
        write_json(self.project.root / POLL_STATE_FILE,
                   {"failures": {}, "successes": [{"sha": OTHER, "check": "unit"}]})
        self.assertEqual(self.live()["state"], "pending")
        write_json(self.project.root / POLL_STATE_FILE,
                   {"failures": {}, "successes": {}, "ci_changes_pending": True})
        self.assertEqual(self.live()["state"], "pending")

    def test_publication_ci_wait_uses_the_same_proof(self):
        self.config.vcs["ci_branch"] = "published"
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        vcs.inject_refs(self.project.root, {"published": OTHER})
        write_json(self.project.root / POLL_STATE_FILE, {"failures": {}, "successes": {}})
        self.snapshot = snapshot = self.project.snapshots.get()
        self.receipt_for()
        item = self.block(key="publication", wait={"kind": "publication_ci",
            "snapshot_id": snapshot["id"], "checks": ["unit", "lint"]})
        row = self.live()
        self.assertEqual((row["state"], row["reason"], row["wait_kind"]),
                         ("impossible", "ci_commit_unreachable", "publication_ci"))
        self.assertEqual(self.store.page_ready(role_name="worker")["items"][0]["id"], item["id"])
