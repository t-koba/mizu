"""Verify the tracked stage-4 CPU evidence record from tracked files only.

Reads docs/evidence/stage4-cpu-evidence.json and checks, without git or network:
no private paths; the harness digest against the tracked script; each embedded
receipt's content digest; that each recorded source-tree digest agrees with the
witnessed recompute value; matched deployment shape and validity flags.

The witness that recomputed the source-tree digests from the named git commits
is recorded in the record's verification section with a dereference rule; this
script reuses that already-captured result and never reruns the measurement.
Prints one bounded JSON summary; exit 0 only when every check passes.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "evidence" / "stage4-cpu-evidence.json"
PRIVATE_MARKERS = ("/tmp/", "/workspace", "/usr/", "/home/", "mizu-before-src")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def main() -> int:
    text = RECORD.read_text(encoding="utf-8")
    record = json.loads(text)
    checks: dict[str, bool] = {}

    checks["no_private_paths"] = not any(marker in text for marker in PRIVATE_MARKERS)

    harness = ROOT / record["measurement"]["harness"]
    harness_sha = hashlib.sha256(harness.read_bytes()).hexdigest()
    checks["harness_digest"] = harness_sha == record["measurement"]["harness_script_sha256"]

    for side in ("before", "after"):
        binding = record[side]["source_binding"]
        checks[f"{side}_source_digest"] = (
            bool(HEX64.fullmatch(binding["daemon_src_sha256"]))
            and binding["daemon_src_sha256"]
            == binding["daemon_src_sha256_recomputed_from_git_tree"])
        checks[f"{side}_embedded_receipt"] = (
            hashlib.sha256(json.dumps(record[side]["embedded_receipt"], sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
            == record[side]["embedded_receipt_sha256"])
        checks[f"{side}_tree_commit"] = bool(HEX40.fullmatch(binding["source_revision"]))

    m = record["measurement"]
    checks["shape_matched"] = bool(m["deployment_matches_after"])
    checks["validity_matched"] = bool(m["validity_matches_after"])
    checks["validity_flags"] = (m["validity"]["method"] == "proc-per-pid"
                                and m["validity"]["steady_state_observed"] is True)

    summary = {"schema": record["schema"], "checks": checks,
               "passed": all(checks.values())}
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
