"""Snapshot capture excludes product-private file and directory basenames."""
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu.distribution import PRIVATE, is_private_name, source_files
from mizu.fs import canonical, digest
from mizu.snapshot import Snapshots


PRIVATE_NAMES = tuple(sorted(PRIVATE | {".env", ".env.local"}))


class PrivateNameCaptureTests(Fixture):
    def capture(self, workspace, *, excludes=()):
        store = Snapshots(Path(self.root / f"private-name-store-{uuid.uuid4().hex}"),
                          excludes=tuple(excludes),
                          max_file=10 ** 7, max_bytes=10 ** 9, max_files=10 ** 6)
        return store.capture_files(workspace)

    def workspace_with_private_names(self):
        workspace = Path(self.root / "private-name-workspace")
        nested = workspace / "nested"
        nested.mkdir(parents=True)
        (workspace / "README.md").write_text("public\n")
        for name in PRIVATE_NAMES:
            (workspace / name).write_text("synthetic private fixture\n")
            (nested / name).write_text("synthetic nested fixture\n")
        private_dir = nested / "private-directory" / "installation.json"
        private_dir.mkdir(parents=True)
        (private_dir / "child.txt").write_text("synthetic child\n")
        return workspace

    def test_private_basenames_are_skipped_at_root_and_nested(self):
        workspace = self.workspace_with_private_names()
        # An empty operator exclusion list proves this product-private rule
        # does not depend on operator-specific capture configuration.
        captured = self.capture(workspace, excludes=())
        for name in PRIVATE_NAMES:
            self.assertNotIn(name, captured["files"])
            self.assertNotIn(f"nested/{name}", captured["files"])
        self.assertNotIn("nested/private-directory/installation.json/child.txt", captured["files"])
        self.assertIn("README.md", captured["files"])

    def test_private_rewrites_do_not_move_the_capture_digest(self):
        workspace = self.workspace_with_private_names()
        before = self.capture(workspace)["code_digest"]
        for name in PRIVATE_NAMES:
            (workspace / name).write_text("rewritten fixture\n")
            (workspace / "nested" / name).write_text("rewritten nested fixture\n")
        after = self.capture(workspace)["code_digest"]
        self.assertEqual(before, after)

    def test_fallback_walker_enforces_the_same_private_name_rule(self):
        workspace = self.workspace_with_private_names()
        with patch("mizu.snapshot._platform.HAS_OPENAT", False), \
                patch("mizu.snapshot._platform.HAS_DIR_FD", False):
            captured = self.capture(workspace, excludes=())
        self.assertNotIn("credentials.env", captured["files"])
        self.assertNotIn("nested/.env.local", captured["files"])
        self.assertNotIn("nested/private-directory/installation.json/child.txt", captured["files"])
        self.assertIn("README.md", captured["files"])

    def test_stored_snapshots_with_private_names_remain_readable(self):
        data = b"legacy private path\n"
        files = {"nested/credentials.env": {"sha256": digest(data),
                                                "bytes": len(data), "executable": False}}
        captured = {"files": files, "code_digest": digest(canonical(files)),
                    "skipped": [], "reserved": [], "bytes": len(data)}
        store = self.project.snapshots
        record = store.create(captured, goal="goal", state="state", run=None,
                              outcome="wait", summary="stored before the private-name rule")
        self.assertEqual(store.get(record["id"])["id"], record["id"])

    def test_capture_and_packager_share_the_private_name_policy(self):
        workspace = Path(self.root / "private-name-workspace")
        source = workspace / "src"
        source.mkdir(parents=True)
        (source / "module.py").write_text("public = True\n")
        for name in PRIVATE_NAMES:
            if name != "config.local.toml":
                (source / name).write_text("synthetic private fixture\n")
        private_dir = source / "config.local.toml"
        private_dir.mkdir()
        (private_dir / "payload.py").write_text("synthetic private child\n")
        captured = self.capture(workspace, excludes=())
        packaged = {path.relative_to(workspace).as_posix() for _, path in source_files(workspace)}
        for name in PRIVATE_NAMES:
            self.assertTrue(is_private_name(name))
            self.assertNotIn(f"src/{name}", captured["files"])
            self.assertNotIn(f"src/{name}", packaged)
        self.assertNotIn("src/config.local.toml/payload.py", captured["files"])
        self.assertNotIn("src/config.local.toml/payload.py", packaged)
        self.assertIn("src/module.py", packaged)
        self.assertIn("src/module.py", captured["files"])


if __name__ == "__main__":
    unittest.main()
