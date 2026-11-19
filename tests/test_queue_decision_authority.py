"""Host-only queue decision authority, gate, acknowledgement and exclusion."""
import contextlib
import io
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src and tests on the path)
from support import ScriptDriver, finish_with_decision_acks
from mizu import decision_delivery, decision_reconciliation, queue_decision_delivery
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.runtime import Engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_activate  # noqa: E402  (example userland host gate)
import stage2_decision_adapter  # noqa: E402,F401  (shim preserves stage-2 parity tests)
import stage2_verify  # noqa: E402  (example userland module)
import test_stage2_decision_adapter as stage2_tests  # noqa: E402  (fixture helper module)

ACCEPT_ID = stage2_tests.ACCEPT_ID
ACTIONS = stage2_tests.ACTIONS
PENDING_ID = stage2_tests.PENDING_ID


class QueueDecisionAuthority(unittest.TestCase):
    def setUp(self):
        source = stage2_tests.Stage2DecisionAdapter(methodName="runTest")
        source.setUp()
        self.addCleanup(source.doCleanups)
        self.project, self.file = source.project, source.file
        self.config = source.config
        self.source = source
        self.role = replace(self.config.roles["worker"], decision_events=ACTIONS)
        self.conversion = convert_legacy.apply(self.project)
        self.verification = stage2_verify.verify(self.project)

    def activate(self):
        return decision_reconciliation.activate_queue_decision_delivery(
            self.project, conversion=self.conversion, verification=self.verification)

    def receipts(self):
        return self.project.kernel_queues.read("receipts", "host")["messages"]

    def test_gate_requires_applied_conversion_and_passing_verify(self):
        for changes in ({"applied": False}, {"unbound_receipts": ["f" * 64]}):
            with self.subTest(changes=changes):
                with self.assertRaises(Denied):
                    decision_reconciliation.activate_queue_decision_delivery(
                        self.project, conversion={**self.conversion, **changes},
                        verification=self.verification)
        with self.assertRaises(Denied):
            decision_reconciliation.activate_queue_decision_delivery(
                self.project, conversion=self.conversion,
                verification={**self.verification, "ok": False})
        with self.assertRaises(Denied):
            decision_reconciliation.activate_queue_decision_delivery(
                self.project, conversion=self.conversion,
                verification={**self.verification, "project": "other"})
        self.assertIsNone(decision_delivery.authority_kind(self.project.insights.root))

    def test_operator_activation_command_applies_gate(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = stage2_activate.main([str(self.file), "--project", "sample"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["authority"], "queues")
        self.assertTrue(report["verification"]["ok"])
        self.assertEqual(decision_delivery.authority_kind(self.project.insights.root), "queues")

    def test_activation_selects_queue_reads_and_retry_reuses_marker(self):
        marker = self.activate()
        self.assertEqual(marker["authority"], "queues")
        self.assertEqual(decision_delivery.authority_kind(self.project.insights.root), "queues")
        page = decision_delivery.pending(self.project, self.role)
        want = queue_decision_delivery.pending(self.project, self.role)
        self.assertEqual(canonical(page), canonical(want))
        with (mock.patch.object(self.project.insights, "decision_events_with_diagnostics",
                                side_effect=AssertionError("legacy decision read")),
              mock.patch.object(self.project.insights, "pending_insight_events",
                                side_effect=AssertionError("legacy attention read"))):
            self.assertEqual(canonical(decision_delivery.pending(self.project, self.role)),
                             canonical(want))
        retained = [(self.project.root / name).read_bytes()
                    for name in (decision_delivery.AUTHORITY_FILE,
                                 decision_delivery.PLAN_FILE, decision_delivery.REPORT_FILE)]
        self.assertEqual(self.activate(), marker)
        self.assertEqual([(self.project.root / name).read_bytes()
                          for name in (decision_delivery.AUTHORITY_FILE,
                                       decision_delivery.PLAN_FILE,
                                       decision_delivery.REPORT_FILE)], retained)

    def test_queue_activation_excludes_shared_activation(self):
        self.activate()
        with self.assertRaisesRegex(Denied, "shared activation is closed"):
            decision_reconciliation.activate_decision_delivery(self.project)
        self.assertEqual(decision_delivery.authority_kind(self.project.insights.root), "queues")

    def test_shared_activation_excludes_queue_activation(self):
        roles = {}
        for name, role in self.config.roles.items():
            actions = ACTIONS if name == "worker" else ()
            roles[name] = replace(role, decision_events=actions,
                                  decision_page_bytes=65536 if actions else None)
        config = replace(self.config, roles=roles)
        self.project.config = config
        marker = decision_reconciliation.activate_decision_delivery(self.project)
        self.assertEqual(marker["authority"], "shared")
        with self.assertRaisesRegex(Denied, "queue activation is closed"):
            self.activate()
        self.assertEqual(decision_delivery.authority_kind(self.project.insights.root), "shared")

    def test_queue_receipts_are_exact_schema_valid_and_idempotent(self):
        self.activate()
        page = decision_delivery.pending(self.project, self.role)
        decision = next(event for event in page["events"] if event.get("insight") == ACCEPT_ID)
        pending = next(event for event in page["events"] if event.get("insight") == PENDING_ID)
        queue_decision_delivery.acknowledge(self.project, self.role, [decision, pending])
        first = self.receipts()
        self.assertEqual(len(first), 2)
        by_token = {row["body"]["ack_token"]: row["body"] for row in first}
        self.assertEqual(by_token[decision["ack_token"]],
                         {"ack_token": decision["ack_token"], "legacy_role": "worker",
                          "legacy_action": "accept"})
        self.assertEqual(by_token[pending["ack_token"]]["legacy_action"], "pending")
        self.assertTrue(all(row["author"] == "host" and row["refs"] == []
                            and row["deliver_at"] is None for row in first))
        self.assertFalse((self.project.root / "decision-receipts" / "worker").exists())
        queue_decision_delivery.acknowledge(self.project, self.role, [decision, pending])
        self.assertEqual(self.receipts(), first)
        delivered = {event.get("insight") for event in
                     decision_delivery.pending(self.project, self.role)["events"]}
        self.assertNotIn(ACCEPT_ID, delivered)
        self.assertNotIn(PENDING_ID, delivered)

    def test_runtime_finish_writes_only_queue_receipts(self):
        self.activate()
        config = replace(self.config,
                         roles={**self.config.roles, "worker": self.role})
        self.project.config = config
        result = Engine(config, driver=ScriptDriver(finish_with_decision_acks)).run(
            self.project, "worker")
        self.assertEqual(result["status"], "completed")
        rows = self.receipts()
        self.assertTrue(rows)
        self.assertTrue(all(row["author"] == "host" for row in rows))
        self.assertFalse((self.project.root / "decision-receipts" / "worker").exists())

    def test_ordinary_submit_and_decide_reach_queue_after_activation(self):
        """The reported failure: legacy writes never reached the runtime queue."""
        self.activate()
        role = self.role
        snap = self.project.snapshots.get()["id"]
        identity = "f" * 32
        before_proposals = self.project.kernel_queues.read("proposals", "operator")["head"]
        before_decisions = self.project.kernel_queues.read("decisions", "operator")["head"]

        # An ordinary submit addressed to the worker must become a deliverable
        # pending event, byte-identical to the legacy envelope it replaces.
        record = self.project.insights.submit(
            source="worker", title="Queued live proposal", body="Queued live body",
            base_snapshot=snap, insight_id=identity,
            kind="proposal", addressee="worker", urgency="high")
        self.assertEqual(record["rev"], 1)
        queue = self.project.kernel_queues.read("proposals", "operator")["messages"]
        self.assertEqual(self.project.kernel_queues.read("proposals", "operator")["head"],
                         before_proposals + 1)
        self.assertEqual(queue[-1]["id"],
                         queue_decision_delivery.message_id("proposals", identity, 1))
        page = decision_delivery.pending(self.project, role)
        pending = [event for event in page["events"] if event.get("insight") == identity]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "insight.pending")
        self.assertEqual(pending[0]["target"], "worker")
        configured = {"worker", "reviewer"}
        legacy = dict(self.project.insights._pending_insight_envelopes_locked(configured, "worker"))
        want = [envelope["payload"] for envelope in legacy.values()
                if envelope["payload"]["insight"] == identity]
        self.assertEqual(len(want), 1)
        self.assertEqual(canonical({k: v for k, v in pending[0].items() if k != "ack_token"}),
                         canonical(want[0]))
        self.assertEqual(pending[0]["ack_token"],
                         self.project.insights._decision_token("worker", dict(want[0])))

        # An ordinary accept must become a decision event on the same queue.
        self.project.insights.decide(identity, "accept", "Queued live reason", "",
                                     "run-live", expected_rev=1, work_item_intent="none")
        decisions = self.project.kernel_queues.read(
            "decisions", "operator", now=queue_decision_delivery.NOW)["messages"]
        self.assertEqual(self.project.kernel_queues.read("decisions", "operator")["head"],
                         before_decisions + 1)
        self.assertEqual(decisions[-1]["id"],
                         queue_decision_delivery.message_id("decisions", identity, 1))
        page = decision_delivery.pending(self.project, role)
        decision = [event for event in page["events"] if event.get("insight") == identity]
        self.assertEqual(len(decision), 1)
        self.assertEqual(decision[0]["action"], "accept")
        projections = dict(self.project.insights._decision_projection_envelopes_locked())
        want = [envelope["payload"] for envelope in projections.values()
                if envelope["payload"]["insight"] == identity]
        self.assertEqual(len(want), 1)
        self.assertEqual(canonical({k: v for k, v in decision[0].items() if k != "ack_token"}),
                         canonical(want[0]))
        self.assertEqual(decision[0]["ack_token"],
                         self.project.insights._decision_token("worker", dict(want[0])))

        # Runtime delivery/ack: queue receipts suppress the event, no legacy file.
        queue_decision_delivery.acknowledge(self.project, role, decision)
        self.assertTrue(any(row["body"]["ack_token"] == decision[0]["ack_token"]
                            for row in self.receipts()))
        self.assertFalse((self.project.root / "decision-receipts" / "worker").exists())
        delivered = {event.get("insight") for event in
                     decision_delivery.pending(self.project, role)["events"]}
        self.assertNotIn(identity, delivered)

    def test_normal_insight_tools_route_through_queue_after_activation(self):
        """The ordinary insight_tools submit/decide callers use the same producer."""
        from mizu import insight_tools
        self.activate()
        ctx = self.source.context("worker")
        before = self.project.kernel_queues.read("proposals", "operator")["head"]
        submitted = insight_tools.submit_insight(ctx, {"title": "Tool proposal", "body": "Tool body",
                                                       "addressee": "worker", "kind": "proposal",
                                                       "urgency": "high"})
        identity = submitted["id"]
        self.assertEqual(self.project.kernel_queues.read("proposals", "operator")["head"],
                         before + 1)
        page = decision_delivery.pending(self.project, self.role)
        pending = [event for event in page["events"] if event.get("insight") == identity]
        self.assertEqual(len(pending), 1)
        insight_tools.decide(ctx, {"id": identity, "action": "accept",
                                   "reason": "Tool reason", "rev": 1,
                                   "work_item_intent": "none"})
        page = decision_delivery.pending(self.project, self.role)
        decision = [event for event in page["events"] if event.get("insight") == identity]
        self.assertEqual(len(decision), 1)
        self.assertEqual(decision[0]["action"], "accept")

    def test_queue_activation_fails_closed_without_live_producer(self):
        """Readers never switch unless the live writer switches with them."""
        with mock.patch.object(self.project.insights, "decision_queue_writer", None):
            with self.assertRaisesRegex(Denied, "producer is not wired"):
                self.activate()
        self.assertIsNone(decision_delivery.authority_kind(self.project.insights.root))
        # With the producer wired, the same reports activate cleanly.
        self.assertEqual(self.activate()["authority"], "queues")

    def test_defer_writes_immediate_and_delayed_queue_messages(self):
        """A deferral keeps the delayed revisit delivery semantics on the queue."""
        self.activate()
        snap = self.project.snapshots.get()["id"]
        identity = "9" * 32
        self.project.insights.submit(source="worker", title="Defer me", body="Defer body",
                                     base_snapshot=snap, insight_id=identity,
                                     addressee="worker")
        before = self.project.kernel_queues.read("decisions", "operator")["head"]
        self.project.insights.decide(identity, "defer", "Defer reason", stage2_tests.REVISIT_AT,
                                     "run-defer", expected_rev=1)
        decisions = self.project.kernel_queues.read(
            "decisions", "operator", now=queue_decision_delivery.NOW)["messages"]
        self.assertEqual(self.project.kernel_queues.read("decisions", "operator")["head"],
                         before + 2)
        ids = {row["id"] for row in decisions}
        self.assertIn(queue_decision_delivery.message_id("decisions", identity, 1), ids)
        self.assertIn(queue_decision_delivery.message_id("revisit", identity, 1), ids)
        proposal_id = queue_decision_delivery.message_id("proposals", identity, 1)
        delayed = [row for row in decisions
                   if row["deliver_at"] == stage2_tests.REVISIT_AT and row["refs"] == [proposal_id]]
        self.assertEqual(len(delayed), 1)

    def test_ack_revalidates_token_and_legacy_write_refuses(self):
        self.activate()
        page = decision_delivery.pending(self.project, self.role)
        decision = next(event for event in page["events"] if event.get("insight") == ACCEPT_ID)
        forged = dict(decision, reason="forged")
        with self.assertRaises(Denied):
            queue_decision_delivery.acknowledge(self.project, self.role, [forged])
        with self.assertRaises(Denied):
            queue_decision_delivery.acknowledge(self.project, self.role,
                                               [dict(decision, ack_token="0" * 64)])
        self.assertEqual(self.receipts(), [])
        with self.assertRaisesRegex(Denied, "queue activation"):
            self.project.insights.acknowledge_decisions("worker", [decision])
        self.assertEqual(self.receipts(), [])
        self.assertFalse((self.project.root / "decision-receipts" / "worker").exists())


if __name__ == "__main__":
    unittest.main()
