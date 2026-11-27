"""Finish descriptions state resolved scheduler effects, not outcome advice."""
import json
import re
from dataclasses import replace

from mizu.config import Limits, load, outcome_effects
from mizu.protocol import (MAX_FINISH_EFFECT_CONTRACT_CHARS,
                           _writer_continuation_contract, tool_definitions)
from mizu.unit_prompts import prompt_delta_for, prompt_for
from support import Fixture

EFFECTS = ("progress", "no_progress", "continuation", "idle", "waiting", "ready_check")
WRITER_ONLY = ("waiting", "ready_check")
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


def advertised_effects(config, role):
    """Effects a finish can actually apply for this unit."""
    resolved = outcome_effects(config, role)
    writer = getattr(role, "workspace", None) == "write"
    return {name: (getattr(resolved, name) if writer or name not in WRITER_ONLY else ())
            for name in EFFECTS}


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
        resolved = advertised_effects(config, role)
        for outcome in role.finish_outcomes:
            with self.subTest(outcome=outcome):
                self.assertEqual(rows[outcome],
                                 {name for name in EFFECTS if outcome in resolved[name]})
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
        writer = replace(role, workspace="write")
        text = _writer_continuation_contract(writer.finish_outcomes, self.config, writer)
        self.assert_resolved(text, self.config, writer)
        self.assertIn("done is terminal; only an operator can reopen.", text)
        self.assertIn("Writable units accept done only after configured verification passes.", text)
        self.assertIn("the configured default wait is 47 seconds", text)
        self.assertIn("messages, check results, a deadline, or an operator wake", text)
        self.assertNotIn("Immediate next-step handoff", text)

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
        self.assertNotIn("waiting:", text)
        self.assertNotIn("ready_check:", text)

    def test_read_only_role_omits_writer_only_effects(self):
        role = self.config.roles["consult"]
        self.assertEqual(role.workspace, "read")
        text = _writer_continuation_contract(role.finish_outcomes, self.config, role)
        self.assert_resolved(text, self.config, role)
        for names in described(text).values():
            self.assertFalse(names & set(WRITER_ONLY))
        self.assertNotIn("parks this unit", text)
        self.assertNotIn("Waiting outcomes park", text)
        self.assertIn("wait_seconds sets a wake deadline", text)
        self.assertNotIn("wait_for_check selects", text)
        [finish] = tool_definitions(("finish",), role=role, config=self.config)
        self.assertIn("\n\n" + text + "\n\n", finish["description"])
        ctx = self.context("consult")
        for prompt in (prompt_for(ctx), prompt_delta_for(ctx)):
            opening = json.loads(prompt)["unit"]
            self.assertTrue(opening.endswith(text))
            self.assertEqual(described(opening), described(text))

    def test_done_verification_fact_tracks_writable_units(self):
        fact = "Writable units accept done only after configured verification passes."
        for workspace, expected in (("write", True), ("read", False)):
            with self.subTest(workspace=workspace):
                role = replace(self.config.roles["consult"], workspace=workspace)
                text = _writer_continuation_contract(role.finish_outcomes, self.config, role)
                self.assert_resolved(text, self.config, role)
                self.assertEqual(fact in text, expected)

    def test_wait_facts_track_capability_and_availability(self):
        role = self.config.roles["consult"]
        fact = "wait_for_check selects a check job owned by this unit."
        self.assertNotIn(fact, _writer_continuation_contract(
            role.finish_outcomes, self.config, role))
        checker = replace(role, capabilities=(*role.capabilities, "check_job"))
        self.assertIn(fact, _writer_continuation_contract(
            checker.finish_outcomes, self.config, checker))
        no_wait = replace(checker, finish_outcomes=("blocked", "done"))
        text = _writer_continuation_contract(no_wait.finish_outcomes, self.config, no_wait)
        self.assertNotIn(fact, text)
        self.assertNotIn("wait_seconds", text)

    def test_next_step_fact_tracks_continuation(self):
        fact = "next_step carries the continuation handoff."
        recorded = "next_step is recorded but applies no handoff here."
        role = self.config.roles["consult"]
        writer = replace(role, workspace="write")
        self.assertIn(fact, _writer_continuation_contract(
            writer.finish_outcomes, self.config, writer))
        reader_text = _writer_continuation_contract(role.finish_outcomes, self.config, role)
        self.assertNotIn(fact, reader_text)
        self.assertIn(recorded, reader_text)
        for outcomes in (("done",), ("blocked",), ("wait",)):
            with self.subTest(outcomes=outcomes):
                limited = replace(writer, finish_outcomes=outcomes)
                text = _writer_continuation_contract(
                    limited.finish_outcomes, self.config, limited)
                self.assertNotIn(fact, text)

    def test_next_step_readiness_fact_tracks_configured_signals(self):
        role = self.configure(limits='progress_signals = ["code"]\n')
        writer = replace(role, workspace="write")
        prefix = "Immediate next-step handoff after continue requires "
        self.assertIn(prefix + "a step differing from the published parent's step, "
                      "continue in the configured no-progress outcomes, and a selected "
                      "work signal among code; when any requirement is unmet, no handoff "
                      "occurs and the configured default wait of 1800 seconds applies "
                      "until a ready work item or another wake.",
                      _writer_continuation_contract(
                          writer.finish_outcomes, self.config, writer))
        narrowed = replace(writer, progress_signals=("state",))
        self.assertIn(prefix + "a step differing from the published parent's step, a "
                      "configured no-progress outcome and qualifying work signal, but "
                      "none applies;", _writer_continuation_contract(
                          narrowed.finish_outcomes, self.config, narrowed))
        self.assertNotIn("Immediate next-step handoff", _writer_continuation_contract(
            role.finish_outcomes, self.config, role))

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
