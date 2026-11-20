"""Prompt delivery is optional; existing state-read authority is not."""
import copy
import dataclasses
import json
from unittest.mock import patch

from support import Fixture, forget_ledger
from mizu import report, work_item_resolution
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.prompt_sections import WORK_CAPABILITIES
from mizu.protocol import DEFINITIONS, STATE_READ_CAPABILITIES, operation_allowed, tool_definitions
from mizu.unit_prompts import prompt_for, prompt_delta_for


class StateReadTests(Fixture):
    def reader(self, name="worker", *, capabilities=None):
        ctx = self.context(name)
        ctx.role = dataclasses.replace(
            ctx.role, context_exclude=("ready_work", "work_order", "report"),
            **({"capabilities": capabilities} if capabilities is not None else {}))
        return ctx

    def create(self, key, actor="worker"):
        return self.project.work_items.create(
            idempotency_key=key, title=key, actor=actor)["record"]["work_item"]["id"]

    def state_files(self):
        # Ignore runtime tool accounting and lock files, not product state.
        return {str(p.relative_to(self.project.root)): p.read_bytes()
                for directory in ("insights", "work-items", "work-order", "artifacts", "snapshots")
                for p in (self.project.root / directory).rglob("*") if p.is_file()}

    def test_modifier_grants_are_not_callable_tools(self):
        for ordinary, operations in (
                (("finish", "withdraw_insight"), ("finish", "withdraw_insight")),
                (("finish", "withdraw_insight", "report"),
                 ("finish", "withdraw_insight", "report", "report_read"))):
            caps = ordinary + ("withdraw_decided",)
            with self.subTest(capabilities=caps):
                expected = [{"name": "mizu_" + name, "operation": name,
                             "description": DEFINITIONS[name][0],
                             "inputSchema": DEFINITIONS[name][1]} for name in operations]
                self.assertEqual(tool_definitions(ordinary), expected)
                self.assertEqual(tool_definitions(caps), expected)
                self.assertFalse(operation_allowed("withdraw_decided", caps))
                self.assertFalse(operation_allowed("withdraw_decided", ordinary))
                for operation in operations:
                    self.assertTrue(operation_allowed(operation, caps))

    def test_exact_inherited_grants_surface_and_denial(self):
        from mizu.config import CAPABILITIES
        self.assertFalse(STATE_READ_CAPABILITIES.keys() & CAPABILITIES)
        for cap in (*WORK_CAPABILITIES, "report", "submit_insight", "decide", "finish"):
            ctx = self.reader(capabilities=(cap,))
            surface = {t["operation"] for t in tool_definitions(ctx.role.capabilities)}
            for operation, gates in STATE_READ_CAPABILITIES.items():
                with self.subTest(cap=cap, operation=operation):
                    self.assertEqual(operation in surface, cap in gates)
                    if cap in gates:
                        ctx.handle(operation, {})
                    else:
                        # Even explicitly including the prompt section grants nothing.
                        ctx.role = dataclasses.replace(ctx.role, context_exclude=(),
                                                       context_include=("ready_work", "work_order", "report"))
                        with self.assertRaises(Denied):
                            ctx.handle(operation, {})
                        with self.assertRaises(Denied):
                            ctx.dispatch(operation, {})

    def test_excluded_full_and_delta_still_allow_bounded_ready_read(self):
        ids = [self.create(str(n)) for n in range(3)]
        ctx = self.reader()
        for payload in (json.loads(prompt_for(ctx)), json.loads(prompt_delta_for(ctx))):
            for key in ("ready_work", "work_order", "previous_report"):
                self.assertNotIn(key, payload)
            self.assertIn("mizu_ready_work_read", payload["unit_tool_surface"]["tools"])
        before = self.state_files()
        signature = ctx.ready_work_signature
        inputs = copy.deepcopy(ctx._work_order_inputs)
        page = ctx.handle("ready_work_read", {"limit": 1})
        self.assertEqual((len(page["items"]), page["total"], page["overflow_count"], page["truncated"]),
                         (1, 3, 2, True))
        self.assertIn(page["items"][0]["id"], ids)
        self.assertTrue(page["coverage_complete"])
        self.assertLessEqual(len(canonical(page)), 1024 * 1024)
        self.assertEqual(ctx.ready_work_signature, signature)
        self.assertEqual(ctx._work_order_inputs, inputs)
        self.assertFalse(ctx.progress_tool)
        self.assertEqual(before, self.state_files())
        for args in ({"limit": 0}, {"limit": 101}, {"cursor": "x"}, {"role_name": "other"},
                     {"_delivered_wait_resolution_tokens": []}):
            with self.subTest(args=args), self.assertRaises(Denied):
                ctx.handle("ready_work_read", args)

    def test_ready_role_filter_and_wait_resolution_without_delivery_or_transition(self):
        from mizu.fs import now
        free = self.create("free")
        owned = self.create("owned")
        other = self.create("other")
        for item, actor in ((owned, "worker"), (other, "searcher")):
            self.project.work_items.transition(item, expected_rev=1, status="doing", actor=actor)
        self.project.work_items.transition(
            owned, expected_rev=2, status="blocked", actor="worker",
            wait={"kind": "deadline", "at": now()})
        ctx = self.reader()
        before = self.state_files()
        # Time wait is ready for reassessment, not implicitly completed/reopened.
        page = ctx.handle("ready_work_read", {})
        self.assertEqual({row["id"] for row in page["items"]}, {free, owned})
        blocked = next(row for row in page["items"] if row["id"] == owned)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["wait_satisfied"])
        self.assertEqual(before, self.state_files())
        # A reconciled object wait reopens for its holder only. Ordinary tool
        # reads retain the proof but never consume its opening-delivery receipt.
        target = self.create("target")
        reopened = self.create("reopened")
        self.project.work_items.transition(
            reopened, expected_rev=1, status="blocked", actor="worker",
            wait={"kind": "object_resolved", "subject": {"type": "work_item", "id": target}})
        self.project.work_items.transition(
            target, expected_rev=1, status="dropped", actor="worker", reason="resolved")
        work_item_resolution.reconcile(self.project, "worker", limit=20)
        before = self.state_files()
        stored = self.project.work_items.get(reopened)
        with patch.object(self.project.work_items, "page_ready",
                          wraps=self.project.work_items.page_ready) as read:
            page = ctx.handle("ready_work_read", {})
        read.assert_called_once_with(limit=25, role_name="worker")
        row = next(row for row in page["items"] if row["id"] == reopened)
        self.assertEqual((row["status"], row["rev"], row["holder"], row["wait_resolution"]),
                         ("open", stored["rev"], "worker", stored["wait_resolution"]))
        other_page = self.reader("searcher").handle("ready_work_read", {})
        self.assertNotIn(reopened, {row["id"] for row in other_page["items"]})
        self.assertEqual(before, self.state_files())
        self.assertFalse(ctx.progress_tool)

    def test_work_order_status_revision_and_frozen_generation_without_writes(self):
        item = self.create("planned")
        ctx = self.reader()
        prompt_for(ctx)
        absent = ctx.handle("work_order_read", {})
        self.assertEqual((absent["status"], absent["rev"]), ("absent", 0))
        generation = absent["opening_generation"]
        ctx.handle("work_order", {"items": [item], "rationale": "one plan",
                                  "expected_rev": 0, "expected_generation": generation})
        fresh = self.reader()
        prompt_for(fresh)
        before = self.state_files()
        current = fresh.handle("work_order_read", {})
        self.assertEqual((current["status"], current["rev"], current["items"]),
                         ("current", 1, [item]))
        self.assertEqual(before, self.state_files())
        self.assertFalse(fresh.progress_tool)
        self.create("arrival")
        # Reads keep this unit's reviewed generation, not newly arrived input.
        self.assertEqual(fresh.handle("work_order_read", {}), current)
        later = self.reader()
        prompt_for(later)
        due = later.handle("work_order_read", {})
        self.assertEqual((due["status"], due["rev"]), ("review_due", 1))
        pointer = self.project.root / "work-order" / "current.json"
        pointer.write_text("{bad")
        before = self.state_files()
        unavailable = later.handle("work_order_read", {})
        self.assertEqual(unavailable, {"status": "unavailable", "rev": None, "generation": None})
        self.assertEqual(before, self.state_files())
        pointer.unlink()
        later._work_order_generation = None
        self.assertEqual(later.handle("work_order_read", {})["status"], "unavailable")
        for args in ({"expected_rev": 0}, {"items": []}):
            with self.assertRaises(Denied):
                fresh.handle("work_order_read", args)

    def test_report_preview_absent_published_corrupt_and_unsafe_are_read_only(self):
        ctx = self.reader("reporter")
        self.assertIsNone(ctx.handle("report_read", {})["previous_report"])
        published = report.publish(self.project, self.project.snapshots.get(),
                                   {"title": "edition", "body": "界" * 10000})
        self.assertNotIn("previous_report", json.loads(prompt_for(ctx)))
        before = self.state_files()
        out = ctx.handle("report_read", {})["previous_report"]
        self.assertEqual(out["artifact"], published["artifact"])
        self.assertTrue(out["body_truncated"])
        self.assertLessEqual(len(out["body"].encode()), report.PREVIOUS_REPORT_BYTES)
        self.assertEqual(before, self.state_files())
        self.assertFalse(ctx.progress_tool)
        self.assertIsNone(ctx.commentary)
        pointer = self.project.root / "artifacts" / "latest.json"
        pointer.write_text("{bad")
        self.assertIsNone(ctx.handle("report_read", {})["previous_report"])
        pointer.write_text(json.dumps({"artifact": "../escape"}))
        self.assertIsNone(ctx.handle("report_read", {})["previous_report"])
        pointer.write_text(json.dumps({"artifact": published["artifact"]}))
        body = self.project.root / "artifacts" / published["artifact"] / "artifact.md"
        body.unlink()
        body.symlink_to(self.project.workspace / "README.md")
        self.assertIsNone(ctx.handle("report_read", {})["previous_report"])
        with self.assertRaises(Denied):
            ctx.handle("report_read", {"body": "do not stage"})

    def test_ready_failure_and_incomplete_coverage_never_claim_exhaustive_empty(self):
        ctx = self.reader()
        with patch.object(self.project.work_items, "page_ready", side_effect=Denied("corrupt ledger")):
            with self.assertRaisesRegex(Denied, "corrupt ledger"):
                ctx.handle("ready_work_read", {})
        raw = self.project.work_items.page_ready(role_name="worker")
        raw.update(coverage_complete=False, unknown_blocked=1)
        with patch.object(self.project.work_items, "page_ready", return_value=raw):
            page = ctx.handle("ready_work_read", {})
        self.assertEqual(page["total"], 0)
        self.assertFalse(page["coverage_complete"])
        self.assertEqual(page["unknown_blocked"], 1)
        raw["unknown_blocked"] = -1
        with patch.object(self.project.work_items, "page_ready", return_value=raw):
            with self.assertRaises(Denied):
                ctx.handle("ready_work_read", {})

    def test_corrupt_ready_store_fails_closed_without_repair(self):
        item = self.create("corrupt")
        path = self.project.work_items.root / "work-items" / item / "create.json"
        path.write_text("{bad")
        forget_ledger(self.project.root)  # Validate immutable source during index rebuild.
        ctx = self.reader()
        before = self.state_files()
        with self.assertRaises(Denied):
            ctx.handle("ready_work_read", {})
        self.assertEqual(before, self.state_files())
        self.assertFalse(ctx.progress_tool)
