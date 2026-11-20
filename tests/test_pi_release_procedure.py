"""The Pi release procedure routes stable releases to review and records holds.

The prose test pins the durable policy text. The handoff tests exercise that
policy's two outcomes behaviourally at the feed-to-insight/release-review
boundary: a bounded Atom feed observation reaches the writer as an
external-untrusted insight input, the resulting decision is recorded and
readable, and a reproduced regression is retained with its hold reason and
reproduction evidence. No Pi updater or release policy lives in the harness.
"""
import dataclasses
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu.web import Web

ROOT = Path(__file__).resolve().parents[1]
RELEASING = ROOT / "docs" / "releasing.md"
FEED = "https://github.com/earendil-works/pi/releases.atom"

#: Deterministic transcript marker printed by the bounded stand-in gate child.
GATE_MARKER = "MIZU-PI-SDK-GATE-TRANSCRIPT-3f91c7a2"
GATE_OUTPUT_CAP = 4096


def run_standin_sdk_gate(*, failing):
    """Run a bounded stand-in Pi SDK gate and return a structured outcome.

    The child prints a deterministic marker and exits non-zero in failing mode
    (zero in passing mode). Fixed argv, no shell, no network, a short timeout
    and an output cap keep this a small self-contained test double: the failure
    is the child's real exit status and captured transcript, never a literal.
    """
    expectation = "fail" if failing else "pass"
    command = (
        "import sys; "
        f"marker = {GATE_MARKER!r}; "
        f"expectation = {expectation!r}; "
        "print('pi-sdk-gate ' + expectation + ' ' + marker); "
        "sys.stdout.flush(); "
        "sys.exit(0 if expectation == 'pass' else 1)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, timeout=5, check=False)
    stdout = completed.stdout[:GATE_OUTPUT_CAP]
    stderr = completed.stderr[:GATE_OUTPUT_CAP]
    transcript = stdout + b"\n" + stderr
    return {
        "expectation": expectation,
        "returncode": completed.returncode,
        "failing": expectation == "fail" and completed.returncode != 0,
        "passed": expectation == "pass" and completed.returncode == 0,
        "marker": GATE_MARKER,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
        "transcript": transcript,
        "receipt": hashlib.sha256(transcript).hexdigest(),
    }


class PiReleaseProcedureTests(unittest.TestCase):
    def setUp(self):
        self.text = RELEASING.read_text(encoding="utf-8").lower()

    def test_release_procedure_documents_the_decision_rule(self):
        # Stable patches, reproduced-regression holds, operator-owned policy
        # tied by evidence, and retained lockstep requirements.
        for token in ("every new stable", "whether or not its changelog",
                      "no matching known defect", "still", "review",
                      "blocking regression", "reproduced", "record the regression",
                      "pin without", "operator-owned", "[web].feeds", "docs/upstream.md",
                      "do not duplicate", "automatic updater", "both adapters",
                      "regenerate both lockfiles", "pi sdk gate", "full check",
                      "one reviewed commit"):
            self.assertIn(token, self.text)


def atom(*entries):
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + "".join(entries) + "</feed>")


def entry(identity, title, summary):
    return (f'<entry><id>{identity}</id><title>{title}</title>'
            f'<updated>2026-11-10T00:00:00Z</updated><summary>{summary}</summary>'
            f'<link rel="alternate" href="https://github.com/earendil-works/pi/releases/tag/{identity}"/>'
            f'</entry>')


class PiReleaseHandoffTests(Fixture):
    """Bounded role walkthrough at the feed-to-insight/release-review handoff."""

    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, web={
            **self.config.web, "hosts": ["github.com"], "feeds": [FEED],
            "search_feed_item_limit": 10})
        role = self.config.roles["searcher"]
        self.config.roles["searcher"] = dataclasses.replace(
            role, capabilities=tuple(role.capabilities) + ("feed_updates",))

    def observe_feed(self, *entries):
        """Run the real feed reader with a bounded synthetic Atom observation."""
        xml = atom(*entries)

        def observe(config, run_dir, operation, value, cancel, role_name):
            web = Web(config.web, config.data / "web-cache", run_dir / "sources", role_name)
            web.fetch = Mock(return_value={"text": xml, "id": "a" * 64})
            return web.feed_entries(value)

        ctx = self.context("searcher")
        ctx.config = self.config
        with patch.object(ctx, "_bounded_web_operation", side_effect=observe):
            result = ctx.dispatch("feed_updates", {})
        self.assertEqual(result["trust"], "external-untrusted")
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope"], "configured-feeds-only")
        return result["feeds"][0]["entries"]

    def submit_release(self, insight_id, observed):
        return self.project.insights.submit(
            source="worker", title=observed["title"],
            body=(f'{observed["summary"]} source={observed["url"]} '
                  f'receipt={observed["source_receipt"]}'),
            base_snapshot=self.project.snapshots.get()["id"], insight_id=insight_id)

    def read_decision(self, insight_id):
        path = self.project.root / "decisions" / f"{insight_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def route_release(self, item, gate):
        """Walkthrough routing: hold only for a reproduced regression with a failing gate.

        A stable patch with no reproduced blocking regression, or a gate that
        passed, has no hold evidence and goes to lockstep adoption review. Any
        hold reason is derived from the captured gate transcript and receipts,
        so it cannot be satisfied by a fabricated literal.
        """
        reproduced = "blocking regression reproduced" in item["summary"]
        if reproduced and gate["failing"]:
            reproduction = (
                f'reproduced blocking regression at {item["url"]} '
                f'(source receipt {item["source_receipt"]}, '
                f'gate transcript {gate["stdout"].strip()}, '
                f'gate receipt sha256:{gate["receipt"]})')
            return {
                "action": "defer",
                "reason": f"Hold: {reproduction}; stay pinned until the regression is resolved",
                "revisit": "recheck after upstream regression fix",
                "token": "run-hold-review",
                "reproduction": reproduction,
            }
        return {
            "action": "defer",
            "reason": ("Route to lockstep adoption review: no reproduced blocking "
                       "regression with a failing gate"),
            "revisit": "lockstep adoption review of " + item["title"],
            "token": "run-stable-review",
            "reproduction": None,
        }

    def test_stable_patch_without_known_defect_reaches_adoption_review(self):
        observed = self.observe_feed(
            entry("1.5.3", "Pi 1.5.3 stable", "stable patch; fixes no known Mizu defect"))
        self.assertEqual(len(observed), 1)
        item = observed[0]
        self.assertIn("no known Mizu defect", item["summary"])
        self.assertEqual(item["url"], "https://github.com/earendil-works/pi/releases/tag/1.5.3")
        self.assertEqual(len(item["source_receipt"]), 64)

        insight_id = "a" * 32
        submitted = self.submit_release(insight_id, item)
        self.assertIn(item["source_receipt"], submitted["body"])
        self.project.insights.decide(
            insight_id, "defer",
            "Route to lockstep adoption review: no known Mizu defect match is not a reason to skip review",
            "lockstep adoption review of Pi 1.5.3", "run-stable-review", expected_rev=1)
        decision = self.read_decision(insight_id)
        self.assertEqual(decision["action"], "defer")
        self.assertIn("adoption review", decision["reason"])
        self.assertIn("no known Mizu defect", decision["reason"])
        self.assertIn("lockstep adoption review", decision["revisit"])
        history = list((self.project.insights.root / "decision-history").glob("*.json"))
        self.assertEqual(len(history), 1)
        self.assertIn("adoption review", history[0].read_text(encoding="utf-8"))

    def test_reproduced_regression_records_hold_with_reason_and_evidence(self):
        observed = self.observe_feed(
            entry("1.5.4", "Pi 1.5.4 stable",
                  "blocking regression reproduced: stable patch fails the Pi SDK gate"))
        item = observed[0]
        insight_id = "b" * 32
        submitted = self.submit_release(insight_id, item)

        gate = run_standin_sdk_gate(failing=True)
        self.assertTrue(gate["failing"])
        self.assertNotEqual(gate["returncode"], 0)
        self.assertIn(gate["marker"], gate["stdout"])
        self.assertEqual(len(gate["receipt"]), 64)
        self.assertEqual(gate["receipt"], hashlib.sha256(gate["transcript"]).hexdigest())

        # The feed itself must report a reproduced blocking regression for this
        # failing gate to justify a hold; a stable-patch suspicion does not.
        self.assertIn("blocking regression reproduced", item["summary"])
        self.assertIn("fails the Pi SDK gate", item["summary"])

        routing = self.route_release(item, gate)
        self.assertIsNotNone(routing["reproduction"])
        self.assertIn(gate["marker"], routing["reproduction"])
        self.assertIn(gate["receipt"], routing["reproduction"])
        self.assertIn(item["source_receipt"], routing["reproduction"])
        self.assertIn(item["url"], routing["reproduction"])

        self.project.insights.decide(
            insight_id, routing["action"], routing["reason"], routing["revisit"],
            routing["token"], expected_rev=1)
        decision = self.read_decision(insight_id)
        self.assertEqual(decision["action"], "defer")
        self.assertIn("Hold:", decision["reason"])
        self.assertIn("blocking regression", decision["reason"])
        self.assertIn("reproduced", decision["reason"])
        self.assertIn(item["source_receipt"], decision["reason"])
        self.assertIn(item["url"], decision["reason"])
        self.assertIn(gate["marker"], decision["reason"])
        self.assertIn(gate["receipt"], decision["reason"])
        history = list((self.project.insights.root / "decision-history").glob("*.json"))
        self.assertEqual(len(history), 1)
        retained = history[0].read_text(encoding="utf-8")
        self.assertIn(item["source_receipt"], retained)
        self.assertIn(gate["marker"], retained)
        self.assertIn(gate["receipt"], retained)

    def test_stable_patch_with_passing_gate_routes_to_adoption_review_without_hold(self):
        observed = self.observe_feed(
            entry("1.5.5", "Pi 1.5.5 stable",
                  "stable patch: no regression reproduced, SDK gate not run"))
        item = observed[0]
        insight_id = "c" * 32
        submitted = self.submit_release(insight_id, item)

        gate = run_standin_sdk_gate(failing=False)
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["returncode"], 0)
        self.assertIn(gate["marker"], gate["stdout"])
        self.assertEqual(len(gate["receipt"]), 64)

        routing = self.route_release(item, gate)
        self.assertIsNone(routing["reproduction"])
        self.assertNotIn("Hold:", routing["reason"])
        self.project.insights.decide(
            insight_id, routing["action"], routing["reason"], routing["revisit"],
            routing["token"], expected_rev=1)
        decision = self.read_decision(insight_id)
        self.assertEqual(decision["action"], "defer")
        self.assertIn("adoption review", decision["reason"])
        self.assertNotIn("Hold:", decision["reason"])
        self.assertNotIn(gate["marker"], decision["reason"])
        self.assertNotIn(gate["receipt"], decision["reason"])
        self.assertIn("lockstep adoption review", decision["revisit"])
        history = list((self.project.insights.root / "decision-history").glob("*.json"))
        self.assertEqual(len(history), 1)
        retained = history[0].read_text(encoding="utf-8")
        self.assertNotIn("Hold:", retained)
        self.assertNotIn("gate transcript", retained)
        self.assertNotIn(gate["marker"], retained)
        self.assertNotIn(gate["receipt"], retained)
        self.assertNotIn(item["source_receipt"], retained)


    def test_hold_requires_both_a_reproduced_regression_and_a_failing_gate(self):
        stable, reproduced = self.observe_feed(
            entry("1.5.6", "Pi 1.5.6 stable",
                  "stable patch: no reproduced regression; SDK gate not run"),
            entry("1.5.7", "Pi 1.5.7 stable",
                  "blocking regression reproduced: stable patch fails the Pi SDK gate"))
        failing = run_standin_sdk_gate(failing=True)
        passing = run_standin_sdk_gate(failing=False)
        self.assertTrue(failing["failing"])
        self.assertTrue(passing["passed"])

        # Both conditions must be load-bearing: a reproduced report alone, a
        # failing gate alone, or a reproduced report with a passing gate must
        # not produce a hold or leak gate evidence into the routing reason.
        cases = (
            (reproduced, failing, True),
            (reproduced, passing, False),
            (stable, failing, False),
            (stable, passing, False),
        )
        for item, gate, held in cases:
            with self.subTest(version=item["title"], gate=gate["expectation"]):
                routing = self.route_release(item, gate)
                if held:
                    self.assertTrue(routing["reason"].startswith("Hold:"))
                    self.assertIsNotNone(routing["reproduction"])
                    self.assertIn(gate["receipt"], routing["reason"])
                else:
                    self.assertEqual(routing["action"], "defer")
                    self.assertNotIn("Hold:", routing["reason"])
                    self.assertIsNone(routing["reproduction"])
                    self.assertNotIn(gate["marker"], routing["reason"])
                    self.assertNotIn(gate["receipt"], routing["reason"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
