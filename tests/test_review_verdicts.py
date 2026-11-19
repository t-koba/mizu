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
from mizu.distribution import public_source_identity
from mizu.errors import ConfigError, Denied, LimitExceeded
from mizu.fs import read_json, write_json
from mizu.protocol import DEFINITIONS, tool_definitions
from mizu.publication import Publication
from mizu.runtime import Engine, _on_change_observed
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
        self.human_go = approve(self, "main", current_digest(self))

    def review(self, verdict="approve", summary="No material findings.", role="reviewer", **extra):
        def work(ctx, *_):
            ctx.handle("finish", {"outcome": "wait", "summary": summary,
                                  **({"review_verdict": verdict} if verdict is not None else {}), **extra})
        result = Engine(self.config, driver=ScriptDriver(work)).run(
            self.project, role, event_trigger=False)
        self.assertEqual(result.get("status"), "completed", result)
        return result

    def publish(self):
        return make_context(self, self.config, self.publisher, review=False).handle(
            "vcs_publish", {"op": "push", "branch": "main"})

    def denied(self):
        with self.assertRaisesRegex(Denied, "typed approval"):
            self.publish()

    def add_reviewers(self):
        roles = dict(self.config.roles)
        for name in ("auditor", "inspector"):
            roles[name] = dataclasses.replace(roles["reviewer"], name=name, on_change=True)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
        settings = self.project.root / "project.toml"
        settings.write_text(settings.read_text().replace(json.dumps(self.project.roles),
            json.dumps([*self.project.roles, "auditor", "inspector"])))
        self.project.refresh_settings()

    def select_quorum(self, names, required):
        self.config = dataclasses.replace(self.config, publication=dataclasses.replace(
            self.config.publication, reviewer_role="", reviewers=names, required_approvals=required))
        self.project.config = self.config

    def approvals(self):
        return reviews.require_approvals(
            make_context(self, self.config, self.publisher, review=False), current_digest(self))

    def publication_state(self, pending=(), open_ids=()):
        return reviews.publication_state(self.context(), pending_insights=pending,
                                         open_insights={"ids": list(open_ids)})

    def test_publication_state_limits_findings_to_exact_source_and_snapshot(self):
        self.review("changes_requested")
        source = self.project.snapshots.get()["id"]
        pending = self.project.insights.submit(source="reviewer", base_snapshot=source,
            title="Review finding", body="Material finding")
        findings = [dict(pending, id=f"finding-{i}") for i in range(17)]
        rows = [pending, pending, dict(pending, id="other-role", source="searcher"),
                dict(pending, id="other-snapshot", base_snapshot="0" * 64), *findings]
        self.assertEqual(self.publication_state(rows, [row["id"] for row in rows]), {
            "cause": "changes_requested", "verdict": "changes_requested", "reviewer": "reviewer",
            "finding_insights": [pending["id"], *(row["id"] for row in findings[:15])]})

    def test_changes_requested_findings_follow_verdict_to_same_code_successor(self):
        # A same-code successor inherits a rejecting verdict by digest. The
        # findings were filed against the snapshot the reviewer actually read,
        # so they must stay linked after the verdict carries forward.
        self.review("changes_requested")
        reviewed = self.project.snapshots.get()
        finding = self.project.insights.submit(source="reviewer", base_snapshot=reviewed["id"],
            title="Review finding", body="Material finding")
        successor = self.project.snapshots.create(
            {key: reviewed[key] for key in ("files", "code_digest", "skipped", "bytes")},
            goal=reviewed["goal"], state="New state, unchanged code", run=None,
            outcome="wait", summary="Same code successor", parent=reviewed["id"])
        self.project.snapshots.publish(successor)
        ctx = self.context()  # Current snapshot is now the same-code successor.
        self.assertNotEqual(ctx.snapshot["id"], reviewed["id"])
        self.assertEqual(ctx.snapshot["code_digest"], reviewed["code_digest"])
        self.assertEqual(reviews.publication_state(ctx, pending_insights=[finding],
            open_insights={"ids": [finding["id"]]}), {
                "cause": "changes_requested", "verdict": "changes_requested",
                "reviewer": "reviewer", "finding_insights": [finding["id"]]})
        # A finding filed against the successor itself is not the review's finding.
        other = self.project.insights.submit(source="reviewer", base_snapshot=successor["id"],
            title="Unrelated", body="Not reviewed")
        self.assertEqual(reviews.publication_state(ctx,
            pending_insights=[finding, other],
            open_insights={"ids": [finding["id"], other["id"]]})["finding_insights"],
            [finding["id"]])

    def test_full_and_delta_prompts_project_publication_state(self):
        self.review("changes_requested")
        ctx = self.context()
        finding = self.project.insights.submit(source="reviewer", base_snapshot=ctx.snapshot["id"],
            title="Review finding", body="Material finding")
        rejected = {"cause": "changes_requested", "verdict": "changes_requested",
                    "reviewer": "reviewer", "finding_insights": [finding["id"]]}
        published = {"cause": "published", "verdict": None, "reviewer": None, "finding_insights": []}

        def check(expected):
            # Delta did not re-offer the finding but it is still in its opening backlog.
            for builder, args in ((prompt_for, ()), (prompt_delta_for, ([],))):
                with self.subTest(builder=builder.__name__, cause=expected["cause"]):
                    prompt = json.loads(builder(ctx, *args))
                    self.assertEqual(prompt["publication_state"], expected)

        def record(code_digest):
            Publication.record_external(self.project.root, branch="main", published_sha="a" * 40,
                code_digest=code_digest, approval=self.human_go["id"],
                projection=public_source_identity(ctx.snapshot["files"]))

        check(rejected)  # A local snapshot pin alone proves nothing about publication.
        record("0" * 64)
        check(rejected)
        record(ctx.snapshot["code_digest"])
        check(published)

    def test_publication_state_matched_receipt_is_conclusive_even_if_scan_stops(self):
        self.review("changes_requested")
        with patch.object(Publication, "list_receipts", side_effect=LimitExceeded("Bounded read")):
            self.assertEqual(self.publication_state()["cause"], "changes_requested")
        # A validated exact-digest match is conclusive even when the scan bound
        # stopped; only an absent or unreadable match falls back to review state.
        with patch.object(Publication, "list_receipts", return_value={
                "publications": [{"code_digest": self.project.snapshots.get()["code_digest"]}],
                "scan_stopped": True}):
            self.assertEqual(self.publication_state()["cause"], "published")
        with patch.object(Publication, "list_receipts", return_value={
                "publications": [], "scan_stopped": True}):
            self.assertEqual(self.publication_state()["cause"], "changes_requested")

    def test_publication_state_uses_current_exact_approval_quorum(self):
        self.add_reviewers()
        self.select_quorum(("auditor", "reviewer", "inspector"), 2)
        self.assertEqual(self.publication_state()["cause"], "awaiting_review")
        self.review()
        reviewer = self.project.root / "observed" / "reviewer.json"
        auditor = self.project.root / "observed" / "auditor.json"
        write_json(auditor, read_json(reviewer))
        self.assertEqual(self.publication_state()["cause"], "awaiting_review")
        auditor.write_text("not JSON")
        self.assertEqual(self.publication_state()["cause"], "awaiting_review")
        self.review("changes_requested", role="inspector")
        self.assertEqual(self.publication_state()["cause"], "changes_requested")
        self.review(role="auditor")
        self.assertEqual(self.publication_state()["cause"], "approved_queued")
        self.select_quorum((), 0)
        self.assertEqual(self.publication_state()["cause"], "review_not_configured")

    def test_one_of_two_counts_only_selected_holders_in_config_order(self):
        self.add_reviewers()
        self.select_quorum(("auditor", "reviewer"), 1)
        self.review(role="inspector")
        self.denied()  # An authorized but unselected approval contributes zero.
        self.review()
        approvals = self.approvals()
        self.assertIsInstance(approvals, tuple)
        self.assertEqual([saved["review"]["role"] for saved in approvals], ["reviewer"])
        self.assertEqual(self.publish()["review"], list(approvals))
        self.review(role="auditor")
        self.assertEqual([saved["review"]["role"] for saved in self.approvals()],
                         ["auditor", "reviewer"])  # Return all counted receipts, not just N.

    def test_two_of_three_missing_invalid_or_rejecting_holder_is_not_a_veto(self):
        self.add_reviewers()
        self.select_quorum(("inspector", "auditor", "reviewer"), 2)
        self.review()
        self.denied()  # N-1 cannot satisfy the threshold.
        self.review(role="auditor")
        third = self.project.root / "observed" / "inspector.json"
        self.assertFalse(third.exists())
        expected = self.approvals()
        self.assertEqual([saved["review"]["role"] for saved in expected], ["auditor", "reviewer"])
        self.assertEqual(self.publish()["review"], list(expected))  # Missing third.
        self.review("changes_requested", role="inspector")
        self.assertEqual(self.publish()["review"], list(expected))  # Rejection is zero, not veto.
        third.write_text("not json")
        self.assertEqual(self.publish()["review"], list(expected))
        write_json(third, read_json(self.project.root / "observed" / "reviewer.json"))
        self.assertEqual(self.approvals(), expected)  # A copied cursor cannot count twice.
        self.select_quorum(("inspector", "auditor", "reviewer"), 3)
        self.denied()

    def test_quorum_does_not_replace_human_go(self):
        self.add_reviewers()
        self.select_quorum(("reviewer", "auditor", "inspector"), 2)
        self.review()
        self.review(role="auditor")
        self.assertEqual(len(self.approvals()), 2)
        record = self.human_go
        self.project.insights.revise(record["id"], source="operator", title=record["title"],
            body=f"Await renewed GO.\ndigest: {current_digest(self)}\n",
            base_snapshot=record["base_snapshot"], run="operator", expected_rev=record["rev"])
        with self.assertRaisesRegex(Denied, "stale.*revision"):
            self.publish()

    def test_multiple_holders_seal_independently_and_cannot_substitute_for_selected_holder(self):
        self.add_reviewers()
        self.config = dataclasses.replace(self.config,
            publication=dataclasses.replace(self.config.publication, reviewer_role="auditor"))
        self.project.config = self.config
        source = self.project.snapshots.get()

        def seal(ctx, *_):
            self.assertTrue(reviews.prompt(ctx)["review_authority"]["authorized"])
            ctx.handle("finish", {"outcome": "wait", "summary": "Independent review",
                                  "review_verdict": "approve"})
        engine = Engine(self.config, driver=ScriptDriver(seal))
        for name in ("inspector", "auditor"):
            result = engine.run(self.project, name, scheduled_invocation=True)
            self.assertEqual(result.get("status"), "completed", result)
            review = result["finish"]["review"]
            self.assertEqual(review, {"verdict": "approve", "role": name,
                "run": result["run"], "reviewed_snapshot": source["id"]})
            cursor = read_json(self.project.root / "observed" / f"{name}.json")
            self.assertEqual(reviews.cursor_review(
                self.project, self.config, name, cursor, source)[1], review)
            self.assertEqual(engine.run(self.project, name,
                scheduled_invocation=True).get("skipped"), "unchanged")
            if name == "inspector":
                self.denied()  # No scan/fallback to the other authorized holder.
                with self.assertRaises(Denied):
                    reviews.cursor_review(self.project, self.config, "auditor", cursor, source)
                write_json(self.project.root / "observed" / "auditor.json", cursor)
                self.denied()  # Even a copied cursor cannot impersonate A.
                self.assertFalse(_on_change_observed(cursor, source, exact_snapshot=True,
                    project=self.project, config=self.config, role_name="auditor"))
                for kwargs in ({}, {"project": self.project},
                               {"project": self.project, "config": self.config}):
                    self.assertFalse(_on_change_observed(cursor, source,
                        exact_snapshot=True, **kwargs))
        self.assertTrue(self.publish()["published"])
        successor = self.project.snapshots.create(
            {key: source[key] for key in ("files", "code_digest", "skipped", "bytes")},
            goal=source["goal"], state="Same code, new review input", run=None,
            outcome="wait", summary="Successor", parent=source["id"])
        self.project.snapshots.publish(successor)
        # Both holders are bound to the reviewed digest, so the same-code
        # successor needs no second verdict for either of them.
        for name in ("inspector", "auditor"):
            self.assertEqual(engine.run(self.project, name,
                scheduled_invocation=True).get("skipped"), "unchanged")
        self.assertEqual([saved["review"]["role"] for saved in self.approvals()], ["auditor"])
        self.assertTrue(self.publish()["published"])

    def test_ungranted_reader_and_reviewer_name_cannot_seal(self):
        for name in ("searcher", "reviewer"):
            ctx = self.context(name)
            ctx.role = dataclasses.replace(ctx.role,
                capabilities=tuple(cap for cap in ctx.role.capabilities if cap != "review_verdict"))
            self.assertFalse(reviews.authorized(ctx.config, ctx.role))
            with self.assertRaises(Denied):
                ctx.handle("finish", {"outcome": "wait", "summary": "Not authorized",
                                      "review_verdict": "approve"})
            self.assertFalse((ctx.run_dir / "finish-intent.json").exists())

    def test_selected_holder_revocation_denies_existing_approval(self):
        self.review()
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"],
            capabilities=tuple(cap for cap in roles["reviewer"].capabilities if cap != "review_verdict"))
        self.config = dataclasses.replace(self.config, roles=roles)
        self.denied()

    def test_approval_does_not_replace_human_go(self):
        self.review()
        record = self.human_go
        self.project.insights.revise(record["id"], source="operator", title=record["title"],
            body=f"Await renewed GO.\ndigest: {current_digest(self)}\n",
            base_snapshot=record["base_snapshot"], run="operator", expected_rev=record["rev"])
        with self.assertRaisesRegex(Denied, "stale.*revision"):
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
        self.assertEqual(publication["review"], [read_json(saved)])

    def test_reviewed_digest_skips_same_code_successor_and_rearms_on_change(self):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=True)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config

        self.review()
        source = self.project.snapshots.get()
        cursor_path = self.project.root / "observed" / "reviewer.json"
        cursor = read_json(cursor_path)
        self.assertEqual(cursor["snapshot"], source["id"])
        self.assertEqual(cursor["review_receipt"], self.project.snapshots.get(
            cursor["review_receipt"])["id"])
        # A review of the current digest does not retrigger itself.
        same = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(same.get("skipped"), "unchanged", same)

        successor = self.project.snapshots.create(
            {key: source[key] for key in ("files", "code_digest", "skipped", "bytes")},
            goal=source["goal"], state="New state, unchanged code", run=None,
            outcome="wait", summary="Same code, new snapshot", parent=source["id"])
        self.project.snapshots.publish(successor)
        self.assertEqual(Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True).get("skipped"), "unchanged")
        self.assertEqual(read_json(cursor_path), cursor)  # No verdict replaced.

        # New code re-arms the reviewer: the stale approval cannot dedup it.
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        changed = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=source["goal"], state="Changed code", run=None,
            outcome="wait", summary="Changed code", parent=successor["id"])
        self.project.snapshots.publish(changed)
        self.assertFalse(_on_change_observed(cursor, changed, exact_snapshot=True,
            project=self.project, config=self.config, role_name="reviewer"))
        result = Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True)
        self.assertEqual(result.get("status"), "completed", result)
        self.assertNotIn("review", result["finish"])

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

    def test_review_binding_requires_same_digest_and_live_tree(self):
        self.review()
        old = self.project.snapshots.get()
        capture = self.project.snapshots.capture_files(self.project.workspace)
        successor = self.project.snapshots.create(capture, goal=old["goal"], state="New state",
            run=None, outcome="wait", summary="Same code, new snapshot", parent=old["id"])
        self.project.snapshots.publish(successor)
        # Same code and the same live tree: the earlier verdict still counts.
        self.assertEqual(len(self.approvals()), 1)
        self.assertTrue(self.publish()["published"])

        # A changed live tree has a changed digest, which needs a new verdict.
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        previous = self.project.snapshots.get()
        changed = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=previous["goal"], state="Changed code", run=None,
            outcome="wait", summary="Changed code", parent=previous["id"])
        self.project.snapshots.publish(changed)
        approve(self, "main", current_digest(self))
        with self.assertRaisesRegex(Denied, "typed approval"):
            self.approvals()
        self.denied()
        self.review()
        self.assertTrue(self.publish()["published"])

    def test_finish_tool_scopes_typed_verdict_to_authorized_reviewer(self):
        for name in ("worker", "searcher", "consult"):
            [finish] = tool_definitions(("finish",), role=self.config.roles[name], config=self.config)
            self.assertNotIn("review_verdict", finish["inputSchema"]["properties"])
            self.assertNotIn("review_verdict", finish["description"])
            self.assertNotIn("a summary cannot approve", finish["description"])
        reviewer = self.config.roles["reviewer"]
        self.assertTrue(reviews.authorized(self.config, reviewer))
        [finish] = tool_definitions(("finish",), role=reviewer, config=self.config)
        self.assertEqual(finish["inputSchema"]["properties"]["review_verdict"],
                         {"type": "string", "enum": ["approve", "changes_requested"]})
        self.assertIn("seal approve or changes_requested", finish["description"])
        self.assertIn("a summary cannot approve", finish["description"])
        # Revoking the capability removes both even for that role name.
        revoked = dataclasses.replace(reviewer, capabilities=tuple(
            cap for cap in reviewer.capabilities if cap != "review_verdict"))
        [finish] = tool_definitions(("finish",), role=revoked, config=self.config)
        self.assertNotIn("review_verdict", finish["inputSchema"]["properties"])
        self.assertNotIn("a summary cannot approve", finish["description"])
        # Without a bound role/config the full manual and unscoped schema remain.
        self.assertEqual(tool_definitions(("finish",))[0]["inputSchema"], DEFINITIONS["finish"][1])
        open_config = dataclasses.replace(self.config, publication=dataclasses.replace(
            self.config.publication, reviewer_role=""))
        [finish] = tool_definitions(("finish",), role=reviewer, config=open_config)
        self.assertIn("review_verdict", finish["inputSchema"]["properties"])

    def test_state_only_republication_keeps_approval_and_skips_reviewer(self):
        roles = dict(self.config.roles)
        roles["reviewer"] = dataclasses.replace(roles["reviewer"], on_change=True)
        self.config = dataclasses.replace(self.config, roles=roles)
        self.project.config = self.config
        self.review()
        reviewed = self.project.snapshots.get()
        cursor_path = self.project.root / "observed" / "reviewer.json"
        cursor = read_json(cursor_path)
        successor = self.project.snapshots.create(
            {key: reviewed[key] for key in ("files", "code_digest", "skipped", "bytes")},
            goal=reviewed["goal"], state="New state, same code", run=None,
            outcome="wait", summary="State-only successor", parent=reviewed["id"])
        self.project.snapshots.publish(successor)
        self.assertNotEqual(successor["id"], reviewed["id"])
        self.assertEqual(successor["code_digest"], reviewed["code_digest"])

        self.assertEqual(self.publication_state()["cause"], "approved_queued")
        self.assertEqual(len(self.approvals()), 1)
        self.assertTrue(_on_change_observed(cursor, successor, exact_snapshot=True,
            project=self.project, config=self.config, role_name="reviewer"))
        self.assertEqual(Engine(self.config, driver=ScriptDriver()).run(
            self.project, "reviewer", scheduled_invocation=True).get("skipped"), "unchanged")
        self.assertEqual(read_json(cursor_path), cursor)
        publication = self.publish()
        self.assertTrue(publication["published"])
        self.assertEqual(publication["review"][0]["review"]["reviewed_snapshot"], reviewed["id"])

    def test_changed_digest_requires_new_verdict_and_rejects_old_approval(self):
        self.review()
        source = self.project.snapshots.get()
        cursor_path = self.project.root / "observed" / "reviewer.json"
        cursor = read_json(cursor_path)
        (self.project.workspace / "app.py").write_text("VALUE = 3\n")
        changed = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=source["goal"], state="Changed code", run=None,
            outcome="wait", summary="Changed code", parent=source["id"])
        self.project.snapshots.publish(changed)
        approve(self, "main", current_digest(self))
        self.denied()  # The earlier approval names the old digest.
        context = make_context(self, self.config, self.publisher, review=False)
        with self.assertRaisesRegex(Denied, "typed approval"):
            reviews.require_approvals(context, "0" * 64)

        # Relabeling the cursor digest cannot promote an old-digest receipt.
        forged = dict(cursor, code_digest=changed["code_digest"])
        write_json(cursor_path, forged)
        with self.assertRaisesRegex(Denied, "code digest"):
            reviews.cursor_review(self.project, self.config, "reviewer", forged, changed)
        self.denied()
        write_json(cursor_path, cursor)

        self.review()
        self.assertEqual(self.approvals()[0]["review"]["reviewed_snapshot"], changed["id"])
        self.assertTrue(self.publish()["published"])

    def test_review_identity_and_reviewed_snapshot_are_host_bound(self):
        self.review()
        source = self.project.snapshots.get()
        cursor = read_json(self.project.root / "observed" / "reviewer.json")
        self.assertEqual(reviews.cursor_review(
            self.project, self.config, "reviewer", cursor, source)[1], cursor["review"])
        with self.assertRaisesRegex(Denied, "code digest"):
            reviews.cursor_review(self.project, self.config, "reviewer", cursor,
                                  {**source, "code_digest": "0" * 64})
        for expected_role in ("worker", "", "missing"):
            with self.assertRaises(Denied):
                reviews.cursor_review(self.project, self.config, expected_role, cursor, source)
        for changed in (
                {**cursor, "snapshot": "a" * 64},
                {**cursor, "review_receipt": "b" * 64},
                {**cursor, "code_digest": "c" * 64},
                {**cursor, "review": {**cursor["review"], "role": "worker"}},
                {**cursor, "review": {**cursor["review"], "run": "0" * 32}},
                {**cursor, "review": {**cursor["review"], "reviewed_snapshot": "d" * 64}},
                {**cursor, "review": {**cursor["review"], "verdict": "changes_requested"}}):
            with self.subTest(changed=changed):
                with self.assertRaises(Denied):
                    reviews.cursor_review(self.project, self.config, "reviewer", changed, source)

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
        closed = dataclasses.replace(self.config, roles=load(self.file).roles,
            publication=dataclasses.replace(self.config.publication, reviewer_role=""))
        ctx = self.context("reviewer")
        ctx.config = closed
        ctx.role = closed.roles["reviewer"]
        with self.assertRaises(Denied):
            ctx.handle("finish", {"outcome": "wait", "summary": "review", "review_verdict": "approve"})
        self.review()
        with self.assertRaises(Denied):
            reviews.require_approvals(make_context(self, closed, self.publisher, review=False), current_digest(self))
        self.config = closed
        self.denied()  # Valid human GO alone never satisfies an empty review requirement.
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
        # Transfer the grant as well as the publication selection. Merely
        # deselecting a holder no longer revokes its review capability.
        roles = dict(load(self.file).roles)
        roles["searcher"] = dataclasses.replace(roles["searcher"],
            capabilities=(*roles["searcher"].capabilities, "review_verdict"))
        self.config = dataclasses.replace(self.config, roles=roles, publication=dataclasses.replace(
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

    def test_publication_state_is_visible_to_roles_without_publish_grant(self):
        self.select_quorum((), 0)
        ctx = self.context("reviewer")
        self.assertNotIn("vcs_publish", ctx.role.capabilities)
        for builder, args in ((prompt_for, ()), (prompt_delta_for, ([],))):
            with self.subTest(builder=builder.__name__):
                state = json.loads(builder(ctx, *args))["publication_state"]
                self.assertEqual(state, {"cause": "review_not_configured", "verdict": None,
                                         "reviewer": None, "finding_insights": []})
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
