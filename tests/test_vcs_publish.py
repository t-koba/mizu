"""M2 step 1: vcs_read/vcs_publish split with GO<branch> approval gate. Offline only."""
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


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


READ_OK = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'checks': []}))")
PUSH_OK = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'digest': req['digest'], 'pushed': True}))")
PUSH_WRONG_DIGEST = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'digest': '" + "f" * 64 + "', 'pushed': True}))")
PUSH_NO_DIGEST = ("import sys,json; req=json.load(sys.stdin); "
           "print(json.dumps({'op': req['op'], 'branch': req['branch'], 'pushed': True}))")


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


def approve(fixture, branch, digest, action="accept", source="operator", run="operator"):
    record = fixture.project.insights.submit(
        source=source, title=f"GO {branch}",
        body=f"Ship it.\ndigest: {digest}\n",
        base_snapshot=None, run=None)
    fixture.project.insights.decide(record["id"], action, "reviewed", "", run)
    return record


class VcsPublishTests(Fixture):
    def test_unapproved_push_refused_without_spawn(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        ctx = make_context(self, config, role)
        # No GO insight at all; adapter must never spawn (no evidence file).
        with self.assertRaisesRegex(Denied, "recorded human approval"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertFalse((ctx.run_dir / "vcs-publish.json").exists())

    def test_stale_digest_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", "0" * 64)
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "stale"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_accepted_digest_publishes(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        digest = current_digest(self)
        rec = approve(self, "main", digest)
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertTrue(out["published"])
        self.assertEqual(out["branch"], "main")
        self.assertEqual(out["code_digest"], digest)
        self.assertEqual(out["approval"], rec["id"])
        self.assertEqual(out["trust"], "external-untrusted")
        evidence = json.loads((ctx.run_dir / "vcs-publish.json").read_text())
        self.assertEqual(evidence["approval"], rec["id"])

    def test_branch_mismatch_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self))
        ctx = make_context(self, config, role)
        with self.assertRaises(Denied):
            ctx.handle("vcs_publish", {"op": "push", "branch": "other"})

    def test_vcs_read_cannot_publish(self):
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs=adapter_settings(READ_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_read", {"op": "status", "branch": "main"})
        self.assertEqual(out["trust"], "external-untrusted")
        with self.assertRaisesRegex(Denied, "cannot publish"):
            vcs.read_via(config.vcs, "push", {"branch": "main"})
        with self.assertRaisesRegex(Denied, "cannot publish"):
            vcs.publish_via(config.vcs, "status", {"branch": "main", "code_digest": "a" * 64})
        # vcs_read capability alone cannot reach publish (capability gate).
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_requires_grant_and_write(self):
        ctx = self.context("worker")
        self.assertNotIn("vcs_publish", ctx.role.capabilities)
        with self.assertRaises(Denied):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        # Read role holding publish is refused at call time (writable gate).
        base = self.config.roles["reviewer"]
        bad = dataclasses.replace(base, capabilities=tuple(list(base.capabilities) + ["vcs_publish"]))
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = self.project.snapshots.get()
        # workspace for read role materialized input
        from mizu.fs import mkdir as _mkdir
        _mkdir(run / "input")
        ctx2 = Context(dataclasses.replace(self.config, vcs=adapter_settings(PUSH_OK)),
                       self.project, bad, run, snap, run / "input")
        with self.assertRaisesRegex(Denied, "[Ww]ritable"):
            ctx2.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_on_read_role_refused_at_load(self):
        text = self.file.read_text()
        needle = '[roles.reviewer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"submit_insight"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "submit_insight", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "submit_insight", "finish", "vcs_publish"]',
            1)
        if patched == text:
            self.skipTest("reviewer capability line shape changed")
        path = self.root / "config/publish-read.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaises(ConfigError):
            load(path)

    def test_consult_cannot_hold_publish(self):
        base = self.config.roles["consult"]
        bad = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_publish"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", bad)
        # vcs_read stays allowed on consult (read-only).
        ok = dataclasses.replace(base, capabilities=("files", "read", "finish", "vcs_read"))
        check_consult_role("consult", ok)

    def test_ci_failure_deduplicated(self):
        sha = "a" * 40
        first = vcs.record_ci_result(self.project, branch="main", sha=sha,
                                     check="unit", state="failure", url="https://x/log")
        second = vcs.record_ci_result(self.project, branch="main", sha=sha,
                                      check="unit", state="failure", url="https://x/log")
        self.assertTrue(first["recorded"])
        self.assertEqual(first["id"], second["id"])
        pending = [i for i in self.project.insights.list(pending=True, limit=1000)
                   if i["id"] == first["id"]]
        self.assertEqual(len(pending), 1)
        ignored = vcs.record_ci_result(self.project, branch="main", sha=sha,
                                       check="unit", state="success")
        self.assertFalse(ignored["recorded"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())


class VcsApprovalChannelTests(Fixture):
    def test_model_submitted_go_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self), source="worker", run="operator")
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "operator channel"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_model_decide_refused(self):
        config, role = with_caps(self, "worker", ["vcs_publish"])
        config = dataclasses.replace(config, vcs=adapter_settings(PUSH_OK))
        approve(self, "main", current_digest(self), source="operator", run="run-abc123")
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "operator channel"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_role_must_not_hold_decide_or_submit(self):
        text = self.file.read_text()
        needle = '[roles.maintainer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"decide"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish"]',
            'capabilities = ["files", "read", "diff", "exec", "experiment", "verify", "insights", "decide", "consult", "finish", "vcs_publish"]',
            1)
        if patched == text:
            self.skipTest("maintainer capability line shape changed")
        path = self.root / "config/publish-decide.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaisesRegex(ConfigError, "self-approval"):
            load(path)


class VcsDigestBindingTests(Fixture):
    def test_adapter_must_echo_approved_digest(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        rec = approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_OK))
        ctx = make_context(self, config, role)
        out = ctx.handle("vcs_publish", {"op": "push", "branch": "main"})
        self.assertEqual(out["code_digest"], digest)
        self.assertEqual(out["result"]["digest"], digest)

    def test_mismatched_digest_echo_refused(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_WRONG_DIGEST))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "confirm the pushed code digest"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_missing_digest_echo_refused(self):
        import dataclasses as _dc
        config, role = with_caps(self, "worker", ["vcs_publish"])
        digest = current_digest(self)
        approve(self, "main", digest)
        config = _dc.replace(config, vcs=adapter_settings(PUSH_NO_DIGEST))
        ctx = make_context(self, config, role)
        with self.assertRaisesRegex(Denied, "confirm the pushed code digest"):
            ctx.handle("vcs_publish", {"op": "push", "branch": "main"})

    def test_publish_via_requires_digest_param(self):
        with self.assertRaisesRegex(Denied, "code digest"):
            vcs.publish_via(adapter_settings(PUSH_OK), "push", {"branch": "main"})
