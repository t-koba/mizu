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
from mizu.config import MAX_INSIGHT_HISTORY_ENTRIES
from mizu.errors import Denied
from mizu.fs import canonical, digest
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
OTHER_ID = stage2_tests.OTHER_ID


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
                         queue_decision_delivery.message_id(queue_decision_delivery.PROPOSAL_KIND, identity, 1))
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
                         queue_decision_delivery.message_id(queue_decision_delivery.DECISION_KIND, identity, 1))
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
        self.assertIn(queue_decision_delivery.message_id(queue_decision_delivery.DECISION_KIND, identity, 1), ids)
        self.assertIn(queue_decision_delivery.message_id("revisit", identity, 1), ids)
        proposal_id = queue_decision_delivery.message_id(queue_decision_delivery.PROPOSAL_KIND, identity, 1)
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

    def test_converted_insight_can_be_decided_after_activation(self):
        """Conversion and live writes must share one stable message identity."""
        self.activate()
        proposal_id = queue_decision_delivery.message_id(
            queue_decision_delivery.PROPOSAL_KIND, OTHER_ID, 1)
        before = {message["id"] for message in self.project.kernel_queues.read(
            "proposals", "operator", cursor=0, now=stage2_verify.NOW)["messages"]}
        self.assertIn(proposal_id, before)

        self.project.insights.decide(
            OTHER_ID, "accept", "Decide converted insight", "", "run-converted",
            expected_rev=1, work_item_intent="none")

        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, OTHER_ID, 1)
        decisions = self.project.kernel_queues.read(
            "decisions", "operator", cursor=0, now=stage2_verify.NOW)["messages"]
        row = next(message for message in decisions if message["id"] == decision_id)
        self.assertEqual(row["refs"], [proposal_id])
        self.assertEqual(decisions.count(row), 1)
        page = decision_delivery.pending(self.project, self.role)
        event = next(event for event in page["events"] if event.get("insight") == OTHER_ID)
        self.assertEqual(event["action"], "accept")

    def test_complete_read_refuses_a_delayed_page_but_userland_keeps_prefix(self):
        """One reader serves both modes: only the administrative one fails closed."""
        store = self.project.kernel_queues
        page = {"messages": [{"id": "f" * 32}], "head": 2,
                "more": True, "next": 1, "delayed": True}
        with mock.patch.object(store, "read", return_value=page):
            with self.assertRaisesRegex(queue_decision_delivery.AdapterError,
                                        "still delayed"):
                queue_decision_delivery.read_complete(store, "decisions", "role:worker")
            self.assertEqual(queue_decision_delivery._read_queue(
                store, "decisions", "role:worker"), page["messages"])


    def test_max_instant_revisit_before_accept_matches_legacy_page(self):
        """A max-instant revisit must not hide the following ordinary accept."""
        latest = "9999-12-31T23:59:59.999999+00:00"
        defer_id, accept_id = "0" * 32, "1" * 32
        snap = self.project.snapshots.get()["id"]
        self.project.insights.submit(
            source="worker", title="Max revisit proposal", body="Max revisit body",
            base_snapshot=snap, insight_id=defer_id)
        self.project.insights.decide(
            defer_id, "defer", "Revisit at the latest instant", latest, "run-max",
            expected_rev=1)
        self.project.insights.submit(
            source="worker", title="Accept after revisit", body="Accept after revisit body",
            base_snapshot=snap, insight_id=accept_id,
            kind="proposal", addressee="worker", urgency="high")
        self.project.insights.decide(
            accept_id, "accept", "Accepted after revisit", "", "run-max",
            expected_rev=1, work_item_intent="none")

        conversion = convert_legacy.apply(self.project)
        verification = stage2_verify.verify(self.project)
        self.assertTrue(verification["ok"])
        rows = self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
        ids = [row["id"] for row in rows]
        revisit_id = queue_decision_delivery.message_id(
            queue_decision_delivery.REVISIT_KIND, defer_id, 1)
        accept_message = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, accept_id, 1)
        self.assertEqual(rows[ids.index(revisit_id)]["deliver_at"], latest)
        self.assertLess(ids.index(revisit_id), ids.index(accept_message))

        # The legacy page before activation carries the accept as a decision.
        want = decision_delivery.pending(self.project, self.role)
        self.assertEqual(decision_delivery.authority_kind(self.project.insights.root), None)
        accept_event = next(event for event in want["events"]
                            if event.get("insight") == accept_id)
        self.assertEqual(accept_event["action"], "accept")

        decision_reconciliation.activate_queue_decision_delivery(
            self.project, conversion=conversion, verification=verification)
        got = decision_delivery.pending(self.project, self.role)
        self.assertEqual(canonical(got), canonical(want))
        selected = next(event for event in got["events"] if event.get("insight") == accept_id)
        self.assertEqual(selected["ack_token"], accept_event["ack_token"])
        self.assertNotIn(
            "insight.pending",
            [event.get("kind") for event in got["events"] if event.get("insight") == accept_id])


    def test_exact_defer_replay_preserves_stable_envelope_without_duplicate(self):
        """A retry crosses the clock tick but keeps the original queue identity."""
        self.activate()
        identity = "8" * 32
        self.live_submit(identity, "Deferred for exact replay")
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-e", expected_rev=1)
        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)

        def rows():
            return {m["id"]: canonical(m["body"]) for m in
                    self.project.kernel_queues.read(
                        "decisions", "operator", now=stage2_verify.NOW)["messages"]}

        before = rows()
        self.assertIn(decision_id, before)
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-e", expected_rev=1)
        after = rows()
        self.assertEqual(after, before)
        self.assertEqual(sum(1 for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id), 1)
        self.assert_parity(identity)

    def test_exact_defer_replay_survives_retained_history_above_scan_ceiling(self):
        """A retry stays a no-op however much decision history is retained."""
        self.activate()
        identity = "3" * 32
        self.live_submit(identity, "Deferred with oversized history")
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-h", expected_rev=1)
        history = self.project.insights.root / "decision-history"
        existing = {path.name for path in history.glob("*.json")}
        for i in range(1005):
            name = f'{"e" * 24}{i:08x}.json'
            if name not in existing:
                (history / name).write_text("{}")
        self.assertGreater(len(list(history.glob("*.json"))), 1000)
        # The old bounded scan raised "Decision history recovery bound exceeded"
        # here; the stable-id message carries the stem with one keyed read.
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-h", expected_rev=1)
        events = self.assert_parity(identity)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "defer")

    def test_exact_defer_replay_uses_message_identity_without_history_scan(self):
        """The O(1) message read does not depend on retained history at all."""
        self.activate()
        identity = "4" * 32
        self.live_submit(identity, "Deferred with detached history")
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-d", expected_rev=1)
        history = self.project.insights.root / "decision-history"
        history.rename(self.project.insights.root / "decision-history-detached")
        # Without the message path the scan would raise "Decision history
        # unavailable for replay"; the stable-id message is sufficient.
        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-d", expected_rev=1)
        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)
        rows = [m for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"]["action"], "defer")

    def test_exact_defer_replay_repairs_missed_append_with_original_identity(self):
        """A retry after a lost first append restores the original legacy_event."""
        self.activate()
        identity = "9" * 32
        self.live_submit(identity, "Deferred with interrupted append")
        writer = self.project.insights.decision_queue_writer
        real_decide = writer.decide
        calls = {"count": 0}

        def drop_first_append(*args):
            calls["count"] += 1
            if calls["count"] == 1:
                return
            return real_decide(*args)

        with mock.patch.object(writer, "decide", side_effect=drop_first_append):
            self.project.insights.decide(
                identity, "defer", "why", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)
        self.assertEqual([m for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id], [])

        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        rows = [m for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id]
        self.assertEqual(len(rows), 1)
        history = self.project.insights.root / "decision-history"
        history_stems = [p.stem for p in history.glob("*.json")
                         if json.loads(p.read_text()).get("id") == identity]
        self.assertEqual(len(history_stems), 1)
        self.assertEqual(rows[0]["body"]["legacy_event"], history_stems[0])
        self.assert_parity(identity)

    def test_missed_defer_append_repairs_from_content_address_above_history_ceiling(self):
        """A lost first append is repaired by one keyed read past the old scan ceiling."""
        self.activate()
        identity = "5" * 32
        self.live_submit(identity, "Defer with lost append and oversized history")
        writer = self.project.insights.decision_queue_writer
        real_decide = writer.decide
        calls = {"count": 0}

        def drop_first_append(*args):
            calls["count"] += 1
            if calls["count"] == 1:
                return
            return real_decide(*args)

        with mock.patch.object(writer, "decide", side_effect=drop_first_append):
            self.project.insights.decide(
                identity, "defer", "why", stage2_tests.REVISIT_AT, "run-lost", expected_rev=1)
        self.assertEqual(calls["count"], 1)
        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)
        history = self.project.insights.root / "decision-history"
        pointer = self.project.insights.root / "decisions" / f"{identity}.json"
        decision = json.loads(pointer.read_bytes())
        stem = digest(canonical(decision))[:32]
        original = (history / f"{stem}.json").read_bytes()
        self.assertEqual(json.loads(original), decision)

        def decision_rows(insight_id):
            return [row for row in self.project.kernel_queues.read(
                "decisions", "operator", now=stage2_verify.NOW)["messages"]
                if row["id"] == queue_decision_delivery.message_id(
                    queue_decision_delivery.DECISION_KIND, insight_id, 1)]

        self.assertEqual(decision_rows(identity), [])
        # Unrelated retained history sorts before the content-derived stem, so
        # the old bounded scan would refuse before it reached this event.
        alphabet = "0123456789abcdef"
        pos = next(index for index, char in enumerate(stem) if char != "0")
        head = stem[:pos] + alphabet[alphabet.index(stem[pos]) - 1]
        existing = {path.name for path in history.iterdir()}
        for i in range(MAX_INSIGHT_HISTORY_ENTRIES + 5):
            name = f"{head}{i:08x}.json"
            if name not in existing:
                (history / name).write_text(
                    json.dumps({"id": "f" * 32, "action": "reject", "rev": 1}))
        unrelated = list(history.glob(f"{head}*.json"))
        self.assertGreater(len(list(history.glob("*.json"))), MAX_INSIGHT_HISTORY_ENTRIES)
        self.assertTrue(all(path.name < f"{stem}.json" for path in unrelated))

        self.project.insights.decide(
            identity, "defer", "why", stage2_tests.REVISIT_AT, "run-lost", expected_rev=1)
        self.assertEqual((history / f"{stem}.json").read_bytes(), original)
        self.assertEqual(json.loads(pointer.read_bytes()), decision)
        repaired = decision_rows(identity)
        self.assertEqual(len(repaired), 1)
        body = repaired[0]["body"]
        self.assertEqual(body["legacy_event"], stem)
        self.assertEqual(body["legacy_record"], original.decode())

        # A defer with an intact first append has the same queue body shape and
        # derives its identity from its own committed record the same way.
        peer = "6" * 32
        self.live_submit(peer, "Defer with intact first append")
        self.project.insights.decide(
            peer, "defer", "why", stage2_tests.REVISIT_AT, "run-lost", expected_rev=1)
        peer_decision = json.loads(
            (self.project.insights.root / "decisions" / f"{peer}.json").read_bytes())
        peer_rows = decision_rows(peer)
        self.assertEqual(len(peer_rows), 1)
        peer_body = peer_rows[0]["body"]
        self.assertEqual(peer_body["legacy_event"], digest(canonical(peer_decision))[:32])
        self.assertEqual(peer_body["legacy_record"], canonical(peer_decision).decode())
        self.assertEqual(set(body), set(peer_body))
        for field in ("action", "reason", "revisit", "legacy_run",
                      "legacy_source", "legacy_addressee"):
            self.assertEqual(body.get(field), peer_body.get(field))
        self.assert_parity(identity)

    def test_converted_defer_replay_keeps_retained_history_identity(self):
        """A converted defer's original history stem survives exact replay."""
        self.activate()
        identity = stage2_tests.DEFER_ID
        decision_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)
        before = [m for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id]
        self.assertEqual(len(before), 1)
        original_event = before[0]["body"]["legacy_event"]

        self.project.insights.decide(
            identity, "defer", "Deferred reason", stage2_tests.REVISIT_AT,
            "run-c", expected_rev=1)
        after = [m for m in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if m["id"] == decision_id]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["body"]["legacy_event"], original_event)
        self.assert_parity(identity)

    def legacy_page(self):
        """Read the legacy page without changing the durable activation marker."""
        with mock.patch.object(decision_delivery, "authority_kind", return_value=None):
            return decision_delivery.pending(self.project, self.role)

    def live_submit(self, identity, title):
        return self.project.insights.submit(
            source="worker", title=title, body=f"{title} body",
            base_snapshot=self.project.snapshots.get()["id"], insight_id=identity)

    def assert_parity(self, identity):
        want = self.legacy_page()
        got = decision_delivery.pending(self.project, self.role)
        self.assertEqual(canonical(want), canonical(got))
        self.assertEqual(want, got)
        return [event for event in got["events"] if event.get("insight") == identity]

    def test_withdraw_after_activation_clears_the_queue_page(self):
        self.activate()
        identity = "9" * 32
        self.live_submit(identity, "Withdrawn after activation")
        self.assertEqual([event.get("kind") for event in self.assert_parity(identity)],
                         ["insight.pending"])

        record = self.project.insights.withdraw(
            identity, source="worker", reason="Author withdrew", expected_rev=1)
        self.assertEqual(record["action"], "withdraw")
        self.assertEqual(self.assert_parity(identity), [])

        proposal_id = queue_decision_delivery.message_id(
            queue_decision_delivery.PROPOSAL_KIND, identity, 1)
        clear_id = queue_decision_delivery.message_id(
            queue_decision_delivery.CLEAR_KIND, identity, 1)
        decisions = self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
        clear = next(row for row in decisions if row["id"] == clear_id)
        self.assertEqual(clear["refs"], [proposal_id])
        self.assertEqual(clear["body"]["action"], "withdraw")
        # An exact retry reuses the same stable message, not a second row.
        self.project.insights.withdraw(
            identity, source="worker", reason="Author withdrew", expected_rev=1)
        self.assertEqual([row["id"] for row in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]].count(clear_id), 1)

    def test_revise_after_activation_replaces_the_pending_proposal(self):
        self.activate()
        identity = "8" * 32
        self.live_submit(identity, "Original title")
        self.assertEqual([event["rev"] for event in self.assert_parity(identity)], [1])

        record = self.project.insights.revise(
            identity, source="worker", title="Revised title", body="Revised body",
            base_snapshot=None, expected_rev=1)
        self.assertEqual(record["rev"], 2)
        events = self.assert_parity(identity)
        self.assertEqual([event.get("kind") for event in events], ["insight.pending"])
        self.assertEqual([event["rev"] for event in events], [2])

        proposals = self.project.kernel_queues.read("proposals", "operator")["messages"]
        ids = {row["id"] for row in proposals}
        for rev in (1, 2):
            self.assertIn(queue_decision_delivery.message_id(
                queue_decision_delivery.PROPOSAL_KIND, identity, rev), ids)
        # The old revision is archived in legacy storage but is no longer a
        # queue delivery candidate; a repeated no-op revise stays idempotent.
        self.project.insights.revise(
            identity, source="worker", title="Revised title", body="Revised body",
            base_snapshot=None, expected_rev=2)
        self.assertEqual(self.assert_parity(identity), events)

    def test_revise_replay_repairs_a_lost_proposal_append(self):
        """A no-op retry re-appends the stable-id proposal after a lost write."""
        self.activate()
        identity = "8" * 32
        self.live_submit(identity, "Before lost revise")
        self.assertEqual([event.get("kind") for event in self.assert_parity(identity)],
                         ["insight.pending"])

        snap = self.project.snapshots.get()["id"]
        revise = dict(source="worker", title="After lost revise",
                      body="After lost revise body", base_snapshot=snap)
        real_submit = self.project.insights.decision_queue_writer.submit
        calls = {"n": 0}

        def lose_first_append(record):
            calls["n"] += 1
            if calls["n"] == 1:  # Crash window: durable r2 write, no queue append.
                return
            return real_submit(record)

        with mock.patch.object(self.project.insights.decision_queue_writer, "submit",
                               side_effect=lose_first_append):
            self.project.insights.revise(identity, expected_rev=1, **revise)
        self.assertEqual([event["rev"] for event in self.legacy_page()["events"]
                          if event.get("insight") == identity], [2])
        stranded = [event["rev"] for event in decision_delivery.pending(self.project, self.role)["events"]
                    if event.get("insight") == identity]
        self.assertEqual(stranded, [1])

        # An exact retry at the current revision is a no-op content-wise, but
        # must replay the stable-id proposal and restore byte-identical parity.
        retry = dict(revise, expected_rev=2)
        self.project.insights.revise(identity, **retry)
        events = self.assert_parity(identity)
        self.assertEqual([event["rev"] for event in events], [2])
        pid = queue_decision_delivery.message_id(queue_decision_delivery.PROPOSAL_KIND, identity, 2)
        proposals = self.project.kernel_queues.read("proposals", "operator")["messages"]
        self.assertEqual(sum(row["id"] == pid for row in proposals), 1)

        # A second replay cannot append another row.
        self.project.insights.revise(identity, **retry)
        self.assertEqual(self.assert_parity(identity), events)
        proposals = self.project.kernel_queues.read("proposals", "operator")["messages"]
        self.assertEqual(sum(row["id"] == pid for row in proposals), 1)

    def test_decide_revise_withdraw_lifecycle_matches_legacy(self):
        self.activate()
        identity = "7" * 32
        self.live_submit(identity, "Lifecycle proposal")
        self.assertEqual([event.get("kind") for event in self.assert_parity(identity)],
                         ["insight.pending"])
        self.project.insights.decide(
            identity, "accept", "Accepted once", "", "run-lifecycle", expected_rev=1,
            work_item_intent="none")
        accepted = self.assert_parity(identity)
        self.assertEqual([event["action"] for event in accepted], ["accept"])

        self.project.insights.revise(
            identity, source="worker", title="Reopened proposal", body="Reopened body",
            base_snapshot=None, expected_rev=1)
        reopened = self.assert_parity(identity)
        self.assertEqual([event.get("kind") for event in reopened], ["insight.pending"])
        self.assertEqual([event["rev"] for event in reopened], [2])

        self.project.insights.withdraw(
            identity, source="worker", reason="Closed by author", expected_rev=2)
        self.assertEqual(self.assert_parity(identity), [])

    def test_withdraw_of_an_accepted_revision_suppresses_the_decision_event(self):
        self.activate()
        identity = "6" * 32
        self.live_submit(identity, "Accepted then withdrawn")
        self.project.insights.decide(
            identity, "accept", "Accepted before withdrawal", "", "run-accepted",
            expected_rev=1, work_item_intent="none")
        accepted = self.assert_parity(identity)
        self.assertEqual([event["action"] for event in accepted], ["accept"])

        self.project.insights.withdraw(
            identity, source="operator", reason="Operator withdrew after acceptance",
            expected_rev=1, allow_decided=True)
        self.assertEqual(self.assert_parity(identity), [])


    def _decision_rows(self, identity):
        return [row for row in self.project.kernel_queues.read(
            "decisions", "operator", now=stage2_verify.NOW)["messages"]
            if row["body"].get("legacy_insight") == identity
            and row.get("deliver_at") is None
            and row["body"].get("legacy_rev") == "1"]

    def test_superseding_defer_uses_distinct_id_and_keeps_parity(self):
        """A same-revision replacement defer cannot reuse the first decision id."""
        self.activate()
        identity = "a1" * 16
        self.live_submit(identity, "Defer then replace")
        self.project.insights.decide(
            identity, "defer", "first", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        stable_id = queue_decision_delivery.message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1)
        self.assertEqual([row["id"] for row in self._decision_rows(identity)], [stable_id])

        self.project.insights.decide(
            identity, "defer", "replacement", "2099-02-01T00:00:00+00:00",
            "run-b", expected_rev=1)
        rows = self._decision_rows(identity)
        self.assertEqual(len(rows), 2)
        decision = json.loads((self.project.insights.root / "decisions" /
                               f"{identity}.json").read_bytes())
        superseding_id = queue_decision_delivery.superseding_message_id(
            queue_decision_delivery.DECISION_KIND, identity, 1,
            digest(canonical(decision))[:32])
        self.assertEqual(rows[1]["id"], superseding_id)
        self.assertNotEqual(rows[1]["id"], rows[0]["id"])
        # The read side selects the last row as effective; both legacy and
        # queue pages show the replacement.
        self.assertEqual(json.loads(rows[-1]["body"]["legacy_record"])["reason"],
                         "replacement")
        events = self.assert_parity(identity)
        self.assertEqual([(event["action"], event["reason"], event["revisit"])
                          for event in events],
                         [("defer", "replacement", "2099-02-01T00:00:00+00:00")])

    def test_defer_then_accept_uses_distinct_id_and_keeps_parity(self):
        """A final disposition after a defer owns its own immutable id."""
        self.activate()
        identity = "b1" * 16
        self.live_submit(identity, "Defer then accept")
        self.project.insights.decide(
            identity, "defer", "first", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        self.project.insights.decide(
            identity, "accept", "evidence arrived", "", "run-b",
            expected_rev=1, work_item_intent="none")
        rows = self._decision_rows(identity)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(json.loads(rows[-1]["body"]["legacy_record"])["action"], "accept")
        events = self.assert_parity(identity)
        self.assertEqual([(event["action"], event["reason"]) for event in events],
                         [("accept", "evidence arrived")])

    def test_exact_retry_of_superseding_defer_is_a_no_op(self):
        """Replaying a replacement defer converges on its existing row."""
        self.activate()
        identity = "c1" * 16
        self.live_submit(identity, "Replace then retry")
        self.project.insights.decide(
            identity, "defer", "first", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        self.project.insights.decide(
            identity, "defer", "replacement", "2099-02-01T00:00:00+00:00",
            "run-b", expected_rev=1)
        self.assertEqual(len(self._decision_rows(identity)), 2)
        self.project.insights.decide(
            identity, "defer", "replacement", "2099-02-01T00:00:00+00:00",
            "run-b", expected_rev=1)
        self.assertEqual(len(self._decision_rows(identity)), 2)
        self.assert_parity(identity)

    def test_missed_superseding_append_repairs_under_distinct_id(self):
        """A lost replacement-defer append is repaired without a second row."""
        self.activate()
        identity = "d1" * 16
        self.live_submit(identity, "Defer then lost replacement")
        self.project.insights.decide(
            identity, "defer", "first", stage2_tests.REVISIT_AT, "run-a", expected_rev=1)
        writer = self.project.insights.decision_queue_writer
        real_decide = writer.decide
        calls = {"count": 0}

        def drop_next_append(*args):
            calls["count"] += 1
            if calls["count"] == 1:
                return
            return real_decide(*args)

        with mock.patch.object(writer, "decide", side_effect=drop_next_append):
            self.project.insights.decide(
                identity, "defer", "replacement", "2099-02-01T00:00:00+00:00",
                "run-b", expected_rev=1)
        self.assertEqual(len(self._decision_rows(identity)), 1)

        self.project.insights.decide(
            identity, "defer", "replacement", "2099-02-01T00:00:00+00:00",
            "run-b", expected_rev=1)
        rows = self._decision_rows(identity)
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[-1]["body"]["legacy_record"])["reason"],
                         "replacement")
        self.assert_parity(identity)


if __name__ == "__main__":
    unittest.main()
