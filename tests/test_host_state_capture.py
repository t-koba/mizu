"""The capture leaves the product's own host-local state out.

The per-engine adapter start-up record and its lock hold one host's
measurements, not the product's source. A capture must leave them out
whatever the operator configured, so a published tree never carries a
per-host record and a regenerated measurement never moves the tree's
identity. A snapshot that already holds such a path stays readable: the
rule governs new captures, it does not invalidate what the store already
holds.
"""
import unittest
from pathlib import Path

from support import Fixture
from mizu.fs import canonical, digest
from mizu.snapshot import Snapshots
from mizu.startup import HOST_STATE_FILES, RECORD_NAME


class HostStateCaptureTests(Fixture):
    def capture(self, workspace, excludes=()):
        store = Snapshots(Path(self.root / "capture-store"), excludes=tuple(excludes),
                          max_file=10 ** 7, max_bytes=10 ** 9, max_files=10 ** 6,
                          create_dirs=False)
        return store.capture_files(workspace)

    def host_state(self):
        record = self.project.workspace / RECORD_NAME
        record.write_bytes(b'{"codex": {"seconds": 1.0}}\n')
        (self.project.workspace / (RECORD_NAME + '.lock')).write_bytes(b'')
        return record

    def test_the_host_start_up_record_is_not_captured(self):
        self.host_state()
        captured = self.capture(self.project.workspace)
        self.assertEqual([name for name in captured["files"]
                          if name in HOST_STATE_FILES], [])

    def test_the_host_state_does_not_move_the_tree_identity(self):
        record = self.host_state()
        before = self.capture(self.project.workspace)["code_digest"]
        record.write_bytes(b'{"codex": {"seconds": 2.0}}\n')
        after = self.capture(self.project.workspace)["code_digest"]
        self.assertEqual(before, after)

    def test_the_rule_holds_whatever_the_operator_configured(self):
        self.host_state()
        captured = self.capture(self.project.workspace, excludes=())
        self.assertEqual([name for name in captured["files"]
                          if name in HOST_STATE_FILES], [])

    def test_the_host_state_is_never_committed(self):
        ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
        for name in HOST_STATE_FILES:
            self.assertIn(name, ignored)

    def test_a_stored_snapshot_with_the_record_stays_readable(self):
        files = {RECORD_NAME: {"sha256": digest(b"{}\n"), "bytes": 3,
                                "executable": False}}
        captured = {"files": files, "code_digest": digest(canonical(files)),
                    "skipped": [], "reserved": [], "bytes": 3}
        store = self.project.snapshots
        record = store.create(captured, goal="goal", state="state", run=None,
                               outcome="wait", summary="stored before the rule")
        self.assertEqual(store.get(record["id"])["id"], record["id"])


if __name__ == "__main__":
    unittest.main()
