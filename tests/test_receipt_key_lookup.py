"""Keyed publication-receipt lookup: writer-indexed and fail-closed.

Offline only: no adapter, no network, no real publication.
"""
from unittest.mock import patch

from support import Fixture
from mizu import ci_waits, publication
from mizu.config import PublicationEvidence
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import canonical
from mizu.publication import Publication

SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "c" * 64
OTHER_DIGEST = "d" * 64
BRANCH = "published"
APPROVAL = "operator-approval-1"


class ReceiptKeyLookupTests(Fixture):
    def setUp(self):
        super().setUp()
        self.root = self.project.root
        self.directory = self.root / "evidence" / "publications"
        self.policy = PublicationEvidence()

    def record(self, **overrides):
        arguments = {"branch": BRANCH, "published_sha": SHA, "code_digest": DIGEST,
                     "approval": APPROVAL, "projection": public_source_identity({})}
        arguments.update(overrides)
        return Publication.record_external(self.root, **arguments)

    def lookup(self, *, branch=BRANCH, code_digest=DIGEST, policy=None):
        return Publication.receipt_for_key(
            self.root, branch, code_digest,
            policy=self.policy if policy is None else policy)

    def test_warm_hit_and_miss_do_not_enumerate_receipt_names(self):
        receipt = self.record()
        self.record(code_digest=OTHER_DIGEST)
        self.assertEqual(self.lookup(), receipt["publication_id"])
        with patch.object(Publication, "_receipt_names",
                          side_effect=AssertionError("directory enumerated")) as names, \
                patch.object(Publication, "_scan_receipts",
                             side_effect=AssertionError("strict rescan")) as scans:
            self.assertEqual(self.lookup(), receipt["publication_id"])
            self.assertIsNone(self.lookup(code_digest="e" * 64))
        self.assertEqual(names.call_count, 0)
        self.assertEqual(scans.call_count, 0)

    def test_zero_matches_and_unrelated_keys_resolve_nothing(self):
        self.assertIsNone(self.lookup())
        other_digest = self.record(code_digest=OTHER_DIGEST)
        other_branch = self.record(branch="other")
        self.assertIsNone(self.lookup())
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST),
                         other_digest["publication_id"])
        self.assertIsNone(self.lookup(branch="other", code_digest=OTHER_DIGEST))
        self.assertIsNone(self.lookup(branch="unpublished", code_digest=OTHER_DIGEST))
        self.assertEqual(self.lookup(branch="other"), other_branch["publication_id"])

    def test_two_matching_receipts_are_ambiguous(self):
        first = self.record()
        second = self.record(published_sha=OTHER_SHA)
        self.assertNotEqual(first["publication_id"], second["publication_id"])
        page = Publication.list_receipts(self.root, code_digest=DIGEST, branch=BRANCH)
        self.assertEqual(page["total"], 2)
        self.assertIsNone(self.lookup())

    def test_corrupt_oversized_or_invalid_candidate_raises_instead_of_absence(self):
        receipt = self.record()
        corrupt = self.directory / ("0" * 64 + ".external.json")
        corrupt.write_text("{}", encoding="utf-8")
        with self.assertRaises(Denied):
            self.lookup()
        corrupt.unlink()
        # A writer replay repairs the stamp and re-establishes keyed evidence.
        self.record()
        self.assertEqual(self.lookup(), receipt["publication_id"])
        corrupt.write_bytes(b'{"x":' + b" " * 9000 + b"}")
        with self.assertRaises(Denied):
            self.lookup()

    def test_unindexed_legacy_file_is_not_used_as_keyed_evidence(self):
        legacy = self.record()
        path = self.directory / (legacy["publication_id"] + ".external.json")
        path.unlink()
        index = self.root / "evidence" / publication.KEY_INDEX_DIR / publication.KEY_INDEX_FILE
        index.unlink()
        path.write_bytes(canonical(legacy))
        # Ordinary listing still sees the legacy file, but keyed resolution
        # has no writer-built index and therefore cannot claim completeness.
        page = Publication.list_receipts(self.root, code_digest=DIGEST, branch=BRANCH)
        self.assertEqual(page["total"], 1)
        self.assertIsNone(self.lookup())
        newer = self.record(code_digest=OTHER_DIGEST)
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST), newer["publication_id"])
        self.assertEqual(self.lookup(), legacy["publication_id"])

    def test_same_name_corruption_of_an_unrelated_receipt_fails_closed(self):
        matching = self.record()
        unrelated = self.record(code_digest=OTHER_DIGEST)
        self.assertEqual(self.lookup(), matching["publication_id"])
        path = self.directory / (unrelated["publication_id"] + ".external.json")
        path.write_bytes(b"{}")
        with self.assertRaises(Denied):
            self.lookup()
        with self.assertRaises(Denied):
            Publication.list_receipts(self.root, strict=True)

    def test_key_bounds_and_name_changes_fail_closed(self):
        matching = self.record()
        self.record(code_digest=OTHER_DIGEST)
        with self.assertRaises(Denied):
            self.lookup(policy=PublicationEvidence(record_limit=1))
        relaxed = PublicationEvidence(name_processing_limit=100, record_limit=100)
        self.assertEqual(self.lookup(policy=relaxed), matching["publication_id"])
        outsider = self.directory / ("0" * 64 + ".external.json")
        outsider.write_bytes(canonical({}))
        with self.assertRaises(Denied):
            self.lookup(policy=relaxed)

    def test_resolved_wait_revalidates_digest_and_branch(self):
        snapshot = self.project.snapshots.get()
        receipt = Publication.record_external(
            self.root, branch=BRANCH, published_sha=SHA,
            code_digest=snapshot["code_digest"], approval=APPROVAL,
            projection=public_source_identity(snapshot["files"]))
        wait = {"kind": "publication_ci", "snapshot_id": snapshot["id"],
                "code_digest": snapshot["code_digest"], "branch": BRANCH,
                "checks": ["unit"]}
        store = self.project.work_items
        resolved = ci_waits._resolved(store, wait)
        self.assertEqual(resolved["publication_id"], receipt["publication_id"])
        self.assertEqual(resolved["sha"], SHA)
        self.assertEqual(resolved["snapshot_relation"], "exact")
        # A foreign ID returned by the lookup cannot resolve the wait: the
        # existing per-call digest and branch checks still apply.
        foreign = self.record(code_digest=OTHER_DIGEST)
        with patch.object(Publication, "receipt_for_key", return_value=foreign["publication_id"]):
            self.assertIsNone(ci_waits._resolved(store, wait))
