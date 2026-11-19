"""Read-only stage-2 projection built entirely from declared queue messages."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from unittest import mock
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from support import Fixture, stored_question_insight
from mizu import operator_records as records
from mizu.config import load
from mizu.fs import canonical
from mizu.kernel import QueueStore
from mizu.project import Project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_view  # noqa: E402  (example userland module)

QUEUES = ROOT / "examples" / "development" / "queues.toml"
ACCEPT_ID = "1" * 32
DEFER_ID = "2" * 32
QUESTION_ID = "3" * 32
ANSWER_ID = "4" * 32
NOTICE_ID = "5" * 32
REVISIT_AT = "2099-01-01T00:00:00+00:00"
BEFORE_REVISIT = "2098-12-31T23:59:59+00:00"
AFTER_REVISIT = "2099-01-01T00:00:01+00:00"
DEFER_BODY = {"action": "defer", "reason": "Need more evidence", "revisit": REVISIT_AT}


class Stage2View(Fixture):
    def setUp(self):
        super().setUp()
        shutil.copyfile(QUEUES, self.file.parent / "queues.toml")
        self.file.write_text('include = ["queues.toml"]\n' + self.file.read_text())
        self.config = load(self.file)
        self.store = QueueStore(self.project.root, self.config.queues)
        # The fixture project predates the queue include; the reader sees the
        # same deployment configuration the CLI loads from disk.
        self.view_project = Project(self.config, "sample", read_only=True)

        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Legacy proposal",
                                     body="Legacy body", base_snapshot=snap["id"],
                                     insight_id=ACCEPT_ID)
        self.project.insights.decide(ACCEPT_ID, "accept", "Reviewed", "", "test-run",
                                     expected_rev=1, work_item_intent="none")
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Deferred proposal",
                                     body="Deferred body", base_snapshot=snap["id"],
                                     insight_id=DEFER_ID)
        self.project.insights.decide(DEFER_ID, "defer", "Need more evidence", REVISIT_AT,
                                     "test-run", expected_rev=1)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 0)
        self.report = json.loads(output.getvalue())

        self.store.append("questions", {"question": "Which source?", "context": "Stage 2",
                                        "default": "kernel queues"}, "role:worker",
                          id=QUESTION_ID, now=REVISIT_AT)
        self.store.append("answers", {"body": "Use the declared queues."}, "operator",
                          refs=[QUESTION_ID], id=ANSWER_ID, now=REVISIT_AT)
        self.store.append("notices", {"body": "Maintenance window."}, "operator",
                          id=NOTICE_ID, now=REVISIT_AT)

    def proposal_id(self, insight_id):
        return convert_legacy._message_id("proposal", insight_id, 1)

    def projection_with(self, queue: str, message: dict) -> dict:
        """Inject one observed message without writing to a durable queue."""
        original = stage2_view._read_queue

        def read(store, name, now):
            messages, meta = original(store, name, now)
            return ([*messages, message] if name == queue else messages), meta

        with mock.patch.object(stage2_view, "_read_queue", side_effect=read):
            return stage2_view.project_view(self.view_project, now=REVISIT_AT)

    def test_dangling_decision_ref_is_refused(self):
        missing = "d" * 32
        with self.assertRaisesRegex(stage2_view.ViewError,
                                    rf"decisions message dangling-decision: "
                                    rf"unresolved ref '{missing}' is not a proposals id"):
            self.projection_with("decisions", {
                "id": "dangling-decision", "refs": [missing],
                "body": {"action": "accept", "reason": "Missing target"}})

    def test_dangling_answer_ref_is_refused(self):
        missing = "e" * 32
        with self.assertRaisesRegex(stage2_view.ViewError,
                                    rf"answers message dangling-answer: "
                                    rf"unresolved ref '{missing}' is not a proposals or questions id"):
            self.projection_with("answers", {
                "id": "dangling-answer", "refs": [missing],
                "body": {"body": "Missing target"}})

    def test_wrong_direction_refs_are_refused(self):
        cases = (("decisions", "wrong-decision", QUESTION_ID, "proposals",
                  {"action": "accept", "reason": "Wrong target"}),
                 ("answers", "wrong-answer", NOTICE_ID, "proposals or questions",
                  {"body": "Wrong target"}))
        for queue, message_id, ref, allowed, body in cases:
            with self.subTest(queue=queue):
                with self.assertRaisesRegex(
                        stage2_view.ViewError,
                        rf"{queue} message {message_id}: "
                        rf"unresolved ref '{ref}' is not a {allowed} id"):
                    self.projection_with(queue, {"id": message_id, "refs": [ref],
                                                 "body": body})

    def test_answer_with_multiple_valid_refs_is_refused(self):
        refs = [self.proposal_id(ACCEPT_ID), self.proposal_id(DEFER_ID)]
        self.store.append("answers", {"body": "Shared answer"}, "operator",
                          refs=refs, id="6" * 32, now=REVISIT_AT)
        with self.assertRaisesRegex(stage2_view.ViewError,
                                    "answers message " + "6" * 32 +
                                    ": expected exactly one proposals or questions ref, got 2"):
            stage2_view.project_view(self.view_project, now=REVISIT_AT)

    def test_missing_refs_are_refused(self):
        for queue, message_id, body, allowed in (
                ("decisions", "no-decision-ref", {"action": "accept", "reason": "None"},
                 "proposals"),
                ("answers", "no-answer-ref", {"body": "No target"},
                 "proposals or questions")):
            with self.subTest(queue=queue):
                with self.assertRaisesRegex(
                        stage2_view.ViewError,
                        rf"{queue} message {message_id}: "
                        rf"expected exactly one {allowed} ref, got 0"):
                    self.projection_with(queue, {"id": message_id, "refs": [], "body": body})

    def test_projection_maps_decisions_revisits_answers_and_notices(self):
        before = stage2_view.project_view(self.view_project, now=BEFORE_REVISIT)
        proposals = {item["id"]: item for item in before["proposals"]}
        self.assertEqual(set(proposals),
                         {self.proposal_id(ACCEPT_ID), self.proposal_id(DEFER_ID)})

        accepted = proposals[self.proposal_id(ACCEPT_ID)]
        self.assertEqual((accepted["title"], accepted["body"]), ("Legacy proposal", "Legacy body"))
        self.assertEqual(accepted["decision"], {"action": "accept", "reason": "Reviewed"})
        self.assertEqual(accepted["revisits"], [])

        deferred = proposals[self.proposal_id(DEFER_ID)]
        self.assertEqual((deferred["title"], deferred["body"]), ("Deferred proposal", "Deferred body"))
        self.assertEqual(deferred["decision"], DEFER_BODY)
        self.assertEqual(deferred["revisits"], [])

        self.assertEqual(before["questions"], [
            {"id": QUESTION_ID, "question": "Which source?", "context": "Stage 2",
             "default": "kernel queues", "answer": {"body": "Use the declared queues."}}])
        self.assertEqual(before["notices"], [{"body": "Maintenance window."}])
        self.assertEqual(before["queues"], {
            "proposals": {"head": 2, "delayed": False},
            "decisions": {"head": 3, "delayed": True},
            "questions": {"head": 1, "delayed": False},
            "answers": {"head": 1, "delayed": False},
            "notices": {"head": 1, "delayed": False}})

        due = stage2_view.project_view(self.view_project, now=REVISIT_AT)
        revisit = due["proposals"][1]["revisits"]
        self.assertEqual(len(revisit), 1)
        self.assertEqual(revisit[0]["id"], self.report["revisits"][0])
        self.assertEqual(revisit[0]["deliver_at"], REVISIT_AT)
        decision = self.store.message(convert_legacy._message_id("decision", DEFER_ID, 1),
                                      "operator")
        self.assertEqual(revisit[0]["body"], decision["body"])
        self.assertEqual({key: revisit[0]["body"][key] for key in DEFER_BODY}, DEFER_BODY)
        self.assertFalse(due["queues"]["decisions"]["delayed"])
        self.assertEqual(due["proposals"][0]["revisits"], [])
        self.assertEqual(due["questions"], before["questions"])
        self.assertEqual(due["notices"], before["notices"])

        after = stage2_view.project_view(self.view_project, now=AFTER_REVISIT)
        self.assertEqual(after, due)

    def test_converted_insight_answer_is_shown_on_its_proposal(self):
        insight = stored_question_insight(self.project)
        subject = {"type": "insight", "id": insight["id"], "rev": insight["rev"]}
        answer = records.write(self.project, event_id="insight-answer", kind="answer",
                               body="Legacy insight answer", subject=subject)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 0)

        view = stage2_view.project_view(self.view_project, now=REVISIT_AT)
        proposals = {item["id"]: item for item in view["proposals"]}
        proposal = proposals[self.proposal_id(insight["id"])]
        self.assertEqual(proposal["answer"], {
            "body": "Legacy insight answer",
            "legacy_addressee": "worker",
            "legacy_subject": canonical(subject).decode(),
            "legacy_writer": "operator.cli",
            "legacy_id": answer["record"]["id"],
            "legacy_digest": answer["record_digest"],
            "legacy_action": "publish",
            "legacy_created_at": answer["record"]["created_at"]})
        self.assertEqual(view["questions"], [
            {"id": QUESTION_ID, "question": "Which source?", "context": "Stage 2",
             "default": "kernel queues", "answer": {"body": "Use the declared queues."}}])
        self.assertEqual(view, stage2_view.project_view(self.view_project, now=REVISIT_AT))

    def test_ambiguous_proposal_answer_is_refused(self):
        proposal_id = self.proposal_id(ACCEPT_ID)
        for body in ("First answer", "Second answer"):
            self.store.append("answers", {"body": body}, "operator", refs=[proposal_id],
                              now=REVISIT_AT)
        with self.assertRaisesRegex(stage2_view.ViewError,
                                    f"Ambiguous answer for {proposal_id}"):
            stage2_view.project_view(self.view_project, now=REVISIT_AT)

    def test_repeated_projection_acknowledges_nothing(self):
        first = stage2_view.project_view(self.view_project, now=REVISIT_AT)
        second = stage2_view.project_view(self.view_project, now=REVISIT_AT)
        self.assertEqual(first, second)
        self.assertEqual([page["head"] for page in first["queues"].values()],
                         [2, 3, 1, 1, 1])
        self.assertEqual([page["head"] for page in second["queues"].values()],
                         [2, 3, 1, 1, 1])
        self.assertEqual(list((self.project.root / "queues").glob("*/cursors/*")), [])
        for queue in stage2_view.QUEUES:
            read = self.store.read(queue, "operator", now=REVISIT_AT)
            self.assertEqual(read, self.store.read(queue, "operator", now=REVISIT_AT))
            self.assertEqual(read["head"], first["queues"][queue]["head"])
            self.assertEqual(read["next"], read["head"])

    def test_page_bound_refuses_instead_of_projecting_a_partial_queue(self):
        with mock.patch.object(stage2_view, "PAGE", 1), \
                mock.patch.object(stage2_view, "MAX_PAGES", 1):
            with self.assertRaisesRegex(stage2_view.ViewError, "proposals exceeded"):
                stage2_view.project_view(self.view_project, now=REVISIT_AT)

    def test_cli_prints_the_same_projection(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = stage2_view.main([str(self.file), "--project", "sample",
                                     "--at", REVISIT_AT])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         stage2_view.project_view(self.view_project, now=REVISIT_AT))


if __name__ == "__main__":
    unittest.main()
