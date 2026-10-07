"""Proposal disposition: close/merge behind grant plus GO approval. Offline only."""
import dataclasses
import json
import sys
import unittest
import uuid

from support import Fixture
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir
from mizu.runtime import Context, check_consult_role

SHA_A = "a" * 40
SHA_B = "b" * 40
PROPOSAL_ID = "forge:owner/repo#1"


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def dispose_code(payload):
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "disposed.json"
    path.write_text(json.dumps(payload))
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(open(%r).read())" % str(path))


def granted(code):
    settings = adapter_settings(code)
    settings["dispose_grant"] = True
    return settings


def echo(identity=PROPOSAL_ID, sha=SHA_A, state="closed"):
    return {"id": identity, "sha": sha, "state": state}


def with_caps(fixture, name, extra):
    role = fixture.config.roles[name]
    new_role = dataclasses.replace(role, capabilities=tuple(list(role.capabilities) + extra))
    config = dataclasses.replace(fixture.config,
                                 roles={**fixture.config.roles, name: new_role})
    return config, new_role


def make_context(fixture, config, role, workspace=None):
    run = fixture.project.root / "runs" / uuid.uuid4().hex
    mkdir(run)
    snap = fixture.project.snapshots.get()
    ws = workspace or fixture.project.workspace
    return Context(config, fixture.project, role, run, snap, ws)


def current_digest(fixture):
    captured = fixture.project.snapshots.capture_files(fixture.project.workspace)
    assert not captured["skipped"]
    return captured["code_digest"]


def approve(fixture, branch, digest):
    record = fixture.project.insights.submit(
        source="operator", title=f"GO {branch}",
        body=f"Ship it.\ndigest: {digest}\n",
        base_snapshot=None, run=None)
    fixture.project.insights.decide(record["id"], "accept", "reviewed", "", "operator")
    return record


class VcsDisposeTests(Fixture):
    def test_dispose_refused_without_grant(self):
        with self.assertRaisesRegex(Denied, "not granted"):
            vcs.dispose_via(adapter_settings(dispose_code(echo())),
                            "close", PROPOSAL_ID, SHA_A)

    def test_unknown_op_refused(self):
        with self.assertRaises(Denied):
            vcs.dispose_via(granted(dispose_code(echo())),
                            "reopen", PROPOSAL_ID, SHA_A)

    def test_bad_params_refused_before_dispatch(self):
        settings = {"command": ["/nonexistent-adapter"],
                    "timeout_seconds": 5, "max_bytes": 524288,
                    "dispose_grant": True}
        for op, identity, sha in (("close", "", SHA_A),
                                  ("close", PROPOSAL_ID, "xyz"),
                                  ("close", None, SHA_A)):
            with self.assertRaises(Denied):
                vcs.dispose_via(settings, op, identity, sha)

    def test_close_receipt(self):
        out = vcs.dispose_via(granted(dispose_code(echo())),
                              "close", PROPOSAL_ID, SHA_A)
        self.assertEqual(out, {"op": "close", "id": PROPOSAL_ID,
                               "sha": SHA_A, "state": "closed",
                               "trust": "external-untrusted"})

    def test_merge_receipt(self):
        out = vcs.dispose_via(
            granted(dispose_code(echo(state="merged"))),
            "merge", PROPOSAL_ID, SHA_A)
        self.assertEqual(out["state"], "merged")

    def test_moved_head_fails_closed(self):
        settings = granted(dispose_code(echo(sha=SHA_B)))
        with self.assertRaisesRegex(Denied, "expected sha"):
            vcs.dispose_via(settings, "close", PROPOSAL_ID, SHA_A)

    def test_externally_superseded_fails_closed(self):
        # The forge resolved the proposal another way: a close must not
        # claim a merge, and a merge must not claim a close.
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="merged"))),
                            "close", PROPOSAL_ID, SHA_A)
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="closed"))),
                            "merge", PROPOSAL_ID, SHA_A)
        # Still open means the action did not take.
        with self.assertRaisesRegex(Denied, "terminal"):
            vcs.dispose_via(granted(dispose_code(echo(state="open"))),
                            "merge", PROPOSAL_ID, SHA_A)

    def test_malformed_echo_refused(self):
        for payload in ({"id": PROPOSAL_ID, "sha": SHA_A},
                        {"id": PROPOSAL_ID, "sha": SHA_A, "state": "closed",
                         "extra": 1},
                        {"id": PROPOSAL_ID, "sha": SHA_A, "state": "wip"},
                        {"id": PROPOSAL_ID, "sha": SHA_A, "state": None},
                        ["closed"]):
            with self.assertRaises(Denied):
                vcs.dispose_via(granted(dispose_code(payload)),
                                "close", PROPOSAL_ID, SHA_A)

    def test_unapproved_dispose_refused_without_spawn(self):
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        settings = granted(dispose_code(echo()))
        config = dataclasses.replace(config, vcs=settings)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "recorded human approval"):
            ctx.handle("vcs_dispose", {"op": "close", "id": PROPOSAL_ID,
                                       "sha": SHA_A, "branch": "main"})
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def test_stale_digest_refused(self):
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(config, vcs=granted(dispose_code(echo())))
        approve(self, "main", "0" * 64)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "stale"):
            ctx.handle("vcs_dispose", {"op": "close", "id": PROPOSAL_ID,
                                       "sha": SHA_A, "branch": "main"})

    def test_approved_dispose_resolves(self):
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(config, vcs=granted(dispose_code(echo())))
        digest = current_digest(self)
        rec = approve(self, "main", digest)
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_dispose", {"op": "close", "id": PROPOSAL_ID,
                                         "sha": SHA_A, "branch": "main"})
        self.assertTrue(out["disposed"])
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["approval"], rec["id"])
        self.assertEqual(out["code_digest"], digest)
        evidence = json.loads((ctx.run_dir / "vcs-dispose.json").read_text())
        self.assertEqual(evidence["approval"], rec["id"])
        self.assertEqual(evidence["state"], "closed")


class VcsDisposeRoleTests(Fixture):
    def test_dispose_requires_write_workspace(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"decide"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish", "vcs_dispose"]',
            1)
        if patched == text:
            self.skipTest("maintainer capability line shape changed")
        path = self.root / "config/dispose-decide.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaisesRegex(ConfigError, "self-approval"):
            load(path)

    def test_dispose_grant_loads(self):
        text = self.file.read_text()
        self.assertIn("[vcs]", text)
        path = self.root / "config/dispose-grant.toml"
        path.write_text(text.replace("retire_grant = false",
                                     "retire_grant = false\ndispose_grant = true", 1))
        from mizu.config import load
        config = load(path)
        self.assertTrue(config.vcs["dispose_grant"])
        bad = self.root / "config/dispose-grant-bad.toml"
        bad.write_text(text.replace("retire_grant = false",
                                    "retire_grant = false\ndispose_grant = \"yes\"", 1))
        with self.assertRaises(ConfigError):
            load(bad)

    def test_consult_cannot_hold_dispose(self):
        base = self.config.roles["consult"]
        bad = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_dispose"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", bad)


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class VcsDisposeRecoveryTests(Fixture):
    def test_changed_head_retry_recovers(self):
        # The assessed head moved between observation and disposition:
        # the first attempt fails closed with no evidence, and a retry
        # at the re-observed head succeeds. The adapter answers a stale
        # sha with the moved head and confirms the fresh one.
        code = ("import sys,json; req=json.load(sys.stdin); "
                "echo = %r if req['sha'] == %r else req['sha']; "
                "print(json.dumps({'id': req['id'], 'sha': echo, "
                "'state': 'closed'}))" % (SHA_B, SHA_A))
        config, role = with_caps(self, "worker", ["vcs_dispose"])
        config = dataclasses.replace(config, vcs=granted(code))
        digest = current_digest(self)
        approve(self, "main", digest)
        ctx = make_context(self, config, role)
        args = {"op": "close", "id": PROPOSAL_ID,
                "sha": SHA_A, "branch": "main"}
        with self.assertRaisesRegex(Denied, "expected sha"):
            ctx.handle("vcs_dispose", dict(args))
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())
        out = ctx.handle("vcs_dispose", {**args, "sha": SHA_B})
        self.assertTrue(out["disposed"])
        self.assertEqual(out["sha"], SHA_B)
        self.assertEqual(out["state"], "closed")
