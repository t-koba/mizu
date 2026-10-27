"""Writer explicit work order: bounded CAS record with review-gated display."""
import dataclasses
import json

from support import Fixture
from mizu import work_order as plan
from mizu.errors import Denied
from mizu.fs import now, write_json
from mizu.snapshot import PROGRESS_SIGNALS


class WorkOrderTests(Fixture):
    def parked(self):
        cur = self.project.snapshots.get()
        progress = {"schema": 1, "signals": {n: False for n in PROGRESS_SIGNALS},
                    "selected": list(self.config.limits.progress_signals), "role": "worker",
                    "has_progress": False, "no_progress_count": 1,
                    "obligations_digest": "0" * 64, "recorded_at": now()}
        snap = self.project.snapshots.create(
            self.project.snapshots.capture_files(self.project.workspace),
            goal=self.project.goal, state="Parked", run=None, outcome="done",
            summary="park", wake_at=None, inbox_seen=self.project.insights.generation(),
            wake_generation="test-wake", progress=progress, parent=cur["id"])
        self.project.snapshots.publish(snap)
        return snap

    def test_capability_is_writer_only_and_children_cannot_hold_it(self):
        from mizu.config import CAPABILITIES
        self.assertIn("work_order", CAPABILITIES)
        caps = self.config.roles["worker"].capabilities
        self.assertIn("work_order", caps)
        for name in ("searcher", "reviewer", "reporter", "consult"):
            self.assertNotIn("work_order", self.config.roles[name].capabilities)
        from mizu.runtime import CONSULT_FORBIDDEN, DELEGATE_FORBIDDEN, check_consult_role, check_delegate_role
        self.assertIn("work_order", CONSULT_FORBIDDEN)
        self.assertIn("work_order", DELEGATE_FORBIDDEN)
        with self.assertRaises(Exception):
            check_delegate_role("bad", dataclasses.replace(
                self.config.roles["searcher"], capabilities=("work_order", "finish")))
        with self.assertRaises(Exception):
            check_consult_role("bad", dataclasses.replace(
                self.config.roles["consult"], capabilities=("work_order", "finish")))

    def test_absent_current_review_due_unavailable_in_prompts_and_status(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        full = json.loads(prompt_for(ctx))
        delta = json.loads(prompt_delta_for(ctx, []))
        self.assertEqual(full["work_order"]["status"], "absent")
        self.assertEqual(delta["work_order"]["status"], "absent")
        self.assertEqual(self.project.work_order_status()["status"], "absent")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "do it",
                                  "expected_rev": 0, "expected_generation": frozen})
        ctx2 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "current")
        self.assertEqual(json.loads(prompt_delta_for(ctx2, []))["work_order"]["status"], "current")
        self.assertEqual(self.project.work_order_status()["status"], "current")
        # New actionable arrival moves both prompts and status to review_due.
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        ctx3 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx3))["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_delta_for(ctx3, []))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")
        # Corrupt store fails closed everywhere, never empty/current.
        (self.project.root / "work-order" / "current.json").write_text("{corrupt")
        ctx4 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx4))["work_order"]["status"], "unavailable")
        self.assertEqual(json.loads(prompt_delta_for(ctx4, []))["work_order"]["status"], "unavailable")
        self.assertEqual(self.project.work_order_status()["status"], "unavailable")

    def test_delta_omitted_pending_bodies_share_full_generation_and_status(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for

        item = self.project.work_items.create(idempotency_key="delta-pending", title="A", actor="worker")["record"]["work_item"]
        insight = self.project.insights.submit(source="worker", title="Pending", body="body", base_snapshot=None)
        ctx = self.context("worker")
        with patch.object(self.project.insights, "projection", wraps=self.project.insights.projection) as projection:
            delta = json.loads(prompt_delta_for(ctx, []))
            full = json.loads(prompt_for(ctx))
            self.assertEqual(projection.call_count, 1)
        self.assertEqual(delta["pending_insights"], [])
        self.assertEqual([row["id"] for row in full["pending_insights"]], [insight["id"]])
        self.assertEqual(delta["open_insights"], full["open_insights"])
        generation = delta["work_order"]["opening_generation"]
        self.assertEqual(generation, full["work_order"]["opening_generation"])
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "reviewed",
                                  "expected_rev": 0, "expected_generation": generation})
        fresh_delta = json.loads(prompt_delta_for(self.context("worker"), []))
        fresh_full = json.loads(prompt_for(self.context("worker")))
        self.assertEqual(fresh_delta["pending_insights"], [])
        self.assertEqual([row["id"] for row in fresh_full["pending_insights"]], [insight["id"]])
        for shown in (fresh_delta["work_order"], fresh_full["work_order"], self.project.work_order_status()):
            self.assertEqual(shown["status"], "current")
        self.assertEqual(fresh_delta["work_order"]["opening_generation"], generation)
        self.assertEqual(fresh_full["work_order"]["opening_generation"], generation)

        # A later arrival is not folded into the opening's backlog or generation.
        later = self.project.insights.submit(source="worker", title="Later", body="body", base_snapshot=None)
        with patch.object(self.project.insights, "projection", side_effect=AssertionError("mid-unit rescan")):
            frozen_delta = json.loads(prompt_delta_for(ctx, []))
            frozen_full = json.loads(prompt_for(ctx))
        self.assertEqual(frozen_delta["open_insights"], delta["open_insights"])
        self.assertEqual(frozen_full["pending_insights"], full["pending_insights"])
        self.assertEqual(frozen_delta["work_order"]["opening_generation"], generation)
        self.assertEqual(frozen_full["work_order"]["status"], "current")
        next_delta = json.loads(prompt_delta_for(self.context("worker"), []))
        self.assertIn(later["id"], next_delta["open_insights"]["ids"])
        self.assertEqual(next_delta["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_for(self.context("worker")))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

    def test_off_page_scope_change_requires_review_without_changing_ready_or_page(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        self.config = dataclasses.replace(self.config, limits=dataclasses.replace(
            self.config.limits, work_items_prompt=1))
        self.project.config = self.config
        for key in ("off-a", "off-b"):
            self.project.work_items.create(idempotency_key=key, title=key, actor="worker")
        page, fingerprint = self.project.work_items.page_open_for_order(limit=1)
        self.assertEqual(page, self.project.work_items.page_open(limit=1))
        all_ids = {row["id"] for row in self.project.work_items.page_open(limit=100)["items"]}
        off_page = (all_ids - {page["items"][0]["id"]}).pop()
        ready = self.project.work_items.page_ready(limit=1, role_name="worker")
        ctx = self.context("worker")
        original_prompt = json.loads(prompt_for(ctx))
        shown = original_prompt["work_order"]
        ctx.handle("work_order", {"items": [off_page], "rationale": "reviewed",
                                  "expected_rev": 0, "expected_generation": shown["opening_generation"]})
        self.assertEqual(self.project.work_order_status()["status"], "current")
        self.project.work_items.transition(off_page, expected_rev=1, actor="worker",
                                           status="open", scope="new off-page scope", reason="revise scope")
        new_page, new_fingerprint = self.project.work_items.page_open_for_order(limit=1)
        self.assertEqual(page, new_page)
        self.assertNotEqual(fingerprint, new_fingerprint)
        self.assertEqual(ready, self.project.work_items.page_ready(limit=1, role_name="worker"))
        # An already-open unit keeps the exact generation and prompt projections.
        frozen_delta = json.loads(prompt_delta_for(ctx, []))
        self.assertEqual(frozen_delta["work_order"]["status"], "current")
        self.assertEqual(frozen_delta["open_work_items"], original_prompt["open_work_items"])
        self.assertEqual(frozen_delta["ready_work"], original_prompt["ready_work"])
        self.assertEqual(frozen_delta["work_order"]["opening_generation"], shown["opening_generation"])
        fresh = self.context("worker")
        self.assertEqual(json.loads(prompt_for(fresh))["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_delta_for(fresh, []))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

    def test_off_page_redecision_changes_order_fingerprint(self):
        first = self.project.work_items.create(idempotency_key="held", title="Held", actor="worker")["record"]["work_item"]
        self.project.work_items.transition(first["id"], expected_rev=1, actor="worker", status="doing")
        proposal = self.project.insights.submit(source="worker", title="Proposal", body="scope",
                                                 base_snapshot=None)
        decision = self.project.insights.decide(proposal["id"], "accept", "accepted", "", "run", expected_rev=1)
        item_id = decision["work_item"]["id"]
        self.project.insights.revise(proposal["id"], source="worker", title="Revised", body="changed",
                                     base_snapshot=None, expected_rev=1)
        page, before = self.project.work_items.page_open_for_order(limit=1)
        self.assertNotEqual(page["items"][0]["id"], item_id)
        ready = self.project.work_items.page_ready(limit=1, role_name="worker")["signature"]
        self.project.insights.decide(proposal["id"], "defer", "reassessed", "later", "run2", expected_rev=2)
        after_page, after = self.project.work_items.page_open_for_order(limit=1)
        self.assertNotEqual(before, after)
        self.assertEqual(page, after_page)
        self.assertEqual(ready, self.project.work_items.page_ready(limit=1, role_name="worker")["signature"])
        view = self.project.work_items.projection(item_id)
        self.assertEqual(view["redecision"], {"rev": 2, "action": "defer"})

    def test_truncated_scope_tail_changes_inventory_fingerprint(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        item = self.project.work_items.create(idempotency_key="tail", title="Tail", actor="worker")["record"]["work_item"]
        prefix = "x" * 1100
        self.project.work_items.transition(item["id"], expected_rev=1, actor="worker",
                                           status="open", scope=prefix + " first", reason="scope")
        page, fingerprint = self.project.work_items.page_open_for_order(limit=1)
        self.assertTrue(page["items"][0]["scope_truncated"])
        ready_signature = self.project.work_items.page_ready(limit=1, role_name="worker")["signature"]
        ctx = self.context("worker")
        opening = json.loads(prompt_for(ctx))["work_order"]["opening_generation"]
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "reviewed",
                                  "expected_rev": 0, "expected_generation": opening})
        self.project.work_items.transition(item["id"], expected_rev=2, actor="worker",
                                           status="open", scope=prefix + " second", reason="scope")
        next_page, next_fingerprint = self.project.work_items.page_open_for_order(limit=1)
        self.assertEqual(next_page, self.project.work_items.page_open(limit=1))
        self.assertEqual(page["items"][0]["scope_preview"], next_page["items"][0]["scope_preview"])
        self.assertEqual(list(page), list(next_page))
        self.assertEqual([row["id"] for row in page["items"]], [row["id"] for row in next_page["items"]])
        self.assertNotEqual(fingerprint, next_fingerprint)
        self.assertEqual(ready_signature, self.project.work_items.page_ready(limit=1, role_name="worker")["signature"])
        fresh = self.context("worker")
        self.assertEqual(json.loads(prompt_for(fresh))["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_delta_for(fresh, []))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

    def test_displayed_absent_generation_creates_first_plan_for_both_prompts(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        a = self.project.work_items.create(idempotency_key="first", title="A", actor="worker")["record"]["work_item"]
        for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx, [])):
            with self.subTest(builder=build):
                # Each builder gets an independent absent store.
                root = self.project.root / "work-order"
                if root.exists():
                    import shutil
                    shutil.rmtree(root)
                ctx = self.context("worker")
                shown = json.loads(build(ctx))["work_order"]
                self.assertEqual(shown["status"], "absent")
                ctx.handle("work_order", {"items": [a["id"]], "rationale": "first",
                                          "expected_rev": shown["rev"],
                                          "expected_generation": shown["opening_generation"]})
                current = json.loads(build(self.context("worker")))["work_order"]
                self.assertEqual(current["status"], "current")
                self.assertEqual(current["opening_generation"], shown["opening_generation"])

    def test_absent_plan_is_unavailable_when_generation_fails(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx, [])):
            with self.subTest(builder=build), patch.object(plan, "generation_from_captured", side_effect=Denied("bad input")):
                shown = json.loads(build(self.context("worker")))["work_order"]
                self.assertEqual(shown["status"], "unavailable")
                self.assertNotIn("opening_generation", shown)

    def test_cas_rejects_stale_rev_and_generation_and_atomic_history(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        b = self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        out = ctx.handle("work_order", {"items": [a["id"]], "rationale": "first",
                                        "expected_rev": 0, "expected_generation": frozen})
        self.assertEqual(out["rev"], 1)
        # Stale rev refused.
        with self.assertRaises(Denied):
            ctx.handle("work_order", {"items": [a["id"]], "rationale": "stale",
                                      "expected_rev": 0, "expected_generation": frozen})
        # Stale generation refused even with the right rev.
        self.project.work_items.create(idempotency_key="c1", title="C", actor="worker")
        ctx2 = self.context("worker")
        fresh = plan._frozen_generation(ctx2)
        self.assertNotEqual(frozen, fresh)
        with self.assertRaises(Denied):
            ctx2.handle("work_order", {"items": [a["id"]], "rationale": "stale gen",
                                       "expected_rev": 1, "expected_generation": frozen})
        # Correct CAS appends history atomically.
        out2 = ctx2.handle("work_order", {"items": [b["id"]], "rationale": "second",
                                          "expected_rev": 1, "expected_generation": fresh})
        self.assertEqual(out2["rev"], 2)
        hist = self.project.root / "work-order" / "history"
        self.assertTrue((hist / "1.json").exists())
        self.assertTrue((hist / "2.json").exists())
        self.assertEqual(json.loads((hist / "2.json").read_text())["items"], [b["id"]])

    def test_rejects_invalid_terminal_duplicate_and_oversize_refs(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        # Unknown ID.
        with self.assertRaises(Denied):
            ctx.handle("work_order", {"items": ["wi_" + "0" * 32], "rationale": "bad",
                                      "expected_rev": 0, "expected_generation": frozen})
        # Duplicate IDs.
        with self.assertRaises(Denied):
            ctx.handle("work_order", {"items": [a["id"], a["id"]], "rationale": "dup",
                                      "expected_rev": 0, "expected_generation": frozen})
        # Blank rationale.
        with self.assertRaises(Denied):
            ctx.handle("work_order", {"items": [a["id"]], "rationale": "  ",
                                      "expected_rev": 0, "expected_generation": frozen})
        # Terminal ref refused.
        self.project.work_items.transition(a["id"], expected_rev=1, actor="worker", status="doing")
        self.project.work_items.transition(a["id"], expected_rev=2, actor="worker", status="dropped", reason="x")
        ctx2 = self.context("worker")
        fresh = plan._frozen_generation(ctx2)
        with self.assertRaises(Denied):
            ctx2.handle("work_order", {"items": [a["id"]], "rationale": "gone",
                                       "expected_rev": 0, "expected_generation": fresh})
        # Oversize rationale refused by protocol validation.
        b = self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")["record"]["work_item"]
        ctx3 = self.context("worker")
        fresh3 = plan._frozen_generation(ctx3)
        with self.assertRaises(Denied):
            ctx3.handle("work_order", {"items": [b["id"]], "rationale": "x" * 2000,
                                       "expected_rev": 0, "expected_generation": fresh3})

    def test_same_count_id_substitution_moves_generation(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        b = self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        before = plan._frozen_generation(ctx)
        # Same count (2 actionable) but different IDs: drop b, add c.
        self.project.work_items.transition(b["id"], expected_rev=1, actor="worker", status="doing")
        self.project.work_items.transition(b["id"], expected_rev=2, actor="worker", status="dropped", reason="x")
        self.project.work_items.create(idempotency_key="c1", title="C", actor="worker")
        after = plan._frozen_generation(self.context("worker"))
        self.assertNotEqual(before, after)

    def test_scope_and_redecision_change_moves_generation(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        before = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": before})
        # Accepted-scope change moves the generation to review_due.
        self.project.work_items.transition(a["id"], expected_rev=1, actor="worker",
                                           status="doing", scope="new accepted scope", reason="scope it")
        ctx2 = self.context("worker")
        after = plan._frozen_generation(ctx2)
        self.assertNotEqual(before, after)
        self.assertEqual(json.loads(__import__("mizu.unit_prompts", fromlist=["prompt_for"]).prompt_for(ctx2))["work_order"]["status"], "review_due")

    def test_mid_unit_arrival_is_not_retroactively_reviewed(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        # Arrival after opening does not move this unit's frozen generation.
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        from mizu.unit_prompts import prompt_for
        self.assertEqual(json.loads(prompt_for(ctx))["work_order"]["status"], "current")
        # The next unit observes the arrival as review_due.
        ctx2 = self.context("worker")
        self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "review_due")

    def test_unchanged_plan_stays_current_and_never_admits(self):
        from mizu.runtime import should_run
        from mizu.unit_prompts import prompt_for
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        ctx2 = self.context("worker")
        self.assertEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "current")
        # Review_due alone never admits: parked with no ready work stays parked.
        self.project.work_items.transition(a["id"], expected_rev=1, actor="worker", status="doing")
        self.project.work_items.transition(a["id"], expected_rev=2, actor="worker", status="dropped", reason="x")
        snap = self.parked()
        self.assertFalse(should_run(self.project, snap, role_name="worker"))
        ctx3 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx3))["work_order"]["status"], "review_due")

    def test_valid_plan_leaves_ready_work_and_selection_unchanged(self):
        from mizu.unit_prompts import prompt_for
        a = self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")["record"]["work_item"]
        b = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        before = [row["id"] for row in self.project.work_items.page_ready(limit=100, role_name="worker")["items"]]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        # Plan order differs from ready order; ready order must not move.
        ctx.handle("work_order", {"items": [a["id"], b["id"]] if before[0] == b["id"] else [b["id"], a["id"]],
                                  "rationale": "explicit", "expected_rev": 0, "expected_generation": frozen})
        after = [row["id"] for row in self.project.work_items.page_ready(limit=100, role_name="worker")["items"]]
        self.assertEqual(before, after)
        stored = self.project.work_order_status()
        self.assertEqual(stored["items"], json.loads(prompt_for(self.context("worker")))["work_order"]["items"])

    def test_non_writer_sees_no_plan_and_cannot_record(self):
        from mizu.unit_prompts import prompt_for
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        reporter = self.context("reporter")
        self.assertIsNone(json.loads(prompt_for(reporter)).get("work_order"))
        with self.assertRaises(Denied):
            reporter.handle("work_order", {"items": [a["id"]], "rationale": "x",
                                           "expected_rev": 0, "expected_generation": "0" * 64})

    def test_goal_and_configured_inputs_move_generation(self):
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        before = plan._frozen_generation(ctx)
        # Goal identity is part of the generation.
        goal_path = self.project.root / "PROJECT.md"
        goal_path.write_text("A different operator goal.\n")
        after_goal = plan._frozen_generation(self.context("worker"))
        self.assertNotEqual(before, after_goal)
        goal_path.write_text("Preserve correctness. Improve only with evidence.\n")

    def test_audit_corruption_and_interrupted_retry(self):
        from unittest.mock import patch
        item = self.project.work_items.create(idempotency_key="audit", title="Audit", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        gen = plan._frozen_generation(ctx)
        args = {"items": [item["id"]], "rationale": "audit", "expected_rev": 0,
                "expected_generation": gen}
        original = plan.write_json
        def fail_pointer(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("interrupted")
            return original(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_pointer):
            with self.assertRaises(Denied):
                ctx.handle("work_order", args)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        hist = self.project.root / "work-order" / "history" / "1.json"
        valid = hist.read_text()
        bad = json.loads(valid)
        bad["updated_at"] = "not-a-time"
        hist.write_text(json.dumps(bad))
        with self.assertRaises(Denied):
            ctx.handle("work_order", args)
        self.assertFalse((hist.parent.parent / "current.json").exists())
        hist.write_text(valid)
        self.assertEqual(ctx.handle("work_order", args)["rev"], 1)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "current")
        # The same interrupted append recovery also works with an existing pointer.
        args2 = dict(args, expected_rev=1, rationale="second")
        with patch.object(plan, "write_json", side_effect=fail_pointer):
            with self.assertRaises(Denied):
                ctx.handle("work_order", args2)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        # A stale generation that matches neither the candidate nor the opening stays denied.
        with self.assertRaises(Denied):
            ctx.handle("work_order", dict(args2, rationale="other", expected_generation="00" * 32))
        self.assertEqual(ctx.handle("work_order", args2)["rev"], 2)
        hist.unlink()
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        hist.write_text(valid)
        hist = hist.parent / "2.json"
        bad = json.loads(hist.read_text())
        bad["rationale"] = "different"
        hist.write_text(json.dumps(bad))
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        hist.unlink()
        (hist.parent.parent / "current.json").unlink()
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")

    def test_noncanonical_duplicate_key_record_fails_closed(self):
        item = self.project.work_items.create(idempotency_key="duplicate-key", title="Duplicate", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "audit",
                                  "expected_rev": 0, "expected_generation": plan._frozen_generation(ctx)})
        path = self.project.root / "work-order" / "history" / "1.json"
        raw = path.read_bytes()
        self.assertIn(b'"schema":1', raw)
        path.write_bytes(raw.replace(b'"schema":1', b'"schema":0,"schema":1', 1))
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")

    def test_unsafe_history_entry_fails_closed(self):
        item = self.project.work_items.create(idempotency_key="unsafe", title="Unsafe", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "audit",
                                  "expected_rev": 0, "expected_generation": plan._frozen_generation(ctx)})
        root = self.project.root / "work-order"
        hist = root / "history" / "1.json"
        hist.unlink()
        try:
            hist.symlink_to(root / "current.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")

    def test_scope_binds_exact_prompt_page_and_signature_covers_beyond_page_ids(self):
        # The generation hashes the exact bounded open-work page shown plus
        # the full ready signature (same-count substitution beyond the page
        # still moves via the signature). Volatile ready ages never move it.
        import copy
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        opening = plan._capture_opening(ctx)
        base_kwargs = dict(goal_digest=opening["goal_digest"],
                           open_work_items=opening["open_work_items"],
                           ready_work=opening["ready_work"],
                           ready_signature=opening["ready_signature"],
                           order_inventory=opening["_order_inventory"],
                           pending_insights=opening["pending_insights"],
                           open_backlog=opening["open_backlog"],
                           ci_failures=opening["ci_failures"],
                           ci_truncated=opening["ci_truncated"],
                           decision_events=opening["decision_events"],
                           decision_page=opening["decision_page"],
                           diagnostics=opening["diagnostics"],
                           work_item_events=opening["work_item_events"],
                           wait_events=opening["wait_events"],
                           obligations=opening["obligations"])
        base = plan.generation_from_captured(**base_kwargs)
        self.assertEqual(plan.generation_from_captured(**base_kwargs), base)
        # Beyond-page same-count ID substitution moves only the signature.
        swapped = dict(base_kwargs, ready_signature="22" * 32 if base_kwargs["ready_signature"] != "22" * 32 else "11" * 32)
        self.assertNotEqual(plan.generation_from_captured(**swapped), base)
        # The order-only fingerprint independently moves the generation.
        self.assertNotEqual(plan.generation_from_captured(**dict(base_kwargs, order_inventory="ff" * 32)), base)
        # A change inside the displayed open page moves the generation.
        changed_open = copy.deepcopy(opening["open_work_items"])
        if changed_open["items"]:
            changed_open["items"] = [dict(changed_open["items"][0], title="changed") if isinstance(changed_open["items"][0], dict) else changed_open["items"][0]] + changed_open["items"][1:]
        else:
            changed_open = dict(changed_open, total=changed_open.get("total", 0) + 1)
        changed = dict(base_kwargs, open_work_items=changed_open)
        self.assertNotEqual(plan.generation_from_captured(**changed), base)
        # Volatile ready ages never move the generation.
        aged = copy.deepcopy(opening["ready_work"])
        for row in aged.get("items", []):
            if isinstance(row, dict):
                row["age_seconds"] = 999999
        self.assertEqual(plan.generation_from_captured(**dict(base_kwargs, ready_work=aged)), base)

    def test_decision_page_summary_is_frozen_hashed_and_used_by_status(self):
        from unittest.mock import patch
        from mizu import decision_delivery
        from mizu.unit_prompts import prompt_for, prompt_delta_for

        item = self.project.work_items.create(idempotency_key="page", title="A", actor="worker")["record"]["work_item"]
        state = {"shared": True, "events": [], "diagnostics": [], "manifest": (),
                 "page": {"events": [{"secret": "hidden"}], "pending_count": 1,
                          "truncated": True, "count_truncated": True, "byte_truncated": False}}
        ctx = self.context("worker")
        decision_delivery.bind(ctx, state)
        full = json.loads(prompt_for(ctx))
        generation = full["work_order"]["opening_generation"]
        self.assertEqual(full["decision_page"], {k: v for k, v in state["page"].items() if k != "events"})
        self.assertNotIn("hidden", json.dumps(full))
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "review",
                                  "expected_rev": 0, "expected_generation": generation})
        # Mid-unit mutations do not rewrite the captured summary in either prompt.
        state["page"]["pending_count"] = 2
        state["page"]["events"] = [{"secret": "other"}]
        delta = json.loads(prompt_delta_for(ctx, []))
        self.assertEqual(delta["decision_page"], full["decision_page"])
        self.assertEqual(delta["work_order"]["opening_generation"], generation)
        with patch.object(decision_delivery, "pending", return_value=state):
            self.assertEqual(self.project.work_order_status()["status"], "review_due")
            newer = self.context("worker")
            decision_delivery.bind(newer, state)
            shown = json.loads(prompt_for(newer))
            self.assertNotEqual(shown["work_order"]["opening_generation"], generation)
            self.assertEqual(shown["decision_page"]["pending_count"], 2)
            self.assertNotIn("other", json.dumps(shown))
            state["page"]["pending_count"] = 1
            self.assertEqual(self.project.work_order_status()["status"], "current")
            same = self.context("worker")
            decision_delivery.bind(same, state)
            self.assertEqual(json.loads(prompt_delta_for(same, []))["work_order"]["opening_generation"], generation)

    def test_roles_without_grant_capture_prompts_without_hashing(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        for role in ("reporter", "consult"):
            for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx, [])):
                with self.subTest(role=role, builder=build), patch.object(
                        plan, "generation_from_captured", side_effect=AssertionError("unexpected hash")) as hashed:
                    shown = json.loads(build(self.context(role)))
                    self.assertIsNone(shown["work_order"])
                    self.assertIn("ready_work", shown)
                    hashed.assert_not_called()

    def test_ack_removal_stability_decision_and_terminal(self):
        # A change in the configured actionable-input set -- including an
        # event disappearing after acknowledgement -- is a semantic input
        # change and may make the next unit review_due. There is no
        # requirement to preserve current after acks and no ack-stable scan.
        import dataclasses
        from mizu.unit_prompts import prompt_for

        def _populate(ctx, role):
            from mizu import decision_delivery as _delivery
            from mizu import work_item_events as _tev
            from mizu import ci_waits as _ciw
            from mizu.config import role_work_item_events_limit as _limit_of
            state = _delivery.pending(ctx.project, role)
            ctx.decision_events = list(state.get("events", []) or [])
            ctx.decision_receipt_diagnostics = list(state.get("diagnostics", []) or [])
            page = _tev.pending(ctx.project.work_items, role.name,
                                getattr(role, "work_item_events", ()),
                                sources=getattr(role, "work_item_sources", "own") or "own",
                                limit=_limit_of(ctx.project.config, role))
            page = _tev.delivery(role.name, page)
            ctx.work_item_events = list(page.get("events", []) or [])
            try:
                snap = ctx.project.snapshots.get()
            except Exception:
                snap = ctx.snapshot
            ctx.wait_events = list(ctx.project.insights.due_waits(role.name, snap)
                                   + _ciw.pending(ctx.project.work_items, role.name))
            ctx.obligations = list(ctx.project.insights.pending_obligations(role.name))

        # Worker with decision + terminal subscriptions plus the writer grant.
        base = self.config.roles["worker"]
        role = dataclasses.replace(base, decision_events=("reject",),
                                   work_item_events=("done", "dropped"),
                                   work_item_sources="any")
        # One open item for the plan; one insight decided reject (pending
        # decision for this role); one closed item (pending terminal).
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        sub = self.project.insights.submit(source="worker", title="T", body="b",
                                           base_snapshot=None, run="test")
        self.project.insights.decide(sub["id"], "reject", "no", "", "test", expected_rev=1)
        b = self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")["record"]["work_item"]
        self.project.work_items.transition(b["id"], expected_rev=1, actor="worker", status="doing")
        self.project.work_items.transition(b["id"], expected_rev=2, actor="worker", status="dropped", reason="no")
        ctx = self.context("worker")
        ctx.role = role
        _populate(ctx, role)
        self.assertTrue(ctx.decision_events, "expected pending decision for worker")
        self.assertTrue(ctx.work_item_events, "expected pending terminal for worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        self.assertEqual(json.loads(prompt_for(ctx))["work_order"]["status"], "current")
        # Acknowledge the decision: the configured set loses one row, so the
        # next unit legitimately reads review_due.
        pending = self.project.insights.decision_events_with_diagnostics("worker", ("reject",))
        self.assertTrue(pending[0], "expected pending decision for worker")
        self.project.insights.acknowledge_decisions("worker", pending[0], sources="own")
        ctx2 = self.context("worker")
        ctx2.role = role
        _populate(ctx2, role)
        self.assertFalse(ctx2.decision_events, "decision should be receipted")
        self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "review_due")
        # Acknowledge the terminal: the configured set loses another row, so
        # the following unit also reads review_due.
        from mizu import work_item_events as _tev
        page = _tev.pending(self.project.work_items, "worker", ("done", "dropped"),
                            sources="any", limit=10)
        self.assertTrue(page["events"], "expected pending terminal for worker")
        delivered = _tev.delivery("worker", page)["events"]
        # Acknowledge only the closed b (keep a open for the plan).
        payloads = [_tev.acknowledgement_payload(self.project.work_items, "worker", e)
                    for e in delivered if e["id"] == b["id"]]
        self.assertTrue(payloads, "expected closed-b payload")
        _tev.acknowledge(self.project.work_items, "worker", payloads)
        ctx3 = self.context("worker")
        ctx3.role = role
        _populate(ctx3, role)
        self.assertNotEqual(plan._frozen_generation(ctx3), frozen)
        self.assertEqual(json.loads(prompt_for(ctx3))["work_order"]["status"], "review_due")
        # A genuinely new decision arrival (new history) still moves to review_due.
        sub2 = self.project.insights.submit(source="worker", title="T2", body="b2",
                                            base_snapshot=None, run="test")
        self.project.insights.decide(sub2["id"], "reject", "no2", "", "test", expected_rev=1)
        ctx4 = self.context("worker")
        ctx4.role = role
        _populate(ctx4, role)
        self.assertTrue(ctx4.decision_events, "expected new pending decision")
        self.assertNotEqual(plan._frozen_generation(ctx4), frozen)
        self.assertEqual(json.loads(prompt_for(ctx4))["work_order"]["status"], "review_due")


    def test_new_pending_insight_moves_generation(self):
        # Pending insights are prompt-visible for insights/decide roles and
        # ack-stable (no receipt): a new arrival must move to review_due.
        from mizu.unit_prompts import prompt_for
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        ctx2 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "current")
        self.project.insights.submit(source="worker", title="New", body="arrival",
                                     base_snapshot=None, run="test")
        ctx3 = self.context("worker")
        self.assertNotEqual(plan._frozen_generation(ctx3), frozen)
        self.assertEqual(json.loads(prompt_for(ctx3))["work_order"]["status"], "review_due")

    def test_new_ci_failure_moves_generation_for_vcs_read_role(self):
        # CI failures are prompt-visible only for vcs_read roles; a new cache
        # entry must move a vcs_read holder to review_due while the writer
        # without the grant stays stable. The poll cache is untouched by CI-
        # wait acknowledgement, so the input is already ack-stable.
        import dataclasses
        from mizu.unit_prompts import prompt_for
        from mizu.fs import write_json as _wj
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        # Writer without vcs_read: CI cache changes do not move them.
        _wj(self.project.root / "vcs-poll.json",
            {"failures": {"mizu": [{"sha": "a" * 40, "check": "core"}]}, "successes": {}})
        ctx_plain = self.context("worker")
        self.assertEqual(plan._frozen_generation(ctx_plain), plan._frozen_generation(self.context("worker")))
        # Holder with both grants sees the same stored plan as review_due.
        both = dataclasses.replace(self.config.roles["worker"],
                                   capabilities=tuple(list(self.config.roles["worker"].capabilities) + ["vcs_read"]))
        ctx_both_before = self.context("worker")
        ctx_both_before.role = both
        # Recompute frozen with the grant: must differ from the no-grant
        # frozen only by the CI marker, and the new cache entry moves it.
        # First clear the cache to get the empty-marker baseline for the grant.
        import os as _os
        try:
            _os.unlink(self.project.root / "vcs-poll.json")
        except FileNotFoundError:
            pass
        base_both = self.context("worker")
        base_both.role = both
        empty_marker = plan._frozen_generation(base_both)
        _wj(self.project.root / "vcs-poll.json",
            {"failures": {"mizu": [{"sha": "b" * 40, "check": "core"}]}, "successes": {}})
        moved_both = self.context("worker")
        moved_both.role = both
        self.assertNotEqual(plan._frozen_generation(moved_both), empty_marker)

    def test_unreadable_ci_marker_is_hashed_as_unknown_in_status(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for
        worker = self.config.roles["worker"]
        role = dataclasses.replace(worker, capabilities=tuple((*worker.capabilities, "vcs_read")))
        config_roles = dict(self.project.config.roles)
        config_roles["worker"] = role
        self.project.config = dataclasses.replace(self.project.config, roles=config_roles)
        item = self.project.work_items.create(
            idempotency_key="unknown-ci", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        ctx.role = role
        shown = json.loads(prompt_for(ctx))["work_order"]
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "reviewed",
                                  "expected_rev": shown["rev"],
                                  "expected_generation": shown["opening_generation"]})
        with patch("mizu.ci_observation.read_json", side_effect=OSError("unreadable cache")):
            status = self.project.work_order_status()
        self.assertEqual(status["status"], "review_due")
        self.assertIsNotNone(status["current_generation"])

    def test_exact_prompt_open_snapshot_and_mid_unit_arrivals(self):
        # The frozen generation binds the exact bounded prompt projections at
        # opening (open page, pending insights, CI marker for this role);
        # arrivals after opening (new work item + new insight mid-unit) do
        # not move this unit's frozen value, but the next unit sees them.
        from mizu.unit_prompts import prompt_for
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        frozen = plan._frozen_generation(ctx)
        shown = json.loads(prompt_for(ctx))
        # Frozen binds the shown open page total and pending-insight backlog.
        self.assertEqual(shown["open_work_items"]["total"], 1)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": frozen})
        # Mid-unit arrivals after opening: new work item + new insight.
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        self.project.insights.submit(source="worker", title="Mid", body="unit",
                                     base_snapshot=None, run="test")
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        self.assertEqual(json.loads(prompt_for(ctx))["work_order"]["status"], "current")
        ctx2 = self.context("worker")
        self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "review_due")

    def test_first_write_crash_safety_empty_history_recovers_absent(self):
        # Crash after mkdir (root and/or empty history) before the first
        # append leaves no pointer and a truly empty history: harmless,
        # recovers as absent. Any entries without a pointer stay unavailable.
        from pathlib import Path as _P
        root = _P(self.project.root) / "work-order"
        hist = root / "history"
        root.mkdir(parents=True, exist_ok=True)
        hist.mkdir(parents=True, exist_ok=True)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "absent")
        # A history entry without a pointer is corrupt, not pristine.
        import json as _json
        from mizu.fs import write_json as _wj
        _wj(hist / "1.json", {"schema": 1, "rev": 1, "generation": "0" * 64,
                              "items": ["wi_" + "00" * 16], "rationale": "x",
                              "actor": "worker", "updated_at": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")

    def test_frozen_capture_holds_for_full_across_all_inputs(self):
        # Full prompt: capture once, inject item/insight/CI/decision between
        # capture and work-order projection, prove the same unit stays
        # frozen and the next unit observes review_due.
        import dataclasses
        from mizu.unit_prompts import prompt_for
        from mizu.fs import write_json as _wj
        base = self.config.roles["worker"]
        role = dataclasses.replace(base, decision_events=("reject",),
                                   capabilities=tuple(list(base.capabilities) + ["vcs_read"]))
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        sub = self.project.insights.submit(source="worker", title="T", body="b",
                                           base_snapshot=None, run="test")
        self.project.insights.decide(sub["id"], "reject", "no", "", "test", expected_rev=1)
        ctx = self.context("worker")
        ctx.role = role
        # Populate dispatch-frozen events as Engine would.
        from mizu import decision_delivery as _delivery
        from mizu import work_item_events as _tev
        from mizu import ci_waits as _ciw
        from mizu.config import role_work_item_events_limit as _limit_of
        state = _delivery.pending(ctx.project, role)
        ctx.decision_events = list(state.get("events", []) or [])
        ctx.decision_receipt_diagnostics = list(state.get("diagnostics", []) or [])
        page = _tev.pending(ctx.project.work_items, role.name,
                            getattr(role, "work_item_events", ()),
                            sources=getattr(role, "work_item_sources", "own") or "own",
                            limit=_limit_of(ctx.project.config, role))
        page = _tev.delivery(role.name, page)
        ctx.work_item_events = list(page.get("events", []) or [])
        try:
            snap = ctx.project.snapshots.get()
        except Exception:
            snap = ctx.snapshot
        ctx.wait_events = list(ctx.project.insights.due_waits(role.name, snap)
                               + _ciw.pending(ctx.project.work_items, role.name))
        ctx.obligations = list(ctx.project.insights.pending_obligations(role.name))
        self.assertTrue(ctx.decision_events, "expected pending decision")
        # Capture via the full prompt (also freezes the generation).
        shown = json.loads(prompt_for(ctx))
        self.assertEqual(shown["work_order"]["status"], "absent")
        frozen = shown["work_order"]["opening_generation"]
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        # First absent-plan write using the displayed token succeeds.
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "first",
                                  "expected_rev": shown["work_order"]["rev"],
                                  "expected_generation": frozen})
        self.assertEqual(json.loads(prompt_for(ctx))["work_order"]["status"], "current")
        # Inject all four input kinds after capture.
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        self.project.insights.submit(source="worker", title="Mid", body="unit",
                                     base_snapshot=None, run="test")
        _wj(self.project.root / "vcs-poll.json",
            {"failures": {"mizu": [{"sha": "c" * 40, "check": "core"}]}, "successes": {}})
        sub2 = self.project.insights.submit(source="worker", title="T2", body="b2",
                                            base_snapshot=None, run="test")
        self.project.insights.decide(sub2["id"], "reject", "no2", "", "test", expected_rev=1)
        # Same unit stays frozen: same generation, still current.
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        self.assertEqual(json.loads(prompt_for(ctx))["work_order"]["status"], "current")
        self.assertEqual(json.loads(prompt_for(ctx))["open_work_items"]["total"],
                         shown["open_work_items"]["total"])
        # Next unit observes the arrivals as review_due.
        ctx2 = self.context("worker")
        ctx2.role = role
        state2 = _delivery.pending(ctx2.project, role)
        ctx2.decision_events = list(state2.get("events", []) or [])
        ctx2.decision_receipt_diagnostics = list(state2.get("diagnostics", []) or [])
        page2 = _tev.pending(ctx2.project.work_items, role.name,
                             getattr(role, "work_item_events", ()),
                             sources=getattr(role, "work_item_sources", "own") or "own",
                             limit=_limit_of(ctx2.project.config, role))
        page2 = _tev.delivery(role.name, page2)
        ctx2.work_item_events = list(page2.get("events", []) or [])
        try:
            snap2 = ctx2.project.snapshots.get()
        except Exception:
            snap2 = ctx2.snapshot
        ctx2.wait_events = list(ctx2.project.insights.due_waits(role.name, snap2)
                                + _ciw.pending(ctx2.project.work_items, role.name))
        ctx2.obligations = list(ctx2.project.insights.pending_obligations(role.name))
        self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "review_due")

    def test_frozen_capture_holds_for_delta_across_all_inputs(self):
        # Delta prompt: same frozen guarantee with the delta pending subset.
        import dataclasses
        from mizu.unit_prompts import prompt_delta_for
        from mizu.fs import write_json as _wj
        base = self.config.roles["worker"]
        role = dataclasses.replace(base, decision_events=("reject",),
                                   capabilities=tuple(list(base.capabilities) + ["vcs_read"]))
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        sub = self.project.insights.submit(source="worker", title="T", body="b",
                                           base_snapshot=None, run="test")
        self.project.insights.decide(sub["id"], "reject", "no", "", "test", expected_rev=1)
        ctx = self.context("worker")
        ctx.role = role
        from mizu import decision_delivery as _delivery
        from mizu import work_item_events as _tev
        from mizu import ci_waits as _ciw
        from mizu.config import role_work_item_events_limit as _limit_of
        state = _delivery.pending(ctx.project, role)
        ctx.decision_events = list(state.get("events", []) or [])
        ctx.decision_receipt_diagnostics = list(state.get("diagnostics", []) or [])
        page = _tev.pending(ctx.project.work_items, role.name,
                            getattr(role, "work_item_events", ()),
                            sources=getattr(role, "work_item_sources", "own") or "own",
                            limit=_limit_of(ctx.project.config, role))
        page = _tev.delivery(role.name, page)
        ctx.work_item_events = list(page.get("events", []) or [])
        try:
            snap = ctx.project.snapshots.get()
        except Exception:
            snap = ctx.snapshot
        ctx.wait_events = list(ctx.project.insights.due_waits(role.name, snap)
                               + _ciw.pending(ctx.project.work_items, role.name))
        ctx.obligations = list(ctx.project.insights.pending_obligations(role.name))
        shown = json.loads(prompt_delta_for(ctx, []))
        self.assertEqual(shown["work_order"]["status"], "absent")
        frozen = shown["work_order"]["opening_generation"]
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "first",
                                  "expected_rev": shown["work_order"]["rev"],
                                  "expected_generation": frozen})
        self.assertEqual(json.loads(prompt_delta_for(ctx, []))["work_order"]["status"], "current")
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        self.project.insights.submit(source="worker", title="Mid", body="unit",
                                     base_snapshot=None, run="test")
        _wj(self.project.root / "vcs-poll.json",
            {"failures": {"mizu": [{"sha": "d" * 40, "check": "core"}]}, "successes": {}})
        sub2 = self.project.insights.submit(source="worker", title="T2", body="b2",
                                            base_snapshot=None, run="test")
        self.project.insights.decide(sub2["id"], "reject", "no2", "", "test", expected_rev=1)
        self.assertEqual(plan._frozen_generation(ctx), frozen)
        self.assertEqual(json.loads(prompt_delta_for(ctx, []))["work_order"]["status"], "current")
        ctx2 = self.context("worker")
        ctx2.role = role
        state2 = _delivery.pending(ctx2.project, role)
        ctx2.decision_events = list(state2.get("events", []) or [])
        ctx2.decision_receipt_diagnostics = list(state2.get("diagnostics", []) or [])
        page2 = _tev.pending(ctx2.project.work_items, role.name,
                             getattr(role, "work_item_events", ()),
                             sources=getattr(role, "work_item_sources", "own") or "own",
                             limit=_limit_of(ctx2.project.config, role))
        page2 = _tev.delivery(role.name, page2)
        ctx2.work_item_events = list(page2.get("events", []) or [])
        try:
            snap2 = ctx2.project.snapshots.get()
        except Exception:
            snap2 = ctx2.snapshot
        ctx2.wait_events = list(ctx2.project.insights.due_waits(role.name, snap2)
                                + _ciw.pending(ctx2.project.work_items, role.name))
        ctx2.obligations = list(ctx2.project.insights.pending_obligations(role.name))
        self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
        self.assertEqual(json.loads(prompt_delta_for(ctx2, []))["work_order"]["status"], "review_due")

    def test_acknowledged_input_moves_next_unit_for_full_and_delta(self):
        # Acknowledging a configured actionable input is a semantic set
        # change: the next unit reads review_due for both prompt variants.
        import dataclasses
        from mizu.unit_prompts import prompt_for, prompt_delta_for

        def _populate(ctx, role):
            from mizu import decision_delivery as _delivery
            from mizu import work_item_events as _tev
            from mizu import ci_waits as _ciw
            from mizu.config import role_work_item_events_limit as _limit_of
            state = _delivery.pending(ctx.project, role)
            ctx.decision_events = list(state.get("events", []) or [])
            ctx.decision_receipt_diagnostics = list(state.get("diagnostics", []) or [])
            page = _tev.pending(ctx.project.work_items, role.name,
                                getattr(role, "work_item_events", ()),
                                sources=getattr(role, "work_item_sources", "own") or "own",
                                limit=_limit_of(ctx.project.config, role))
            page = _tev.delivery(role.name, page)
            ctx.work_item_events = list(page.get("events", []) or [])
            try:
                snap = ctx.project.snapshots.get()
            except Exception:
                snap = ctx.snapshot
            ctx.wait_events = list(ctx.project.insights.due_waits(role.name, snap)
                                   + _ciw.pending(ctx.project.work_items, role.name))
            ctx.obligations = list(ctx.project.insights.pending_obligations(role.name))

        base = self.config.roles["worker"]
        role = dataclasses.replace(base, decision_events=("reject",))
        a = self.project.work_items.create(idempotency_key="a1", title="A", actor="worker")["record"]["work_item"]
        for idx, build in enumerate((lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx, []))):
            with self.subTest(builder=build):
                # Fresh store and fresh decision per builder.
                import shutil
                root = self.project.root / "work-order"
                if root.exists():
                    shutil.rmtree(root)
                sub = self.project.insights.submit(source="worker", title=f"T-{idx}", body="b",
                                                   base_snapshot=None, run="test")
                self.project.insights.decide(sub["id"], "reject", "no", "", "test", expected_rev=1)
                ctx = self.context("worker")
                ctx.role = role
                _populate(ctx, role)
                self.assertTrue(ctx.decision_events, "expected pending decision")
                frozen = plan._frozen_generation(ctx)
                ctx.handle("work_order", {"items": [a["id"]], "rationale": "ack",
                                          "expected_rev": 0, "expected_generation": frozen})
                self.assertEqual(json.loads(build(ctx))["work_order"]["status"], "current")
                pending = self.project.insights.decision_events_with_diagnostics("worker", ("reject",))
                # Acknowledge only the just-created decision for this builder.
                target = [e for e in pending[0] if isinstance(e, dict) and e.get("insight") == sub["id"]]
                self.assertTrue(target, "expected pending decision for this builder")
                self.project.insights.acknowledge_decisions("worker", target, sources="own")
                ctx2 = self.context("worker")
                ctx2.role = role
                _populate(ctx2, role)
                self.assertNotEqual(plan._frozen_generation(ctx2), frozen)
                self.assertEqual(json.loads(build(ctx2))["work_order"]["status"], "review_due")

    def test_protocol_accepts_absolute_revision_beyond_retention_window(self):
        from mizu.protocol import DEFINITIONS, validate
        validate({"items": ["wi_" + "0" * 32], "rationale": "reviewed",
                  "expected_rev": 257, "expected_generation": "0" * 64},
                 DEFINITIONS["work_order"][1])

    def test_rollover_bounded_history_and_integrity(self):
        from unittest.mock import patch
        from mizu.fs import canonical, digest
        item = self.project.work_items.create(idempotency_key="rollover", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        gen = plan._frozen_generation(ctx)
        root = self.project.root / "work-order"
        hist = root / "history"
        hist.mkdir(parents=True)
        for rev in range(1, 256):
            record = {"schema": 1, "rev": rev, "generation": gen, "items": [item["id"]],
                      "rationale": "review", "actor": "worker", "updated_at": now()}
            write_json(hist / f"{rev}.json", record)
        write_json(root / "current.json", record)
        args = {"items": [item["id"]], "rationale": "review", "expected_generation": gen}
        bodies = {n: (hist / f"{n}.json").read_bytes() for n in range(1, 6)}
        previous = plan._GENESIS
        for rev in range(256, 262):
            self.assertEqual(ctx.handle("work_order", dict(args, expected_rev=rev-1))["rev"], rev)
            self.assertEqual(plan._read_stored(self.project.root)["rev"], rev)
            self.assertEqual(len(list(hist.iterdir())), 256)
            if rev > 256:
                pruned = rev - 256
                cp = json.loads((root / "checkpoint.json").read_bytes())
                previous = plan._checkpoint_hash(pruned, previous, digest(bodies[pruned]))
                self.assertEqual(cp["digest"], previous)
                self.assertEqual(cp["body"], digest(bodies[pruned]))
                self.assertFalse((hist / f"{pruned}.json").exists())
            with self.assertRaises(Denied):
                ctx.handle("work_order", dict(args, expected_rev=rev-1))
        with patch.object(plan, "safe_read", wraps=plan.safe_read) as reads:
            self.assertEqual(plan._read_stored(self.project.root)["status"], "current")
        self.assertEqual(reads.call_count, 258)
        total = sum(p.stat().st_size for p in hist.iterdir()) + (root / "current.json").stat().st_size + (root / "checkpoint.json").stat().st_size
        self.assertLessEqual(total, 258 * plan.MAX_WORK_ORDER_BYTES)
        self.assertEqual(len(canonical(cp)), (root / "checkpoint.json").stat().st_size)
        checkpoint_path = root / "checkpoint.json"
        checkpoint = checkpoint_path.read_bytes()
        checkpoint_path.unlink()
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        checkpoint_path.write_bytes(checkpoint)
        for bad in (dict(cp, digest="0" * 64), dict(cp, body="x"),
                    dict(cp, rev=cp["rev"] + 1), dict(cp, schema=True),
                    dict(cp, unexpected=1)):
            write_json(root / "checkpoint.json", bad)
            self.assertEqual(plan._read_stored(self.project.root, allow_interrupted=True)["status"], "unavailable")
        (root / "checkpoint.json").write_bytes(checkpoint)
        head = hist / "261.json"
        saved_head = head.read_bytes()
        wrong_head = json.loads(saved_head)
        wrong_head["rationale"] = "not current"
        write_json(head, wrong_head)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        head.write_bytes(saved_head)
        missing = hist / "6.json"
        saved = missing.read_bytes()
        missing.unlink()
        self.assertEqual(plan._read_stored(self.project.root, allow_interrupted=True)["status"], "unavailable")
        missing.write_bytes(saved)
        (hist / "1.json").write_bytes(bodies[1])
        self.assertEqual(plan._read_stored(self.project.root, allow_interrupted=True)["status"], "unavailable")

    def test_rollover_interrupted_boundaries_retry_and_corruption(self):
        from unittest.mock import patch
        item = self.project.work_items.create(idempotency_key="roll-crash", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        gen = plan._frozen_generation(ctx)
        root = self.project.root / "work-order"
        hist = root / "history"
        hist.mkdir(parents=True)
        for rev in range(1, 257):
            record = {"schema": 1, "rev": rev, "generation": gen, "items": [item["id"]],
                      "rationale": "review", "actor": "worker", "updated_at": now()}
            write_json(hist / f"{rev}.json", record)
        write_json(root / "current.json", record)
        args = {"items": [item["id"]], "rationale": "next", "expected_generation": gen, "expected_rev": 256}
        original_unlink = type(hist).unlink
        def fail_prune(path, *a, **kw):
            if path == hist / "1.json":
                raise OSError("after checkpoint")
            return original_unlink(path, *a, **kw)
        with patch.object(type(hist), "unlink", fail_prune), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        old = (hist / "1.json").read_bytes()
        altered = json.loads(old)
        altered["rationale"] = "tampered"
        write_json(hist / "1.json", altered)
        with self.assertRaises(Denied):
            ctx.handle("work_order", args)
        (hist / "1.json").write_bytes(old)
        with self.assertRaises(Denied):
            ctx.handle("work_order", dict(args, expected_rev=255))
        with patch.object(plan, "sync_dir", side_effect=OSError("after prune")), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        self.assertFalse((hist / "1.json").exists())
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        original_write = plan.write_json
        def fail_current(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("after append")
            return original_write(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_current), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        self.assertTrue((hist / "257.json").exists())
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        # A stale generation that matches neither the candidate nor the opening stays denied.
        with self.assertRaises(Denied):
            ctx.handle("work_order", dict(args, rationale="different", expected_generation="00" * 32))
        self.assertEqual(ctx.handle("work_order", args)["rev"], 257)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "current")

    def _seed_rollover(self):
        item = self.project.work_items.create(idempotency_key="fresh-recovery", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        gen = plan._frozen_generation(ctx)
        root = self.project.root / "work-order"
        hist = root / "history"
        hist.mkdir(parents=True)
        for rev in range(1, 257):
            record = {"schema": 1, "rev": rev, "generation": gen, "items": [item["id"]],
                      "rationale": "review", "actor": "worker", "updated_at": now()}
            write_json(hist / f"{rev}.json", record)
        write_json(root / "current.json", record)
        return ctx, root, {"items": [item["id"]], "rationale": "next",
                           "expected_generation": gen, "expected_rev": 256}

    def test_fresh_unit_recovers_exact_candidate_after_generation_change(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        ctx, root, args = self._seed_rollover()
        original = plan.write_json
        def fail_current(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("after append")
            return original(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_current), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        before = (root / "current.json").read_bytes()
        self.project.insights.submit(source="worker", title="New", body="body", base_snapshot=None)
        fresh = self.context("worker")
        full = json.loads(prompt_for(fresh))["work_order"]
        delta = json.loads(prompt_delta_for(fresh, []))["work_order"]
        operator = self.project.work_order_status()
        self.assertEqual(full, delta)
        self.assertEqual(full["status"], "unavailable")
        self.assertEqual(full["rev"], 256)
        self.assertNotEqual(full["opening_generation"], args["expected_generation"])
        self.assertEqual(operator["current_generation"], full["opening_generation"])
        self.assertEqual(operator["rev"], full["rev"])
        candidate = {"rev": 257, "generation": args["expected_generation"], "items": args["items"],
                     "rationale": args["rationale"], "actor": "worker"}
        self.assertEqual(full["recovery_candidate"], candidate)
        self.assertEqual(operator["recovery_candidate"], candidate)
        self.assertEqual((root / "current.json").read_bytes(), before)
        stale_generation = "00" * 32
        self.assertNotIn(stale_generation, (args["expected_generation"], full["opening_generation"]))
        for bad in (dict(args, rationale="different"), dict(args, expected_rev=255),
                    dict(args, expected_generation=stale_generation)):
            with self.assertRaises(Denied):
                fresh.handle("work_order", bad)
        with self.assertRaises(Denied):
            plan.write_plan(self.project, actor="other", opening_generation=full["opening_generation"], **args)
        with patch.object(plan, "_is_actionable_locked", return_value=False), self.assertRaises(Denied):
            fresh.handle("work_order", args)
        self.assertEqual((root / "current.json").read_bytes(), before)
        self.assertEqual(fresh.handle("work_order", args)["rev"], 257)
        self.assertEqual(json.loads(prompt_for(self.context("worker")))["work_order"]["status"], "review_due")
        self.assertEqual(len(list((root / "history").iterdir())), 256)

    def test_fresh_unit_checkpoint_only_can_write_new_plan(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        for after_prune in (False, True):
            with self.subTest(after_prune=after_prune):
                if after_prune:
                    import shutil
                    shutil.rmtree(self.project.root / "work-order")
                ctx, root, args = self._seed_rollover() if not after_prune else (ctx, root, args)
                # Reset the already removed store for the second boundary.
                if after_prune:
                    hist = root / "history"
                    hist.mkdir(parents=True)
                    for rev in range(1, 257):
                        record = {"schema": 1, "rev": rev, "generation": args["expected_generation"],
                                  "items": args["items"], "rationale": "review", "actor": "worker", "updated_at": now()}
                        write_json(hist / f"{rev}.json", record)
                    write_json(root / "current.json", record)
                hist = root / "history"
                if after_prune:
                    failure = patch.object(plan, "sync_dir", side_effect=OSError("after prune"))
                else:
                    original = type(hist).unlink
                    def fail(path, *a, **kw):
                        if path == hist / "1.json":
                            raise OSError("after checkpoint")
                        return original(path, *a, **kw)
                    failure = patch.object(type(hist), "unlink", fail)
                with failure, self.assertRaises(Denied):
                    ctx.handle("work_order", args)
                self.project.insights.submit(source="worker", title="Changed", body="body", base_snapshot=None)
                fresh = self.context("worker")
                full = json.loads(prompt_for(fresh))["work_order"]
                self.assertEqual(full, json.loads(prompt_delta_for(fresh, []))["work_order"])
                operator = self.project.work_order_status()
                self.assertEqual((full["status"], full["rev"]), ("unavailable", 256))
                self.assertNotIn("recovery_candidate", full)
                self.assertNotIn("recovery_candidate", operator)
                self.assertEqual(operator["current_generation"], full["opening_generation"])
                with self.assertRaises(Denied):
                    fresh.handle("work_order", args)
                new = dict(args, rationale="fresh plan", expected_generation=full["opening_generation"])
                self.assertEqual(fresh.handle("work_order", new)["rev"], 257)
                self.assertEqual(len(list(hist.iterdir())), 256)

    def test_corrupt_interrupted_store_has_no_recovery_projection(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for
        ctx, root, args = self._seed_rollover()
        with patch.object(plan, "sync_dir", side_effect=OSError("after prune")), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        (root / "checkpoint.json").write_text("{}")
        fresh = self.context("worker")
        for shown in (json.loads(prompt_for(fresh))["work_order"], self.project.work_order_status()):
            self.assertEqual(shown, {"status": "unavailable", "rev": None, "generation": None})
        with self.assertRaises(Denied):
            fresh.handle("work_order", dict(args, expected_generation=plan._frozen_generation(fresh)))

    def test_fresh_recovery_of_first_unpointed_record(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for
        item = self.project.work_items.create(idempotency_key="first-crash", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = {"items": [item["id"]], "rationale": "first", "expected_rev": 0,
                "expected_generation": plan._frozen_generation(ctx)}
        original = plan.write_json
        def fail_current(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("after append")
            return original(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_current), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        self.project.insights.submit(source="worker", title="New", body="body", base_snapshot=None)
        fresh = self.context("worker")
        shown = json.loads(prompt_for(fresh))["work_order"]
        operator = self.project.work_order_status()
        self.assertEqual((shown["status"], shown["rev"]), ("unavailable", 0))
        self.assertEqual(operator["rev"], 0)
        self.assertEqual(operator["recovery_candidate"], shown["recovery_candidate"])
        self.assertEqual(operator["current_generation"], shown["opening_generation"])
        self.assertEqual(fresh.handle("work_order", args)["rev"], 1)
    def test_fresh_replacement_recovers_nonactionable_candidate(self):
        # Experiment dcb6856: interrupt the append, then make the candidate item
        # non-actionable (blocked on a future deadline) before the next unit.
        # Exact retry must refuse actionability, but a fresh current-generation
        # plan at the unchanged committed rev replaces the candidate.
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        stale_a = self.project.work_items.create(idempotency_key="stale-a", title="A", actor="worker")["record"]["work_item"]
        fresh_b = self.project.work_items.create(idempotency_key="fresh-b", title="B", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        opening = plan._frozen_generation(ctx)
        args = {"items": [stale_a["id"]], "rationale": "candidate",
                "expected_rev": 0, "expected_generation": opening}
        original = plan.write_json
        def fail_current(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("interrupted append")
            return original(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_current), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        root = self.project.root / "work-order"
        hist = root / "history"
        self.assertTrue((hist / "1.json").exists())
        self.assertFalse((root / "current.json").exists())
        # Move the candidate item out of actionability in a fresh unit's past.
        self.project.work_items.transition(stale_a["id"], expected_rev=1, actor="worker",
                                           status="blocked",
                                           wait={"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"})
        fresh = self.context("worker")
        full = json.loads(prompt_for(fresh))["work_order"]
        delta = json.loads(prompt_delta_for(fresh, []))["work_order"]
        operator = self.project.work_order_status()
        self.assertEqual(full, delta)
        self.assertEqual(full["status"], "unavailable")
        self.assertEqual(full["rev"], 0)
        self.assertNotEqual(full["opening_generation"], opening)
        self.assertEqual(operator["current_generation"], full["opening_generation"])
        candidate = {"rev": 1, "generation": opening, "items": [stale_a["id"]],
                     "rationale": "candidate", "actor": "worker"}
        self.assertEqual(full["recovery_candidate"], candidate)
        self.assertEqual(operator["recovery_candidate"], candidate)
        # Exact retry is still recognized but refuses the non-actionable item.
        with self.assertRaises(Denied):
            fresh.handle("work_order", args)
        self.assertFalse((root / "current.json").exists())
        self.assertEqual(json.loads((hist / "1.json").read_text())["items"], [stale_a["id"]])
        # A stale generation that matches neither candidate nor opening is denied.
        stale_generation = "00" * 32
        self.assertNotIn(stale_generation, (opening, full["opening_generation"]))
        with self.assertRaises(Denied):
            fresh.handle("work_order", {"items": [fresh_b["id"]], "rationale": "fresh",
                                        "expected_rev": 0, "expected_generation": stale_generation})
        # Fresh opening-generation plan replaces the unpointed candidate.
        replacement = {"items": [fresh_b["id"]], "rationale": "fresh recovery",
                       "expected_rev": 0, "expected_generation": full["opening_generation"]}
        self.assertEqual(fresh.handle("work_order", replacement)["rev"], 1)
        stored = plan._read_stored(self.project.root)
        self.assertEqual(stored["status"], "current")
        self.assertEqual(stored["rev"], 1)
        self.assertNotIn("interrupted", stored)
        current = json.loads((root / "current.json").read_text())
        body = json.loads((hist / "1.json").read_text())
        self.assertEqual(body, current)
        self.assertEqual(body["rev"], 1)
        self.assertEqual(body["generation"], full["opening_generation"])
        self.assertEqual(body["items"], [fresh_b["id"]])
        self.assertEqual(body["rationale"], "fresh recovery")
        self.assertEqual(body["actor"], "worker")
        self.assertEqual(len(list(hist.iterdir())), 1)
        for path in list(hist.iterdir()) + [root / "current.json"]:
            self.assertLessEqual(path.stat().st_size, plan.MAX_WORK_ORDER_BYTES)
        self.assertEqual(self.project.work_order_status()["status"], "current")
        self.assertEqual(json.loads(prompt_for(self.context("worker")))["work_order"]["status"], "current")

    def test_fresh_candidate_replacement_at_rollover_boundary(self):
        from unittest.mock import patch
        ctx, root, args = self._seed_rollover()
        original = plan.write_json
        def fail_current(path, value, **kwargs):
            if path.name == "current.json":
                raise OSError("after rollover candidate append")
            return original(path, value, **kwargs)
        with patch.object(plan, "write_json", side_effect=fail_current), self.assertRaises(Denied):
            ctx.handle("work_order", args)
        hist = root / "history"
        self.assertFalse((hist / "1.json").exists())
        self.assertTrue((hist / "257.json").exists())
        alternative = self.project.work_items.create(
            idempotency_key="rollover-replacement", title="B", actor="worker")["record"]["work_item"]
        self.project.insights.submit(source="worker", title="Fresh review", body="new input", base_snapshot=None)
        fresh = self.context("worker")
        opening = plan._frozen_generation(fresh)
        request = {"items": [alternative["id"]], "rationale": "fresh rollover recovery",
                   "expected_rev": 256, "expected_generation": opening}
        self.assertEqual(fresh.handle("work_order", request)["rev"], 257)
        stored = plan._read_stored(self.project.root)
        self.assertEqual((stored["status"], stored["rev"]), ("current", 257))
        self.assertNotIn("interrupted", stored)
        self.assertEqual(len(list(hist.iterdir())), plan.MAX_WORK_ORDER_HISTORY)
        self.assertEqual({p.name for p in hist.iterdir()}, {f"{n}.json" for n in range(2, 258)})
        current = json.loads((root / "current.json").read_text())
        self.assertEqual(json.loads((hist / "257.json").read_text()), current)
        self.assertEqual(current["generation"], opening)
        self.assertEqual(current["items"], [alternative["id"]])
        self.assertEqual(current["rationale"], "fresh rollover recovery")
        self.assertEqual(self.project.work_order_status()["status"], "current")
