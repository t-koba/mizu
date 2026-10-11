"""vcs_read CI artifacts: list recorded files, read one text file bound to its digest. Offline only."""
import dataclasses
import json
import sys
import unittest

from support import Fixture
from mizu import vcs
from mizu.errors import Denied

SHA_A = "a" * 40


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def serving_code(payload):
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(json.dumps(%s))" % json.dumps(payload))


class ArtifactTests(Fixture):
    def test_artifacts_list_normalized(self):
        payload = {"artifacts": [
            {"artifact": "target/perf/audit.jsonl", "size": 12},
            {"artifact": "profiles/callgrind.out"},
        ]}
        config = dataclasses.replace(self.config, vcs=adapter_settings(serving_code(payload)))
        out = vcs.read_via(config.vcs, "artifacts", {"branch": "mizu"})
        self.assertEqual(out["branch"], "mizu")
        self.assertEqual([r["artifact"] for r in out["artifacts"]],
                         ["target/perf/audit.jsonl", "profiles/callgrind.out"])
        self.assertEqual(out["trust"], "external-untrusted")

    def test_artifact_read_digest_bound(self):
        content = '{"bench": 1}\n'
        payload = {"artifact": "target/perf/audit.jsonl",
                   "digest": vcs.acquire_digest(content), "content": content}
        config = dataclasses.replace(self.config, vcs=adapter_settings(serving_code(payload)))
        out = vcs.read_via(config.vcs, "artifact",
                           {"branch": "mizu", "artifact": "target/perf/audit.jsonl"})
        self.assertEqual(out["content"], content)
        self.assertEqual(out["digest"], vcs.acquire_digest(content))
        self.assertEqual(out["trust"], "external-untrusted")

    def test_traversal_and_bad_names_refused_before_dispatch(self):
        config = dataclasses.replace(
            self.config, vcs={"command": ["/nonexistent-adapter"],
                             "timeout_seconds": 5, "max_bytes": 524288})
        for bad in ("", "../escape", "/abs", "a\\b", "a/./b", "a//b", "x\x1b[2K"):
            with self.assertRaises(Denied, msg=repr(bad)):
                vcs.read_via(config.vcs, "artifact", {"branch": "mizu", "artifact": bad})
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "artifacts", {"branch": ""})

    def test_name_and_digest_mismatch_refused(self):
        content = "measured\n"
        good_digest = vcs.acquire_digest(content)
        # Wrong echo name.
        payload = {"artifact": "other.jsonl", "digest": good_digest, "content": content}
        config = dataclasses.replace(self.config, vcs=adapter_settings(serving_code(payload)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "artifact",
                         {"branch": "mizu", "artifact": "target/perf/audit.jsonl"})
        # Wrong digest echo.
        payload = {"artifact": "target/perf/audit.jsonl", "digest": "0" * 64, "content": content}
        config = dataclasses.replace(self.config, vcs=adapter_settings(serving_code(payload)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "artifact",
                         {"branch": "mizu", "artifact": "target/perf/audit.jsonl"})
        # Oversize content is refused by shape validation alone (no spawn).
        with self.assertRaises(Denied):
            vcs.parse_artifact({"artifact": "target/perf/audit.jsonl",
                                "digest": good_digest,
                                "content": "x" * (vcs.MAX_ARTIFACT_CONTENT + 1)},
                               name="target/perf/audit.jsonl")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
