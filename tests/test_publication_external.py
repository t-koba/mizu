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
        import dataclasses, json, sys
        from unittest.mock import patch
        from mizu import vcs
        from mizu.process import Result
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            req = json.loads(kwargs["input_data"])
            self.assertEqual(req, {"op": "status", "branch": BRANCH, "sha": SHA})
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
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()))
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
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
        record = Publication.record_external(self.root, **self.base_args(lineage=self.fact()))
        path = (self.root / "evidence" / "publications"
                / (record["publication_id"] + ".external.json"))
        corrupted = json.loads(path.read_text(encoding="utf-8"))
        corrupted["lineage"]["merge_bases"] = ["a" * 40, "c" * 40]
        path.write_text(json.dumps(corrupted), encoding="utf-8")
        config, role = with_caps(self, "worker", ["vcs_read"])
        config = dataclasses.replace(config, vcs={"command": [sys.executable, "-c", "pass"],
                                                  "timeout_seconds": 5, "max_bytes": 524288})
        ctx = make_context(self, config, role)

        def adapter(_argv, **kwargs):
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request, {"op": "status", "branch": BRANCH, "sha": SHA})
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
