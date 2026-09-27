"""Policy contract tests: guidance stays explicit without changing mechanism."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PolicyContractTests(unittest.TestCase):
    def test_worker_blocked_template_is_explicit(self):
        text = (ROOT / "policies/worker.md").read_text()
        self.assertIn("Qn:", text)
        self.assertIn("tried", text)
        self.assertIn("unblocks", text)
        self.assertIn("resume", text)

    def test_worker_state_stays_short_and_scoped(self):
        text = (ROOT / "policies/worker.md").read_text()
        self.assertIn("next single action", text)
        self.assertIn("Refer to stored records", text)
        self.assertIn("Do not use consensus as a substitute for validation", text)

    def test_editor_remains_propose_only(self):
        text = (ROOT / "policies/editor.md").read_text()
        self.assertIn("no shared-code write authority", text)
        self.assertIn("submit_insight", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
