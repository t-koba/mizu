"""The tracked native-wake CPU evidence record is path-free and git-verifiable."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-stage4-cpu-evidence.py"
RECORD = ROOT / "docs" / "evidence" / "native-wake-cpu-evidence.json"


def run_verifier(record, extra=None):
    cmd = [sys.executable, str(VERIFIER), "--record", str(record)] + (extra or [])
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def canonical(obj):
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


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


class NativeWakeCpuEvidenceTests(unittest.TestCase):
    def test_record_is_tracked_path_free_and_id_free(self):
        text = RECORD.read_text(encoding="utf-8")
        for marker in ("/tmp/", "/workspace", "/usr/", "/home/", "mizu-before-src"):
            self.assertNotIn(marker, text)
        for key in ("check_id", "check_command_id", "check_result_recompute",
                    "check_log_recompute", "command_id", "source_receipt_sha256",
                    "daemon_src_sha256_recomputed_from_git_tree"):
            self.assertNotIn(key, text)

    def test_tracked_only_verifier_passes_and_scopes_tree_checks(self):
        run = run_verifier(RECORD)
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["scope"], "tracked_only")
        self.assertTrue(summary["passed"])
        self.assertIn("before_tree_digest_from_git", summary["skipped"])
        self.assertNotIn("before_tree_digest_from_git", summary["checks"])

    @unittest.skipUnless((ROOT / ".git").exists(),
                         "source-tree recompute needs a full git checkout")
    def test_with_git_verifier_recomputes_tree_digests(self):
        if not anchor_commits_available(json.loads(RECORD.read_text(encoding="utf-8"))):
            self.skipTest("public anchor commits absent from this checkout")
        run = run_verifier(RECORD, ["--repo", str(ROOT)])
        self.assertEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertEqual(summary["scope"], "with_git")
        self.assertEqual(summary["skipped"], [])
        self.assertTrue(summary["checks"]["before_tree_digest_from_git"])
        self.assertTrue(summary["checks"]["after_tree_digest_from_git"])

    def test_falsified_numbers_fail_with_git(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        record["after"]["embedded_receipt"]["idle"]["median_seconds"] = 9.9
        record["after"]["embedded_receipt"]["source"]["source_revision"] = "1" * 40
        record["after"]["embedded_receipt"]["source"]["daemon_src_sha256"] = "f" * 64
        record["after"]["source_binding"]["source_revision"] = "1" * 40
        record["after"]["source_binding"]["daemon_src_sha256"] = "f" * 64
        record["after"]["embedded_receipt_sha256"] = canonical(record["after"]["embedded_receipt"])
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_record = fake_root / "docs" / "evidence" / "native-wake-cpu-evidence.json"
            fake_record.parent.mkdir(parents=True)
            fake_record.write_text(json.dumps(record), encoding="utf-8")
            run = subprocess.run(
                [sys.executable, str(VERIFIER), "--record", str(fake_record),
                 "--repo", str(ROOT)], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        summary = json.loads(run.stdout)
        self.assertFalse(summary["checks"]["after_tree_digest_from_git"])
        self.assertFalse(summary["checks"]["after_witness_consistent"])

    def test_matched_shape_validity_and_public_commit_anchor(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "native-wake-cpu-evidence-v1")
        m = record["measurement"]
        self.assertTrue(m["deployment_matches_after"])
        self.assertTrue(m["validity_matches_after"])
        self.assertEqual(m["validity"]["method"], "proc-per-pid")
        self.assertIs(m["validity"]["steady_state_observed"], True)
        self.assertLess(record["result"]["idle_median_seconds"]["after"],
                        record["result"]["idle_median_seconds"]["before"])
        for side in ("before", "after"):
            binding = record[side]["source_binding"]
            self.assertTrue(binding["recompute_from_public_commit"]["commit_is_public_ancestor_of_head"])
            self.assertEqual(binding["source_revision"],
                             record["witness"]["git_recompute"][0 if side == "before" else 1]["source_revision"])


if __name__ == "__main__":
    unittest.main()
