"""Externally recorded publication receipts: durable, create-only, discoverable.

Offline only: no adapter, no network, no real publication.
"""
import dataclasses
import json
import io
import uuid
from unittest.mock import Mock, patch

from support import Fixture, set_project_target
from mizu import publication
from mizu.cli import execute, parser
from mizu.config import PublicationEvidence
from mizu.errors import Denied
from mizu.fs import canonical, digest, mkdir
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
        # Control recorded_at so the newest-first order is unambiguous.
        with patch("mizu.publication.now", side_effect=[
                "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"]):
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
        self.assertEqual(set(listing), {"publications", "total", "truncated", "scan_stopped",
                                        "next_cursor"})
        self.assertFalse(listing["truncated"])
        self.assertFalse(listing["scan_stopped"])
        listed = listing["publications"]
        ids = [row["publication_id"] for row in listed]
        self.assertIn(first["publication_id"], ids)
        self.assertIn(second["publication_id"], ids)
        self.assertIn(product.id, ids)
        self.assertNotIn("0" * 64, ids)
        self.assertEqual(ids, [second["publication_id"], first["publication_id"], product.id])
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

    def test_listing_pages_through_every_receipt_newest_first(self):
        stamps = [f"2026-02-{index + 1:02d}T00:00:00+00:00" for index in range(5)]
        records = []
        with patch("mizu.publication.now", side_effect=stamps):
            for index in range(5):
                records.append(self.record(
                    branch=BRANCH if index < 3 else "other",
                    published_sha="%040x" % (index + 1),
                    code_digest="%064x" % (index + 1),
                    approval="approval-%d" % index))
        newest = [record["publication_id"] for record in reversed(records)]
        keys = {"publications", "total", "truncated", "scan_stopped", "next_cursor"}
        seen = []
        cursor = None
        pages = [([newest[0], newest[1]], True), ([newest[2], newest[3]], True),
                 ([newest[4]], False)]
        for expected, more in pages:
            page = Publication.list_receipts(self.root, limit=2, cursor=cursor)
            self.assertEqual(set(page), keys)
            self.assertEqual(page["total"], 5)
            self.assertFalse(page["scan_stopped"])
            self.assertEqual(page["truncated"], more)
            ids = [row["publication_id"] for row in page["publications"]]
            self.assertEqual(ids, expected)
            seen.extend(ids)
            cursor = page["next_cursor"]
            if more:
                self.assertRegex(cursor, r"\Apub1\|[0-9]+\|[0-9a-f]{64}\Z")
        self.assertIsNone(cursor)
        self.assertEqual(seen, newest)
        self.assertEqual(len(set(seen)), len(newest))

        # Filters keep their own full match count across pages.
        branch_seen = []
        cursor = None
        for expected, more in (([newest[2], newest[3]], True), ([newest[4]], False)):
            page = Publication.list_receipts(self.root, limit=2, cursor=cursor, branch=BRANCH)
            self.assertEqual(page["total"], 3)
            self.assertEqual(page["truncated"], more)
            self.assertEqual([row["publication_id"] for row in page["publications"]], expected)
            branch_seen.extend(row["publication_id"] for row in page["publications"])
            cursor = page["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(branch_seen, [newest[2], newest[3], newest[4]])
        digest_id = records[0]["publication_id"]
        page = Publication.list_receipts(self.root, cursor=cursor,
                                         code_digest=records[0]["code_digest"])
        self.assertEqual(page["total"], 1)
        self.assertEqual([row["publication_id"] for row in page["publications"]], [digest_id])
        self.assertFalse(page["truncated"])
        self.assertIsNone(page["next_cursor"])

        # The writer's index is a complete inventory: bounds smaller than the
        # five-receipt inventory do not hide a receipt from a narrow page.
        directory = self.root / "evidence" / "publications"
        names = sorted(path.name for path in directory.iterdir()
                       if publication.RECEIPT_FILE.fullmatch(path.name))
        prefix = {name.split(".")[0] for name in names[:3]}
        self.assertEqual(len(prefix), 3)
        narrow = PublicationEvidence(name_processing_limit=3, record_limit=3)
        page = Publication.list_receipts(self.root, limit=1, policy=narrow)
        self.assertEqual(page["total"], 5)
        self.assertFalse(page["scan_stopped"])
        self.assertEqual([row["publication_id"] for row in page["publications"]],
                         [newest[0]])
        narrow_seen = [newest[0]]
        cursor = page["next_cursor"]
        while cursor is not None:
            page = Publication.list_receipts(self.root, limit=1, cursor=cursor,
                                             policy=narrow)
            self.assertEqual(page["total"], 5)
            self.assertFalse(page["scan_stopped"])
            self.assertEqual(len(page["publications"]), 1)
            narrow_seen.extend(row["publication_id"] for row in page["publications"])
            self.assertLessEqual(len(narrow_seen), len(newest))
            cursor = page["next_cursor"]
        self.assertEqual(narrow_seen, newest)  # the oldest is the last row
        self.assertIsNone(cursor)

        outside = next(record for record in records
                       if record["publication_id"] not in prefix)
        # An omitted limit is an upper bound, not a reason to abandon a
        # healthy index: the policy caps the page and supplies a cursor.
        bounded = Publication.list_receipts(self.root, limit=5, policy=narrow)
        self.assertNotIn(outside["publication_id"],
                         [row["publication_id"] for row in bounded["publications"]])
        self.assertFalse(bounded["scan_stopped"])
        self.assertEqual(bounded["total"], 5)
        self.assertEqual([row["publication_id"] for row in bounded["publications"]],
                         newest[:3])
        self.assertTrue(bounded["truncated"])
        self.assertRegex(bounded["next_cursor"], r"\Apub1\|[0-9]+\|[0-9a-f]{64}\Z")
        default_seen = [row["publication_id"] for row in bounded["publications"]]
        cursor = bounded["next_cursor"]
        while cursor is not None:
            page = Publication.list_receipts(self.root, cursor=cursor, policy=narrow)
            self.assertEqual(page["total"], 5)
            self.assertFalse(page["scan_stopped"])
            default_seen.extend(row["publication_id"] for row in page["publications"])
            self.assertLessEqual(len(default_seen), len(newest))
            cursor = page["next_cursor"]
        self.assertEqual(default_seen, newest)
        self.assertIsNone(cursor)

        exact = Publication.list_receipts(self.root, policy=narrow,
                                          code_digest=records[1]["code_digest"])
        self.assertEqual(exact["total"], 1)
        self.assertEqual([row["publication_id"] for row in exact["publications"]],
                         [newest[3]])
        self.assertFalse(exact["truncated"])
        self.assertFalse(exact["scan_stopped"])
        self.assertIsNone(exact["next_cursor"])
        for filters, size in (
                ({"code_digest": outside["code_digest"]}, 1),
                ({"published_sha": outside["published_sha"]}, 1),
                ({"branch": outside["branch"]}, 3)):
            with self.subTest(filters=filters):
                found = Publication.list_receipts(self.root, limit=size,
                                                  policy=narrow, **filters)
                self.assertIn(outside["publication_id"],
                              [row["publication_id"] for row in found["publications"]])
                self.assertFalse(found["scan_stopped"])

    def test_equal_instants_break_by_publication_id_descending(self):
        with patch("mizu.publication.now", return_value="2026-03-01T00:00:00+00:00"):
            ids = [self.record(published_sha="%040x" % index,
                               code_digest="%064x" % index)["publication_id"]
                   for index in range(1, 4)]
        self.assertEqual(
            [row["publication_id"] for row in Publication.list_receipts(self.root)["publications"]],
            sorted(ids, reverse=True))

    def test_invalid_listing_cursor_is_refused(self):
        publication_id = self.record()["publication_id"]
        self.assertIsNone(Publication.list_receipts(self.root)["next_cursor"])
        for bad in (f"pub1|not-a-time|{publication_id}", "pub1|1|short",
                    f"wi1|1|{publication_id}", "x" * 129,
                    f"pub1|1|{publication_id}|extra", f"pub1|1|{'A' * 64}", 12):
            with self.subTest(cursor=bad), self.assertRaises(Denied):
                Publication.list_receipts(self.root, cursor=bad)

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

    def test_configured_listing_budgets_reach_cli_and_runtime(self):
        records = [self.record(published_sha="%040x" % i) for i in range(3)]
        directory = self.root / "evidence" / "publications"
        # Non-record names count, and sorting precedes processing regardless of
        # the order returned by listdir. Enumeration itself remains unbounded.
        (directory / "!not-a-receipt").touch()
        names = sorted(path.name for path in directory.iterdir())
        for policy, expected_ids in (
            (PublicationEvidence(name_processing_limit=1), set()),
            (PublicationEvidence(name_processing_limit=3),
             {name.split(".")[0] for name in names[1:3]}),
            (PublicationEvidence(record_limit=1), {names[1].split(".")[0]}),
        ):
            with self.subTest(policy=policy):
                with patch("mizu.publication.os.listdir", return_value=list(reversed(names))):
                    direct = Publication.list_receipts(self.root, policy=policy)
                self.assertEqual({r["publication_id"] for r in direct["publications"]}, expected_ids)
                self.assertEqual(direct["total"], len(expected_ids))
                self.assertTrue(direct["scan_stopped"])
                self.assertFalse(direct["truncated"])
                config, role = with_caps(self, "worker", ["vcs_read"])
                config = dataclasses.replace(config, publication_evidence=policy)
                ctx = make_context(self, config, role)
                self.assertEqual(ctx.handle("vcs_read", {"op": "publications"})["result"], direct)
                original = self.file.read_text()
                try:
                    self.file.write_text(original + "\n[publication_evidence]\n"
                        f"name_processing_limit = {policy.name_processing_limit}\n"
                        f"record_limit = {policy.record_limit}\n")
                    cli = execute(parser().parse_args([
                        "--config", str(self.file), "publication", "list", "sample"]))
                finally:
                    self.file.write_text(original)
                self.assertEqual(cli, direct)
        complete = Publication.list_receipts(self.root,
            policy=PublicationEvidence(name_processing_limit=len(names), record_limit=len(records)))
        self.assertEqual(complete["total"], len(records))
        self.assertFalse(complete["scan_stopped"])

    def test_list_receipts_reports_a_stopped_scan_instead_of_an_absent_receipt(self):
        for index in range(3):
            self.record(published_sha="%040x" % index, code_digest="%064x" % index)
        # Bump the directory stamp so this narrow reader must use its scan;
        # with a fresh index the reader would instead get a bounded index page.
        (self.root / "evidence" / "publications" / "!reader-change").touch()
        page = Publication.list_receipts(self.root, policy=PublicationEvidence(record_limit=1))
        # The scan stopped at its bound, so the count is a lower bound and the
        # page never claims that no receipt exists.
        self.assertTrue(page["scan_stopped"])
        self.assertLess(page["total"], 3)
        complete = Publication.list_receipts(self.root)
        self.assertFalse(complete["scan_stopped"])
        self.assertEqual(complete["total"], 3)

    def test_unusable_index_falls_back_to_the_bounded_scan(self):
        with patch("mizu.publication.now", side_effect=[
                "2026-04-01T00:00:00+00:00", "2026-04-02T00:00:00+00:00",
                "2026-04-03T00:00:00+00:00"]):
            records = [self.record(published_sha="%040x" % index,
                                   code_digest="%064x" % index)
                       for index in range(3)]
        policy = PublicationEvidence(name_processing_limit=1, record_limit=1)
        path = (self.root / "evidence" / publication.KEY_INDEX_DIR
                / publication.KEY_INDEX_FILE)
        original = path.read_bytes()
        fresh = Publication.list_receipts(self.root, limit=1, policy=policy)
        self.assertFalse(fresh["scan_stopped"])
        self.assertEqual(fresh["total"], 3)
        self.assertEqual([row["publication_id"] for row in fresh["publications"]],
                         [records[-1]["publication_id"]])

        value = json.loads(original)
        stale = dict(value, stamp=[value["stamp"][0] + 1, value["stamp"][1]])
        incomplete = dict(value, incomplete=True)
        legacy = dict(value, schema=publication.KEY_INDEX_LEGACY_SCHEMA)
        value["entries"][-1]["published_sha"] = "%040x" % 9
        mismatched = value
        variants = (b"{}", canonical(stale), canonical(incomplete),
                    canonical(legacy), canonical(mismatched))
        for bad in variants:
            with self.subTest(index=bad[:40]):
                path.write_bytes(bad)
                with patch.object(Publication, "_build_key_index",
                                  side_effect=AssertionError("index rebuilt")):
                    page = Publication.list_receipts(self.root, limit=1, policy=policy)
                # The bounded scan, not the unusable index, answers: its count
                # is a lower bound and it is never mistaken for absence.
                self.assertTrue(page["scan_stopped"])
                self.assertLess(page["total"], 3)
        path.unlink()
        with patch.object(Publication, "_build_key_index",
                          side_effect=AssertionError("index rebuilt")):
            page = Publication.list_receipts(self.root, limit=1, policy=policy)
        self.assertTrue(page["scan_stopped"])
        path.write_bytes(original)
        self.assertFalse(Publication.list_receipts(
            self.root, limit=1, policy=policy)["scan_stopped"])

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
        self.assertEqual(set(listed), {"publications", "total", "truncated", "scan_stopped",
                                       "next_cursor"})
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


class ExternalLineageTests(Fixture):
    """Optional bounded lineage on external receipts; same shape as product path."""

    def setUp(self):
        super().setUp()
        self.root = self.project.root

    def fact(self, published_sha=SHA, **changes):
        import datetime as dt
        base = {"schema": 1, "repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "target_oid": "a" * 40,
                "published_oid": published_sha, "merge_bases": ["a" * 40],
                "target_is_ancestor": True,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        base.update(changes)
        return base

    def base_args(self, **overrides):
        args = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                "approval": APPROVAL, "projection": public_source_identity({})}
        args.update(overrides)
        return args

    def target(self, **changes):
        base = {"repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "version": 1}
        base.update(changes)
        return base

    def test_valid_lineage_record_keeps_identity_and_reads_back(self):
        from mizu.fs import canonical, digest
        lineage = self.fact()
        record = Publication.record_external(self.root, **self.base_args(lineage=lineage))
        self.assertEqual(record["schema"], 4)
        self.assertEqual(record["lineage"], lineage)
        self.assertEqual(set(record), {"schema", "publication_id", "op", "branch",
                                       "published_sha", "code_digest", "approval",
                                       "source_commit", "run", "recorded_at",
                                       "projection_schema", "projection_algorithm",
                                       "projection_digest", "lineage"})
        # The publication ID never incorporates lineage: it binds only the
        # projection identity, exactly like the schema-3 shape.
        identity = {"op": "external", "branch": BRANCH, "code_digest": DIGEST,
                    "published_sha": SHA,
                    "projection_schema": record["projection_schema"],
                    "projection_algorithm": record["projection_algorithm"],
                    "projection_digest": record["projection_digest"]}
        self.assertEqual(record["publication_id"], digest(canonical(identity)))
        self.assertEqual(Publication.read_receipt(self.root, record["publication_id"]), record)
        # An identical retry replays the stored record, including lineage.
        replay = Publication.record_external(self.root, **self.base_args(lineage=dict(lineage)))
        self.assertEqual(replay, record)

    def test_no_lineage_record_stays_schema3_and_unknown(self):
        record = Publication.record_external(self.root, **self.base_args())
        self.assertEqual(record["schema"], 3)
        self.assertNotIn("lineage", record)
        self.assertEqual(Publication.read_receipt(self.root, record["publication_id"]), record)
        # Status without a recorded observation is never readiness.
        # With a configured target the missing receipt stays receipt_missing_or_invalid.
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target()
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req, {"op": "status", "branch": BRANCH, "sha": SHA,
                                   "lineage_target": expected})
            bound = {**self.fact(), "schema": 2,
                     "expected_repository": expected["repository"],
                     "expected_remote": expected["remote"],
                     "expected_target_ref": expected["target_ref"],
                     "expected_version": expected["version"]}
            return Result(0, json.dumps({"checks": [], "lineage": self.fact()}), "", "exited", 0)

        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("unknown", "receipt_missing_or_invalid"))

    def test_external_lineage_status_ready_and_stale(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target()
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()),
                                             lineage_target=expected)
        # Bound receipts carry the descriptor identity and version.
        self.assertEqual(record["lineage"]["expected_repository"], expected["repository"])
        self.assertEqual(record["lineage"]["expected_target_ref"], expected["target_ref"])
        self.assertEqual(record["lineage"]["expected_version"], expected["version"])
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req["lineage_target"], expected)
            return Result(0, json.dumps({"checks": [], "lineage": self.fact()}), "", "exited", 0)

        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("ready", "target_ancestor"))
        self.assertEqual(out["result"]["lineage"]["receipt"], record["lineage"])

        def moved(_argv, **kwargs):
            return Result(0, json.dumps({"checks": [],
                                         "lineage": self.fact(target_oid="c" * 40,
                                                               merge_bases=["c" * 40])}),
                          "", "exited", 0)

        with patch.object(vcs, "run", side_effect=moved):
            stale = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((stale["result"]["lineage"]["status"], stale["result"]["lineage"]["reason"]),
                         ("ready", "target_ancestor"))
        self.assertEqual((stale["result"]["lineage"]["receipt_status"],
                          stale["result"]["lineage"]["receipt_reason"]), ("stale", "target_moved"))
        self.assertEqual(stale["result"]["lineage"]["receipt"], record["lineage"])
        self.assertEqual(stale["result"]["lineage"]["observation"]["target_oid"], "c" * 40)

    def test_malformed_external_lineage_keeps_ci_status(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target()
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()),
                                             lineage_target=expected)
        path = (self.root / "evidence" / "publications"
                / (record["publication_id"] + ".external.json"))
        corrupted = json.loads(path.read_text(encoding="utf-8"))
        corrupted["lineage"]["merge_bases"] = ["a" * 40, "c" * 40]
        path.write_text(json.dumps(corrupted), encoding="utf-8")
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request, {"op": "status", "branch": BRANCH, "sha": SHA,
                                       "lineage_target": expected})
            return Result(0, json.dumps({"checks": [{"check": "ci", "state": "success",
                                                      "sha": SHA}],
                                         "lineage": self.fact()}), "", "exited", 0)

        with patch.object(vcs, "run", side_effect=adapter):
            result = ctx.handle("vcs_read", {"op": "status",
                                               "publication_id": record["publication_id"]})["result"]
        self.assertEqual(result["checks"][0]["check"], "ci")
        self.assertEqual((result["lineage"]["status"], result["lineage"]["reason"]),
                         ("unknown", "receipt_missing_or_invalid"))

    def test_invalid_mismatched_or_malformed_lineage_is_refused(self):
        cases = [
            {"schema": 2},
            {"published_oid": OTHER_SHA},
            {"remote": "upstream"},
            {"target_ref": "refs/heads/other"},
            {"repository": "not-hex"},
            {"repository": "1" * 63},
            {"target_oid": "not-an-oid"},
            {"published_oid": "a" * 39},
            {"merge_bases": []},
            {"merge_bases": ["a" * 40, "c" * 40]},
            {"merge_bases": "a" * 40},
            {"merge_bases": ["c" * 40], "target_is_ancestor": True},
            {"merge_bases": ["a" * 40], "target_is_ancestor": False},
            {"target_is_ancestor": "yes"},
            {"observed_at": "2025-01-01T00:00:00"},
            {"observed_at": "not-a-time"},
            {"observed_at": "x" * 41},
            {"extra": "field"},
        ]
        for idx, changes in enumerate(cases):
            with self.subTest(changes=changes):
                fact = self.fact()
                if "extra" in changes:
                    fact["extra"] = changes["extra"]
                else:
                    fact.update(changes)
                with self.assertRaises(Denied):
                    Publication.record_external(self.root, **self.base_args(
                        branch="invalid-%d" % idx,
                        published_sha=SHA, lineage=fact))
        for idx, bad in enumerate(("not-a-dict", ["list"], "string", 123, [])):
            with self.subTest(bad=bad), self.assertRaises(Denied):
                Publication.record_external(self.root, **self.base_args(
                    branch="badtype-%d" % idx, published_sha=OTHER_SHA, code_digest=OTHER_DIGEST,
                    lineage=bad))
        # A missing lineage object field is malformed, not an absent fact.
        incomplete = self.fact()
        del incomplete["repository"]
        with self.assertRaises(Denied):
            Publication.record_external(self.root, **self.base_args(
                branch="incomplete", published_sha=OTHER_SHA, code_digest=OTHER_DIGEST,
                lineage=incomplete))
        self.assertEqual(Publication.list_receipts(self.root)["publications"], [])

    def test_conflicting_lineage_retry_is_refused(self):
        first = Publication.record_external(self.root, **self.base_args(lineage=self.fact()))
        # A differing observation for the same publication is a conflict.
        with self.assertRaisesRegex(Denied, first["publication_id"]):
            Publication.record_external(self.root, **self.base_args(
                lineage=self.fact(target_oid="c" * 40, merge_bases=["c" * 40])))
        # Adding lineage where none was recorded changes the retry, so it conflicts.
        plain = Publication.record_external(self.root, **self.base_args(
            branch="other", published_sha=OTHER_SHA, code_digest=OTHER_DIGEST))
        self.assertEqual(plain["schema"], 3)
        with self.assertRaisesRegex(Denied, plain["publication_id"]):
            Publication.record_external(self.root, **self.base_args(
                branch="other", published_sha=OTHER_SHA, code_digest=OTHER_DIGEST,
                lineage=self.fact(published_sha=OTHER_SHA)))
        # Dropping lineage is also a conflict, never a silent downgrade.
        with self.assertRaisesRegex(Denied, first["publication_id"]):
            Publication.record_external(self.root, **self.base_args())
        self.assertEqual(Publication.read_receipt(self.root, first["publication_id"]), first)
        self.assertEqual(Publication.read_receipt(self.root, plain["publication_id"]), plain)

    def test_legacy_schema2_record_stays_readable_and_immutable(self):
        from mizu.fs import canonical, digest, write_json
        code_digest = DIGEST
        identity = {"op": "external", "branch": "legacy-ext", "published_sha": "b" * 40,
                    "code_digest": code_digest}
        publication_id = digest(canonical(identity))
        legacy = {**identity, "schema": 2, "publication_id": publication_id,
                  "approval": APPROVAL, "source_commit": None, "run": None,
                  "recorded_at": "2026-01-01T00:00:00+00:00"}
        path = self.root / "evidence" / "publications" / (publication_id + ".external.json")
        write_json(path, legacy)
        before = path.read_bytes()
        self.assertEqual(Publication.read_receipt(self.root, publication_id), legacy)
        # A schema-4 record missing projection identity is malformed and refused.
        tampered_id = digest(canonical({**identity, "branch": "legacy-tampered"}))
        tampered = {**legacy, "schema": 4, "publication_id": tampered_id,
                    "branch": "legacy-tampered", "lineage": {"bad": "shape"}}
        tampered_path = self.root / "evidence" / "publications" / (tampered_id + ".external.json")
        write_json(tampered_path, tampered)
        with self.assertRaises(Denied):
            Publication.read_receipt(self.root, tampered_id)
        listing = Publication.list_receipts(self.root)
        self.assertIn(publication_id, [r["publication_id"] for r in listing["publications"]])
        self.assertNotIn(tampered_id, [r["publication_id"] for r in listing["publications"]])
        # The legacy file itself is untouched by any new-write path.
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(Publication.read_receipt(self.root, publication_id), legacy)

    def test_cli_lineage_round_trip_and_refusals(self):
        import json
        fact = self.fact()
        args = parser().parse_args([
            "--config", str(self.file), "publication", "record", "sample",
            "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
            "--approval", APPROVAL,
            "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
            "--projection-digest", public_source_identity({})["digest"],
            "--lineage", json.dumps(fact)])
        record = execute(args)
        self.assertEqual(record["schema"], 4)
        self.assertEqual(record["lineage"], fact)
        self.assertEqual(Publication.read_receipt(self.root, record["publication_id"])["lineage"], fact)
        # Identical CLI retry replays.
        replay = execute(parser().parse_args([
            "--config", str(self.file), "publication", "record", "sample",
            "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
            "--approval", APPROVAL,
            "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
            "--projection-digest", public_source_identity({})["digest"],
            "--lineage", json.dumps(fact)]))
        self.assertEqual(replay, record)
        base = ["--config", str(self.file), "publication", "record", "sample",
                "--branch", "cli-bad", "--published-sha", OTHER_SHA, "--code-digest", OTHER_DIGEST,
                "--approval", APPROVAL,
                "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
                "--projection-digest", public_source_identity({})["digest"]]
        bad_fact = self.fact(published_sha=OTHER_SHA, remote="upstream")
        with self.assertRaises(Denied):
            execute(parser().parse_args(base + ["--lineage", json.dumps(bad_fact)]))
        mismatched = self.fact(published_sha=SHA)
        with self.assertRaises(Denied):
            execute(parser().parse_args(base + ["--lineage", json.dumps(mismatched)]))
        with self.assertRaises(Denied):
            execute(parser().parse_args(base + ["--lineage", "{not json"]))
        with self.assertRaises(Denied):
            execute(parser().parse_args(base + ["--lineage", "NaN"]))
        oversized = json.dumps({**fact, "repository": "1" * 64, "extra_pad": "x" * 5000})
        # The CLI cap refuses the value before any write, whatever its shape.
        with self.assertRaises(Denied):
            execute(parser().parse_args(base + ["--lineage", oversized]))
        self.assertEqual(len([r for r in Publication.list_receipts(self.root)["publications"]
                              if r["branch"] == "cli-bad"]), 0)
class LineageTargetExternalTests(Fixture):
    """Configured target for external receipts: binding, staleness and CLI."""

    def setUp(self):
        super().setUp()
        self.root = self.project.root

    def fact(self, published_sha=None, **changes):
        import datetime as dt
        sha = published_sha if published_sha is not None else SHA
        base = {"schema": 1, "repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "target_oid": "a" * 40,
                "published_oid": sha, "merge_bases": ["a" * 40],
                "target_is_ancestor": True,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        base.update(changes)
        return base

    def target(self, **changes):
        base = {"repository": "1" * 64, "remote": "origin",
                "target_ref": "refs/heads/main", "version": 1}
        base.update(changes)
        return base

    def base_args(self, **overrides):
        args = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                "approval": APPROVAL, "projection": public_source_identity({})}
        args.update(overrides)
        return args

    def test_configured_main_binds_and_readies(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target()
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()),
                                             lineage_target=expected)
        self.assertEqual(record["schema"], 4)
        self.assertEqual(record["lineage"]["schema"], 2)
        self.assertEqual(record["lineage"]["expected_version"], 1)
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)
        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req["lineage_target"], expected)
            return Result(0, json.dumps({"checks": [], "lineage": self.fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("ready", "target_ancestor"))

    def test_configured_release_ref_readies(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target(target_ref="refs/heads/release-1")
        fact = self.fact(target_ref="refs/heads/release-1")
        record = Publication.record_external(self.root, **self.base_args(lineage=fact),
                                             lineage_target=expected)
        self.assertEqual(record["lineage"]["target_ref"], "refs/heads/release-1")
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)
        def adapter(_argv, **kwargs):
            fresh = self.fact(target_ref="refs/heads/release-1")
            return Result(0, json.dumps({"checks": [], "lineage": fresh}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual(out["result"]["lineage"]["status"], "ready")

    def test_missing_descriptor_leaves_unconfigured(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()))
        self.assertEqual(record["lineage"]["schema"], 1)
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        ctx = make_context(self, config, role)
        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertNotIn("lineage_target", req)
            return Result(0, json.dumps({"checks": [{"check": "ci", "state": "success", "sha": SHA}],
                                         "lineage": self.fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("unknown", "unconfigured"))
        self.assertEqual(out["result"]["checks"][0]["check"], "ci")

    def test_wrong_repository_and_ref_are_invalid(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        expected = self.target()
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()),
                                             lineage_target=expected)
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)
        for bad in (self.fact(repository="2" * 64), self.fact(target_ref="refs/heads/other")):
            with self.subTest(bad=bad["target_ref"] if "target_ref" in bad else bad["repository"][:8]):
                def adapter(_argv, _bad=bad, **kwargs):
                    return Result(0, json.dumps({"checks": [], "lineage": _bad}), "", "exited", 0)
                with patch.object(vcs, "run", side_effect=adapter):
                    out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
                self.assertEqual(out["result"]["lineage"]["status"], "unknown")
                self.assertEqual(out["result"]["lineage"]["reason"], "observation_invalid")

    def test_descriptor_change_makes_prior_stale(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        old = self.target()
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()),
                                             lineage_target=old)
        new = self.target(version=2)
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, new)
        ctx = make_context(self, config, role)
        def adapter(_argv, **kwargs):
            return Result(0, json.dumps({"checks": [], "lineage": self.fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("unknown", "descriptor_mismatch"))
        self.assertEqual((out["result"]["lineage"]["receipt_status"], out["result"]["lineage"]["receipt_reason"]),
                         ("stale", "descriptor_mismatch"))

    def test_unbound_legacy_never_readies(self):
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()))
        self.assertEqual(record["lineage"]["schema"], 1)
        expected = self.target()
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        set_project_target(self, expected)
        ctx = make_context(self, config, role)
        def adapter(_argv, **kwargs):
            return Result(0, json.dumps({"checks": [], "lineage": self.fact()}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            out = ctx.handle("vcs_read", {"op": "status", "publication_id": record["publication_id"]})
        self.assertEqual((out["result"]["lineage"]["status"], out["result"]["lineage"]["reason"]),
                         ("unknown", "unbound"))

    def test_self_asserted_binding_without_config_is_refused(self):
        from mizu import lineage as lineage_module
        bound = lineage_module.bind(self.fact(), SHA, self.target())
        self.assertIsNotNone(bound)
        with self.assertRaises(Denied):
            Publication.record_external(self.root, **self.base_args(lineage=bound))

    def test_external_mismatched_lineage_is_refused(self):
        expected = self.target()
        with self.assertRaises(Denied):
            Publication.record_external(self.root, **self.base_args(lineage=self.fact(repository="2" * 64)),
                                        lineage_target=expected)
        with self.assertRaises(Denied):
            Publication.record_external(self.root, **self.base_args(lineage=self.fact(target_ref="refs/heads/other")),
                                        lineage_target=expected)

    def test_cli_uses_each_projects_target_with_shared_config(self):
        from mizu.project import initialize
        second = initialize(self.config, "other", self.goal.parent / "source",
                            self.goal, ["worker"], ["python3 -m unittest"])
        projects = (self.project, second)
        targets = (self.target(), self.target(repository="2" * 64, remote="upstream",
                                               target_ref="refs/heads/release-1"))
        for project, target in zip(projects, targets):
            self.project = project
            set_project_target(self, target)
        for project, target in zip(projects, targets):
            fact = self.fact(repository=target["repository"], remote=target["remote"],
                             target_ref=target["target_ref"])
            args = parser().parse_args([
                "--config", str(self.file), "publication", "record", project.name,
                "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
                "--approval", APPROVAL, "--projection-schema", "1",
                "--projection-algorithm", public_source_identity({})["algorithm"],
                "--projection-digest", public_source_identity({})["digest"],
                "--lineage", json.dumps(fact)])
            record = execute(args)
            self.assertEqual(record["lineage"]["expected_repository"], target["repository"])
            self.assertEqual(record["lineage"]["expected_target_ref"], target["target_ref"])
            self.assertEqual(Publication.read_receipt(project.root, record["publication_id"]), record)

    def test_cli_binds_descriptor_when_configured(self):
        import json
        from mizu.cli import parser, execute
        expected = self.target()
        set_project_target(self, expected)
        fact = self.fact()
        args = parser().parse_args([
            "--config", str(self.file), "publication", "record", "sample",
            "--branch", BRANCH, "--published-sha", SHA, "--code-digest", DIGEST,
            "--approval", APPROVAL,
            "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
            "--projection-digest", public_source_identity({})["digest"],
            "--lineage", json.dumps(fact)])
        record = execute(args)
        self.assertEqual(record["schema"], 4)
        self.assertEqual(record["lineage"]["schema"], 2)
        self.assertEqual(record["lineage"]["expected_repository"], expected["repository"])
        self.assertEqual(record["lineage"]["expected_version"], 1)
        self.assertEqual(Publication.read_receipt(self.root, record["publication_id"])["lineage"], record["lineage"])
        # A mismatched lineage against the configured descriptor is refused via CLI too.
        bad = self.fact(target_ref="refs/heads/other")
        with self.assertRaises(Denied):
            execute(parser().parse_args([
                "--config", str(self.file), "publication", "record", "sample",
                "--branch", "other", "--published-sha", OTHER_SHA, "--code-digest", OTHER_DIGEST,
                "--approval", APPROVAL,
                "--projection-schema", "1", "--projection-algorithm", public_source_identity({})["algorithm"],
                "--projection-digest", public_source_identity({})["digest"],
                "--lineage", json.dumps(bad)]))
        listed = execute(parser().parse_args(["--config", str(self.file), "publication", "list", "sample"]))
        self.assertIn(record["publication_id"], [r["publication_id"] for r in listed["publications"]])


class ReceiptValidationReuseTests(Fixture):
    """Validation reuse keyed by the exact publication receipt bytes."""

    def setUp(self):
        super().setUp()
        self.root = self.project.root
        self.directory = self.root / "evidence" / "publications"
        self.policy = PublicationEvidence()
        publication._VALIDATED_RECEIPTS.clear()

    @property
    def index_path(self):
        return (self.root / "evidence" / publication.KEY_INDEX_DIR
                / publication.KEY_INDEX_FILE)

    def record(self, **overrides):
        arguments = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                     "approval": APPROVAL, "projection": public_source_identity({})}
        arguments.update(overrides)
        record = Publication.record_external(self.root, **arguments)
        # The writer's index build validates the receipt it just wrote. Start
        # these reuse tests with that validation out of the process cache.
        publication._VALIDATED_RECEIPTS.clear()
        return record

    def lookup(self, *, branch=BRANCH, code_digest=DIGEST):
        return Publication.receipt_for_key(self.root, branch, code_digest,
                                           policy=self.policy)

    def counted_parses(self):
        """Count receipt parse/validation work without changing its result."""
        calls = []
        real = publication._parse_record

        def counted(raw, kind, publication_id):
            calls.append((kind, publication_id, digest(raw)))
            return real(raw, kind, publication_id)

        return patch.object(publication, "_parse_record", side_effect=counted), calls

    def cache_keys(self, publication_id):
        return [key for key in publication._VALIDATED_RECEIPTS
                if key[0] == str(self.directory) and key[1] == publication_id]

    def no_enumeration(self):
        labels = ("directory enumerated", "strict rescan", "index rebuilt")
        mocks = tuple(Mock(side_effect=AssertionError(label)) for label in labels)
        patches = tuple(patch.object(Publication, attribute, new=mock)
                        for attribute, mock in zip(
                            ("_receipt_names", "_scan_receipts", "_build_key_index"), mocks))
        return patches, mocks

    def test_key_lookup_validates_the_index_witness_byte_read(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        publication._VALIDATED_RECEIPTS.clear()
        reads = []
        real_read = publication.safe_read

        def counted(root, name, maximum):
            reads.append(name)
            return real_read(root, name, maximum)

        with patch.object(publication, "safe_read", side_effect=counted):
            self.assertEqual(self.lookup(), publication_id)
            self.assertEqual(self.lookup(), publication_id)
        # One bounded read per lookup, not a second read for validation: the
        # indexed digest, cache key and parsed record all describe those bytes.
        self.assertEqual(reads.count(publication_id + ".external.json"), 2)

    def test_external_receipt_validates_once_for_strict_reads_and_key_lookup(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        raw = (self.directory / (publication_id + ".external.json")).read_bytes()
        counted, calls = self.counted_parses()
        with counted:
            for _ in range(2):
                self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
                self.assertEqual(Publication.list_receipts(self.root, strict=True)["total"], 1)
                self.assertEqual(self.lookup(), publication_id)
        self.assertEqual(calls, [("external", publication_id, digest(raw))])
        self.assertEqual(
            self.cache_keys(publication_id),
            [(str(self.directory), publication_id, (("external", digest(raw)),))])

    def test_changed_receipt_bytes_revalidate_and_fail_closed(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        path = self.directory / (publication_id + ".external.json")
        original = path.read_bytes()
        changed = original.replace(
            b'"published_sha":"' + SHA.encode() + b'"',
            b'"published_sha":"' + OTHER_SHA.encode() + b'"')
        self.assertNotEqual(changed, original)
        counted, calls = self.counted_parses()
        with counted:
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            path.write_bytes(changed)
            with self.assertRaises(Denied):
                Publication.read_receipt(self.root, publication_id)
            with self.assertRaises(Denied):
                Publication.list_receipts(self.root, strict=True)
            with self.assertRaises(Denied):
                self.lookup()
            # Strict listing revalidates too: failures are never cached.
            self.assertEqual([call[2] for call in calls],
                             [digest(original), digest(changed), digest(changed)])
            not_cached = [key for key in self.cache_keys(publication_id)
                          if any(part[1] == digest(changed) for part in key[2])]
            self.assertEqual(not_cached, [])
            # A failed validation is never cached: every read parses again.
            with self.assertRaises(Denied):
                Publication.read_receipt(self.root, publication_id)
            self.assertEqual(len(calls), 4)
            path.write_bytes(original)
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            # The original bytes still have their cached validation.
            self.assertEqual(len(calls), 4)
            self.assertEqual(self.lookup(), publication_id)

    def test_corrupt_evidence_is_never_served_from_cache(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        path = self.directory / (publication_id + ".external.json")
        original = path.read_bytes()
        with self.counted_parses()[0]:
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        corrupt = original[:len(original) // 2]
        path.write_bytes(corrupt)
        counted, calls = self.counted_parses()
        with counted:
            for read in (lambda: Publication.read_receipt(self.root, publication_id),
                         lambda: Publication.list_receipts(self.root, strict=True),
                         self.lookup):
                with self.assertRaises(Denied):
                    read()
            self.assertTrue(calls)
            self.assertEqual({call[2] for call in calls}, {digest(corrupt)})
        path.write_bytes(original)
        publication._VALIDATED_RECEIPTS.clear()
        counted, calls = self.counted_parses()
        with counted:
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            self.assertEqual(self.lookup(), publication_id)
        self.assertEqual(calls, [("external", publication_id, digest(original))])

    def test_product_trio_key_covers_every_validated_component(self):
        product = Publication(self.root, "e" * 40, "f" * 64, BRANCH, "push",
                              projection=public_source_identity({}))
        intent, _ = product.prepare("approval", "run")
        product.mark_dispatch()
        receipt = product.complete(intent, "1" * 40)
        publication._VALIDATED_RECEIPTS.clear()
        paths = {kind: self.directory / (product.id + "." + kind + ".json")
                 for kind in ("intent", "dispatch", "receipt")}
        raws = {kind: path.read_bytes() for kind, path in paths.items()}
        expected_key = (str(self.directory), product.id,
                        tuple((kind, digest(raws[kind])) for kind in raws))
        counted, calls = self.counted_parses()
        with counted:
            for _ in range(2):
                self.assertEqual(Publication.read_receipt(self.root, product.id), receipt)
                self.assertEqual(Publication.list_receipts(self.root, strict=True)["total"], 1)
                self.assertEqual(self.lookup(code_digest="f" * 64), product.id)
        self.assertEqual(calls, [("intent", product.id, digest(raws["intent"])),
                                 ("dispatch", product.id, digest(raws["dispatch"])),
                                 ("receipt", product.id, digest(raws["receipt"]))])
        self.assertEqual(self.cache_keys(product.id), [expected_key])
        changed_intent = json.loads(raws["intent"])
        changed_intent["approval"] = "approved"
        changed_receipt = json.loads(raws["receipt"])
        changed_receipt["approval"] = "approved"
        mutations = {
            "intent": canonical(changed_intent),
            "dispatch": canonical({"publication_id": "0" * 64}),
            "receipt": canonical(changed_receipt),
        }
        for kind, changed in mutations.items():
            with self.subTest(component=kind):
                paths[kind].write_bytes(changed)
                counted, calls = self.counted_parses()
                with counted:
                    with self.assertRaises(Denied):
                        Publication.read_receipt(self.root, product.id)
                    with self.assertRaises(Denied):
                        Publication.list_receipts(self.root, strict=True)
                expected = [digest(mutations[part]) if part == kind else digest(raws[part])
                            for part in ("intent", "dispatch", "receipt")]
                self.assertEqual([call[2] for call in calls], expected * 2)
                self.assertNotIn((str(self.directory), product.id,
                                  tuple((part, digest(mutations[part]) if part == kind
                                         else digest(raws[part]))
                                        for part in raws)),
                                 self.cache_keys(product.id))
                paths[kind].write_bytes(raws[kind])
                self.assertEqual(Publication.read_receipt(self.root, product.id), receipt)

    def test_a_formatting_only_byte_change_revalidates_under_a_new_identity(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        path = self.directory / (publication_id + ".external.json")
        original = path.read_bytes()
        counted, calls = self.counted_parses()
        with counted:
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            # Whitespace is outside the record's semantic fields but inside
            # the bytes that must be validated: it changes the cache key.
            reformatted = original.replace(b"}\n", b"} \n")
            self.assertNotEqual(reformatted, original)
            path.write_bytes(reformatted)
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        self.assertEqual([call[2] for call in calls],
                         [digest(original), digest(reformatted)])
        self.assertIn((str(self.directory), publication_id,
                       (("external", digest(reformatted)),)),
                      self.cache_keys(publication_id))

    def test_missing_receipt_fails_closed_after_a_warm_read(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        path = self.directory / (publication_id + ".external.json")
        self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        path.unlink()
        counted, calls = self.counted_parses()
        with counted:
            with self.assertRaises(Denied):
                Publication.read_receipt(self.root, publication_id)
            self.assertEqual(Publication.list_receipts(self.root, strict=True)["total"], 0)
            with self.assertRaises(Denied):
                self.lookup()
        self.assertEqual(calls, [])
        self.assertEqual(len(self.cache_keys(publication_id)), 1)

    def test_metadata_change_alone_never_authorizes_validation_reuse(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        path = self.directory / (publication_id + ".external.json")
        counted, calls = self.counted_parses()
        with counted:
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
            path.touch()
            self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        self.assertEqual(len(calls), 1)

    def test_key_lookup_never_enumerates_on_cold_or_warm_paths(self):
        external = self.record()
        product = Publication(self.root, "e" * 40, "f" * 64, BRANCH, "push",
                              projection=public_source_identity({}))
        intent, _ = product.prepare("approval", "run")
        product.mark_dispatch()
        product.complete(intent, "1" * 40)
        publication._VALIDATED_RECEIPTS.clear()
        patches, mocks = self.no_enumeration()
        with patches[0], patches[1], patches[2]:
            self.assertEqual(self.lookup(), external["publication_id"])
            self.assertEqual(self.lookup(), external["publication_id"])
            self.assertIsNone(self.lookup(code_digest="e" * 64))
            self.assertEqual(self.lookup(code_digest="f" * 64), product.id)
            self.assertIsNone(self.lookup(branch="absent"))
            self.assertEqual([mock.call_count for mock in mocks], [0, 0, 0])

    def test_warm_cache_still_checks_the_evidence_directories(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        with patch.object(Publication, "_check_directories", side_effect=Denied("unsafe evidence")):
            with self.assertRaisesRegex(Denied, "unsafe evidence"):
                Publication.read_receipt(self.root, publication_id)
            with self.assertRaisesRegex(Denied, "unsafe evidence"):
                self.lookup()

    def test_warm_cache_does_not_relax_key_index_witnesses(self):
        receipt = self.record()
        publication_id = receipt["publication_id"]
        self.assertEqual(Publication.read_receipt(self.root, publication_id), receipt)
        patches, mocks = self.no_enumeration()
        with patches[0], patches[1], patches[2]:
            self.assertEqual(self.lookup(), publication_id)
            self.assertEqual(self.lookup(), publication_id)
            self.assertEqual([mock.call_count for mock in mocks], [0, 0, 0])
        original = self.index_path.read_bytes()
        value = json.loads(original)
        stale = dict(value)
        stale["stamp"] = [value["stamp"][0] + 1, value["stamp"][1]]
        incomplete = dict(value)
        incomplete["incomplete"] = True
        malformed = dict(value)
        del malformed["incomplete"]
        for bad in (canonical(stale), canonical(incomplete), canonical(malformed), b"{"):
            with self.subTest(index=bad[:24]):
                self.index_path.write_bytes(bad)
                with self.assertRaises(Denied):
                    self.lookup()
        self.index_path.write_bytes(original)
        self.assertEqual(self.lookup(), publication_id)
        # A changed receipt name still fails closed even though the bytes and
        # their cached validation are unchanged.
        extra = self.directory / ("0" * 64 + ".external.json")
        extra.write_bytes(canonical(receipt))
        with self.assertRaises(Denied):
            self.lookup()


class ReceiptWriteCapacityTests(Fixture):
    """A capacity refusal commits no receipt and keeps the index usable."""

    def setUp(self):
        super().setUp()
        self.root = self.project.root
        self.directory = self.root / "evidence" / "publications"
        self.index_path = (self.root / "evidence" / publication.KEY_INDEX_DIR
                           / publication.KEY_INDEX_FILE)
        self.narrow = dataclasses.replace(PublicationEvidence(), record_limit=1)

    def record(self, **overrides):
        arguments = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                     "approval": APPROVAL, "projection": public_source_identity({})}
        arguments.update(overrides)
        return Publication.record_external(self.root, **arguments)

    def receipts(self):
        return sorted(path.name for path in self.directory.glob("*.json")
                      if publication.RECEIPT_FILE.fullmatch(path.name))

    def assert_index_matches(self, receipt):
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        raw = (self.directory / (receipt["publication_id"] + ".external.json")).read_bytes()
        self.assertEqual(value["stamp"], publication._directory_stamp(self.directory))
        self.assertFalse(value["incomplete"])
        self.assertEqual(value["entries"], [
            {"publication_id": receipt["publication_id"], "kind": "external",
             "sha256": digest(raw),
             "order": publication._receipt_order(receipt)[0],
             "branch": receipt["branch"], "code_digest": receipt["code_digest"],
             "published_sha": receipt["published_sha"]}])
        self.assertEqual(value["keys"], [
            {"branch": receipt["branch"], "code_digest": receipt["code_digest"],
             "publication_ids": [receipt["publication_id"]]}])
        return value

    def test_second_external_receipt_is_refused_before_it_is_committed(self):
        with patch.object(publication, "PublicationEvidence", return_value=self.narrow):
            first = self.record()
            original = self.index_path.read_bytes()
            with self.assertRaises(Denied):
                self.record(published_sha=OTHER_SHA, code_digest=OTHER_DIGEST)
            self.assertEqual(
                Publication.receipt_for_key(self.root, BRANCH, DIGEST, policy=self.narrow),
                first["publication_id"])
        self.assertEqual(self.receipts(), [first["publication_id"] + ".external.json"])
        current = self.assert_index_matches(first)
        self.assertEqual(current["entries"], json.loads(original)["entries"])
        self.assertEqual(current["keys"], json.loads(original)["keys"])

    def test_product_receipt_is_refused_before_it_is_committed(self):
        product = Publication(self.root, "e" * 40, "f" * 64, BRANCH, "push",
                              projection=public_source_identity({}))
        intent, _ = product.prepare(APPROVAL, "run-ref")
        product.mark_dispatch()
        with patch.object(publication, "PublicationEvidence", return_value=self.narrow):
            first = self.record()
            original = self.index_path.read_bytes()
            with self.assertRaises(Denied):
                product.complete(intent, "1" * 40)
            self.assertEqual(
                Publication.receipt_for_key(self.root, BRANCH, DIGEST, policy=self.narrow),
                first["publication_id"])
        self.assertEqual(self.receipts(), [first["publication_id"] + ".external.json"])
        current = self.assert_index_matches(first)
        self.assertEqual(current["entries"], json.loads(original)["entries"])
        self.assertEqual(current["keys"], json.loads(original)["keys"])

    def test_serialized_index_capacity_is_checked_before_the_receipt_is_committed(self):
        first = self.record()
        original = self.index_path.read_bytes()
        tight = len(original) - 1
        with patch.object(publication, "KEY_INDEX_BYTES", tight):
            with self.assertRaisesRegex(Denied, "capacity"):
                self.record(published_sha=OTHER_SHA, code_digest=OTHER_DIGEST)
        self.assertEqual(self.receipts(), [first["publication_id"] + ".external.json"])
        current = self.assert_index_matches(first)
        self.assertEqual(current["entries"], json.loads(original)["entries"])
        self.assertEqual(current["keys"], json.loads(original)["keys"])
        self.assertEqual(
            Publication.receipt_for_key(self.root, BRANCH, DIGEST, policy=PublicationEvidence()),
            first["publication_id"])
