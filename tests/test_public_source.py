"""Public-source projection boundary: whole capture vs filtered projection.

Offline only: no adapter, no network, no ignored-state inspection.
"""
import json
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
from mizu.fs import canonical, digest, lock
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
        # The projection digest is not a code digest: IDs differ by construction.
        legacy = Publication(self.project.root, "a" * 40, code_digest, "main", "push")
        self.assertNotEqual(legacy.id, store.id)
        self.assertEqual(legacy.schema, 1)
        self.assertEqual(store.schema, 3)

    def test_legacy_schema1_receipt_stays_readable(self):
        _, code_digest = self.projection()
        legacy = Publication(self.project.root, "a" * 40, code_digest, "legacy", "push")
        with lock(legacy.lock):
            intent, _ = legacy.prepare("approval-ref", "run-ref")
            legacy.mark_dispatch()
            receipt = legacy.complete(intent, "b" * 40)
        self.assertEqual(receipt["schema"], 1)
        self.assertNotIn("projection_digest", receipt)
        self.assertEqual(Publication.read_receipt(self.project.root, legacy.id), receipt)

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
        legacy = Publication.record_external(
            self.project.root, branch="legacy-ext", published_sha="b" * 40,
            code_digest=code_digest, approval="operator-approval")
        self.assertEqual(legacy["schema"], 2)
        self.assertEqual(Publication.read_receipt(self.project.root, legacy["publication_id"]), legacy)
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

    def test_projection_echo_mismatch_refused(self):
        params = self.params()
        reply = Result(0, json.dumps({"digest": params["code_digest"],
                                      "projection_schema": params["projection_schema"],
                                      "projection_algorithm": params["projection_algorithm"],
                                      "projection_digest": "f" * 64,
                                      "publication_id": params["publication_id"],
                                      "published_sha": "a" * 40}), "", "exited", 0)
        with patch.object(vcs, "run", return_value=reply):
            with self.assertRaisesRegex(Denied, "projection identity"):
                vcs.publish_via({"command": ["never"]}, "push", params)


class EditorExportProjectionTests(Fixture):
    def test_export_carries_projection_and_capsule_serves_it(self):
        bundle = self.root / "bundle-projection"
        result = export(self.project, bundle)
        stored = json.loads((bundle / "public_source.json").read_text(encoding="utf-8"))
        snapshot = json.loads((bundle / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["code_digest"], snapshot["code_digest"])
        self.assertEqual(stored["projection_schema"], PUBLIC_SOURCE_SCHEMA)
        self.assertEqual(stored["projection_algorithm"], PUBLIC_SOURCE_ALGORITHM)
        self.assertEqual(result["projection_digest"], stored["projection_digest"])
        self.assertEqual(stored, export_public_source(snapshot))
        capsule = Capsule(bundle, self.project.root / "spool/editor")
        status = capsule.call("get_status", {})
        self.assertEqual(status["public_source"], stored)
        self.assertNotEqual(stored["projection_digest"], snapshot["code_digest"])

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
