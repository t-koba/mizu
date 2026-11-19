"""Stage-2 userland shadow of authoritative operator notice writes."""
import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401  (puts src on the path)
from mizu import operator_records as records
from mizu.config import load
from mizu.fs import canonical, digest
from mizu.kernel_shadow import notice_fields, notice_object_id
from mizu.project import initialize

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "kernel_userland.toml"


class OperatorNoticeShadow(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-notice-shadow-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = dataclasses.replace(
            load(CONFIG), data=root / "data",
            operator_records=records.configuration({
                "answer": {"enabled": True},
                "operator.notice": {"enabled": True},
            }))
        source = root / "source"
        source.mkdir()
        (source / "app.py").write_bytes(b"VALUE = 2\n")
        goal = root / "GOAL.md"
        goal.write_text("Preserve correctness.\n")
        self.project = initialize(self.config, "sample", source, goal,
                                  ["writer", "reviewer", "observer"],
                                  ["python3 -m unittest"])
        self.question = self.project.insights.submit(
            source="writer", title="Question", body="Which behavior?",
            base_snapshot=None, addressee="operator")
        self.subject = {"type": "insight", "id": self.question["id"], "rev": 1}

    def shadow(self):
        return self.project.insights.kernel_shadow

    def notice(self, event="notice1", **kwargs):
        return records.write(self.project, event_id=event, kind="operator.notice",
                             body="Exact notice", addressee="reviewer", **kwargs)

    def pending_ids(self):
        return [row["record_id"] for row in
                records.pending(self.project, self.config.roles["reviewer"])[0]["events"]]

    def test_notice_write_creates_deterministic_kernel_object(self):
        result = self.notice(subject=self.subject)
        record, computed = result["record"], result["record_digest"]
        object_id = notice_object_id(record)
        self.assertRegex(object_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(object_id,
                         digest(canonical(["kernel-shadow", "notice", record["id"]]))[:32])
        self.assertNotEqual(object_id, record["id"])
        mirrored = self.project.kernel_objects.read("notice", object_id)
        self.assertEqual(mirrored["fields"], {
            "subject_digest": computed, "kind": "operator.notice",
            "body": "Exact notice", "reference": self.question["id"]})
        self.assertEqual(mirrored["state"], "recorded")
        self.assertEqual(mirrored["author"], "host")
        self.assertEqual(self.project.kernel_objects.revisions("notice", object_id), [1])
        self.assertEqual(self.shadow().failures, [])

        # Only notice writes take this route; an answer stays entirely legacy.
        records.write(self.project, event_id="answer1", kind="answer",
                      body="Exact answer", subject=self.subject)
        self.assertEqual(len(self.project.kernel_objects.list("notice")["objects"]), 1)
        self.assertEqual(self.shadow().failures, [])

    def test_target_reference_takes_precedence_over_subject(self):
        first = self.notice()
        target = {"id": first["record"]["id"], "digest": first["record_digest"]}
        result = self.notice(event="notice2", action="supersede", target=target,
                             subject=self.subject)
        fields = self.project.kernel_objects.read(
            "notice", notice_object_id(result["record"]))["fields"]
        self.assertEqual(fields["reference"], target["id"])
        self.assertEqual(fields["subject_digest"], result["record_digest"])

    def test_exact_retry_does_not_duplicate_or_change_the_object(self):
        first = self.notice()
        object_id = notice_object_id(first["record"])
        head = self.project.kernel_events.head()
        second = self.notice()
        self.assertEqual(first, second)
        self.assertEqual(self.project.kernel_events.head(), head)
        self.assertEqual(self.project.kernel_objects.revisions("notice", object_id), [1])
        self.assertEqual(self.project.kernel_objects.read("notice", object_id)["fields"], {
            "subject_digest": second["record_digest"], "kind": "operator.notice",
            "body": "Exact notice"})
        self.assertEqual(self.shadow().failures, [])

    def test_projection_failure_is_diagnostic_only_and_bounded(self):
        with patch.object(self.shadow(), "noticed",
                          side_effect=OSError("x" * 400)):
            result = self.notice(event="failure-notice")
        record_id = result["record"]["id"]
        self.assertIn(record_id, self.pending_ids())
        self.assertEqual(self.notice(event="failure-notice"), result)

        failures = self.shadow().failures
        self.assertEqual(len(failures), 1)
        row = failures[0]
        self.assertEqual(row["operation"], "operator.notice")
        self.assertEqual(row["insight"], record_id)
        self.assertEqual(row["error"], "OSError")
        self.assertEqual(len(row["reason"]), 256)
        self.assertTrue(row["reason"].endswith("..."))
        mirrored = self.project.kernel_objects.read("notice", notice_object_id(result["record"]))
        self.assertEqual(mirrored["fields"]["subject_digest"], result["record_digest"])

    def test_existing_object_mismatch_is_reported_without_changing_the_write(self):
        record_id = f"operator/{self.project.name}/mismatch-notice"
        record = {"id": record_id, "kind": "operator.notice",
                  "payload": {"body": "Exact notice"}, "subject": {}, "target": None}
        object_id = notice_object_id(record)
        self.project.kernel_objects.create(
            "notice", {"subject_digest": "00" * 32, "kind": "operator.notice",
                       "body": "Different notice"}, "host", object_id=object_id)
        result = self.notice(event="mismatch-notice")
        self.assertEqual(result["record"]["id"], record_id)
        self.assertIn(record_id, self.pending_ids())
        failures = self.shadow().failures
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error"], "Denied")
        self.assertIn("different content", failures[0]["reason"])
        self.assertEqual(self.project.kernel_objects.read("notice", object_id)["fields"], {
            "subject_digest": "00" * 32, "kind": "operator.notice",
            "body": "Different notice"})

    def test_oversized_reference_is_omitted(self):
        record = {"id": "operator/sample/long", "kind": "operator.notice",
                  "payload": {"body": "Notice", "target": {"id": "n" * 257}},
                  "subject": {}}
        self.assertNotIn("reference", notice_fields(record, "ab" * 32))


if __name__ == "__main__":
    unittest.main()
