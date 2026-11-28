"""Tests for the tracked stage-4 CPU evidence record and its verifier.

The record is path-free and ID-free; its in-record checksums give intra-record
consistency, and ``--repo`` adds external git-tree source naming. A party that
can rewrite the record can refresh its checksums, so neither is an authenticity
proof of the recorded CPU numbers.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-stage4-cpu-evidence.py"
RECORD = ROOT / "docs" / "evidence" / "stage4-cpu-evidence.json"
NATIVE_RECORD = ROOT / "docs" / "evidence" / "native-wake-cpu-evidence.json"


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


def run_verifier_on_record(record, repo=None):
    """Run the verifier with --repo over a temporary record/harness tree."""
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
        return subprocess.run(
            [sys.executable, str(fake_root / "scripts" / "verify-stage4-cpu-evidence.py"),
             "--repo", str(repo if repo is not None else ROOT)], cwd=fake_root,
            capture_output=True, text=True)


def _source_digest(src):
    """Same recipe as the verifier's tree_digest_from_git after extraction."""
    digest = hashlib.sha256()
    for path in sorted((src / "mizu").rglob("*.py")):
        digest.update(str(path.relative_to(src)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_source_record(base_record, tmp):
    """Fabricate a record whose source identity is pinned to a real git commit.

    Returns (record, repo). Both sides' bindings, embedded sources and witness
    rows point at a disposable committed src/mizu tree, so git-backed checks
    can be exercised without ROOT having .git or the named public anchors.
    """
    repo = Path(tmp) / "source-repo"
    source = repo / "src" / "mizu" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    env_args = ["git", "-C", str(repo)]
    subprocess.run(env_args + ["init", "-q"], check=True, capture_output=True)
    subprocess.run(env_args + ["add", "src/mizu/sample.py"], check=True,
                   capture_output=True)
    subprocess.run(env_args + ["-c", "user.email=test@example.invalid", "-c",
                               "user.name=test", "commit", "-q", "-m", "source"],
                   check=True, capture_output=True)
    revision = subprocess.run(env_args + ["rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    digest = _source_digest(repo / "src")
    record = json.loads(json.dumps(base_record))
    for side in ("before", "after"):
        record[side]["source_binding"]["source_revision"] = revision
        record[side]["source_binding"]["daemon_src_sha256"] = digest
        emb = record[side]["embedded_receipt"]
        emb["source"]["source_revision"] = revision
        emb["source"]["daemon_src_sha256"] = digest
        record[side]["embedded_receipt_sha256"] = _canonical_sha(emb)
    for row in record["witness"]["git_recompute"]:
        row["source_revision"] = revision
        row["tree_digest"] = digest
    return record, repo


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
        run = run_verifier_on_record(record)
        self.assertNotEqual(run.returncode, 0)
        summary = json.loads(run.stdout)
        self.assertFalse(summary["checks"]["before_tree_digest_from_git"])
        self.assertFalse(summary["checks"]["before_witness_consistent"])

    def test_forged_receipt_source_fails_binding_check_with_git(self):
        # Refreshing the inline checksum over a forged embedded source must
        # not pass while the detached binding, witness and git tree are honest.
        # A disposable git repo supplies the source tree so this regression
        # runs even where ROOT has no .git or named anchor commits.
        with tempfile.TemporaryDirectory() as tmp:
            record, repo = git_source_record(
                json.loads(RECORD.read_text(encoding="utf-8")), tmp)
            source = record["before"]["embedded_receipt"]["source"]
            source["daemon_src_sha256"] = "f" * 64
            source["source_revision"] = "1" * 40
            record["before"]["embedded_receipt_sha256"] = _canonical_sha(
                record["before"]["embedded_receipt"])
            run = run_verifier_on_record(record, repo=repo)
        self.assertNotEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertFalse(summary["checks"]["before_receipt_source_bound"])
        self.assertFalse(summary["checks"]["before_receipt_source_matches_git"])
        self.assertTrue(summary["checks"]["before_tree_digest_from_git"])
        self.assertTrue(summary["checks"]["before_embedded_receipt_integrity"])
        self.assertTrue(summary["checks"]["before_witness_consistent"])
        self.assertTrue(summary["checks"]["after_receipt_source_bound"])
        self.assertFalse(summary["passed"])

    def test_forged_receipt_validity_fails_shape_checks_with_git(self):
        # A receipt that denies the steady-state precondition must not be
        # blessed by an untouched worker-authored measurement summary, even
        # with refreshed checksums and honest source/git bindings.
        with tempfile.TemporaryDirectory() as tmp:
            record, repo = git_source_record(
                json.loads(RECORD.read_text(encoding="utf-8")), tmp)
            for side in ("before", "after"):
                emb = record[side]["embedded_receipt"]
                emb["validity"]["method"] = "getrusage"
                emb["validity"]["steady_state_observed"] = False
                emb["settled"] = False
                record[side]["embedded_receipt_sha256"] = _canonical_sha(emb)
            run = run_verifier_on_record(record, repo=repo)
        self.assertNotEqual(run.returncode, 0, run.stderr)
        summary = json.loads(run.stdout)
        self.assertFalse(summary["checks"]["shape_matched"])
        self.assertFalse(summary["checks"]["validity_matched"])
        self.assertFalse(summary["checks"]["validity_flags"])
        self.assertTrue(summary["checks"]["before_embedded_receipt_integrity"])
        self.assertTrue(summary["checks"]["before_receipt_source_bound"])
        self.assertTrue(summary["checks"]["before_tree_digest_from_git"])
        self.assertTrue(summary["checks"]["after_tree_digest_from_git"])
        self.assertFalse(summary["passed"])

    def test_absent_noncritical_receipt_field_is_not_a_none_claim(self):
        # An older supported schema (native-wake) omits `python` from its
        # receipts. An absent field means "not recorded", not a None claim
        # that mismatches measurement.deployment; keeping it must pass shape.
        # Dropping a security-critical field (`settled`) must still fail.
        base = json.loads(RECORD.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            record = json.loads(json.dumps(base))
            for side in ("before", "after"):
                emb = record[side]["embedded_receipt"]
                emb.pop("python", None)
                record[side]["embedded_receipt_sha256"] = _canonical_sha(emb)
            path = Path(tmp) / "record.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            kept = run_verifier(["--record", str(path)])

            record = json.loads(json.dumps(base))
            record["before"]["embedded_receipt"].pop("settled")
            record["before"]["embedded_receipt_sha256"] = _canonical_sha(
                record["before"]["embedded_receipt"])
            path.write_text(json.dumps(record), encoding="utf-8")
            dropped = run_verifier(["--record", str(path)])

        self.assertEqual(kept.returncode, 0, kept.stderr)
        kept_summary = json.loads(kept.stdout)
        self.assertTrue(kept_summary["checks"]["shape_matched"])
        self.assertEqual(kept_summary["unattested_deployment"], ["python"])
        self.assertNotEqual(dropped.returncode, 0, dropped.stderr)
        self.assertFalse(json.loads(dropped.stdout)["checks"]["shape_matched"])

    def test_one_sided_deployment_field_fails_shape_and_is_reported(self):
        # A declared field recorded in only one receipt (checksum refreshed)
        # cannot establish a like-for-like before/after comparison: it must
        # fail shape_matched and appear in unattested_deployment. A field
        # absent on BOTH sides stays the reported-but-passing historical case.
        field = "retained_messages"
        base = json.loads(RECORD.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            record = json.loads(json.dumps(base))
            emb = record["before"]["embedded_receipt"]
            emb.pop(field)
            record["before"]["embedded_receipt_sha256"] = _canonical_sha(emb)
            path.write_text(json.dumps(record), encoding="utf-8")
            one_sided = run_verifier(["--record", str(path)])
        self.assertNotEqual(one_sided.returncode, 0, one_sided.stderr)
        summary = json.loads(one_sided.stdout)
        self.assertFalse(summary["checks"]["shape_matched"])
        self.assertIn(field, summary["unattested_deployment"])

    def test_native_wake_schema_fails_closed_on_settled_forge(self):
        # Seq 279 forge guard must hold on the second supported schema too:
        # dropping `settled` from a receipt and refreshing its checksum may
        # not hide the steady-state precondition behind "not recorded".
        base = json.loads(NATIVE_RECORD.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            shipped = run_verifier(["--record", str(path)])

            record = json.loads(json.dumps(base))
            for side in ("before", "after"):
                emb = record[side]["embedded_receipt"]
                emb.pop("settled")
                record[side]["embedded_receipt_sha256"] = _canonical_sha(emb)
            path.write_text(json.dumps(record), encoding="utf-8")
            forged = run_verifier(["--record", str(path)])

        self.assertEqual(shipped.returncode, 0, shipped.stderr)
        shipped_summary = json.loads(shipped.stdout)
        self.assertTrue(shipped_summary["checks"]["shape_matched"])
        self.assertEqual(shipped_summary["unattested_deployment"], ["python"])
        self.assertNotEqual(forged.returncode, 0, forged.stderr)
        self.assertFalse(json.loads(forged.stdout)["checks"]["shape_matched"])

    def test_malformed_records_fail_closed_without_a_traceback(self):
        """An untrusted malformed shape yields one bounded JSON summary, never
        an uncaught exception (whose traceback could leak private paths)."""
        mutations = {
            "top_level_list": lambda r: [],
            "schema_oversized": lambda r: r.__setitem__("schema", "x" * 12000),
            "schema_unsupported": lambda r: r.__setitem__("schema", "not-a-schema"),
            "deployment_not_dict": lambda r: r["measurement"].__setitem__("deployment", [1]),
            "measurement_not_dict": lambda r: r.__setitem__("measurement", "bad"),
            "validity_not_dict": lambda r: r["measurement"].__setitem__("validity", "bad"),
            "receipt_not_dict": lambda r: r["before"].__setitem__("embedded_receipt", "bad"),
            "receipt_validity_not_dict": lambda r: r["before"]["embedded_receipt"].__setitem__("validity", "bad"),
            "receipt_source_not_dict": lambda r: r["before"]["embedded_receipt"].__setitem__("source", "bad"),
            "binding_not_dict": lambda r: r["before"].__setitem__("source_binding", "bad"),
            "witness_not_list": lambda r: r["witness"].__setitem__("git_recompute", "bad"),
            "witness_row_not_dict": lambda r: r["witness"].__setitem__("git_recompute", ["bad"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                # Use a temporary record directly; no source checkout needed.
                record = json.loads(RECORD.read_text(encoding="utf-8"))
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "record.json"
                    path.write_text(json.dumps(mutate(record)), encoding="utf-8")
                    proc = subprocess.run(
                        [sys.executable, str(VERIFIER), "--record", str(path)],
                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                for marker in ("Traceback", "AttributeError", "TypeError", "KeyError"):
                    self.assertNotIn(marker, proc.stderr)
                # A malformed record must not control the summary size: only
                # a fixed diagnostic is emitted, never echoed record data.
                self.assertLess(len(proc.stdout), 1024, proc.stdout[:200])
                summary = json.loads(proc.stdout)  # must be parseable JSON
                self.assertIs(summary["passed"], False)
                self.assertIs(summary["checks"]["record_structure"], False)
                self.assertTrue(summary["structure_reason"])

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
