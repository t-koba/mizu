"""Stage-2 userland shadow of authoritative questions and operator answers."""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu import host_records, operator_records as records
from mizu.cli import _cmd_kernel_shadow, parser
from mizu.config import load
from mizu.errors import Denied
from mizu.fs import canonical, digest
from mizu.kernel_shadow import (MAX_SHADOW_FAILURES, answer_object_id,
                                insight_object_id, insight_question_object_id,
                                record_question_object_id)
from mizu.project import initialize

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "kernel_userland.toml"
QUESTION_DECLARATION = {
    "role.question": {"writer": "role.writer", "route": "operator_question",
                      "wake": False, "retention": "permanent",
                      "acknowledgement": "explicit", "subject": "object",
                      "maximum": 8192}}


class QuestionAnswerShadow(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-qa-shadow-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        base = load(CONFIG)
        writer = dataclasses.replace(
            base.roles["writer"],
            capabilities=(*base.roles["writer"].capabilities, "record_write"))
        self.config = dataclasses.replace(
            base, data=root / "data", roles={**base.roles, "writer": writer},
            record_kinds=QUESTION_DECLARATION,
            operator_records=records.configuration({
                "answer": {"enabled": True}, "operator.notice": {"enabled": True}}))
        source = root / "source"
        source.mkdir()
        (source / "app.py").write_bytes(b"VALUE = 2\n")
        goal = root / "GOAL.md"
        goal.write_text("Preserve correctness.\n")
        self.project = initialize(self.config, "sample", source, goal,
                                  ["writer", "reviewer", "observer"],
                                  ["python3 -m unittest"])
        self.context = SimpleNamespace(role=self.config.roles["writer"],
                                       config=self.config, project=self.project)

    def shadow(self):
        return self.project.insights.kernel_shadow

    def insight_question(self, event="aa" * 16, addressee="operator", **kwargs):
        return self.project.insights.submit(
            source="writer", title="Question", body="Which behavior?",
            base_snapshot=None, insight_id=event, addressee=addressee, **kwargs)

    def ask(self, event="question1", body="Which option?", **payload):
        return host_records.write_role(self.context, args={
            "kind": "role.question", "id": event, "subject": "{}",
            "payload": json.dumps({"body": body, **payload})})

    def subject(self, result):
        return {"type": "record", "kind": result["record"]["kind"],
                "id": result["record"]["id"], "digest": result["record_digest"]}

    def answer(self, subject, event="answer1", body="Use option A"):
        return records.write(self.project, event_id=event, kind="answer",
                             body=body, subject=subject)

    def cli_report(self, insight_id, *, reproject=False):
        argv = ["kernel-shadow", self.project.name, insight_id]
        if reproject:
            argv.append("--reproject")
        return _cmd_kernel_shadow(self.config, self.project, parser().parse_args(argv))

    def test_insight_question_and_publish_answer_transition(self):
        insight = self.insight_question(urgency="high")
        question_id = insight_question_object_id(insight)
        self.assertRegex(question_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(question_id, digest(canonical(
            ["kernel-shadow", "question", "insight", insight["id"], 1]))[:32])
        question = self.project.kernel_objects.read("question", question_id)
        self.assertEqual(question["fields"], {
            "subject_type": "insight", "subject_ref": insight_object_id(insight),
            "subject_rev": 1, "body": "Which behavior?", "urgency": "high"})
        self.assertEqual((question["state"], question["author"]), ("pending", "host"))

        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        result = self.answer(subject)
        answer_id = answer_object_id(result["record"])
        self.assertRegex(answer_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(answer_id, digest(canonical(
            ["kernel-shadow", "answer", result["record"]["id"]]))[:32])
        mirrored = self.project.kernel_objects.read("answer", answer_id)
        self.assertEqual(mirrored["fields"], {
            "question": question_id, "revision": 1, "action": "publish",
            "body": "Use option A", "reference": result["record"]["id"]})
        self.assertEqual((mirrored["state"], mirrored["author"]), ("recorded", "host"))
        answered = self.project.kernel_objects.read("question", question_id)
        self.assertEqual((answered["state"], answered["rev"], answered["actor"]),
                         ("answered", 2, "host"))
        self.assertEqual(self.shadow().failures, [])

        writer_addressed = self.insight_question(event="bb" * 16,
                                                 addressee="reviewer")
        self.assertEqual(self.project.kernel_objects.revisions(
            "question", insight_question_object_id(writer_addressed)), [])
        self.assertEqual(len(self.project.kernel_objects.list("question")["objects"]), 1)

    def test_revised_insight_question_can_be_answered(self):
        first = self.insight_question()
        revised = self.project.insights.revise(
            first["id"], source="writer", title="Question",
            body="Which behavior now?", base_snapshot=None, run="test")
        question_id = insight_question_object_id(revised)
        question = self.project.kernel_objects.read("question", question_id)
        self.assertEqual((question["fields"]["subject_rev"], question["fields"]["body"]),
                         (2, "Which behavior now?"))
        answer = self.answer({"type": "insight", "id": first["id"], "rev": 2})
        mirrored = self.project.kernel_objects.read(
            "answer", answer_object_id(answer["record"]))
        self.assertEqual((mirrored["fields"]["question"], mirrored["fields"]["revision"]),
                         (question_id, 2))
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "answered")
        self.assertEqual(self.shadow().failures, [])

    def test_replaying_answer_repairs_a_failed_question_transition(self):
        insight = self.insight_question()
        question_id = insight_question_object_id(insight)
        with patch.object(self.project.kernel_objects, "transition",
                          side_effect=Denied("derived transition failure")):
            answer = self.answer({"type": "insight", "id": insight["id"], "rev": 1})
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "pending")
        self.assertEqual(len(self.project.kernel_objects.revisions(
            "answer", answer_object_id(answer["record"]))), 1)
        self.shadow().clear_failures(insight["id"])
        self.shadow().answered(answer["record"], insight)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "answered")
        self.assertTrue(self.shadow().verify(insight)["ok"])

    def test_role_record_question_and_publish_answer_transition(self):
        asked = self.ask(urgency="normal")
        record, computed = asked["record"], asked["record_digest"]
        question_id = record_question_object_id(record, computed)
        self.assertEqual(question_id, digest(canonical(
            ["kernel-shadow", "question", "record", record["id"], computed]))[:32])
        question = self.project.kernel_objects.read("question", question_id)
        self.assertEqual(question["fields"], {
            "subject_type": "record", "record_kind": "role.question",
            "record_id": record["id"], "record_digest": computed,
            "body": "Which option?", "urgency": "normal"})
        self.assertEqual(question["state"], "pending")

        result = self.answer(self.subject(asked))
        mirrored = self.project.kernel_objects.read(
            "answer", answer_object_id(result["record"]))
        self.assertEqual(mirrored["fields"], {
            "question": question_id, "revision": 1, "action": "publish",
            "body": "Use option A", "reference": result["record"]["id"]})
        answered = self.project.kernel_objects.read("question", question_id)
        self.assertEqual((answered["state"], answered["actor"]), ("answered", "host"))
        self.assertEqual(self.shadow().failures, [])
        self.assertTrue(self.shadow().verify_question(record, computed)["ok"])
        self.assertTrue(self.shadow().verify_answer(result["record"])["ok"])

    def test_exact_retries_leave_both_objects_unchanged(self):
        insight = self.insight_question()
        question_id = insight_question_object_id(insight)
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        answer = self.answer(subject)
        head = self.project.kernel_events.head()
        self.assertEqual(self.insight_question(), insight)
        self.assertEqual(self.answer(subject), answer)
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [1, 2])
        self.assertEqual(self.project.kernel_objects.revisions(
            "answer", answer_object_id(answer["record"])), [1])

        asked = self.ask()
        record_question_id = record_question_object_id(
            asked["record"], asked["record_digest"])
        record_answer = self.answer(self.subject(asked), event="answer2")
        head = self.project.kernel_events.head()
        self.assertEqual(self.ask(), asked)
        self.assertEqual(self.answer(self.subject(asked), event="answer2"), record_answer)
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions(
            "question", record_question_id), [1, 2])
        self.assertEqual(self.project.kernel_objects.revisions(
            "answer", answer_object_id(record_answer["record"])), [1])
        self.assertEqual(self.shadow().failures, [])

    def test_answer_to_missing_question_is_diagnostic_only(self):
        plain = self.insight_question(event="cc" * 16, addressee="reviewer")
        subject = {"type": "insight", "id": plain["id"], "rev": 1}
        result = self.answer(subject, event="orphan-answer")
        record = result["record"]
        self.assertEqual(records.store(self.project).get(record["id"])[0], record)
        self.assertEqual(self.project.kernel_objects.revisions(
            "answer", answer_object_id(record)), [])
        failures = self.shadow().failures
        self.assertEqual(len(failures), 1)
        self.assertEqual((failures[0]["operation"], failures[0]["insight"],
                          failures[0]["error"]), ("answer", record["id"], "Denied"))
        self.assertIn("must reference an existing object", failures[0]["reason"])
        pending = records.pending(self.project, self.config.roles["writer"])[0]["events"]
        self.assertIn(record["id"], [row["record_id"] for row in pending])

    def test_projection_failures_are_diagnostic_only_and_bounded(self):
        with patch.object(self.shadow(), "questioned", side_effect=OSError("x" * 400)):
            insight = self.insight_question(event="dd" * 16)
        self.assertEqual(insight["id"], "dd" * 16)
        self.assertEqual(self.project.kernel_objects.list("question")["objects"], [])
        failure = self.shadow().failures[-1]
        self.assertEqual((failure["operation"], failure["insight"], failure["error"]),
                         ("question", insight["id"], "OSError"))
        self.assertEqual(len(failure["reason"]), 256)
        self.assertTrue(failure["reason"].endswith("..."))

        with patch.object(self.shadow(), "questioned_record",
                          side_effect=OSError("x" * 400)):
            asked = self.ask(event="failure-question")
        shared = host_records.store(self.project.root, self.project.snapshots,
                                    self.config.record_kinds)
        self.assertEqual(shared.get(asked["record"]["id"])[0], asked["record"])
        self.assertEqual(self.project.kernel_objects.list("question")["objects"], [])
        failure = self.shadow().failures[-1]
        self.assertEqual((failure["operation"], failure["insight"]),
                         ("question", asked["record"]["id"]))

        insight = self.insight_question(event="ee" * 16)
        question_id = insight_question_object_id(insight)
        with patch.object(self.shadow(), "answered", side_effect=OSError("x" * 400)):
            result = self.answer({"type": "insight", "id": insight["id"], "rev": 1})
        self.assertEqual(records.store(self.project).get(result["record"]["id"])[0],
                         result["record"])
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "pending")
        failure = self.shadow().failures[-1]
        self.assertEqual((failure["operation"], failure["insight"], failure["error"]),
                         ("answer", result["record"]["id"], "OSError"))

        queries = len(self.shadow().failures)
        with patch.object(self.shadow(), "answered", side_effect=OSError("x" * 400)):
            for index in range(MAX_SHADOW_FAILURES + 4):
                self.answer({"type": "insight", "id": insight["id"], "rev": 1},
                            event=f"failure-answer-{index}")
        self.assertEqual(len(self.shadow().failures), MAX_SHADOW_FAILURES)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "pending")
        self.assertTrue(all(len(row["reason"]) == 256
                            for row in self.shadow().failures))
        self.assertGreater(MAX_SHADOW_FAILURES, queries)

    def test_unrepresentable_role_question_body_is_diagnostic_only(self):
        for event, body in (("no-body", ""), ("wrong-body", 42)):
            asked = self.ask(event=event, body=body)
            shared = host_records.store(self.project.root, self.project.snapshots,
                                        self.config.record_kinds)
            self.assertEqual(shared.get(asked["record"]["id"])[0], asked["record"])
            self.assertEqual(self.project.kernel_objects.list("question")["objects"], [])
            failure = self.shadow().failures[-1]
            self.assertEqual((failure["operation"], failure["insight"], failure["error"]),
                             ("question", asked["record"]["id"], "Denied"))
            self.assertIn("representable body", failure["reason"])

    def test_reused_question_id_with_different_content_is_diagnostic_only(self):
        event = "ff" * 16
        question_id = digest(canonical(
            ["kernel-shadow", "question", "insight", event, 1]))[:32]
        original = {"subject_type": "record", "record_kind": "role.question",
                    "record_id": "other/question", "record_digest": "ab" * 32,
                    "body": "Another question"}
        self.project.kernel_objects.create("question", original, "host",
                                           object_id=question_id)
        insight = self.insight_question(event=event)
        self.assertEqual(self.project.insights.read(insight["id"])["id"], event)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["fields"],
                         original)
        failure = self.shadow().failures[-1]
        self.assertEqual((failure["operation"], failure["insight"], failure["error"]),
                         ("question", event, "Denied"))
        self.assertIn("different content", failure["reason"])

    def test_non_publish_answer_does_not_transition_the_question(self):
        insight = self.insight_question()
        question_id = insight_question_object_id(insight)
        envelope = {
            "id": "operator/sample/withdraw-answer",
            "subject": {"type": "insight", "id": insight["id"], "rev": 1},
            "payload": {"action": "withdraw", "body": "Withdrawn answer",
                        "addressee": "writer", "target": None}}
        self.shadow().answered(envelope, insight)
        answer = self.project.kernel_objects.read(
            "answer", answer_object_id(envelope))
        self.assertEqual(answer["fields"]["action"], "withdraw")
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "pending")
        self.assertEqual(self.shadow().failures, [])
        # A non-publish answer has no expected question state to verify.
        self.assertTrue(self.shadow().verify_answer(envelope, insight)["ok"])

    def test_verify_question_reports_divergence_without_writing(self):
        insight = self.insight_question(urgency="high")
        question_id = insight_question_object_id(insight)
        asked = self.ask(urgency="normal")
        record, computed = asked["record"], asked["record_digest"]
        record_id = record_question_object_id(record, computed)
        head = self.project.kernel_events.head()
        revisions = self.project.kernel_objects.revisions("question", question_id)
        failures = list(self.shadow().failures)
        stored = self.project.insights.read(insight["id"])
        expected = {"kind": "question", "object": question_id, "ok": True,
                    "mismatches": []}
        self.assertEqual(self.shadow().verify_question(insight, insight=insight), expected)
        self.assertTrue(self.shadow().verify_question(record, computed)["ok"])
        self.assertEqual(self.shadow().verify_question(record, computed)["object"], record_id)

        read = self.project.kernel_objects.read

        def corrupted(kind, oid, *args, **kwargs):
            current = read(kind, oid, *args, **kwargs)
            if (kind, oid) == ("question", question_id):
                return {**current, "fields": {**current["fields"], "body": "Tampered"}}
            return current

        with patch.object(self.project.kernel_objects, "read", side_effect=corrupted):
            divergent = self.shadow().verify_question(insight, insight=insight)
        self.assertFalse(divergent["ok"])
        self.assertEqual(divergent["mismatches"], ["question field body differs"])
        with patch.object(self.project.kernel_objects, "read", side_effect=Denied("gone")):
            missing = self.shadow().verify_question(insight, insight=insight)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["mismatches"],
                         [f"question object {question_id} unavailable: Denied"])

        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), revisions)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["fields"]["body"],
                         "Which behavior?")
        self.assertEqual(self.shadow().failures, failures)
        self.assertEqual(self.project.insights.read(insight["id"]), stored)

    def test_verify_answer_reads_exact_projection_without_writing(self):
        insight = self.insight_question()
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        result = self.answer(subject)
        answer = result["record"]
        answer_id = answer_object_id(answer)
        head = self.project.kernel_events.head()
        revisions = self.project.kernel_objects.revisions("answer", answer_id)
        failures = list(self.shadow().failures)
        stored = records.store(self.project).get(answer["id"])[0]
        self.assertEqual(self.shadow().verify_answer(answer, insight), {
            "kind": "answer", "object": answer_id, "ok": True, "mismatches": []})

        read = self.project.kernel_objects.read

        def corrupted(kind, oid, *args, **kwargs):
            current = read(kind, oid, *args, **kwargs)
            if (kind, oid) == ("answer", answer_id):
                return {**current, "fields": {**current["fields"], "action": "withdraw"}}
            return current

        with patch.object(self.project.kernel_objects, "read", side_effect=corrupted):
            divergent = self.shadow().verify_answer(answer, insight)
        self.assertFalse(divergent["ok"])
        self.assertEqual(divergent["mismatches"], ["answer field action differs"])
        without_insight = self.shadow().verify_answer(answer)
        self.assertFalse(without_insight["ok"])
        self.assertEqual(without_insight["object"], answer_id)
        self.assertEqual(without_insight["mismatches"], ["answer fields unavailable: Denied"])
        with patch.object(self.project.kernel_objects, "read", side_effect=Denied("gone")):
            missing = self.shadow().verify_answer(answer, insight)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["mismatches"],
                         [f"answer object {answer_id} unavailable: Denied",
                          f"question object {insight_question_object_id(insight)} unavailable: Denied"])

        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("answer", answer_id), revisions)
        self.assertEqual(self.project.kernel_objects.read("answer", answer_id)["fields"]["action"],
                         "publish")
        self.assertEqual(self.shadow().failures, failures)
        self.assertEqual(records.store(self.project).get(answer["id"])[0], stored)

    def test_verify_answer_checks_published_question_state(self):
        insight = self.insight_question()
        question_id = insight_question_object_id(insight)
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        with patch.object(self.project.kernel_objects, "transition",
                          side_effect=Denied("derived transition failure")):
            result = self.answer(subject)
        answer = result["record"]
        head = self.project.kernel_events.head()
        stored = records.store(self.project).get(answer["id"])[0]
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"],
                         "pending")
        report = self.shadow().verify_answer(answer, insight)
        self.assertEqual(report["object"], answer_object_id(answer))
        self.assertFalse(report["ok"])
        self.assertEqual(report["mismatches"], ["question state is pending, expected answered"])
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(records.store(self.project).get(answer["id"])[0], stored)

        self.shadow().answered(answer, insight)
        self.assertTrue(self.shadow().verify_answer(answer, insight)["ok"])


    def test_exact_resubmit_replays_failed_question_projection(self):
        shadow = self.shadow()
        with patch.object(shadow, "questioned", side_effect=OSError("transient question failure")):
            insight = self.insight_question(event="re" * 16)
        question_id = insight_question_object_id(insight)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [])
        self.assertEqual(shadow.failures[-1]["operation"], "question")
        stored = self.project.insights.read(insight["id"])

        self.assertEqual(self.insight_question(event=insight["id"]), insight)
        self.assertEqual(self.project.insights.read(insight["id"]), stored)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["fields"]["body"],
                         "Which behavior?")
        self.assertTrue(shadow.verify_question(insight, insight=insight)["ok"])

    def test_auto_id_dedup_replays_failed_question_projection(self):
        shadow = self.shadow()
        submit = dict(source="writer", title="Queued question", body="Which behavior?",
                      base_snapshot=None, addressee="operator")
        with patch.object(shadow, "questioned", side_effect=OSError("transient question failure")):
            first = self.project.insights.submit(**submit)
        question_id = insight_question_object_id(first)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [])

        replay = self.project.insights.submit(**submit)
        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.project.insights.read(first["id"]), first)
        self.assertTrue(shadow.verify_question(first, insight=first)["ok"])

    def test_revise_no_op_replays_failed_question_projection(self):
        shadow = self.shadow()
        with patch.object(shadow, "questioned", side_effect=OSError("transient question failure")):
            insight = self.insight_question(event="rv" * 16)
        question_id = insight_question_object_id(insight)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [])

        revised = self.project.insights.revise(
            insight["id"], source="writer", title=insight["title"], body=insight["body"],
            base_snapshot=None, expected_rev=1, addressee="operator")
        self.assertEqual(revised, insight)
        self.assertEqual(self.project.insights.read(insight["id"]), insight)
        self.assertTrue(shadow.verify_question(insight, insight=insight)["ok"])
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [1])

    def test_reproject_repairs_failed_question_and_reports_verification(self):
        shadow = self.shadow()
        with patch.object(shadow, "questioned", side_effect=OSError("transient question failure")):
            insight = self.insight_question(event="rp" * 16)
        question_id = insight_question_object_id(insight)
        self.assertEqual(self.project.kernel_objects.revisions("question", question_id), [])

        report = self.cli_report(insight["id"], reproject=True)
        self.assertTrue(report["enabled"])
        self.assertTrue(report["verification"]["ok"])
        self.assertTrue(report["question_verification"]["ok"])
        self.assertEqual(report["question_verification"]["object"], question_id)
        self.assertEqual(report["answer_verification"], [])
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"], "pending")
        self.assertEqual(self.project.insights.read(insight["id"]), insight)

    def test_legal_long_body_projects_question(self):
        bound = self.config.kinds["question"].fields["body"].max
        # Half the declared bound is the legacy insight preview byte bound.
        body = "x" * (bound // 2 + 1)
        self.assertGreater(len(body), bound // 2)
        insight = self.project.insights.submit(
            source="writer", title="Long question", body=body, base_snapshot=None,
            insight_id="lg" * 16, addressee="operator")
        question_id = insight_question_object_id(insight)
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["fields"]["body"],
                         body)
        self.assertTrue(self.shadow().verify_question(insight, insight=insight)["ok"])
        self.assertEqual(self.shadow().failures, [])

    def test_reproject_repairs_failed_answer_projection(self):
        insight = self.insight_question(event="ar" * 16)
        question_id = insight_question_object_id(insight)
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        with patch.object(self.shadow(), "answered", side_effect=OSError("transient answer failure")):
            result = self.answer(subject, event="answer-reproject")
        answer = result["record"]
        answer_id = answer_object_id(answer)
        self.assertEqual(self.project.kernel_objects.revisions("answer", answer_id), [])
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"], "pending")

        report = self.cli_report(insight["id"], reproject=True)
        self.assertTrue(report["question_verification"]["ok"])
        self.assertEqual(len(report["answer_verification"]), 1)
        self.assertTrue(report["answer_verification"][0]["ok"])
        self.assertEqual(report["answer_verification"][0]["object"], answer_id)
        self.assertEqual(self.project.kernel_objects.read("answer", answer_id)["state"], "recorded")
        self.assertEqual(self.project.kernel_objects.read("question", question_id)["state"], "answered")
        self.assertEqual(records.store(self.project).get(answer["id"])[0], answer)

    def test_verify_report_observes_question_and_answer_without_writing(self):
        insight = self.insight_question(event="vf" * 16)
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        result = self.answer(subject, event="answer-verify")
        answer_id = answer_object_id(result["record"])
        question_id = insight_question_object_id(insight)
        head = self.project.kernel_events.head()
        revisions = (self.project.kernel_objects.revisions("question", question_id),
                     self.project.kernel_objects.revisions("answer", answer_id))
        failures = list(self.shadow().failures)

        report = self.cli_report(insight["id"])
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertTrue(report["verification"]["ok"])
        self.assertTrue(report["question_verification"]["ok"])
        self.assertEqual(len(report["answer_verification"]), 1)
        self.assertTrue(report["answer_verification"][0]["ok"])
        self.assertEqual(report["failures"], failures)
        self.assertEqual((self.project.kernel_objects.revisions("question", question_id),
                          self.project.kernel_objects.revisions("answer", answer_id)), revisions)


if __name__ == "__main__":
    unittest.main()
