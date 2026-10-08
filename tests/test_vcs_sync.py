"""M1 step 4: grant-gated `sync` refresh plus conflict-flow e2e. Offline only."""
import dataclasses
import json
import sys
import unittest

from support import Fixture, ScriptDriver
from mizu import vcs
from mizu.errors import ConfigError, Denied
from mizu.runtime import Engine, check_consult_role, refresh_upstream

SHA_A = "a" * 40
SHA_B = "b" * 64


def adapter_settings(code, timeout=5, maximum=524288):
    return {"command": [sys.executable, "-c", code],
            "timeout_seconds": timeout, "max_bytes": maximum}


def fetch_code(refs_json):
    return ("import sys,json; json.load(sys.stdin); "
            "print(json.dumps({'refs': %s}))" % refs_json)


class VcsSyncTests(Fixture):
    def _synced_context(self, refs_json='{"main": "%s"}' % SHA_A):
        worker = self.config.roles["worker"]
        synced = dataclasses.replace(
            worker, capabilities=tuple(list(worker.capabilities) + ["sync"]))
        config = dataclasses.replace(
            self.config,
            vcs=adapter_settings(fetch_code(refs_json)),
            roles={**self.config.roles, "worker": synced})
        from mizu.fs import mkdir
        import uuid
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = self.project.snapshots.get()
        from mizu.runtime import Context
        return Context(config, self.project, synced, run, snap,
                       self.project.workspace)

    def test_sync_refused_without_grant(self):
        ctx = self.context("worker")
        self.assertNotIn("sync", ctx.role.capabilities)
        with self.assertRaises(Denied):
            ctx.handle("sync", {})

    def test_sync_requires_writable_workspace_at_call(self):
        import dataclasses
        base = self.config.roles["reviewer"]
        bad = dataclasses.replace(base, capabilities=tuple(list(base.capabilities) + ["sync"]))
        # Configured adapter isolates the workspace gate: without the
        # writable check this call would succeed instead of raising.
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(fetch_code('{"main": "%s"}' % SHA_A)))
        from mizu.fs import mkdir
        import uuid
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        snap = self.project.snapshots.get()
        from mizu.runtime import Context
        ctx = Context(config, self.project, bad, run, snap, run / "input")
        with self.assertRaisesRegex(Denied, "writable"):
            ctx.handle("sync", {})
        self.assertEqual(vcs.list_refs(self.project.root), [])

    def test_sync_on_read_role_refused_at_load(self):
        text = self.file.read_text()
        # reviewer is read-only; granting sync must fail validation.
        needle = '[roles.reviewer]'
        self.assertIn(needle, text)
        segment = text.split(needle, 1)[1].split('[roles.', 1)[0]
        self.assertIn('"submit_insight"', segment)
        patched = text.replace(
            'capabilities = ["files", "read", "search", "fetch", "experiment", "submit_insight", "finish"]',
            'capabilities = ["files", "read", "search", "fetch", "experiment", "submit_insight", "finish", "sync"]',
            1)
        # Only proceed if the exact reviewer line matched; otherwise edit explicitly.
        if patched == text:
            self.skipTest("reviewer capability line shape changed")
        path = self.root / "config/sync-read.toml"
        path.write_text(patched)
        from mizu.config import load
        with self.assertRaises(ConfigError):
            load(path)

    def test_sync_unconfigured_refused(self):
        worker = self.config.roles["worker"]
        synced = dataclasses.replace(
            worker, capabilities=tuple(list(worker.capabilities) + ["sync"]))
        config = dataclasses.replace(self.config, roles={**self.config.roles, "worker": synced})
        # vcs.command stays [] from the fixture default.
        self.assertEqual(config.vcs["command"], [])
        from mizu.fs import mkdir
        import uuid
        run = self.project.root / "runs" / uuid.uuid4().hex
        mkdir(run)
        from mizu.runtime import Context
        ctx = Context(config, self.project, synced, run,
                      self.project.snapshots.get(), self.project.workspace)
        with self.assertRaises(Denied):
            ctx.handle("sync", {})

    def test_sync_refreshes_refs_and_writes_receipt(self):
        ctx = self._synced_context()
        out = ctx.handle("sync", {})
        self.assertTrue(out["synced"])
        self.assertEqual(out["injected"], 1)
        self.assertEqual(out["prefix"], "refs/remotes/upstream")
        self.assertEqual(out["trust"], "external-untrusted")
        self.assertEqual(out["upstream"], SHA_A)
        self.assertEqual(out["refs"], ["main"])
        self.assertEqual(vcs.read_ref(self.project.root, "main"), SHA_A)
        receipt = json.loads((ctx.run_dir / "sync.json").read_text())
        self.assertEqual(receipt["refs"], {"main": SHA_A})
        # Harness state stays outside the product working tree.
        self.assertFalse((self.project.workspace / "refs").exists())
        self.assertFalse((self.project.workspace / "upstream-refs").exists())
        # Digest still excludes the injected view.
        captured = self.project.snapshots.capture_files(self.project.workspace)
        self.assertNotIn("refs/remotes/upstream/main", captured["files"])

    def test_host_refresh_helper_shares_injection(self):
        config = dataclasses.replace(
            self.config, vcs=adapter_settings(fetch_code('{"main": "%s"}' % SHA_B)))
        out = refresh_upstream(config, self.project)
        self.assertEqual(out["injected"], 1)
        self.assertEqual(out["refs"], {"main": SHA_B})
        self.assertEqual(vcs.read_ref(self.project.root, "main"), SHA_B)

    def test_consult_role_cannot_hold_sync(self):
        import dataclasses
        base = self.config.roles["consult"]
        bad = dataclasses.replace(base, capabilities=("files", "read", "finish", "sync"))
        with self.assertRaises(ConfigError):
            check_consult_role("consult", bad)

    def test_conflict_flow_e2e_with_fake_engine(self):
        # Upstream publishes main; the writer syncs, resolves the
        # "conflict" as an ordinary workspace edit, and publishes. Refs
        # never enter the snapshot digest.
        worker = self.config.roles["worker"]
        synced = dataclasses.replace(
            worker, capabilities=tuple(list(worker.capabilities) + ["sync"]))
        config = dataclasses.replace(
            self.config,
            vcs=adapter_settings(fetch_code('{"main": "%s"}' % SHA_A)),
            roles={**self.config.roles, "worker": synced})
        before = self.project.snapshots.get()
        project = self.project

        def work(ctx, prompt, profile):
            receipt = ctx.handle("sync", {})
            assert receipt["upstream"] == SHA_A
            # Conflict resolution is an ordinary edit, not adapter magic.
            (ctx.workspace / "app.py").write_bytes(b"VALUE = 3\n")
            ctx.handle("finish", {"outcome": "continue",
                                  "summary": "Synced upstream and resolved edit",
                                  "state": "Resolved; next is verification."})

        result = Engine(config, driver=ScriptDriver(callback=work)).run(project, "worker")
        self.assertEqual(result["status"], "completed")
        after = project.snapshots.get()
        self.assertNotEqual(after["id"], before["id"])
        self.assertIn("app.py", after["files"])
        self.assertNotIn("refs/remotes/upstream/main", after["files"])
        data = project.snapshots.read(after, "app.py")
        self.assertEqual(data, b"VALUE = 3\n")
        # The injected ref view survived publication but stays out of digest.
        self.assertEqual(vcs.read_ref(project.root, "main"), SHA_A)
        recaptured = project.snapshots.capture_files(project.workspace)
        self.assertEqual(recaptured["code_digest"], after["code_digest"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
