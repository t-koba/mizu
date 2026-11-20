"""The Pi release procedure routes stable releases to review and records holds.

The prose test pins the durable policy text. The handoff tests exercise that
policy's two outcomes behaviourally at the feed-to-insight/release-review
boundary: a bounded Atom feed observation reaches the writer as an
external-untrusted insight input, the resulting decision is recorded and
readable, and a reproduced regression is retained with its hold reason and
reproduction evidence. No Pi updater or release policy lives in the harness.
"""
import dataclasses
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import Fixture
from mizu.web import Web

ROOT = Path(__file__).resolve().parents[1]
RELEASING = ROOT / "docs" / "releasing.md"
FEED = "https://github.com/earendil-works/pi/releases.atom"


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
        # Reproduction evidence is the feed's source receipt plus the observed
        # gate failure, both carried into the hold decision rather than assumed.
        reproduction = (f'reproduced blocking regression at {item["url"]} '
                        f'(receipt {item["source_receipt"]}, Pi SDK gate failure)')
        self.project.insights.decide(
            insight_id, "defer",
            f"Hold: {reproduction}; stay pinned until the regression is resolved",
            "recheck after upstream regression fix", "run-hold-review", expected_rev=1)
        decision = self.read_decision(insight_id)
        self.assertEqual(decision["action"], "defer")
        self.assertIn("blocking regression", decision["reason"])
        self.assertIn("reproduced", decision["reason"])
        self.assertIn(item["source_receipt"], decision["reason"])
        self.assertIn(item["url"], decision["reason"])
        history = list((self.project.insights.root / "decision-history").glob("*.json"))
        self.assertEqual(len(history), 1)
        retained = history[0].read_text(encoding="utf-8")
        self.assertIn(item["source_receipt"], retained)
        self.assertIn("Pi SDK gate failure", retained)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
