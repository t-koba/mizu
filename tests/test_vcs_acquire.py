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


BASE_A = "c" * 40
BASE_B = "d" * 40


def receipt(sha=SHA_A, content=CONTENT, identity=PROPOSAL_ID,
            scope="head", base_sha=None):
    payload = {"id": identity, "sha": sha, "scope": scope,
               "digest": vcs.acquire_digest(content), "content": content}
    if scope == "full":
        payload["base_sha"] = BASE_A if base_sha is None else base_sha
    return payload


def params(identity=PROPOSAL_ID, sha=SHA_A, scope="head", base_sha=None):
    request = {"id": identity, "sha": sha, "scope": scope}
    if base_sha is not None:
        request["base_sha"] = base_sha
    return request


class AcquireTests(Fixture):
    def test_acquire_returns_bound_receipt(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt())))
        out = vcs.read_via(config.vcs, "acquire",
                           {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"})
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
                           {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"})
        self.assertEqual(out["sha"], SHA_A)

    def test_acquire_validates_before_dispatch(self):
        # A command that always fails proves no adapter call happens.
        config = dataclasses.replace(
            self.config, vcs={"command": ["/nonexistent-adapter"],
                             "timeout_seconds": 5, "max_bytes": 524288})
        scoped = {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"}
        for params in ({"sha": SHA_A}, {"id": PROPOSAL_ID},
                       {"id": "", "sha": SHA_A},
                       {"id": PROPOSAL_ID, "sha": "xyz"},
                       dict(scoped, scope="everything"),
                       dict(scoped, base_sha=BASE_A),
                       {"id": PROPOSAL_ID, "sha": SHA_A}):
            with self.assertRaises(Denied):
                vcs.read_via(config.vcs, "acquire", params)

    def test_moved_head_fails_closed_then_retry_succeeds(self):
        # The head moved mid-flight: the adapter serves a stale revision.
        stale = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(sha=SHA_B))))
        with self.assertRaises(Denied):
            vcs.read_via(stale.vcs, "acquire",
                         {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"})
        # The caller re-observes proposals and retries with the fresh sha.
        fresh = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(sha=SHA_B))))
        out = vcs.read_via(fresh.vcs, "acquire",
                           {"id": PROPOSAL_ID, "sha": SHA_B,
                            "scope": "head"})
        self.assertEqual(out["sha"], SHA_B)

    def test_digest_mismatch_refused(self):
        bad = receipt()
        bad["digest"] = "0" * 64
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(bad)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire",
                         {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"})

    def test_malformed_shapes_refused(self):
        good = receipt()
        variants = [dict(good, id="other"),
                    dict(good, sha=SHA_B),
                    dict(good, content=123),
                    dict(good, content="x" * (vcs.MAX_ACQUIRE_CONTENT + 1)),
                    {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"},
                    [good]]
        for payload in variants:
            config = dataclasses.replace(
                self.config, vcs=adapter_settings(acquire_code(payload)))
            with self.assertRaises(Denied):
                vcs.read_via(config.vcs, "acquire",
                             {"id": PROPOSAL_ID, "sha": SHA_A, "scope": "head"})

    def test_fork_head_acquires_by_id_and_sha(self):
        # A fork head (repo outside the base) resolves by id and sha
        # alone: the adapter sees no branch and still serves the content.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "assert req == {'op': 'acquire', 'id': %r, 'sha': %r, 'scope': 'head'}, req; "
                "print(json.dumps(%r))" % (
                    "forge:fork-owner/repo#7", SHA_A,
                    receipt(sha=SHA_A, identity="forge:fork-owner/repo#7")))
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        out = vcs.read_via(config.vcs, "acquire",
                           {"id": "forge:fork-owner/repo#7", "sha": SHA_A,
                            "scope": "head"})
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


class AcquireToolTests(Fixture):
    def test_tool_serves_acquire_by_id_and_sha(self):
        # Regression: id/acquire were unreachable through the tool
        # schema even though read_via served them. The adapter echoes
        # the requested id/sha with digest-bound content.
        import uuid
        from mizu.fs import mkdir
        from mizu.runtime import Context
        digest = vcs.acquire_digest(CONTENT)
        code = ("import sys,json; req=json.load(sys.stdin); "
                "print(json.dumps({'id': req['id'], 'sha': req['sha'], "
                "'scope': req['scope'], "
                "'digest': %r, 'content': %r}))" % (digest, CONTENT))
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        role = config.roles["worker"]
        role = dataclasses.replace(
            role, capabilities=tuple(list(role.capabilities) + ["vcs_read"]))
        config = dataclasses.replace(
            config, roles={**config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        out = ctx.handle("vcs_read", {"op": "acquire",
                                      "id": PROPOSAL_ID, "sha": SHA_A,
                                      "scope": "head"})
        self.assertEqual(out["result"]["id"], PROPOSAL_ID)
        self.assertEqual(out["result"]["sha"], SHA_A)
        self.assertEqual(out["result"]["digest"], digest)
        self.assertEqual(out["result"]["content"], CONTENT)


class AcquireScopeTests(Fixture):
    def test_full_scope_pins_base_revision(self):
        content = "commit c1\ncommit c2\ncommit c3 head\n"
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(scope="full", content=content,
                                     base_sha=BASE_A))))
        out = vcs.read_via(config.vcs, "acquire",
                           params(scope="full", base_sha=BASE_A))
        self.assertEqual(out["scope"], "full")
        self.assertEqual(out["base_sha"], BASE_A)
        self.assertEqual(out["digest"], vcs.acquire_digest(content))
        self.assertEqual(out["content"], content)

    def test_unpinned_full_binds_served_base(self):
        # The caller did not pin a base: the adapter binds the current
        # one and the receipt records what was actually served.
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(scope="full", content=CONTENT))))
        out = vcs.read_via(config.vcs, "acquire", params(scope="full"))
        self.assertEqual(out["scope"], "full")
        self.assertEqual(out["base_sha"], BASE_A)

    def test_changed_base_with_unchanged_head_changes_content(self):
        # Retargeting the base changes proposal content with the same
        # head sha: each base revision acquires its own digest, so a
        # stale base binding never passes as fresh evidence.
        first = receipt(scope="full", content="diff base cccc\n+a\n",
                        base_sha=BASE_A)
        second = receipt(scope="full", content="diff base dddd\n+a\n+b\n",
                         base_sha=BASE_B)
        code = ("import sys,json; req=json.load(sys.stdin); "
                "table=%r; out=dict(table[req.get('base_sha')]); "
                "out['id'] = req['id']; out['sha'] = req['sha']; "
                "print(json.dumps(out))" % {BASE_A: first, BASE_B: second})
        config = dataclasses.replace(self.config, vcs=adapter_settings(code))
        before = vcs.read_via(config.vcs, "acquire",
                              params(scope="full", base_sha=BASE_A))
        after = vcs.read_via(config.vcs, "acquire",
                             params(scope="full", base_sha=BASE_B))
        self.assertEqual(after["sha"], before["sha"])
        self.assertNotEqual(after["base_sha"], before["base_sha"])
        self.assertNotEqual(after["digest"], before["digest"])

    def test_head_scope_is_not_the_whole_proposal(self):
        # A multi-commit proposal: head scope carries the head commit
        # only, full scope carries every commit. Labels differ and the
        # head content is a strict subset, never the whole proposal.
        commits = ["commit c1\n+one\n", "commit c2\n+two\n",
                   "commit c3 head\n+three\n"]
        table = {"head": receipt(content=commits[2]),
                 "full": receipt(scope="full",
                                 content="".join(commits))}
        code = ("import sys,json; req=json.load(sys.stdin); "
                "table=%r; out=dict(table[req['scope']]); "
                "out['id'] = req['id']; out['sha'] = req['sha']; "
                "print(json.dumps(out))" % table)
        config = dataclasses.replace(self.config, vcs=adapter_settings(code))
        head = vcs.read_via(config.vcs, "acquire", params(scope="head"))
        full = vcs.read_via(config.vcs, "acquire",
                            params(scope="full", base_sha=BASE_A))
        self.assertEqual(head["scope"], "head")
        self.assertIsNone(head["base_sha"])
        self.assertIn(commits[0], full["content"])
        self.assertNotIn(commits[0], head["content"])
        self.assertNotEqual(full["digest"], head["digest"])

    def test_scope_echo_mismatch_refused(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(receipt())))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire", params(scope="full"))

    def test_full_without_echoed_base_refused(self):
        payload = receipt(scope="full", content=CONTENT)
        del payload["base_sha"]
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(payload)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire", params(scope="full"))

    def test_head_with_echoed_base_refused(self):
        payload = receipt(content=CONTENT)
        payload["base_sha"] = BASE_A
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(acquire_code(payload)))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire", params(scope="head"))

    def test_pinned_base_mismatch_refused(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(
                acquire_code(receipt(scope="full", content=CONTENT,
                                     base_sha=BASE_B))))
        with self.assertRaises(Denied):
            vcs.read_via(config.vcs, "acquire",
                         params(scope="full", base_sha=BASE_A))

    def test_tool_serves_full_scope_with_base_pin(self):
        # scope and base_sha travel through the tool schema to the
        # adapter and the bound pair surfaces in the tool result.
        import uuid
        from mizu.fs import mkdir
        from mizu.runtime import Context
        content = "commit c1\ncommit c3 head\n"
        digest = vcs.acquire_digest(content)
        code = ("import sys,json; req=json.load(sys.stdin); "
                "print(json.dumps({'id': req['id'], 'sha': req['sha'], "
                "'scope': req['scope'], 'base_sha': req['base_sha'], "
                "'digest': %r, 'content': %r}))" % (digest, content))
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(code))
        role = config.roles["worker"]
        role = dataclasses.replace(
            role, capabilities=tuple(list(role.capabilities) + ["vcs_read"]))
        config = dataclasses.replace(
            config, roles={**config.roles, "worker": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        ctx = Context(config, self.project, role, run,
                      self.project.snapshots.get(), self.project.workspace)
        out = ctx.handle("vcs_read", {"op": "acquire", "id": PROPOSAL_ID,
                                      "sha": SHA_A, "scope": "full",
                                      "base_sha": BASE_A})
        self.assertEqual(out["result"]["scope"], "full")
        self.assertEqual(out["result"]["base_sha"], BASE_A)
        self.assertEqual(out["result"]["digest"], digest)
