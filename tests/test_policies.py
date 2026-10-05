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

    def test_publisher_ships_only_approved_tree(self):
        text = (ROOT / "policies/publisher.md").read_text()
        self.assertIn("GO <branch>", text)
        self.assertIn("vcs_publish", text)
        self.assertIn("vcs_read", text)
        self.assertIn("never holds `submit_insight`", text)
        self.assertIn("mizu_finish", text)
        example = (ROOT / "config/config.example.toml").read_text()
        self.assertIn('[roles.publisher]', example)
        self.assertIn('policies/publisher.md', example)
        self.assertIn('vcs_publish', example)
        head, tail = example.split('[roles.publisher]')
        comment = head.split('Dedicated publisher for external publication only')[1]
        self.assertIn('required plumbing', comment)
        stanza = tail.split('\n\n')[0]
        self.assertIn('workspace = "write"', stanza)
        self.assertNotIn('"exec"', stanza)
        self.assertIn('required plumbing', text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
