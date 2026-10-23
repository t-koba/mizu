"""Externally recorded publication receipts: durable, create-only, discoverable.

Offline only: no adapter, no network, no real publication.
"""
import dataclasses
import json
import io
import uuid
from unittest.mock import patch

from support import Fixture
from mizu.cli import execute, parser
from mizu.errors import Denied
from mizu.fs import mkdir
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu.runtime import Context

SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "c" * 64
OTHER_DIGEST = "d" * 64
BRANCH = "published"
APPROVAL = "operator-approval-1"


def with_caps(fixture, name, extra):
    role = fixture.config.roles[name]
    new_role = dataclasses.replace(role, capabilities=tuple(list(role.capabilities) + extra))
    config = dataclasses.replace(fixture.config, roles={**fixture.config.roles, name: new_role})
    return config, new_role


def make_context(fixture, config, role):
    run = fixture.project.root / "runs" / uuid.uuid4().hex
    mkdir(run)
    snap = fixture.project.snapshots.get()
    return Context(config, fixture.project, role, run, snap, fixture.project.workspace)


class ExternalPublicationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.root = self.project.root

    def record(self, **overrides):
        arguments = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                     "approval": APPROVAL, "projection": public_source_identity({})}
        arguments.update(overrides)
        return Publication.record_external(self.root, **arguments)

    def test_recording_creates_evidence_file_and_read_receipt_returns_it(self):
        record = self.record()
        path = self.root / "evidence" / "publications" / (record["publication_id"] + ".external.json")
        self.assertTrue(path.is_file())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored, record)
        self.assertEqual(set(stored), {"schema", "publication_id", "op", "branch",
                                        "published_sha", "code_digest", "approval",
                                        "source_commit", "run", "recorded_at",
                                        "projection_schema", "projection_algorithm", "projection_digest"})
        self.assertEqual(stored["schema"], 3)
        self.assertEqual(stored["op"], "external")
        self.assertIsNone(stored["source_commit"])
        self.assertIsNone(stored["run"])
        # The stored record is what a reader validates and returns.
        self.assertEqual(Publication.read_receipt(self.root, record["publication_id"]), record)

    def test_identical_re_record_is_an_idempotent_replay(self):
        first = self.record()
        second = self.record()
        self.assertEqual(second, first)
        path = self.root / "evidence" / "publications" / (first["publication_id"] + ".external.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), first)

    def test_re_record_with_a_different_approval_is_refused(self):
        first = self.record()
        with self.assertRaisesRegex(Denied, first["publication_id"]):
            self.record(approval="another-approval")
        path = self.root / "evidence" / "publications" / (first["publication_id"] + ".external.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["approval"], APPROVAL)

    def test_invalid_inputs_are_refused(self):
        cases = [
            {"published_sha": "not-a-sha"},
            {"published_sha": "A" * 40},
            {"published_sha": "a" * 39},
            {"code_digest": "c" * 63},
            {"code_digest": "C" * 64},
            {"branch": "bad:branch"},
            {"branch": "/leading"},
            {"branch": "trailing/"},
            {"branch": ""},
            {"source_commit": "not-a-sha"},
            {"source_commit": "a" * 41},
            {"approval": ""},
            {"approval": "   "},
            {"approval": "x" * 257},
            {"approval": "line\nbreak"},
            {"approval": "bell\x07"},
            {"run": ""},
            {"run": "x" * 257},
            {"run": "tab\tchar"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(Denied):
                self.record(**overrides)
        self.assertEqual(Publication.list_receipts(self.root)["publications"], [])

    def test_list_receipts_filters_by_digest_and_branch_and_skips_invalid_records(self):
        first = self.record()
        second = self.record(published_sha=OTHER_SHA, code_digest=OTHER_DIGEST, branch="other")
        # A schema-1 receipt of the product's own publish path is listed too.
        product = Publication(self.root, "e" * 40, "f" * 64, "product", "push",
                              projection=public_source_identity({}))
        intent, _ = product.prepare("approval-ref", "run-ref")
        product.mark_dispatch()
        product.complete(intent, "1" * 40)
        # An unvalidatable record is skipped, never returned unvalidated.
        broken = self.root / "evidence" / "publications" / ("0" * 64 + ".external.json")
        broken.write_text("{}", encoding="utf-8")
        listing = Publication.list_receipts(self.root)
        self.assertEqual(set(listing), {"publications", "total", "truncated", "scan_stopped"})
        self.assertFalse(listing["truncated"])
        self.assertFalse(listing["scan_stopped"])
        listed = listing["publications"]
        ids = [row["publication_id"] for row in listed]
        self.assertIn(first["publication_id"], ids)
        self.assertIn(second["publication_id"], ids)
        self.assertIn(product.id, ids)
        self.assertNotIn("0" * 64, ids)
        self.assertEqual(ids, sorted(ids))
        row = next(row for row in listed if row["publication_id"] == first["publication_id"])
        self.assertEqual(set(row), {"publication_id", "op", "branch", "published_sha",
                                    "code_digest", "approval", "source_commit", "run",
                                    "projection_schema", "projection_algorithm", "projection_digest"})
        self.assertEqual(row["op"], "external")
        self.assertEqual(row["published_sha"], SHA)
        self.assertEqual(
            [r["publication_id"] for r in Publication.list_receipts(
                self.root, code_digest=DIGEST)["publications"]],
            [first["publication_id"]])
        self.assertEqual(
            [r["publication_id"] for r in Publication.list_receipts(
                self.root, branch="other")["publications"]],
            [second["publication_id"]])
        self.assertEqual(Publication.list_receipts(self.root, branch="absent")["publications"], [])
        # A caller that knows the published SHA can find its receipt directly.
        self.assertEqual(
            [r["publication_id"] for r in Publication.list_receipts(
                self.root, published_sha=SHA)["publications"]],
            [first["publication_id"]])
        self.assertEqual(
            Publication.list_receipts(self.root, published_sha="2" * 40)["publications"], [])
        for bad in ({"code_digest": "short"}, {"branch": "bad:branch"},
                    {"published_sha": "not-a-sha"}, {"limit": 0}, {"limit": 101}):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                Publication.list_receipts(self.root, **bad)

    def test_list_receipts_reports_a_truncated_page(self):
        for index in range(3):
            self.record(published_sha="%040x" % index, code_digest="%064x" % index)
        page = Publication.list_receipts(self.root, limit=2)
        self.assertEqual(len(page["publications"]), 2)
        self.assertEqual(page["total"], 3)
        self.assertTrue(page["truncated"])
        exact = Publication.list_receipts(self.root, published_sha="%040x" % 0)
        self.assertEqual(exact["total"], 1)
        self.assertFalse(exact["truncated"])
        self.assertFalse(exact["scan_stopped"])

    def test_list_receipts_reports_a_stopped_scan_instead_of_an_absent_receipt(self):
        from mizu import publication as publication_module
        for index in range(3):
            self.record(published_sha="%040x" % index, code_digest="%064x" % index)
        with patch.object(publication_module, "RECEIPT_RECORD_LIMIT", 1):
            page = Publication.list_receipts(self.root)
        # The scan stopped at its bound, so the count is a lower bound and the
        # page never claims that no receipt exists.
        self.assertTrue(page["scan_stopped"])
        self.assertLess(page["total"], 3)
        complete = Publication.list_receipts(self.root)
        self.assertFalse(complete["scan_stopped"])
        self.assertEqual(complete["total"], 3)

    def test_oversized_record_is_skipped_and_never_overwritten(self):
        record = self.record()
        # A record larger than the read bound must not deny the valid receipts
        # beside it, and must never be overwritten by a new recording.
        oversized = self.root / "evidence" / "publications" / ("0" * 64 + ".external.json")
        oversized.write_bytes(b'{"x": "' + b"y" * 20000 + b'"}')
        listing = Publication.list_receipts(self.root)
        self.assertEqual([row["publication_id"] for row in listing["publications"]],
                         [record["publication_id"]])
        publication_id = record["publication_id"]
        blocking = self.root / "evidence" / "publications" / (publication_id + ".external.json")
        blocking.write_bytes(b'{"x": "' + b"y" * 20000 + b'"}')
        with self.assertRaises(Denied):
            self.record()
        self.assertEqual(blocking.read_bytes(), b'{"x": "' + b"y" * 20000 + b'"}')

    def test_missing_publications_directory_lists_nothing(self):
        empty = self.root / "empty-project"
        listing = Publication.list_receipts(empty)
        self.assertEqual(listing["publications"], [])
        self.assertEqual(listing["total"], 0)
        self.assertFalse(listing["truncated"])

    def test_ci_wait_pins_an_external_receipt_branch_and_sha(self):
        config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "poll_enabled": True, "command": ["configured-adapter"], "poll_max_branches": 2})
        project = type(self.project)(config, "sample")
        store = project.work_items
        record = Publication.record_external(
            project.root, branch=BRANCH, published_sha=SHA, code_digest=DIGEST,
            approval=APPROVAL, source_commit="e" * 40, run="operator-run",
            projection=public_source_identity({}))
        wait = {"kind": "ci", "publication_id": record["publication_id"], "checks": ["unit"]}
        seed = store.create(actor="worker", idempotency_key="external-wait",
                            title="Observe CI")["record"]["work_item"]
        item = store.transition(seed["id"], expected_rev=1, status="blocked",
                                actor="worker", wait=wait)
        self.assertEqual(item["wait"], {"kind": "ci", "publication_id": record["publication_id"],
                                        "branch": BRANCH, "sha": SHA, "checks": ["unit"]})
        self.assertEqual(store.get(item["id"])["wait"], item["wait"])

    def test_vcs_read_publications_op_lists_without_the_adapter(self):
        record = self.record()
        config, role = with_caps(self, "worker", ["vcs_read"])
        ctx = make_context(self, config, role)
        with patch("mizu.vcs.read_via") as reader:
            out = ctx.handle("vcs_read", {"op": "publications"})
        reader.assert_not_called()
        self.assertEqual(out["op"], "publications")
        self.assertEqual(out["trust"], "recorded-local-untrusted")
        self.assertEqual([row["publication_id"] for row in out["result"]["publications"]],
                         [record["publication_id"]])
        self.assertEqual(out["result"]["publications"][0]["published_sha"], SHA)
        evidence = json.loads((ctx.run_dir / out["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["trust"], "recorded-local-untrusted")
        filtered = ctx.handle("vcs_read", {"op": "publications", "code_digest": DIGEST})
        self.assertEqual(len(filtered["result"]["publications"]), 1)
        self.assertEqual(filtered["result"]["total"], 1)
        self.assertFalse(filtered["result"]["truncated"])
        by_sha = ctx.handle("vcs_read", {"op": "publications", "published_sha": SHA})
        self.assertEqual([row["publication_id"] for row in by_sha["result"]["publications"]],
                         [record["publication_id"]])
        self.assertEqual(ctx.handle("vcs_read", {"op": "publications", "code_digest": OTHER_DIGEST})
                         ["result"]["publications"], [])
        for extra in ({"sha": SHA}, {"publication_id": record["publication_id"]}, {"id": "x"}):
            with self.subTest(extra=extra), self.assertRaises(Denied):
                ctx.handle("vcs_read", {"op": "publications", **extra})

    def test_cli_record_and_list_round_trip(self):
        args = parser().parse_args([
            "--config", str(self.file), "publication", "record", "sample",
            "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
            "--approval", APPROVAL, "--source-commit", "e" * 40, "--run", "operator-run",
            "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
            "--projection-digest", public_source_identity({})["digest"]])
        record = execute(args)
        self.assertEqual(record["op"], "external")
        self.assertEqual(record["schema"], 3)
        self.assertEqual(record["projection_digest"], public_source_identity({})["digest"])
        self.assertEqual(record["projection_algorithm"], public_source_identity({})["algorithm"])
        self.assertEqual(record["projection_schema"], 1)
        self.assertEqual(record["published_sha"], SHA)
        self.assertEqual(record["source_commit"], "e" * 40)
        listed = execute(parser().parse_args(
            ["--config", str(self.file), "publication", "list", "sample"]))
        self.assertEqual(set(listed), {"publications", "total", "truncated", "scan_stopped"})
        self.assertEqual([row["publication_id"] for row in listed["publications"]],
                         [record["publication_id"]])
        by_sha = execute(parser().parse_args(
            ["--config", str(self.file), "publication", "list", "sample",
             "--published-sha", SHA]))
        self.assertEqual([row["publication_id"] for row in by_sha["publications"]],
                         [record["publication_id"]])
        self.assertEqual(listed["publications"][0]["approval"], APPROVAL)
        filtered = execute(parser().parse_args(
            ["--config", str(self.file), "publication", "list", "sample", "--branch", BRANCH]))
        self.assertEqual(len(filtered["publications"]), 1)
        self.assertEqual(execute(parser().parse_args(
            ["--config", str(self.file), "publication", "list", "sample",
             "--branch", "absent"]))["publications"], [])

    def test_cli_refuses_missing_or_invalid_projection_identity(self):
        base = ["--config", str(self.file), "publication", "record", "sample",
                "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
                "--approval", APPROVAL]
        flags = {"--projection-schema": "1", "--projection-algorithm": public_source_identity({})["algorithm"],
                 "--projection-digest": public_source_identity({})["digest"]}
        for missing in flags:
            argv = base + [part for key, value in flags.items() if key != missing for part in (key, value)]
            with self.subTest(missing=missing), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    parser().parse_args(argv)
                self.assertEqual(exc.exception.code, 2)
        for key, invalid in (("--projection-schema", "2"), ("--projection-algorithm", "x" * 129),
                             ("--projection-digest", "not-a-digest")):
            values = {**flags, key: invalid}
            argv = base + [part for pair in values.items() for part in pair]
            with self.subTest(key=key), self.assertRaises(Denied):
                execute(parser().parse_args(argv))
        self.assertFalse((self.root / "evidence/publications").exists())
