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
                               "supply `next_step`",
                               "new proposed follow-up", "next_step alone does not admit",
                               "parent snapshot", "role-matching validated progress record",
                               "at least one selected true",
                               "`tool`, `code`, `decision`, or `insight` signal",
                               "continuation shows that step in the published snapshot as the previous unit's",
                               "a wait or blocked finish is accepted and the host",
                               "re-admits from that work",
                               "Unknown or truncated evidence refuses finish"):
                    self.assertIn(phrase, text)
                # Product strings describe the interface; behaviour stays in
                # configurable role policy, never in an unconfigurable prompt.
                for behaviour in ("Use outcome=wait for external results",
                                  "work not yet actionable", "Finish each ready item",
                                  "held doing item is still ready",
                                  "do not rely on next_step as a wake signal",
                                  "correct next_step and retry finish",
                                  "Call after all other tools",
                                  "when no independent work remains"):
                    self.assertNotIn(behaviour, text)
        schema = finish["inputSchema"]
        self.assertEqual(schema["properties"]["next_step"],
                         {"type": "string", "minLength": 1, "maxLength": 4096})
        self.assertNotIn("next_step", schema["required"])
        for text in surfaces.values():
            self.assertNotIn("Next: <", text)
            self.assertIn("differ from the typed parent step", text)
        # bounded is a host-sealed outcome, not a new finish-tool argument.
        self.assertEqual(finish["inputSchema"]["properties"]["outcome"]["enum"],
                         ["continue", "wait", "blocked", "done"])

    def test_full_and_delta_expose_the_exact_dispatch_admission(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        ctx = self.context("worker")
        for admission in ("schedule", "decision", "work_item", "operator_record",
                          "wait", "ci", "operator", "ready_work", "change",
                          "next_task", "continuation"):
            with self.subTest(admission=admission):
                ctx.admission = admission
                self.assertEqual(json.loads(prompt_for(ctx))["admission"], admission)
                self.assertEqual(json.loads(prompt_delta_for(ctx, []))["admission"], admission)

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

    def test_grant_transition_is_fail_closed_without_changing_cursor_projection(self):
        from mizu.engine_config import effective, save_session, session_record, write_prompt_state
        from mizu.fs import read_json, write_json
        from mizu.runtime import session_prompt
        from mizu.unit_prompts import _grant_session, _prompt_tool_surface
        ctx = self.context("worker")
        path, _ = session_record(ctx, ctx.role.profile, effective(self.config, ctx.role, ctx.role.profile))
        save_session(path, "test-session", {})
        write_prompt_state(path.parent, snapshot=ctx.snapshot["id"],
                           inbox_generation=self.project.insights.generation())
        cursor = read_json(path.parent / "prompt_state.json")
        prior = {"session": _grant_session(read_json(path), path.parent),
                 "tools": ["mizu_finish"], "bound": "drain", "wrap_up": True}
        variants = [None, {}, [], {**prior, "session": "wrong-session"},
                    {**prior, "unexpected": True}, {**prior, "tools": [None]},
                    {**prior, "bound": None}, {**prior, "bound": "unknown"},
                    {**prior, "wrap_up": 1},
                    {**prior, "tools": "mizu_finish"},
                    {**prior, "tools": ["mizu_unknown"]},
                    {**prior, "tools": ["mizu_finish", "mizu_finish"]},
                    {**prior, "tools": _prompt_tool_surface(ctx)["tools"]}]
        for grant in variants:
            with self.subTest(grant=grant):
                write_json(path.parent / "prompt_state.json", {**cursor, "broker_grant": grant})
                prompt, mode, *_ = session_prompt(ctx, ctx.role, ctx.run_dir)
                decoded = json.loads(prompt)
                self.assertEqual(mode, "delta")
                self.assertNotIn("grant_transition", decoded)
                self.assertEqual(decoded["pending_insights"], [])
                self.assertEqual(decoded["unit_tool_surface"], _prompt_tool_surface(ctx))
        # Malformed cursor identity cannot describe any previous grant.
        malformed_cursor = {**cursor, "snapshot": "not-a-snapshot-id", "broker_grant": prior}
        write_json(path.parent / "prompt_state.json", malformed_cursor)
        self.assertNotIn("grant_transition", json.loads(
            session_prompt(ctx, ctx.role, ctx.run_dir)[0]))
        # A changed grant without a recorded bound is reported without guessing
        # a cause or claiming a temporary restriction was lifted.
        changed = {"session": prior["session"], "tools": ["mizu_finish"],
                   "bound": None, "wrap_up": False}
        write_json(path.parent / "prompt_state.json", {**cursor, "broker_grant": changed})
        transition = json.loads(session_prompt(ctx, ctx.role, ctx.run_dir)[0])["grant_transition"]
        self.assertIsNone(transition["previous_bound"])
        current_tools = _prompt_tool_surface(ctx)["tools"]
        self.assertEqual(transition["added_tools"],
                         [tool for tool in current_tools if tool != "mizu_finish"])
        self.assertEqual(transition["removed_tools"], [])
        self.assertIn("does not establish a per-run bound as the cause", transition["message"])
        without_wrapup = {**prior, "wrap_up": False}
        write_json(path.parent / "prompt_state.json", {**cursor, "broker_grant": without_wrapup})
        transition = json.loads(session_prompt(ctx, ctx.role, ctx.run_dir)[0])["grant_transition"]
        self.assertEqual(transition["previous_bound"], "drain")
        self.assertIn("does not establish a per-run bound as the cause", transition["message"])
        (path.parent / "prompt_state.json").write_text("{broken", encoding="utf-8")
        self.assertNotIn("grant_transition", json.loads(session_prompt(ctx, ctx.role, ctx.run_dir)[0]))
        # A bound still in force cannot be described as ended.
        write_json(path.parent / "prompt_state.json", {**cursor, "broker_grant": prior})
        for active in ("drain", "wrap_up", "bound"):
            with self.subTest(active=active):
                ctx.wrap_up_attempted = active == "wrap_up"
                ctx.bound_hit = "requests" if active == "bound" else None
                with patch.object(self.project, "control", return_value={"draining": active == "drain"}):
                    decoded = json.loads(session_prompt(ctx, ctx.role, ctx.run_dir)[0])
                    self.assertNotIn("grant_transition", decoded)

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
                                      "unseeded_decisions", "coverage_complete",
                                      "unknown_blocked"])

    def test_moved_builders_still_set_the_ready_work_signature(self):
        import mizu.unit_prompts as unit_prompts
        ctx = self.context("worker")
        self.assertFalse(hasattr(ctx, "ready_work_signature"))
        unit_prompts.prompt_for(ctx)
        first = ctx.ready_work_signature
        self.assertTrue(first)
        unit_prompts.prompt_delta_for(ctx, [])
        self.assertEqual(ctx.ready_work_signature, first)

    def test_full_delta_compaction_preserves_metadata_and_reduces_utf8_bytes(self):
        import copy
        import dataclasses
        from mizu import check_jobs, unit_prompts, work_order
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=tuple(set(ctx.role.capabilities)
                                                                   | {"check_job", "work_items_read"}))
        opening = copy.deepcopy(work_order._capture_opening(ctx))
        # Bounded synthetic pages, not provider/cache or scheduler measurements.
        opening["open_work_items"] = {
            "items": [{"id": "wi_" + f"{n:032x}", "title": "Scope", "status": "blocked", "rev": 3,
                       "wait": {"kind": "deadline", "at": "2099-01-01T00:00:00+00:00"},
                       **{key: "界" * 256 for key in
                          ("description_preview", "scope_preview", "reason_preview")},
                       "description_truncated": True, "doing_stale": False} for n in range(6)],
            "total": 9, "truncated": True, "overflow_count": 3, "coverage_complete": False}
        checks = {"check_jobs": [{"id": f"{n:032x}", "status": "completed", "passed": False,
                                  "kind": "experiment", "command_count": 2,
                                  "command_preview": ["界" * 100] * 2, "error": "界" * 256}
                                 for n in range(6)],
                  "shared_checks": [{"id": "f" * 32, "command_preview": ["echo shared"],
                                     "requires_input_check": True}],
                  "check_continuation": None,
                  "check_discovery": {"coverage_complete": False, "summaries_omitted": 2}}
        originals = copy.deepcopy((opening, checks))
        self.prompt_size_comparison = {}
        with patch.object(work_order, "_capture_opening", return_value=opening), \
                patch.object(check_jobs, "prompt_projection", return_value=checks), \
                patch.object(unit_prompts, "_prompt_clock", return_value={"now": "fixed"}):
            for mode, build in (("full", lambda: unit_prompts.prompt_for(ctx)),
                                ("delta", lambda: unit_prompts.prompt_delta_for(ctx, []))):
                ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
                    ctx.config.limits, session_delta_details="full"))
                before = build()
                fingerprints = ctx.prompt_row_fingerprints
                ctx.config = dataclasses.replace(ctx.config, limits=dataclasses.replace(
                    ctx.config.limits, session_delta_details="elide"))
                after = build()
                self.assertEqual(ctx.prompt_row_fingerprints, fingerprints)
                baseline, compact = json.loads(before), json.loads(after)
                for group, tool, arguments in (
                        ("open_work_items", "mizu_work_items_read", {}),
                        ("check_jobs", "mizu_check_job", {"action": "result"})):
                    rows = compact[group]["items"] if group == "open_work_items" else compact[group]
                    source = baseline[group]["items"] if group == "open_work_items" else baseline[group]
                    keys = ({"description_preview", "scope_preview", "reason_preview"}
                            if group == "open_work_items" else {"command_preview", "error"})
                    for row, original in zip(rows, source, strict=True):
                        self.assertEqual(row["details_reference"], {
                            "tool": tool, "arguments": {**arguments, "id": row["id"]}})
                        self.assertEqual({k: v for k, v in row.items() if k != "details_reference"},
                                         {k: v for k, v in original.items() if k not in keys})
                    if group == "open_work_items":
                        self.assertEqual({**compact[group], "items": source}, baseline[group])
                    compact[group] = baseline[group]
                self.assertEqual(compact, baseline)  # All other opening facts are unchanged.
                sizes = {"before": len(before.encode("utf-8")), "after": len(after.encode("utf-8"))}
                self.prompt_size_comparison[mode] = sizes
                self.assertLess(sizes["after"], sizes["before"])
        self.assertEqual((opening, checks), originals)


class ExternalCheckWakePromptTests(Fixture):
    def setUp(self):
        super().setUp()
        import dataclasses
        from mizu.ci_observation import wake_ci_facts
        from mizu.ci_polling import poll_ci
        from mizu.fs import write_json
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "command": ["true"], "ci_branch": "main",
            "ci_wake_extra_branches": ("release",)})
        self.project.config = self.config
        self.facts = [{"subject": subject, "revision": "a" * 40, "outcome": "failure",
                       "non_passing_checks": 1, "may_be_incomplete": False}
                      for subject in ("main", "release")]
        state = {}
        event = poll_ci(self.config, self.project, state, now=1000, interval=15,
                        branches=["main", "release", "off-wake"],
                        reader=lambda *args: {"checks": [
                            {"sha": "a" * 40, "check": "unit", "state": "failure"}]})
        self.assertTrue(event["ok"])
        write_json(self.project.root / "vcs-poll.json", state)
        self.assertEqual(wake_ci_facts(self.project, self.config), self.facts)

    def frozen_context(self, capabilities, *, include_ci=False):
        import copy
        import dataclasses
        from mizu import prompt_sections
        ctx = self.context("worker")
        ctx.role = dataclasses.replace(ctx.role, capabilities=capabilities,
            context_include=("ci",) if include_ci else (),
            context_exclude=tuple(name for name in prompt_sections.SECTIONS
                                  if name != "ci" or not include_ci))
        # Engine's dispatch capture is authoritative, not a prompt-time read.
        ctx.external_check_wake_facts = copy.deepcopy(self.facts)
        return ctx

    def builders(self):
        from mizu.unit_prompts import prompt_for, prompt_delta_for
        return ((prompt_for, ()), (prompt_delta_for, ([],)))

    def objects(self, value):
        if isinstance(value, dict):
            yield value
            for nested in value.values():
                yield from self.objects(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from self.objects(nested)

    def test_capability_matrix_keeps_fixed_facts_and_only_one_vcs_details_route(self):
        from mizu import prompt_sections
        optional_keys = {key for section in prompt_sections.SECTIONS.values() for key in section.keys}
        for caps in (("vcs_read",), ("check_job",), (), ("vcs_read", "check_job")):
            rendered = []
            for builder, args in self.builders():
                with self.subTest(capabilities=caps, builder=builder.__name__):
                    ctx = self.frozen_context(caps)
                    payload = json.loads(builder(ctx, *args))
                    self.assertFalse(optional_keys & payload.keys())
                    route = ({"tool": "mizu_vcs_read", "operations": ["status", "log"]}
                             if "vcs_read" in caps else None)
                    expected = {"facts": self.facts, "details": route}
                    self.assertEqual(payload["external_check_wake"], expected)
                    routes = [obj for obj in self.objects(payload) if obj.get("tool") == "mizu_vcs_read"]
                    self.assertEqual(routes, [route] if route else [])
                    self.assertNotIn("mizu_check_job", json.dumps(payload["external_check_wake"]))
                    rendered.append(payload["external_check_wake"])
            self.assertEqual(*rendered)

    def test_off_wake_subject_is_only_in_explicit_ci_summary(self):
        for caps in ((), ("vcs_read",)):
            for builder, args in self.builders():
                for include in (False, True):
                    with self.subTest(capabilities=caps, builder=builder.__name__, include=include):
                        ctx = self.frozen_context(caps, include_ci=include)
                        payload = json.loads(builder(ctx, *args))
                        self.assertEqual(payload["external_check_wake"]["facts"], self.facts)
                        if include:
                            self.assertEqual(payload.pop("ci_summary"), [
                                self.facts[0], dict(self.facts[0], subject="off-wake"), self.facts[1]])
                        else:
                            self.assertNotIn("ci_summary", payload)
                        self.assertNotIn("off-wake", json.dumps(payload))

    def test_cache_mutation_does_not_change_dispatch_frozen_wake(self):
        from mizu.fs import write_json
        for builder, args in self.builders():
            with self.subTest(builder=builder.__name__):
                ctx = self.frozen_context(("vcs_read",), include_ci=True)
                before = json.loads(builder(ctx, *args))
                write_json(self.project.root / "vcs-poll.json", {"failures": {
                    "main": [{"sha": "b" * 40, "check": "different"}]}})
                after = json.loads(builder(ctx, *args))
                self.assertEqual(after["external_check_wake"], before["external_check_wake"])
                self.assertEqual(after["external_check_wake"]["facts"], self.facts)
                self.assertNotEqual(after["ci_summary"], before["ci_summary"])
                self.assertEqual(after["ci_summary"], [{
                    "subject": "main", "revision": "b" * 40, "outcome": "failure",
                    "non_passing_checks": 1, "may_be_incomplete": True}])
                # Distinct mutation for the second builder as well.
                (self.project.root / "vcs-poll.json").unlink()

    def test_simultaneous_decision_and_ci_wake_are_both_delivered(self):
        import dataclasses
        from mizu import decision_delivery
        submitted = self.project.insights.submit(source="worker", title="Review", body="Scope",
                                                base_snapshot=None, run="test")
        self.project.insights.decide(submitted["id"], "reject", "not yet", "", "test", expected_rev=1)
        for builder, args in self.builders():
            ctx = self.frozen_context(())
            ctx.role = dataclasses.replace(ctx.role, decision_events=("reject",))
            ctx.decision_events = decision_delivery.pending(self.project, ctx.role)["events"]
            self.assertTrue(ctx.decision_events)
            payload = json.loads(builder(ctx, *args))
            self.assertEqual(payload["decision_events"], ctx.decision_events)
            self.assertEqual(payload["external_check_wake"], {"facts": self.facts, "details": None})

    def test_recursive_external_check_rows_are_denied(self):
        from mizu.errors import Denied
        forbidden = ({"passed_on": {"revision": "a" * 40}},
                     {"branch": "main", "sha": "a" * 40, "check": "unit"},
                     {"sha": "a" * 40, "check": "unit", "state": "failure"})
        for builder, args in self.builders():
            for row in forbidden:
                with self.subTest(builder=builder.__name__, row=row):
                    ctx = self.frozen_context(())
                    ctx.operator_record_page = {"events": [{"payload": {"nested": [row]}}]}
                    with self.assertRaisesRegex(Denied, "Per-check external observation"):
                        builder(ctx, *args)

    def test_local_check_jobs_and_check_name_selectors_are_accepted(self):
        import dataclasses
        from mizu import check_jobs
        local = {"id": "c" * 32, "status": "completed", "passed": False,
                 "kind": "experiment", "command_count": 1}
        selector = {"checks": ["unit"]}
        for builder, args in self.builders():
            ctx = self.frozen_context(("check_job",))
            ctx.role = dataclasses.replace(ctx.role, context_exclude=tuple(
                name for name in ctx.role.context_exclude if name != "check_jobs"))
            ctx.operator_record_page = {"events": [{"payload": {"nested": [selector]}}]}
            with patch.object(check_jobs, "prompt_projection", return_value={
                    "check_jobs": [local], "shared_checks": [], "check_continuation": None}):
                payload = json.loads(builder(ctx, *args))
            self.assertEqual(payload["check_jobs"], [local])
            self.assertEqual(payload["operator_records"], ctx.operator_record_page)
            self.assertEqual(payload["external_check_wake"], {"facts": self.facts, "details": None})


if __name__ == "__main__":
    unittest.main()
