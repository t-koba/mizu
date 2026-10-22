"""The capture leaves the product's reserved private evidence tree out.

The product writes regenerable receipts into a reserved tree (ignored, never
committed). A capture must leave that tree out whatever the operator
configured, so a regenerated receipt never enters a published snapshot and
never moves the tree's identity. A snapshot that already holds such a path
stays readable: the rule governs new captures, it does not invalidate what
the store already holds.
"""
import unittest
from pathlib import Path

from support import Fixture
from mizu.fs import canonical, digest
from mizu.snapshot import Snapshots


class PrivateEvidenceCaptureTests(Fixture):
    def capture(self, workspace, excludes=()):
        store = Snapshots(Path(self.root / "capture-store"), excludes=tuple(excludes),
                          max_file=10 ** 7, max_bytes=10 ** 9, max_files=10 ** 6,
                          create_dirs=False)
        return store.capture_files(workspace)

    def private_tree(self):
        private = self.project.workspace / "private-validation"
        private.mkdir(parents=True, exist_ok=True)
        (private / "offline.json").write_bytes(b"{}\n")
        return private

    def test_reserved_private_evidence_tree_is_not_captured(self):
        self.private_tree()
        captured = self.capture(self.project.workspace)
        self.assertEqual([name for name in captured["files"]
                          if name.startswith("private-validation/")], [])

    def test_private_evidence_does_not_move_the_tree_identity(self):
        private = self.private_tree()
        receipt = private / "offline.json"
        receipt.write_bytes(b'{"run": 1}\n')
        before = self.capture(self.project.workspace)["code_digest"]
        receipt.write_bytes(b'{"run": 2}\n')
        after = self.capture(self.project.workspace)["code_digest"]
        self.assertEqual(before, after)

    def test_the_rule_holds_whatever_the_operator_excluded(self):
        self.private_tree()
        captured = self.capture(self.project.workspace, excludes=())
        self.assertEqual([name for name in captured["files"]
                          if name.startswith("private-validation/")], [])

    def test_a_stored_snapshot_with_the_tree_stays_readable(self):
        files = {"private-validation/offline.json": {"sha256": digest(b"{}\n"),
                                                      "bytes": 3, "executable": False}}
        captured = {"files": files, "code_digest": digest(canonical(files)),
                    "skipped": [], "reserved": [], "bytes": 3}
        store = self.project.snapshots
        record = store.create(captured, goal="goal", state="state", run=None,
                               outcome="wait", summary="stored before the rule")
        self.assertEqual(store.get(record["id"])["id"], record["id"])

    def test_the_packager_and_the_capture_share_one_reserved_name(self):
        from mizu.distribution import EXCLUDED
        from mizu.fs import PRIVATE_EVIDENCE_TREES
        self.assertTrue(PRIVATE_EVIDENCE_TREES)
        self.assertTrue(PRIVATE_EVIDENCE_TREES <= EXCLUDED)
