"""Authoritative legacy insight/decision writes mirror into declared kernel objects."""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu.cli import _cmd_kernel_shadow, parser
from mizu.config import load
from mizu.errors import Denied
from mizu.kernel_shadow import decision_object_id, insight_fields, insight_object_id
from mizu.project import initialize

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "kernel_userland.toml"


class KernelShadowProjection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-kernel-shadow-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = dataclasses.replace(load(CONFIG), data=root / "data")
        source = root / "source"
        source.mkdir()
        (source / "app.py").write_bytes(b"VALUE = 2\n")
        goal = root / "GOAL.md"
        goal.write_text("Preserve correctness.\n")
        self.project = initialize(self.config, "shadow", source, goal,
                                  ["writer", "reviewer", "observer", "idle"],
                                  ["python3 -m unittest"])
        self.project.set_control(armed=True, paused=False, wake_generation="test-wake")

    def submit(self, insight_id=None):
        return self.project.insights.submit(
            source="writer", title="Migrate proposals", body="Mirror the accepted facts",
            base_snapshot=None, insight_id=insight_id, addressee="reviewer", urgency="high")

    def corrupt_state(self, kind, object_id, state):
        """Tamper with the stored revision itself, not with a read result."""
        path = (self.project.root / "objects" / kind / object_id / "00000001.json")
        record = json.loads(path.read_text())
        record["state"] = state
        path.write_text(json.dumps(record))

    def test_submit_revise_and_decide_project_into_kernel(self):
        insight = self.submit("aa" * 16)
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["fields"], {
            "title": "Migrate proposals", "body": "Mirror the accepted facts",
            "holder": "role:reviewer", "urgency": "high"})
        self.assertEqual(mirrored["author"], "role:writer")
        self.assertEqual(mirrored["state"], "pending")

        decision = self.project.insights.decide(
            insight["id"], "accept", "The addressed holder accepted", "", "test-run",
            expected_rev=1, work_item_intent="none")
        mirrored_decision = self.project.kernel_objects.read("decision", decision["decision_key"])
        self.assertEqual(mirrored_decision["fields"], {
            "insight": insight["id"], "revision": 1, "action": "accept",
            "reason": "The addressed holder accepted"})
        self.assertEqual(mirrored_decision["author"], "host")
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "accepted")

        revised = self.project.insights.revise(
            insight["id"], source="writer", title="Revised proposal", body="With new evidence",
            base_snapshot=None, expected_rev=1)
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["rev"], 3)  # create, accepted, revise
        self.assertEqual(mirrored["fields"]["title"], "Revised proposal")
        self.assertEqual(mirrored["state"], "pending")
        self.assertEqual(mirrored["actor"], "role:writer")

    def test_decision_replay_does_not_duplicate_kernel_objects(self):
        insight = self.submit("bb" * 16)
        first = self.project.insights.decide(
            insight["id"], "reject", "Not now", "", "test-run",
            expected_rev=1)
        head = self.project.kernel_events.head()
        second = self.project.insights.decide(
            insight["id"], "reject", "Not now", "", "test-run",
            expected_rev=1)
        self.assertEqual(first, second)
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("decision", first["decision_key"]), [1])

    def test_deferral_projects_state_instant_and_decision(self):
        insight = self.submit("dd" * 16)
        moment = "2099-01-01T00:00:00+00:00"
        decision = self.project.insights.decide(
            insight["id"], "defer", "Wait for the reviewer", "deadline at " + moment,
            "test-run", expected_rev=1)
        decision_object = self.project.kernel_objects.read(
            "decision", decision_object_id(decision))
        self.assertEqual(decision_object["fields"]["action"], "defer")
        self.assertEqual(decision_object["fields"]["revisit"], moment)
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["state"], "deferred")
        self.assertEqual(mirrored["fields"]["revisit_at"], moment)
        self.assertEqual(self.project.kernel_objects.next_instant(), moment)

    def test_withdrawal_projects_state_and_decision(self):
        insight = self.submit("ee" * 16)
        record = self.project.insights.withdraw(
            insight["id"], source="writer", reason="No longer needed")
        decision_object = self.project.kernel_objects.read(
            "decision", decision_object_id(record))
        self.assertEqual(decision_object["fields"]["action"], "withdraw")
        self.assertEqual(decision_object["fields"]["reason"], "No longer needed")
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["state"], "withdrawn")
        self.assertEqual(mirrored["actor"], "role:writer")

    def test_non_hex_legacy_ids_project_under_deterministic_kernel_ids(self):
        insight = self.submit("topic-one")
        kernel_id = insight_object_id(insight)
        self.assertEqual(len(kernel_id), 32)
        self.assertRegex(kernel_id, r"\A[0-9a-f]{32}\Z")
        self.assertNotEqual(kernel_id, insight["id"])
        mirrored = self.project.kernel_objects.read("insight", kernel_id)
        self.assertEqual(mirrored["fields"]["title"], insight["title"])
        decision = self.project.insights.decide(
            insight["id"], "accept", "Accepted", "", "test-run",
            expected_rev=1, work_item_intent="none")
        recorded = self.project.kernel_objects.read("decision", decision["decision_key"])
        self.assertEqual(recorded["fields"]["insight"], kernel_id)
        self.assertEqual(self.project.kernel_objects.read("insight", kernel_id)["state"], "accepted")
        self.assertTrue(self.project.insights.kernel_shadow.verify(insight, decision)["ok"])
        self.assertEqual(self.project.insights.read(insight["id"])["id"], "topic-one")

    def test_readdress_projects_holder_and_keeps_decision_mirror_working(self):
        insight = self.submit("b" * 32)
        revised = self.project.insights.revise(
            insight["id"], source="writer", title="Migrate proposals",
            body="Mirror the accepted facts", base_snapshot=None,
            expected_rev=1, addressee="observer")
        self.assertEqual(revised["addressee"], "observer")
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["fields"]["holder"], "role:observer")
        decision = self.project.insights.decide(
            insight["id"], "accept", "Accepted by the new holder", "", "test-run",
            expected_rev=2, work_item_intent="none")
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "accepted")
        self.assertEqual(self.project.kernel_objects.read(
            "decision", decision["decision_key"])["fields"]["action"], "accept")
        self.assertTrue(self.project.insights.kernel_shadow.verify(revised, decision)["ok"])

    def test_reprojection_repairs_field_divergence_on_submitted_insight(self):
        insight = self.submit("a" * 32)
        current = self.project.kernel_objects.read("insight", insight["id"])
        self.project.kernel_objects.transition(
            "insight", insight["id"], "revise", expected_rev=current["rev"],
            fields={"title": "CORRUPTED", "body": "CORRUPTED", "urgency": "low"},
            actor="role:writer")
        divergent = self.project.insights.kernel_shadow.verify(insight)
        self.assertFalse(divergent["ok"])
        self.assertEqual(len(divergent["mismatches"]), 3)
        args = parser().parse_args(["kernel-shadow", "shadow", insight["id"], "--reproject"])
        report = _cmd_kernel_shadow(self.config, self.project, args)
        self.assertTrue(report["verification"]["ok"])
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["fields"],
                         insight_fields(insight))

    def test_undecided_insight_state_change_on_disk_is_detected(self):
        insight = self.submit("ud" * 16)
        shadow = self.project.insights.kernel_shadow
        self.assertIsNone(self.project.insights.decision(insight["id"]))
        self.assertTrue(shadow.verify(insight)["ok"])
        self.corrupt_state("insight", insight_object_id(insight), "accepted")
        result = shadow.verify(insight)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"],
                         ["insight state is accepted, expected pending"])
        args = parser().parse_args(["kernel-shadow", self.project.name, insight["id"]])
        report = _cmd_kernel_shadow(self.config, self.project, args)
        self.assertFalse(report["verification"]["ok"])
        self.assertIn("insight state is accepted, expected pending",
                      report["verification"]["mismatches"])
        self.assertEqual(self.project.insights.read(insight["id"])["id"], insight["id"])

    def test_defer_without_revisit_keeps_pending_base_state_on_disk(self):
        insight = self.submit("df" * 16)
        decision = self.project.insights.decide(
            insight["id"], "defer", "Wait", "revisit next Tuesday", "test-run",
            expected_rev=1)
        path = (self.project.root / "objects" / "insight" / insight_object_id(insight)
                / "00000001.json")
        self.assertEqual(json.loads(path.read_text())["state"], "pending")
        shadow = self.project.insights.kernel_shadow
        self.assertTrue(shadow.verify(insight, decision)["ok"])
        self.corrupt_state("insight", insight_object_id(insight), "deferred")
        result = shadow.verify(insight, decision)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"],
                         ["insight state is deferred, expected pending"])

    def test_unparseable_defer_is_not_a_verification_divergence(self):
        insight = self.submit("d" * 32)
        decision = self.project.insights.decide(
            insight["id"], "defer", "Wait", "revisit next Tuesday", "test-run",
            expected_rev=1)
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "pending")
        result = self.project.insights.kernel_shadow.verify(insight, decision)
        self.assertTrue(result["ok"], result["mismatches"])

    def test_defer_then_withdraw_updates_state_and_clears_revisit(self):
        insight = self.submit("ab" * 16)
        moment = "2099-01-01T00:00:00+00:00"
        self.project.insights.decide(
            insight["id"], "defer", "Wait", "deadline at " + moment, "test-run",
            expected_rev=1)
        self.assertEqual(self.project.kernel_objects.next_instant(), moment)
        record = self.project.insights.withdraw(
            insight["id"], source="writer", reason="Not needed", expected_rev=1)
        mirrored = self.project.kernel_objects.read("insight", insight["id"])
        self.assertEqual(mirrored["state"], "withdrawn")
        self.assertIsNone(mirrored["fields"].get("revisit_at"))
        self.assertIsNone(self.project.kernel_objects.next_instant())
        decision = self.project.kernel_objects.read("decision", decision_object_id(record))
        self.assertEqual(decision["fields"]["action"], "withdraw")
        self.assertTrue(self.project.insights.kernel_shadow.verify(insight, record)["ok"])

    def test_rejected_decision_fields_leave_insight_state_unchanged(self):
        insight = self.submit("e" * 32)
        decision = self.project.insights.decide(
            insight["id"], "accept", "x" * 9000, "", "test-run",
            expected_rev=1, work_item_intent="none")
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "pending")
        self.assertEqual(self.project.kernel_objects.revisions(
            "decision", decision_object_id(decision)), [])
        result = self.project.insights.kernel_shadow.verify(insight, decision)
        self.assertFalse(result["ok"])
        self.assertEqual(self.project.insights.kernel_shadow.failures[-1]["operation"],
                         "decided")

    def test_transition_failure_leaves_replayable_decision_record(self):
        insight = self.submit("f" * 32)
        shadow = self.project.insights.kernel_shadow
        with patch.object(self.project.kernel_objects, "transition",
                          side_effect=Denied("derived transition failure")):
            decision = self.project.insights.decide(
                insight["id"], "accept", "Accepted", "", "test-run",
                expected_rev=1, work_item_intent="none")
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "pending")
        recorded = self.project.kernel_objects.read("decision", decision_object_id(decision))
        self.assertEqual(recorded["fields"]["action"], "accept")
        shadow.clear_failures(insight["id"])
        shadow.decided(insight, decision)
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "accepted")
        self.assertTrue(shadow.verify(insight, decision)["ok"])

    def test_projection_failure_does_not_change_legacy_authority(self):
        with patch.object(self.project.kernel_objects, "create", side_effect=Denied("derived failure")):
            insight = self.submit("cc" * 16)
        self.assertEqual(self.project.insights.read(insight["id"])["title"], "Migrate proposals")
        self.assertEqual(self.project.kernel_objects.revisions("insight", insight["id"]), [])
        failures = self.project.insights.kernel_shadow.failures
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["operation"], "submitted")
        self.assertEqual(failures[0]["insight"], insight["id"])
        self.assertEqual(failures[0]["error"], "Denied")

    def test_verify_detects_divergence_and_operator_reprojection_repairs_it(self):
        insight = self.submit("ff" * 16)
        decision = self.project.insights.decide(
            insight["id"], "accept", "Accepted", "", "test-run",
            expected_rev=1, work_item_intent="none")
        shadow = self.project.insights.kernel_shadow
        self.assertTrue(shadow.verify(insight, decision)["ok"])
        current = self.project.kernel_objects.read("insight", insight["id"])
        self.project.kernel_objects.transition(
            "insight", insight["id"], "revise", expected_rev=current["rev"],
            fields={}, actor="role:writer")
        divergent = shadow.verify(insight, decision)
        self.assertFalse(divergent["ok"])
        self.assertIn("insight state is pending, expected accepted", divergent["mismatches"])

        args = parser().parse_args(["kernel-shadow", "shadow", insight["id"], "--reproject"])
        report = _cmd_kernel_shadow(self.config, self.project, args)
        self.assertTrue(report["enabled"])
        self.assertTrue(report["verification"]["ok"])
        self.assertEqual(report["failures"], [])
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["state"],
                         "accepted")
