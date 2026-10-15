"""Internal work-item seeds, append-only CAS, evidence and fail-closed reads."""
import concurrent.futures
import copy
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

from support import Fixture
from mizu.errors import Denied, LimitExceeded
from mizu.fs import PREVIEW_BYTES, canonical, write_json
from mizu.work_items import (MAX_OPEN_ROW_BYTES, MAX_PROJECTION_BYTES, MAX_REVISIONS, WorkItems,
                             _entry_stat)


class WorkItemTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = self.project.work_items
        self.proposal = self.project.insights.submit(
            source="worker", title="Bounded lifecycle", body="Implement a core slice", base_snapshot=None)
        self.decision = self.project.insights.decide(
            self.proposal["id"], "accept", "reviewed", "", "run-1", expected_rev=1)
        self.seed = self.decision["work_item"]
        self.item_id = self.seed["id"]
        self.seed_path = self.project.root / "decision-history" / f"{self.item_id[3:]}.json"
        self.directory = self.project.root / "work-items" / self.item_id
        self.serial = 0

    def change(self, status="doing", expected_rev=1, **kwargs):
        return self.store.transition(self.item_id, expected_rev=expected_rev, status=status,
                                     actor=kwargs.pop("actor", "worker"), **kwargs)

    def snapshot(self, verification=None, publish=True):
        self.serial += 1
        store = self.project.snapshots
        captured = store.capture_files(self.project.workspace)
        if verification == "valid":
            verification = {"passed": True, "code_digest": captured["code_digest"], "run": "run-1"}
        snapshot = store.create(captured, goal="Goal", state="State", run="run-1", outcome="done",
                                summary=f"Snapshot {self.serial}", verification=verification)
        if publish:
            store.publish(snapshot)
        return snapshot

    def test_entry_stat_uses_full_windows_link_metadata(self):
        entry = Mock(path=r"C:\store\entry", stat=Mock())
        expected = object()
        with patch("mizu.work_items.IS_WINDOWS", True), \
                patch("mizu.work_items.os.stat", return_value=expected) as path_stat:
            self.assertIs(_entry_stat(entry), expected)
        path_stat.assert_called_once_with(entry.path, follow_symlinks=False)
        entry.stat.assert_not_called()

    def test_seed_lookup_is_direct_and_does_not_use_mutable_insight(self):
        self.assertIs(self.store.insights, self.project.insights)
        self.assertIs(self.store.snapshots, self.project.snapshots)
        # New insight revisions must not rewrite the seed's title or origin.
        self.project.insights.revise(self.proposal["id"], source="worker", title="New title",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        read = self.store._read
        with patch.object(self.store, "_read", wraps=read) as reads, \
                patch.object(self.project.insights, "read", side_effect=AssertionError("mutable lookup")), \
                patch("mizu.work_items.os.scandir", side_effect=AssertionError("history scan")):
            self.assertEqual(self.store.get(self.item_id), self.seed)
        reads.assert_called_once_with(f"decision-history/{self.item_id[3:]}.json")
        self.assertFalse(self.directory.exists())

    def test_projection_marks_revision_stale_until_current_decision(self):
        view = self.store.projection(self.item_id)
        self.assertEqual(view["item"], self.seed)
        self.assertEqual(view["current_origin_rev"], 1)
        self.assertFalse(view["stale"])
        self.assertIsNone(view["redecision"])
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        pending = self.store.projection(self.item_id)
        self.assertEqual(pending["current_origin_rev"], 2)
        self.assertTrue(pending["stale"])
        self.assertIsNone(pending["redecision"])
        decision = self.project.insights.decide(
            self.proposal["id"], "defer", "Reassess after new evidence", "revisit the revised scope",
            "run-redecision", expected_rev=2)
        settled = self.store.projection(self.item_id)
        self.assertEqual(settled["current_origin_rev"], 2)
        self.assertFalse(settled["stale"])
        self.assertEqual(settled["redecision"], {"rev": 2, "action": "defer"})
        self.assertEqual(settled["item"]["origin"], self.seed["origin"])
        self.assertEqual(self.store.get(self.item_id), self.seed)
        self.assertEqual(decision["action"], "defer")

    def test_projection_rejects_incomplete_current_decision(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        path = self.project.root / "decisions" / f"{self.proposal['id']}.json"
        write_json(path, {"id": self.proposal["id"], "rev": 2, "action": "accept"})
        with self.assertRaises(Denied):
            self.store.projection(self.item_id)

    def test_projection_validates_current_accept_seed_before_clearing_stale(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        self.project.insights.decide(self.proposal["id"], "accept", "reviewed again", "",
                                     "run-redecision", expected_rev=2)
        view = self.store.projection(self.item_id)
        self.assertFalse(view["stale"])
        self.assertEqual(view["redecision"], {"rev": 2, "action": "accept"})
        path = self.project.root / "decisions" / f"{self.proposal['id']}.json"
        record = json.loads(path.read_text())
        record["work_item"]["title"] = None
        write_json(path, record)
        with self.assertRaises(Denied):
            self.store.projection(self.item_id)

    def test_projection_keeps_withdrawn_revision_stale(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        withdrawal = self.project.insights.withdraw(
            self.proposal["id"], source="worker", reason="Withdraw revised proposal", expected_rev=2)
        self.assertIsNone(withdrawal["run"])
        view = self.store.projection(self.item_id)
        self.assertTrue(view["stale"])
        self.assertIsNone(view["redecision"])

    def test_projection_treats_missing_origin_as_stale(self):
        (self.project.root / "inbox" / f"{self.proposal['id']}.json").unlink()
        view = self.store.projection(self.item_id)
        self.assertIsNone(view["current_origin_rev"])
        self.assertTrue(view["stale"])

    def test_projection_is_byte_bounded(self):
        event = self.change(reason="r" * (PREVIEW_BYTES - 2048))
        view = self.store.projection(self.item_id)
        self.assertEqual(view["item"], event)
        self.assertLessEqual(len(canonical(view)), MAX_PROJECTION_BYTES)

    def test_projection_rejects_symlinked_current_decision(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        path = self.project.root / "decisions" / f"{self.proposal['id']}.json"
        target = self.project.root / "saved-decision.json"
        path.rename(target)
        self.symlink(target, path)
        with self.assertRaises(Denied):
            self.store.projection(self.item_id)

    def test_projection_rejects_symlinked_origin(self):
        path = self.project.root / "inbox" / f"{self.proposal['id']}.json"
        target = self.project.root / "saved-origin.json"
        path.rename(target)
        self.symlink(target, path)
        with self.assertRaises(Denied):
            self.store.projection(self.item_id)

    def test_seed_decision_is_bounded_before_persisting(self):
        proposal = self.project.insights.submit(
            source="worker", title="Oversized decision", body="Check event bound", base_snapshot=None)
        history = set((self.project.root / "decision-history").glob("*.json"))
        with self.assertRaises(LimitExceeded):
            self.project.insights.decide(
                proposal["id"], "accept", "x" * PREVIEW_BYTES, "", "run-oversized", expected_rev=1)
        self.assertEqual(set((self.project.root / "decision-history").glob("*.json")), history)
        self.assertFalse((self.project.root / "decisions" / f"{proposal['id']}.json").exists())
        self.assertFalse((self.project.root / "work-items").exists())

    def test_modify_seed_works(self):
        modified = self.project.insights.decide(self.proposal["id"], "modify", "smaller scope", "",
                                                "run-2", expected_rev=1)
        self.assertEqual(self.store.get(modified["work_item"]["id"]), modified["work_item"])

    def test_full_state_events_preserve_seed_and_reconstruct_without_pointer(self):
        original = self.seed_path.read_bytes()
        doing = self.change()
        self.assertEqual(doing["holder"], "worker")
        self.assertEqual(doing["rev"], 2)
        self.assertEqual(doing["actor"], "worker")
        self.assertEqual(doing["transition"], {"from": "open", "to": "doing"})
        opened = self.change("open", expected_rev=2, actor="reviewer", reason="Release holder")
        self.assertIsNone(opened["holder"])
        for key in ("origin", "title", "created_at"):
            self.assertEqual(opened[key], self.seed[key])
        for key in ("wait", "snapshot_id", "verification"):
            self.assertIsNone(opened[key])
        self.assertEqual(self.seed_path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["2.json", "3.json"])
        self.assertEqual(json.loads((self.directory / "2.json").read_bytes()), doing)
        reopened = WorkItems(self.project.root, self.project.snapshots, self.project.insights)
        self.assertEqual(reopened.get(self.item_id), opened)

    def test_stale_and_invalid_revisions_write_nothing(self):
        self.change()
        original = (self.directory / "2.json").read_bytes()
        for revision in (1, 0, -1, True, False, 2.0, "2", None):
            with self.subTest(revision=revision), self.assertRaises(Denied):
                self.change("open", expected_rev=revision)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["2.json"])
        self.assertEqual((self.directory / "2.json").read_bytes(), original)

    def test_unknown_and_malformed_ids_fail_closed(self):
        for item_id in (None, 1, "", "wi_" + "f" * 32, "wi_" + "A" * 32,
                        "wi_" + "a" * 31, "wi_" + "a" * 33, "../decision-history/a", self.proposal["id"]):
            with self.subTest(item_id=item_id):
                with self.assertRaises(Denied):
                    self.store.get(item_id)
                with self.assertRaises(Denied):
                    self.store.transition(item_id, expected_rev=1, status="doing", actor="worker")
        self.assertFalse(self.directory.exists())

    def test_malformed_seed_records_fail_closed(self):
        cases = []
        for key, value in (("id", "other"), ("title", ""), ("title", 1), ("origin", {}),
                           ("origin", {"insight_id": self.proposal["id"], "rev": True}),
                           ("rev", True), ("rev", 2), ("status", "done"), ("holder", "worker"),
                           ("created_at", "bad"), ("updated_at", "bad")):
            record = copy.deepcopy(self.decision)
            record["work_item"][key] = value
            cases.append(record)
        for key, value in (("action", "defer"), ("id", "other"), ("rev", 2), ("rev", True),
                           ("created_at", "bad"), ("work_item", None)):
            record = copy.deepcopy(self.decision)
            record[key] = value
            cases.append(record)
        for record in cases + [[], {}, None]:
            with self.subTest(record=record):
                write_json(self.seed_path, record)
                with self.assertRaises(Denied):
                    self.store.get(self.item_id)
                with self.assertRaises(Denied):
                    self.change()
        self.seed_path.write_bytes(b"{not-json")
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        self.seed_path.write_bytes(b" " * (PREVIEW_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            self.store.get(self.item_id)
        self.assertFalse(self.directory.exists())

    def test_blocked_requires_and_normalizes_wait_without_scheduler_registration(self):
        for wait in (None, "later", {}, {"kind": "unknown"}, {"kind": "deadline", "at": "2030-01-01"}):
            with self.subTest(wait=wait), self.assertRaises(Denied):
                self.change("blocked", wait=wait)
        self.assertFalse(self.directory.exists())
        blocked = self.change("blocked", actor="reviewer",
                              wait={"kind": "deadline", "at": "2030-01-01T00:00:00.123+09:00"})
        self.assertEqual(blocked["wait"], {"kind": "deadline", "at": "2030-01-01T00:00:00+09:00"})
        self.assertEqual(blocked["holder"], "reviewer")
        self.assertEqual(self.store.get(self.item_id), blocked)
        self.assertFalse((self.project.root / "waits").exists())
        doing = self.change("doing", expected_rev=2)
        self.assertEqual(doing["holder"], "worker")
        self.assertIsNone(doing["wait"])
        blocked = self.change("blocked", expected_rev=3, actor="reviewer",
                              wait={"kind": "insight_decided", "insight": self.proposal["id"]})
        self.assertEqual(blocked["holder"], "worker")
        self.assertEqual(blocked["wait"], {"kind": "insight_decided", "target": self.proposal["id"]})
        self.assertEqual(self.store.get(self.item_id), blocked)

    def test_code_change_wait_keeps_original_baseline(self):
        baseline = self.project.snapshots.get()["code_digest"]
        blocked = self.change("blocked", wait={"kind": "code_change"})
        self.assertEqual(blocked["wait"], {"kind": "code_change", "digest": baseline})
        with patch.object(self.project.insights, "project_snapshots_digest", side_effect=AssertionError("rebase")):
            self.assertEqual(self.store.get(self.item_id), blocked)

    def test_dependency_wait_uses_existing_validator(self):
        wait = {"kind": "dependency", "recipient": "reviewer",
                "requires": {"insight": self.proposal["id"], "rev": 1, "action": "accept"}}
        blocked = self.change("blocked", wait=wait)
        self.assertEqual(blocked["wait"], wait)
        self.assertEqual(self.store.get(self.item_id), blocked)

    def test_done_rejects_missing_unpublished_unverified_failed_and_mismatched_evidence(self):
        candidates = [None, "bad", "f" * 64, self.snapshot("valid", publish=False)["id"],
                      self.snapshot()["id"], self.snapshot({"passed": False, "code_digest": "a" * 64})["id"],
                      self.snapshot({"passed": True, "code_digest": "a" * 64})["id"],
                      self.snapshot({"passed": 1, "code_digest": self.project.snapshots.get()["code_digest"]})["id"]]
        for sid in candidates:
            with self.subTest(sid=sid), self.assertRaises(Denied):
                self.change("done", snapshot_id=sid)
        self.assertFalse(self.directory.exists())

    def test_done_requires_retained_history_not_just_an_existing_manifest(self):
        old = self.snapshot("valid")
        self.project.snapshots.history_index = 1
        self.snapshot("valid")
        self.assertEqual(self.project.snapshots.get(old["id"]), old)
        with self.assertRaises(Denied):
            self.change("done", snapshot_id=old["id"])
        self.assertFalse(self.directory.exists())

    def test_done_validates_manifest_and_copies_stored_verification(self):
        bad = self.snapshot("valid")
        path = self.project.snapshots.root / "snapshots" / f"{bad['id']}.json"
        write_json(path, {**bad, "summary": "tampered"})
        with self.assertRaises(Denied):
            self.change("done", snapshot_id=bad["id"])
        write_json(path, bad)
        self.change()
        # Retained published history, not just the moving head, is allowed.
        self.snapshot()
        completed = self.change("done", expected_rev=2, snapshot_id=bad["id"], reason="Verified fix")
        self.assertIsNone(completed["holder"])
        self.assertEqual(completed["snapshot_id"], bad["id"])
        self.assertEqual(completed["verification"], bad["verification"])
        self.project.snapshots.history_index = 1
        self.snapshot()
        self.assertEqual(self.store.get(self.item_id), completed)
        for status in ("open", "doing", "blocked", "done", "dropped"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.change(status, expected_rev=3, reason="Reopen")
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["2.json", "3.json"])

    def test_reconstruction_rejects_completion_copy_mismatched_to_snapshot(self):
        snapshot = self.snapshot("valid")
        completed = self.change("done", snapshot_id=snapshot["id"])
        path = self.directory / "2.json"
        tampered = copy.deepcopy(completed)
        tampered["verification"]["code_digest"] = "a" * 64
        write_json(path, tampered)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        # The actual manifest is also revalidated on reconstruction.
        write_json(path, completed)
        manifest = self.project.snapshots.root / "snapshots" / f"{snapshot['id']}.json"
        saved = json.loads(manifest.read_bytes())
        write_json(manifest, {**saved, "summary": "tampered"})
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        write_json(manifest, saved)
        self.assertEqual(self.store.get(self.item_id), completed)

    def test_drop_requires_nonempty_reason_and_is_terminal(self):
        for reason in (None, "", " \n", 1):
            with self.subTest(reason=reason), self.assertRaises(Denied):
                self.change("dropped", reason=reason)
        self.assertFalse(self.directory.exists())
        dropped = self.change("dropped", reason="Superseded by a smaller goal")
        self.assertIsNone(dropped["holder"])
        self.assertEqual(dropped["reason"], "Superseded by a smaller goal")
        self.assertEqual(self.store.get(self.item_id), dropped)
        for status in ("open", "doing", "blocked", "done", "dropped"):
            with self.subTest(status=status), self.assertRaises(Denied):
                self.change(status, expected_rev=2, reason="Another decision")

    def test_invalid_status_actor_and_irrelevant_evidence_do_not_write(self):
        for args in ({"status": "open"}, {"status": "unknown"}, {"status": None},
                     {"actor": "../worker"}, {"actor": None}, {"reason": 1},
                     {"wait": {"kind": "code_change"}}, {"snapshot_id": self.project.snapshots.get()["id"]}):
            with self.subTest(args=args), self.assertRaises(Denied):
                self.change(**args)
        self.assertFalse(self.directory.exists())

    def test_oversized_transition_writes_nothing(self):
        with self.assertRaises(LimitExceeded):
            self.change(reason="x" * PREVIEW_BYTES)
        self.assertFalse(self.directory.exists())

    def test_history_gap_bad_names_malformed_and_oversized_events_fail_closed(self):
        event = self.change()
        path = self.directory / "2.json"
        mutations = [{**event, "rev": True}, {**event, "rev": 3}, {**event, "title": "changed"},
                     {**event, "holder": None}, {**event, "transition": {}},
                     {**event, "origin": {"insight_id": self.proposal["id"], "rev": True}},
                     {**event, "status": "blocked", "wait": {}}, {"rev": 2}, []]
        for record in mutations:
            with self.subTest(record=record):
                write_json(path, record)
                with self.assertRaises(Denied):
                    self.store.get(self.item_id)
                with self.assertRaises(Denied):
                    self.change("open", expected_rev=2)
        path.write_bytes(b" " * (PREVIEW_BYTES + 1))
        with self.assertRaises(LimitExceeded):
            self.store.get(self.item_id)
        path.unlink()
        for name in ("3.json", "257.json", "02.json", "current.json", ".new-temp"):
            with self.subTest(name=name):
                write_json(self.directory / name, event)
                with self.assertRaises(Denied):
                    self.store.get(self.item_id)
                (self.directory / name).unlink()

    def test_revision_bound_includes_seed(self):
        template = self.change()
        previous = self.seed
        for rev in range(2, MAX_REVISIONS + 1):
            status = "doing" if rev % 2 == 0 else "open"
            event = {**template, "rev": rev, "status": status,
                     "holder": "worker" if status == "doing" else None,
                     "transition": {"from": previous["status"], "to": status}}
            write_json(self.directory / f"{rev}.json", event)
            previous = event
        self.assertEqual(self.store.get(self.item_id)["rev"], MAX_REVISIONS)
        with self.assertRaises(Denied):
            self.change("open", expected_rev=MAX_REVISIONS)
        self.assertFalse((self.directory / f"{MAX_REVISIONS + 1}.json").exists())
        write_json(self.directory / f"{MAX_REVISIONS + 1}.json", template)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)

    def symlink(self, target, link, **kwargs):
        try:
            os.symlink(target, link, **kwargs)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")

    def test_interrupted_orphan_temporary_is_removed_without_promoting_it(self):
        self.directory.mkdir(parents=True)
        temporary = self.directory / ".new-leftover"
        temporary.write_bytes(b"incomplete write")
        self.assertEqual(self.store.get(self.item_id), self.seed)
        self.assertFalse(temporary.exists())
        self.assertEqual(self.change()["status"], "doing")

    def test_interrupted_post_link_temporary_is_removed_before_read(self):
        committed = self.change()
        event_path = self.directory / "2.json"
        temporary = self.directory / ".new-leftover"
        try:
            os.link(event_path, temporary)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        self.assertEqual(event_path.stat().st_nlink, 2)
        self.assertEqual(self.store.get(self.item_id), committed)
        self.assertFalse(temporary.exists())
        self.assertEqual(event_path.stat().st_nlink, 1)

    def test_atomic_temporary_symlink_fails_closed(self):
        self.directory.mkdir(parents=True)
        temporary = self.directory / ".new-leftover"
        target = self.project.root / "saved-temporary"
        target.write_bytes(b"outside")
        self.symlink(target, temporary)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        self.assertTrue(target.is_file())

    def test_symlink_seed_fails_closed(self):
        target = self.project.root / "saved-seed.json"
        self.seed_path.rename(target)
        self.symlink(target, self.seed_path)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change()

    def test_symlink_transition_fails_closed(self):
        self.change()
        path = self.directory / "2.json"
        target = self.project.root / "saved-transition.json"
        path.rename(target)
        self.symlink(target, path)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change("open", expected_rev=2)

    def test_symlink_history_directory_fails_closed(self):
        target = self.project.root / "other-items"
        target.mkdir()
        self.symlink(target, self.directory.parent, target_is_directory=True)
        with self.assertRaises(Denied):
            self.store.get(self.item_id)
        with self.assertRaises(Denied):
            self.change()
        self.assertEqual(list(target.iterdir()), [])

    def test_page_open_checks_missing_work_item_history_directory_once(self):
        from mizu.work_items import _directory
        with patch("mizu.work_items._directory", wraps=_directory) as checks:
            self.store.page_open()
        matching = [call for call in checks.call_args_list if call.args[0] == self.directory.parent]
        self.assertEqual(len(matching), 1)

    def test_page_open_reuses_scanned_seed_event_without_rereading_it(self):
        path = f"decision-history/{self.item_id[3:]}.json"
        read = self.store._read
        with patch.object(self.store, "_read", wraps=read) as reads:
            self.store.page_open()
        self.assertEqual(sum(call.args[0] == path for call in reads.call_args_list), 1)

    def test_page_open_still_validates_scanned_seed_fields(self):
        event = json.loads(self.seed_path.read_bytes())
        event["work_item"]["status"] = "doing"
        write_json(self.seed_path, event)
        with self.assertRaises(Denied):
            self.store.page_open()

    def test_page_open_enumerates_deterministic_exclusive_pages(self):
        extra_ids = []
        for index in range(4):
            proposal = self.project.insights.submit(
                source="worker", title=f"Page item {index}", body="Enumerate this item", base_snapshot=None)
            decision = self.project.insights.decide(
                proposal["id"], "accept", "reviewed", "", f"page-{index}", expected_rev=1)
            extra_ids.append(decision["work_item"]["id"])
        expected = sorted([self.item_id, *extra_ids])
        seen = []
        cursor = None
        while True:
            page = self.store.page_open(cursor=cursor, limit=2)
            self.assertEqual(page["total"], len(expected))
            self.assertTrue(page["coverage_complete"])
            ids = [row["id"] for row in page["items"]]
            self.assertEqual(ids, sorted(ids))
            seen.extend(ids)
            if not page["truncated"]:
                self.assertIsNone(page["next_cursor"])
                break
            self.assertEqual(page["next_cursor"], ids[-1])
            cursor = page["next_cursor"]
        self.assertEqual(seen, expected)
        self.assertEqual(len(seen), len(set(seen)))

    def test_page_open_live_cursor_does_not_offset_skip_after_earlier_close(self):
        extra_ids = []
        for index in range(3):
            proposal = self.project.insights.submit(
                source="worker", title=f"Live page item {index}", body="Enumerate this item", base_snapshot=None)
            decision = self.project.insights.decide(
                proposal["id"], "accept", "reviewed", "", f"live-page-{index}", expected_rev=1)
            extra_ids.append(decision["work_item"]["id"])
        expected = sorted([self.item_id, *extra_ids])
        first = self.store.page_open(limit=2)
        self.assertEqual([row["id"] for row in first["items"]], expected[:2])
        self.assertEqual(first["next_cursor"], expected[1])
        self.store.transition(expected[0], expected_rev=1, status="dropped", actor="worker",
                              reason="Closed after first live page")
        second = self.store.page_open(cursor=first["next_cursor"], limit=2)
        self.assertEqual(second["total"], 3)
        self.assertEqual([row["id"] for row in second["items"]], expected[2:])
        self.assertFalse(second["truncated"])

    def test_page_open_validates_cursor_and_exact_integer_limit(self):
        for cursor in ("", "wi_" + "A" * 32, "../anything", 1, True):
            with self.subTest(cursor=cursor), self.assertRaises(Denied):
                self.store.page_open(cursor=cursor)
        for limit in (0, -1, 101, True, False, 1.0, "2", None):
            with self.subTest(limit=limit), self.assertRaises(Denied):
                self.store.page_open(limit=limit)
        self.assertEqual(self.store.page_open(limit=1)["items"][0]["id"], self.item_id)
        self.assertEqual(self.store.page_open(cursor="wi_" + "f" * 32, limit=100)["items"], [])

    def test_page_open_includes_current_stale_projection(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised page title",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        page = self.store.page_open()
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual(row["current_origin_rev"], 2)
        self.assertTrue(row["stale"])
        self.assertIsNone(row["redecision"])

    def test_page_open_includes_blocked_wait_and_bounded_reason(self):
        # Control characters expand when escaped in the canonical JSON row;
        # the documented row bound accommodates the full byte-capped preview.
        reason = "\x01" * 1200
        self.change("blocked", reason=reason,
                    wait={"kind": "deadline", "at": "2030-01-01T00:00:00+00:00"})
        page = self.store.page_open()
        row = next(row for row in page["items"] if row["id"] == self.item_id)
        self.assertEqual(row["wait"], {"kind": "deadline", "at": "2030-01-01T00:00:00+00:00"})
        self.assertEqual(len(row["reason_preview"].encode("utf-8")), 1024)
        self.assertTrue(row["reason_truncated"])
        self.assertLessEqual(len(canonical(row)), MAX_OPEN_ROW_BYTES)

    def test_page_open_reports_legacy_unseeded_decisions_and_closed_items(self):
        legacy = copy.deepcopy(self.decision)
        legacy.pop("work_item")
        legacy.pop("rev")
        legacy.pop("created_at")
        legacy_id = "a" * 32
        write_json(self.project.root / "decision-history" / f"{legacy_id}.json", legacy, exclusive=True)
        self.change("dropped", reason="No longer active")
        page = self.store.page_open()
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["unseeded_decisions"], 1)
        self.assertFalse(page["coverage_complete"])

    def test_page_open_recovers_post_link_decision_history_temporary(self):
        history = self.project.root / "decision-history"
        temporary = history / ".new-alias123"
        try:
            os.link(self.seed_path, temporary)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        self.assertEqual(self.seed_path.stat().st_nlink, 2)
        page = self.store.page_open()
        self.assertEqual([row["id"] for row in page["items"]], [self.item_id])
        self.assertFalse(temporary.exists())
        self.assertEqual(self.seed_path.stat().st_nlink, 1)

    def test_page_open_recovers_current_redecision_alias_before_projection(self):
        self.project.insights.revise(self.proposal["id"], source="worker", title="Revised scope",
                                     body="Changed claims", base_snapshot=None, expected_rev=1)
        decision = self.project.insights.decide(
            self.proposal["id"], "accept", "reassessed", "", "run-redecision", expected_rev=2)
        history = self.project.root / "decision-history"
        event_path = history / f"{decision['work_item']['id'][3:]}.json"
        temporary = history / ".new-redec123"
        try:
            os.link(event_path, temporary)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        page = self.store.page_open(limit=10)
        self.assertEqual(page["total"], 2)
        self.assertEqual([row["id"] for row in page["items"]],
                         sorted((self.item_id, decision["work_item"]["id"])))
        self.assertFalse(temporary.exists())
        self.assertEqual(event_path.stat().st_nlink, 1)

    def test_page_open_fails_closed_on_malformed_committed_history_and_ignores_temporary(self):
        history = self.project.root / "decision-history"
        temporary = history / ".new-leftover"
        temporary.write_bytes(b"uncommitted")
        self.assertEqual([row["id"] for row in self.store.page_open()["items"]], [self.item_id])
        self.assertFalse(temporary.exists())
        unknown = history / "unexpected.data"
        unknown.write_text("not a committed event")
        with self.assertRaises(Denied):
            self.store.page_open()
        unknown.unlink()
        bad = history / ("b" * 32 + ".json")
        bad.write_bytes(b"{not-json")
        with self.assertRaises(Denied):
            self.store.page_open()

    def test_page_open_rejects_unmatched_decision_history_hard_link(self):
        history = self.project.root / "decision-history"
        outside = self.project.root / "outside-decision-event"
        outside.write_bytes(b"not a committed event")
        temporary = history / ".new-extern12"
        try:
            os.link(outside, temporary)
        except (OSError, NotImplementedError):
            self.skipTest("Hard links unavailable")
        with self.assertRaisesRegex(Denied, "Unrecognized decision-history temporary hard link"):
            self.store.page_open()
        self.assertTrue(temporary.exists())
        self.assertEqual(outside.read_bytes(), b"not a committed event")

    def test_concurrent_same_revision_has_one_winner(self):
        barrier = threading.Barrier(2)

        def attempt(actor):
            store = WorkItems(self.project.root, self.project.snapshots, self.project.insights)
            barrier.wait(timeout=5)
            try:
                return store.transition(self.item_id, expected_rev=1, status="doing", actor=actor)
            except Denied:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("worker", "reviewer")))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get(self.item_id), winners[0])
        self.assertEqual([p.name for p in self.directory.iterdir()], ["2.json"])


if __name__ == "__main__":
    unittest.main()
