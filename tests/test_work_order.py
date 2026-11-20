"""Writer explicit work order: bounded CAS record with review-gated display."""
import copy
import dataclasses
import json

from support import Fixture, without_blocked_age
from mizu import work_order as plan
from mizu.errors import Denied
from mizu.fs import digest, now, write_json
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
            summary="park", wake_at=None,
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
        self.project.work_items.transition(a["id"], expected_rev=1, status="doing", actor="worker")
        ctx = self.context("worker")
        full = json.loads(prompt_for(ctx))
        delta = json.loads(prompt_delta_for(ctx))
        self.assertEqual(full["work_order"]["status"], "absent")
        self.assertEqual(delta["work_order"]["status"], "absent")
        self.assertEqual(self.project.work_order_status()["status"], "absent")
        frozen = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [a["id"]], "rationale": "do it",
                                  "expected_rev": 0, "expected_generation": frozen})
        ctx2 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx2))["work_order"]["status"], "current")
        self.assertEqual(json.loads(prompt_delta_for(ctx2))["work_order"]["status"], "current")
        self.assertEqual(self.project.work_order_status()["status"], "current")
        # New actionable arrival moves both prompts and status to review_due.
        self.project.work_items.create(idempotency_key="b1", title="B", actor="worker")
        ctx3 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx3))["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_delta_for(ctx3))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")
        # Corrupt store fails closed everywhere, never empty/current.
        (self.project.root / "work-order" / "current.json").write_text("{corrupt")
        ctx4 = self.context("worker")
        self.assertEqual(json.loads(prompt_for(ctx4))["work_order"]["status"], "unavailable")
        self.assertEqual(json.loads(prompt_delta_for(ctx4))["work_order"]["status"], "unavailable")
        self.assertEqual(self.project.work_order_status()["status"], "unavailable")

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
        frozen_delta = json.loads(prompt_delta_for(ctx))
        self.assertEqual(frozen_delta["work_order"]["status"], "current")
        self.assertEqual(frozen_delta["open_work_items"], original_prompt["open_work_items"])
        self.assertEqual(frozen_delta["ready_work"], original_prompt["ready_work"])
        self.assertEqual(frozen_delta["work_order"]["opening_generation"], shown["opening_generation"])
        fresh = self.context("worker")
        self.assertEqual(json.loads(prompt_for(fresh))["work_order"]["status"], "review_due")
        self.assertEqual(json.loads(prompt_delta_for(fresh))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

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
        self.assertEqual(json.loads(prompt_delta_for(fresh))["work_order"]["status"], "review_due")
        self.assertEqual(self.project.work_order_status()["status"], "review_due")

    def test_displayed_absent_generation_creates_first_plan_for_both_prompts(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        a = self.project.work_items.create(idempotency_key="first", title="A", actor="worker")["record"]["work_item"]
        for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx)):
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
        for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx)):
            with self.subTest(builder=build), patch.object(plan, "generation_from_captured", side_effect=Denied("bad input")):
                shown = json.loads(build(self.context("worker")))["work_order"]
                self.assertEqual(shown["status"], "unavailable")
                self.assertNotIn("opening_generation", shown)

    def test_cas_rejects_stale_rev_and_generation_and_atomic_current(self):
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
        # Correct CAS replaces the current record atomically.
        out2 = ctx2.handle("work_order", {"items": [b["id"]], "rationale": "second",
                                          "expected_rev": 1, "expected_generation": fresh})
        self.assertEqual(out2["rev"], 2)
        root = self.project.root / "work-order"
        self.assertEqual(list(root.iterdir()), [root / "current.json"])
        self.assertEqual(json.loads((root / "current.json").read_bytes())["items"], [b["id"]])

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

    def test_noncanonical_duplicate_key_record_fails_closed(self):
        item = self.project.work_items.create(idempotency_key="duplicate-key", title="Duplicate", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "audit",
                                  "expected_rev": 0, "expected_generation": plan._frozen_generation(ctx)})
        path = self.project.root / "work-order" / "current.json"
        raw = path.read_bytes()
        self.assertIn(b'"schema":2', raw)
        path.write_bytes(raw.replace(b'"schema":2', b'"schema":0,"schema":2', 1))
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")

    def test_blocked_age_is_visible_but_does_not_churn_work_generation(self):
        from mizu.work_order import _stable_open_for_hash, _stable_ready_for_hash

        ready = {"items": [{"id": "wi_" + "1" * 32,
                            "wait_liveness": {"state": "pending", "reason": "condition_pending",
                                              "blocked_since": "2030-01-01T00:00:00+00:00",
                                              "blocked_age_seconds": 5}}],
                 "total": 1, "overflow_count": 0, "truncated": False,
                 "unseeded_decisions": 0, "coverage_complete": True, "unknown_blocked": 0}
        aged_ready = copy.deepcopy(ready)
        aged_ready["items"][0]["wait_liveness"]["blocked_age_seconds"] = 10**9
        self.assertEqual(_stable_ready_for_hash(ready), _stable_ready_for_hash(aged_ready))
        due_ready = copy.deepcopy(aged_ready)
        due_ready["items"][0]["wait_liveness"].update(
            state="due", reason="condition_met")
        self.assertNotEqual(_stable_ready_for_hash(ready), _stable_ready_for_hash(due_ready))

        opened = {"items": [{"id": "wi_" + "1" * 32, "status": "blocked",
                            "blocked_since": "2030-01-01T00:00:00+00:00",
                            "blocked_age_seconds": 5}]}
        aged_opened = copy.deepcopy(opened)
        aged_opened["items"][0]["blocked_age_seconds"] = 10**9
        self.assertEqual(_stable_open_for_hash(opened), _stable_open_for_hash(aged_opened))

    def test_ancient_pending_wait_keeps_readiness_and_diagnostic_age(self):
        from test_blocked_wait_age import clock

        store = self.project.work_items
        with clock(0):
            seed = store.create(idempotency_key="waiting", title="Waiting", actor="worker")["record"]["work_item"]
            item = store.transition(seed["id"], expected_rev=1, actor="worker", status="blocked",
                                    wait={"kind": "code_change"})
            context = self.context("worker")
            opening = plan._capture_opening(context)
            generation = plan._frozen_generation(context)
        with clock(10**9):
            context = self.context("worker")
            aged = plan._capture_opening(context)
            self.assertEqual(without_blocked_age(aged["ready_work"]),
                             without_blocked_age(opening["ready_work"]))
            self.assertEqual(aged["ready_work"]["total"], 1)
            ready = aged["ready_work"]["items"][0]
            self.assertEqual(ready["id"], item["id"])
            self.assertEqual(ready["status"], "open")
            for absent in ("wait", "wait_liveness", "wait_satisfied"):
                self.assertNotIn(absent, ready)
            self.assertEqual(aged["open_work_items"]["items"][0]["blocked_age_seconds"], 10**9)
            self.assertEqual(plan._frozen_generation(context), generation)
            self.assertEqual(store.get(item["id"]), item)

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
        delta = json.loads(prompt_delta_for(ctx))
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
            self.assertEqual(json.loads(prompt_delta_for(same))["work_order"]["opening_generation"], generation)

    def test_roles_without_grant_capture_prompts_without_hashing(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        for role in ("reporter", "consult"):
            for build in (lambda ctx: prompt_for(ctx), lambda ctx: prompt_delta_for(ctx)):
                with self.subTest(role=role, builder=build), patch.object(
                        plan, "generation_from_captured", side_effect=AssertionError("unexpected hash")) as hashed:
                    shown = json.loads(build(self.context(role)))
                    self.assertIsNone(shown["work_order"])
                    self.assertIn("ready_work", shown)
                    hashed.assert_not_called()


    def test_configured_ci_aggregate_moves_generation_independently_of_grants_and_sections(self):
        from mizu.ci_observation import wake_ci_failures
        from mizu.ci_wake import _ci_due_for, write_ci_wake
        from mizu.unit_prompts import prompt_for
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "ci_branch": "main", "ci_wake_extra_branches": ("release",)})
        self.project.config = self.config
        item = self.project.work_items.create(
            idempotency_key="ci-generation", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        empty = plan._frozen_generation(ctx)
        ctx.handle("work_order", {"items": [item["id"]], "rationale": "solo",
                                  "expected_rev": 0, "expected_generation": empty})
        state = {"failures": {name: [{"sha": "a" * 40, "check": "core"}]
                              for name in ("main", "release", "off-wake")},
                 "ci_check_summaries": {name: [{"sha": "a" * 40,
                     "non_passing_checks": 1, "may_be_incomplete": False}]
                     for name in ("main", "release", "off-wake")}}
        cache = self.project.root / "vcs-poll.json"
        write_json(cache, state)

        def generations():
            values = []
            for vcs_read in (False, True):
                for include_ci in (False, True):
                    fresh = self.context("worker")
                    caps = tuple(c for c in fresh.role.capabilities if c != "vcs_read")
                    fresh.role = dataclasses.replace(fresh.role,
                        capabilities=caps + (("vcs_read",) if vcs_read else ()),
                        context_include=("ci",) if include_ci else (),
                        context_exclude=() if include_ci else ("ci",))
                    values.append(plan._frozen_generation(fresh))
                    self.assertEqual(json.loads(prompt_for(fresh))["work_order"]["status"], "review_due")
            self.assertEqual(len(set(values)), 1, "grants and sections must not change generation")
            return values[0]

        current = generations()
        self.assertNotEqual(current, empty)
        # A delivery receipt changes neither an existing unit nor the next one.
        frozen_ctx = self.context("worker")
        self.assertEqual(plan._frozen_generation(frozen_ctx), current)
        due = wake_ci_failures(self.project, self.config)
        self.assertEqual({row["branch"] for row in due}, {"main", "release"})
        self.assertEqual(_ci_due_for(self.project, self.config, "worker", "head", 1000), due)
        write_ci_wake(self.project, "worker", ("main", "release"), due, "head", 1000)
        self.assertEqual(_ci_due_for(self.project, self.config, "worker", "head", 1001), [])
        self.assertEqual(plan._frozen_generation(frozen_ctx), current)
        self.assertEqual(generations(), current)
        # Only the aggregate changes, not the retained check identity.
        state["ci_check_summaries"]["off-wake"][0]["non_passing_checks"] = 2
        write_json(cache, state)
        self.assertEqual(generations(), current)
        for subject in ("main", "release"):
            state["ci_check_summaries"][subject][0]["non_passing_checks"] = 2
            write_json(cache, state)
            moved = generations()
            self.assertNotEqual(moved, current, subject)
            current = moved
        # Captured units remain frozen despite later real aggregate changes.
        self.assertNotEqual(current, plan._frozen_generation(frozen_ctx))

    def test_unreadable_ci_cache_does_not_create_false_review_due(self):
        from unittest.mock import patch
        from mizu.unit_prompts import prompt_for
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, "ci_branch": "main"})
        self.project.config = self.config
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
        with patch("mizu.ci_observation.read_json", side_effect=OSError("unreadable cache")) as read:
            status = self.project.work_order_status()
            self.assertTrue(read.called, "configured wake subjects must attempt the unreadable cache")
            self.assertEqual(status["status"], "current")
            self.assertEqual(status["generation"], shown["opening_generation"])
            self.assertEqual(plan._frozen_generation(self.context("worker")), shown["opening_generation"])
            # Cache unreadability must not mask a real inventory change.
            self.project.work_items.create(idempotency_key="real-change", title="B", actor="worker")
            changed = self.project.work_order_status()
            self.assertEqual(changed["status"], "review_due")
            self.assertNotEqual(changed["current_generation"], shown["opening_generation"])
            self.assertEqual(plan._frozen_generation(self.context("worker")), changed["current_generation"])

    def test_protocol_accepts_absolute_revision(self):
        from mizu.protocol import DEFINITIONS, validate
        validate({"items": ["wi_" + "0" * 32], "rationale": "reviewed",
                  "expected_rev": 257, "expected_generation": "0" * 64},
                 DEFINITIONS["work_order"][1])

    def test_nonempty_order_and_rationale_minima(self):
        item = self.project.work_items.create(
            idempotency_key="minimum-order", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        generation = plan._frozen_generation(ctx)
        for args in (
                {"items": [], "rationale": "x", "expected_rev": 0,
                 "expected_generation": generation},
                {"items": [item["id"]], "rationale": "", "expected_rev": 0,
                 "expected_generation": generation}):
            with self.subTest(args=args), self.assertRaises(Denied):
                ctx.handle("work_order", args)
            self.assertEqual(plan._read_stored(self.project.root)["status"], "absent")

        result = ctx.handle("work_order", {
            "items": [item["id"]], "rationale": "x", "expected_rev": 0,
            "expected_generation": generation})
        record = plan._read_stored(self.project.root)["record"]
        self.assertEqual(result["rev"], 1)
        self.assertEqual(record["items"], [item["id"]])
        self.assertEqual(record["rationale"], "x")

    def _order_args(self, ctx, items, rev=0, rationale="reviewed"):
        return {"items": items, "rationale": rationale, "expected_rev": rev,
                "expected_generation": plan._frozen_generation(ctx)}

    def test_atomic_replacement_failure_leaves_absent_old_or_new_complete_record(self):
        from unittest.mock import patch
        from mizu import fs
        item = self.project.work_items.create(idempotency_key="atomic", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [item["id"]])
        path = self.project.root / "work-order" / "current.json"
        # Failure while syncing the temporary body cannot publish partial data.
        with patch.object(fs.os, "fsync", side_effect=OSError("before replacement")):
            with self.assertRaises(Denied):
                ctx.handle("work_order", args)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "absent")
        self.assertFalse(path.exists())
        self.assertFalse(list(ctx.run_dir.glob("work-order-*.json")))
        ctx.handle("work_order", args)
        old = path.read_bytes()
        args = dict(args, expected_rev=1, rationale="second")
        with patch.object(fs.os, "fsync", side_effect=OSError("before replacement")):
            with self.assertRaises(Denied):
                ctx.handle("work_order", args)
        self.assertEqual(path.read_bytes(), old)
        # Simulate interruption immediately before the atomic replacement.
        replace = "_replace_windows" if fs._platform.IS_WINDOWS else "os.replace"
        with patch("mizu.fs." + replace, side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ctx.handle("work_order", args)
        self.assertEqual(path.read_bytes(), old)
        # A failure/interruption after replacement leaves a complete new head,
        # not an unpointed candidate. No automatic retry or rev reuse is allowed.
        for rev, failure in ((1, OSError("after replacement")), (2, KeyboardInterrupt())):
            args = dict(args, expected_rev=rev)
            with patch.object(fs, "sync_dir", side_effect=failure) as sync:
                with self.assertRaises(Denied if isinstance(failure, OSError) else KeyboardInterrupt):
                    ctx.handle("work_order", args)
            self.assertEqual(sync.call_count, 1)
            stored = plan._read_stored(self.project.root)
            self.assertEqual(stored["status"], "current")
            self.assertEqual(stored["record"]["rev"], rev + 1)
            self.assertEqual(stored["record"]["rationale"], "second")
            self.assertEqual(stored["record"]["run"], ctx.run_dir.name)
            with self.assertRaisesRegex(Denied, "Stale work order"):
                ctx.handle("work_order", args)
        self.assertEqual(list(path.parent.iterdir()), [path])
        projection = plan.projection_for(self.context("worker"))
        self.assertEqual(projection["status"], "current")
        self.assertEqual(set(projection), {"status", "rev", "generation", "items",
                                         "rationale", "actor", "updated_at", "opening_generation"})

    def test_legacy_files_are_inert_and_unchanged_with_or_without_current(self):
        root = self.project.root / "work-order"
        hist = root / "history"
        hist.mkdir(parents=True)
        files = {hist / "1.json": b"not json", hist / "999.json": b"unpointed",
                 root / "checkpoint.json": b"corrupt checkpoint",
                 root / ".new-orphan": b"partial temporary body"}
        for path, raw in files.items():
            path.write_bytes(raw)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "absent")
        item = self.project.work_items.create(idempotency_key="inert", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        ctx.handle("work_order", self._order_args(ctx, [item["id"]]))
        self.assertEqual(plan._read_stored(self.project.root)["status"], "current")
        self.assertEqual(self.project.work_order_status()["status"], "current")
        for path, raw in files.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(set(hist.iterdir()), {hist / "1.json", hist / "999.json"})

    def test_legacy_schema_one_current_is_authoritative_and_upgrades_on_write(self):
        item = self.project.work_items.create(idempotency_key="legacy", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        generation = plan._frozen_generation(ctx)
        legacy = {"schema": 1, "rev": 257, "generation": generation,
                  "items": [item["id"]], "rationale": "legacy", "actor": "worker",
                  "updated_at": "2026-01-01T00:00:00+00:00"}
        path = self.project.root / "work-order" / "current.json"
        write_json(path, legacy)
        original = path.read_bytes()
        self.assertEqual(plan._read_stored(self.project.root)["record"], legacy)
        self.assertEqual(plan.projection_for(ctx)["status"], "current")
        self.assertEqual(self.project.work_order_status()["status"], "current")
        self.assertEqual(path.read_bytes(), original)
        out = ctx.handle("work_order", self._order_args(ctx, [item["id"]], rev=257))
        self.assertEqual(out["rev"], 258)
        record = plan._read_stored(self.project.root)["record"]
        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["run"], ctx.run_dir.name)
        self.assertFalse((path.parent / "history").exists())
        self.assertFalse((path.parent / "checkpoint.json").exists())

    def test_full_run_receipt_preserves_order_actor_time_and_runtime_identity(self):
        from unittest.mock import patch
        a = self.project.work_items.create(idempotency_key="receipt-a", title="A", actor="worker")["record"]["work_item"]
        b = self.project.work_items.create(idempotency_key="receipt-b", title="B", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [b["id"], a["id"]], rationale="B then A")
        timestamp = "2026-01-01T00:00:00+00:00"
        with patch.object(plan, "now", return_value=timestamp):
            out = ctx.handle("work_order", args)
        self.assertEqual(out["rev"], 1)
        self.assertEqual(out["generation"], args["expected_generation"])
        receipt_path = ctx.run_dir / out["evidence"]
        receipt = receipt_path.read_bytes()
        self.assertEqual(out["evidence"], f"work-order-{digest(receipt)}.json")
        self.assertEqual(receipt, (self.project.root / "work-order" / "current.json").read_bytes())
        first_record = json.loads(receipt)
        self.assertEqual(first_record, {"schema": 2, "rev": 1,
            "generation": args["expected_generation"], "items": [b["id"], a["id"]],
            "rationale": "B then A", "actor": "worker", "updated_at": timestamp,
            "run": ctx.run_dir.name})

        # A second successful write in the same run gets a distinct receipt;
        # the first result continues to name its unchanged revision.
        second = ctx.handle("work_order", self._order_args(
            ctx, [a["id"]], rev=1, rationale="A only"))
        second_path = ctx.run_dir / second["evidence"]
        second_receipt = second_path.read_bytes()
        self.assertEqual(second["rev"], 2)
        self.assertNotEqual(out["evidence"], second["evidence"])
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertEqual(second_receipt, (self.project.root / "work-order" / "current.json").read_bytes())
        self.assertEqual(json.loads(second_receipt)["rationale"], "A only")
        self.assertEqual(second["evidence"], f"work-order-{digest(second_receipt)}.json")

        # Later runs retain their own receipt without mutating this run's.
        ctx2 = self.context("worker")
        third = ctx2.handle("work_order", self._order_args(ctx2, [b["id"]], rev=2))
        third_receipt = (ctx2.run_dir / third["evidence"]).read_bytes()
        self.assertEqual(json.loads(third_receipt)["run"], ctx2.run_dir.name)
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertEqual(second_path.read_bytes(), second_receipt)
        for run in (None, "", "../run", "f" * 31, "G" * 32, "f" * 33):
            with self.subTest(run=run), self.assertRaisesRegex(Denied, "Invalid work-order run"):
                plan.write_plan(self.project, actor="worker", run=run,
                                opening_generation=args["expected_generation"], **args)
        # Neither identity is part of the caller's protocol.
        for field in ("run", "actor"):
            with self.subTest(field=field), self.assertRaises(Denied):
                ctx2.handle("work_order", dict(args, **{field: "f" * 32}))

    def test_receipt_failure_reports_error_after_current_commit(self):
        from unittest.mock import patch
        item = self.project.work_items.create(idempotency_key="receipt-failure", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [item["id"]], rationale="committed before receipt")
        with patch("mizu.work_order_ops.write_json", side_effect=OSError("receipt write failed")):
            with self.assertRaises(OSError):
                ctx.handle("work_order", args)
        stored = plan._read_stored(self.project.root)
        self.assertEqual(stored["status"], "current")
        self.assertEqual(stored["record"]["rev"], 1)
        self.assertEqual(stored["record"]["run"], ctx.run_dir.name)
        self.assertEqual(stored["record"]["rationale"], "committed before receipt")
        self.assertFalse(list(ctx.run_dir.glob("work-order-*.json")))

    def test_malformed_current_fails_closed_without_repair(self):
        from mizu.fs import canonical
        item = self.project.work_items.create(idempotency_key="malformed", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [item["id"]])
        ctx.handle("work_order", args)
        path = self.project.root / "work-order" / "current.json"
        original = path.read_bytes()
        valid = json.loads(original)
        malformed = [b"{broken", b"[]\n", original.replace(b"\n", b"\r\n"),
                     b" " * (plan.MAX_WORK_ORDER_BYTES + 1)]
        for field, value in (("schema", True), ("schema", 3), ("rev", 0), ("rev", True),
                             ("generation", "bad"), ("items", [item["id"], item["id"]]),
                             ("rationale", " "), ("actor", "../worker"),
                             ("updated_at", "bad"), ("updated_at", "2026-01-01"),
                             ("run", "../run"), ("extra", True)):
            malformed.append(canonical(dict(valid, **{field: value})))
        missing_run = dict(valid)
        del missing_run["run"]
        malformed.append(canonical(missing_run))
        for raw in malformed:
            with self.subTest(raw=raw[:80]):
                path.write_bytes(raw)
                self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
                self.assertEqual(plan.projection_for(ctx), {"status": "unavailable", "rev": None, "generation": None})
                self.assertEqual(self.project.work_order_status()["status"], "unavailable")
                with self.assertRaisesRegex(Denied, "Work order is unavailable"):
                    ctx.handle("work_order", dict(args, expected_rev=1))
                self.assertEqual(path.read_bytes(), raw)

    def test_unsafe_current_fails_closed(self):
        item = self.project.work_items.create(idempotency_key="unsafe", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [item["id"]])
        out = ctx.handle("work_order", args)
        path = self.project.root / "work-order" / "current.json"
        target = ctx.run_dir / out["evidence"]
        path.unlink()
        path.mkdir()
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        with self.assertRaises(Denied):
            ctx.handle("work_order", dict(args, expected_rev=1))
        path.rmdir()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.assertEqual(plan._read_stored(self.project.root)["status"], "unavailable")
        with self.assertRaises(Denied):
            ctx.handle("work_order", dict(args, expected_rev=1))
        self.assertTrue(path.is_symlink())

    def test_live_actionability_is_rechecked_after_opening(self):
        item = self.project.work_items.create(idempotency_key="live", title="A", actor="worker")["record"]["work_item"]
        ctx = self.context("worker")
        args = self._order_args(ctx, [item["id"]])
        self.project.work_items.transition(item["id"], expected_rev=1, actor="worker", status="doing")
        ctx.handle("work_order", args)
        self.project.work_items.transition(item["id"], expected_rev=2, actor="worker", status="dropped", reason="no longer actionable")
        with self.assertRaisesRegex(Denied, "not actionable"):
            ctx.handle("work_order", dict(args, expected_rev=1))
        self.assertEqual(plan._read_stored(self.project.root)["rev"], 1)

    def test_record_byte_cap_stays_8192(self):
        from unittest.mock import patch
        from mizu.errors import LimitExceeded
        self.assertEqual(plan.MAX_WORK_ORDER_BYTES, 8192)
        ctx = self.context("worker")
        args = self._order_args(ctx, [f"wi_{n:032x}" for n in range(100)], rationale="\x01" * 1024)
        with patch.object(plan, "_is_actionable_locked", return_value=True):
            with self.assertRaises(LimitExceeded):
                plan.write_plan(self.project, actor="worker", run=ctx.run_dir.name,
                                opening_generation=args["expected_generation"], **args)
        self.assertEqual(plan._read_stored(self.project.root)["status"], "absent")
