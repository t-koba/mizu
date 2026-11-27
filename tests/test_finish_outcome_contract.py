"""Finish descriptions state resolved scheduler effects, not outcome advice."""
import json
import re

from mizu.config import Limits, load, outcome_effects
from mizu.protocol import (MAX_FINISH_EFFECT_CONTRACT_CHARS,
                           _writer_continuation_contract, tool_definitions)
from mizu.unit_prompts import prompt_delta_for, prompt_for
from support import Fixture

EFFECTS = ("progress", "no_progress", "continuation", "idle", "waiting", "ready_check")
ROWS = re.compile(r"\b(continue|wait|blocked|done)=\[([^\]]*)\]")
ADVICE = ("Use outcome=", "ordinary finish", "naming a new proposed follow-up",
          "to pause until", "to report what prevents", "to mark the work complete",
          "requires successful verification", "New work can wake you sooner",
          "with wait_seconds=N", "with wait_for_check=<id>")
DEPLOYMENT_WORDS = ("project", "role", "CI", "VCS", "policy", "branch", "publication")


def described(text):
    """Parse the machine-readable effect rows out of a derived contract."""
    return {outcome: set(value.split(", ")) if value else set()
            for outcome, value in ROWS.findall(text)}


class FinishOutcomeContractTests(Fixture):
    def configure(self, *, limits="", effects="", role_effects=""):
        text = self.file.read_text()
        for line in limits.splitlines():
            key = line.split("=", 1)[0].strip()
            text, count = re.subn(rf"^#?\s*{re.escape(key)}\s*=.*$", lambda _: line, text,
                                  count=1, flags=re.M)
            self.assertTrue(count, key)
        if role_effects:
            text = text.replace("[roles.consult]\n", "[roles.consult]\n" + role_effects, 1)
        if effects:
            text += "\n[limits.outcome_effects]\n" + effects
        self.file.write_text(text)
        self.config = self.project.config = load(self.file)
        return self.config.roles["consult"]

    def assert_resolved(self, text, config, role):
        rows = described(text)
        self.assertEqual(set(rows), set(role.finish_outcomes))
        resolved = outcome_effects(config, role)
        for outcome in role.finish_outcomes:
            with self.subTest(outcome=outcome):
                self.assertEqual(rows[outcome],
                                 {name for name in EFFECTS if outcome in getattr(resolved, name)})
        for phrase in ADVICE + DEPLOYMENT_WORDS:
            self.assertNotIn(phrase, text)
        self.assertLessEqual(len(text), MAX_FINISH_EFFECT_CONTRACT_CHARS)
        self.assertEqual(text, _writer_continuation_contract(role.finish_outcomes, config, role))

    def test_limits_effects_are_described_exactly(self):
        role = self.configure(
            limits='default_wait_seconds = 47\nprogress_outcomes = ["continue", "wait"]\n',
            effects=('no_progress = ["continue", "blocked"]\n'
                     'continuation = ["wait"]\n'
                     'idle = ["wait"]\n'
                     'waiting = ["continue"]\n'))
        text = _writer_continuation_contract(role.finish_outcomes, self.config, role)
        self.assert_resolved(text, self.config, role)
        self.assertIn("done is terminal; only an operator can reopen.", text)
        self.assertIn("the configured default wait is 47 seconds", text)
        self.assertIn("messages, check results, a deadline, or an operator wake", text)

    def test_role_override_changes_described_effects(self):
        role = self.configure(
            role_effects=('progress_outcomes = ["blocked"]\n'
                          'outcome_effects = { no_progress = ["done"], '
                          'continuation = ["blocked", "done"], idle = ["blocked"], '
                          'waiting = ["wait"] }\n'))
        text = _writer_continuation_contract(role.finish_outcomes, self.config, role)
        self.assert_resolved(text, self.config, role)
        self.assertEqual(described(text)["done"], set())
        self.assertNotIn("47 seconds", text)
        self.assertIn(f"the configured default wait is {Limits().default_wait_seconds} seconds", text)

    def test_tool_description_and_unit_opening_share_the_derived_text(self):
        role = self.configure(effects='waiting = ["blocked"]\n')
        contract = _writer_continuation_contract(role.finish_outcomes, self.config, role)
        [finish] = tool_definitions(("finish",), role=role, config=self.config)
        self.assertIn("\n\n" + contract + "\n\n", finish["description"])
        ctx = self.context("consult")
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx)):
            opening = json.loads(prompt)["unit"]
            self.assertTrue(opening.endswith(contract))
            self.assertEqual(described(opening), described(contract))
