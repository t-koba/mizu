"""Operator migration gate: declared stage-2 queues versus the conversion plan."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src and tests on the path)
from mizu.fs import canonical
from test_stage2_conversion import BEFORE_REVISIT, DEFER_ID, REVISIT_AT
import test_stage2_conversion as conversion_tests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)
import stage2_verify  # noqa: E402  (example userland module)

COUNTS = {name: {"planned": 1, "stored": 1} for name in convert_legacy.QUEUES}
COUNTS["receipts"] = {"planned": 0, "stored": 0}  # No legacy receipts in this fixture.


class Stage2Verify(unittest.TestCase):
    """Reuse the stage-2 conversion fixture without inheriting its test cases."""

    def setUp(self):
        source = conversion_tests.Stage2QuestionConversion(methodName="runTest")
        source.setUp()
        self.addCleanup(source.doCleanups)
        self.fixture = source
        self.project, self.store, self.file = source.project, source.store, source.file

    def convert(self, *flags):
        return self.fixture.convert(*flags)

    def verify(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = stage2_verify.main([str(self.file), "--project", "sample"])
        return code, json.loads(output.getvalue()), errors.getvalue()

    def rewrite_message(self, queue, seq, changes):
        """Model store drift: replace one immutable message file in place."""
        path = self.project.root / "queues" / queue / "messages" / f"{seq:020d}.json"
        path.write_bytes(canonical({**json.loads(path.read_text()), **changes}))

    def add_deferral(self):
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Deferred proposal",
                                     body="Deferred body", base_snapshot=snap["id"],
                                     insight_id=DEFER_ID)
        self.project.insights.decide(DEFER_ID, "defer", "Need more evidence", REVISIT_AT,
                                     "test-run", expected_rev=1)

    def test_converted_queues_match_the_plan(self):
        self.assertTrue(self.convert("--apply")["applied"])
        code, report, _ = self.verify()
        self.assertEqual(code, 0)
        self.assertEqual(report, {"project": "sample", "checked": COUNTS,
                                  "ok": True, "differences": []})
        self.assertEqual(self.verify(), (code, report, ""))
        # A gate read creates no durable cursor and acknowledges nothing.
        self.assertEqual(list((self.project.root / "queues").glob("*/cursors/*")), [])

    def test_extra_message_fails_the_gate(self):
        self.convert("--apply")
        extra = "f" * 32
        self.store.append("notices", {"body": "Unexpected"}, "operator",
                          id=extra, now=REVISIT_AT)
        code, report, errors = self.verify()
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["differences"],
                         [{"kind": "extra", "queue": "notices", "id": extra}])
        self.assertEqual(report["checked"]["notices"], {"planned": 1, "stored": 2})
        self.assertIn("mismatch: 1 difference(s)", errors)

    def test_extra_message_after_old_sentinel_fails_the_gate(self):
        self.convert("--apply")
        extra = "a" * 32
        just_after_old_sentinel = "9999-12-31T23:59:59.000001+00:00"
        self.store.append("notices", {"body": "Beyond the old sentinel"}, "operator",
                          id=extra, now=just_after_old_sentinel)
        code, report, errors = self.verify()
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["differences"],
                         [{"kind": "extra", "queue": "notices", "id": extra}])
        self.assertEqual(report["checked"]["notices"], {"planned": 1, "stored": 2})
        self.assertIn("mismatch: 1 difference(s)", errors)

    def test_delayed_page_refuses_the_gate(self):
        self.convert("--apply")
        with mock.patch.object(stage2_verify, "_read_queue",
                               side_effect=stage2_verify.VerifyError(
                                   "notices read is incomplete: a message is still delayed")):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = stage2_verify.main([str(self.file), "--project", "sample"])
        self.assertEqual(code, 2)
        self.assertIn("still delayed", errors.getvalue())

    def test_changed_message_fails_the_gate(self):
        self.convert("--apply")
        notice = self.store.read("notices", "operator")["messages"][0]
        self.rewrite_message("notices", notice["seq"],
                             {"body": {**notice["body"], "body": "Changed notice"}})
        code, report, _ = self.verify()
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["differences"], [
            {"kind": "changed", "queue": "notices", "id": notice["id"],
             "fields": ["body"]}])

    def test_delayed_revisit_is_included_by_far_future_read(self):
        self.add_deferral()
        self.convert("--apply")
        revisit_id = convert_legacy._message_id("revisit", DEFER_ID, 1)
        pending = self.store.read("decisions", "operator", now=BEFORE_REVISIT)
        self.assertNotIn(revisit_id, [message["id"] for message in pending["messages"]])
        due = self.store.read("decisions", "operator", now=stage2_verify.NOW)
        self.assertIn(revisit_id, [message["id"] for message in due["messages"]])

        code, report, _ = self.verify()
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["checked"]["decisions"], {"planned": 3, "stored": 3})

    def test_duplicate_store_id_refuses(self):
        self.convert("--apply")
        original = stage2_verify._read_queue

        def duplicate(store, queue):
            rows = original(store, queue)
            return rows + rows[:1] if queue == "notices" else rows

        with mock.patch.object(stage2_verify, "_read_queue", side_effect=duplicate):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = stage2_verify.main([str(self.file), "--project", "sample"])
        self.assertEqual(code, 2)
        self.assertIn("Duplicate store message id", errors.getvalue())

    def test_ungrowing_pagination_and_oversized_queue_refuse(self):
        self.convert("--apply")
        store = self.project.kernel_queues
        cap = stage2_verify.MAX_PAGES * stage2_verify.PAGE
        pages = (
            ({"messages": [], "head": 10, "more": True, "next": 0, "delayed": False},
             "pagination did not advance"),
            ({"messages": [], "head": cap + 1, "more": False, "next": 0,
              "delayed": False}, "page bound"),
        )
        for page, expected in pages:
            with self.subTest(expected=expected):
                with mock.patch.object(store, "read", return_value=page):
                    with self.assertRaisesRegex(stage2_verify.VerifyError, expected):
                        stage2_verify.verify(self.project)


if __name__ == "__main__":
    unittest.main()
