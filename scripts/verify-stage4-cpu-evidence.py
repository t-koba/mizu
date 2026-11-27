"""Verify the tracked stage-4 CPU evidence record.

Two scopes:

* Tracked-only (default): uses only files in the repository. It verifies no
  private paths, the schema, the harness digest against the tracked script,
  each embedded receipt's content digest and source identity against the
  detached source_binding (an intra-record consistency check), that each
  receipt's deployment and validity summary matches the recorded measurement
  and observes a proc-per-pid steady-state window, the recorded witness's
  consistency with the receipts,
  and the absence of worker-private IDs. The harness digest is SHA-256 over
  the script's bytes after normalizing CRLF line endings to LF, so a CRLF
  checkout passes while any other byte change fails. It does NOT claim to
  prove the source-tree link from fields that live in the record: that would
  be circular.

* With git (--repo <checkout>): additionally recomputes each source-tree
  digest from the named git commit with the harness's own recipe and compares
  it to both the detached binding and the embedded receipt's source digest.
  The commit tree is external to the record, so this check is non-circular
  and establishes which source a receipt names; it does NOT authenticate the
  recorded CPU numbers. A reader reproduces it with a full clone; the named
  commits are ancestors of HEAD in the public history.

A named record can be selected with --record; the default stays the stage-4
record. Prints one bounded JSON summary; exit 0 only when every reported check
passes. The already-captured measurement is reused; this never reruns it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "evidence" / "stage4-cpu-evidence.json"
PRIVATE_MARKERS = ("/tmp/", "/workspace", "/usr/", "/home/", "mizu-before-src")
PRIVATE_ID_KEYS = ("check_id", "check_command_id", "check_result_recompute",
                   "check_log_recompute", "command_id", "source_receipt_sha256",
                   "daemon_src_sha256_recomputed_from_git_tree")
SUPPORTED_SCHEMAS = ("stage4-cpu-evidence-v2", "native-wake-cpu-evidence-v1")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


DEPLOYMENT_FIELDS = ("projects", "retained_messages", "idle_seconds",
                     "window_seconds", "warmup_seconds", "repeats", "settled",
                     "python", "platform")


def canonical_sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def harness_digest(path: pathlib.Path) -> str:
    """SHA-256 of the harness with CRLF line endings normalized to LF."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_digest(src: pathlib.Path) -> str:
    """Same recipe as scripts/daemon-cpu.py::_source_digest."""
    digest = hashlib.sha256()
    for path in sorted((src / "mizu").rglob("*.py")):
        digest.update(str(path.relative_to(src)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def tree_digest_from_git(repo: pathlib.Path, revision: str) -> str:
    """Recompute the source-tree digest from an external git commit."""
    with tempfile.TemporaryDirectory(prefix="stage4-evidence-") as tmp:
        archive = subprocess.run(["git", "-C", str(repo), "archive", revision, "src"],
                                 check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True,
                       capture_output=True)
        return source_digest(pathlib.Path(tmp) / "src")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=None,
                        help="full git checkout to recompute source-tree digests from")
    parser.add_argument("--record", type=pathlib.Path, default=None,
                        help="evidence record to verify (defaults to stage-4)")
    args = parser.parse_args(argv)

    record_path = args.record or RECORD
    text = record_path.read_text(encoding="utf-8")
    record = json.loads(text)
    checks: dict[str, bool] = {}

    checks["no_private_paths"] = not any(m in text for m in PRIVATE_MARKERS)
    checks["schema"] = record["schema"] in SUPPORTED_SCHEMAS
    checks["no_worker_private_ids"] = not any(k in text for k in PRIVATE_ID_KEYS)

    harness = ROOT / record["measurement"]["harness"]
    checks["harness_digest"] = (harness_digest(harness)
                                == record["measurement"]["harness_script_sha256"])

    witness_rows = {row["side"]: row for row in record["witness"]["git_recompute"]}
    for side in ("before", "after"):
        binding = record[side]["source_binding"]
        claimed = binding["daemon_src_sha256"]
        checks[f"{side}_source_hex"] = bool(HEX64.fullmatch(claimed))
        checks[f"{side}_embedded_receipt_integrity"] = (
            canonical_sha(record[side]["embedded_receipt"])
            == record[side]["embedded_receipt_sha256"])
        receipt_source = record[side]["embedded_receipt"]["source"]
        checks[f"{side}_receipt_source_bound"] = (
            receipt_source["daemon_src_sha256"] == claimed
            and receipt_source["source_revision"] == binding["source_revision"])
        checks[f"{side}_tree_commit"] = bool(HEX40.fullmatch(binding["source_revision"]))
        # Consistency of the recorded (external-untrusted) witness with the
        # receipt claim. NOT a proof of the tree link by itself.
        checks[f"{side}_witness_consistent"] = (
            witness_rows[side]["tree_digest"] == claimed
            and witness_rows[side]["source_revision"] == binding["source_revision"]
            and witness_rows[side]["matched_receipt"] is True)

    # The embedded receipts, not the worker-authored measurement summary, own
    # shape and validity. Recompute the summary claims from each receipt so a
    # receipt that denies the steady-state precondition cannot be blessed by
    # an untouched summary. Missing fields fail closed via .get().
    receipts = [record[side].get("embedded_receipt", {})
                for side in ("before", "after")]
    after_receipt = record["after"].get("embedded_receipt", {})

    def receipt_deployment(receipt):
        return {k: receipt.get(k) for k in DEPLOYMENT_FIELDS}

    def receipt_validity(receipt):
        validity = receipt.get("validity") or {}
        return {"method": validity.get("method"),
                "steady_state_observed": validity.get("steady_state_observed")}

    m = record["measurement"]
    checks["shape_matched"] = (
        m.get("deployment") == receipt_deployment(after_receipt)
        and all(receipt_deployment(r) == m.get("deployment") for r in receipts))
    checks["validity_matched"] = (
        m.get("validity") == receipt_validity(after_receipt)
        and all(receipt_validity(r) == m.get("validity") for r in receipts))
    checks["validity_flags"] = all(
        receipt_validity(r)["method"] == "proc-per-pid"
        and receipt_validity(r)["steady_state_observed"] is True
        for r in receipts)

    skipped: list[str] = []
    if args.repo is None:
        skipped = ["before_tree_digest_from_git", "after_tree_digest_from_git",
                   "before_receipt_source_matches_git", "after_receipt_source_matches_git"]
    else:
        for side in ("before", "after"):
            revision = record[side]["source_binding"]["source_revision"]
            try:
                recomputed = tree_digest_from_git(args.repo, revision)
            except (subprocess.CalledProcessError, OSError):
                recomputed = None  # unresolvable commit is a failed check, not a crash
            checks[f"{side}_tree_digest_from_git"] = (
                recomputed == record[side]["source_binding"]["daemon_src_sha256"])
            checks[f"{side}_receipt_source_matches_git"] = (
                recomputed == record[side]["embedded_receipt"]["source"]["daemon_src_sha256"])

    summary = {"schema": record["schema"],
               "scope": "with_git" if args.repo is not None else "tracked_only",
               "checks": checks, "skipped": skipped,
               "passed": all(checks.values())}
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
