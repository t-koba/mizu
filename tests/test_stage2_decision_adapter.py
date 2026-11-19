"""Queue-backed decision read adapter compared with the legacy page bytes."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu import decision_delivery
from mizu.config import load
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.project import Project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_decision_adapter as adapter  # noqa: E402
import test_stage2_conversion as conversion_tests  # noqa: E402  (fixture helper module)

QUEUES = conversion_tests.QUEUES
ACCEPT_ID = "a" * 32
PENDING_ID = "b" * 32
DEFER_ID = "c" * 32
OTHER_ID = "d" * 32
REVIEWER_ID = "e" * 32
REVISIT_AT = "2099-01-01T00:00:00+00:00"
ACTIONS = ("accept", "modify", "defer", "reject")


class Stage2DecisionAdapter(Fixture):
    def setUp(self):
        super().setUp()
        shutil.copyfile(QUEUES, self.file.parent / "queues.toml")
        self.file.write_text('include = ["queues.toml"]\n' + self.file.read_text())
        self.config = load(self.file)
        self.project = Project(self.config, "sample")
        snap = self.project.snapshots.get()
        self.project.insights.submit(
            source="worker", title="Accepted proposal", body="Accepted body",
            base_snapshot=snap["id"], insight_id=ACCEPT_ID)
        self.project.insights.attach_evidence(
            ACCEPT_ID, source="worker", text="Accepted evidence", run="run-a", expected_rev=1)
        self.project.insights.decide(
            ACCEPT_ID, "accept", "Accepted reason", "", "run-a", expected_rev=1,
            work_item_intent="none")
        self.project.insights.submit(
            source="worker", title="Deferred proposal", body="Deferred body",
            base_snapshot=snap["id"], insight_id=DEFER_ID)
        self.project.insights.attach_evidence(
            DEFER_ID, source="worker", text="Deferred evidence", run="run-c", expected_rev=1)
        self.project.insights.decide(
            DEFER_ID, "defer", "Deferred reason", REVISIT_AT, "run-c", expected_rev=1)
        self.project.insights.submit(
            source="worker", title="Attention proposal", body="Attention body",
            base_snapshot=snap["id"], insight_id=PENDING_ID,
            kind="proposal", addressee="worker", urgency="high")
        self.project.insights.submit(
            source="worker", title="Writer-addressed proposal", body="Writer body",
            base_snapshot=snap["id"], insight_id=OTHER_ID)
        self.project.insights.submit(
            source="reviewer", title="Reviewer proposal", body="Reviewer body",
            base_snapshot=snap["id"], insight_id=REVIEWER_ID)
        self.project.insights.decide(
            REVIEWER_ID, "reject", "Reviewer reason", "", "run-e", expected_rev=1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 0)

    def role(self, *, sources="own", actions=ACTIONS):
        return replace(self.config.roles["worker"], decision_events=actions,
                       decision_sources=sources)

    def parity(self, **kwargs):
        """Compare canonical bytes of the legacy and queue-backed pages."""
        role = kwargs.pop("role", None)
        sources = kwargs.pop("sources", None)
        if role is None:
            role = self.role(sources=sources or "own")
        elif sources is not None:
            role = replace(role, decision_sources=sources)
        want = decision_delivery.pending(self.project, role, **kwargs)
        if sources is None:
            got = adapter.pending(self.project, role, **kwargs)
        else:
            got = adapter.pending(self.project, role, sources=sources, **kwargs)
        self.assertEqual(canonical(want), canonical(got))
        self.assertEqual(want, got)
        return got

    def test_decision_and_pending_events_match_legacy_bytes(self):
        page = self.parity()
        self.assertEqual(page["shared"], False)
        self.assertEqual(page["manifest"], ())
        self.assertEqual(page["page"], None)
        self.assertEqual({event["action"] for event in page["events"]
                          if event.get("kind") != "insight.pending"}, {"accept", "defer"})
        pending = [event for event in page["events"] if event.get("kind") == "insight.pending"]
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(event["insight_kind"] == "question" or event["urgency"] == "high"
                            or event["addressee"] == "writer" for event in pending))

    def test_actions_filtering(self):
        page = self.parity(actions=("reject",))
        self.assertEqual([event["action"] for event in page["events"]
                          if event.get("kind") != "insight.pending"], [])
        page = self.parity(actions=("accept",))
        self.assertEqual([event["action"] for event in page["events"]
                          if event.get("kind") != "insight.pending"], ["accept"])

    def test_sources_own_and_any(self):
        own = self.parity(sources="own")
        self.assertNotIn(REVIEWER_ID, [event["insight"] for event in own["events"]])
        any_page = self.parity(role=self.role(sources="any"), sources="any")
        self.assertIn(REVIEWER_ID, [event["insight"] for event in any_page["events"]])

    def test_runtime_call_default_sources_follow_role_configuration(self):
        for sources in ("own", "any"):
            with self.subTest(sources=sources):
                role = self.role(sources=sources)
                want = decision_delivery.pending(self.project, role)
                got = adapter.pending(self.project, role)
                self.assertEqual(canonical(want), canonical(got))
                self.assertEqual(want, got)
        role = self.role(sources="any")
        self.assertIn(REVIEWER_ID, [event["insight"] for event in
                                    adapter.pending(self.project, role)["events"]])

    def test_limits_truncation_and_no_insights(self):
        for limit in (1, 2, 3, 10):
            with self.subTest(limit=limit):
                self.parity(limit=limit)
        self.parity(include_insights=False)
        self.parity(actions=("accept",), include_insights=False, limit=1)

    def test_byte_truncation_page_matches_legacy(self):
        with mock.patch.object(adapter, "MAX_FRAME", 300), \
                mock.patch.object(decision_delivery, "MAX_FRAME", 300):
            want = decision_delivery.pending(self.project, self.role())
            got = adapter.pending(self.project, self.role())
        self.assertEqual(canonical(want), canonical(got))
        self.assertIsNotNone(got["page"])
        self.assertTrue(got["page"]["byte_truncated"])
        self.assertTrue(all(event.get("kind") != "insight.pending"
                            for event in got["events"]))

    def test_malformed_converted_metadata_refuses(self):
        # File tampering keeps the adapter's real queue reads in play.
        def rewrite(name, number, change):
            path = (self.project.root / "queues" / name / "messages" /
                    f"{number:020d}.json")
            original = path.read_bytes()
            message = json.loads(original)
            change(message)
            path.write_bytes(canonical(message))
            return path, original

        cases = (
            ("decisions", 1, lambda m: m["body"].update(legacy_event="g" * 32),
             "invalid legacy_event"),
            ("decisions", 1, lambda m: m["body"].update(legacy_record="not canonical json"),
             "canonical JSON"),
            ("decisions", 1, lambda m: m["body"].update(legacy_run="not the recorded run"),
             "run does not bind"),
            ("proposals", 3, lambda m: m["body"].update(
                legacy_created_at="2099-01-01T00:00:00"), "invalid legacy_created_at"),
            ("proposals", 3, lambda m: m["body"].update(legacy_urgency="urgent"),
             "invalid legacy_urgency"),
        )
        for name, number, change, expected in cases:
            with self.subTest(name=name, number=number, expected=expected):
                path, original = rewrite(name, number, change)
                try:
                    with self.assertRaises(Denied) as refused:
                        adapter.pending(self.project, self.role())
                    self.assertIn(expected, str(refused.exception))
                finally:
                    path.write_bytes(original)

    def test_other_corrupt_reasons_match_legacy_diagnostics(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == DEFER_ID)
        pending = next(event for event in before["events"]
                       if event.get("insight") == OTHER_ID)
        parent = self.project.root / "decision-receipts" / "worker"
        parent.mkdir(parents=True, exist_ok=True)
        (parent / f"{decision['ack_token']}.json").write_bytes(
            canonical({"role": "worker"}))
        (parent / f"{pending['ack_token']}.json").write_bytes(canonical({
            "role": "worker", "event": pending["event"],
            "ack_token": pending["ack_token"], "acknowledged_at": "not-an-instant"}))
        report = self.convert()
        self.assertEqual(len(report["receipts"]), 2)
        reasons = {message["body"]["ack_token"]: message["body"]["legacy_reason"]
                   for message in self.project.kernel_queues.read(
                       "receipts", "operator")["messages"]}
        self.assertEqual(reasons, {decision["ack_token"]: "invalid_receipt",
                                   pending["ack_token"]: "invalid_timestamp"})
        want, got = self.parity_page()
        self.assertEqual(canonical(want["diagnostics"]), canonical(got["diagnostics"]))
        self.assertEqual({item["ack_token"]: item["reason"] for item in got["diagnostics"]},
                         reasons)

    def convert(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def legacy_page(self, **kwargs):
        return decision_delivery.pending(self.project, self.role(), **kwargs)

    def parity_page(self, **kwargs):
        want = self.legacy_page(**kwargs)
        got = adapter.pending(self.project, self.role(), **kwargs)
        self.assertEqual(canonical(want), canonical(got))
        self.assertEqual(want, got)
        return want, got

    def test_no_receipts_matches_legacy(self):
        """An explicit zero-receipt case stays byte-identical to legacy."""
        self.assertFalse((self.project.root / "decision-receipts" / "worker").exists())
        self.assertEqual(self.project.kernel_queues.read(
            "receipts", "operator")["messages"], [])
        want, got = self.parity_page()
        self.assertEqual(want["diagnostics"], [])
        self.assertEqual(got["diagnostics"], [])

    def test_valid_receipts_suppress_decision_and_pending_events(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == ACCEPT_ID)
        pending = next(event for event in before["events"]
                       if event.get("insight") == PENDING_ID)
        self.project.insights.acknowledge_decisions("worker", [decision, pending])
        report = self.convert()
        self.assertEqual(len(report["receipts"]), 2)
        self.assertEqual(report["unbound_receipts"], [])
        want, got = self.parity_page()
        self.assertEqual(want["diagnostics"], [])
        delivered = [event.get("insight") for event in got["events"]]
        self.assertNotIn(ACCEPT_ID, delivered)
        self.assertNotIn(PENDING_ID, delivered)
        self.assertEqual([event["insight"] for event in want["events"]], delivered)

    def test_corrupt_receipts_match_legacy_diagnostics_and_suppression(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == ACCEPT_ID)
        pending = next(event for event in before["events"]
                       if event.get("insight") == PENDING_ID)
        parent = self.project.root / "decision-receipts" / "worker"
        parent.mkdir(parents=True, exist_ok=True)
        for event in (decision, pending):
            (parent / f"{event['ack_token']}.json").write_bytes(b"{invalid-json")
        report = self.convert()
        self.assertEqual(len(report["receipts"]), 2)
        stored = self.project.kernel_queues.read("receipts", "operator")["messages"]
        body_by_token = {message["body"]["ack_token"]: message["body"] for message in stored}
        self.assertEqual(set(body_by_token), {decision["ack_token"], pending["ack_token"]})
        candidates = convert_legacy._receipt_candidates(self.project, "worker")
        for item, action in ((decision, "accept"), (pending, "pending")):
            body = body_by_token[item["ack_token"]]
            self.assertEqual(body["ack_token"], item["ack_token"])
            self.assertEqual(body["legacy_role"], "worker")
            self.assertEqual(body["legacy_action"], action)
            self.assertEqual(body["legacy_reason"], "malformed_json")
            self.assertRegex(body["legacy_order"], r"[0-9a-f]{8}\Z")
            self.assertEqual(body["legacy_order"], candidates[item["ack_token"]][2])
            self.assertEqual(set(body), {"ack_token", "legacy_role", "legacy_action",
                                         "legacy_reason", "legacy_order"})
        want, got = self.parity_page()
        expected = [{"event": item["event"], "ack_token": item["ack_token"],
                     "action": action, "reason": "malformed_json"}
                    for item, action in ((decision, "accept"), (pending, "pending"))]
        self.assertEqual(canonical(want["diagnostics"]), canonical(got["diagnostics"]))
        self.assertEqual(got["diagnostics"], expected)
        delivered = [event.get("insight") for event in got["events"]]
        self.assertNotIn(ACCEPT_ID, delivered)
        self.assertNotIn(PENDING_ID, delivered)

    def test_two_corrupt_decisions_match_legacy_bytes_in_legacy_order(self):
        """Two corrupt decision receipts stay byte-identical in legacy order."""
        before = self.legacy_page()
        first = next(event for event in before["events"]
                     if event.get("insight") == ACCEPT_ID)
        second = next(event for event in before["events"]
                      if event.get("insight") == DEFER_ID)
        parent = self.project.root / "decision-receipts" / "worker"
        parent.mkdir(parents=True, exist_ok=True)
        for event in (first, second):
            (parent / f"{event['ack_token']}.json").write_bytes(b"{invalid-json")
        report = self.convert()
        self.assertEqual(len(report["receipts"]), 2)
        stored = self.project.kernel_queues.read("receipts", "operator")["messages"]
        orders = {message["body"]["ack_token"]: message["body"]["legacy_order"]
                  for message in stored}
        self.assertEqual(set(orders), {first["ack_token"], second["ack_token"]})
        for order in orders.values():
            self.assertRegex(order, r"[0-9a-f]{8}\Z")
        candidates = convert_legacy._receipt_candidates(self.project, "worker")
        for token, order in orders.items():
            self.assertEqual(order, candidates[token][2])
        want, got = self.parity_page()
        self.assertEqual(canonical(want["diagnostics"]), canonical(got["diagnostics"]))
        self.assertEqual(want, got)
        self.assertEqual([item["ack_token"] for item in got["diagnostics"]],
                         sorted([item["ack_token"] for item in got["diagnostics"]],
                                key=lambda token: orders[token]))
        delivered = [event.get("insight") for event in got["events"]]
        self.assertNotIn(ACCEPT_ID, delivered)
        self.assertNotIn(DEFER_ID, delivered)

    def test_diagnostics_cap_and_page_copy(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == ACCEPT_ID)
        parent = self.project.root / "decision-receipts" / "worker"
        parent.mkdir(parents=True, exist_ok=True)
        (parent / f"{decision['ack_token']}.json").write_bytes(b"{invalid-json")
        self.convert()
        with mock.patch.object(self.project.insights, "decision_receipt_diagnostics", 0):
            want, got = self.parity_page()
        self.assertEqual(want["diagnostics"], [])
        self.assertEqual(got["diagnostics"], [])
        with mock.patch.object(adapter, "MAX_FRAME", 300), \
                mock.patch.object(decision_delivery, "MAX_FRAME", 300):
            got = adapter.pending(self.project, self.role())
            legacy = decision_delivery.pending(self.project, self.role())
        self.assertEqual(canonical(legacy), canonical(got))
        self.assertEqual(canonical(legacy["diagnostics"]), canonical(got["diagnostics"]))
        self.assertIsNotNone(got["page"])
        self.assertNotIn("diagnostics", got["page"])

    def test_explicit_null_receipt_reason_refuses(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == ACCEPT_ID)
        self.project.insights.acknowledge_decisions("worker", [decision])
        self.convert()
        messages = (self.project.root / "queues" / "receipts" / "messages")
        for path in sorted(messages.glob("*.json")):
            message = json.loads(path.read_bytes())
            if message["body"]["ack_token"] == decision["ack_token"]:
                message["body"]["legacy_reason"] = None
                path.write_bytes(canonical(message))
                break
        else:
            self.fail("Converted receipt message not found")
        with self.assertRaises(Denied):
            adapter.pending(self.project, self.role())

    def test_oversized_receipt_refuses_the_whole_conversion(self):
        before = self.legacy_page()
        decision = next(event for event in before["events"]
                        if event.get("insight") == ACCEPT_ID)
        parent = self.project.root / "decision-receipts" / "worker"
        parent.mkdir(parents=True, exist_ok=True)
        receipt = parent / f"{decision['ack_token']}.json"
        receipt.write_bytes(b"x" * 1025)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertIn("conversion refused", errors.getvalue())
        self.assertEqual(self.project.kernel_queues.read("receipts", "operator")["messages"], [])

    def test_work_item_decision_and_other_role_addressee(self):
        """A rich history record and another role's attention still reconstruct."""
        snap = self.project.snapshots.get()
        rich = "f" * 32
        self.project.insights.submit(
            source="worker", title="Work proposal", body="Work body",
            base_snapshot=snap["id"], insight_id=rich)
        self.project.insights.decide(
            rich, "accept", "Work reason", "", "run-f", expected_rev=1,
            work_item_intent="create")
        addressed = "1" * 32
        self.project.insights.submit(
            source="worker", title="For reviewer", body="Reviewer attention",
            base_snapshot=snap["id"], insight_id=addressed,
            addressee="reviewer", urgency="low")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 0)
        self.parity()
        reviewer = replace(self.config.roles["reviewer"], decision_events=ACTIONS,
                           decision_sources="own")
        want = decision_delivery.pending(self.project, reviewer)
        got = adapter.pending(self.project, reviewer)
        self.assertEqual(canonical(want), canonical(got))
        self.assertIn(addressed, [event["insight"] for event in got["events"]])

    def test_reads_do_not_move_consumer_cursors(self):
        store = self.project.kernel_queues
        before = {name: (store.root / "queues" / name / "cursors" / "role:worker.json").exists()
                  for name in ("proposals", "decisions")}
        adapter.pending(self.project, self.role())
        after = {name: (store.root / "queues" / name / "cursors" / "role:worker.json").exists()
                 for name in ("proposals", "decisions")}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
