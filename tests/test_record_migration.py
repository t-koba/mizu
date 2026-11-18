import contextlib
import io
import json
from unittest.mock import patch

from mizu import ci_waits, operator_records, record_migration
from mizu.cli import main
from mizu.fs import canonical, digest
from mizu.operator_record_index import DIRECTORY, catalog
from mizu.shared_records import RECORD_BYTES, SharedRecords

from support import Fixture


class RecordMigrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.shared = operator_records.store(self.project)
        self.envelope = dict(schema=1, id="operator/sample/notice", kind="operator.notice",
                             writer="operator.cli", created_at="2026-01-01T00:00:00Z", subject={},
                             payload=dict(action="publish", body="Notice", addressee="worker", target=None))
        self.raw = canonical(self.envelope)
        self.flat, self.shard = self.paths(self.shared, self.envelope["id"])
        self.flat.parent.mkdir(exist_ok=True)

    def paths(self, store, record_id):
        return (self.project.root / "shared-records" / f"{digest(record_id.encode())}.json",
                self.project.root / store.record_name(record_id))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.file), "migrate-records", self.project.name, *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_preview_apply_and_retry(self):
        self.flat.write_bytes(self.raw)
        before = sorted(self.project.root.rglob("*"))
        code, out, err = self.cli()
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual((report["candidates"], report["converted"], report["applied"]), (1, 0, False))
        self.assertEqual(report["records"], [dict(flat=self.flat.relative_to(self.project.root).as_posix(),
                                                 canonical=self.shared.record_name(self.envelope["id"]),
                                                 digest=digest(self.raw))])
        self.assertEqual(sorted(self.project.root.rglob("*")), before)
        self.assertEqual(self.flat.read_bytes(), self.raw)
        self.assertFalse(self.shard.parent.exists())

        code, out, err = self.cli("--apply")
        self.assertEqual((code, out), (77, ""))
        self.assertIn("Pause", err)
        self.assertEqual(self.flat.read_bytes(), self.raw)
        self.assertFalse(self.shard.exists())

        self.project.set_control(paused=True)
        code, out, err = self.cli("--apply")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual((report["candidates"], report["converted"], report["applied"]), (1, 1, True))
        self.assertEqual(self.shard.read_bytes(), self.raw)
        self.assertFalse(self.flat.exists())
        self.assertEqual(self.shared.get(self.envelope["id"], expected_digest=digest(self.raw)),
                         (self.envelope, digest(self.raw)))
        report = record_migration.apply(self.project)
        self.assertTrue(report["ok"])
        self.assertEqual((report["candidates"], report["converted"]), (0, 0))

    def test_conflict_preserves_both_copies_and_identical_retry_converges(self):
        self.project.set_control(paused=True)
        self.flat.write_bytes(self.raw)
        self.shard.parent.mkdir()
        other = canonical(dict(self.envelope, payload=dict(self.envelope["payload"], body="Different")))
        self.shard.write_bytes(other)
        for operation in (record_migration.plan, record_migration.apply):
            with self.subTest(operation=operation.__name__):
                report = operation(self.project)
                self.assertFalse(report["ok"])
                self.assertEqual((report["candidates"], report["converted"], len(report["conflicts"])), (0, 0, 1))
                self.assertEqual(self.flat.read_bytes(), self.raw)
                self.assertEqual(self.shard.read_bytes(), other)

        self.shard.write_bytes(self.raw)
        with patch.object(record_migration, "atomic_write", side_effect=AssertionError("Existing file rewritten")):
            report = record_migration.apply(self.project)
        self.assertEqual((report["candidates"], report["converted"]), (1, 1))
        self.assertFalse(self.flat.exists())
        self.assertEqual(self.shard.read_bytes(), self.raw)

    def test_malformed_oversize_and_invalid_records_are_not_moved(self):
        self.project.set_control(paused=True)
        bad_policy = canonical(dict(self.envelope, payload={}))
        for raw in (b"{", b"x" * (RECORD_BYTES + 1), self.raw.rstrip(b"\n"), bad_policy):
            with self.subTest(case=len(raw)):
                self.flat.write_bytes(raw)
                report = record_migration.apply(self.project)
                self.assertFalse(report["ok"])
                self.assertEqual((report["candidates"], report["converted"], len(report["errors"])), (0, 0, 1))
                self.assertEqual(self.flat.read_bytes(), raw)
                self.assertFalse(self.shard.parent.exists())

    def test_failed_canonical_verification_leaves_flat_file(self):
        self.project.set_control(paused=True)
        self.flat.write_bytes(self.raw)
        real_read = SharedRecords._read

        def bad_read(store, name, bound):
            value = real_read(store, name, bound)
            if name == self.shared.record_name(self.envelope["id"]) and value is not None:
                return b"bad read-back"
            return value

        with patch.object(SharedRecords, "_read", bad_read):
            report = record_migration.apply(self.project)
        self.assertEqual((report["candidates"], report["converted"], len(report["errors"])), (1, 0, 1))
        self.assertEqual(self.flat.read_bytes(), self.raw)
        self.assertEqual(self.shard.read_bytes(), self.raw)

    def test_scan_budget_counts_even_non_json_entries_without_recursing(self):
        self.shard.parent.mkdir()
        self.shard.write_bytes(b"invalid but not scanned")
        (self.flat.parent / "scratch").touch()
        with patch.object(record_migration, "SCAN_ENTRIES", 1):
            report = record_migration.plan(self.project)
        self.assertEqual((report["entries_examined"], report["truncated"], report["errors"]), (1, True, []))
        report = record_migration.plan(self.project)
        self.assertEqual((report["entries_examined"], report["candidates"], report["ok"]), (2, 0, True))

    def test_moving_foreign_record_discards_only_its_legacy_catalog_projection(self):
        self.project.set_control(paused=True)
        self.flat.write_bytes(self.raw)
        ci = ci_waits.shared_store(self.project.work_items)
        subject = dict(work_item="sample", rev=1, holder="worker")
        foreign = dict(self.envelope, id="ci-wait:sample:1", kind="ci.wait", writer="host.ci",
                       subject=subject, payload=dict(subject, kind="ci", trust="external-untrusted"))
        flat, shard = self.paths(ci, foreign["id"])
        flat.write_bytes(canonical(foreign))
        refs = self.project.root / DIRECTORY
        before = catalog(self.shared)
        self.assertEqual({ref.name for ref in refs.iterdir()}, {self.flat.name, flat.name})

        report = record_migration.apply(self.project)
        self.assertEqual((report["candidates"], report["converted"]), (2, 2))
        self.assertEqual({ref.name for ref in refs.iterdir()}, {self.flat.name})
        self.assertEqual(catalog(self.shared, readonly=True), before)
        self.assertEqual(shard.read_bytes(), canonical(foreign))
        self.assertEqual(ci.get(foreign["id"]), (foreign, digest(canonical(foreign))))
