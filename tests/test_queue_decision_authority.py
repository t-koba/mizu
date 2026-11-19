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
