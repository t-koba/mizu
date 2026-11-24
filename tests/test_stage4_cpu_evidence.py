"""The tracked stage-4 CPU evidence record stays path-free and self-consistent."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-stage4-cpu-evidence.py"
RECORD = ROOT / "docs" / "evidence" / "stage4-cpu-evidence.json"


class Stage4CpuEvidenceTests(unittest.TestCase):
    def test_record_is_tracked_and_path_free(self):
        text = RECORD.read_text(encoding="utf-8")
        for marker in ("/tmp/", "/workspace", "/usr/", "/home/", "mizu-before-src"):
            self.assertNotIn(marker, text)

    def test_verifier_passes_every_check(self):
        run = subprocess.run([sys.executable, str(VERIFIER)], cwd=ROOT,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertTrue(summary["passed"])
        self.assertTrue(all(summary["checks"].values()))

    def test_matched_shape_validity_and_verifiable_source_binding(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        m = record["measurement"]
        self.assertTrue(m["deployment_matches_after"])
        self.assertTrue(m["validity_matches_after"])
        self.assertEqual(m["validity"]["method"], "proc-per-pid")
        self.assertIs(m["validity"]["steady_state_observed"], True)
        for side in ("before", "after"):
            binding = record[side]["source_binding"]
            self.assertEqual(binding["daemon_src_sha256"],
                             binding["daemon_src_sha256_recomputed_from_git_tree"])
        # BEFORE was an explicit label, so its tree link was verified against git.
        self.assertIs(record["before"]["source_binding"]["label_verified_against_git"], True)


if __name__ == "__main__":
    unittest.main()
