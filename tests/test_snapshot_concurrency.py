"""Manifest merges and process-safe parent-checked publication (no VCS)."""
from __future__ import annotations

import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mizu import snapshot as snapshot_module
from mizu.errors import Denied, LimitExceeded, SnapshotConflict
from mizu.fs import atomic_write, canonical, digest, read_json, write_json
from mizu.snapshot import Snapshots


def store_at(root, **limits):
    return Snapshots(root, excludes=(".git",),
                     **{"max_file": 1024, "max_bytes": 4096, "max_files": 16, **limits})


def cas_attempt(root, sid, barrier, results):
    """Independent store/process, deliberately slow commit to expose a lost CAS."""
    store = store_at(Path(root))
    publish = snapshot_module.publish_pointer

    def slow_commit(*args, **kwargs):
        time.sleep(.1)
        return publish(*args, **kwargs)

    try:
        barrier.wait(timeout=10)
        with patch.object(snapshot_module, "publish_pointer", side_effect=slow_commit):
            store.publish({"id": sid})
        results.put((sid, "published"))
    except SnapshotConflict:
        results.put((sid, "conflict"))
    except Exception as error:
        results.put((sid, type(error).__name__))


class SnapshotConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mizu-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = store_at(self.root / "store")
        self.serial = 0

    def capture(self, contents):
        files = {}
        for path, value in contents.items():
            data, executable = value if isinstance(value, tuple) else (value, False)
            data = data.encode()
            sha = digest(data)
            atomic_write(self.store.root / "objects" / sha, data, exclusive=True)
            files[path] = {"sha256": sha, "bytes": len(data), "executable": executable}
        return {"files": files, "code_digest": digest(canonical(files)),
                "bytes": sum(e["bytes"] for e in files.values()), "skipped": []}

    def create(self, contents, **kwargs):
        self.serial += 1
        return self.store.create(self.capture(contents), goal="goal", state="state", run=None,
                                 outcome="wait", summary=f"snapshot {self.serial}", **kwargs)

    def branches(self, base, head, target):
        base = self.create(base)
        head = self.create(head, parent=base["id"])
        target = self.create(target, parent=base["id"])
        return base, head, target

    def merge(self, records, store=None):
        return (store or self.store).merge(*(record["id"] for record in records))

    def rewrite(self, record, **changes):
        body = {k: v for k, v in record.items() if k != "id"}
        body.update(changes)
        body["id"] = digest(canonical(body))
        write_json(self.store.root / "snapshots" / (body["id"] + ".json"), body)
        return body

    def publication_bytes(self):
        return {p.relative_to(self.store.root).as_posix(): p.read_bytes()
                for p in [self.store.root / "current.json", *sorted((self.store.root / "histories").glob("*.json"))]}

    def test_disjoint_merge_is_createable_and_materializable(self):
        records = self.branches({"a": "old", "b": "old", "gone": "x"},
                                {"a": "new", "b": "old"},
                                {"a": "old", "b": "changed", "gone": "x", "new": "added"})
        capture, conflicts = self.merge(records)
        self.assertEqual(conflicts, [])
        expected = self.capture({"a": "new", "b": "changed", "new": "added"})
        self.assertEqual(capture, expected)
        base, head, target = records
        self.store.publish(base)
        self.store.publish(target)
        merged = self.store.create(capture, goal="goal", state="state", run=None,
                                   outcome="wait", summary="merged", parent=target["id"])
        self.store.publish(merged)
        destination = self.root / "merged"
        self.store.materialize(self.store.get(), destination)
        self.assertEqual((destination / "a").read_bytes(), b"new")
        self.assertFalse((destination / "gone").exists())

    def test_identical_edits_additions_and_deletions(self):
        records = self.branches({"a": "old", "gone": "x"},
                                {"a": ("new", True), "added": "x"},
                                {"a": ("new", True), "added": "x"})
        capture, conflicts = self.merge(records)
        self.assertEqual(conflicts, [])
        self.assertEqual(capture, self.capture({"a": ("new", True), "added": "x"}))

    def test_divergent_same_path_and_delete_edit_conflicts_are_sorted(self):
        records = self.branches({"z": "old", "a": "old", "d": "old"},
                                {"z": "left", "a": "left"},
                                {"z": "right", "a": "right", "d": "edited"})
        self.assertEqual(self.merge(records), (None, ["a", "d", "z"]))
        self.assertEqual(self.store.merge(records[0]["id"], records[2]["id"], records[1]["id"]),
                         (None, ["a", "d", "z"]))

    def test_executable_is_part_of_entry_not_a_separate_merge_dimension(self):
        records = self.branches({"a": "old"}, {"a": ("old", True)}, {"a": "new"})
        self.assertEqual(self.merge(records), (None, ["a"]))

    def test_file_directory_prefix_collision(self):
        records = self.branches({}, {"dir": "file"}, {"dir/sub/file": "child"})
        self.assertEqual(self.merge(records), (None, ["dir", "dir/sub/file"]))

    def test_host_case_collisions_are_explicit(self):
        records = self.branches({}, {"Name": "left"}, {"name": "right"})
        with patch.object(snapshot_module._platform, "IS_WINDOWS", True):
            self.assertEqual(self.merge(records), (None, ["Name", "name"]))

    def test_combined_count_and_byte_bounds(self):
        records = self.branches({}, {"a": "123"}, {"b": "456"})
        for limits in ({"max_files": 1}, {"max_bytes": 5}):
            with self.subTest(limits=limits), self.assertRaises(LimitExceeded):
                self.merge(records, store_at(self.store.root, **limits))
        capture, conflicts = self.merge(records, store_at(self.store.root, max_files=2, max_bytes=6))
        self.assertEqual((len(capture["files"]), capture["bytes"], conflicts), (2, 6, []))

    def test_input_count_and_byte_bounds_even_when_merge_deletes_files(self):
        records = self.branches({"a": "123", "b": "456"}, {}, {})
        for limits in ({"max_files": 1}, {"max_bytes": 5}):
            with self.subTest(limits=limits), self.assertRaises(LimitExceeded):
                self.merge(records, store_at(self.store.root, **limits))

    def test_merge_requires_exact_valid_ids_and_direct_parent(self):
        base, head, target = self.branches({}, {"a": "a"}, {"b": "b"})
        for invalid in (None, "../escape", 3, "0" * 64):
            for position in range(3):
                ids = [base["id"], head["id"], target["id"]]
                ids[position] = invalid
                with self.subTest(ids=ids), self.assertRaises(Denied):
                    self.store.merge(*ids)
        descendant = self.create({"c": "c"}, parent=head["id"])
        with self.assertRaisesRegex(Denied, "directly based"):
            self.merge((base, descendant, target))
        unrelated = self.create({"elsewhere": "x"}, parent=None)
        unrelated_target = self.create({"also-elsewhere": "y"}, parent=unrelated["id"])
        with self.assertRaisesRegex(Denied, "target must descend"):
            self.store.merge(base["id"], head["id"], unrelated_target["id"])
        path = self.store.root / "snapshots" / (target["id"] + ".json")
        write_json(path, {**target, "summary": "tampered"})
        with self.assertRaisesRegex(Denied, "integrity"):
            self.merge((base, head, target))

    def test_merge_target_ancestry_obeys_history_bound(self):
        base = self.create({}, parent=None)
        head = self.create({"a": "a"}, parent=base["id"])
        target = base
        for _ in range(8):
            target = self.create({}, parent=target["id"])
        bounded = store_at(self.store.root, history_index=8)
        with self.assertRaisesRegex(Denied, "ancestry exceeds"):
            bounded.merge(base["id"], head["id"], target["id"])

    def test_merge_rejects_invalid_paths_and_metadata_in_each_input(self):
        records = self.branches({}, {"a": "a"}, {"b": "b"})
        for contents in ({"../escape": "x"}, {".git/config": "x"}, {"a": "x", "a/b": "x"}):
            capture = self.capture(contents)
            for position in range(3):
                invalid = list(records)
                invalid[position] = self.rewrite(records[position], **capture)
                with self.subTest(contents=contents, position=position), self.assertRaises(Denied):
                    self.merge(invalid)
        capture = self.capture({"a": "x"})
        capture["files"]["a"]["executable"] = 1
        capture["code_digest"] = digest(canonical(capture["files"]))
        invalid = self.rewrite(records[1], **capture)
        with self.assertRaisesRegex(Denied, "metadata"):
            self.merge((records[0], invalid, records[2]))

    def test_parent_capture_explicit_parent_and_root_cas(self):
        root = self.create({})
        other_root = self.create({})
        self.assertIsNone(root["parent"])
        self.store.publish(root)
        automatic = self.create({"a": "a"})
        self.assertEqual(automatic["parent"], root["id"])
        self.store.publish(automatic)
        derived = self.create({"b": "b"}, parent=root["id"])
        self.assertEqual(self.store.get(derived["id"])["parent"], root["id"])
        before = self.publication_bytes()
        for stale in (other_root, derived):
            with self.assertRaises(SnapshotConflict):
                self.store.publish(stale)
            self.assertEqual(self.publication_bytes(), before)
        with self.assertRaises(Denied):
            self.create({}, parent=None)

    def test_success_retry_and_stale_cas_use_stored_parent(self):
        base = self.create({})
        self.store.publish(base)
        first = self.create({"a": "first"})
        stale = self.create({"a": "stale"})
        self.store.publish(first)
        before = self.publication_bytes()
        self.store.publish(first)
        self.assertEqual(self.publication_bytes(), before)
        for forged in ({"id": stale["id"]}, {**stale, "parent": first["id"]}):
            with self.assertRaises(SnapshotConflict):
                self.store.publish(forged)
            self.assertEqual(self.publication_bytes(), before)
        self.assertEqual([r["id"] for r in self.store.history(first["id"])], [base["id"], first["id"]])

    def test_legacy_read_and_malformed_parent_validation(self):
        root = self.create({})
        body = {k: v for k, v in root.items() if k not in ("parent", "id")}
        legacy = {**body, "id": digest(canonical(body))}
        write_json(self.store.root / "snapshots" / (legacy["id"] + ".json"), legacy)
        self.assertEqual(self.store.get(legacy["id"]), legacy)
        self.store.publish(legacy)
        child = self.create({})
        self.assertEqual(child["parent"], legacy["id"])
        self.store.publish(child)
        before = self.publication_bytes()
        with self.assertRaises(SnapshotConflict):
            self.store.publish(legacy)
        self.assertEqual(self.publication_bytes(), before)
        for parent in ("", "../escape", "A" * 64, 0, False, {}, []):
            with self.subTest(parent=parent):
                invalid = self.rewrite(child, parent=parent)
                with self.assertRaisesRegex(Denied, "parent"):
                    self.store.get(invalid["id"])
                with self.assertRaises(Denied):
                    self.create({}, parent=parent)
        with self.assertRaises(Denied):
            self.create({}, parent="0" * 64)

    def test_concurrent_process_cas_has_one_winner_and_valid_history(self):
        base = self.create({})
        self.store.publish(base)
        candidates = [self.create({"a": value}) for value in ("left", "right")]
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(3), context.Queue()
        processes = [context.Process(target=cas_attempt,
                                    args=(str(self.store.root), s["id"], barrier, results))
                     for s in candidates]
        try:
            for process in processes:
                process.start()
            barrier.wait(timeout=10)
            outcomes = dict(results.get(timeout=15) for _ in processes)
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sorted(outcomes.values()), ["conflict", "published"])
            winner = next(sid for sid, result in outcomes.items() if result == "published")
            current = self.store.get()
            self.assertEqual(current["id"], winner)
            self.assertEqual([s["id"] for s in self.store.history(winner)], [base["id"], winner])
            self.assertEqual(len(list((self.store.root / "histories").glob("*.json"))), 2)
            pointer = read_json(self.store.root / "current.json")
            ids = read_json(self.store.root / "histories" / (pointer["history"] + ".json"))
            self.assertEqual(digest(canonical(ids)), pointer["history"])
            self.assertIn(self.store.read(current, "a"), (b"left", b"right"))
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()


if __name__ == "__main__":
    unittest.main()
