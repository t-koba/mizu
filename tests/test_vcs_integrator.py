"""Read-workspace integration authority: dispose/retire without source writes.

Offline only. The integrator owns external PR/ref operations behind explicit
capabilities plus configured grants and approvals; it never touches the shared
workspace. Source edits stay with writable worker roles.
"""
import dataclasses
import json
import sys
import unittest
import uuid
from pathlib import Path

from support import Fixture
from mizu.errors import ConfigError, Denied
from mizu.fs import mkdir
from mizu.runtime import Context

SHA_A = "a" * 40
BASE_A = "c" * 40
TARGET = "main"
PROPOSAL_ID = "forge:owner/repo#1"


def close_args(**over):
    args = {"op": "close", "id": PROPOSAL_ID, "sha": SHA_A,
            "base": BASE_A, "target": TARGET, "branch": "mizu/x-1"}
    args.update(over)
    return args

INTEGRATOR_CAPS = ("files", "read", "vcs_read", "vcs_dispose", "vcs_retire", "finish")


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def dispose_code(payload):
    import tempfile
    path = Path(tempfile.mkdtemp()) / "disposed.json"
    path.write_text(json.dumps(payload))
    return ("import sys,json; req=json.load(sys.stdin); "
            "print(open(%r).read())" % str(path))


def echo(state="closed"):
    return {"id": PROPOSAL_ID, "sha": SHA_A, "base": BASE_A,
            "target": TARGET, "state": state}


RETIRE_OK = ("import sys,json; req=json.load(sys.stdin); "
             "print(json.dumps({'op': req['op'], 'branch': req['branch'], "
             "'sha': req['expected_sha'], 'deleted': True, "
             "'live_refs': []}))")


def dispose_settings(state="closed", close=True, merge=True):
    settings = adapter_settings(dispose_code(echo(state)))
    settings["close_grant"] = close
    settings["merge_grant"] = merge
    return settings


def retire_settings():
    settings = adapter_settings(RETIRE_OK)
    settings.update({"retire_grant": True, "owned_prefixes": ["mizu/"],
                     "protected_refs": ["main"]})
    return settings


def integrator_role(fixture):
    base = fixture.config.roles["consult"]
    return dataclasses.replace(base, name="integrator",
                               capabilities=INTEGRATOR_CAPS)


def integrator_context(fixture, role):
    # Mirror Engine.run for read roles: the input is materialized from the
    # published snapshot into the run directory, never the shared workspace.
    run = fixture.project.root / "runs" / uuid.uuid4().hex
    mkdir(run)
    snap = fixture.project.snapshots.get()
    workspace = run / "input"
    fixture.project.snapshots.materialize(snap, workspace)
    config = dataclasses.replace(fixture.config,
                                 roles={**fixture.config.roles, "integrator": role})
    return config, Context(config, fixture.project, role, run, snap, workspace)


def approve(fixture, branch, digest):
    record = fixture.project.insights.submit(
        source="operator", title=f"GO {branch}",
        body=f"Ship it.\ndigest: {digest}\n",
        base_snapshot=None, run=None)
    fixture.project.insights.decide(record["id"], "accept", "reviewed", "", "operator",
                                    expected_rev=record["rev"])
    return record


class ReadIntegratorTests(Fixture):
    def test_dispose_closes_from_materialized_input(self):
        # Routine terminal reconciliation: the read integrator closes
        # behind close_grant alone, with no approval recorded.
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        config = dataclasses.replace(config, vcs=dispose_settings())
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        out = ctx.handle("vcs_dispose", close_args())
        self.assertTrue(out["disposed"])
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["base"], BASE_A)
        self.assertEqual(out["target"], TARGET)
        self.assertNotIn("approval", out)

    def test_dispose_merges_with_approval_from_materialized_input(self):
        # Promotion binds the materialized input digest exactly like a
        # writable capture: the GO approval must name the input digest.
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        captured = self.project.snapshots.capture_files(ctx.workspace)
        expected = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(captured["code_digest"], expected["code_digest"])
        config = dataclasses.replace(config, vcs=dispose_settings("merged"))
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        rec = approve(self, "mizu/x-1", captured["code_digest"])
        out = ctx.handle("vcs_dispose", close_args(op="merge"))
        self.assertTrue(out["disposed"])
        self.assertEqual(out["state"], "merged")
        self.assertEqual(out["approval"], rec["id"])

    def test_main_promotion_refused_without_matching_approval(self):
        # A merge aimed at main with no GO record is refused before the
        # adapter spawns: promotion approval is never inferred.
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        config = dataclasses.replace(config, vcs=dispose_settings("merged"))
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        with self.assertRaisesRegex(Denied, "recorded human approval"):
            ctx.handle("vcs_dispose", close_args(op="merge", target="main",
                                                 branch="main"))
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def test_retire_owned_branch_without_workspace_write(self):
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        config = dataclasses.replace(config, vcs=retire_settings())
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        out = ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                        "expected_sha": SHA_A})
        self.assertTrue(out["retired"])
        self.assertEqual(out["classification"], "owned")

    def test_role_without_capability_denied(self):
        # A read role without the grant-capability cannot dispose, even
        # with the adapter grant configured: authority starts at capability.
        role = self.config.roles["consult"]
        _, ctx = integrator_context(self, role)
        with self.assertRaisesRegex(Denied, "no capability"):
            ctx.handle("vcs_dispose", close_args())

    def test_ungranted_integrator_denied(self):
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        captured = self.project.snapshots.capture_files(ctx.workspace)
        approve(self, "mizu/x-1", captured["code_digest"])
        config = dataclasses.replace(config, vcs=dispose_settings(close=False))
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        with self.assertRaisesRegex(Denied, "not granted"):
            ctx.handle("vcs_dispose", close_args())

    def test_wrong_target_refused(self):
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        config = dataclasses.replace(config, vcs=dispose_settings())
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        with self.assertRaisesRegex(Denied, "expected base and target"):
            ctx.handle("vcs_dispose", close_args(target="release/2"))
        self.assertFalse((ctx.run_dir / "vcs-dispose.json").exists())

    def test_integrator_refused_protected_branch(self):
        role = integrator_role(self)
        config, ctx = integrator_context(self, role)
        config = dataclasses.replace(config, vcs=retire_settings())
        ctx = Context(config, self.project, role, ctx.run_dir,
                      ctx.snapshot, ctx.workspace)
        with self.assertRaisesRegex(Denied, "protected"):
            ctx.handle("vcs_retire", {"branch": "main",
                                      "expected_sha": SHA_A})

    def test_nowhere_role_denied(self):
        role = dataclasses.replace(integrator_role(self), workspace="none")
        config = dataclasses.replace(self.config,
                                     roles={**self.config.roles, "integrator": role})
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = self.project.snapshots.get()
        ctx = Context(config, self.project, role, run, snap, run / "input")
        with self.assertRaisesRegex(Denied, "visible workspace"):
            ctx.handle("vcs_dispose", close_args())
        with self.assertRaisesRegex(Denied, "visible workspace"):
            ctx.handle("vcs_retire", {"branch": "mizu/x-1",
                                      "expected_sha": SHA_A})


class IntegratorConfigTests(Fixture):
    def _integrator_file(self, workspace):
        text = self.file.read_text()
        head, _, _ = text.partition("[roles.consult]")
        block = ('[roles.integrator]\nprofile = "alternate"\n'
                 'policy = "policies/consult.md"\n'
                 f'workspace = "{workspace}"\n'
                 'capabilities = ["files", "read", "vcs_read", "vcs_dispose", '
                 '"vcs_retire", "finish"]\n')
        path = self.root / f"config/integrator-{workspace}.toml"
        path.write_text(head + block)
        return path

    def test_read_integrator_loads(self):
        from mizu.config import load
        config = load(self._integrator_file("read"))
        self.assertEqual(config.roles["integrator"].workspace, "read")
        self.assertIn("vcs_dispose", config.roles["integrator"].capabilities)
        self.assertIn("vcs_retire", config.roles["integrator"].capabilities)

    def test_nowhere_integrator_refused(self):
        from mizu.config import load
        with self.assertRaisesRegex(ConfigError, "visible workspace"):
            load(self._integrator_file("none"))

    def test_integrator_cannot_self_approve(self):
        from mizu.config import load
        path = self._integrator_file("read")
        head, sep, tail = path.read_text().partition("[roles.integrator]")
        assert sep
        tail = tail.replace('"vcs_retire", "finish"]',
                            '"vcs_retire", "submit_insight", "finish"]')
        path.write_text(head + sep + tail)
        with self.assertRaisesRegex(ConfigError, "self-approval"):
            load(path)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
