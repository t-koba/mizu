"""Operator conversion of current legacy insights/decisions into declared queues."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from support import Fixture
from mizu.config import load
from mizu.kernel import QueueStore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "development"))
import convert_legacy  # noqa: E402  (example userland module)

QUEUES = ROOT / "examples" / "development" / "queues.toml"
INSIGHT_ID = "a" * 32
DEFER_ID = "c" * 32
REVISIT_AT = "2099-01-01T00:00:00+00:00"
BEFORE_REVISIT = "2098-12-31T23:59:59+00:00"
AFTER_REVISIT = "2099-01-01T00:00:01+00:00"


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
        self.project.insights.decide(INSIGHT_ID, "accept", "Reviewed", "", "test-run",
                                     expected_rev=1, work_item_intent="none")

    def add_deferral(self, insight_id, revisit):
        snap = self.project.snapshots.get()
        self.project.insights.submit(source="worker", title="Deferred proposal",
                                     body="Deferred body", base_snapshot=snap["id"],
                                     insight_id=insight_id)
        self.project.insights.decide(insight_id, "defer", "Need more evidence", revisit,
                                     "test-run", expected_rev=1)

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

        proposals = self.store.read("proposals", "operator")["messages"]
        decisions = self.store.read("decisions", "operator")["messages"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(proposals[0]["id"], first["proposals"][0])
        self.assertEqual(proposals[0]["author"], "operator")
        self.assertEqual(proposals[0]["body"], {"title": "Legacy proposal", "body": "Legacy body"})
        self.assertEqual(decisions[0]["id"], first["decisions"][0])
        self.assertEqual(decisions[0]["body"], {"action": "accept", "reason": "Reviewed"})
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
        defer_body = {"action": "defer", "reason": "Need more evidence", "revisit": REVISIT_AT}
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
                          "revisit": "when the next build finishes"})

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


if __name__ == "__main__":
    unittest.main()
