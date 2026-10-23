"""Public-source projection boundary: whole capture vs filtered projection.

Offline only: no adapter, no network, no ignored-state inspection.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Fixture
from mizu import vcs
from mizu.distribution import (
    PUBLIC_SOURCE_ALGORITHM, PUBLIC_SOURCE_SCHEMA, is_public_source_path,
    public_projection, public_source_digest, public_source_identity,
    source_manifest,
)
from mizu.editor import Capsule, export, export_public_source
from mizu.errors import Denied
from mizu.fs import canonical, digest, lock, write_json
from mizu.process import Result
from mizu.publication import Publication


def entry(sha="a" * 64, executable=False, extra_bytes=True):
    record = {"sha256": sha, "executable": executable}
    if extra_bytes:
        record["bytes"] = 3
    return record


class ProjectionInventoryTests(unittest.TestCase):
    def test_inventory_scope_matches_source_files(self):
        root = Path(".").resolve()
        manifest = source_manifest(root)
        captured = {path: {**record, "bytes": 1} for path, record in manifest.items()}
        # Non-inventory and private files never enter the projection.
        captured["credentials.env"] = entry()
        captured["config/config.local.toml"] = entry()
        captured["src/mizu/__pycache__/cached.pyc"] = entry()
        captured["notes.txt"] = entry()
        captured["config/other.toml"] = entry()
        self.assertEqual(public_projection(captured), manifest)

    def test_private_names_and_excluded_trees_are_not_public(self):
        for path in ("credentials.env", "nested/credentials.env",
                     "installation.json", "nested/.env.local",
                     "src/__pycache__/x.py", ".git/config",
                     "config/config.local.toml", "config/other.toml",
                     "top-level.txt", "", "/abs", "a/../b", "src/mizu/app.pyc"):
            with self.subTest(path=path):
                self.assertFalse(is_public_source_path(path))
        for path in ("README.md", "pyproject.toml", "src/mizu/distribution.py",
                     "config/config.example.toml", "tests/test_public_source.py",
                     "docs/architecture.md", ".github/workflows/ci.yml"):
            with self.subTest(path=path):
                self.assertTrue(is_public_source_path(path))

    def test_path_and_suffix_edges_match_distribution_inventory(self):
        paths = ("README.md", "notes.txt", "config/config.example.toml", "config/other.toml",
                 "src/.pyc", "src/.pyo", "src/a.pyc", "src/a.pyo", "src/upper.PYC",
                 "src/a.pyc.py", "src/dir.pyc/child.py", "src/dir.pyo/child.py",
                 "src/.env.local", "src/credentials.env", "src/.env.dir/child.py",
                 "src/build/child.py", "docs/__pycache__/child.md", "tests/.DS_Store",
                 ".github/workflows/ci.yml", "src/.hidden", "src/..pyc")
        with tempfile.TemporaryDirectory(prefix="mizu-inventory-") as directory:
            root = Path(directory)
            for relative in paths:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture\n")
            manifest = source_manifest(root)
            for relative in paths:
                with self.subTest(path=relative):
                    self.assertEqual(is_public_source_path(relative), relative in manifest)
            captured = {relative: entry() for relative in paths}
            self.assertEqual(set(public_projection(captured)), set(manifest))
            self.assertIn("src/.pyc", manifest)
            self.assertIn("src/.pyo", manifest)
            self.assertNotIn("src/a.pyc", manifest)
            # Same metadata manifest -> same adapter and capture projection digest.
            self.assertEqual(public_source_digest(manifest), digest(canonical(manifest)))

    def test_digest_is_deterministic_and_bound_to_manifest(self):
        files = {"src/a.py": entry("a" * 64), "README.md": entry("b" * 64, True)}
        first = public_source_digest(files)
        self.assertEqual(public_source_digest(dict(reversed(list(files.items())))), first)
        altered = dict(files)
        altered["src/a.py"] = entry("c" * 64)
        self.assertNotEqual(public_source_digest(altered), first)
        identity = public_source_identity(files)
        self.assertEqual(identity, {"schema": PUBLIC_SOURCE_SCHEMA,
                                   "algorithm": PUBLIC_SOURCE_ALGORITHM, "digest": first})

    def test_malformed_public_entries_fail_closed(self):
        good = {"src/a.py": entry()}
        with self.assertRaises(ValueError):
            public_projection({"src/a.py": {"sha256": "zz", "executable": False, "bytes": 1}})
        with self.assertRaises(ValueError):
            public_projection({"src/a.py": {"sha256": "a" * 64, "bytes": 1}})
        with self.assertRaises(ValueError):
            public_projection(["not-a-dict"])  # type: ignore[arg-type]
        # The empty projection is defined (deterministic digest of {}).
        self.assertEqual(public_source_digest({}), digest(canonical({})))


class ProjectionPublicationTests(Fixture):
    def projection(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        from mizu.distribution import public_source_identity as identity
        return identity(captured.get("files", {})), captured["code_digest"]

    def test_schema3_intent_binds_projection_and_reads_back(self):
        projection, code_digest = self.projection()
        store = Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                            projection=projection)
        with lock(store.lock):
            intent, receipt = store.prepare("approval-ref", "run-ref")
        self.assertEqual(intent["schema"], 3)
        self.assertEqual(intent["projection_digest"], projection["digest"])
        self.assertEqual(intent["projection_algorithm"], PUBLIC_SOURCE_ALGORITHM)
        self.assertEqual(intent["projection_schema"], PUBLIC_SOURCE_SCHEMA)
        with lock(store.lock):
            store.mark_dispatch()
            record = store.complete(intent, "b" * 40)
        self.assertEqual(Publication.read_receipt(self.project.root, store.id), record)
        # Legacy identity remains distinct, without using a legacy writer.
        legacy_id = digest(canonical({"source_commit": "a" * 40, "code_digest": code_digest,
                                      "branch": "main", "op": "push"}))
        self.assertNotEqual(legacy_id, store.id)
        self.assertEqual(store.schema, 3)

    def test_legacy_schema1_receipt_stays_readable(self):
        _, code_digest = self.projection()
        identity = {"source_commit": "a" * 40, "code_digest": code_digest,
                    "branch": "legacy", "op": "push"}
        publication_id = digest(canonical(identity))
        intent = {**identity, "schema": 1, "publication_id": publication_id,
                  "approval": "approval-ref", "run": "run-ref"}
        receipt = {**intent, "published_sha": "b" * 40}
        root = self.project.root / "evidence/publications"
        # Raw compatibility fixture: new constructors must never write legacy records.
        for kind, record in (("intent", intent), ("dispatch", {"publication_id": publication_id}),
                             ("receipt", receipt)):
            write_json(root / (publication_id + "." + kind + ".json"), record)
        before = {path: path.read_bytes() for path in root.iterdir()}
        self.assertEqual(Publication.read_receipt(self.project.root, publication_id), receipt)
        self.assertEqual(Publication.list_receipts(self.project.root)["total"], 1)
        self.assertEqual({path: path.read_bytes() for path in root.iterdir()}, before)

    def test_projectionless_writes_are_refused_without_side_effects(self):
        _, code_digest = self.projection()
        for kwargs in ({}, {"projection": None}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(Denied, "projection identity"):
                    Publication(self.project.root, "a" * 40, code_digest, "main", "push", **kwargs)
                with self.assertRaisesRegex(Denied, "projection identity"):
                    Publication.record_external(self.project.root, branch="main", published_sha="b" * 40,
                                                code_digest=code_digest, approval="approval", **kwargs)
        self.assertFalse((self.project.root / "evidence/publications").exists())

    def test_mismatched_projection_identity_is_refused(self):
        projection, code_digest = self.projection()
        store = Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                            projection=projection)
        with lock(store.lock):
            intent, _ = store.prepare("approval-ref", "run-ref")
        # A forged projection binds a different publication ID, never this one.
        forged = Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                             projection={"schema": projection["schema"],
                                         "algorithm": projection["algorithm"],
                                         "digest": "0" * 64})
        self.assertNotEqual(forged.id, store.id)
        with self.assertRaises(Denied):
            Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                        projection={"schema": 99, "algorithm": PUBLIC_SOURCE_ALGORITHM,
                                    "digest": "a" * 64})
        with self.assertRaises(Denied):
            Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                        projection={"schema": PUBLIC_SOURCE_SCHEMA, "algorithm": "other",
                                    "digest": "a" * 64})

    def test_projection_write_bounds_fail_closed_without_echoing_input(self):
        projection, code_digest = self.projection()
        invalid = (None, {}, [], {**projection, "extra": "field"},
                   {**projection, "schema": True}, {**projection, "schema": 2},
                   {**projection, "algorithm": "x" * 129},
                   {**projection, "algorithm": "bad\nlabel"},
                   {**projection, "digest": "A" * 64}, {**projection, "digest": "0" * 65})
        for candidate in invalid:
            with self.subTest(projection=candidate):
                with self.assertRaisesRegex(Denied, "^Invalid projection"):
                    Publication(self.project.root, "a" * 40, code_digest, "main", "push",
                                projection=candidate)
                with self.assertRaisesRegex(Denied, "^Invalid projection"):
                    Publication.record_external(self.project.root, branch="main", published_sha="b" * 40,
                                                code_digest=code_digest, approval="approval",
                                                projection=candidate)
        self.assertFalse((self.project.root / "evidence/publications").exists())

    def test_external_schema3_roundtrip_and_legacy_compat(self):
        projection, code_digest = self.projection()
        record = Publication.record_external(
            self.project.root, branch="ext", published_sha="a" * 40,
            code_digest=code_digest, approval="operator-approval",
            projection=projection)
        self.assertEqual(record["schema"], 3)
        self.assertEqual(record["projection_digest"], projection["digest"])
        self.assertEqual(Publication.read_receipt(self.project.root, record["publication_id"]), record)
        replay = Publication.record_external(
            self.project.root, branch="ext", published_sha="a" * 40,
            code_digest=code_digest, approval="operator-approval",
            projection=projection)
        self.assertEqual(replay, record)
        identity = {"op": "external", "branch": "legacy-ext", "published_sha": "b" * 40,
                    "code_digest": code_digest}
        publication_id = digest(canonical(identity))
        legacy = {**identity, "schema": 2, "publication_id": publication_id,
                  "approval": "operator-approval", "source_commit": None, "run": None,
                  "recorded_at": "2026-01-01T00:00:00+00:00"}
        path = self.project.root / "evidence/publications" / (publication_id + ".external.json")
        write_json(path, legacy)
        before = path.read_bytes()
        self.assertEqual(Publication.read_receipt(self.project.root, publication_id), legacy)
        self.assertEqual(Publication.list_receipts(self.project.root)["total"], 2)
        self.assertEqual(path.read_bytes(), before)
        # A projection-less replay must not serve as a new-write escape hatch.
        with self.assertRaisesRegex(Denied, "projection identity"):
            Publication.record_external(self.project.root, branch="legacy-ext", published_sha="b" * 40,
                                        code_digest=code_digest, approval="operator-approval")
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(Denied):
            Publication.record_external(
                self.project.root, branch="ext", published_sha="a" * 40,
                code_digest=code_digest, approval="other-approval",
                projection=projection)


class PublishViaProjectionTests(Fixture):
    def params(self):
        captured = self.project.snapshots.capture_files(self.project.workspace)
        from mizu.distribution import public_source_identity as identity
        projection = identity(captured.get("files", {}))
        return {"branch": "main", "code_digest": captured["code_digest"],
                "projection_schema": projection["schema"],
                "projection_algorithm": projection["algorithm"],
                "projection_digest": projection["digest"],
                "publication_id": "b" * 64, "source_commit": "c" * 40}

    def test_missing_projection_params_refused_before_adapter(self):
        params = self.params()
        for key in ("projection_schema", "projection_algorithm", "projection_digest"):
            bad = dict(params)
            del bad[key]
            with self.subTest(key=key), patch.object(vcs, "run") as adapter:
                with self.assertRaises(Denied):
                    vcs.publish_via({"command": ["never"]}, "push", bad)
                adapter.assert_not_called()

    def test_adapter_request_keeps_capture_and_projection_distinct(self):
        params = self.params()
        self.assertNotEqual(params["code_digest"], params["projection_digest"])
        def adapter(_argv, **kwargs):
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request, {"op": "push", "branch": params["branch"],
                "digest": params["code_digest"], "source_commit": params["source_commit"],
                "publication_id": params["publication_id"],
                "projection_schema": params["projection_schema"],
                "projection_algorithm": params["projection_algorithm"],
                "projection_digest": params["projection_digest"]})
            # Mock acknowledgement only, not evidence of actual tree verification.
            return Result(0, json.dumps({**request, "published_sha": "a" * 40}), "", "exited", 0)
        with patch.object(vcs, "run", side_effect=adapter):
            result = vcs.publish_via({"command": ["mock-adapter"]}, "push", params)
        self.assertEqual(result["trust"], "external-untrusted")
        self.assertEqual(result["digest"], params["code_digest"])
        self.assertEqual(result["projection_digest"], params["projection_digest"])

    def test_missing_malformed_or_mismatched_projection_echo_refused(self):
        params = self.params()
        good = {"digest": params["code_digest"],
                "projection_schema": params["projection_schema"],
                "projection_algorithm": params["projection_algorithm"],
                "projection_digest": params["projection_digest"],
                "publication_id": params["publication_id"], "published_sha": "a" * 40}
        for key, bad in (("projection_schema", True), ("projection_schema", 2),
                         ("projection_algorithm", "x" * 129), ("projection_digest", "f" * 64)):
            for missing in (False, True):
                data = {**good, key: bad}
                if missing:
                    del data[key]
                reply = Result(0, json.dumps(data), "", "exited", 0)
                with self.subTest(key=key, missing=missing), patch.object(vcs, "run", return_value=reply):
                    with self.assertRaisesRegex(Denied, "projection identity"):
                        vcs.publish_via({"command": ["never"]}, "push", params)


class EditorExportProjectionTests(Fixture):
    def test_export_carries_projection_and_capsule_serves_it(self):
        source = self.project.workspace / "src"
        source.mkdir()
        (source / "module.py").write_bytes(b"VALUE = 1\n")
        capture = self.project.snapshots.capture_files(self.project.workspace)
        before = self.project.snapshots.get()
        current = self.project.snapshots.create(capture, goal=before["goal"], state="", run=None,
                                                outcome="continue", summary="Export fixture", parent=before["id"])
        self.project.snapshots.publish(current)
        bundle = self.root / "bundle-projection"
        result = export(self.project, bundle)
        stored = json.loads((bundle / "public_source.json").read_text(encoding="utf-8"))
        snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["code_digest"], snapshot["code_digest"])
        self.assertEqual(snapshot["files"], capture["files"])
        self.assertEqual(snapshot["code_digest"], capture["code_digest"])
        self.assertEqual(stored["projection_digest"], digest(canonical(source_manifest(self.project.workspace))))
        self.assertEqual(stored["projection_schema"], PUBLIC_SOURCE_SCHEMA)
        self.assertEqual(stored["projection_algorithm"], PUBLIC_SOURCE_ALGORITHM)
        self.assertEqual(result["projection_digest"], stored["projection_digest"])
        self.assertEqual(stored, export_public_source(snapshot))
        capsule = Capsule(bundle, self.project.root / "spool/editor")
        status = capsule.call("get_status", {})
        self.assertEqual(status["public_source"], stored)
        self.assertNotEqual(stored["projection_digest"], snapshot["code_digest"])

    def test_export_projection_rejects_wrong_types_and_extra_fields(self):
        bundle = self.root / "bundle-projection-types"
        export(self.project, bundle)
        path = bundle / "public_source.json"
        good = json.loads(path.read_bytes())
        for alteration in ({"projection_schema": True}, {"projection_schema": 1.0},
                           {"public_files": False}, {"captured_files": -1},
                           {"extra": "unapproved-data"}):
            path.write_text(json.dumps({**good, **alteration}), encoding="utf-8")
            with self.subTest(alteration=alteration):
                with self.assertRaisesRegex(Denied, "^Bundle public-source integrity failure$"):
                    Capsule(bundle, self.project.root / "spool/editor")

    def test_unreadable_projection_is_not_treated_as_a_legacy_bundle(self):
        bundle = self.root / "bundle-unsafe-projection"
        export(self.project, bundle)
        (bundle / "public_source.json").unlink()
        (bundle / "public_source.json").mkdir()
        with self.assertRaises(Denied):
            Capsule(bundle, self.project.root / "spool/editor")

    def test_tampered_projection_refused_and_legacy_bundle_reports_none(self):
        bundle = self.root / "bundle-tamper"
        export(self.project, bundle)
        capsule = Capsule(bundle, self.project.root / "spool/editor")
        self.assertIsNotNone(capsule.call("get_status", {})["public_source"])
        tampered = json.loads((bundle / "public_source.json").read_text(encoding="utf-8"))
        tampered["projection_digest"] = "0" * 64
        (bundle / "public_source.json").write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(Denied, "public-source integrity"):
            Capsule(bundle, self.project.root / "spool/editor")
        legacy = self.root / "bundle-legacy"
        export(self.project, legacy)
        (legacy / "public_source.json").unlink()
        legacy_capsule = Capsule(legacy, self.project.root / "spool/editor")
        self.assertIsNone(legacy_capsule.call("get_status", {})["public_source"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
