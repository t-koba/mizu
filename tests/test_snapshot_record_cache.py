"""Validated immutable snapshot records are reused per manifest identity."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from mizu import platform as _platform
from mizu.errors import Denied
from mizu.fs import canonical, digest, write_json
from mizu.snapshot import SnapshotNotDescendant, Snapshots


def manifest(files, *, parent=None):
    body = {
        "files": files,
        "code_digest": digest(canonical(files)),
        "goal": "snapshot record cache test",
        "goal_digest": digest(b"snapshot record cache test"),
    }
    if parent is not None:
        body["parent"] = parent
    return {**body, "id": digest(canonical(body))}


def file_entry(seed=b"x"):
    return {"sha256": digest(seed), "bytes": len(seed), "executable": False}


def store(root):
    return Snapshots(Path(root), excludes=(), max_file=64,
                     max_bytes=4096, max_files=1024)


def publish(snapshot_store, record):
    path = snapshot_store.root / "snapshots" / f"{record['id']}.json"
    write_json(path, record)
    return path


class SnapshotRecordCacheTests(unittest.TestCase):
    def test_unchanged_record_is_validated_once(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            record = manifest({"src/a.py": file_entry()})
            publish(snapshot_store, record)
            with patch.object(snapshot_store, "validate_manifest",
                              wraps=snapshot_store.validate_manifest) as validate:
                results = [snapshot_store.get(record["id"]) for _ in range(4)]
            # Windows metadata is not a trustworthy witness, so get() keeps
            # the full read and validation on every call and caches nothing.
            self.assertEqual(validate.call_count, 4 if _platform.IS_WINDOWS else 1)
            self.assertEqual(len(snapshot_store._record_cache), 0 if _platform.IS_WINDOWS else 1)
            self.assertEqual(len({id(result) for result in results}), len(results))
            for result in results:
                self.assertEqual(result, record)

    def test_changed_manifest_identity_revalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            record = manifest({"src/a.py": file_entry()})
            path = publish(snapshot_store, record)
            identity = snapshot_store._manifest_identity(path)
            self.assertIsNotNone(identity)
            current = [identity]
            # A content-addressed ID cannot name two different valid records,
            # so vary only the stat witness: a deterministic simulation of
            # this path being atomically replaced by another file generation.
            with patch.object(snapshot_store, "_manifest_identity",
                              side_effect=lambda _path: current[0]):
                with patch.object(snapshot_store, "validate_manifest",
                                  wraps=snapshot_store.validate_manifest) as validate:
                    snapshot_store.get(record["id"])
                    snapshot_store.get(record["id"])
                    self.assertEqual(validate.call_count, 2 if _platform.IS_WINDOWS else 1)
                    current[0] = (*identity, "replaced")
                    snapshot_store.get(record["id"])
                    snapshot_store.get(record["id"])
                    self.assertEqual(validate.call_count, 4 if _platform.IS_WINDOWS else 2)
            if _platform.IS_WINDOWS:
                self.assertFalse(snapshot_store._record_cache)
            else:
                cached_identity, cached_record = snapshot_store._record_cache[record["id"]]
                self.assertEqual(cached_identity, current[0])
                self.assertEqual(cached_record, record)

    def test_corrupt_record_is_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            original = manifest({"src/a.py": file_entry()})
            body = {key: value for key, value in original.items() if key != "id"}
            body["files"] = {"src/a.py": file_entry(b"tampered")}
            corrupt = {**body, "id": digest(canonical(body))}
            path = publish(snapshot_store, corrupt)
            with self.assertRaisesRegex(Denied, "code digest"):
                snapshot_store.get(corrupt["id"])
            self.assertNotIn(corrupt["id"], snapshot_store._record_cache)
            identity = snapshot_store._manifest_identity(path)
            with patch.object(snapshot_store, "_manifest_identity",
                              side_effect=lambda _path: (*identity, "replaced")):
                with patch.object(snapshot_store, "validate_manifest",
                                  wraps=snapshot_store.validate_manifest) as validate:
                    with self.assertRaisesRegex(Denied, "code digest"):
                        snapshot_store.get(corrupt["id"])
            self.assertEqual(validate.call_count, 1)
            self.assertNotIn(corrupt["id"], snapshot_store._record_cache)

    def test_returned_records_are_independent_on_hit_and_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            record = manifest({"src/a.py": file_entry()})
            publish(snapshot_store, record)
            first = snapshot_store.get(record["id"])
            first["goal"] = "mutated"
            first["files"]["src/a.py"]["bytes"] = 999
            second = snapshot_store.get(record["id"])
            self.assertEqual(second, record)
            self.assertIsNot(second["files"], first["files"])
            second["files"]["src/a.py"]["executable"] = True
            self.assertEqual(snapshot_store.get(record["id"]), record)

    def test_ancestry_walks_share_the_record_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            parent = manifest({"src/a.py": file_entry()})
            child = manifest({"src/b.py": file_entry()}, parent=parent["id"])
            publish(snapshot_store, parent)
            publish(snapshot_store, child)
            with patch.object(snapshot_store, "validate_manifest",
                              wraps=snapshot_store.validate_manifest) as validate:
                for _ in range(3):
                    self.assertEqual(snapshot_store.require_descendant(
                        parent["id"], child["id"]), child)
                    with self.assertRaises(SnapshotNotDescendant):
                        snapshot_store._descendant_distance_from(parent["id"])(parent["id"])
            validated_ids = Counter(call.args[0] for call in validate.call_args_list)
            # Each traversal asks for both manifests twice per iteration.
            expected = 6 if _platform.IS_WINDOWS else 1
            self.assertEqual(validated_ids[parent["id"]], expected)
            self.assertEqual(validated_ids[child["id"]], expected)

    def test_windows_keeps_the_full_read_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_store = store(directory)
            record = manifest({"src/a.py": file_entry()})
            publish(snapshot_store, record)
            with patch.object(_platform, "IS_WINDOWS", True):
                with patch.object(snapshot_store, "validate_manifest",
                                  wraps=snapshot_store.validate_manifest) as validate:
                    results = [snapshot_store.get(record["id"]) for _ in range(3)]
            self.assertEqual(validate.call_count, 3)
            self.assertEqual(results, [record] * 3)
            self.assertFalse(snapshot_store._record_cache)


if __name__ == "__main__":
    unittest.main()
