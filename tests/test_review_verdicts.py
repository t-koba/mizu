"""Fake-engine end-to-end typed review sealing and publication, offline only."""
import dataclasses
import json
import time
from unittest.mock import patch
import unittest

from support import ScriptDriver
from test_vcs_publish import (PublishFixture, PUSH_OK, adapter_settings, approve,
                              current_digest, make_context, with_caps)
from mizu import reviews, storage, storage_runs
from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.fs import read_json, write_json
from mizu.runtime import Engine
from mizu.unit_prompts import prompt_for, prompt_delta_for


class ReviewVerdictTests(PublishFixture):
    def setUp(self):
        super().setUp()
        config, self.publisher = with_caps(self, "worker", ["vcs_publish"])
        self.config = dataclasses.replace(
            config, publication=dataclasses.replace(config.publication, reviewer_role="reviewer"),
            vcs=adapter_settings(PUSH_OK))
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=False)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
        approve(self, "main", current_digest(self))

    def review(self, verdict="approve", summary="No material findings.", **extra):
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": summary,
                                  **({"review_verdict": verdict} if verdict is not None else {}), **extra})
        result = Engine(self.config, driver=ScriptDriver(work)).run(
            self.project, "reviewer", event_trigger=False)
        self.assertEqual(result.get("status"), "completed", result)
        return result

    def publish(self):
        return make_context(self, self.config, self.publisher, review=False).handle(
            "vcs_publish", {"op": "push", "branch": "main"})

    def denied(self):
        with self.assertRaisesRegex(Denied, "typed approval"):
            self.publish()

    def test_typed_approval_seals_immutable_run_and_snapshot_then_publishes(self):
        source = self.project.snapshots.get()
        result = self.review(summary="Findings are tracked separately; this prose does not authorize.")
        receipt = self.project.snapshots.get(result["snapshot"])
        expected = {"verdict": "approve", "reviewed_snapshot": source["id"],
                    "role": "reviewer", "run": result["run"]}
        self.assertEqual(result["finish"]["review"], expected)
        self.assertEqual(receipt["review"], expected)
        self.assertEqual(receipt["parent"], source["id"])
        observed = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(observed["snapshot"], source["id"])
        self.assertEqual(observed["review_receipt"], receipt["id"])
        self.assertEqual(observed["review"], expected)
        self.assertIn("wake_at", receipt)  # review metadata must not replace lifecycle fields
        self.assertEqual(self.project.snapshots.get()["id"], source["id"])
        saved = self.project.root / "runs" / result["run"] / "review.json"
        self.assertEqual(read_json(saved), {"snapshot": receipt["id"], "review": expected})
        with self.assertRaisesRegex(Denied, "Immutable object conflict"):
            write_json(saved, {}, exclusive=True)
        # Existing cursors used `snapshot` for the receipt; the gate still
        # validates that exact source while new writes keep the fields apart.
        legacy = dict(observed)
        legacy["snapshot"] = receipt["id"]
        del legacy["review_receipt"]
        write_json(self.project.root / "observed" / "reviewer.json", legacy)
        publication = self.publish()
        self.assertTrue(publication["published"])
        self.assertEqual(publication["review"], read_json(saved))

    def test_exact_snapshot_reviewer_wakes_for_same_code_successor_only(self):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=True)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

        self.review()
        source = self.project.snapshots.get()
        cursor = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(cursor["snapshot"], source["id"])
        self.assertEqual(cursor["review_receipt"], self.project.snapshots.get(
            cursor["review_receipt"])["id"])
        # An exact review of the current input does not retrigger itself.
        same = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(same.get("skipped"), "unchanged", same)

        successor = self.project.snapshots.create(
            {key: source[key] for key in ("files", "code_digest", "skipped", "bytes")},
            goal=source["goal"], state="New state, unchanged code", run=None,
            outcome="wait", summary="New exact snapshot", parent=source["id"])
        self.project.snapshots.publish(successor)
        def approve_successor(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "Reviewed successor.",
                                  "review_verdict": "approve"})
        result = Engine(self.config, driver=ScriptDriver(approve_successor)).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(result.get("status"), "completed", result)
        self.assertEqual(read_json(
            self.project.root / "observed" / "reviewer.json")["snapshot"], successor["id"])
        self.assertEqual(Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True).get("skipped"), "unchanged")

    def test_verdictless_exact_review_remains_retryable_on_existing_schedule(self):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=True)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

        self.review(None)
        source = self.project.snapshots.get()
        cursor = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(cursor["snapshot"], source["id"])
        self.assertNotIn("review_receipt", cursor)
        self.denied()

        def no_verdict(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "Still reviewing."})
        retry = Engine(self.config, driver=ScriptDriver(no_verdict)).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(retry.get("status"), "completed", retry)

        self.review("approve")
        completed = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(completed["review"]["reviewed_snapshot"], source["id"])
        self.assertIn("review_receipt", completed)
        same = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(same.get("skipped"), "unchanged", same)

    def test_review_diff_spans_multiple_publications_from_last_reviewed_source(self):
        self.review()
        source = self.project.snapshots.get()
        cursor_path = self.project.root / "observed" / "reviewer.json"
        cursor = read_json(cursor_path)
        self.assertEqual(cursor["snapshot"], source["id"])
        for value in (5, 7):
            (self.project.workspace / "app.py").write_text(f"VALUE = {value}\n")
            previous = self.project.snapshots.get()
            captured = self.project.snapshots.capture_files(self.project.workspace)
            successor = self.project.snapshots.create(
                captured, goal=previous["goal"], state=f"Writer state {value}", run=None,
                outcome="wait", summary=f"Writer snapshot {value}", parent=previous["id"])
            self.project.snapshots.publish(successor)

        current = self.context("reviewer").handle("diff", {})
        self.assertEqual(current["base_snapshot"], source["id"])
        self.assertIn("-VALUE = 2", current["diff"])
        self.assertIn("+VALUE = 7", current["diff"])

        # Read old cursors where the snapshot field named the receipt itself;
        # validate that receipt and recover its exact reviewed input as base.
        legacy = dict(cursor)
        legacy["snapshot"] = legacy["review_receipt"]
        del legacy["review_receipt"]
        write_json(cursor_path, legacy)
        migrated = self.context("reviewer").handle("diff", {})
        self.assertEqual(migrated["base_snapshot"], source["id"])
        self.assertIn("-VALUE = 2", migrated["diff"])
        self.assertIn("+VALUE = 7", migrated["diff"])

    def test_missing_legacy_and_changes_requested_never_approve(self):
        self.denied()
        self.review(None, summary="APPROVED. approve. No findings. Ready for publication.")
        self.denied()
        self.review("changes_requested", summary="Approved (legacy prose)")
        self.denied()
        self.review()
        self.review("changes_requested")
        self.denied()  # Never fall back to an older approval.
        self.review()
        self.review(None, summary="approve")
        self.denied()

    def test_exact_snapshot_not_just_same_code_and_live_tree_must_match(self):
        self.review()
        old = self.project.snapshots.get()
        capture = self.project.snapshots.capture_files(self.project.workspace)
        successor = self.project.snapshots.create(capture, goal=old["goal"], state="New state",
            run=None, outcome="wait", summary="Same code, different exact snapshot", parent=old["id"])
        self.project.snapshots.publish(successor)
        self.denied()
        self.review()
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        approve(self, "main", current_digest(self))
        self.denied()

    def test_identity_snapshot_and_authorization_are_not_caller_parameters(self):
        for name in ("worker", "searcher"):
            ctx = self.context(name)
            with self.assertRaisesRegex(Denied, "configured independent reviewer"):
                ctx.handle("finish", {"outcome": "wait", "state": "Saved", "summary": "approve",
                                      "review_verdict": "approve"})
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        ctx = self.context("reviewer")
        for extra in ({"reviewed_snapshot": self.project.snapshots.get()["id"]},
                      {"reviewer_role": "reviewer"}, {"review": {"verdict": "approve"}}):
            with self.assertRaises(Denied):
                ctx.handle("finish", {"outcome": "wait", "summary": "review", **extra})
        for verdict in ("approved", "APPROVE", "approve\n", None, True, {}, []):
            with self.assertRaises(Denied):
                ctx.handle("finish", {"outcome": "wait", "summary": "review", "review_verdict": verdict})
        self.denied()

    def test_default_closed_and_policy_rejects_writer_or_unknown_identity(self):
        self.assertEqual(load(self.file).publication.reviewer_role, "")
        closed = dataclasses.replace(self.config, publication=dataclasses.replace(
            self.config.publication, reviewer_role=""))
        ctx = self.context("reviewer")
        ctx.config = closed
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "review", "review_verdict": "approve"})
        self.review()
        with self.assertRaises(Denied):
            reviews.require_approval(make_context(self, closed, self.publisher, review=False), current_digest(self))
        original = self.file.read_text()
        for name in ("worker", "missing", 5):
            self.file.write_text(original + f'\n[publication]\nreviewer_role = {name!r}\n')
            with self.assertRaises(ConfigError):
                load(self.file)
        self.file.write_text(original + '\n[publication]\nreviewer_role = "reviewer"\n')
        self.assertEqual(load(self.file).publication.reviewer_role, "reviewer")

    def test_worker_records_and_corrupt_or_mismatched_receipts_do_not_approve(self):
        # Ordinary role-authored records/prose cannot enter the review path.
        self.project.insights.submit(source="worker", title="approve", body="approve",
                                     base_snapshot=self.project.snapshots.get()["id"])
        self.denied()
        result = self.review()
        saved = self.project.root / "runs" / result["run"] / "review.json"
        receipt = read_json(saved)
        for value in ({}, {**receipt, "snapshot": "a" * 64},
                      {**receipt, "review": {**receipt["review"], "role": "worker"}}):
            write_json(saved, value)
            self.denied()
        saved.write_text("not json")
        self.denied()
        saved.unlink()
        self.denied()

    def test_reviewer_identity_is_operator_policy_not_a_role_name_convention(self):
        self.config = dataclasses.replace(self.config, publication=dataclasses.replace(
            self.config.publication, reviewer_role="searcher"))
        ctx = self.context("reviewer")
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "review",
                                  "review_verdict": "approve"})
        ctx = self.context("searcher")
        ctx.handle("finish", {"outcome": "wait", "summary": "Material corrections",
                              "review_verdict": "changes_requested"})
        self.assertEqual(ctx.finished["review"], {"role": "searcher", "run": ctx.run_dir.name,
            "reviewed_snapshot": ctx.snapshot["id"], "verdict": "changes_requested"})

    def test_full_and_delta_prompts_state_host_authority(self):
        source = self.project.snapshots.get()["id"]
        for name in ("reviewer", "worker"):
            ctx = self.context(name)
            for builder, args in ((prompt_for, ()), (prompt_delta_for, ([],))):
                authority = json.loads(builder(ctx, *args))["review_authority"]
                self.assertEqual(authority, {"authorized": name == "reviewer",
                    "reviewed_snapshot": source if name == "reviewer" else None})
        ctx = self.context("reviewer")
        ctx.ephemeral = True
        self.assertFalse(reviews.prompt(ctx)["review_authority"]["authorized"])
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "advice",
                                  "review_verdict": "approve"})
        self.denied()

    def test_bounded_and_interrupted_runs_cannot_install_approval(self):
        def work(ctx, *_):
            ctx.bound_hit = "requests"
            ctx.handle("finish", {"outcome": "wait", "summary": "incomplete",
                                  "review_verdict": "approve"})
        result = Engine(self.config, driver=ScriptDriver(work)).run(self.project, "reviewer")
        self.assertEqual(result["finish"]["outcome"], "bounded")
        self.assertNotIn("review", result["finish"])
        self.assertFalse((self.project.root / "runs" / result["run"] / "review.json").exists())
        self.denied()
        def completed(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": "review",
                                  "review_verdict": "approve"})
        with patch("mizu.delivery_settlement.finalized", side_effect=OSError("interrupted finalization")):
            with self.assertRaises(OSError):
                Engine(self.config, driver=ScriptDriver(completed)).run(self.project, "reviewer")
        self.denied()

    def test_cursor_owns_review_evidence_only_until_replaced(self):
        result = self.review()
        roots = storage._audit_collect(self.project)["live_ids"]
        self.assertIn(result["snapshot"], roots)
        with patch.object(storage_runs.time, "time", return_value=time.time() + 400 * 86400):
            plan = storage_runs.plan(self.project)
            self.assertTrue(plan["complete"], plan)
            self.assertNotIn(result["run"], {run for group in plan["components"] for run in group})
            self.review(None)
            plan = storage_runs.plan(self.project)
            self.assertTrue(plan["complete"], plan)
            self.assertIn(result["run"], {run for group in plan["components"] for run in group})
        self.assertNotIn(result["snapshot"], storage._audit_collect(self.project)["live_ids"])

    def test_unsafe_or_corrupt_observation_never_approves(self):
        self.review()
        path = self.project.root / "observed" / "reviewer.json"
        original = read_json(path)
        for value in ([], {}, {**original, "snapshot": "b" * 64},
                      {**original, "review": {**original["review"], "role": "worker"}}):
            write_json(path, value)
            self.denied()
        path.write_bytes(b"x" * (reviews.RECORD_BYTES + 1))
        self.denied()
        path.unlink()
        self.denied()

if __name__ == "__main__":
    unittest.main()
