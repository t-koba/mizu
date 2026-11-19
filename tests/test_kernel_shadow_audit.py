"""Bounded read-only audit of the stage-2 kernel shadow projections."""
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu import host_records, insight_tools, kernel_shadow_audit, operator_records as records
from mizu.cli import _cmd_kernel_shadow_audit, parser
from mizu.config import load
from mizu.kernel_shadow import (answer_object_id, insight_object_id,
                                insight_question_object_id, notice_object_id,
                                record_question_object_id)
from mizu.project import initialize

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "kernel_userland.toml"
QUESTION_DECLARATION = {
    "role.question": {"writer": "role.writer", "route": "operator_question",
                      "wake": False, "retention": "permanent",
                      "acknowledgement": "explicit", "subject": "object",
                      "maximum": 8192}}


class KernelShadowAudit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-shadow-audit-")
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

    def audit(self):
        return kernel_shadow_audit.audit(self.project)

    def insight(self, event="aa" * 16, addressee="operator"):
        return self.project.insights.submit(
            source="writer", title="Question", body="Which behavior?",
            base_snapshot=None, insight_id=event, addressee=addressee)

    def ask(self, event="question1"):
        return host_records.write_role(self.context, args={
            "kind": "role.question", "id": event, "subject": "{}",
            "payload": json.dumps({"body": "Which option?"})})

    def answer(self, subject, event="answer1", body="Use option A"):
        return records.write(self.project, event_id=event, kind="answer",
                             body=body, subject=subject)

    def notice(self, event="notice1"):
        return records.write(self.project, event_id=event, kind="operator.notice",
                             body="Exact notice", addressee="reviewer")

    def populate(self):
        insight = self.insight()
        self.project.insights.decide(
            insight["id"], "accept", "Accepted", "", "test-run",
            expected_rev=1, work_item_intent="none")
        answer = self.answer({"type": "insight", "id": insight["id"], "rev": 1})
        asked = self.ask()
        record_answer = self.answer(
            {"type": "record", "kind": asked["record"]["kind"],
             "id": asked["record"]["id"], "digest": asked["record_digest"]},
            event="answer2")
        notice = self.notice()
        return insight, answer, asked, record_answer, notice

    def corrupt_state(self, kind, object_id, state):
        """Tamper with the stored revision itself, not with a read result."""
        path = self.project.root / "objects" / kind / object_id / "00000001.json"
        record = json.loads(path.read_text())
        record["state"] = state
        path.write_text(json.dumps(record))

    def corrupted(self, kind, object_id, field, value):
        read = self.project.kernel_objects.read

        def divergent(read_kind, read_id, *args, **kwargs):
            current = read(read_kind, read_id, *args, **kwargs)
            if (read_kind, read_id) == (kind, object_id):
                return {**current, "fields": {**current["fields"], field: value}}
            return current

        return patch.object(self.project.kernel_objects, "read", side_effect=divergent)

    def rows(self, report, kind):
        return [row for row in report["mismatches"] if row["kind"] == kind]

    def tree(self):
        return {str(path.relative_to(self.project.root)): path.read_bytes()
                for path in self.project.root.rglob("*")
                if path.is_file() and not path.is_symlink()}

    def test_clean_projection_has_no_mismatches(self):
        insight, answer, asked, record_answer, notice = self.populate()
        report = self.audit()
        self.assertTrue(report["enabled"])
        self.assertFalse(report["truncated"])
        self.assertTrue(report["answers_complete"])
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["counts"],
                         {"insights": 1, "notices": 1, "role_questions": 1, "answers": 2})
        for key, value in report["counts"].items():
            self.assertEqual(report[key], value)
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(self.shadow().failures, [])
        self.assertEqual(self.project.kernel_objects.read("insight", insight["id"])["fields"]["body"],
                         "Which behavior?")
        self.assertEqual(records.store(self.project).get(answer["record"]["id"])[0],
                         answer["record"])
        self.assertEqual(records.store(self.project).get(record_answer["record"]["id"])[0],
                         record_answer["record"])
        self.assertEqual(records.store(self.project).get(notice["record"]["id"])[0],
                         notice["record"])
        self.assertEqual(self.project.kernel_objects.read("question", record_question_object_id(
            asked["record"], asked["record_digest"]))["fields"]["body"], "Which option?")

    def test_corrupted_insight_object_is_reported(self):
        insight, *_ = self.populate()
        with self.corrupted("insight", insight_object_id(insight), "body", "Tampered"):
            report = self.audit()
        rows = self.rows(report, "insight")
        self.assertEqual([row["record"] for row in rows], [insight["id"]])
        self.assertEqual(rows[0]["result"], "insight")
        self.assertIn("insight field body differs", rows[0]["mismatches"])

    def test_corrupted_notice_is_reported(self):
        *_, notice = self.populate()
        with self.corrupted("notice", notice_object_id(notice["record"]), "body", "Tampered"):
            report = self.audit()
        rows = self.rows(report, "notice")
        self.assertEqual([row["record"] for row in rows], [notice["record"]["id"]])
        self.assertEqual(rows[0]["result"], "notice")
        self.assertIn("notice field body differs", rows[0]["mismatches"])

    def test_corrupted_insight_question_is_reported(self):
        insight, *_ = self.populate()
        with self.corrupted("question", insight_question_object_id(insight), "body", "Tampered"):
            report = self.audit()
        rows = self.rows(report, "question")
        self.assertEqual([row["record"] for row in rows], [insight["id"]])
        self.assertEqual(rows[0]["result"], "question")
        self.assertIn("question field body differs", rows[0]["mismatches"])

    def test_corrupted_role_question_is_reported(self):
        _, _, asked, _, _ = self.populate()
        object_id = record_question_object_id(asked["record"], asked["record_digest"])
        with self.corrupted("question", object_id, "body", "Tampered"):
            report = self.audit()
        rows = self.rows(report, "question")
        self.assertEqual([row["record"] for row in rows], [asked["record"]["id"]])
        self.assertIn("question field body differs", rows[0]["mismatches"])

    def test_corrupted_answer_is_reported(self):
        _, answer, _, _, _ = self.populate()
        object_id = answer_object_id(answer["record"])
        with self.corrupted("answer", object_id, "action", "withdraw"):
            report = self.audit()
        rows = self.rows(report, "answer")
        self.assertEqual([row["record"] for row in rows], [answer["record"]["id"]])
        self.assertIn("answer field action differs", rows[0]["mismatches"])

    def test_unavailable_shadow_yields_bounded_unavailable_report(self):
        self.populate()
        self.project.insights.kernel_shadow = None
        head = self.project.kernel_events.head()
        before = self.tree()
        report = self.audit()
        self.assertEqual(report, {"enabled": False,
                                  "counts": {"insights": 0, "notices": 0,
                                             "role_questions": 0, "answers": 0},
                                  "insights": 0, "notices": 0, "role_questions": 0,
                                  "answers": 0, "mismatches": [], "truncated": False,
                                  "answers_complete": True})
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.tree(), before)

    def test_lowered_answer_scan_bound_never_claims_full_proof(self):
        insight, _, _, _, _ = self.populate()
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        with patch.object(self.shadow(), "answered",
                          side_effect=OSError("transient answer failure")):
            outside = self.answer(subject, event="outside-answer")
        outside_id = answer_object_id(outside["record"])
        self.assertEqual(self.project.kernel_objects.revisions("answer", outside_id), [])
        self.project.config = dataclasses.replace(
            self.project.config, limits=dataclasses.replace(
                self.project.config.limits, operator_record_catalog_scan_entries=1))
        with patch.object(self.shadow(), "verify_answer",
                          wraps=self.shadow().verify_answer) as verify:
            report = self.audit()
        self.assertFalse(report["answers_complete"])
        self.assertTrue(report["truncated"])
        self.assertEqual(report["counts"]["answers"], verify.call_count)
        self.assertTrue(any("incomplete" in message
                            for row in self.rows(report, "answer")
                            for message in row["mismatches"]))
        self.assertEqual(self.project.kernel_objects.revisions("answer", outside_id), [])

    def test_insight_bound_marks_truncation_and_reports_outside_answer(self):
        first = self.insight(event="11" * 16)
        second = self.insight(event="22" * 16)
        orphan = self.answer({"type": "insight", "id": second["id"], "rev": 1},
                             event="outside-answer")
        with patch.object(insight_tools, "INSIGHT_RETRIEVAL_LIMIT", 1):
            report = self.audit()
        self.assertTrue(report["truncated"])
        self.assertEqual(report["counts"]["insights"], 1)
        rows = self.rows(report, "answer")
        self.assertEqual([row["record"] for row in rows], [orphan["record"]["id"]])
        self.assertIn("outside the bounded authoritative set", rows[0]["mismatches"][0])
        self.assertEqual(first["id"], "11" * 16)

    def test_undecided_insight_state_corruption_is_reported(self):
        insight = self.insight(event="ud" * 16)
        self.assertIsNone(self.project.insights.decision(insight["id"]))
        self.assertEqual(self.audit()["mismatches"], [])
        self.corrupt_state("insight", insight_object_id(insight), "accepted")
        rows = self.rows(self.audit(), "insight")
        self.assertEqual([row["record"] for row in rows], [insight["id"]])
        self.assertIn("expected pending", rows[0]["mismatches"][0])

    def test_unanswered_insight_question_state_corruption_is_reported(self):
        insight = self.insight(event="uq" * 16)
        self.assertEqual(self.audit()["mismatches"], [])
        self.corrupt_state("question", insight_question_object_id(insight), "answered")
        rows = self.rows(self.audit(), "question")
        self.assertEqual([row["record"] for row in rows], [insight["id"]])
        self.assertIn("question state is answered, expected pending",
                      rows[0]["mismatches"])

    def test_unanswered_role_question_state_corruption_is_reported(self):
        asked = self.ask()
        self.assertEqual(self.audit()["mismatches"], [])
        object_id = record_question_object_id(asked["record"], asked["record_digest"])
        self.corrupt_state("question", object_id, "answered")
        rows = self.rows(self.audit(), "question")
        self.assertEqual([row["record"] for row in rows], [asked["record"]["id"]])
        self.assertIn("question state is answered, expected pending",
                      rows[0]["mismatches"])

    def test_command_is_read_only(self):
        insight, answer, _, _, notice = self.populate()
        head = self.project.kernel_events.head()
        failures = list(self.shadow().failures)
        stored = {row["record"]["id"]: row["record"] for row in (answer, notice)}
        before = self.tree()
        args = parser().parse_args(["kernel-shadow-audit", self.project.name])
        report = _cmd_kernel_shadow_audit(self.config, self.project, args)
        self.assertTrue(report["enabled"])
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.shadow().failures, failures)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.project.insights.read(insight["id"])["id"], insight["id"])
        for record_id, record in stored.items():
            self.assertEqual(records.store(self.project).get(record_id)[0], record)


if __name__ == "__main__":
    unittest.main()
