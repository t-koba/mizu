"""M5: AGENTS.md holds contributor rules only; operator rules live in docs/.

Checks that AGENTS.md keeps the contributor invariant, points at operator
docs instead of instructing deployment/credentials/scheduling/promotion,
contains no operator commands or private paths, and that its doc pointers
resolve to existing files.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"

# Operator-command / private-path markers that must not appear in AGENTS.md.
FORBIDDEN = [
    "systemctl", "launchctl", "schtasks", "loginctl", "enable-linger",
    "credentials.env", "/home/", "/Users/", "$HOME", "%APPDATA%",
    "deployment remains an explicit operator action",
    "Executing Maintainer roles work in their own candidate projects",
]

# Doc pointers AGENTS.md must reference (contributor file points, not instructs).
REQUIRED_POINTERS = [
    "docs/setup.md",
    "docs/operations.md",
    "docs/releasing.md",
    "docs/security.md",
]


class AgentsSplitTests(unittest.TestCase):
    def test_contributor_scope_and_invariant_kept(self):
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("contributors working on Mizu itself", text)
        self.assertIn(
            "No external publication without recorded human approval", text)
        self.assertIn("GO <branch>", text)

    def test_operator_pointers_present_and_valid(self):
        text = AGENTS.read_text(encoding="utf-8")
        for pointer in REQUIRED_POINTERS:
            self.assertIn(pointer, text, f"missing operator pointer {pointer}")
        # Every docs/*.md mention must resolve to an existing file.
        for match in re.findall(r"docs/[A-Za-z0-9_.-]+\.md", text):
            self.assertTrue((ROOT / match).is_file(),
                            f"broken doc pointer {match}")

    def test_no_operator_instructions_or_private_paths(self):
        text = AGENTS.read_text(encoding="utf-8")
        for marker in FORBIDDEN:
            self.assertNotIn(marker, text, f"operator leftover {marker!r}")
        # No home-directory shortcuts or tilde paths anywhere.
        self.assertNotRegex(text, r"~/\S")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
