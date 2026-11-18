import json

from mizu.config import load
from mizu.errors import ConfigError, Denied
from mizu.protocol import DEFINITIONS, tool_definitions
from support import Fixture


class FinishOutcomeTests(Fixture):
    def configure(self, value):
        header = "[roles.consult]\n"
        self.file.write_text(self.file.read_text().replace(
            header, header + "finish_outcomes = " + json.dumps(value) + "\n", 1))
        self.config = self.project.config = load(self.file)
        return self.config.roles["consult"]

    def test_default_role_preserves_finish_definition(self):
        role = self.config.roles["consult"]
        self.assertEqual(role.finish_outcomes, ("continue", "wait", "blocked", "done"))
        [finish] = tool_definitions(("finish",), role=role)
        self.assertEqual((finish["description"], finish["inputSchema"]), DEFINITIONS["finish"])
        fields = finish["inputSchema"]["properties"]
        self.assertEqual(fields["outcome"]["enum"], ["continue", "wait", "blocked", "done"])
        self.assertIn("wait_for_check", fields)
        self.assertIn("wait_seconds", fields)
        for fragment in ("outcome=wait with wait_seconds=N", "outcome=wait with wait_for_check=<id>",
                         "'done' on writable roles requires successful verification."):
            self.assertIn(fragment, finish["description"])

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
        self.assertIn("a blocked finish is accepted", description)
        self.assertNotIn("'done' on writable roles", description)

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
