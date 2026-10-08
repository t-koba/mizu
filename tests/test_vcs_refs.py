"""M1 step 3: read-only ref injection outside the working tree. Offline only."""
import dataclasses
import os
import stat
import unittest
from pathlib import Path

from support import Fixture
from mizu import vcs
from mizu.errors import Denied
from mizu.snapshot import Snapshots

SHA_A = "a" * 40
SHA_B = "b" * 64


class VcsRefInjectionTests(Fixture):
    def test_prefix_constant_shared_with_snapshots(self):
        from mizu.snapshot import REF_PREFIX as SNAP_PREFIX
        self.assertEqual(tuple(SNAP_PREFIX), ("refs", "remotes", "upstream"))
        self.assertEqual(vcs.REF_PREFIX_PATH, "refs/remotes/upstream")

    def test_inject_writes_readonly_files_outside_workspace(self):
        receipt = vcs.inject_refs(self.project.root, {"main": SHA_A, "feature/x": SHA_B})
        self.assertEqual(receipt, {"injected": 2, "prefix": "refs/remotes/upstream",
                                   "trust": "external-untrusted"})
        main = self.project.root / "upstream-refs/main"
        nested = self.project.root / "upstream-refs/feature/x"
        self.assertEqual(main.read_bytes(), (SHA_A + "\n").encode())
        self.assertEqual(nested.read_bytes(), (SHA_B + "\n").encode())
        if os.name == "posix":
            for path in (main, nested):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0,
                                 f"{path} must not be writable")
        self.assertEqual(vcs.list_refs(self.project.root), ["feature/x", "main"])
        self.assertEqual(vcs.read_ref(self.project.root, "main"), SHA_A)
        # Harness state never appears in the product working tree.
        self.assertFalse((self.project.workspace / "refs").exists())
        self.assertFalse((self.project.workspace / "upstream-refs").exists())

    def test_store_outside_workspace_keeps_digest_stable(self):
        before = self.project.snapshots.capture_files(self.project.workspace)
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        after = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(before["code_digest"], after["code_digest"])
        self.assertNotIn("refs/remotes/upstream/main", after["files"])
        self.assertNotIn("upstream-refs/main", after["files"])
        self.assertFalse((self.project.workspace / "refs").exists())

    def test_store_tampering_cannot_move_workspace_digest(self):
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        before = self.project.snapshots.capture_files(self.project.workspace)
        target = self.project.root / "upstream-refs/main"
        target.chmod(0o600)
        target.write_bytes(("c" * 40 + "\n").encode())
        after = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(before["code_digest"], after["code_digest"])

    def test_stale_refs_pruned_on_refresh(self):
        vcs.inject_refs(self.project.root, {"main": SHA_A, "old": SHA_B})
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        self.assertEqual(vcs.list_refs(self.project.root), ["main"])
        self.assertFalse((self.project.root / "upstream-refs/old").exists())

    def test_invalid_ref_names_refused(self):
        bad_names = ["", "../escape", "/absolute", "a//b", ".", "..", "a/./b",
                     "with\nnewline", "with:colon", "back\\slash", "x" * 513,
                     "main\x1b[2K\r", "main\u202e", "main\x7f", "main\x85",
                     "main\u200b", "main\r", "main\u2066"]
        for name in bad_names:
            with self.assertRaises(Denied, msg=repr(name)):
                vcs.inject_refs(self.project.root, {name: SHA_A})
        for refs in ({"main": "xyz"}, {"main": "A" * 40}, {"main": "a" * 39},
                     {"ok": SHA_A, "../escape": SHA_B}, "not-a-dict",
                     {f"r{i}": SHA_A for i in range(4097)}):
            with self.assertRaises(Denied, msg=repr(refs)[:60]):
                vcs.validate_refs(refs)
        # Adapter-side shape validation shares the same gate.
        with self.assertRaises(Denied):
            vcs.validate_refs({"../escape": SHA_A})

    def test_symlink_escape_refused(self):
        (self.project.root / "upstream-refs").symlink_to("/tmp")
        with self.assertRaises(Denied):
            vcs.inject_refs(self.project.root, {"main": SHA_A})
        self.assertEqual(vcs.list_refs(self.project.root), [])
        # Legacy symlinked subtree is left in place, never followed.
        (self.project.root / "upstream-refs").unlink()
        (self.project.workspace / "refs").mkdir(exist_ok=True)
        (self.project.workspace / "refs" / "remotes").symlink_to("/tmp")
        receipt = vcs.inject_refs(self.project.root, {"main": SHA_A})
        self.assertEqual(receipt["injected"], 1)
        self.assertTrue((self.project.workspace / "refs" / "remotes").is_symlink())

    def test_symlink_target_refused(self):
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        target = self.project.root / "upstream-refs/main"
        target.chmod(0o600)
        target.unlink()
        target.symlink_to("/etc/hostname")
        with self.assertRaises(Denied):
            vcs.inject_refs(self.project.root, {"main": SHA_A})
        with self.assertRaises(Denied):
            vcs.read_ref(self.project.root, "main")

    def test_refresh_removes_legacy_workspace_tree(self):
        legacy = self.project.workspace / "refs/remotes/upstream"
        legacy.mkdir(parents=True)
        (legacy / "main").write_bytes((SHA_A + "\n").encode())
        before = self.project.snapshots.capture_files(self.project.workspace)
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        self.assertFalse((self.project.workspace / "refs").exists())
        after = self.project.snapshots.capture_files(self.project.workspace)
        self.assertEqual(before["code_digest"], after["code_digest"])
        self.assertEqual(vcs.read_ref(self.project.root, "main"), SHA_A)

    def test_excluded_even_with_empty_operator_excludes(self):
        probe = Snapshots(self.root / "probe-store", excludes=(), max_file=1024,
                           max_bytes=65536, max_files=100)
        self.assertTrue(probe.excluded("refs/remotes/upstream"))
        self.assertTrue(probe.excluded("refs/remotes/upstream/main"))
        self.assertTrue(probe.excluded("refs/remotes/upstream/feature/x"))
        self.assertFalse(probe.excluded("refs/remotes/other"))
        self.assertFalse(probe.excluded("refs"))
        self.assertFalse(probe.excluded("app.py"))

    def test_split_ref_path(self):
        self.assertEqual(vcs.split_ref_path("refs/remotes/upstream/main"), "main")
        self.assertEqual(vcs.split_ref_path("refs/remotes/upstream/a/b"), "a/b")
        for path in ("refs/remotes/upstream", "refs/remotes", "refs",
                     "app.py", "refs/remotes/upstream/../x", "refs/remotes/upstream/",
                     "refs/remotes/upstream/a:bad", ""):
            self.assertIsNone(vcs.split_ref_path(path), msg=path)

    def test_files_and_read_views_show_refs_readonly(self):
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        worker = self.context("worker")
        self.assertIn("refs/remotes/upstream/main", worker.handle("files", {})["files"])
        self.assertEqual(worker.handle("read", {"path": "refs/remotes/upstream/main"}),
                         {"path": "refs/remotes/upstream/main", "text": SHA_A})
        # Read roles see refs from the harness store even though their
        # materialized snapshot input excludes them.
        reviewer = self.context("reviewer")
        self.assertIn("refs/remotes/upstream/main", reviewer.handle("files", {})["files"])
        self.assertEqual(reviewer.handle("read", {"path": "refs/remotes/upstream/main"})["text"], SHA_A)
        # Unknown refs stay refused, not empty reads.
        with self.assertRaises(Denied):
            worker.handle("read", {"path": "refs/remotes/upstream/missing"})

    def test_none_workspace_sees_no_refs(self):
        vcs.inject_refs(self.project.root, {"main": SHA_A})
        ctx = self.context("reviewer")
        ctx.role = dataclasses.replace(ctx.role, workspace="none",
                                       capabilities=("files", "read", "finish"))
        self.assertEqual(ctx.handle("files", {})["files"], [])
        with self.assertRaises(Denied):
            ctx.handle("read", {"path": "refs/remotes/upstream/main"})


if __name__ == "__main__":
    raise SystemExit(unittest.main())
