"""Unit prompt projections live in mizu.unit_prompts; runtime re-exports them."""
import json
import unittest
from unittest.mock import patch

from support import Fixture


MOVED = ("UNIT_BEGINS_STATEMENT", "MAX_READY_WORK_AGE_SECONDS",
         "_prompt_clock", "_prompt_research_state", "_decision_task",
         "_open_backlog", "_prompt_tool_surface", "_prompt_run_identity",
         "_prompt_capture_limit", "_ready_work_age", "_ready_work_projection",
         "prompt_for", "prompt_delta_for")
STAYS_IN_RUNTIME = ("_reject_constant", "_has_ready_work", "_ready_work_changed",
                    "session_prompt", "should_run")


class UnitPromptSeamTests(Fixture):
    def test_runtime_reexports_are_the_same_objects(self):
        import mizu.runtime as runtime
        import mizu.unit_prompts as unit_prompts
        for name in MOVED:
            self.assertIs(getattr(runtime, name), getattr(unit_prompts, name),
                          name)

    def test_canonical_home_is_unit_prompts(self):
        import mizu.unit_prompts as unit_prompts
        for name in MOVED:
            value = getattr(unit_prompts, name)
            if callable(value):
                self.assertEqual(value.__module__, "mizu.unit_prompts", name)

    def test_scheduling_and_session_lifecycle_stay_in_runtime(self):
        import mizu.runtime as runtime
        import mizu.unit_prompts as unit_prompts
        for name in STAYS_IN_RUNTIME:
            self.assertTrue(hasattr(runtime, name), name)
            self.assertFalse(hasattr(unit_prompts, name), name)

    def test_both_import_paths_build_the_same_full_and_delta(self):
        import mizu.runtime as runtime
        import mizu.unit_prompts as unit_prompts
        for role in ("worker", "reporter", "consult"):
            ctx = self.context(role)
            clock = unit_prompts._prompt_clock(ctx)
            with patch.object(unit_prompts, "_prompt_clock", return_value=clock):
                self.assertEqual(runtime.prompt_for(ctx), unit_prompts.prompt_for(ctx))
            ctx = self.context(role)
            clock = unit_prompts._prompt_clock(ctx)
            with patch.object(unit_prompts, "_prompt_clock", return_value=clock):
                self.assertEqual(runtime.prompt_delta_for(ctx, []),
                                 unit_prompts.prompt_delta_for(ctx, []))

    def test_full_delta_and_finish_tool_explain_writer_continuation(self):
        from mizu.protocol import tool_definitions
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        ctx = self.context("worker")
        finish = tool_definitions(("finish",))[0]
        surfaces = {"full": json.loads(prompt_for(ctx))["unit"],
                    "delta": json.loads(prompt_delta_for(ctx, []))["unit"],
                    "finish": finish["description"]}
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                for phrase in ("outcome=continue", "host-sealed bounded outcome",
                               "exactly one standalone `Next: <actionable step>` line in state",
                               "new actionable step", "outcome=wait for external results",
                               "work that is not yet actionable",
                               "Next is not a wake/wait mechanism",
                               "An admitted continuation opens with that step as its task"):
                    self.assertIn(phrase, text)
        # bounded is a host-sealed outcome, not a new finish-tool argument.
        self.assertEqual(finish["inputSchema"]["properties"]["outcome"]["enum"],
                         ["continue", "wait", "blocked", "done"])

    def test_delta_clock_boundary_changes_only_the_clock_projection(self):
        import mizu.unit_prompts as unit_prompts
        ctx = self.context("worker")
        first_clock = {"now": "2026-10-25T05:00:00+00:00",
                       "timezone": str(ctx.config.timezone)}
        second_clock = {"now": "2026-10-25T05:00:01+00:00",
                        "timezone": str(ctx.config.timezone)}
        with patch.object(unit_prompts, "_prompt_clock",
                          side_effect=[first_clock, second_clock]):
            first = json.loads(unit_prompts.prompt_delta_for(ctx, []))
            second = json.loads(unit_prompts.prompt_delta_for(ctx, []))
        self.assertEqual(first.pop("now"), first_clock["now"])
        self.assertEqual(second.pop("now"), second_clock["now"])
        self.assertEqual(first, second)

    def test_ready_helper_uses_captured_page_without_another_scan(self):
        import mizu.unit_prompts as unit_prompts
        ctx = self.context("worker")
        page = self.project.work_items.page_ready(
            limit=ctx.config.limits.work_items_prompt, role_name=ctx.role.name)
        with patch.object(self.project.work_items, "page_ready", side_effect=AssertionError("rescan")):
            ready = unit_prompts._ready_work_projection(page, context=ctx)
        self.assertEqual(ctx.ready_work_signature, page["signature"])
        self.assertEqual(ready["total"], page["total"])
        self.assertEqual(list(ready), ["items", "total", "overflow_count", "truncated",
                                      "unseeded_decisions", "coverage_complete"])

    def test_moved_builders_still_set_the_ready_work_signature(self):
        import mizu.unit_prompts as unit_prompts
        ctx = self.context("worker")
        self.assertFalse(hasattr(ctx, "ready_work_signature"))
        unit_prompts.prompt_for(ctx)
        first = ctx.ready_work_signature
        self.assertTrue(first)
        unit_prompts.prompt_delta_for(ctx, [])
        self.assertEqual(ctx.ready_work_signature, first)


if __name__ == "__main__":
    unittest.main()
