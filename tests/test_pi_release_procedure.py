"""The Pi release procedure routes stable releases to review and records holds."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASING = ROOT / "docs" / "releasing.md"


class PiReleaseProcedureTests(unittest.TestCase):
    def setUp(self):
        self.text = RELEASING.read_text(encoding="utf-8")

    def test_stable_release_without_known_defect_reaches_review(self):
        text = self.text.lower()
        self.assertIn("every new stable", text)
        self.assertIn("whether or not its changelog", text)
        self.assertIn("no matching known defect", text)
        self.assertIn("still", text)
        self.assertIn("review", text)

    def test_stay_pinned_requires_recorded_reproduced_regression(self):
        text = self.text.lower()
        self.assertIn("blocking regression", text)
        self.assertIn("reproduced", text)
        self.assertIn("record the regression", text)
        self.assertIn("pin without", text)

    def test_operator_policy_tied_by_evidence_not_duplicated(self):
        text = self.text.lower()
        self.assertIn("operator-owned", text)
        self.assertIn("[web].feeds", text)
        self.assertIn("docs/upstream.md", text)
        self.assertIn("do not duplicate", text)
        self.assertIn("automatic updater", text)

    def test_lockstep_requirements_retained(self):
        text = self.text.lower()
        self.assertIn("both adapters", text)
        self.assertIn("regenerate both lockfiles", text)
        self.assertIn("pi sdk gate", text)
        self.assertIn("full check", text)
        self.assertIn("one reviewed commit", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
