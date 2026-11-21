"""Keyed publication-receipt lookup: writer-indexed and fail-closed.

Offline only: no adapter, no network, no real publication.
"""
import json
from unittest.mock import patch

from support import Fixture
from mizu import ci_waits, publication
from mizu.config import PublicationEvidence
from mizu.distribution import public_source_identity
from mizu.errors import Denied
from mizu.fs import canonical, digest
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

    @property
    def index_path(self):
        return (self.root / "evidence" / publication.KEY_INDEX_DIR
                / publication.KEY_INDEX_FILE)

    def index(self):
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def test_warm_hit_and_miss_do_not_enumerate_receipt_names(self):
        receipt = self.record()
        self.record(code_digest=OTHER_DIGEST)
        self.assertEqual(self.lookup(), receipt["publication_id"])
        with patch.object(Publication, "_receipt_names",
                          side_effect=AssertionError("directory enumerated")) as names, \
                patch.object(Publication, "_scan_receipts",
                             side_effect=AssertionError("strict rescan")) as scans, \
                patch.object(Publication, "_build_key_index",
                             side_effect=AssertionError("index rebuilt")) as builds:
            self.assertEqual(self.lookup(), receipt["publication_id"])
            self.assertIsNone(self.lookup(code_digest="e" * 64))
        self.assertEqual(names.call_count, 0)
        self.assertEqual(scans.call_count, 0)
        self.assertEqual(builds.call_count, 0)

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

    def counted_rebuilds(self):
        """Count bounded rebuilds while leaving their result unchanged."""
        rebuilds = []
        real_build = Publication._build_key_index

        def counted(store, policy):
            rebuilds.append(1)
            return real_build(store, policy)

        return patch.object(Publication, "_build_key_index",
                            side_effect=counted), rebuilds

    def test_write_onto_a_legacy_index_rebuilds_it_before_appending(self):
        old = self.record()
        legacy = self.index()
        self.assertEqual(legacy["schema"], publication.KEY_INDEX_SCHEMA)
        legacy["schema"] = publication.KEY_INDEX_LEGACY_SCHEMA
        for entry in legacy["entries"]:
            for field in ("order", "branch", "code_digest", "published_sha"):
                entry.pop(field)
        self.index_path.write_bytes(canonical(legacy))
        # The legacy index is still valid for keyed resolution on its own.
        self.assertEqual(self.lookup(), old["publication_id"])

        newer = self.record(code_digest=OTHER_DIGEST)
        rebuilt = self.index()
        self.assertEqual(rebuilt["schema"], publication.KEY_INDEX_SCHEMA)
        fields = {"publication_id", "kind", "sha256", "order", "branch",
                  "code_digest", "published_sha"}
        self.assertEqual(len(rebuilt["entries"]), 2)
        self.assertTrue(all(set(entry) == fields for entry in rebuilt["entries"]))
        # Both the pre-existing and the new key still resolve.
        self.assertEqual(self.lookup(), old["publication_id"])
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST),
                         newer["publication_id"])
        # Listing also runs from the rebuilt index, not the scan fallback.
        page = Publication.list_receipts(self.root, limit=1)
        self.assertFalse(page["scan_stopped"])
        self.assertEqual(page["total"], 2)

    def test_sequential_writer_calls_only_rebuild_once(self):
        # Product intent and dispatch files precede the first build; the
        # later receipt they authorize is the writer's own file.
        product = Publication(self.root, "e" * 40, "f" * 64, BRANCH, "push",
                              projection=public_source_identity({}))
        intent, _ = product.prepare("approval", "run")
        product.mark_dispatch()
        builds, rebuilds = self.counted_rebuilds()
        with builds:
            first = self.record()
            second = self.record(code_digest=OTHER_DIGEST)
            replay = self.record()  # An identical replay writes no receipt.
            product.complete(intent, "1" * 40)
        self.assertEqual(len(rebuilds), 1)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.index()["entries"]), 3)
        # The appended index carries a current stamp, so warmed lookups work.
        self.assertEqual(self.lookup(), first["publication_id"])
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST), second["publication_id"])

    def test_copied_in_receipt_forces_a_rebuild_and_denies_until_clean_rebuild(self):
        receipt = self.record()
        copied = self.directory / ("0" * 64 + ".external.json")
        copied.write_bytes(canonical(receipt))
        # Neutralize the stamp change so only the name-set check can detect the
        # out-of-band file: refreshing the old stamp must never index it.
        value = self.index()
        value["stamp"] = publication._directory_stamp(self.directory)
        self.index_path.write_bytes(canonical(value))
        builds, rebuilds = self.counted_rebuilds()
        with builds:
            self.record()  # Replay of the writer's own receipt.
            self.assertEqual(len(rebuilds), 1)
            self.assertTrue(self.index()["incomplete"])
            with self.assertRaises(Denied):
                self.lookup()
            copied.unlink()
            self.record()
            self.assertEqual(len(rebuilds), 2)
        self.assertFalse(self.index()["incomplete"])
        self.assertEqual(self.lookup(), receipt["publication_id"])

    def test_same_name_edit_stays_denied_through_an_append(self):
        matching = self.record()
        edited = self.record(code_digest=OTHER_DIGEST)
        path = self.directory / (edited["publication_id"] + ".external.json")
        original = canonical(edited)
        path.write_bytes(canonical({"approval": "out-of-band"}))
        with self.assertRaises(Denied):
            self.lookup(code_digest=OTHER_DIGEST)
        # A content-only edit leaves the directory stamp and name set intact;
        # appending another receipt must not adopt the changed content.
        builds, rebuilds = self.counted_rebuilds()
        with builds:
            self.record(code_digest="e" * 64)
            self.assertEqual(rebuilds, [])
        with self.assertRaises(Denied):
            self.lookup(code_digest=OTHER_DIGEST)
        entry = next(row for row in self.index()["entries"]
                     if row["publication_id"] == edited["publication_id"])
        self.assertEqual(entry["sha256"], digest(original))
        # Only a fresh writer rebuild clears the denial.
        path.write_bytes(original)
        self.index_path.write_bytes(b"{}")
        builds, rebuilds = self.counted_rebuilds()
        with builds:
            self.record(code_digest="f" * 64)
            self.assertEqual(len(rebuilds), 1)
        self.assertFalse(self.index()["incomplete"])
        self.assertEqual(self.lookup(), matching["publication_id"])
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST), edited["publication_id"])

    def test_corrupt_file_blocks_lookup_but_a_writer_still_indexes_its_receipt(self):
        receipt = self.record()
        corrupt = self.directory / ("0" * 64 + ".external.json")
        corrupt.write_text("{}", encoding="utf-8")
        with self.assertRaises(Denied):
            self.lookup()
        # An unrelated corrupt file cannot block a new valid receipt: the
        # writer rebuilds, indexes it and records incompleteness instead.
        newer = self.record(code_digest=OTHER_DIGEST)
        index = self.index()
        self.assertTrue(index["incomplete"])
        self.assertIn(newer["publication_id"],
                      {row["publication_id"] for row in index["entries"]})
        with self.assertRaises(Denied):
            self.lookup()
        with self.assertRaises(Denied):
            self.lookup(code_digest=OTHER_DIGEST)
        corrupt.unlink()
        # Removing the file alone leaves a stale stamp; no reader repairs it.
        with self.assertRaises(Denied):
            self.lookup()
        self.record()
        self.assertFalse(self.index()["incomplete"])
        self.assertEqual(self.lookup(), receipt["publication_id"])
        self.assertEqual(self.lookup(code_digest=OTHER_DIGEST),
                         newer["publication_id"])

    def test_missing_index_and_corrupt_receipt_does_not_block_first_write(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        corrupt = self.directory / ("0" * 64 + ".external.json")
        corrupt.write_text("{}", encoding="utf-8")
        self.assertIsNone(self.lookup())
        receipt = self.record()
        index = self.index()
        self.assertTrue(index["incomplete"])
        self.assertEqual([row["publication_id"] for row in index["entries"]],
                         [receipt["publication_id"]])
        with self.assertRaises(Denied):
            self.lookup()
        corrupt.unlink()
        self.record()
        self.assertFalse(self.index()["incomplete"])
        self.assertEqual(self.lookup(), receipt["publication_id"])

    def test_oversized_candidate_is_tolerated_by_writer_and_denies_lookup(self):
        receipt = self.record()
        corrupt = self.directory / ("0" * 64 + ".external.json")
        corrupt.write_bytes(b'{"x":' + b" " * 9000 + b"}")
        with self.assertRaises(Denied):
            self.lookup()
        self.record()
        self.assertTrue(self.index()["incomplete"])
        with self.assertRaises(Denied):
            self.lookup()
        corrupt.unlink()
        self.record()
        self.assertEqual(self.lookup(), receipt["publication_id"])

    def test_incomplete_flag_is_validated_and_denies_lookup(self):
        receipt = self.record()
        value = self.index()
        for invalid in (None, "yes", 1):
            with self.subTest(invalid=invalid):
                mutable = dict(value)
                if invalid is None:
                    del mutable["incomplete"]
                else:
                    mutable["incomplete"] = invalid
                self.index_path.write_bytes(canonical(mutable))
                with self.assertRaises(Denied):
                    self.lookup()
        value["incomplete"] = True
        self.index_path.write_bytes(canonical(value))
        with self.assertRaises(Denied):
            self.lookup()
        # Only a writer rebuild, never a plain append, clears the denial.
        self.record()
        self.assertFalse(self.index()["incomplete"])
        self.assertEqual(self.lookup(), receipt["publication_id"])

    def test_malformed_receipt_name_refuses_rebuild(self):
        self.record()
        malformed = self.directory / ("e" * 64 + ".external.json.bak")
        malformed.write_bytes(canonical({}))
        with self.assertRaises(Denied):
            self.record()

    def test_non_dict_key_index_entry_is_denied(self):
        self.record()
        index = self.root / "evidence" / publication.KEY_INDEX_DIR / publication.KEY_INDEX_FILE
        value = json.loads(index.read_text(encoding="utf-8"))
        value["keys"] = [123]
        index.write_bytes(canonical(value))
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
        with patch.object(Publication, "_receipt_names",
                          side_effect=AssertionError("directory enumerated")) as names, \
                patch.object(Publication, "_build_key_index",
                             side_effect=AssertionError("index rebuilt")) as builds:
            self.assertIsNone(self.lookup())
        self.assertEqual(names.call_count, 0)
        self.assertEqual(builds.call_count, 0)
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
        # The writer update must rebuild from directory state: refreshing the
        # stamp on the old view would silently clear this denial.
        self.record()
        self.assertTrue(self.index()["incomplete"])
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

    def observation(self, policy=None):
        return Publication.key_observation(
            self.root, policy=self.policy if policy is None else policy)

    def test_batch_answers_distinct_keys_from_one_index_stamp_and_byte_scan(self):
        first = self.record()
        second = self.record(code_digest=OTHER_DIGEST)
        with patch.object(Publication, "_read_key_index",
                          wraps=Publication._read_key_index) as index_reads, \
                patch.object(publication, "_directory_stamp",
                             wraps=publication._directory_stamp) as stamps, \
                patch.object(publication, "safe_read",
                             wraps=publication.safe_read) as byte_reads, \
                patch.object(Publication, "_validated_receipt",
                             wraps=Publication._validated_receipt) as validations:
            observation = self.observation()
            self.assertEqual(observation.resolve(BRANCH, DIGEST), first["publication_id"])
            self.assertEqual(observation.resolve(BRANCH, OTHER_DIGEST), second["publication_id"])
            self.assertIsNone(observation.resolve(BRANCH, "e" * 64))
            self.assertIsNone(observation.resolve("unpublished", DIGEST))
        self.assertEqual(index_reads.call_count, 1)
        self.assertEqual(stamps.call_count, 1)
        self.assertEqual(byte_reads.call_count, 3)  # the index and two receipts
        self.assertEqual(validations.call_count, 2)

    def test_single_key_entry_point_reuses_one_observation(self):
        first = self.record()
        second = self.record(code_digest=OTHER_DIGEST, published_sha=OTHER_SHA)
        observation = self.observation()
        with patch.object(Publication, "_read_key_index",
                          wraps=Publication._read_key_index) as index_reads:
            for code_digest, expected in ((DIGEST, first), (OTHER_DIGEST, second),
                                          ("e" * 64, None)):
                self.assertEqual(
                    Publication.receipt_for_key(
                        self.root, BRANCH, code_digest, policy=self.policy,
                        observation=observation),
                    expected["publication_id"] if expected else None)
        self.assertEqual(index_reads.call_count, 1)

    def test_observation_is_bound_to_its_project_root_and_policy(self):
        self.record()
        observation = self.observation()
        with self.assertRaises(Denied):
            Publication.receipt_for_key(self.root, BRANCH, DIGEST,
                                        policy=self.policy, observation=object())
        with self.assertRaises(Denied):
            Publication.receipt_for_key(self.root / "other", BRANCH, DIGEST,
                                        policy=self.policy, observation=observation)
        with self.assertRaises(Denied):
            Publication.receipt_for_key(self.root, BRANCH, DIGEST,
                                        policy=PublicationEvidence(record_limit=1),
                                        observation=observation)

    def test_same_name_change_fails_closed_for_every_key_in_a_fresh_batch(self):
        matching = self.record()
        unrelated = self.record(code_digest=OTHER_DIGEST)
        observation = self.observation()
        self.assertEqual(observation.resolve(BRANCH, DIGEST), matching["publication_id"])
        path = self.directory / (unrelated["publication_id"] + ".external.json")
        path.write_bytes(b"{}")
        for branch, code_digest in ((BRANCH, DIGEST), (BRANCH, OTHER_DIGEST),
                                    (BRANCH, "e" * 64)):
            with self.subTest(branch=branch, code_digest=code_digest):
                with self.assertRaises(Denied):
                    self.observation().resolve(branch, code_digest)
        # The earlier observation stays bound to the bytes it verified, so a
        # later answer cannot silently adopt the changed file.
        self.assertEqual(observation.resolve(BRANCH, OTHER_DIGEST),
                         unrelated["publication_id"])

    def test_fresh_observation_sees_a_rebuilt_index(self):
        matching = self.record()
        old = self.record(code_digest=OTHER_DIGEST)
        observation = self.observation()
        self.assertEqual(observation.resolve(BRANCH, OTHER_DIGEST), old["publication_id"])
        (self.directory / (old["publication_id"] + ".external.json")).unlink()
        replacement = self.record(code_digest="e" * 64)
        fresh = self.observation()
        self.assertIsNone(fresh.resolve(BRANCH, OTHER_DIGEST))
        self.assertEqual(fresh.resolve(BRANCH, DIGEST), matching["publication_id"])
        self.assertEqual(fresh.resolve(BRANCH, "e" * 64), replacement["publication_id"])
        self.assertEqual(observation.resolve(BRANCH, OTHER_DIGEST), old["publication_id"])

    def test_batch_validates_the_candidate_with_index_witnessed_bytes(self):
        receipt = self.record()
        observation = self.observation()
        with patch.object(Publication, "_validated_receipt",
                          side_effect=Denied("candidate refused")) as validate:
            with self.assertRaises(Denied):
                observation.resolve(BRANCH, DIGEST)
        self.assertEqual(validate.call_count, 1)
        with patch.object(Publication, "_validated_receipt",
                          wraps=Publication._validated_receipt) as validate:
            self.assertEqual(observation.resolve(BRANCH, DIGEST), receipt["publication_id"])
        raw, sha = validate.call_args.kwargs["observed"]["external"]
        self.assertEqual(raw, canonical(receipt))
        self.assertEqual(sha, digest(canonical(receipt)))
