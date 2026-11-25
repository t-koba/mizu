"""The tracked stage-4 CPU evidence record is path-free, ID-free and tamper-evident."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-stage4-cpu-evidence.py"
RECORD = ROOT / "docs" / "evidence" / "stage4-cpu-evidence.json"


def run_verifier(extra=None):
    cmd = [sys.executable, str(VERIFIER)] + (extra or [])
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def anchor_commits_available(record):
    """True when this checkout holds the public commits the record names."""
    for side in ("before", "after"):
        revision = record[side]["source_binding"]["source_revision"]
        probe = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", revision + "^{commit}"],
            capture_output=True)
        if probe.returncode:
            return False
    return True


class Stage4CpuEvidenceTests(unittest.TestCase):
    def test_record_is_tracked_path_free_and_id_free(self):
        text = RECORD.read_text(encoding="utf-8")
        for marker in ("/tmp/", "/workspace", "/usr/", "/home/", "mizu-before-src"):
            self.assertNotIn(marker, text)
        for key in ("check_id", "check_command_id", "check_result_recompute",
                    "check_log_recompute", "command_id", "source_receipt_sha256",
                    "daemon_src_sha256_recomputed_from_git_tree"):
            self.assertNotIn(key, text)

    def test_tracked_only_verifier_passes_and_scopes_tree_checks(self):
        run = run_verifier()
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["scope"], "tracked_only")
        self.assertTrue(summary["passed"])
        self.assertTrue(all(summary["checks"].values()))
        self.assertIn("before_tree_digest_from_git", summary["skipped"])
        self.assertIn("after_tree_digest_from_git", summary["skipped"])
        self.assertNotIn("before_tree_digest_from_git", summary["checks"])

    @unittest.skipUnless((ROOT / ".git").exists(),
                         "source-tree recompute needs a full git checkout")
    def test_with_git_verifier_recomputes_tree_digests(self):
        if not anchor_commits_available(json.loads(RECORD.read_text(encoding="utf-8"))):
            self.skipTest("public anchor commits absent from this checkout")
        run = run_verifier(["--repo", str(ROOT)])
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["scope"], "with_git")
        self.assertEqual(summary["skipped"], [])
        self.assertTrue(summary["checks"]["before_tree_digest_from_git"])
        self.assertTrue(summary["checks"]["after_tree_digest_from_git"])

    def test_falsified_numbers_fail_with_git(self):
        # The exact defect the reviewer found must now be caught: mutate the
        # BEFORE numbers, source revision and source digest in a copy, then
        # confirm the git-backed recompute rejects it.
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        record["before"]["embedded_receipt"]["idle"]["median_seconds"] = 9.9
        record["before"]["embedded_receipt"]["one_change"]["median_seconds"] = 9.9
        record["before"]["embedded_receipt"]["source"]["source_revision"] = "1" * 40
        record["before"]["embedded_receipt"]["source"]["daemon_src_sha256"] = "f" * 64
        record["before"]["source_binding"]["source_revision"] = "1" * 40
        record["before"]["source_binding"]["daemon_src_sha256"] = "f" * 64
        record["before"]["embedded_receipt_sha256"] = _canonical_sha(
            record["before"]["embedded_receipt"])
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            (fake_root / "docs" / "evidence").mkdir(parents=True)
            (fake_root / "scripts").mkdir()
            (fake_root / "docs" / "evidence" / "stage4-cpu-evidence.json").write_text(
                json.dumps(record), encoding="utf-8")
            (fake_root / "scripts" / "verify-stage4-cpu-evidence.py").write_bytes(
                VERIFIER.read_bytes())
            harness = fake_root / "scripts" / "daemon-cpu.py"
            harness.parent.mkdir(parents=True, exist_ok=True)
            harness.write_bytes((ROOT / "scripts" / "daemon-cpu.py").read_bytes())
            run = subprocess.run(
                [sys.executable, str(fake_root / "scripts" / "verify-stage4-cpu-evidence.py"),
                 "--repo", str(ROOT)], cwd=fake_root, capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        summary = json.loads(run.stdout)
        self.assertFalse(summary["checks"]["before_tree_digest_from_git"])
        self.assertFalse(summary["checks"]["before_witness_consistent"])

    def test_matched_shape_validity_and_public_commit_anchor(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        m = record["measurement"]
        self.assertTrue(m["deployment_matches_after"])
        self.assertTrue(m["validity_matches_after"])
        self.assertEqual(m["validity"]["method"], "proc-per-pid")
        self.assertIs(m["validity"]["steady_state_observed"], True)
        self.assertEqual(record["schema"], "stage4-cpu-evidence-v2")
        for side in ("before", "after"):
            binding = record[side]["source_binding"]
            recompute = binding["recompute_from_public_commit"]
            self.assertIs(recompute["commit_is_public_ancestor_of_head"], True)
        self.assertIs(record["before"]["source_binding"]["label_verified_against_git"], True)


def _canonical_sha(obj):
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


if __name__ == "__main__":
    unittest.main()
