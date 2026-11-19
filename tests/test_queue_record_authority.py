"""Queue-backed record authority: gate, parity, receipts and actor isolation."""
import contextlib
import io
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src and tests on the path)
from mizu import (decision_delivery, decision_reconciliation, host_records, operator_records,
                  queue_record_delivery, record_reconciliation, runtime, services)
from mizu.errors import Denied
from mizu.fs import canonical, digest
from mizu.shared_records import _token

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_record_activate  # noqa: E402  (example userland host gate)
import stage2_verify  # noqa: E402  (example userland module)
import test_stage2_conversion as conversion_tests  # noqa: E402  (fixture module)
import test_stage2_decision_adapter as decision_tests  # noqa: E402  (decision fixture)


class QueueRecordAuthority(conversion_tests.Stage2QuestionConversion):
    def convert(self, *flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", *flags])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def gate(self, *, queues=None):
        conversion = convert_legacy.apply(self.project)
        verification = stage2_verify.verify(self.project, queues=queues or
                                            convert_legacy.VERIFY_QUEUES)
        return conversion, verification

    def activate(self, conversion=None, verification=None):
        if conversion is None or verification is None:
            conversion, verification = self.gate()
        return record_reconciliation.activate_queue_record_delivery(
            self.project, conversion=conversion, verification=verification)

    def receipts(self):
        return self.project.kernel_queues.read("record-receipts", "host")["messages"]

    def test_activation_gate_marker_retry_and_decision_coexistence(self):
        conversion, verification = self.gate()
        self.assertIsNone(queue_record_delivery.authority_kind(self.project.insights.root))
        for changes in ({"applied": False}, {"unbound_receipts": ["f" * 64]},
                        {"record_receipts": None}):
            with self.subTest(changes=changes), self.assertRaises(Denied):
                self.activate({**conversion, **changes}, verification)
        with self.assertRaises(Denied):
            self.activate(conversion, {**verification, "ok": False})
        with self.assertRaises(Denied):
            self.activate(conversion, {**verification, "checked": {
                key: value for key, value in verification["checked"].items()
                if key != "record-receipts"}})
        self.assertIsNone(queue_record_delivery.authority_kind(self.project.insights.root))

        marker = self.activate(conversion, verification)
        self.assertEqual(marker["authority"], "queues")
        self.assertEqual(marker["kind"], queue_record_delivery.KIND)
        self.assertEqual(queue_record_delivery.authority_kind(self.project.insights.root), "queues")
        for name, value in (("record-authority.json", marker),
                            ("record-activation-plan.json", conversion),
                            ("record-activation-report.json", verification)):
            self.assertEqual((self.project.root / name).read_bytes(), canonical(value))
        self.assertEqual(self.activate(conversion, verification), marker)
        self.assertEqual(marker["plan_digest"], digest(canonical(conversion)))
        self.assertEqual(marker["report_digest"], digest(canonical(verification)))

        # The decision marker has its own file, plan, report and identity.
        decision_source = decision_tests.Stage2DecisionAdapter(methodName="runTest")
        decision_source.setUp()
        self.addCleanup(decision_source.doCleanups)
        decision_conversion = convert_legacy.apply(decision_source.project)
        decision_verification = stage2_verify.verify(decision_source.project)
        decision_marker = decision_reconciliation.activate_queue_decision_delivery(
            decision_source.project, conversion=decision_conversion,
            verification=decision_verification)
        self.assertEqual(decision_delivery.authority_kind(decision_source.project.insights.root),
                         "queues")
        self.assertIsNone(queue_record_delivery.authority_kind(decision_source.project.insights.root))
        self.assertNotEqual(decision_marker["plan_digest"], marker["plan_digest"])

    def test_operator_record_activation_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = stage2_record_activate.main([str(self.file), "--project", "sample"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["authority"], "queues")
        self.assertTrue(report["verification"]["ok"])
        self.assertIn("record-receipts", report["verification"]["checked"])
        self.assertEqual(queue_record_delivery.authority_kind(self.project.insights.root), "queues")

    def test_pending_manifest_envelope_and_token_are_byte_identical(self):
        self.convert("--apply")
        role = self.config.roles["worker"]
        legacy_page, legacy_manifest = operator_records.pending(self.project, role)
        queued_page, queued_manifest = queue_record_delivery.pending(self.project, role)
        self.assertEqual(legacy_page, queued_page)
        self.assertEqual(legacy_manifest, queued_manifest)
        for entry in queued_manifest:
            self.assertEqual(entry.ack_token, _token("worker", entry.record_id,
                                                      entry.record_digest))
        page = self.context("worker")
        operator_records.bind(page, (legacy_page, legacy_manifest))
        queued = self.context("worker")
        queue_record_delivery.bind(queued, (queued_page, queued_manifest))
        for token in (entry.ack_token for entry in legacy_manifest):
            self.assertEqual(canonical(operator_records.read(page, {"ack_token": token})),
                             canonical(queue_record_delivery.read(queued, {"ack_token": token})))
        self.assertEqual(canonical(operator_records.questions(
            self.project, limit=10, scan_limit=1000)),
            canonical(queue_record_delivery.questions(
                self.project, limit=10, scan_limit=1000)))

    def test_post_activation_writes_are_exact_and_retry_idempotent(self):
        conversion, verification = self.gate()
        self.activate(conversion, verification)

        def rows(queue):
            return self.project.kernel_queues.read(queue, "operator", cursor=0,
                                                   limit=512)["messages"]

        answers_before = rows(queue_record_delivery.ANSWERS)
        notices_before = rows(queue_record_delivery.NOTICES)
        answer_args = dict(event_id="live-answer", kind="answer", body="Live answer",
                           subject=self.subject)
        answer = queue_record_delivery.write(self.project, **answer_args)
        answer_rows = [row for row in rows(queue_record_delivery.ANSWERS)
                       if row["body"]["legacy_id"] == answer["record"]["id"]]
        self.assertEqual(len(answer_rows), 1)
        self.assertEqual(answer_rows[0]["author"], "operator")
        self.assertEqual(answer_rows[0]["refs"], [self.ids()[0]])
        self.assertEqual(answer_rows[0]["body"]["legacy_digest"], answer["record_digest"])
        self.assertEqual(answer, queue_record_delivery.write(self.project, **answer_args))
        self.assertEqual(rows(queue_record_delivery.ANSWERS)[len(answers_before):], answer_rows)

        notice_args = dict(event_id="live-notice", kind="operator.notice",
                           body="Live notice", addressee="worker")
        notice = queue_record_delivery.write(self.project, **notice_args)
        notice_rows = [row for row in rows(queue_record_delivery.NOTICES)
                       if row["body"]["legacy_id"] == notice["record"]["id"]]
        self.assertEqual(len(notice_rows), 1)
        self.assertEqual(notice_rows[0]["author"], "operator")
        self.assertEqual(notice_rows[0]["refs"], [])
        self.assertEqual(notice_rows[0]["body"]["legacy_digest"], notice["record_digest"])
        self.assertEqual(notice, queue_record_delivery.write(self.project, **notice_args))
        self.assertEqual(rows(queue_record_delivery.NOTICES)[len(notices_before):], notice_rows)

        records, _ = queue_record_delivery._load(self.project, "operator")
        for result, queue in ((answer, queue_record_delivery.ANSWERS),
                              (notice, queue_record_delivery.NOTICES)):
            row = next(row for row in records if row["id"] == result["record"]["id"])
            self.assertEqual((row["queue"], row["kind"], row["digest"], row["envelope"]),
                             (queue, result["record"]["kind"], result["record_digest"],
                              result["record"]))
        self.assertEqual(queue_record_delivery.QUEUE_BY_KIND, {
            "answer": queue_record_delivery.ANSWERS,
            "operator.notice": queue_record_delivery.NOTICES})

        for kind in ("role.question", "operator.notice.", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(Denied):
                queue_record_delivery.write(self.project, event_id="bad", kind=kind,
                                            body="No", addressee="worker")

    def test_read_role_matches_legacy_path_for_own_record_after_activation(self):
        conversion, verification = self.gate()
        self.activate(conversion, verification)
        worker = replace(self.config.roles["worker"], capabilities=(
            *self.config.roles["worker"].capabilities, "record_read"))
        self.config = replace(self.config, roles={**self.config.roles, "worker": worker})
        context = self.context("worker")
        args = {"kind": "role.question", "id": "question1"}
        legacy = host_records.read_role(context, args)
        queued = queue_record_delivery.read_role(context, args)
        self.assertEqual(legacy, queued)
        self.assertEqual((queued["record_id"], queued["record_digest"]),
                         (self.question_record["id"], self.question_digest))
        self.assertEqual(json.loads(queued["text"]), self.question_record)

        created = queue_record_delivery.write_role(context, {
            "kind": "role.question", "id": "live-question", "subject": "{}",
            "payload": json.dumps(conversion_tests.QUESTION_PAYLOAD)})
        live = queue_record_delivery.read_role(context, {
            "kind": "role.question", "id": "live-question"})
        self.assertEqual((live["record_id"], live["record_digest"]),
                         (created["record"]["id"], created["record_digest"]))
        self.assertEqual(json.loads(live["text"]), created["record"])

    def test_converted_receipts_suppress_and_replay_is_idempotent(self):
        role = self.config.roles["worker"]
        page, manifest = operator_records.pending(self.project, role)
        context = self.context("worker")
        operator_records.bind(context, (page, manifest))
        operator_records.acknowledge(context, {manifest[0].ack_token})
        conversion = self.convert("--apply")
        self.assertEqual(len(conversion["record_receipts"]), 1)
        verification = stage2_verify.verify(self.project, queues=convert_legacy.VERIFY_QUEUES)
        self.assertTrue(verification["ok"])
        rows = self.receipts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "host")
        self.assertEqual(rows[0]["body"], {
            "ack_token": manifest[0].ack_token, "legacy_role": "worker",
            "legacy_record_id": manifest[0].record_id,
            "legacy_record_digest": manifest[0].record_digest})
        self.assertEqual(rows[0]["id"], queue_record_delivery._receipt_id(
            "worker", manifest[0].ack_token))
        queued = queue_record_delivery.pending(self.project, role)[0]
        self.assertEqual(queued["pending_count"], len(manifest) - 1)
        self.activate(conversion, verification)
        context = self.context("worker")
        queue_record_delivery.bind(context, queue_record_delivery.pending(self.project, role))
        self.assertEqual(self.receipts(), rows)

    def test_runtime_switch_uses_only_queue_receipts(self):
        conversion, verification = self.gate()
        self.activate(conversion, verification)
        role = replace(self.config.roles["worker"], capabilities=(
            *self.config.roles["worker"].capabilities, "record_read"))
        config = replace(self.config, roles={**self.config.roles, "worker": role})
        from support import ScriptDriver

        def driver(context, prompt, profile=None):
            token = context.operator_record_manifest[0].ack_token
            return context.handle("finish", {"outcome": "wait", "summary": "Handled record",
                                             "state": "Next step is explicit.",
                                             "ack_events": [token]})

        result = runtime.Engine(config, driver=ScriptDriver(driver)).run(self.project, "worker")
        self.assertEqual(result["status"], "completed")
        rows = self.receipts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "host")
        self.assertFalse((self.project.root / "shared-record-receipts").exists())

    def test_actor_binding_and_cursor_immutability(self):
        self.convert("--apply")
        role = self.config.roles["worker"]
        queue_record_delivery.pending(self.project, role)
        context = self.context("worker")
        page, manifest = queue_record_delivery.pending(self.project, role)
        queue_record_delivery.bind(context, (page, manifest))
        queue_record_delivery.read(context, {"ack_token": manifest[0].ack_token})
        with self.assertRaises(Denied):
            queue_record_delivery.read(context, {"ack_token": manifest[0].ack_token,
                                                 "actor": "operator"})
        with self.assertRaises(Denied):
            queue_record_delivery.write_role(context, {"kind": "role.question", "id": "x",
                "subject": "{}", "payload": "{}", "actor": "operator"})
        for queue in queue_record_delivery.QUEUES:
            self.assertFalse((self.project.root / "queues" / queue / "cursors").exists())

    def test_malformed_metadata_and_missing_queue_refuse(self):
        self.convert("--apply")
        role = self.config.roles["worker"]

        def rewrite(queue, number, change):
            path = self.project.root / "queues" / queue / "messages" / f"{number:020d}.json"
            original = path.read_bytes()
            message = json.loads(original)
            change(message)
            path.write_bytes(canonical(message))

        cases = (
            ("questions", 1, lambda m: m["body"].update(legacy_kind="other.question"),
             "legacy digest"),
            ("questions", 1, lambda m: m["body"].update(
                legacy_created_at="2099-01-01T00:00:00"), "legacy_created_at"),
            ("answers", 1, lambda m: m["body"].update(legacy_digest="0" * 64),
             "legacy digest"),
            ("notices", 1, lambda m: m["body"].update(legacy_target="not json"),
             "Notice target is not canonical JSON"),
        )
        for queue, number, change, expected in cases:
            with self.subTest(queue=queue):
                path = self.project.root / "queues" / queue / "messages" / f"{number:020d}.json"
                original = path.read_bytes()
                rewrite(queue, number, change)
                with self.assertRaisesRegex(Exception, expected):
                    queue_record_delivery.pending(self.project, role)
                path.write_bytes(original)
        config = replace(self.config, queues={name: value for name, value
                                             in self.config.queues.items()
                                             if name != "record-receipts"})
        self.project.config = config
        with self.assertRaisesRegex(Exception, "record-receipts declaration"):
            queue_record_delivery.pending(self.project, role)


if __name__ == "__main__":
    unittest.main()
