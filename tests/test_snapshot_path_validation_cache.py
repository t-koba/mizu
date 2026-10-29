"""Bounded path-policy caching must retain content-addressed validation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mizu.errors import Denied
from mizu.fs import canonical, digest, write_json
from mizu.snapshot import Snapshots, _PATH_VALIDATION_CACHE_LIMIT


def manifest(files):
    body = {
        "files": files,
        "code_digest": digest(canonical(files)),
        "goal": "snapshot path cache test",
        "goal_digest": digest(b"snapshot path cache test"),
    }
    return {**body, "id": digest(canonical(body))}


def file_entry():
    return {"sha256": digest(b"x"), "bytes": 1, "executable": False}


def store(root=None, *, excludes=()):
    return Snapshots(Path(root or "."), excludes=excludes, max_file=16,
                     max_bytes=4096, max_files=1024, create_dirs=root is not None)


class SnapshotPathValidationCacheTests(unittest.TestCase):
    def test_repeated_manifest_validation_skips_only_the_path_walk(self):
        snapshot_store = store()
        record = manifest({"src/a.py": file_entry(), "src/b.py": file_entry()})
        original = snapshot_store._path_key
        with patch.object(snapshot_store, "_path_key", wraps=original) as path_key:
            for _ in range(4):
                self.assertIs(snapshot_store.validate_manifest(record["id"], record), record)
        self.assertEqual(path_key.call_count, 2)
        self.assertEqual(len(snapshot_store._path_validation_cache), 1)

    def test_cache_is_bounded_and_evicts_old_entries(self):
        snapshot_store = store()
        first = None
        original = snapshot_store._path_key
        with patch.object(snapshot_store, "_path_key", wraps=original) as path_key:
            for index in range(_PATH_VALIDATION_CACHE_LIMIT + 1):
                record = manifest({f"src/file-{index}.py": file_entry()})
                if index == 0:
                    first = record
                snapshot_store.validate_manifest(record["id"], record)
            self.assertLessEqual(len(snapshot_store._path_validation_cache),
                                 _PATH_VALIDATION_CACHE_LIMIT)
            self.assertEqual(path_key.call_count, _PATH_VALIDATION_CACHE_LIMIT + 1)
            snapshot_store.validate_manifest(first["id"], first)
            self.assertEqual(path_key.call_count, _PATH_VALIDATION_CACHE_LIMIT + 2)

    def test_policy_change_does_not_reuse_old_path_result(self):
        snapshot_store = store()
        record = manifest({"src/a.py": file_entry()})
        snapshot_store.validate_manifest(record["id"], record)
        snapshot_store.excludes = ("src",)
        with self.assertRaises(Denied):
            snapshot_store.validate_manifest(record["id"], record)

    def test_manifest_corruption_after_cache_hit_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            record = manifest({"src/a.py": file_entry()})
            path = snapshot_store.root / "snapshots" / f"{record['id']}.json"
            write_json(path, record)
            self.assertEqual(snapshot_store.get(record["id"]), record)
            self.assertEqual(len(snapshot_store._path_validation_cache), 1)
            write_json(path, {**record, "goal": "tampered"})
            with self.assertRaises(Denied):
                snapshot_store.get(record["id"])


if __name__ == "__main__":
    unittest.main()
