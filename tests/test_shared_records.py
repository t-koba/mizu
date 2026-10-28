"""Reusable persistence only: no source, prompt, routing or wake integration."""
import copy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mizu.errors import Denied, LimitExceeded
from mizu.fs import canonical, digest
from mizu.shared_records import (KindPolicy, MANIFEST_ROWS, RECORD_BYTES,
                                 RECEIPT_BYTES, SharedRecords)
from mizu.storage import MEMBERS
from mizu.project import Project
import mizu.storage as storage
from support import Fixture


def validate(subject, payload):
    if set(subject) != {"event"} or not isinstance(subject["event"], str) or set(payload) != {"text"}:
        raise Denied("Invalid test event binding")
    if not isinstance(payload["text"], str):
        raise Denied("Invalid test payload")


def envelope(event="one"):
    return {"schema": 1, "id": f"project/test/{event}", "kind": "test.event",
            "created_at": "2026-01-02T03:04:05+09:00", "writer": "host.test",
            "subject": {"event": event}, "payload": {"text": "hello"}}


class SharedRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policies = {"test.event": KindPolicy(frozenset({"host.test"}), validate)}
        self.store = SharedRecords(self.root, self.policies)

    def put(self, event="one"):
        record = envelope(event)
        return record, self.store.put(record, writer="host.test")

    def record_path(self, record_id):
        return self.root / self.store._record_name(record_id)

    def receipt_path(self, role, record_id):
        return (self.root / "shared-record-receipts" / digest(role.encode()) /
                (digest(record_id.encode()) + ".json"))

    def delivery(self):
        record, computed = self.put()
        manifest = self.store.freeze("worker", [record["id"]])
        return record, computed, manifest, asdict(manifest[0])

    def test_exact_replay_and_digest_recomputation(self):
        record, computed = self.put()
        self.assertEqual(computed, digest(canonical(record)))
        path = self.record_path(record["id"])
        before = path.stat()
        # Input key order is immaterial; disk bytes and digest are canonical.
        self.assertEqual(self.store.put(dict(reversed(list(record.items()))), writer="host.test"), computed)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.store.get(record["id"], expected_digest=computed), (record, computed))
        with self.assertRaises(Denied):
            self.store.get(record["id"], expected_digest="0" * 64)
        conflict = copy.deepcopy(record)
        conflict["payload"]["text"] = "different"
        with self.assertRaises(Denied):
            self.store.put(conflict, writer="host.test")
        self.assertEqual(path.read_bytes(), canonical(record))

    def test_default_deny_unknown_kind_and_authenticated_writer(self):
        for store, record, writer in [
            (SharedRecords(self.root), envelope(), "host.test"),
            (self.store, dict(envelope(), kind="unknown.kind"), "host.test"),
            (self.store, dict(envelope(), writer="other.writer"), "other.writer"),
            (self.store, envelope(), "other.writer"),
        ]:
            with self.subTest(writer=writer, kind=record["kind"]), self.assertRaises(Denied):
                store.put(record, writer=writer)
        self.assertFalse((self.root / "shared-records").exists())

    def test_malformed_envelope_and_closed_kind_validation(self):
        variants = [None, [], {}, dict(envelope(), extra=True)]
        for field, values in {
            "schema": [True, 2, "1"], "subject": [[], None, {"wrong": "field"}],
            "payload": [None, {"text": 1}, {"text": "ok", "extra": 1}],
            "created_at": ["2026-01-02T03:04:05", "nonsense", "2026-01-02", 4, "x" * 65],
        }.items():
            variants.extend(dict(envelope(), **{field: value}) for value in values)
        for field in ("id", "kind", "writer"):
            for value in ("", "plain", "../escape", "/absolute", "a//b", "a\\b", "x." + "y" * 256, [], 1):
                variants.append(dict(envelope(), **{field: value}))
        for record in variants:
            with self.subTest(record=record), self.assertRaises(Denied):
                self.store.put(record, writer="host.test")

    def test_json_values_size_and_depth_are_bounded(self):
        permissive = SharedRecords(self.root, {"test.event": KindPolicy(
            frozenset({"host.test"}), lambda subject, payload: None)})
        for value in (float("nan"), float("inf"), -float("inf"), {1: "value"}, (1, 2), object(), "\ud800"):
            record = dict(envelope(), payload={"value": value})
            with self.subTest(value=type(value)), self.assertRaises(Denied):
                permissive.put(record, writer="host.test")
        nested = []
        for _ in range(70):
            nested = [nested]
        with self.assertRaises(Denied):
            permissive.put(dict(envelope(), payload={"value": nested}), writer="host.test")
        record = dict(envelope(), payload={"text": "x" * RECORD_BYTES})
        with self.assertRaises(LimitExceeded):
            self.store.put(record, writer="host.test")
        smaller = SharedRecords(self.root, {"test.event": replace(self.policies["test.event"], maximum=100)})
        with self.assertRaises(LimitExceeded):
            smaller.put(envelope(), writer="host.test")

    def test_policy_bound_validation_and_no_validator_mutation(self):
        for policy in (replace(self.policies["test.event"], maximum=RECORD_BYTES + 1),
                       replace(self.policies["test.event"], maximum=True),
                       KindPolicy(frozenset({"unnamespaced"}), validate)):
            with self.assertRaises(Denied):
                SharedRecords(self.root, {"test.event": policy})
        def mutate(subject, payload):
            payload["text"] = "changed"
        store = SharedRecords(self.root, {"test.event": KindPolicy(frozenset({"host.test"}), mutate)})
        record = envelope()
        with self.assertRaises(Denied):
            store.put(record, writer="host.test")
        self.assertEqual(record, envelope())

    def test_partial_empty_omitted_selection_and_exact_receipt_retry(self):
        records = [self.put(name)[0] for name in ("one", "two", "three")]
        manifest = self.store.freeze("worker", [r["id"] for r in records])
        self.store.acknowledge("worker", manifest=manifest)
        self.store.acknowledge("worker", [], manifest=manifest)
        self.assertFalse((self.root / "shared-record-receipts").exists())
        self.store.acknowledge("worker", [asdict(manifest[1])], manifest=manifest)
        for index, entry in enumerate(manifest):
            self.assertEqual(self.store.received(entry.role, entry.record_id, entry.record_digest), index == 1)
        path = self.receipt_path("worker", manifest[1].record_id)
        original = path.read_bytes()
        with patch("mizu.shared_records.now", return_value="2027-01-01T00:00:00Z"):
            self.store.acknowledge("worker", [asdict(manifest[1])], manifest=manifest)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(set(json.loads(original)), {"role", "record_id", "record_digest", "acknowledged_at"})
        self.assertFalse(self.store.received("reviewer", manifest[1].record_id, manifest[1].record_digest))

    def test_selection_spoofing_and_whole_batch_validation(self):
        record, computed, manifest, selection = self.delivery()
        self.put("two")
        for field, values in {"role": ["reviewer", "../bad"], "record_id": ["project/test/two", "../bad"],
                              "record_digest": ["0" * 64, "bad"], "ack_token": ["0" * 64, [], None]}.items():
            for value in values:
                forged = dict(selection, **{field: value})
                with self.subTest(field=field, value=value), self.assertRaises(Denied):
                    self.store.acknowledge("worker", [forged], manifest=manifest)
        other = self.store.freeze("worker", ["project/test/two"])[0]
        with self.assertRaises(Denied):
            self.store.acknowledge("worker", [selection, asdict(other)], manifest=manifest)
        with self.assertRaises(Denied):
            self.store.acknowledge("worker", [selection, selection], manifest=manifest)
        with self.assertRaises(Denied):
            self.store.acknowledge("worker", [dict(selection, extra=True)], manifest=manifest)
        with self.assertRaises(Denied):
            self.store.acknowledge("reviewer", [selection], manifest=manifest)
        self.assertFalse(self.store.received("worker", record["id"], computed))

    def test_invalid_or_moved_manifest_is_not_reconstituted(self):
        record, computed, manifest, selection = self.delivery()
        for forged in ((), [manifest[0]], (replace(manifest[0], record_digest="0" * 64),),
                       (replace(manifest[0], role="reviewer"),), (manifest[0], manifest[0]), (selection,)):
            with self.subTest(forged=forged), self.assertRaises(Denied):
                self.store.acknowledge("worker", [selection], manifest=forged)
        # Even a well-formed old manifest cannot acknowledge changed bytes.
        changed = dict(record, payload={"text": "tampered"})
        self.record_path(record["id"]).write_bytes(canonical(changed))
        with self.assertRaises(Denied):
            self.store.acknowledge("worker", [selection], manifest=manifest)
        self.assertFalse(self.receipt_path("worker", record["id"]).exists())

    def test_manifest_bounds_and_duplicate_identities(self):
        record, _ = self.put()
        for ids in ([record["id"], record["id"]], [record["id"]] * (MANIFEST_ROWS + 1), "not a list"):
            with self.assertRaises(Denied):
                self.store.freeze("worker", ids)
        with self.assertRaises(Denied):
            self.store.freeze("../worker", [])

    def test_corrupt_noncanonical_misplaced_and_oversized_record_reads(self):
        record, _ = self.put()
        path = self.record_path(record["id"])
        variants = [b"{broken", b"[]", json.dumps(record).encode(),
                    canonical(dict(record, id="project/test/other")),
                    canonical(record).replace(b'"text":"hello"', b'"text":NaN'),
                    b" " * (RECORD_BYTES + 1)]
        for raw in variants:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:25]), self.assertRaises((Denied, LimitExceeded)):
                self.store.get(record["id"])
            with self.assertRaises((Denied, LimitExceeded)):
                self.store.put(record, writer="host.test")
            self.assertEqual(path.read_bytes(), raw)

    def test_corrupt_receipts_are_never_repaired(self):
        record, computed, manifest, selection = self.delivery()
        self.store.acknowledge("worker", [selection], manifest=manifest)
        path = self.receipt_path("worker", record["id"])
        valid = json.loads(path.read_bytes())
        variants = [b"{broken", b"null", json.dumps(valid).encode(), b" " * (RECEIPT_BYTES + 1)]
        for field, value in (("role", "reviewer"), ("record_id", "project/test/other"),
                             ("record_digest", "0" * 64), ("acknowledged_at", "2026-01-02")):
            variants.append(canonical(dict(valid, **{field: value})))
        for raw in variants:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:25]), self.assertRaises((Denied, LimitExceeded)):
                self.store.received("worker", record["id"], computed)
            with self.assertRaises((Denied, LimitExceeded)):
                self.store.acknowledge("worker", [selection], manifest=manifest)
            self.assertEqual(path.read_bytes(), raw)

    def test_record_and_receipt_symlinks_and_hardlinks_fail_closed(self):
        record, computed, manifest, selection = self.delivery()
        self.store.acknowledge("worker", [selection], manifest=manifest)
        for path, check in ((self.record_path(record["id"]), lambda: self.store.put(record, writer="host.test")),
                            (self.receipt_path("worker", record["id"]), lambda: self.store.acknowledge(
                                "worker", [selection], manifest=manifest))):
            original = path.read_bytes()
            target = self.root / "target"
            target.write_bytes(original)
            for link in (os.symlink, os.link):
                path.unlink()
                try:
                    link(target, path)
                except (OSError, NotImplementedError):
                    path.write_bytes(original)
                    continue  # Symlink creation may be unavailable on Windows.
                with self.subTest(path=path.name, link=link.__name__), self.assertRaises(Denied):
                    check()
                self.assertEqual(target.read_bytes(), original)
                path.unlink()
                path.write_bytes(original)
            target.unlink()

    def test_unsafe_directories_and_special_files(self):
        for directory in ("shared-records", "shared-record-receipts"):
            target = self.root / "outside"
            target.mkdir(exist_ok=True)
            path = self.root / directory
            try:
                path.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                continue
            try:
                if directory == "shared-records":
                    with self.assertRaises(Denied):
                        self.put()
                else:
                    record, computed, manifest, selected = self.delivery()
                    with self.assertRaises(Denied):
                        self.store.acknowledge("worker", [selected], manifest=manifest)
                self.assertEqual(list(target.iterdir()), [])
            finally:
                path.unlink()
        record, _ = self.put()
        path = self.record_path(record["id"])
        path.unlink()
        path.mkdir()
        with self.assertRaises(Denied):
            self.store.get(record["id"])

    def test_one_mib_payload_fits_record_envelope_framing(self):
        record = envelope()
        record["payload"]["text"] = "x" * (1024 * 1024)
        raw = canonical(record)
        self.assertGreater(len(raw), 1024 * 1024)
        self.assertLessEqual(len(raw), RECORD_BYTES)
        computed = self.store.put(record, writer="host.test")
        self.assertEqual(self.store.get(record["id"]), (record, computed))

    def test_exact_byte_boundary(self):
        record = envelope()
        record["payload"]["text"] = ""
        record["payload"]["text"] = "x" * (RECORD_BYTES - len(canonical(record)))
        self.assertEqual(len(canonical(record)), RECORD_BYTES)
        computed = self.store.put(record, writer="host.test")
        self.assertEqual(self.store.get(record["id"])[1], computed)
        record["payload"]["text"] += "x"
        with self.assertRaises(LimitExceeded):
            self.store.put(record, writer="host.test")

    def test_concurrent_receipt_retry_preserves_first_acknowledgement(self):
        record, computed, manifest, selected = self.delivery()
        from mizu.fs import atomic_write
        first = {"role": "worker", "record_id": record["id"], "record_digest": computed,
                 "acknowledged_at": "2025-12-31T00:00:00Z"}
        def racing_write(path, raw, *, exclusive):
            atomic_write(path, canonical(first), exclusive=True)
            atomic_write(path, raw, exclusive=exclusive)
        with patch("mizu.shared_records.atomic_write", side_effect=racing_write):
            self.store.acknowledge("worker", [selected], manifest=manifest)
        self.assertEqual(self.receipt_path("worker", record["id"]).read_bytes(), canonical(first))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files require POSIX")
    def test_fifo_is_refused_without_blocking(self):
        record, _ = self.put()
        path = self.record_path(record["id"])
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(Denied):
            self.store.get(record["id"])
        with self.assertRaises(Denied):
            self.store.put(record, writer="host.test")

    def test_backup_restore_membership(self):
        self.assertIn("shared-records", MEMBERS)
        self.assertIn("shared-record-receipts", MEMBERS)

    def test_sharded_canonical_and_namespace_isolation(self):
        one, _ = self.put("one")
        other = dict(envelope("other"), id="operator/sample/other")
        computed = self.store.put(other, writer="host.test")
        self.assertEqual(self.store._record_name("project/test/one"),
                         f"shared-records/project/{digest(b'project/test/one')}.json")
        self.assertEqual(self.store._record_name("operator/sample/other"),
                         f"shared-records/operator/{digest(b'operator/sample/other')}.json")
        self.assertTrue((self.root / self.store._record_name(other["id"])).is_file())
        self.assertFalse((self.root / self.store._legacy_record_name(other["id"])).exists())
        self.assertEqual({row[0]["id"] for row in self.store.records(namespace="project")},
                         {"project/test/one"})
        self.assertEqual({row[0]["id"] for row in self.store.records(namespace="operator")},
                         {"operator/sample/other"})
        self.assertEqual({row[0]["id"] for row in self.store.records()},
                         {"project/test/one", "operator/sample/other"})
        for bad in ("", "../escape", "Bad", "a/b", "a:b", "a.b", 1):
            with self.subTest(namespace=bad), self.assertRaises(Denied):
                list(self.store.records(namespace=bad))
        long_namespace = "n" * 100
        long_record = dict(envelope("long"), id=f"{long_namespace}/event")
        long_record["subject"] = {"event": "long"}
        self.store.put(long_record, writer="host.test")
        partition = (self.root / self.store._record_name(long_record["id"])).parent.name
        self.assertLessEqual(len(partition), 34)
        self.assertEqual(list(self.store.records(namespace=long_namespace))[0][0], long_record)

    def test_legacy_flat_read_replay_preserved_without_second_copy(self):
        record = envelope("legacy")
        record["id"] = "project/test/legacy"
        record["subject"] = {"event": "legacy"}
        raw = self.store._validate(record)
        legacy = self.root / self.store._legacy_record_name(record["id"])
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(raw)
        self.assertFalse((self.root / self.store._record_name(record["id"])).exists())
        self.assertEqual(self.store.get(record["id"]), (record, digest(raw)))
        self.assertEqual(self.store.put(dict(record), writer="host.test"), digest(raw))
        self.assertFalse((self.root / self.store._record_name(record["id"])).exists())
        self.assertEqual(legacy.read_bytes(), raw)
        self.assertEqual({row[0]["id"] for row in self.store.records()}, {record["id"]})
        # Namespace scans cover only the sharded partition; the flat legacy
        # stays retrievable by exact get and by the unfiltered scan.
        self.assertEqual(list(self.store.records(namespace="project")), [])
        with self.assertRaises(Denied):
            self.store.put(dict(record, payload={"text": "different"}), writer="host.test")
        self.assertEqual(legacy.read_bytes(), raw)

    def test_conflicting_duplicates_fail_closed_without_repair(self):
        record = envelope("dup")
        record["id"] = "project/test/dup"
        record["subject"] = {"event": "dup"}
        raw = self.store._validate(record)
        canonical = self.root / self.store._record_name(record["id"])
        legacy = self.root / self.store._legacy_record_name(record["id"])
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(raw)
        legacy.write_bytes(raw)
        self.assertEqual(self.store.get(record["id"])[1], digest(raw))
        self.assertEqual(self.store.put(dict(record), writer="host.test"), digest(raw))
        self.assertEqual({row[0]["id"] for row in self.store.records()}, {record["id"]})
        other = dict(record, created_at="2026-01-03T03:04:05+09:00")
        other_raw = self.store._validate(other)
        self.assertNotEqual(raw, other_raw)
        canonical.write_bytes(other_raw)
        for check in (lambda: self.store.get(record["id"]),
                      lambda: self.store.put(dict(record), writer="host.test"),
                      lambda: list(self.store.records())):
            with self.subTest(check=check), self.assertRaises(Denied):
                check()
        self.assertEqual(canonical.read_bytes(), other_raw)
        self.assertEqual(legacy.read_bytes(), raw)

    def test_unknown_kind_duplicate_cannot_hide_a_known_record(self):
        record = envelope('shadow')
        record['id'] = 'project/test/shadow'
        record['subject'] = {'event': 'shadow'}
        raw = self.store._validate(record)
        legacy = self.root / self.store._legacy_record_name(record['id'])
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(raw)
        unknown = dict(record, kind='other.kind')
        canonical_path = self.root / self.store._record_name(record['id'])
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(canonical(unknown))
        with self.assertRaises(Denied):
            list(self.store.records())
        with self.assertRaises(Denied):
            self.store.get(record['id'])

    def test_enumeration_validates_once_without_second_get(self):
        self.put("one")
        self.put("two")
        with patch.object(SharedRecords, "get", side_effect=AssertionError("second read")):
            self.assertEqual({row[0]["id"] for row in self.store.records()},
                             {"project/test/one", "project/test/two"})
            self.assertEqual({row[0]["id"] for row in self.store.records(namespace="project")},
                             {"project/test/one", "project/test/two"})
        # Misplaced files fail closed in both scans.
        record = envelope("misplaced")
        record["id"] = "operator/sample/misplaced"
        record["subject"] = {"event": "misplaced"}
        raw = self.store._validate(record)
        wrong = self.root / f"shared-records/project/{digest(record['id'].encode())}.json"
        wrong.parent.mkdir(parents=True, exist_ok=True)
        wrong.write_bytes(raw)
        with self.assertRaises(Denied):
            list(self.store.records(namespace="project"))
        with self.assertRaises(Denied):
            list(self.store.records())


class SharedRecordsBackupTests(Fixture):
    def test_backup_restore_preserves_records_and_receipts(self):
        policies = {"test.event": KindPolicy(frozenset({"host.test"}), validate)}
        store = SharedRecords(self.project.root, policies)
        record = envelope()
        record_digest = store.put(record, writer="host.test")
        manifest = store.freeze("worker", [record["id"]])
        store.acknowledge("worker", [asdict(manifest[0])], manifest=manifest)

        self.project.set_control(paused=True)
        archive = self.root / "shared-records-backup.tar.gz"
        storage.backup(self.project, archive)
        storage.restore(self.config, "shared-records-restored", archive)
        restored = Project(self.config, "shared-records-restored")
        restored_store = SharedRecords(restored.root, policies)
        self.assertEqual(restored_store.get(record["id"]), (record, record_digest))
        self.assertTrue(restored_store.received("worker", record["id"], record_digest))


if __name__ == "__main__":
    unittest.main()
