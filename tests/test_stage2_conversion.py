"""Operator conversion of current legacy insights/decisions into declared queues."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu import operator_records as records
from mizu import record_reconciliation
from mizu.config import load
from mizu.fs import canonical
from mizu.kernel import QueueStore
from mizu.project import Project
from mizu.shared_records import _token

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_verify  # noqa: E402  (example userland module)

QUEUES = ROOT / "examples" / "development" / "queues.toml"
INSIGHT_ID = "a" * 32
DEFER_ID = "c" * 32
REVISIT_AT = "2099-01-01T00:00:00+00:00"
BEFORE_REVISIT = "2098-12-31T23:59:59+00:00"
AFTER_REVISIT = "2099-01-01T00:00:01+00:00"
QUESTION_DECLARATION = """
[record_kinds."role.question"]
writer = "role.worker"
route = "operator_question"
wake = false
retention = "permanent"
acknowledgement = "explicit"
subject = "object"
maximum = 8192
"""
QUESTION_PAYLOAD = {"body": "Which option?", "source": "reviewer"}


def tree_state(root):
    """File names, bytes and mtimes: any write in a dry run is visible here."""
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


class Stage2Conversion(Fixture):
    def setUp(self):
        super().setUp()
        shutil.copyfile(QUEUES, self.file.parent / "queues.toml")
        self.file.write_text('include = ["queues.toml"]\n' + self.file.read_text())
        self.config = load(self.file)
        self.store = QueueStore(self.project.root, self.config.queues)
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Legacy proposal",
                                     body="Legacy body", base_snapshot=snap["id"],
                                     insight_id=INSIGHT_ID)
        self.project.insights.attach_evidence(
            INSIGHT_ID, source="worker", text="Legacy evidence text", run="test-run",
            expected_rev=1)
        self.project.insights.decide(INSIGHT_ID, "accept", "Reviewed", "", "test-run",
                                     expected_rev=1, work_item_intent="none")

    def add_deferral(self, insight_id, revisit):
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Deferred proposal",
                                     body="Deferred body", base_snapshot=snap["id"],
                                     insight_id=insight_id)
        self.project.insights.attach_evidence(
            insight_id, source="worker", text="Deferred evidence text", run="test-run",
            expected_rev=1)
        self.project.insights.decide(insight_id, "defer", "Need more evidence", revisit,
                                     "test-run", expected_rev=1)

    def legacy_metadata(self, insight_id):
        """Read the exact metadata the conversion must retain for one insight."""
        summary = next(item for item in self.project.insights.list_page(
            scope="live", limit=1000)["items"] if item["id"] == insight_id)
        record = self.project.insights.read(insight_id)
        proposal = convert_legacy._legacy_metadata(
            summary, record, None, self.config.queues["proposals"])
        decision = summary["decision"]
        source = convert_legacy._decision_source(
            self.project, insight_id, summary["rev"], decision)
        decision_metadata = convert_legacy._legacy_metadata(
            summary, record, decision, self.config.queues["decisions"], source)
        return proposal, decision_metadata

    def convert(self, *flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", *flags])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_apply_twice_appends_one_proposal_and_one_referencing_decision(self):
        first = self.convert("--apply")
        second = self.convert("--apply")
        self.assertEqual(first, second)
        self.assertEqual((first["proposals"], first["decisions"]),
                         ([convert_legacy._message_id("proposal", INSIGHT_ID, 1)],
                          [convert_legacy._message_id("decision", INSIGHT_ID, 1)]))

        proposal_metadata, decision_metadata = self.legacy_metadata(INSIGHT_ID)
        proposals = self.store.read("proposals", "operator")["messages"]
        decisions = self.store.read("decisions", "operator")["messages"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(proposals[0]["id"], first["proposals"][0])
        self.assertEqual(proposals[0]["author"], "operator")
        self.assertEqual(proposals[0]["body"],
                         {"title": "Legacy proposal", "body": "Legacy body",
                          **proposal_metadata})
        self.assertEqual(decisions[0]["id"], first["decisions"][0])
        self.assertEqual(decisions[0]["body"],
                         {"action": "accept", "reason": "Reviewed", **decision_metadata})
        self.assertIn("Legacy evidence text", decision_metadata["legacy_evidence"])
        self.assertRegex(decision_metadata["legacy_event"], r"[0-9a-f]{32}\Z")
        self.assertEqual(decisions[0]["refs"], [proposals[0]["id"]])
        self.assertEqual(self.store.message(decisions[0]["refs"][0], "operator"), proposals[0])
        self.assertEqual((self.store.read("proposals", "operator")["head"],
                          self.store.read("decisions", "operator")["head"]), (1, 1))
        # Legacy records remain untouched: conversion is a projection, not a cutover.
        self.assertEqual(self.project.insights.read(INSIGHT_ID)["title"], "Legacy proposal")
        self.assertEqual(self.project.insights.decision(INSIGHT_ID)["reason"], "Reviewed")

    def test_deferred_revisit_is_delayed_and_idempotent(self):
        self.add_deferral(DEFER_ID, REVISIT_AT)
        revisit_id = convert_legacy._message_id("revisit", DEFER_ID, 1)
        decision_id = convert_legacy._message_id("decision", DEFER_ID, 1)
        proposal_id = convert_legacy._message_id("proposal", DEFER_ID, 1)
        self.assertNotEqual(revisit_id, decision_id)

        preview = self.convert()
        self.assertEqual(preview["revisits"], [revisit_id])
        self.assertNotIn(revisit_id, preview["decisions"])
        self.assertFalse((self.project.root / "queues").exists())
        first = self.convert("--apply")
        second = self.convert("--apply")
        self.assertEqual(first, second)
        self.assertEqual(first["revisits"], [revisit_id])

        before = self.store.read("decisions", "operator", now=BEFORE_REVISIT)
        self.assertNotIn(revisit_id, [m["id"] for m in before["messages"]])
        self.assertTrue(before["delayed"])
        due = self.store.read("decisions", "operator", now=REVISIT_AT)
        delayed = [m for m in due["messages"] if m["id"] == revisit_id]
        self.assertEqual(len(delayed), 1)
        self.assertEqual(delayed[0]["deliver_at"], REVISIT_AT)
        defer_metadata = self.legacy_metadata(DEFER_ID)[1]
        defer_body = {"action": "defer", "reason": "Need more evidence", "revisit": REVISIT_AT,
                      **defer_metadata}
        self.assertIn("Deferred evidence text", defer_metadata["legacy_evidence"])
        self.assertEqual(delayed[0]["body"], defer_body)
        ordinary = [m for m in due["messages"] if m["id"] == decision_id]
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(ordinary[0]["body"], defer_body)
        self.assertEqual(delayed[0]["refs"], [proposal_id])
        self.assertEqual(delayed[0]["author"], "role:writer")
        after = self.store.read("decisions", "operator", now=AFTER_REVISIT)
        self.assertEqual([m["id"] for m in after["messages"]].count(revisit_id), 1)
        self.assertEqual((after["head"], len(after["messages"])), (3, 3))

    def test_revisit_without_an_instant_has_no_delayed_message(self):
        self.add_deferral(DEFER_ID, "when the next build finishes")
        report = self.convert("--apply")
        self.assertEqual(report["revisits"], [])
        self.assertEqual(report["decisions"],
                         [convert_legacy._message_id("decision", INSIGHT_ID, 1),
                          convert_legacy._message_id("decision", DEFER_ID, 1)])
        decisions = self.store.read("decisions", "operator")["messages"]
        deferred = [m for m in decisions
                    if m["id"] == convert_legacy._message_id("decision", DEFER_ID, 1)]
        self.assertEqual(deferred[0]["body"],
                         {"action": "defer", "reason": "Need more evidence",
                          "revisit": "when the next build finishes",
                          **self.legacy_metadata(DEFER_ID)[1]})

    def test_malformed_revisit_instant_refuses_before_any_write(self):
        self.add_deferral(DEFER_ID, "2099-13-01T00:00:00+00:00")
        before = tree_state(self.project.root)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertIn("parseable aware instant", errors.getvalue())
        self.assertFalse((self.project.root / "queues").exists())
        self.assertEqual(tree_state(self.project.root), before)

    def test_dry_run_writes_nothing(self):
        before = tree_state(self.project.root)
        report = self.convert()
        self.assertFalse(report["applied"])
        self.assertEqual(len(report["proposals"]), 1)
        self.assertEqual(len(report["decisions"]), 1)
        self.assertFalse((self.project.root / "queues").exists())
        self.assertEqual(tree_state(self.project.root), before)

    def refuse_metadata(self, *, page=None, record=None):
        """Run the real CLI against a legacy view with one field changed."""
        before = tree_state(self.project.root)
        real_project = convert_legacy.Project

        def project_factory(config, name, *, read_only):
            project = real_project(config, name, read_only=read_only)
            if page is not None:
                original_page = project.insights.list_page

                def list_page(**kwargs):
                    result = original_page(**kwargs)
                    items = [(page(item) if item["id"] == INSIGHT_ID else item)
                             for item in result["items"]]
                    return {**result, "items": items}

                project.insights.list_page = list_page
            if record is not None:
                original_read = project.insights.read

                def read(insight_id):
                    result = original_read(insight_id)
                    return record(result) if insight_id == INSIGHT_ID else result

                project.insights.read = read
            return project

        with mock.patch.object(convert_legacy, "Project", side_effect=project_factory):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertFalse((self.project.root / "queues").exists())
        self.assertEqual(tree_state(self.project.root), before)
        return errors.getvalue()

    def test_malformed_legacy_metadata_refuses_before_any_write(self):
        cases = (
            (lambda item: {**item, "source": None}, "Legacy insight source"),
            (lambda item: {**item, "addressee": " "}, "Legacy insight addressee"),
            (lambda item: {**item, "decision": {**item["decision"],
                                               "decision_key": "g" * 32}},
             "Legacy decision key"),
            (lambda item: {**item, "decision": {**item["decision"],
                                               "created_at": "2099-01-01T00:00:00"}},
             "Legacy decision instant"),
            (None, "Legacy evidence is not a list"),
        )
        for page, expected in cases:
            with self.subTest(expected=expected):
                record = (lambda value: {**value, "evidence": "not a list"}) if page is None else None
                self.assertIn(expected, self.refuse_metadata(page=page, record=record))

    def test_malformed_legacy_record_refuses_before_any_write(self):
        from support import write_fixture_json
        other = "b" * 32
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Bad body", body="x",
                                     base_snapshot=snap["id"], insight_id=other)
        item = json.loads((self.project.root / "inbox" / f"{other}.json").read_text())
        path = self.project.root / "inbox" / f"{other}.json"
        path.unlink()
        write_fixture_json(path, {**item, "body": "y" * 100000})
        support.forget_ledger(self.project.root)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertIn("exceeds the declared", errors.getvalue())
        self.assertFalse((self.project.root / "queues").exists())


class Stage2QuestionConversion(Fixture):
    """Declared role questions, their answers and notices become queue messages."""

    def setUp(self):
        super().setUp()
        shutil.copyfile(QUEUES, self.file.parent / "queues.toml")
        text = 'include = ["queues.toml"]\n' + self.file.read_text()
        index = text.index("capabilities = [", text.index("[roles.worker]")) + len("capabilities = [")
        self.file.write_text(text[:index] + '"record_write", ' + text[index:] + QUESTION_DECLARATION)
        self.config = load(self.file)
        self.project = Project(self.config, "sample")
        self.store = QueueStore(self.project.root, self.config.queues)
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Legacy proposal",
                                     body="Legacy body", base_snapshot=snap["id"],
                                     insight_id=INSIGHT_ID)
        self.project.insights.decide(INSIGHT_ID, "accept", "Reviewed", "", "test-run",
                                     expected_rev=1, work_item_intent="none")
        question = self.context().handle("record_write", dict(
            kind="role.question", id="question1", subject="{}",
            payload=json.dumps(QUESTION_PAYLOAD)))
        self.question_record = question["record"]
        self.question_digest = question["record_digest"]
        self.subject = dict(type="record", kind="role.question",
                            id=self.question_record["id"], digest=self.question_digest)
        self.answer = records.write(self.project, event_id="answer1", kind="answer",
                                    body="Use option A", subject=self.subject)
        self.notice = records.write(self.project, event_id="notice1", kind="operator.notice",
                                    body="Answer in prose", addressee="worker")

    def convert(self, *flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = convert_legacy.main([str(self.file), "--project", "sample", *flags])
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def ids(self):
        return (convert_legacy._message_id(
                    "question", f"{self.subject['kind']}/{self.subject['id']}",
                    self.question_digest),
                convert_legacy._message_id("answer", self.answer["record"]["id"],
                                           self.answer["record_digest"]),
                convert_legacy._message_id("notice", self.notice["record"]["id"],
                                           self.notice["record_digest"]))

    def legacy_state(self):
        root = self.project.root
        return {str(path.relative_to(root)): path.read_bytes()
                for name in ("inbox", "shared-records", "operator-record-index",
                             "insight-revisions")
                for path in sorted((root / name).rglob("*")) if path.is_file()}

    def test_question_answer_notice_convert_exactly_and_replay_appends_nothing(self):
        question_id, answer_id, notice_id = self.ids()
        legacy_before = self.legacy_state()
        preview = self.convert()
        self.assertFalse(preview["applied"])
        self.assertEqual((preview["questions"], preview["answers"], preview["notices"]),
                         ([question_id], [answer_id], [notice_id]))
        first = self.convert("--apply")
        second = self.convert("--apply")
        self.assertEqual(first, {**preview, "applied": True})
        self.assertEqual(second, first)
        self.assertEqual(self.legacy_state(), legacy_before)

        questions = self.store.read("questions", "operator")["messages"]
        answers = self.store.read("answers", "operator")["messages"]
        notices = self.store.read("notices", "operator")["messages"]
        self.assertEqual([m["id"] for m in questions], [question_id])
        self.assertEqual([m["id"] for m in answers], [answer_id])
        self.assertEqual([m["id"] for m in notices], [notice_id])
        self.assertEqual(questions[0]["author"], "role:worker")
        self.assertEqual(questions[0]["body"], {
            "question": "Which option?",
            "legacy_payload": canonical(QUESTION_PAYLOAD).decode(),
            "legacy_subject": canonical({}).decode(),
            "legacy_writer": "role.worker",
            "legacy_id": self.question_record["id"],
            "legacy_digest": self.question_digest,
            "legacy_kind": self.question_record["kind"],
            "legacy_created_at": self.question_record["created_at"]})
        self.assertEqual(answers[0]["author"], "operator")
        self.assertEqual(answers[0]["refs"], [question_id])
        self.assertEqual(self.store.message(question_id, "operator"), questions[0])
        self.assertEqual(answers[0]["body"], {
            "body": "Use option A",
            "legacy_addressee": "worker",
            "legacy_subject": canonical(self.subject).decode(),
            "legacy_writer": "operator.cli",
            "legacy_id": self.answer["record"]["id"],
            "legacy_digest": self.answer["record_digest"],
            "legacy_action": "publish",
            "legacy_created_at": self.answer["record"]["created_at"]})
        self.assertEqual(notices[0]["author"], "operator")
        self.assertEqual(notices[0]["body"], {
            "body": "Answer in prose",
            "legacy_addressee": "worker",
            "legacy_subject": canonical({}).decode(),
            "legacy_writer": "operator.cli",
            "legacy_id": self.notice["record"]["id"],
            "legacy_digest": self.notice["record_digest"],
            "legacy_action": "publish",
            "legacy_target": canonical(None).decode(),
            "legacy_created_at": self.notice["record"]["created_at"]})
        self.assertEqual((self.store.read("questions", "operator")["head"],
                          self.store.read("answers", "operator")["head"],
                          self.store.read("notices", "operator")["head"]), (1, 1, 1))
        # The proposal/decision family converts in the same run, unchanged.
        self.assertEqual(len(preview["proposals"]), 1)
        self.assertEqual(len(preview["decisions"]), 1)

    def test_dry_run_writes_nothing(self):
        before = tree_state(self.project.root)
        report = self.convert()
        self.assertEqual(len(report["questions"]), 1)
        self.assertEqual(len(report["answers"]), 1)
        self.assertEqual(len(report["notices"]), 1)
        self.assertEqual(tree_state(self.project.root), before)

    def refuse(self):
        before = self.legacy_state()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertFalse((self.project.root / "queues").exists())
        self.assertEqual(self.legacy_state(), before)
        return errors.getvalue()

    def test_insight_answer_converts_to_proposal_referencing_answer(self):
        from support import stored_question_insight
        insight = stored_question_insight(self.project)
        subject = {"type": "insight", "id": insight["id"], "rev": insight["rev"]}
        answer = records.write(self.project, event_id="insight-answer", kind="answer",
                               body="Legacy insight answer", subject=subject)
        proposal_id = convert_legacy._message_id("proposal", insight["id"], insight["rev"])
        answer_id = convert_legacy._message_id("answer", answer["record"]["id"],
                                               answer["record_digest"])
        question_id, question_answer_id, _ = self.ids()
        legacy_before = self.legacy_state()

        messages, report = convert_legacy._plan(self.project)
        self.assertEqual(report["answers"], [question_answer_id, answer_id])
        self.assertIn(proposal_id, report["proposals"])
        ids = [message["id"] for message in messages]
        self.assertLess(ids.index(proposal_id), ids.index(answer_id))

        first = self.convert("--apply")
        self.assertEqual(first, {**report, "applied": True})
        self.assertEqual(self.convert("--apply"), first)
        self.assertEqual(self.legacy_state(), legacy_before)

        answers = self.store.read("answers", "operator")["messages"]
        self.assertEqual([m["id"] for m in answers], [question_answer_id, answer_id])
        self.assertEqual(answers[0]["refs"], [question_id])
        self.assertEqual(answers[1]["refs"], [proposal_id])
        self.assertEqual(answers[1]["author"], "operator")
        self.assertEqual(answers[1]["body"], {
            "body": "Legacy insight answer",
            "legacy_addressee": "worker",
            "legacy_subject": canonical(subject).decode(),
            "legacy_writer": "operator.cli",
            "legacy_id": answer["record"]["id"],
            "legacy_digest": answer["record_digest"],
            "legacy_action": "publish",
            "legacy_created_at": answer["record"]["created_at"]})
        self.assertEqual(self.store.message(proposal_id, "operator")["queue"], "proposals")

    def test_archived_revision_answer_binds_its_exact_revision_proposal(self):
        """An answer accepted against a retained r1 never follows the live r2."""
        from support import history_write_json, stored_question_insight
        insight = stored_question_insight(self.project)
        root = self.project.root
        archived = {**insight, "superseded_by_rev": 2,
                    "superseded_at": insight["updated_at"]}
        history_write_json(root / "insight-revisions" / f"{insight['id']}.r1.json", archived)
        history_write_json(root / "inbox" / f"{insight['id']}.json",
                           {**insight, "rev": 2, "title": "Legacy question rev 2",
                            "body": "Q: Which? (rev 2)"})
        self.assertEqual(self.project.insights.read(insight["id"])["rev"], 2)
        self.assertEqual(self.project.insights.read_revision(insight["id"], 1), archived)
        # The legacy operator accepted this answer against the archived question
        # after revision 2 was already live.
        subject = {"type": "insight", "id": insight["id"], "rev": 1}
        answer = records.write(self.project, event_id="archived-question-answer",
                               kind="answer", body="Legacy rev-1 answer", subject=subject)
        answer_id = convert_legacy._message_id("answer", answer["record"]["id"],
                                               answer["record_digest"])
        proposal_id = convert_legacy._message_id("proposal", insight["id"], 1)
        live_proposal_id = convert_legacy._message_id("proposal", insight["id"], 2)

        messages, report = convert_legacy._plan(self.project)
        self.assertIn(proposal_id, report["proposals"])
        self.assertIn(live_proposal_id, report["proposals"])
        self.assertEqual(report["proposals"].count(proposal_id), 1)
        archived_proposal = next(m for m in messages if m["id"] == proposal_id)
        self.assertEqual(archived_proposal["queue"], "proposals")
        self.assertEqual(archived_proposal["author"], "operator")
        self.assertEqual(archived_proposal["refs"], ())
        self.assertEqual(archived_proposal["body"]["title"], archived["title"])
        self.assertEqual(archived_proposal["body"]["body"], archived["body"])
        self.assertEqual(archived_proposal["body"]["legacy_rev"], "1")
        self.assertEqual(archived_proposal["body"]["legacy_kind"], "question")
        answer_message = next(m for m in messages if m["id"] == answer_id)
        self.assertEqual(answer_message["refs"], (proposal_id,))
        ids = [m["id"] for m in messages]
        self.assertLess(ids.index(proposal_id), ids.index(answer_id))

        first = self.convert("--apply")
        second = self.convert("--apply")
        self.assertEqual(first, second)
        self.assertEqual(first, {**report, "applied": True})
        stored_answer = self.store.message(answer_id, "operator")
        self.assertEqual(stored_answer["refs"], [proposal_id])
        self.assertNotEqual(stored_answer["refs"], [live_proposal_id])
        self.assertEqual(self.store.message(proposal_id, "operator")["body"],
                         archived_proposal["body"])
        gate = stage2_verify.verify(self.project, queues=convert_legacy.VERIFY_QUEUES)
        self.assertTrue(gate["ok"], gate["differences"])
        marker = record_reconciliation.activate_queue_record_delivery(
            self.project, conversion=first, verification=gate)
        self.assertEqual(marker["authority"], "queues")
        retry = record_reconciliation.activate_queue_record_delivery(
            self.project, conversion=first, verification=gate)
        self.assertEqual(retry, marker)

    def test_answer_to_revised_insight_refuses_before_any_append(self):
        from support import history_write_json, stored_question_insight
        insight = stored_question_insight(self.project)
        records.write(self.project, event_id="insight-answer", kind="answer",
                      body="Legacy insight answer",
                      subject={"type": "insight", "id": insight["id"], "rev": 1})
        # Only the original release could revise this retained question insight.
        path = self.project.root / "inbox" / f"{insight['id']}.json"
        history_write_json(path, {**json.loads(path.read_text()), "rev": 2})
        self.assertIn("does not bind a converted insight revision", self.refuse())

    def test_answer_to_nonexistent_insight_revision_refuses_before_any_append(self):
        import copy
        from support import stored_question_insight
        insight = stored_question_insight(self.project)
        answer = records.write(self.project, event_id="insight-answer", kind="answer",
                               body="Legacy insight answer",
                               subject={"type": "insight", "id": insight["id"], "rev": 1})
        envelope = copy.deepcopy(answer["record"])
        envelope["id"] = "operator/sample/missing-revision"
        envelope["subject"] = {"type": "insight", "id": insight["id"], "rev": 2}
        records.store(self.project).put(envelope, writer=records.WRITER)
        self.assertIn("does not bind a converted insight revision", self.refuse())


class Stage2RecordReceiptConversion(Stage2QuestionConversion):
    """Converted SharedRecords acknowledgements and the two verify queue sets."""

    def acknowledge_question(self):
        role = self.config.roles["worker"]
        page, manifest = records.pending(self.project, role)
        context = self.context("worker")
        records.bind(context, (page, manifest))
        records.acknowledge(context, {manifest[0].ack_token})
        return manifest[0]

    def test_receipt_converts_exactly_replays_and_verifies_both_queue_sets(self):
        entry = self.acknowledge_question()
        legacy_receipt_bytes = next(
            (self.project.root / "shared-record-receipts").rglob("*.json")).read_bytes()
        token = _token("worker", entry.record_id, entry.record_digest)
        self.assertEqual(token, entry.ack_token)
        receipt_id = convert_legacy._record_receipt_id("worker", token)
        self.assertRegex(receipt_id, r"[0-9a-f]{32}\Z")
        self.assertEqual(convert_legacy.QUEUES, ("proposals", "decisions", "questions",
                                                 "answers", "notices", "receipts"))
        self.assertEqual(convert_legacy.VERIFY_QUEUES,
                         (*convert_legacy.QUEUES, "record-receipts"))

        preview = self.convert()
        self.assertEqual(preview["record_receipts"], [receipt_id])
        first = self.convert("--apply")
        self.assertEqual(first, {**preview, "applied": True})
        self.assertEqual(self.convert("--apply"), first)

        rows = self.store.read("record-receipts", "host")["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual({key: rows[0][key] for key in
                          ("queue", "id", "author", "refs", "body")},
                         {"queue": "record-receipts", "id": receipt_id,
                          "author": "host", "refs": [],
                          "body": {"ack_token": token,
                                   "legacy_role": "worker",
                                   "legacy_record_id": entry.record_id,
                                   "legacy_record_digest": entry.record_digest}})
        # The legacy receipt file stays authoritative and byte-identical.
        paths = list((self.project.root / "shared-record-receipts").rglob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), legacy_receipt_bytes)

        default = stage2_verify.verify(self.project)
        self.assertEqual(tuple(default["checked"]), convert_legacy.QUEUES)
        self.assertTrue(default["ok"])
        explicit = stage2_verify.verify(self.project, queues=convert_legacy.VERIFY_QUEUES)
        self.assertEqual(tuple(explicit["checked"]), convert_legacy.VERIFY_QUEUES)
        self.assertTrue(explicit["ok"])
        self.assertEqual(explicit["checked"]["record-receipts"],
                         {"planned": 1, "stored": 1})

    def test_undeclared_or_repeated_verify_queues_are_refused(self):
        for queues in (("record-receipts", "record-receipts"), ("notices", "unknown"),
                       ("receipts", 1)):
            with self.subTest(queues=queues), self.assertRaises(stage2_verify.VerifyError):
                stage2_verify.verify(self.project, queues=queues)

    def test_unbindable_receipt_refuses_before_any_append(self):
        self.acknowledge_question()
        path = next((self.project.root / "shared-record-receipts").rglob("*.json"))
        receipt = json.loads(path.read_text())
        receipt["record_digest"] = "b" * 64
        path.write_bytes(canonical(receipt))
        before = tree_state(self.project.root)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = convert_legacy.main([str(self.file), "--project", "sample", "--apply"])
        self.assertEqual(code, 2)
        self.assertIn("Unbindable legacy record receipt", errors.getvalue())
        self.assertFalse((self.project.root / "queues").exists())
        self.assertEqual(tree_state(self.project.root), before)

    def test_stored_receipt_extra_is_reported_only_when_its_queue_is_selected(self):
        self.acknowledge_question()
        self.convert("--apply")
        extra_id = "e" * 32
        self.store.append("record-receipts", {"ack_token": "e" * 64,
                                              "legacy_role": "worker",
                                              "legacy_record_id": "extra.record",
                                              "legacy_record_digest": "e" * 64},
                          "host", id=extra_id)
        default = stage2_verify.verify(self.project)
        self.assertTrue(default["ok"])
        explicit = stage2_verify.verify(self.project, queues=convert_legacy.VERIFY_QUEUES)
        self.assertFalse(explicit["ok"])
        self.assertEqual(explicit["checked"]["record-receipts"],
                         {"planned": 1, "stored": 2})
        self.assertEqual(explicit["differences"],
                         [{"kind": "extra", "queue": "record-receipts", "id": extra_id}])


if __name__ == "__main__":
    unittest.main()
