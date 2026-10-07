"""Exact-revision acquisition: materialize a proposal head by id and sha. Offline only."""
import dataclasses
import json
import sys
import unittest
from pathlib import Path

from support import Fixture
from mizu import vcs
from mizu.errors import Denied

SHA_A = "a" * 40
SHA_B = "b" * 40
PROPOSAL_ID = "forge:owner/repo#1"
CONTENT = "diff --git a/x b/x\n+exact revision under assessment\n"


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def acquire_code(payload):
    import tempfile
    path = Path(tempfile.mkdtemp()) / "acquired.json"
    path.write_text(json.dumps(payload))
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(open(%r).read())" % str(path))


def receipt(sha=SHA_A, content=CONTENT, identity=PROPOSAL_ID):
    return {"id": identity, "sha": sha,
            "digest": vcs.acquire_digest(content), "content": content}


class AcquireTests(Fixture):
    def test_acquire_returns_bound_receipt(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt())))
        out = vcs.read_via(config.vcs, "acquire",
                           {"id": PROPOSAL_ID, "sha": SHA_A})
        self.assertEqual(out["op"], "acquire")
        self.assertEqual(out["id"], PROPOSAL_ID)
        self.assertEqual(out["sha"], SHA_A)
        self.assertEqual(out["digest"], vcs.acquire_digest(CONTENT))
        self.assertEqual(out["content"], CONTENT)
        self.assertEqual(out["trust"], "external-untrusted")

    def test_acquire_needs_no_branch(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt())))
        out = vcs.read_via(config.vcs, "acquire",
                           {"id": PROPOSAL_ID, "sha": SHA_A})
        self.assertEqual(out["sha"], SHA_A)

    def test_acquire_validates_before_dispatch(self):
        # A command that always fails proves no adapter call happens.
        config = dataclasses.replace(
            self.config, vcs={"command": ["/nonexistent-adapter"],
                             "timeout_seconds": 5, "max_bytes": 524288})
        for params in ({"sha": SHA_A}, {"id": PROPOSAL_ID},
                       {"id": "", "sha": SHA_A},
                       {"id": PROPOSAL_ID, "sha": "xyz"}):
            with self.assertRaises(Denied):
                vcs.read_via(config.vcs, "acquire", params)

    def test_moved_head_fails_closed_then_retry_succeeds(self):
        # The head moved mid-flight: the adapter serves a stale revision.
        stale = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(sha=SHA_B))))
        with self.assertRaises(Denied):
            vcs.read_via(stale.vcs, "acquire",
                         {"id": PROPOSAL_ID, "sha": SHA_A})
        # The caller re-observes proposals and retries with the fresh sha.
        fresh = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(sha=SHA_B))))
        out = vcs.read_via(fresh.vcs, "acquire",
                           {"id": PROPOSAL_ID, "sha": SHA_B})
        self.assertEqual(out["sha"], SHA_B)

    def test_digest_mismatch_refused(self):
        bad = receipt()
        bad["digest"] = "0" * 64
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(bad)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire",
                         {"id": PROPOSAL_ID, "sha": SHA_A})

    def test_malformed_shapes_refused(self):
        good = receipt()
        variants = [dict(good, id="other"),
                    dict(good, sha=SHA_B),
                    dict(good, content=123),
                    dict(good, content="x" * (vcs.MAX_ACQUIRE_CONTENT + 1)),
                    {"id": PROPOSAL_ID, "sha": SHA_A},
                    [good]]
        for payload in variants:
            config = dataclasses.replace(
                self.config, vcs=adapter_settings(acquire_code(payload)))
            with self.assertRaises(Denied):
                vcs.read_via(config.vcs, "acquire",
                             {"id": PROPOSAL_ID, "sha": SHA_A})

    def test_fork_head_acquires_by_id_and_sha(self):
        # A fork head (repo outside the base) resolves by id and sha
        # alone: the adapter sees no branch and still serves the content.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "assert req == {'op': 'acquire', 'id': %r, 'sha': %r}, req; "
                "print(json.dumps(%r))" % (
                    "forge:fork-owner/repo#7", SHA_A,
                    receipt(sha=SHA_A, identity="forge:fork-owner/repo#7")))
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        out = vcs.read_via(config.vcs, "acquire",
                           {"id": "forge:fork-owner/repo#7", "sha": SHA_A})
        self.assertEqual(out["id"], "forge:fork-owner/repo#7")
        self.assertEqual(out["content"], CONTENT)

    def test_acquire_is_read_only(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(receipt())))
        with self.assertRaises(Denied):
            vcs.publish_via(config.vcs, "acquire",
                            {"branch": "x", "code_digest": "0" * 64})


if __name__ == "__main__":
    unittest.main()
