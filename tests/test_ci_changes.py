"""Change-driven receipt-wait observation: fake adapters, no network."""
import dataclasses
import json
from unittest.mock import Mock, patch

import test_ci_waits as existing

from test_ci_waits import CiWaitFixture, SHA
from mizu import ci_waits, vcs
from mizu.errors import Denied
from mizu.fs import read_json
from mizu.project import Project
from mizu.publication import Publication
from mizu.distribution import public_source_identity
from mizu.runtime import poll_project_vcs, refresh_poll_state


class CiChangesTests(CiWaitFixture):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
                                                          "change_stream_enabled": True})
        self.trace = []
        self.subjects = []
        self.next_cursor = "opaque / é:1"
        self.status = "pending"

    def adapter(self, settings, request, **kwargs):
        self.trace.append(request.copy())
        if request["op"] == "changes":
            return {"op": "changes", "cursor": request.get("cursor"),
                    "subjects": self.subjects, "next_cursor": self.next_cursor}
        self.assertEqual(request["op"], "status")
        return {"checks": self.rows(self.status, request["sha"])}

    def tick(self, adapter=None, advance=True):
        if advance:
            self.clock += 30
        self.trace.clear()
        with patch.object(vcs, "invoke", side_effect=adapter or self.adapter):
            return ci_waits.poll(self.config, self.project, self.state,
                                 now=self.clock, interval=15)

    def calls(self, op):
        return [row for row in self.trace if row["op"] == op]

    def make_wait(self, i, branch="published"):
        publication = Publication(self.project.root, f"{i:040x}", "c" * 64,
                                  branch, "push", projection=public_source_identity({}))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        publication.complete(intent, f"{i:040x}")
        return {"kind": "ci", "publication_id": publication.id, "checks": ["lint", "unit"]}

    def test_subject_contract(self):
        self.assertEqual(vcs.ci_status_subject("topic/exact"), "ci-status:topic/exact")
        self.assertEqual(vcs.parse_ci_status_subject("ci-status:topic/exact"), "topic/exact")
        self.assertIsNone(vcs.parse_ci_status_subject("other:published"))
        for branch in ("", "../bad", "bad\n", "bad:branch"):
            with self.subTest(branch=branch), self.assertRaises(Denied):
                vcs.parse_ci_status_subject("ci-status:" + branch)

    def test_disabled_unchanged_cadence_rotation_and_evidence(self):
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
                                                          "change_stream_enabled": False})
        items = [self.block(str(i), wait=self.make_wait(i)) for i in range(5)]
        seen = set()
        self.status = "success"
        for _ in range(3):
            result = self.tick()
            self.assertEqual(self.calls("changes"), [])
            self.assertLessEqual(result["polled"], 2)
            seen.update(row["sha"] for row in self.calls("status"))
        self.assertEqual(seen, {f"{i:040x}" for i in range(5)})
        self.assertTrue(all(ci_waits.evidence(self.store, item) for item in items))
        self.assertIsNone(self.tick(advance=False))
        self.assertEqual(self.trace, [])
        self.assertNotIn("ci_changes_cursor", self.state)
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, "command": []})
        self.assertIsNone(self.tick())
        self.assertEqual(self.trace, [])

    def test_empty_stream_enrolls_once_not_on_cadence_and_new_wait_enrolls(self):
        item = self.block()
        self.assertEqual(self.tick()["polled"], 1)
        for _ in range(2):
            self.assertEqual(self.tick()["polled"], 0)
            self.assertEqual(len(self.calls("changes")), 1)
            self.assertEqual(self.calls("status"), [])
        new = self.block("new")
        self.status = "success"
        self.assertEqual(self.tick()["observed"], 2)  # shared exact-target response
        self.assertEqual(len(self.calls("status")), 1)
        self.assertTrue(ci_waits.evidence(self.store, item))
        self.assertTrue(ci_waits.evidence(self.store, new))

    def test_idle_empty_page_skips_response_traversal(self):
        items = [self.block(str(i), wait=self.make_wait(i)) for i in range(2)]
        self.subjects = []
        self.assertEqual(self.tick()["polled"], 2)  # enroll both targets
        self.assertEqual(self.tick()["polled"], 0)  # caught up
        with patch.object(ci_waits, "_poll_candidates",
                          wraps=ci_waits._poll_candidates) as counted:
            result = self.tick()
        self.assertEqual((result["polled"], result["observed"]), (0, 0))
        self.assertTrue(result["ok"])
        # Only the single eligibility pass traverses candidates. The empty
        # response pass used to resolve every waiter's candidates again to no
        # effect (call count was 2); it is now skipped when no target is selected.
        self.assertEqual(counted.call_count, 1)
        self.assertEqual(self.calls("status"), [])
        self.assertEqual(len(self.calls("changes")), 1)

    def test_subject_maps_exact_branch_and_all_pinned_shas_with_shared_reads(self):
        items = [self.block("first"), self.block("same")]
        items.append(self.block("older", wait=self.make_wait(1)))
        other = self.block("other", wait=self.make_wait(2, "Published"))
        self.tick()
        self.tick()  # initial enrollment of third target
        self.subjects = ["unrelated:published"]
        self.assertEqual(self.tick()["polled"], 0)
        self.subjects = ["ci-status:published", "ci-status:published"]
        self.status = "success"
        result = self.tick()
        self.assertEqual((result["polled"], result["observed"]), (2, 3))
        self.assertEqual({(r["branch"], r["sha"]) for r in self.calls("status")},
                         {("published", SHA), ("published", f"{1:040x}")})
        self.assertTrue(all(ci_waits.evidence(self.store, item) for item in items))
        self.assertIsNone(ci_waits.evidence(self.store, other))

    def test_fanout_drain_survives_restart_does_not_commit_past_tail(self):
        items = [self.block(str(i), wait=self.make_wait(i)) for i in range(5)]
        for _ in range(3):
            self.tick()
        self.subjects = ["ci-status:published", "ci-status:published"]
        self.next_cursor = "new opaque cursor"
        self.status = "success"
        seen = set()
        for index in range(3):
            result = self.tick()
            self.assertLessEqual(result["polled"], 2)
            seen.update(row["sha"] for row in self.calls("status"))
            self.assertEqual(len(self.calls("changes")), 1 if index == 0 else 0)
            if index < 2:
                self.assertNotEqual(self.state["ci_changes_cursor"], self.next_cursor)
                self.assertTrue(self.state["ci_changes_pending"])
            resumed = {}
            refresh_poll_state(resumed, json.loads(json.dumps(self.state)))
            self.state = resumed
        self.assertEqual(seen, {f"{i:040x}" for i in range(5)})
        self.assertEqual(self.state["ci_changes_cursor"], self.next_cursor)
        self.assertIsNone(self.state["ci_changes_pending"])
        self.assertTrue(all(ci_waits.evidence(self.store, item) for item in items))

    def test_unsupported_malformed_truncated_and_overbudget_fall_back_once(self):
        for i in range(5):
            self.block(str(i), wait=self.make_wait(i))
        responses = [None, {"next_cursor": None}, {"subjects": None},
                     {"subjects": ["ci-status:published"] * 3},
                     {"truncated": True}, {"cursor": "wrong"},
                     {"subjects": ["ci-status:../bad"]}, OSError("offline")]
        for bad in responses:
            def adapter(settings, request, **kwargs):
                if request["op"] != "changes":
                    return self.adapter(settings, request, **kwargs)
                self.trace.append(request.copy())
                if isinstance(bad, Exception):
                    raise bad
                if bad is None:
                    return {"op": "changes", "cursor": request.get("cursor"), "supported": False}
                return {"op": "changes", "cursor": request.get("cursor"),
                        "subjects": [], "next_cursor": "next", **bad}
            with self.subTest(bad=bad):
                result = self.tick(adapter)
                self.assertEqual(len(self.calls("changes")), 1)
                self.assertEqual((result["polled"], len(self.calls("status"))), (2, 2))
                self.assertIsNone(self.tick(adapter, advance=False))
                self.assertEqual(self.trace, [])

    def test_corrupt_state_recovers_via_full_read_not_false_caught_up(self):
        self.block()
        self.tick()
        for corrupt in ({"ci_changes_cursor": 3}, {"ci_changes_cursor": "bad\n"},
                        {"ci_changes_pending": {}}, {"ci_changes_epoch": "bad"},
                        {"ci_changes_pending": {"cursor": self.state["ci_changes_cursor"],
                         "next_cursor": "next", "branches": ["published"] * 3, "after": []}}):
            with self.subTest(corrupt=corrupt):
                refresh_poll_state(self.state, corrupt)
                result = self.tick()
                self.assertEqual(result["polled"], 1)
                self.assertEqual(len(self.calls("status")), 1)
                self.assertFalse(result["ok"])
                self.tick()  # recovery enrollment
                self.assertEqual(self.tick()["polled"], 0)

    def test_runtime_round_trip_preserves_stream_state(self):
        self.block()
        with patch("mizu.runtime.poll_upstream", return_value=None), \
                patch("mizu.runtime.poll_ci", return_value=None), \
                patch("mizu.runtime.poll_proposals", return_value=None), \
                patch("mizu.runtime._live_ci_branches", return_value=set()), \
                patch.object(vcs, "invoke", side_effect=self.adapter):
            poll_project_vcs(self.config, self.project, self.state, now=1000, interval=15)
        shared = read_json(self.project.root / "vcs-poll.json")
        self.assertEqual(shared["ci_changes_cursor"], self.next_cursor)
        resumed = {}
        refresh_poll_state(resumed, shared)
        self.state = resumed
        self.assertEqual(self.tick()["polled"], 0)
        self.assertEqual(self.calls("status"), [])

    def test_exact_status_refusals_in_change_mode(self):
        item = self.block()
        self.tick()
        bad = [{"branch": "other"}, {"sha": "e" * 40}, {"complete": False},
               {"truncated": True}, {"scan_stopped": True}, {"cursor": "more"},
               {"checks": self.rows(sha="e" * 40)}, {"checks": self.rows()[:1]},
               {"checks": self.rows() + [self.rows()[0]]}]
        self.subjects = ["ci-status:published"]
        for extra in bad:
            def adapter(settings, request, **kwargs):
                if request["op"] == "changes":
                    return self.adapter(settings, request, **kwargs)
                self.trace.append(request.copy())
                return {"checks": self.rows(), **extra}
            with self.subTest(extra=extra):
                self.assertEqual(self.tick(adapter)["observed"], 0)
                self.assertEqual(len(self.calls("status")), 1)
                self.assertIsNone(ci_waits.evidence(self.store, item))

    def test_changed_revision_during_adapter_call_cannot_create_evidence(self):
        item = self.block()
        self.tick()
        self.subjects = ["ci-status:published"]
        self.status = "success"
        def adapter(settings, request, **kwargs):
            if request["op"] == "status":
                self.store.transition(item["id"], expected_rev=item["rev"], status="open", actor="worker")
            return self.adapter(settings, request, **kwargs)
        self.assertEqual(self.tick(adapter)["observed"], 0)
        self.assertFalse(ci_waits._path(self.store, item).exists())
        self.tick()  # polling writer removes observations for the released wait
        self.assertEqual(list((self.project.root / "ci-wait-observations").iterdir()), [])

    def test_failed_invalidated_read_retries_after_cursor_commit(self):
        item = self.block()
        self.tick()
        self.subjects = ["ci-status:published"]
        def adapter(settings, request, **kwargs):
            if request["op"] == "status":
                self.trace.append(request.copy())
                raise Denied("status unavailable")
            return self.adapter(settings, request, **kwargs)
        self.next_cursor = "past failure"
        self.assertEqual(self.tick(adapter)["observed"], 0)
        self.assertEqual(self.state["ci_changes_cursor"], "past failure")
        self.subjects = []
        self.status = "success"
        self.assertEqual(self.tick()["observed"], 1)
        self.assertEqual(len(self.calls("status")), 1)
        self.assertTrue(ci_waits.evidence(self.store, item))

    def test_enrollment_interleaves_with_repeated_invalidations_at_one_target_bound(self):
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
                                                          "poll_max_ci_wait_targets": 1})
        self.block()
        self.tick()
        self.block("new-branch", wait=self.make_wait(1, "other"))
        self.subjects = ["ci-status:published"]
        seen = set()
        for _ in range(3):
            self.assertLessEqual(self.tick()["polled"], 1)
            seen.update(row["branch"] for row in self.calls("status"))
        self.assertEqual(seen, {"published", "other"})

    def test_corrupt_observations_reenroll_and_old_revision_is_pruned(self):
        item = self.block()
        self.tick()
        path = self.project.root / "ci-wait-observations" / (item["id"] + ".json")
        path.write_text("not json")
        self.assertEqual(self.tick()["polled"], 1)
        self.assertEqual(self.tick()["polled"], 0)
        self.store.transition(item["id"], expected_rev=item["rev"], status="open", actor="worker")
        self.store.transition(item["id"], expected_rev=item["rev"] + 1, status="blocked",
                              actor="worker", wait=self.wait)
        self.assertEqual(self.tick()["polled"], 1)
        self.assertEqual(read_json(path)["rev"], item["rev"] + 2)

    def test_retry_persistence_failure_holds_page_until_invalidation_is_safe(self):
        self.block()
        self.tick()
        old_cursor = self.state["ci_changes_cursor"]
        self.next_cursor = "later"
        self.subjects = ["ci-status:published"]
        with patch.object(ci_waits._Enrollment, "set", side_effect=OSError("cannot persist retry")):
            result = self.tick()
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls("status"), [])
        self.assertEqual(self.state["ci_changes_cursor"], old_cursor)
        self.assertIsNotNone(self.state["ci_changes_pending"])
        self.status = "success"
        self.assertEqual(self.tick()["observed"], 1)
        self.assertEqual(self.calls("changes"), [])
        self.assertEqual(self.state["ci_changes_cursor"], "later")

    def test_failed_evidence_write_is_durable_retry_not_terminal_delivery(self):
        item = self.block()
        self.tick()
        self.subjects = ["ci-status:published"]
        self.status = "success"
        with patch.object(ci_waits, "write_json", side_effect=OSError("cannot write result")):
            self.assertFalse(self.tick()["ok"])
        self.assertFalse(ci_waits._path(self.store, item).exists())
        self.subjects = []
        self.assertEqual(self.tick()["observed"], 1)
        self.assertTrue(ci_waits.evidence(self.store, item))

    def test_new_receipt_and_pinned_publication_branch_enroll_without_subject(self):
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, "ci_branch": "published"})
        self.store.vcs = self.config.vcs
        snapshot = self.project.snapshots.get()
        wait = {"kind": "publication_ci", "snapshot_id": snapshot["id"], "checks": ["unit", "lint"]}
        item = self.block(wait=wait)
        self.assertEqual(self.tick()["polled"], 0)
        publication = Publication(self.project.root, "d" * 40, snapshot["code_digest"],
                                  "published", "push", projection=public_source_identity(snapshot["files"]))
        intent, _ = publication.prepare("approval", "run")
        publication.mark_dispatch()
        publication.complete(intent, SHA)
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs, "ci_branch": "other"})
        self.assertEqual(self.tick()["polled"], 1)
        self.assertEqual(self.tick()["polled"], 0)
        self.subjects = ["ci-status:published"]
        self.status = "success"
        self.assertEqual(self.tick()["observed"], 1)
        self.assertTrue(ci_waits.evidence(self.store, item))


class ChangePublicationSafetyTests(CiWaitFixture):
    """Reuse the exact receipt/candidate race checks against the new mode."""
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, vcs={**self.config.vcs,
            "ci_branch": "published", "change_stream_enabled": True})
        self.project = Project(self.config, "sample")
        self.store = self.project.work_items
        self.snapshot = self.project.snapshots.get()
        self.wait = {"kind": "publication_ci", "snapshot_id": self.snapshot["id"],
                     "checks": ["unit", "lint"]}
        self.subjects = ["ci-status:published"]

    receipt_for = existing.PublicationCiWaitTests.receipt_for
    descendant = existing.PublicationCiWaitTests.descendant
    publish_descendant = existing.PublicationCiWaitTests.publish_descendant
    test_receipt_ambiguity_during_status_read_cannot_publish_result = (
        existing.PublicationCiWaitTests.test_receipt_ambiguity_during_status_read_cannot_publish_result)
    test_unknown_status_is_pending_even_when_required_rows_appear_green = (
        existing.PublicationCiWaitTests.test_unknown_status_is_pending_even_when_required_rows_appear_green)
    test_mismatched_receipts_unknown_or_ambiguous_scans_never_query_status = (
        existing.PublicationCiWaitTests.test_mismatched_receipts_unknown_or_ambiguous_scans_never_query_status)

    def poll(self, rows=None, reader=None):
        self.clock += 30
        if reader is None:
            reader = Mock(return_value={"checks": self.rows() if rows is None else rows})
        changes = Mock(return_value={"subjects": self.subjects, "next_cursor": "stationary"})
        result = ci_waits.poll(self.config, self.project, self.state, now=self.clock,
                              interval=15, reader=reader, changes_reader=changes)
        self.assertLessEqual(changes.call_count, 1)
        return result, reader

    def test_new_descendant_after_empty_page_is_enrolled(self):
        item = self.block()
        self.receipt_for()
        self.subjects = []
        self.poll(self.rows("pending"))
        self.assertEqual(self.poll()[0]["polled"], 0)
        child = self.descendant("new child")
        self.publish_descendant(child, sha="d" * 40, source="e" * 40)
        result, reader = self.poll(rows=self.rows(sha="d" * 40))
        self.assertEqual(result["observed"], 1)
        reader.assert_called_once_with(self.config.vcs, "status", {"branch": "published", "sha": "d" * 40})
        self.assertEqual(ci_waits.evidence(self.store, item)[0]["payload"]["observed_snapshot"]["snapshot_id"],
                         child["id"])

    def test_changed_candidate_during_status_read_cannot_publish(self):
        item = self.block()
        self.receipt_for()
        original = ci_waits._candidate_wait
        changed = False
        def candidate(*args, **kwargs):
            return None if changed else original(*args, **kwargs)
        def reader(*args):
            nonlocal changed
            changed = True
            return {"checks": self.rows()}
        with patch.object(ci_waits, "_candidate_wait", side_effect=candidate):
            self.assertEqual(self.poll(reader=reader)[0]["observed"], 0)
        self.assertFalse(ci_waits._path(self.store, item).exists())
