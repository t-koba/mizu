import json

from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.protocol import DEFINITIONS, finish_definition, tool_definitions
from mizu.unit_prompts import prompt_delta_for, prompt_for
from support import Fixture


class FinishOutcomeTests(Fixture):
    def configure(self, value):
        header = "[roles.consult]\n"
        self.file.write_text(self.file.read_text().replace(
            header, header + "finish_outcomes = " + json.dumps(value) + "\n", 1))
        self.config = self.project.config = load(self.file)
        return self.config.roles["consult"]

    def opening(self):
        ctx = self.context("consult")
        full = json.loads(prompt_for(ctx))["unit"]
        self.assertEqual(json.loads(prompt_delta_for(ctx, []))["unit"], full)
        return full

    def test_default_role_preserves_finish_definition(self):
        role = self.config.roles["consult"]
        self.assertEqual(role.finish_outcomes, ("continue", "wait", "blocked", "done"))
        [finish] = tool_definitions(("finish",), role=role)
        self.assertEqual((finish["description"], finish["inputSchema"]), DEFINITIONS["finish"])
        fields = finish["inputSchema"]["properties"]
        self.assertEqual(fields["outcome"]["enum"], ["continue", "wait", "blocked", "done"])
        self.assertIn("wait_for_check", fields)
        self.assertIn("wait_seconds", fields)
        self.assertEqual(fields["next_step"], {"type": "string", "minLength": 1, "maxLength": 4096})
        self.assertNotIn("next_step", finish["inputSchema"]["required"])
        for fragment in ("Use outcome=wait with wait_seconds=N to set a wake deadline",
                         "Use outcome=wait with wait_for_check=<id> to resume",
                         "For a writable role, outcome=done requires successful verification."):
            self.assertIn(fragment, finish["description"])

    def test_default_opening_states_purposes_not_refusals(self):
        opening = self.opening()
        self.assertIn("do useful work within your role and capabilities, then finish with mizu_finish", opening)
        description, _ = DEFINITIONS["finish"]
        for text in (opening, description):
            for purpose in ("outcome=continue as the ordinary finish",
                            "must supply a typed `next_step` naming a new proposed follow-up",
                            "outcome=wait to pause", "outcome=blocked to report", "outcome=done to mark"):
                self.assertIn(purpose, text)
            for refusal in ("A malformed writer continue is refused", "next_step alone does not admit",
                            "the host requires a parent snapshot", "role-matching validated progress record",
                            "accepted only with a validated next_step", "Unknown or truncated evidence refuses finish",
                            "Ready work alone does not create a timed re-admission"):
                self.assertNotIn(refusal, text)

    def test_subset_removes_unavailable_outcomes_and_wait_fields(self):
        role = self.configure(["blocked", "continue"])
        self.assertEqual(role.finish_outcomes, ("blocked", "continue"))
        [finish] = tool_definitions(("finish",), role=role)
        fields = finish["inputSchema"]["properties"]
        self.assertEqual(fields["outcome"]["enum"], ["blocked", "continue"])
        self.assertNotIn("wait_for_check", fields)
        self.assertNotIn("wait_seconds", fields)
        description = finish["description"]
        self.assertNotRegex(description, r"outcome=wait|a wait\b|wait_for_check|wait_seconds")
        self.assertIn("outcome=blocked to report", description)
        self.assertNotIn("outcome=done", description)
        opening = self.opening()
        self.assertIn("outcome=blocked", opening)
        self.assertNotRegex(opening, r"\bwait\b|wake event|\boutcome=done\b")

    def test_no_parking_outcomes_have_no_parking_description(self):
        for outcomes in (("continue", "done"), ("done",)):
            with self.subTest(outcomes=outcomes):
                description, _ = finish_definition(outcomes)
                self.assertNotRegex(description, r"\b(wait|blocked)\b|wake event|prevents further work")

    def test_done_only_opening_has_no_parking_or_continuation(self):
        self.configure(["done"])
        opening = self.opening()
        self.assertIn("outcome=done", opening)
        self.assertNotRegex(opening, r"\b(wait|blocked|continue|next_step)\b|wake event|prevents further work")

    def test_blocked_only_description_omits_continuation(self):
        description, _ = finish_definition(("blocked",))
        self.assertIn("outcome=blocked to report", description)
        self.assertNotRegex(description, r"\bcontinue\b|\bnext_step\b")

    def test_invalid_outcomes_fail_configuration(self):
        original = self.file.read_text()
        for value in ([], ["wait", "wait"], ["blocked", "bounded"], "done", [4]):
            with self.subTest(value=value):
                self.file.write_text(original)
                with self.assertRaisesRegex(ConfigError, r"roles\.consult\.finish_outcomes"):
                    self.configure(value)

    def test_broker_refuses_omitted_wait_and_done_before_sealing(self):
        self.configure(["blocked"])
        ctx = self.context("consult")
        for outcome in ("wait", "done"):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(Denied, f"does not allow finish outcome '{outcome}'"):
                    ctx.handle("finish", {"outcome": outcome, "summary": "Work complete"})
                self.assertIsNone(ctx.finished)
                self.assertFalse((ctx.run_dir / "finish-intent.json").exists())
        self.assertEqual(ctx.handle("finish", {"outcome": "blocked", "summary": "Work complete"}),
                         {"sealed": True})
